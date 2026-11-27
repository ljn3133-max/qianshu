"""云资产同步作业：接入账号即全自动，资产/拓扑不需要手动点同步。

三条入口共用同一套逻辑：
  1) 接入即同步：POST /clouds/accounts 建号后后台线程 bootstrap_account——
     先连通测试，再同步资产，最后按本账号实际资产一次性播种常见告警规则；
  2) 定时同步：main.py _start_cloud_asset_sync 每 CLOUD_SYNC_INTERVAL_S 秒
     遍历启用账号同步（leader 锁内单调度进程执行）；
  3) 手动同步：POST /clouds/sync 直接复用 sync_accounts（原有行为不变）。

口径：
  - 单账号/单产品失败不阻断其余批次，失败如实进结果（清扫在失败时天然跳过，不清资产）；
  - 拓扑不需要额外动作：build_graph 每次实时查库，资产入库即自动可见；
  - 自动告警规则只播种一次（cloud_accounts.alert_seeded_at 一次性标记），
    之后用户改阈值/禁用/删除都保留——删除不复活；
  - 自动规则与人工规则同权（可编辑），只多一个 origin='auto' 标识；
  - 播种规则不带通知渠道（channel_ids=[]）：默认站内记录，不外发，
    需要推送由用户显式勾选渠道（对外发消息只能用户主动触发）。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.core.security import decrypt_secret
from app.models.alert_rule import AlertRule
from app.models.asset import Asset
from app.models.cloud_account import CloudAccount
from app.services import onboarding
from app.services.cloud.provider import (RESOURCE_TYPES, capabilities_of,
                                         get_provider, vendor_label)

logger = logging.getLogger("qianshu.cloud.sync_job")

# 定时轮次互斥：多轮定时同步不叠加；账号级互斥由 _account_locks 负责，
# 新账号接入不再被其他账号的同步整轮阻塞。
_auto_lock = threading.Lock()

_account_locks: dict[int, threading.RLock] = {}
_account_locks_guard = threading.Lock()


def report_cloud_issue(account_id: int, *, product: str = "", capability: str = "assets",
                       reason_code: str = "cloud_sync_failed", error: str = "",
                       retryable: bool = True, severity: str = "warning") -> None:
    """同步/接入失败进入统一问题漏斗；任何异常都不回抛。"""
    try:
        from app.models.cloud_account import CloudAccount
        from app.services.self_heal.service import report_issue_safe

        db = SessionLocal()
        try:
            acc = db.get(CloudAccount, int(account_id))
            name = acc.name if acc is not None else f"#{account_id}"
            vendor = acc.vendor if acc is not None else ""
        finally:
            db.close()
        report_issue_safe(
            source="cloud_sync",
            fingerprint_parts={"error_class": reason_code, "product": product},
            target={"product": product or capability},
            severity=severity,
            title=f"云账号 {name} 同步失败：{product or capability}（{reason_code}）",
            reason_code=reason_code, error=error or reason_code,
            account_id=int(account_id), capability=capability,
            source_ref={"vendor": vendor, "product": product, "capability": capability},
            evidence={"product": product, "capability": capability,
                      "reason_code": reason_code},
            retryable=retryable,
        )
    except Exception:
        logger.warning("云同步失败上报自愈问题单失败（已忽略）", exc_info=True)


def account_lock(account_id: int) -> threading.RLock:
    """账号级可重入锁：同一账号的接入/同步不并发调用云 API，不同账号互不阻塞。"""
    with _account_locks_guard:
        lock = _account_locks.get(account_id)
        if lock is None:
            lock = threading.RLock()
            _account_locks[account_id] = lock
        return lock

# 接入生命周期：只有探活、完整资产同步、规则播种都结束才允许进入 ok。
# 沿用现有 status 列，不引入第二套阶段字段；前端和定时兜底共用这三个非终态。
ONBOARDING_STATUSES = ("probing", "syncing", "seeding")

# 产品 -> 播种规则名里的短标签
_PRODUCT_LABELS = {
    **RESOURCE_TYPES,
    "ecs": "ECS", "rds": "RDS", "redis": "Redis", "slb": "SLB",
    "vmware_vm": "VMware虚拟机", "vmware_host": "VMware主机",
}


def sync_accounts(
    db: Session,
    account_ids: Optional[list[int]] = None,
    product: str = "",
    force: bool = False,
    *,
    report_issues: bool = True,
) -> dict:
    """遍历启用账号串行同步资产（幂等 upsert + 清扫守卫）。

    account_ids=None 全部启用账号；product 为空=该厂商全部支持产品。
    force=True（接入即同步/手动同步）绕过数据源优先级节奏。
    单账号/单产品失败不阻断其余批次，逐条结果 + 汇总返回。
    report_issues=False 供上层（接入任务/自愈重试）自行上报，避免同一次失败
    同时生成 onboarding 与 cloud_sync 两张问题单。
    """
    from app.services.monitor_policy import collect_due

    q = db.query(CloudAccount).filter(CloudAccount.enabled == True)  # noqa: E712
    if account_ids:
        q = q.filter(CloudAccount.id.in_(account_ids))
    accounts = q.order_by(CloudAccount.id).all()

    results: list = []
    attempted: list = []
    for acc in accounts:
        cap = capabilities_of(acc.vendor)
        if not cap.sync:
            # 不采集厂商（仅登记/仅探活/Agent）：确定性跳过，不算失败、不推进 last_sync_at
            results.append({
                "account_id": acc.id, "account": acc.name, "vendor": acc.vendor,
                "product": product or "*", "ok": True, "skipped": True,
                "skipped_reason": "vendor_not_supported",
                "note": f"{vendor_label(acc.vendor)}：本期仅支持登记，资产采集未启用",
            })
            continue
        # 分级监控：medium 12 小时 / low 每天 04:00 一次；last_sync_at 即游标
        if not force and not collect_due(db, "assets", acc.id):
            continue
        lock = account_lock(acc.id)
        if not lock.acquire(blocking=False):
            # 同账号正在接入/同步：本轮不重复打云 API，也不冒充失败或成功。
            results.append({"account_id": acc.id, "account": acc.name, "vendor": acc.vendor,
                            "product": product or "*", "ok": True, "skipped": True,
                            "skipped_reason": "account_busy",
                            "note": "该账号正在接入或同步，本轮跳过以免重复调用云 API"})
            continue
        attempted.append(acc)
        try:
            provider = get_provider(acc.vendor)
            products = [product] if product else None
            try:
                for r in provider.sync(db, acc, products=products):
                    results.append({"account_id": acc.id, "account": acc.name,
                                    "vendor": acc.vendor, **r})
            except NotImplementedError as e:
                results.append({"account_id": acc.id, "account": acc.name, "vendor": acc.vendor,
                                "product": product or "*", "ok": False, "skipped": True,
                                "note": str(e)})
            except Exception as e:
                logger.exception("云账号 %s 同步失败", acc.name)
                results.append({"account_id": acc.id, "account": acc.name, "vendor": acc.vendor,
                                "product": product or "*", "ok": False,
                                "error": str(e)[:200]})
        finally:
            lock.release()

    # 一个产品/区域失败即代表本账号当次同步不完整；没有任何结果也不能冒充成功。
    # 失败账号不推进成功游标，下轮定时同步会重试，而不是等 12 小时/一天。
    attempted_ids = {acc.id for acc in attempted}
    result_ids = {r.get("account_id") for r in results}
    failed_ids = {
        r.get("account_id") for r in results
        if r.get("ok") is False or bool(r.get("failed_regions"))
    }
    failed_ids |= attempted_ids - result_ids
    # 与 failed_accounts 区分：这里只回答“是否至少有一个产品成功返回”。
    # 部分产品失败仍可为空/部分资产播种规则，不能把整账号当成完全不可用。
    usable_ids = {
        r.get("account_id") for r in results
        if r.get("account_id") in attempted_ids
        and r.get("ok") is not False
        and not (r.get("failed_regions") or [])
        and not r.get("skipped")
    }
    no_asset_ids = attempted_ids - usable_ids
    synced_at = datetime.now()
    for acc in attempted:
        # 同一账号的向导/智能体/后台任务共享逐产品证据；部分失败不能被账号级 ok 吞掉。
        onboarding.record_sync_results(db, acc, results)
        own = [r for r in results if r.get("account_id") == acc.id]
        failed_rows = [r for r in own if r.get("ok") is False or r.get("failed_regions")]
        asset_errors = []
        for row in failed_rows:
            detail = (row.get("error") or row.get("note")
                      or "；".join(str(x) for x in (row.get("failed_regions") or [])))
            if detail:
                asset_errors.append(f"{row.get('product', '*')}: {str(detail)[:300]}")
        if report_issues:
            for row in failed_rows:
                detail = (row.get("error") or row.get("note")
                          or "；".join(str(x) for x in (row.get("failed_regions") or [])))
                if detail:
                    report_cloud_issue(
                        acc.id, product=str(row.get("product") or ""),
                        reason_code="asset_sync_failed",
                        error=str(detail)[:800], retryable=True)
        total = sum(int(r.get("total") or 0) for r in own)
        blocked = acc.id in failed_ids or not own
        # 资产能力证据必须每次同步都回写：否则向导只看到“等待执行”，
        # 即使逐产品条目已经同步成功，也无法判定资产能力就绪。
        onboarding.record_capability(
            db, acc, "assets",
            execution="failed" if blocked else "succeeded",
            data_state="unknown" if blocked else ("available" if total else "no_data"),
            coverage={"discovered": total, "checked": len(own), "failed": len(failed_rows),
                      "unknown": None, "total_known": not blocked, "truncated": blocked},
            checked_at=datetime.now(),
            evidence_refs=[f"assets:{acc.id}"] if not blocked else [],
            errors=asset_errors[:20],
            reason_code="asset_sync_failed" if blocked else "",
            retryable=True, next_action="retry_failed" if blocked else "",
        )
        if acc.alert_seeded_at:
            onboarding.record_seed_result(db, acc)
        if acc.id not in failed_ids:
            acc.last_sync_at = synced_at
        onboarding.set_account_stage(db, acc, acc.status)
        # 逐账号提交：释放该账号 Run 行锁，其他线程/新账号接入不必等整轮结束。
        db.commit()

    summary = {
        "accounts": len(attempted),
        "skipped_accounts": len(accounts) - len(attempted),
        "total": sum(r.get("total", 0) for r in results),
        "created": sum(r.get("created", 0) for r in results),
        "updated": sum(r.get("updated", 0) for r in results),
        "missing_marked": sum(r.get("missing_marked", 0) for r in results),
        "failed_accounts": sorted(failed_ids),
        "no_asset_accounts": sorted(no_asset_ids),
        "failed_regions": [f"{r['account']}/{r['product']}: {fr}"
                           for r in results for fr in r.get("failed_regions", [])],
        "errors": [f"{r['account']}/{r.get('product', '*')}: {r.get('error') or r.get('note')}"
                   for r in results if r.get("ok") is False],
    }
    if accounts:
        db.commit()
    return {"results": results, "summary": summary}


def seed_rules_for_account(db: Session, acc: CloudAccount) -> list[str]:
    """按账号实际资产播种常见告警规则（每账号一次，删除不复活）。

    规则绑定 account_id，只评估该账号资产；阈值与安装向导默认规则同源
    （copilot/workflows/onboard.DEFAULT_METRIC_RULES：90% / 5xx>10 / 持续 300s）。
    资产为空时不播种也不打标记（下次同步后再试），避免把"暂时没采到"固化为"永远没有"。
    """
    from app.services.copilot.workflows.onboard import DEFAULT_METRIC_RULES

    products = sorted({
        p for (p,) in db.query(Asset.product)
        .filter(Asset.account_id == acc.id, Asset.status != "Deleted")
        .distinct().all()
        if p in DEFAULT_METRIC_RULES
    })
    if not products:
        logger.info("账号 %s 暂无指标可采资产，跳过自动规则播种", acc.name)
        onboarding.record_seed_result(db, acc)
        return []

    existing_names = {r.name for r in db.query(AlertRule).all()}
    created: list[str] = []
    for product in products:
        plabel = _PRODUCT_LABELS.get(product, product)
        for metric, label, cmp_, thr, level, dur in DEFAULT_METRIC_RULES[product]:
            name = f"自动规则·{acc.name}·{plabel}·{label}"
            if name in existing_names:
                continue  # 同名规则已存在（人工创建或其它账号同名账号名）——不重复建
            db.add(AlertRule(
                name=name, source="metric", level=level,
                product=product, metric=metric,
                comparator=cmp_, threshold=thr, duration=dur,
                channel_ids="[]", silences="[]",
                account_id=acc.id, origin="auto", enabled=True,
            ))
            existing_names.add(name)
            created.append(name)
    acc.alert_seeded_at = datetime.now()
    onboarding.record_seed_result(db, acc)
    db.commit()
    logger.info("账号 %s 自动播种告警规则 %d 条: %s", acc.name, len(created), created)
    return created


def _set_status(db: Session, acc: CloudAccount, status: str) -> None:
    acc.status = status
    acc.updated_at = datetime.now()
    onboarding.set_account_stage(db, acc, status)
    db.commit()


def bootstrap_account(account_id: int) -> None:
    """接入即用：probing → syncing → seeding，全部成功后才进入 ok。

    按账号加锁而不是全局锁：新账号接入不被其他账号的同步整轮阻塞，
    同一账号的重复触发只排队等待，不误写成接入失败。
    """
    lock = account_lock(account_id)
    if not lock.acquire(blocking=False):
        logger.info("账号 %s 已有接入/同步任务在跑，本次接入排队等待", account_id)
        db = SessionLocal()
        try:
            acc = db.get(CloudAccount, account_id)
            if acc is not None and acc.enabled:
                onboarding.record_runtime_waiting(
                    db, acc, "该账号已有接入或同步任务在跑，本次接入已排队；请稍后刷新查看")
                db.commit()
        finally:
            db.close()
        return
    db = SessionLocal()
    try:
        acc = db.get(CloudAccount, account_id)
        if acc is None or not acc.enabled:
            return
        provider = get_provider(acc.vendor)
        cap = capabilities_of(acc.vendor)
        if not cap.probe:
            acc.last_test_at = datetime.now()
            onboarding.record_probe(db, acc, ok=False, supported=False,
                                    message="该厂商当前仅支持登记，未执行云端验证")
            _set_status(db, acc, "unsupported")
            logger.info("账号 %s 厂商 %s 仅支持登记，跳过接入测试/同步", acc.name, acc.vendor)
            return

        _set_status(db, acc, "probing")
        try:
            ak = decrypt_secret(acc.ak_cipher) if acc.ak_cipher else ""
            sk = decrypt_secret(acc.secret_cipher) if acc.secret_cipher else ""
            res = provider.test_account(acc, ak, sk)
            ok = bool(res.get("ok"))
            message = res.get("message", "")
            terminal = None if ok else "error"
        except NotImplementedError as e:
            ok, message, terminal = False, str(e), "unsupported"
        except Exception as e:
            logger.exception("账号 %s 接入测试异常", acc.name)
            ok, message, terminal = False, str(e)[:200], "error"
        acc.last_test_at = datetime.now()
        onboarding.record_probe(db, acc, ok=ok, message=message)
        if terminal:
            _set_status(db, acc, terminal)
        else:
            db.commit()   # 只落探活时间；状态仍是 probing，绝不提前 ok
        if not ok:
            logger.warning("账号 %s 接入测试未通过，跳过资产同步: %s", acc.name, message)
            reason = ("credential_unavailable" if terminal in ("error", "unsupported")
                      else "probe_failed")
            report_cloud_issue(acc.id, capability="assets", reason_code=reason,
                               error=str(message or reason)[:800], retryable=False)
            return
        if not cap.sync:
            onboarding.record_sync_results(db, acc, [{
                "account_id": acc.id, "account": acc.name, "vendor": acc.vendor,
                "product": "*", "ok": True, "skipped": True,
                "note": "该厂商只有连通性验证，没有资产采集能力",
            }])
            onboarding.record_seed_result(db, acc)
            _set_status(db, acc, "ok")
            logger.info("账号 %s 厂商 %s 无资产采集能力，探活完成", acc.name, acc.vendor)
            return

        _set_status(db, acc, "syncing")
        try:
            out = sync_accounts(db, account_ids=[account_id], force=True)
            logger.info("账号 %s 接入自动同步完成: %s", acc.name, out["summary"])
            sync_failed = (account_id in out["summary"]["failed_accounts"]
                           or out["summary"]["accounts"] != 1)
        except Exception as exc:
            logger.exception("账号 %s 接入自动同步失败", acc.name)
            onboarding.record_sync_results(db, acc, [])
            report_cloud_issue(acc.id, capability="assets",
                               reason_code="asset_sync_failed",
                               error=f"{type(exc).__name__}: {str(exc)[:500]}",
                               retryable=True)
            sync_failed = True
        if sync_failed:
            _set_status(db, acc, "error")
            logger.warning("账号 %s 资产同步不完整，继续播种可用资产并执行日志发现",
                           acc.name)
            # 不 return：规则播种与 SLS 发现不依赖每个产品都成功。一个可选产品
            # 失败不能让默认规则/SLS 永远停在“尚无结果”，否则用户只能看到接入失败。
        else:
            _set_status(db, acc, "seeding")
        try:
            seed_rules_for_account(db, acc)
        except Exception as exc:
            logger.exception("账号 %s 自动规则播种失败", acc.name)
            onboarding.record_seed_result(db, acc, error="默认规则播种失败")
            report_cloud_issue(acc.id, capability="alerts",
                               reason_code="alert_seed_failed",
                               error=f"{type(exc).__name__}: {str(exc)[:500]}",
                               retryable=True)
            _set_status(db, acc, "error")
            sync_failed = True
        if not sync_failed:
            _set_status(db, acc, "ok")
        if acc.vendor == "aliyun":
            # 仅新账号显式接入触发新增数据面；独立线程不与 SLS 互相阻塞，也不占用资产同步锁。
            from app.services import onboarding_tasks
            threading.Thread(
                target=onboarding_tasks.prepare_account,
                kwargs={"account_id": account_id, "capabilities": ["cost"]},
                daemon=True, name=f"onboarding-cost-{account_id}",
            ).start()
            threading.Thread(
                target=onboarding_tasks.prepare_account,
                kwargs={"account_id": account_id, "capabilities": ["metrics"]},
                daemon=True, name=f"onboarding-metrics-{account_id}",
            ).start()
            threading.Thread(
                target=onboarding_tasks.prepare_account,
                kwargs={"account_id": account_id, "capabilities": ["changes"]},
                daemon=True, name=f"onboarding-changes-{account_id}",
            ).start()
            # 后台执行：SLS 枚举可能耗时数分钟，不能拖住本账号接入完成与后续账号。
            from app.services import sls_discovery
            sls_discovery.discover(account_id, background=True)
    finally:
        db.close()
        lock.release()


def auto_sync_all() -> dict:
    """定时同步入口：全部启用账号同步 + 未播种账号补播（一次性标记内幂等）。"""
    if not _auto_lock.acquire(blocking=False):
        return {"skipped": "上一轮自动同步尚未结束"}
    db = SessionLocal()
    try:
        out = sync_accounts(db)
        # 兜底只依据当次同步的真实结果收口。probing 未做探活，不能由资产任务代为宣布成功；
        # seeding 表示上次同步已成功，可只补播种。资产不完整的账号仍补播种，
        # 因为规则只依赖本账号已有资产，不调用云；否则规则证据会永远缺失。
        failed_ids = set(out["summary"]["failed_accounts"])
        result_ids = {r.get("account_id") for r in out["results"]}
        handled: set[int] = set()
        active = (db.query(CloudAccount)
                  .filter(CloudAccount.enabled == True,  # noqa: E712
                          CloudAccount.status.in_(ONBOARDING_STATUSES))
                  .order_by(CloudAccount.id).all())
        for acc in active:
            if not capabilities_of(acc.vendor).probe:
                _set_status(db, acc, "unsupported")
                continue
            if acc.id in failed_ids:
                _set_status(db, acc, "error")
                try:
                    seed_rules_for_account(db, acc)
                except Exception:
                    logger.exception("账号 %s 自动规则播种失败", acc.name)
                    onboarding.record_seed_result(db, acc, error="默认规则播种失败")
                    _set_status(db, acc, "error")
                handled.add(acc.id)
                continue
            if acc.status == "probing":
                continue
            if acc.status != "seeding" and acc.id not in result_ids:
                continue
            _set_status(db, acc, "seeding")
            try:
                seed_rules_for_account(db, acc)
            except Exception:
                logger.exception("账号 %s 自动规则播种失败", acc.name)
                onboarding.record_seed_result(db, acc, error="默认规则播种失败")
                _set_status(db, acc, "error")
                continue
            handled.add(acc.id)
            _set_status(db, acc, "ok")
        for acc in (db.query(CloudAccount)
                    .filter(CloudAccount.enabled == True,  # noqa: E712
                            CloudAccount.alert_seeded_at.is_(None))
                    .order_by(CloudAccount.id).all()):
            if (not capabilities_of(acc.vendor).sync or acc.id in handled
                    or acc.status in ONBOARDING_STATUSES):
                continue
            try:
                seed_rules_for_account(db, acc)
            except Exception:
                logger.exception("账号 %s 自动规则播种失败", acc.name)
                onboarding.record_seed_result(db, acc, error="默认规则播种失败")
                onboarding.set_account_stage(db, acc, acc.status)
                db.commit()
        return out
    finally:
        db.close()
        _auto_lock.release()
