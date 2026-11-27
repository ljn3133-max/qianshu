"""成本与资源包异动检测。

在每日成本采集完成后自动运行，也可手动触发：
- detect_cost_for_date：按量日成本异动——总额与 产品×实例×计费项 两个粒度，
  环比昨日 / 近7日均值，超阈值（默认 1.5 倍且金额 >= 10 元）记异动；
  新出现且金额达标的费用项记"新增"。
- snapshot_and_detect_packs：资源包余量每日快照 -> 日消耗、消耗突增、
  池级耗尽预测（按抵扣能力池而非单包）、刚耗尽（级别看池）、即将到期仍有余量。

检测与提醒口径：
- 资源包以 (commodity_code, applicable_products) 池为告警单元；单包耗尽但池内仍有额度
  属正常轮转，只记 info；
- 同一事件 7 日窗口内只刷新不重插，detect_date 保留首次发现时间，last_seen 记录最近复现；
  持续型事件 3 日未复现自动 resolved，支持人工确认或忽略；
- 按金额和倍数分级，info 不对外推送。

notify=True 时汇总推送：站内信（全部活跃用户）+ 全部启用的告警渠道。
包年包月不在检测范围（订单事件已在采集层单列）。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.collector.bss import _f, _s, fetch_resource_packages
from app.collector.credential import resolve_aliyun_credential
from app.core.config import settings
from app.models.alert_channel import AlertChannel
from app.models.cost_anomaly import CostAnomaly
from app.models.cost_daily import CostDaily
from app.models.resource_pack_snapshot import ResourcePackSnapshot
from app.services.pack_impact import (
    compute_pools, norm_amount, pack_chinese, pool_key, related_pack_exhaustions,
)
from app.services.notify import NotifyError, send_to_channel

logger = logging.getLogger("qianshu.cost_anomaly")

_MAX_ANOMALIES_PER_DAY = 20          # 单日单类最多记多少条，防告警风暴

# ---- 异动类型域：同一存储表按检测依据与写入方划分展示域 ----
# 判定依据：看检测依据是"钱/资源包"还是"监控指标基线/运行状态"，以及事件由谁写入：
# - cost    成本类：按量费用/资源包的钱与余量（detect_cost_for_date / snapshot_and_detect_packs）
# - monitor 监控类：监控基线与状态检测（baseline_job 周期基线 / host_watch 主机巡检）
# - cdn     CDN 流量类（monitor 子集，traffic_spike 一项；CostCdn 提示条单独取用）
# - profile 画像类：构成漂移（profile_engine / object_profile），展示归画像域
ANOMALY_TYPE_DOMAINS: dict[str, tuple[str, ...]] = {
    "cost": ("payg_spike", "payg_new", "pack_spike",
             "pack_exhausting", "pack_exhausted", "pack_expiry"),
    "monitor": ("traffic_spike", "growth_accel", "host_down", "aux_spillover"),
    "cdn": ("traffic_spike",),
    "profile": ("profile_drift", "chain_drift"),
}


def _fmt_money(v: float) -> str:
    return f"¥{v:,.2f}"


# 生命周期：同一事件在窗口内不重复入库，只刷新既有记录。
_REFRESH_WINDOW = timedelta(days=7)   # 近 7 日内仍复现视为同一事件
# 持续型异动（含监控联动的流量/增速类）连续 3 日未复现自动关闭
def _expiry_ignores(db) -> set:
    """到期忽略清单（与 services/expiry._ignore_set 同源，避免第三套口径）。"""
    try:
        from app.services.expiry import _ignore_set
        return _ignore_set(db)
    except Exception:
        return set()


_AUTO_RESOLVE_TYPES = ("payg_spike", "pack_spike", "pack_exhausting", "pack_expiry",
                       "traffic_spike", "growth_accel", "chain_drift")


def _refresh(a: CostAnomaly, title: str, content: str, value: float,
             baseline: float, ratio: float, level: str) -> None:
    """在既有记录上刷新最新观测（不动 ack_status，除非 critical 升级需要重开）。"""
    a.title, a.content = title, content
    a.value, a.baseline, a.ratio = round(value, 4), round(baseline, 4), round(ratio, 2)
    a.level = level
    # 注：resolved 的事件**不在此重开**——同日幂等重跑不该把用户手动关闭的事件撬开；
    # 真正的跨日复现走 _add 的插入路径（新记录+新通知），语义是"新的一次发生"
    if level == "critical" and a.ack_status in ("acknowledged", "ignored"):
        # 已确认/忽略的事件升级到 critical：自动重开，避免用户漏看
        a.ack_status = "open"
        a.ack_note = (a.ack_note or "") + "\n[级别升级到 critical，自动重开]"


def _profile_active_hook(db: Session, a: CostAnomaly) -> None:
    """异动落库后立即驱动画像事件采样与短时突变判定，event_tick 扫表仅作兜底。

    _add 是 cost_anomaly/host_watch/baseline_job/object_profile 共用的落库口，在此挂钩
    可覆盖所有检出器，并与异动落库处于同一事务。自反馈闸、幂等去重和启停开关由
    profile_engine.on_anomaly_landed 判定；钩子异常不得影响异动检测本身。
    """
    try:
        from app.services.profile_engine import on_anomaly_landed
        r = on_anomaly_landed(db, a)
        if r.get("recorded") or r.get("mutations"):
            logger.info("画像主动联动: %s", r)
    except Exception:
        logger.warning("画像主动联动钩子异常", exc_info=True)


def _add(db: Session, detect_date: date, type_: str, key: str, name: str,
         title: str, content: str, value: float = 0, baseline: float = 0,
         ratio: float = 0, level: str = "warning",
         account_id: Optional[int] = None) -> Optional[CostAnomaly]:
    """生命周期 upsert：返回新插入的记录（用于通知）；同一事件刷新时返回 None。

    1) 同 (type,target_key) 且未关闭（open/acknowledged/ignored）且近 7 日复现
       -> 刷新该记录（已确认/忽略的事件持续期间静默刷新，不重新打扰）；
    2) 同 (detect_date,type,target_key) 已存在（重跑当日检测）-> 原地刷新；
    3) 否则插入新记录（detect_date=首次发现，last_seen=本次）。

    仅新插入时触发画像主动联动钩子；刷新路径不重复驱动，事件样本按 anomaly:{id} 去重。
    profile_drift 在钩子内短路，防止自反馈。
    """
    prev = db.query(CostAnomaly).filter(
        CostAnomaly.type == type_,
        CostAnomaly.target_key == key,
        CostAnomaly.ack_status.in_(("open", "acknowledged", "ignored")),
        CostAnomaly.last_seen >= detect_date - _REFRESH_WINDOW,
    ).order_by(CostAnomaly.last_seen.desc()).first()
    if prev is None:
        prev = db.query(CostAnomaly).filter(
            CostAnomaly.detect_date == detect_date,
            CostAnomaly.type == type_,
            CostAnomaly.target_key == key,
        ).first()
    if prev is not None:
        prev.last_seen = detect_date
        _refresh(prev, title, content, value, baseline, ratio, level)
        return None
    if account_id is None:
        # 检测在账号上下文内运行时自动归因；全局检测（总账/池级跨账号）
        # 落 0=全局，按模块开关判定提醒。
        try:
            from app.collector.credential import active_account_id
            account_id = active_account_id() or 0
        except Exception:
            account_id = 0
    a = CostAnomaly(
        detect_date=detect_date, last_seen=detect_date, type=type_, level=level,
        target_key=key, target_name=name, title=title, content=content,
        value=round(value, 4), baseline=round(baseline, 4), ratio=round(ratio, 2),
        account_id=int(account_id or 0),
    )
    db.add(a)
    _profile_active_hook(db, a)   # 异动落库即刻驱动画像；钩内含自反馈闸、幂等与开关
    return a


def _auto_resolve_stale(db: Session, day: date) -> None:
    """持续型异动连续 3 日未复现 -> 自动 resolved。

    一次性事件（pack_exhausted/payg_new）不自动关——事件本身已结束但需要
    用户知晓，保持 open 直到人工确认/忽略。
    """
    n = db.query(CostAnomaly).filter(
        CostAnomaly.ack_status == "open",
        CostAnomaly.type.in_(_AUTO_RESOLVE_TYPES),
        CostAnomaly.last_seen < day - timedelta(days=3),
    ).update(
        {CostAnomaly.ack_status: "resolved", CostAnomaly.ack_note: "连续 3 日未复现，自动关闭"},
        synchronize_session=False,
    )
    if n:
        logger.info("自动关闭 %d 条持续型成本异动（3 日未复现）", n)


# ---------------- 按量日成本异动 ----------------

def detect_cost_for_date(db: Session, day: date, notify: bool = False) -> list:
    """检测 day 的按量成本异动（需 day 之前有历史数据做基线）。返回新插入的异动列表。"""
    ratio_th = settings.COST_ANOMALY_RATIO
    min_th = settings.COST_ANOMALY_MIN
    new_min = settings.COST_ANOMALY_NEW_MIN

    _auto_resolve_stale(db, day)

    def level_by_money(amount: float, base: float) -> str:
        """影响权重分级：钱是唯一通用分级语言。
        critical=倍数够大且日金额>=100；warning=中间档；info=<20 元（不推送）。"""
        if amount >= 3 * base and amount >= settings.COST_CRITICAL_MIN:
            return "critical"
        if amount < settings.COST_INFO_MAX:
            return "info"
        return "warning"

    def payg(q):
        # bill=阿里账单明细 / tc_bill=腾讯账单明细；两家按量异动采用同类口径
        return q.filter(CostDaily.source.in_(("bill", "tc_bill")),
                        CostDaily.subscription_type == "PayAsYouGo")

    # 当日 + 历史明细（窗口=基线窗口 56 天，供同日型基线取样）
    rows = payg(db.query(CostDaily).filter(CostDaily.stat_date == day)).all()
    hist = payg(db.query(CostDaily).filter(
        CostDaily.stat_date >= day - timedelta(days=settings.BASELINE_WINDOW_DAYS),
        CostDaily.stat_date < day,
    )).all()

    if not rows:
        return []  # 该日无按量数据（未采集或全订阅）

    def key_of(r):
        return f"{r.product_code}|{r.instance_id}|{r.billing_item}"

    # 按日序列（周期基线引擎的输入）：总口径 + 项目口径
    daily_total: dict = {}
    daily_key: dict = {}
    prev_map: dict = {}
    for r in hist:
        k = key_of(r)
        daily_total[r.stat_date] = daily_total.get(r.stat_date, 0.0) + r.pretax_amount
        daily_key.setdefault(k, {})[r.stat_date] = daily_key.get(k, {}).get(r.stat_date, 0.0) + r.pretax_amount
        if r.stat_date == day - timedelta(days=1):
            prev_map[k] = prev_map.get(k, 0.0) + r.pretax_amount

    def name_of(r):
        # 文案人话化：业务名优先，长实例 ID 截断（ID 直出=噪音）
        def _short(v):
            v = str(v or "")
            return v if len(v) <= 18 else v[:12] + "…"
        parts = [r.product_name or r.product_code]
        if r.instance_name or r.instance_id:
            parts.append(r.instance_name or _short(r.instance_id))
        if r.billing_item:
            parts.append(r.billing_item[:20])
        return " / ".join(p for p in parts if p)

    found: list = []

    def related_packs_text(product: str, day_: date) -> str:
        """疑似关联：近 3 天内耗尽的、抵扣范围覆盖本产品的资源包（只向前看，因果指认）。"""
        rel = related_pack_exhaustions(db, product, day_)
        if not rel:
            return ""
        lines = "\n".join(
            f"- 疑似关联: {p['type_label']} {p['instance_id']} 于 {p['detect_date']} 耗尽"
            for p in rel
        )
        return f"\n{lines}"

    # 周期基线判定：同日型基线+双门槛，吃掉周周期/调休假象。
    # 旧内核"环比昨日/7日均值 1.5 倍"在周一/调休日必误报，已废弃。
    from app.services.baseline import V_OBSERVE, V_SPIKE, day_series_check
    from app.services.holiday import day_kind as _day_kind

    def kind_fn(d):
        return _day_kind(db, d)

    def baseline_notes(chk: dict) -> str:
        return "\n".join(f"- {n}" for n in chk.get("notes", []))

    def spike_ok(chk: dict) -> bool:
        """引擎判定突增；冷启动（observe）时按全样本中位数兜底双门槛，避免漏报。"""
        if chk["verdict"] == V_SPIKE:
            return True
        if chk["verdict"] == V_OBSERVE and chk["baseline"] > 0:
            return (chk.get("ratio", 0) >= ratio_th
                    and chk.get("value", 0) - chk["baseline"] >= min_th)
        return False

    # 1) 总额（同日型基线）
    day_total = sum(r.pretax_amount for r in rows)
    chk = day_series_check(sorted(daily_total.items()), day, day_total, kind_fn,
                           unit=" 元", abs_min=settings.BASELINE_MIN_COST_YUAN)
    if day_total >= min_th and spike_ok(chk):
        a = _add(db, day, "payg_spike", "__total__", "全部按量支出",
                 f"按量日总额突增: {_fmt_money(day_total)}",
                 (f"按量日总额 {day.isoformat()} 突增（周期基线判定）\n\n"
                  f"- 当日金额（账单金额）: {_fmt_money(day_total)}\n"
                  f"- 同类日基线: {_fmt_money(chk['baseline'])}（MAD ±{_fmt_money(chk['mad'])}）\n"
                  f"- 为基线的 {chk['ratio']:.1f} 倍\n"
                  + baseline_notes(chk)
                  + related_packs_text("__all__", day)
                  + "\n- 口径：金额以 pretax_amount（账单金额）为准；"
                    "payment_amount 是挂账现金字段（常 0），不得据此推断抵扣；"
                    "T+1 账单非终态，近 7 天可能补账修订"),
                 value=day_total, baseline=chk["baseline"], ratio=chk["ratio"],
                 level=("info" if chk["level"] == "info"
                        else level_by_money(day_total, chk["baseline"] or 1.0)))
        if a:
            found.append(a)

    # 2) 明细粒度（产品×实例×计费项，同日型基线）
    agg: dict = {}
    for r in rows:
        k = key_of(r)
        if k not in agg:
            agg[k] = {"amount": 0.0, "sample": r}
        agg[k]["amount"] += r.pretax_amount

    for k, info in agg.items():
        amount = info["amount"]
        if amount < min_th:
            continue
        sample = info["sample"]
        series = sorted(daily_key.get(k, {}).items())
        if not series:
            # 全新费用项（窗口内从未出现过）——**新=变化信号，金额只定级不否决**
            # 全新费用项金额只定级、不否决：短周期低水位异动不应被 info 档淹没。
            # 已关闭服务仍出账、新开通服务及计费口径变化都应进入视野。
            if amount >= 0.1:
                first_seen = day.isoformat()
                trend = (f"- 费用时序：**{first_seen} 首次出现**，此前 {len(daily_key.get(k, {})) and '窗口内' or '近 30 天'}"
                         f"无此费用项（由零到有）\n"
                         f"- 定性提示：'此前无费用、某日突现'常见于三种情况——①新开通服务 "
                         f"②**已关闭服务仍在产生账单**（核对服务开关状态与账单）③计费口径变更。请在费用中心核对该产品状态")
                a = _add(db, day, "payg_new", k, name_of(sample),
                         f"新增按量费用项: {name_of(sample)} {_fmt_money(amount)}",
                         (f"新出现（或由零开始）的按量费用项\n\n"
                          f"- 项目: {name_of(sample)}\n"
                          f"- 当日金额（账单金额）: {_fmt_money(amount)}\n"
                          f"- 日期: {day.isoformat()}\n"
                          f"{trend}"
                          + related_packs_text(k.split("|")[0], day)
                          + "\n- 口径：金额以 pretax_amount（账单金额）为准；"
                            "payment_amount 是挂账现金字段（常 0），不得据此推断抵扣；"
                            "T+1 账单非终态，近 7 天可能补账修订"),
                         value=amount, baseline=0, ratio=0,
                         # 新费用项最低 warning：短周期低水位异动不应被 info 档淹没
                         level=("critical" if amount >= settings.COST_CRITICAL_MIN else "warning"))
                if a:
                    found.append(a)
            continue
        chk = day_series_check(series, day, amount, kind_fn,
                               unit=" 元", abs_min=settings.BASELINE_MIN_COST_YUAN)
        if not spike_ok(chk):
            continue
        a = _add(db, day, "payg_spike", k, name_of(sample),
                 f"按量费用突增: {name_of(sample)} {_fmt_money(amount)}",
                 (f"按量费用突增 {day.isoformat()}（周期基线判定）\n\n"
                  f"- 项目: {name_of(sample)}\n"
                  f"- 当日金额（账单金额）: {_fmt_money(amount)}\n"
                  f"- 同类日基线: {_fmt_money(chk['baseline'])}（MAD ±{_fmt_money(chk['mad'])}）\n"
                  f"- 为基线的 {chk['ratio']:.1f} 倍\n"
                  + baseline_notes(chk)
                  + related_packs_text(k.split("|")[0], day)
                  + "\n- 口径：金额以 pretax_amount（账单金额）为准；"
                    "payment_amount 是挂账现金字段（常 0），不得据此推断抵扣；"
                    "T+1 账单非终态，近 7 天可能补账修订"),
                 value=amount, baseline=chk["baseline"], ratio=chk["ratio"],
                 level=("info" if chk["level"] == "info"
                        else level_by_money(amount, chk["baseline"] or 1.0)))
        if a:
            found.append(a)

    found.sort(key=lambda a: a.value, reverse=True)
    if len(found) > _MAX_ANOMALIES_PER_DAY:
        for a in found[_MAX_ANOMALIES_PER_DAY:]:
            db.delete(a)
        logger.warning("成本异动 %s 超过 %d 条，仅保留金额最大的",
                       day, _MAX_ANOMALIES_PER_DAY)
        found = found[:_MAX_ANOMALIES_PER_DAY]
    db.commit()
    if found and notify:
        _notify_anomalies(db, found)
    return found


# ---------------- 资源包快照与异动 ----------------
# _norm_amount 已下沉 pack_impact（池化聚合也要用），此处 norm_amount 即原实现


def _parse_time(s: str) -> Optional[datetime]:
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None


def snapshot_and_detect_packs(db: Session, notify: bool = False) -> dict:
    """拉当前资源包 -> 落今日快照 -> 与上一份快照对比做异动检测。幂等（重跑覆盖当日快照）。

    多账号下删除/对比范围必须等于写入打标范围（account_id，同 mark_missing_deleted
    口径），否则账号 B 重跑会把账号 A 当日快照整批删掉（跨账号数据丢失）。
    """
    from app.collector.credential import active_account_id
    from app.services.tenant import account_id as _acc

    ak, sk, _src = resolve_aliyun_credential(db)
    items = fetch_resource_packages(ak, sk, settings.BSS_ENDPOINT)
    today = date.today()
    acc = _acc(db)

    q = db.query(ResourcePackSnapshot).filter(ResourcePackSnapshot.snapshot_date == today)
    cur = active_account_id()
    if cur is not None:
        q = q.filter(ResourcePackSnapshot.account_id == cur)
    q.delete()

    snaps: list = []
    for it in items:
        total = _f(getattr(it, "total_amount", 0))
        rem = _f(getattr(it, "remaining_amount", 0))
        total_n = norm_amount(total, _s(getattr(it, "total_amount_unit", "")))
        rem_n = norm_amount(rem, _s(getattr(it, "remaining_amount_unit", "")))
        ap = getattr(it, "applicable_products", None)
        products = ""
        if ap is not None:
            p = getattr(ap, "product", None)
            if isinstance(p, list):
                products = ",".join(_s(x) for x in p)
            elif p:
                products = _s(p)
        s = ResourcePackSnapshot(
            snapshot_date=today,
            account_id=acc,
            instance_id=_s(getattr(it, "instance_id", "")),
            commodity_code=_s(getattr(it, "commodity_code", "")),
            package_type=_s(getattr(it, "package_type", "")),
            applicable_products=products,
            region=_s(getattr(it, "region", "")),
            deduct_type=_s(getattr(it, "deduct_type", "")),
            total_amount=total,
            total_unit=_s(getattr(it, "total_amount_unit", "")),
            remaining_amount=rem,
            remaining_unit=_s(getattr(it, "remaining_amount_unit", "")),
            remaining_pct=round(rem_n / total_n * 100, 4) if total_n > 0 else 0.0,
            status=_s(getattr(it, "status", "")),
            effective_time=_s(getattr(it, "effective_time", "")),
            expiry_time=_s(getattr(it, "expiry_time", "")),
        )
        snaps.append(s)
        db.add(s)

    # 上一份快照（不含今天，本账号）
    prev_date = db.query(func.max(ResourcePackSnapshot.snapshot_date)).filter(
        ResourcePackSnapshot.snapshot_date < today,
        ResourcePackSnapshot.account_id == acc,
    ).scalar()
    prev_map: dict = {}
    if prev_date is not None:
        for p in db.query(ResourcePackSnapshot).filter(
            ResourcePackSnapshot.snapshot_date == prev_date,
            ResourcePackSnapshot.account_id == acc,
        ).all():
            prev_map[p.instance_id] = p
    db.commit()

    found: list = []
    if prev_date is not None:
        found.extend(_detect_packs(db, today, prev_date, snaps, prev_map))
    else:
        # 首次快照：无前日对比，只做耗尽/到期类检测
        found.extend(_detect_packs(db, today, None, snaps, {}))

    if found and notify:
        # 持续型异动（即将耗尽/到期）每日落库但只留紧急档推送，避免天天刷屏
        to_notify = [a for a in found if _should_push(a)]
        if to_notify:
            _notify_anomalies(db, to_notify)
    return {"packs": len(snaps), "prev_date": prev_date.isoformat() if prev_date else None,
            "new_anomalies": len(found)}


def _should_push(a: CostAnomaly) -> bool:
    """资源包持续型异动的推送门槛：耗尽预测 <=7 天(critical)或到期 <=7 天才推；
    突增/刚耗尽是一次性事件，始终推送。"""
    if a.type == "pack_exhausting":
        return a.level == "critical"
    if a.type == "pack_expiry":
        return a.value <= 7
    return True


def _pack_label(s: ResourcePackSnapshot) -> str:
    prods = [p for p in (s.applicable_products or "").split(",") if p]
    prod_desc = ",".join(prods[:3]) + ("等" if len(prods) > 3 else "") if prods else s.commodity_code
    return f"{s.commodity_code}({prod_desc}) {s.instance_id}"


def _pack_rate(s: ResourcePackSnapshot, db: Session) -> tuple:
    """返回 (日消耗率 %/天, 是否来自快照历史)。快照历史 >= 3 天时用近 7 日均值，
    否则用购入至今平均速率兜底。速率 <= 0 视为无消耗。"""
    # 近 7 个快照日的余量序列
    dates = [r[0] for r in db.query(ResourcePackSnapshot.snapshot_date).filter(
        ResourcePackSnapshot.instance_id == s.instance_id,
        ResourcePackSnapshot.snapshot_date >= s.snapshot_date - timedelta(days=7),
    ).order_by(ResourcePackSnapshot.snapshot_date).all()]
    if len(dates) >= 3:
        rows = db.query(ResourcePackSnapshot).filter(
            ResourcePackSnapshot.instance_id == s.instance_id,
            ResourcePackSnapshot.snapshot_date >= dates[0],
        ).order_by(ResourcePackSnapshot.snapshot_date).all()
        pcts = [(r.snapshot_date, r.remaining_pct) for r in rows]
        consumed = [max(0.0, pcts[i - 1][1] - pcts[i][1]) for i in range(1, len(pcts))]
        n_days = (pcts[-1][0] - pcts[0][0]).days
        if n_days > 0 and consumed:
            rate = sum(consumed) / n_days
            if rate > 0:
                return rate, True
    # 兜底：购入至今平均
    eff = _parse_time(s.effective_time)
    if eff is not None:
        days = max(1, (datetime.combine(s.snapshot_date, datetime.min.time()) - eff).days)
        rate = (100.0 - s.remaining_pct) / days
        if rate > 0:
            return rate, False
    return 0.0, False


def _detect_packs(db: Session, today: date, prev_date: Optional[date],
                  snaps: list, prev_map: dict) -> list:
    """资源包异动检测。

    逐包只做一次性事件（刚耗尽/消耗突增/到期浪费）；"N 天后耗尽"升级为池级判断：
    单包快耗尽而池内还有大量额度属于正常轮转，不应告警。
    刚耗尽的级别也由池决定：池内仍有充足额度 -> info 级轮转记录（不推送）。
    """
    _auto_resolve_stale(db, today)

    rates = {s.instance_id: _pack_rate(s, db)[0] for s in snaps}
    pools = compute_pools(snaps, rates)
    found: list = []

    for s in snaps:
        label = _pack_label(s)
        rate = rates[s.instance_id]

        # 1) 刚耗尽（>0 -> 0）：池内还有额度是轮转，池也见底才构成容量事故
        if prev_date is not None:
            prev = prev_map.get(s.instance_id)
            if prev is not None and prev.remaining_pct > 0.5 and s.remaining_pct <= 0.01:
                zh = pack_chinese(s.commodity_code, s.applicable_products)
                pool = pools[pool_key(s.commodity_code, s.applicable_products)]
                # 轮转判定必须先看池是否真有余量：池已见底时 days_left 必为 None
                #（无消耗速率），只看 days_left 会把"整池耗尽"误判成"额度充足"
                rotating = (pool["remaining_pct"] > 0.5
                            and (pool["days_left"] is None
                                 or pool["days_left"] > settings.PACK_EXHAUST_DAYS))
                pool_gone = pool["remaining_pct"] <= 0.5
                dl_desc = (f"，按当前消耗池预计 {pool['days_left']:.0f} 天后耗尽"
                           if pool["days_left"] is not None else "")
                a = _add(db, prev_date, "pack_exhausted", s.instance_id, label,
                         (f"资源包轮转（池内仍有额度）: {zh['type_label']} {s.instance_id}"
                          if rotating else
                          (f"资源包耗尽且整池已见底: {zh['type_label']} {s.instance_id}"
                           if pool_gone else
                           f"资源包刚耗尽且池内额度吃紧: {zh['type_label']} {s.instance_id}")),
                         (f"资源包余量已用尽，"
                          + (f"同池还有 {len(pool['packs'])} 个包、合计剩余 {pool['remaining_pct']:.1f}%"
                             f"——抵扣能力充足，属正常轮转，无需处理"
                             if rotating else
                             (f"且整池 {len(pool['packs'])} 个包已全部耗尽"
                              if pool_gone else "且池内额度同样吃紧")
                             + "，后续用量将全额转按量计费")
                          + "\n\n"
                          f"- 资源包: {zh['type_label']}（{zh['scope_label']}）{s.instance_id}\n"
                          f"- 抵扣范围: {zh['products_label']}\n"
                          f"- 所在池剩余: {pool['remaining_pct']:.1f}%（{len(pool['packs'])} 个包）{dl_desc}\n"
                          f"- 前一日余量: {prev.remaining_pct:.1f}%\n"
                          f"- 到期时间: {s.expiry_time[:10] if s.expiry_time else '-'}\n"
                          + ("" if rotating else "- 建议尽快续购，否则消耗转入按量支出")),
                         value=100 - s.remaining_pct, baseline=prev.remaining_pct,
                         ratio=0, level="info" if rotating else "critical")
                if a:
                    found.append(a)

        # 2) 消耗速率突增（需前日快照）
        if prev_date is not None:
            prev = prev_map.get(s.instance_id)
            if prev is not None and s.total_amount > 0:
                consumed_pct = max(0.0, prev.remaining_pct - s.remaining_pct)
                base_rate = rate  # 快照历史不足时用购入至今平均兜底，口径略粗但可参照
                # 轮转期假象排除：池总量充足时单包快速消耗=正常换包，
                # 不是异常；单包消耗必须结合池级余量判断。
                pool_now = pools.get(pool_key(s.commodity_code, s.applicable_products), {})
                rotating = pool_now.get("remaining_pct", 100) > 20
                if (not rotating and base_rate > 0
                        and consumed_pct >= settings.PACK_SPIKE_MIN_PCT
                        and consumed_pct >= settings.PACK_SPIKE_RATIO * base_rate):
                    unit = s.remaining_unit or ""
                    a = _add(db, prev_date, "pack_spike", s.instance_id, label,
                             f"资源包消耗突增: {label}",
                             (f"资源包日消耗速率突增\n\n"
                              f"- 资源包: {label}\n"
                              f"- 昨日消耗: {consumed_pct:.1f}% 总量"
                              f"（剩余 {s.remaining_amount:g}{unit} / {s.total_amount:g}{s.total_unit}）\n"
                              f"- 近期日均: {base_rate:.2f}%\n"
                              f"- 为日均的 {consumed_pct / base_rate:.1f} 倍，请排查用量来源"),
                             value=consumed_pct, baseline=base_rate,
                             ratio=consumed_pct / base_rate)
                    if a:
                        found.append(a)

        # 3) 即将到期但仍有较多余量（浪费风险，额度按包到期作废，池不能兜底）
        exp = _parse_time(s.expiry_time)
        if exp is not None and s.remaining_pct >= settings.PACK_EXPIRY_MIN_PCT:
            days_to_exp = (exp.date() - today).days
            if 0 <= days_to_exp <= settings.PACK_EXPIRY_DAYS:
                unit = s.remaining_unit or ""
                if f"pack:{s.instance_id}" in _expiry_ignores(db):
                    continue   # 用户在到期账里忽略的包不再报异动，保持同一忽略口径
                a = _add(db, today, "pack_expiry", s.instance_id, label,
                         f"资源包 {days_to_exp} 天后到期且余量还剩 {s.remaining_pct:.0f}%: {label}",
                         (f"资源包即将到期但仍有较多余量\n\n"
                          f"- 资源包: {label}\n"
                          f"- 剩余: {s.remaining_amount:g}{unit}（{s.remaining_pct:.1f}%）\n"
                          f"- 到期时间: {exp.date().isoformat()}（{days_to_exp} 天后）\n"
                          f"- 到期后剩余额度作废，请确认用量计划或调整续购"),
                         value=days_to_exp, baseline=s.remaining_pct, ratio=0)
                if a:
                    found.append(a)

    # 4) 池级耗尽预测：整池按当前速率 N 天内耗尽才值得告警
    for pool in pools.values():
        dl = pool["days_left"]
        if dl is None or dl <= 0 or dl > settings.PACK_EXHAUST_DAYS:
            continue
        zh = pack_chinese(pool["commodity_code"], pool["products"])
        n = len(pool["packs"])
        a = _add(db, today, "pack_exhausting", pool["key"],
                 f"{zh['type_label']}抵扣池（{n}个包）",
                 f"抵扣能力池约 {dl:.0f} 天后耗尽: {zh['type_label']}（{zh['products_label']}）",
                 (f"按当前消耗速率整池额度将耗尽——届时抵扣范围内产品全部转按量\n\n"
                  f"- 池: {zh['type_label']}，抵扣范围 {zh['products_label']}\n"
                  f"- 池内 {n} 个包，合计剩余 {pool['remaining_pct']:.1f}%\n"
                  f"- 池日消耗: {pool['daily_use']:g}{pool['unit']}/天\n"
                  f"- 预计 {dl:.0f} 天后整池耗尽\n"
                  f"- 建议提前续购，耗尽后用量转按量计费"),
                 value=dl, baseline=pool["remaining_pct"], ratio=0,
                 level="critical" if dl <= 7 else "warning")
        if a:
            found.append(a)

    db.commit()
    return found


# ---------------- 通知 ----------------

def _notify_anomalies(db: Session, anomalies: list) -> None:
    """汇总推送站内信 + 全部启用渠道。单渠道失败不阻断。

    info 级（<¥20/天的费用异动、池内正常轮转）不推送——噪音治理的一部分。
    按 (模块, 账号) 分组，各组独立判定总览/站内信/外推；
    成本、CDN、主机、画像漂移不再混在一条消息里，vCenter 之类的账号可单独静音。
    """
    anomalies = [a for a in anomalies if a.level != "info"]
    if not anomalies:
        return
    from app.services.monitor_policy import can_inapp, can_push, module_of_anomaly
    from app.services.notify import notify_inapp

    groups: dict[tuple[str, int], list] = {}
    for a in anomalies:
        groups.setdefault((module_of_anomaly(a), int(getattr(a, "account_id", 0) or 0)),
                          []).append(a)

    for (module, account_id), items in groups.items():
        allow_inapp = can_inapp(db, module, account_id)
        allow_push = can_push(db, module, account_id)
        if not allow_inapp and not allow_push:
            continue
        level = "critical" if any(a.level == "critical" for a in items) else "warning"
        day = max(a.detect_date for a in items).isoformat()
        title = f"[{level.upper()}] 成本异动检测（{day}）: 发现 {len(items)} 项异常"
        lines = [f"- [{'CRITICAL' if a.level == 'critical' else 'WARNING'}] {a.title}"
                 for a in items]
        content = f"成本异动检测结果（{day}）\n\n" + "\n".join(lines)

        n = notify_inapp(db, title, content, level=level, msg_type="cost",
                         audience="active", module=module, account_id=account_id) \
            if allow_inapp else 0

        # 分诊门：企微只推 critical 及以上；warning 并入每日决策清单
        from app.core.config import settings as _st
        pushable = level == "critical" if _st.NOTIFY_PUSH_MIN_LEVEL == "critical" \
            else level in ("critical", "warning")
        sent_push = False
        if allow_push and pushable:
            for ch in db.query(AlertChannel).filter(AlertChannel.enabled == True).all():  # noqa: E712
                try:
                    send_to_channel(ch, title, content)
                    sent_push = True
                except NotifyError as e:
                    logger.warning("成本异动推送失败 渠道=%s: %s", ch.name, e)
            crit = [a for a in items if a.level == "critical"]
            if crit:
                from app.services.auto_diag import fire_auto_diag
                fire_auto_diag(crit[0].title, crit[0].content or "", crit[0].target_key)
        if n or sent_push:
            now = datetime.now()
            for a in items:
                a.notify_count = (a.notify_count or 0) + 1
                a.last_notified_at = now
    db.commit()
