"""成本采集任务：BSS T+1 每日成本拉取入 cost_daily。

- 每日 10:00（可配）拉 T-1；手动触发可指定日期或回填最近 N 天；
- 幂等：重拉某日时按 (stat_date, source) 整体覆盖；
- 两个来源：bill（账单明细口径）+ amortized（包年包月摊销，未开通时软跳过并提示）；
- 与指标采集/告警评估同款互斥锁 + 状态字典模式。
- 腾讯账单走 run_tencent_collection（source='tc_bill'，collector/tc_bss.py），与阿里
  同口径写入 cost_daily；阿里凭证缺失软跳过，支持腾讯单独部署。腾讯数据的删除范围
  限定为 (stat_date, source='tc_bill', account_id)，不得触碰阿里行。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.collector.bss import BSSError, aggregate_amortized, aggregate_bill, fetch_day_amortized, fetch_day_bill
from app.collector.credential import resolve_aliyun_credential
from app.core.config import settings
from app.models.asset import Asset
from app.models.cost_daily import CostDaily
from app.services.cost_anomaly import detect_cost_for_date, snapshot_and_detect_packs

logger = logging.getLogger("qianshu.cost_job")

_run_lock = threading.Lock()
_last_status: dict = {
    "running": False,
    "last_started_at": None,
    "last_finished_at": None,
    "last_duration_s": None,
    "last_dates": [],        # 最近一轮拉的日期
    "rows": 0,
    "total_amount": 0.0,
    "amortized_active": None,  # 摊销成本是否可用（None=当前执行周期未尝试）
    "notes": [],              # 软提示（如摊销未开通）
    "errors": [],
    "anomalies": 0,           # 当前执行周期新检出的成本/资源包异动条数
}


def get_status() -> dict:
    return dict(_last_status)


def guard_bill_staleness(db: Session) -> dict:
    """账单断供守卫：bill/tc_bill 历史非空但停更 ≥2 天时，每天发送一封站内信。

    凭证缺失的软跳过可能掩盖既有账单通道断供，因此用历史数据区分从未接入与采集中断；
    无账单历史时不提醒，同日同题只发一封。守卫自行 commit，独立通知写不连累采集事务。
    """
    from datetime import datetime as _dt, time as _time
    from app.models.in_app_message import InAppMessage
    from app.services.notify import notify_inapp

    last = db.query(func.max(CostDaily.stat_date)).filter(
        CostDaily.source.in_(("bill", "tc_bill"))).scalar()
    if last is None:
        return {"notified": 0, "reason": "无账单史（未接入或纯新部署），不打扰"}
    stale_days = (date.today() - last).days
    if stale_days < 2:
        return {"notified": 0, "reason": f"账单新鲜（最后 {last.isoformat()}）"}

    title = f"账单采集断供 {stale_days} 天（最后 {last.isoformat()}）"
    day_start = _dt.combine(date.today(), _time.min)
    dup = db.query(InAppMessage.id).filter(
        InAppMessage.title == title,
        InAppMessage.created_at >= day_start).first()
    if dup:
        return {"notified": 0, "reason": "今日已提醒"}

    n = notify_inapp(
        db, title,
        f"账单数据停更 **{stale_days} 天**（最后账单日 {last.isoformat()}，T+1 账单）。\n\n"
        "影响：总览「昨日按量付费」、成本异动、资源包耗尽预测、到期总账全部拿不到新数据。\n\n"
        "**处理**：到「云账号管理」检查账号是否启用、凭证连通性测试是否通过；"
        "或配置环境变量 ALIBABA_CLOUD_ACCESS_KEY_ID / ALIBABA_CLOUD_ACCESS_KEY_SECRET（只读 RAM key）。",
        level="warning", msg_type="system")
    try:
        db.commit()
        logger.warning("账单断供守卫触发: 停更 %d 天（最后 %s），站内信 %d 条", stale_days, last, n)
        return {"notified": n, "stale_days": stale_days, "last": last.isoformat()}
    except Exception as e:
        db.rollback()
        logger.error("账单断供守卫写站内信失败: %s", e)
        return {"notified": 0, "error": str(e)[:120]}


def catch_up_needed(db: Session) -> Optional[tuple]:
    """采集窗口：补缺口 + 固定重拉 T-6..T-1，返回 (start, end)。

    T-1 账单当天可能补账或改行，并非终态；月度出账前更早的日期也可能重算。
    因此除补缺口外固定重拉 T-6..T-1，覆盖一周内补账和月初结算波动。账单行按
    (date,product,instance,计费项) 聚合覆盖，重拉保持幂等。

    账号上下文激活时必须按本账号计算缺口，避免其他账号已有的 max(stat_date)
    阻止新账号采集。
    """
    from app.collector.credential import active_account_id

    q = db.query(func.max(CostDaily.stat_date))
    acc = active_account_id()
    if acc is not None:
        q = q.filter(CostDaily.account_id == acc)
    latest = q.scalar()
    yesterday = date.today() - timedelta(days=1)
    refresh_from = yesterday - timedelta(days=6)   # T-6 起重拉（覆盖出账周期修订）
    if latest is None:
        return (yesterday, yesterday)
    start = min(latest + timedelta(days=1), refresh_from)
    return (start, yesterday)


def _asset_name_map(db: Session) -> dict:
    return {a.instance_id: (a.name or a.instance_id) for a in db.query(Asset).all()}


def _collect_one_day(db: Session, ak: str, sk: str, day: date) -> dict:
    """采集一天：bill 必拉；amortized 失败软跳过（不连累账单数据）。返回 {rows, amount, amortized_active}。"""
    rows: list = []
    notes: list = []
    amortized_active: Optional[bool] = None

    bill_items = fetch_day_bill(ak, sk, day, settings.BSS_ENDPOINT)
    rows.extend(aggregate_bill(bill_items))

    try:
        amortized_items = fetch_day_amortized(ak, sk, day, settings.BSS_ENDPOINT)
        rows.extend(aggregate_amortized(amortized_items, _asset_name_map(db)))
        amortized_active = True
    except BSSError as e:
        if e.code == "NotActiveService":
            amortized_active = False
            notes.append("摊销成本未开通（费用与成本中心-成本分析开通后自动纳入）")
        else:
            # 瞬时故障等：软失败，保住账单数据；摊销缺失由错误记录提示手动重采
            notes.append(f"摊销成本拉取失败（可稍后重采该日）: {str(e)[:120]}")

    # 删除范围必须等于写入打标范围（同 mark_missing_deleted 口径）：账号上下文激活时
    # 只删本账号当日行再重插，避免跨账号删除；无上下文时保持整日覆盖的兼容行为。
    from app.collector.credential import active_account_id
    from app.services.tenant import account_id as _acc

    acc = _acc(db)
    q = db.query(CostDaily).filter(CostDaily.stat_date == day)
    cur = active_account_id()
    if cur is not None:
        q = q.filter(CostDaily.account_id == cur)
    q.delete()
    for r in rows:
        db.add(CostDaily(stat_date=day, account_id=acc, **r))
    db.commit()
    # 真实每日成本 = bill按量 + 摊销（bill 的 Subscription 是一次性订单事件不计入）
    amount = sum(
        r["pretax_amount"] for r in rows
        if r["source"] == "amortized" or r["subscription_type"] == "PayAsYouGo"
    )
    return {
        "rows": len(rows),
        "amount": round(amount, 4),
        "amortized_active": amortized_active,
        "notes": notes,
    }


def collect_account_days(db: Session, account, dates: list[date]) -> dict:
    """新手接入专用：显式账号逐日采集，不触发异动、包快照或通知。

    凭证上下文在本执行线程内建立；每日独立提交，后续日期失败不回滚已提交日期。
    """
    from app.collector.credential import account_credential
    from app.core.security import decrypt_secret

    if account is None or account.vendor != "aliyun" or not account.enabled:
        raise ValueError("账单账号不存在、已停用或不是阿里云账号")
    unique_dates = list(dict.fromkeys(dates))
    if not 1 <= len(unique_dates) <= 31:
        raise ValueError("单次账单采集必须限定为 1~31 个账单日")
    today = date.today()
    if any(day >= today for day in unique_dates):
        raise ValueError("账单日必须早于今天")
    if not _run_lock.acquire(blocking=False):
        return {"account_id": account.id, "lock_waiting": True, "days": []}
    try:
        ak = decrypt_secret(account.ak_cipher) if account.ak_cipher else ""
        sk = decrypt_secret(account.secret_cipher) if account.secret_cipher else ""
        if not ak or not sk:
            raise ValueError("该账号缺少账单采集凭证")
        results = []
        with account_credential(ak, sk, account.id, source=f"cloud_account:{account.name}"):
            for day in unique_dates:
                try:
                    collected = _collect_one_day(db, ak, sk, day)
                    amortized_active = collected.get("amortized_active")
                    results.append({
                        "bill_date": day.isoformat(), "ok": True,
                        **collected,
                        "reason_code": ("" if amortized_active else
                                        "amortized_unavailable" if amortized_active is False
                                        else "amortized_incomplete"),
                        # 非 NotActiveService 的摊销失败可在限定次数内重试该日。
                        "retryable": amortized_active is None,
                        "error": "",
                    })
                except Exception as exc:
                    db.rollback()
                    text = str(exc)
                    lowered = text.lower()
                    code = getattr(exc, "code", "")
                    denied = (code in ("NoPermission", "Forbidden", "Unauthorized")
                              or "403" in text or "permission" in lowered or "forbidden" in lowered)
                    not_active = code == "NotActiveService"
                    results.append({
                        "bill_date": day.isoformat(), "ok": False, "rows": 0,
                        "amount": None, "amortized_active": None, "notes": [],
                        "reason_code": ("permission_denied" if denied else
                                        "service_not_active" if not_active else "collection_failed"),
                        "retryable": not (denied or not_active),
                        "error": text[:200],
                    })
        return {"account_id": account.id, "lock_waiting": False, "days": results}
    finally:
        _run_lock.release()


def _iter_dates(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def run_collection(db: Session, days: int = 1, start: Optional[date] = None, end: Optional[date] = None) -> dict:
    """采集成本。默认 T-1 一天；days>1 回填最近 N 天（不含今天）；也可显式 start/end。"""
    if not _run_lock.acquire(blocking=False):
        return {**dict(_last_status), "skipped": "上一轮成本采集仍在进行"}
    try:
        _last_status["running"] = True
        _last_status["last_started_at"] = datetime.now().isoformat()
        _last_status["errors"] = []
        _last_status["notes"] = []
        _last_status["anomalies"] = 0
        started = time.monotonic()
        from app.collector.credential import CredentialError

        try:
            ak, sk, _src = resolve_aliyun_credential(db)
        except CredentialError as e:
            # 支持腾讯单独部署：无阿里凭证时软跳过阿里账单，腾讯账单独立采集。
            ak = sk = None
            _last_status["notes"].append(f"{str(e)}（阿里账单跳过，腾讯账单走 tc 通道）")
            # 曾有账单历史的部署不得静默断供；同日同题幂等，并与 main.py
            # 通道空转守卫互不重复提醒。
            guard_bill_staleness(db)

        today = date.today()
        if start or end:
            end = end or today - timedelta(days=1)
            start = start or end
        else:
            end = today - timedelta(days=1)
            start = end - timedelta(days=max(days, 1) - 1)
        dates = [d for d in _iter_dates(start, end) if d < today]
        _last_status["last_dates"] = [d.isoformat() for d in dates]

        total_rows = 0
        total_amount = 0.0
        amortized_active = None
        for d in (dates if ak is not None else []):
            try:
                r = _collect_one_day(db, ak, sk, d)
                total_rows += r["rows"]
                total_amount += r["amount"]
                if r["amortized_active"] is not None:
                    amortized_active = r["amortized_active"]
                for n in r["notes"]:
                    if n not in _last_status["notes"]:
                        _last_status["notes"].append(n)
                logger.info(
                    "成本采集完成 %s: %d 行, 日成本 %.2f", d, r["rows"], r["amount"]
                )
            except BSSError as e:
                msg = f"{d.isoformat()}: {str(e)[:160]}"
                _last_status["errors"].append(msg)
                logger.warning("成本采集失败 %s", msg)
            except Exception as e:
                db.rollback()
                msg = f"{d.isoformat()}: {type(e).__name__} {str(e)[:160]}"
                _last_status["errors"].append(msg)
                logger.exception("成本采集异常 %s", d)

        # 采集完成后自动做异动检测：按量成本逐日检测，资源包快照一天一份。
        if dates:
            anomaly_count = 0
            try:
                yesterday = today - timedelta(days=1)
                for d in dates:
                    # 只有采到 T-1（当日例行）才推送；历史回填只落库不推送
                    new = detect_cost_for_date(db, d, notify=(d == yesterday))
                    anomaly_count += len(new)
                pack_r = snapshot_and_detect_packs(db, notify=True)
                anomaly_count += pack_r.get("new_anomalies", 0)
                logger.info("异动检测完成: 新增 %d 条", anomaly_count)
            except Exception as e:
                db.rollback()
                _last_status["errors"].append(f"异动检测失败: {type(e).__name__} {str(e)[:160]}")
                logger.exception("异动检测失败")
            _last_status["anomalies"] = anomaly_count

        _last_status["running"] = False
        _last_status["last_finished_at"] = datetime.now().isoformat()
        _last_status["last_duration_s"] = round(time.monotonic() - started, 1)
        _last_status["rows"] = total_rows
        _last_status["total_amount"] = round(total_amount, 2)
        _last_status["amortized_active"] = amortized_active
        logger.info(
            "成本采集结束: %s~%s, %d 行, 日成本合计 %.2f, 错误 %d",
            dates[0] if dates else "-", dates[-1] if dates else "-",
            total_rows, total_amount, len(_last_status["errors"]),
        )
        return dict(_last_status)
    finally:
        _run_lock.release()


# ---------------- 腾讯账单采集 ----------------

_tc_run_lock = threading.Lock()
_tc_status: dict = {
    "running": False,
    "last_started_at": None,
    "last_finished_at": None,
    "last_duration_s": None,
    "last_dates": [],
    "rows": 0,
    "total_amount": 0.0,
    "balance": None,      # 账户余额旁注（DescribeAccountBalance，只读）
    "notes": [],
    "errors": [],
    "anomalies": 0,
}


def get_tencent_status() -> dict:
    return dict(_tc_status)


def _tc_gap_dates(db: Session, acc_id: int, days: int, today: date) -> list:
    """腾讯账单采集窗口（与 catch_up_needed 同款 T-6 修订窗口口径，来源限定 tc_bill）。"""
    q = db.query(func.max(CostDaily.stat_date)).filter(CostDaily.source == "tc_bill")
    if acc_id:
        q = q.filter(CostDaily.account_id == acc_id)
    latest = q.scalar()
    yesterday = today - timedelta(days=1)
    refresh_from = yesterday - timedelta(days=6)   # T-6 起重拉（覆盖出账周期修订，同阿里口径）
    if latest is None:
        start = yesterday - timedelta(days=max(days, 1) - 1)
    else:
        start = min(latest + timedelta(days=1), refresh_from)
    return [d for d in _iter_dates(start, yesterday) if d < today]


def _collect_tc_one_day(db: Session, ak: str, sk: str, day: date) -> dict:
    """采集一天腾讯账单：拉取成功才删同日旧 tc_bill 行再重插（清扫守卫）。

    删除范围限定为 (stat_date, source='tc_bill', account_id)——绝不碰阿里 bill/amortized 行
    （多厂商同表，删除范围=写入打标范围，与 _collect_one_day 同款守则）。
    """
    from app.collector.credential import active_account_id
    from app.collector.tc_bss import aggregate_bill, fetch_day_bill

    items = fetch_day_bill(ak, sk, day)      # 失败抛 TCBSSError——外层软失败，旧行保留
    rows = aggregate_bill(items)

    from app.services.tenant import account_id as _acc

    acc = _acc(db)
    q = db.query(CostDaily).filter(CostDaily.stat_date == day, CostDaily.source == "tc_bill")
    cur = active_account_id()
    if cur is not None:
        q = q.filter(CostDaily.account_id == cur)
    q.delete()
    for r in rows:
        db.add(CostDaily(stat_date=day, account_id=acc, **r))
    db.commit()
    return {"rows": len(rows), "amount": round(sum(r["pretax_amount"] for r in rows), 4)}


def run_tencent_collection(db: Session, days: int = 1, start: Optional[date] = None,
                           end: Optional[date] = None, force: bool = False) -> dict:
    """腾讯账单采集：每个启用 tencent 账号一轮（account_credential 上下文打标）。

    - 无腾讯账号：软跳过（note），不报错；
    - 单日拉取失败：软失败记 errors，**不删该日旧行**（失败跳过清扫，同清单清扫守卫口径）；
    - 采集后跑按量成本异动检测（与阿里 run_collection 同款；资源包快照是阿里侧，不在此跑）。
    """
    if not _tc_run_lock.acquire(blocking=False):
        return {**dict(_tc_status), "skipped": "上一轮腾讯账单采集仍在进行"}
    try:
        _tc_status.update(running=True, last_started_at=datetime.now().isoformat(),
                          errors=[], notes=[], anomalies=0, rows=0, total_amount=0.0)
        started = time.monotonic()
        today = date.today()
        explicit = None
        if start or end:
            end = end or today - timedelta(days=1)
            start = start or end
            explicit = [d for d in _iter_dates(start, end) if d < today]

        from app.collector.credential import (
            account_credential_contexts, active_account_id, resolve_tencent_credential,
        )
        from app.collector.tc_bss import TCBSSError, fetch_account_balance

        used_dates: set = set()
        contexts = account_credential_contexts(db, vendor="tencent")
        if not contexts:
            _tc_status["notes"].append("无启用中的腾讯云账号，跳过腾讯账单采集")
        from app.services.monitor_policy import collect_due, mark_collected

        for ctx in contexts:
            with ctx:
                ak, sk, src = resolve_tencent_credential(db)
                acc = active_account_id() or 0
                # 分级监控：medium 2 小时 / low 每天 03:30；手动采集 force=True 不受限
                if not force and not collect_due(db, "cost", acc):
                    continue
                dates = explicit if explicit is not None else _tc_gap_dates(db, acc, days, today)
                used_dates.update(dates)
                try:
                    bal = fetch_account_balance(ak, sk)
                    _tc_status["balance"] = bal
                    if (bal.get("owe") or 0) > 0:
                        _tc_status["notes"].append(f"腾讯云账号欠费 {bal['owe']} 元（{src}）")
                except Exception as e:
                    _tc_status["notes"].append(f"账户余额查询失败: {str(e)[:120]}")
                for d in dates:
                    try:
                        r = _collect_tc_one_day(db, ak, sk, d)
                        _tc_status["rows"] += r["rows"]
                        _tc_status["total_amount"] += r["amount"]
                        logger.info("腾讯账单采集完成 %s: %d 行, %.2f 元", d, r["rows"], r["amount"])
                    except TCBSSError as e:
                        _tc_status["errors"].append(f"{d.isoformat()}: {str(e)[:160]}")
                        logger.warning("腾讯账单采集失败 %s", str(e)[:160])
                    except Exception as e:
                        db.rollback()
                        _tc_status["errors"].append(
                            f"{d.isoformat()}: {type(e).__name__} {str(e)[:160]}")
                        logger.exception("腾讯账单采集异常 %s", d)
                mark_collected(db, "cost", acc)
                db.commit()

        if used_dates:
            anomaly_count = 0
            yesterday = today - timedelta(days=1)
            for d in sorted(used_dates):
                try:
                    # 只有采到 T-1（当日例行）才推送；历史回填只落库不推送（同阿里口径）
                    new = detect_cost_for_date(db, d, notify=(d == yesterday))
                    anomaly_count += len(new)
                except Exception as e:
                    db.rollback()
                    _tc_status["errors"].append(f"异动检测失败 {d}: {str(e)[:120]}")
            _tc_status["anomalies"] = anomaly_count

        _tc_status["running"] = False
        _tc_status["last_finished_at"] = datetime.now().isoformat()
        _tc_status["last_duration_s"] = round(time.monotonic() - started, 1)
        _tc_status["last_dates"] = [d.isoformat() for d in sorted(used_dates)]
        _tc_status["total_amount"] = round(_tc_status["total_amount"], 2)
        logger.info("腾讯账单采集结束: %d 行, 合计 %.2f 元, 错误 %d",
                    _tc_status["rows"], _tc_status["total_amount"], len(_tc_status["errors"]))
        return dict(_tc_status)
    finally:
        _tc_status["running"] = False
        _tc_run_lock.release()
