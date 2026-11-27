"""Onboarding 数据面编排：独立 Session、显式账号、按范围恢复。

云调用只在本模块的执行函数及其适配器内发生；状态查询不调用这里。
"""
from __future__ import annotations

from datetime import date, datetime, time as datetime_time, timedelta
import logging
import time

from app.core.db import SessionLocal
from app.core.security import decrypt_secret
from app.models.cloud_account import CloudAccount
from app.services import onboarding

logger = logging.getLogger("qianshu.onboarding_tasks")
_MAX_ATTEMPTS = 3


def _dates_for_scope(scope: dict, dates: list[date] | None = None) -> list[date]:
    if scope.get("bill_date"):
        return [date.fromisoformat(scope["bill_date"])]
    if dates is not None:
        return list(dict.fromkeys(dates))
    yesterday = date.today() - timedelta(days=1)
    return [yesterday - timedelta(days=offset) for offset in range(30)]


def _validate_account(account: CloudAccount | None) -> str:
    if account is None or not account.enabled:
        return "account_disabled"
    if account.vendor != "aliyun":
        return "vendor_not_supported"
    try:
        ak = decrypt_secret(account.ak_cipher) if account.ak_cipher else ""
        sk = decrypt_secret(account.secret_cipher) if account.secret_cipher else ""
    except Exception:
        return "credential_unavailable"
    return "" if ak and sk else "credential_unavailable"


def _report_capability_issue(account: CloudAccount, capability: str, reason_code: str,
                             error: str, *, scope: dict | None = None,
                             retryable: bool = True) -> None:
    """接入失败进入统一问题漏斗；任何异常都不回抛、不阻断接入。"""
    if reason_code in ("vendor_not_supported",):
        return
    try:
        from app.services.self_heal.service import report_issue_safe
        scope = scope or {}
        report_issue_safe(
            source="onboarding",
            fingerprint_parts={
                "error_class": reason_code,
                "region": scope.get("region"),
                "source_id": scope.get("source_id"),
                "bill_date": scope.get("bill_date"),
                "product": scope.get("capability") or capability,
            },
            target={"region": scope.get("region"),
                    "source_id": scope.get("source_id"),
                    "bill_date": scope.get("bill_date"),
                    "product": capability},
            severity="warning",
            title=f"账号 {account.name} 的 {capability} 接入失败：{reason_code}",
            reason_code=reason_code,
            error=error or reason_code,
            account_id=account.id, capability=capability,
            source_ref={"capability": capability, "scope": scope,
                        "vendor": account.vendor},
            evidence={"capability": capability, "scope": scope,
                      "reason_code": reason_code},
            retryable=retryable,
        )
    except Exception:
        logger.warning("接入失败上报自愈问题单失败（已忽略）", exc_info=True)


def _record_failure(db, account, entry, reason_code: str, *, retryable: bool = True) -> None:
    onboarding.record_capability(
        db, account, entry["capability"], scope=entry.get("scope") or {},
        expected_revision=entry["revision"], revision=entry["revision"],
        execution="failed", data_state="unknown", checked_at=datetime.now(),
        reason_code=reason_code, retryable=retryable,
        next_action="restore_credential" if reason_code == "credential_unavailable" else "retry_later",
    )
    db.commit()
    _report_capability_issue(account, entry["capability"], reason_code,
                             reason_code, scope=entry.get("scope") or {},
                             retryable=retryable)


def _collect_cost_day(db, account: CloudAccount, day: date) -> dict:
    """暂时性失败最多三次；403/未开通由适配器标记为不可自动重试。"""
    last = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        result = cost_job.collect_account_days(db, account, [day])
        if result.get("account_id") != account.id:
            raise ValueError("账单适配器返回了错误账号")
        if result.get("lock_waiting"):
            last = {"lock_waiting": True}
        else:
            days = result.get("days") or []
            last = days[0] if days else {
                "bill_date": day.isoformat(), "ok": False, "rows": 0,
                "reason_code": "result_missing", "retryable": True,
                "error": "账单适配器未返回该日结果", "amortized_active": None,
            }
        retry = last.get("lock_waiting") or (last.get("retryable") and (
            not last.get("ok") or last.get("amortized_active") is None))
        if not retry or attempt == _MAX_ATTEMPTS:
            return last
        time.sleep(min(2 ** (attempt - 1), 4))
    return last


def _execute_cost_day(db, account: CloudAccount, entry: dict, day: date) -> None:
    result = _collect_cost_day(db, account, day)
    scope = {"bill_date": day.isoformat()}
    now = datetime.now()
    common = {
        "scope": scope, "expected_revision": entry["revision"], "revision": entry["revision"],
        "checked_at": now,
    }
    if result.get("lock_waiting"):
        onboarding.record_capability(
            db, account, "cost", **common, execution="waiting", data_state="unknown",
            reason_code="collection_locked", retryable=True, next_action="retry_later")
        db.commit()
        return
    ok = bool(result.get("ok"))
    rows = int(result.get("rows") or 0)
    amortized_active = result.get("amortized_active")
    reason = result.get("reason_code") or ("" if ok else "collection_failed")
    onboarding.record_capability(
        db, account, "cost", **common,
        execution="succeeded" if ok else "failed",
        data_state=("available" if rows else "no_data") if ok else "unknown",
        coverage={
            "discovered": 1, "checked": 1 if ok else 0,
            "failed": 0 if ok else 1, "unknown": 0,
            "total_known": True, "truncated": (not ok) or amortized_active is not True,
        },
        observed_until=datetime.combine(day, datetime_time.max) if ok else None,
        evidence_refs=[f"cost_daily:{account.id}:{day.isoformat()}"] if ok else [],
        reason_code=reason, retryable=bool(result.get("retryable")),
        next_action=("resolve_permission" if reason == "permission_denied" else
                     "retry_failed" if result.get("retryable") else ""),
    )
    db.commit()
    if not ok:
        _report_capability_issue(account, "cost", reason,
                                 result.get("error") or reason,
                                 scope=scope, retryable=bool(result.get("retryable")))


def _summarize_cost(db, account: CloudAccount, entry: dict, days: list[date]) -> None:
    current = onboarding.capability_results(onboarding.get_run(db, account.id), account)
    by_date = {item["scope"]["bill_date"]: item for item in current
               if item["capability"] == "cost" and item["scope"]["bill_date"]}
    values = [by_date.get(day.isoformat()) for day in days]
    checked = sum(item is not None and item["execution"] in ("succeeded", "skipped") for item in values)
    failed = sum(item is not None and item["execution"] == "failed" for item in values)
    pending = len(values) - checked - failed
    available = any(item is not None and item["data_state"] == "available" for item in values)
    complete = checked == len(values) and not failed and not pending
    data_state = ("available" if available else
                  "no_data" if complete else "unknown")
    retryable = any(item is not None and item["execution"] in ("failed", "waiting")
                    and item["retryable"] for item in values)
    observed = [item["observed_until"] for item in values
                if item is not None and item.get("observed_until")]
    evidence = [ref for item in values if item is not None for ref in item["evidence_refs"]]
    onboarding.record_capability(
        db, account, "cost", scope={}, expected_revision=entry["revision"],
        revision=entry["revision"], execution="succeeded" if complete else "failed",
        data_state=data_state,
        coverage={
            "discovered": len(days), "checked": checked, "failed": failed,
            "unknown": pending, "total_known": True,
            "truncated": not complete or any(
                item is not None and item["coverage"]["truncated"] for item in values),
        },
        checked_at=datetime.now(), observed_until=max(observed) if observed else None,
        evidence_refs=evidence[:100],
        reason_code=("cost_ready" if complete and available else
                     "empty_bill" if complete else "cost_partial_failure"),
        retryable=retryable,
        next_action=("retry_failed" if retryable else
                     "resolve_permission" if failed else ""),
    )
    db.commit()


def _execute_cost(db, account: CloudAccount, entry: dict,
                  dates: list[date] | None = None) -> None:
    scope = entry.get("scope") or {}
    days = _dates_for_scope(scope, dates)
    if scope.get("bill_date"):
        _execute_cost_day(db, account, entry, days[0])
        return
    # 首日单独执行并提交，用户无需等待整个回填批次。
    for day in days:
        prepared = onboarding.prepare_retries(db, account, [{
            "capability": "cost", "bill_date": day.isoformat(),
        }], automatic=True)
        # 先落 running 状态并释放 Run 行锁，再调云 API：同账号的指标/变更/日志
        # 线程不能因为账单逐日查询被长时间挡在锁外。
        db.commit()
        for daily_entry in prepared["started"]:
            _execute_cost_day(db, account, daily_entry, day)
        db.commit()
    _summarize_cost(db, account, entry, days)


def _execute_changes(db, account: CloudAccount, entry: dict) -> None:
    from app.services import change_source

    result = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        result = change_source.collect_account(
            db, account, resource_ids=(entry.get("scope") or {}).get("resource_ids") or None,
            source_id=(entry.get("scope") or {}).get("source_id") or "")
        if result.get("account_id") != account.id:
            raise ValueError("变更适配器返回了错误账号")
        if result.get("ok") or not result.get("retryable") or attempt == _MAX_ATTEMPTS:
            break
        time.sleep(min(2 ** (attempt - 1), 4))
    onboarding.record_capability(
        db, account, "changes", scope=entry.get("scope") or {},
        expected_revision=entry["revision"], revision=entry["revision"],
        execution="succeeded" if result.get("ok") else "failed",
        data_state=result.get("data_state") or "unknown",
        coverage={"discovered": int(result.get("fetched") or 0),
                  "checked": int(result.get("fetched") or 0), "failed": 0,
                  "unknown": None if result.get("truncated") else 0,
                  "total_known": bool(result.get("total_known")),
                  "truncated": bool(result.get("truncated"))},
        checked_at=datetime.now(), observed_until=result.get("observed_until"),
        evidence_refs=result.get("evidence_refs") or [],
        reason_code=result.get("reason_code") or "source_failed",
        retryable=bool(result.get("retryable")),
        next_action=(result.get("next_action") or
                     ("resolve_permission" if result.get("reason_code") == "permission_denied"
                      else "retry_failed" if result.get("retryable") and not result.get("ok") else "")),
    )
    db.commit()
    if not result.get("ok"):
        _report_capability_issue(
            account, "changes", result.get("reason_code") or "source_failed",
            "；".join(str(x) for x in (result.get("errors") or []))[:800] or
            str(result.get("error") or result.get("reason_code") or "变更采集失败"),
            scope=entry.get("scope") or {}, retryable=bool(result.get("retryable")))


def _execute_metrics(db, account: CloudAccount, entry: dict) -> None:
    from app.collector import metrics_job

    result = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        result = metrics_job.collect_account(
            db, account, resource_ids=(entry.get("scope") or {}).get("resource_ids") or None,
            region=(entry.get("scope") or {}).get("region") or "")
        if result.get("account_id") != account.id:
            raise ValueError("指标适配器返回了错误账号")
        retry = result.get("lock_waiting") or (not result.get("ok") and result.get("retryable"))
        if not retry or attempt == _MAX_ATTEMPTS:
            break
        time.sleep(min(2 ** (attempt - 1), 4))
    common = {"scope": entry.get("scope") or {}, "expected_revision": entry["revision"],
              "revision": entry["revision"], "checked_at": datetime.now()}
    if result.get("lock_waiting"):
        onboarding.record_capability(
            db, account, "metrics", **common, execution="waiting", data_state="unknown",
            reason_code="collection_locked", retryable=True, next_action="retry_later")
        db.commit()
        return
    discovered = int(result.get("discovered") or 0)
    checked = int(result.get("checked") or 0)
    errors = [str(x)[:300] for x in (result.get("errors") or [])][:20]
    if errors and not result.get("ok"):
        logger.warning("账号 %s 指标采集/回读失败: %s", account.id, "；".join(errors[:5]))
    onboarding.record_capability(
        db, account, "metrics", **common,
        execution="succeeded" if result.get("ok") else "failed",
        data_state=result.get("data_state") or "unknown",
        coverage={"discovered": discovered, "checked": checked, "failed": 0,
                  "unknown": max(0, discovered - checked), "total_known": True,
                  "truncated": bool(result.get("truncated"))},
        observed_until=result.get("observed_until"),
        evidence_refs=result.get("evidence_refs") or [],
        errors=errors,
        reason_code=result.get("reason_code") or "metrics_failed",
        retryable=bool(result.get("retryable")),
        next_action=("resolve_permission" if result.get("reason_code") == "permission_denied"
                     else "retry_failed" if result.get("retryable") and not result.get("ok") else ""),
    )
    db.commit()
    if not result.get("ok"):
        _report_capability_issue(
            account, "metrics", result.get("reason_code") or "metrics_failed",
            "；".join(errors)[:800] or "指标采集/回读失败",
            scope=entry.get("scope") or {}, retryable=bool(result.get("retryable")))


def _execute_assets(db, account: CloudAccount, entry: dict) -> None:
    from app.services.cloud.sync_job import sync_accounts

    # 接入层在失败时统一上报 onboarding 问题单（带 run 上下文），
    # 这里关闭同步层上报，避免同一次失败生成两张问题单。
    out = sync_accounts(db, account_ids=[account.id], force=True, report_issues=False)
    rows = [row for row in out["results"] if row.get("account_id") == account.id]
    failed = account.id in out["summary"]["failed_accounts"] or not rows
    total = sum(int(row.get("total") or 0) for row in rows)
    onboarding.record_capability(
        db, account, "assets", scope=entry.get("scope") or {},
        expected_revision=entry["revision"], revision=entry["revision"],
        execution="failed" if failed else "succeeded",
        data_state="unknown" if failed else ("available" if total else "no_data"),
        coverage={"discovered": total, "checked": len(rows), "failed": 1 if failed else 0,
                  "unknown": None, "total_known": not failed, "truncated": failed},
        checked_at=datetime.now(), evidence_refs=[f"assets:{account.id}"] if not failed else [],
        reason_code="asset_sync_failed" if failed else "", retryable=True,
        next_action="retry_failed" if failed else "",
    )
    db.commit()
    if failed:
        _report_capability_issue(
            account, "assets", "asset_sync_failed",
            str(out.get("summary") or "资产同步失败")[:800],
            scope=entry.get("scope") or {}, retryable=True)


def _execute_started(db, account: CloudAccount, run_id: int, started: list[dict],
                     dates: list[date] | None = None) -> None:
    for entry in started:
        # run_id + revision 双重约束，避免旧账号任务或旧响应写入新任务。
        run = onboarding.get_run(db, account.id)
        if run is None or run.id != run_id:
            continue
        try:
            if entry["capability"] == "cost":
                _execute_cost(db, account, entry, dates)
            elif entry["capability"] == "metrics":
                _execute_metrics(db, account, entry)
            elif entry["capability"] == "changes":
                _execute_changes(db, account, entry)
            elif entry["capability"] == "assets":
                _execute_assets(db, account, entry)
            else:
                _record_failure(db, account, entry, "capability_not_ready", retryable=True)
        except Exception as exc:
            logger.exception("账号 %s 能力 %s 执行失败", account.id, entry["capability"])
            db.rollback()
            _record_failure(db, account, entry, "execution_failed", retryable=True)


def _finish_run(db, account: CloudAccount) -> None:
    run = onboarding.get_run(db, account.id)
    if run is None:
        return
    capabilities = onboarding.capability_results(run, account)
    if any(item["execution"] == "running" for item in capabilities):
        run.status = "running"
        run.finished_at = None
    elif any(item["execution"] == "failed" or item["data_state"] == "stale"
             for item in capabilities):
        run.status = "attention"
        run.finished_at = datetime.now()
    else:
        # 重试成功但账号仍停在 error：只有完整接入证据派生为 ready 才恢复 ok，
        # 避免一次能力成功就冒充整账号接入完成；否则保留 error 让用户继续处理。
        promoted = account.status == "error"
        onboarding.set_account_stage(db, account, "ok" if promoted else account.status)
        if promoted:
            account.status = "ok" if run.status == "ready" else "error"
            account.updated_at = datetime.now()
    run.updated_at = datetime.now()
    db.commit()


def prepare_account(account_id: int, *, capabilities: list[str] | None = None,
                    dates: list[date] | None = None, run_id: int | None = None) -> dict:
    """新账号显式接入后的自动准备；本函数自身建立 Session，不依赖请求线程。"""
    db = SessionLocal()
    try:
        account = db.get(CloudAccount, account_id)
        if account is None or not account.enabled:
            return {"accepted": False, "reason_code": "account_disabled"}
        run = onboarding.get_run(db, account_id) or onboarding.ensure_run(db, account)
        if run_id is not None and run.id != run_id:
            return {"accepted": False, "reason_code": "stale_run"}
        requested = capabilities or ["cost"]
        reason = _validate_account(account)
        scopes = []
        for name in requested:
            scope = {"capability": name}
            if name == "changes":
                from app.services import change_source
                try:
                    scope["source_id"] = change_source.source_key_for_account(account)
                except Exception:
                    # 适配器会把配置错误写成失败证据，不回退到其他账号或默认源。
                    pass
            scopes.append(scope)
        prepared = onboarding.prepare_retries(db, account, scopes, automatic=True)
        db.commit()
        if reason and any(name in ("cost", "metrics", "logs", "changes") for name in requested):
            for entry in prepared["started"]:
                if entry["capability"] != "assets":
                    _record_failure(db, account, entry, reason, retryable=False)
        elif prepared["started"]:
            _execute_started(db, account, run.id, prepared["started"], dates)
        _finish_run(db, account)
        return {"accepted": not reason, "run_id": run.id, "reason_code": reason, **prepared}
    finally:
        db.close()


def retry_scopes(account_id: int, run_id: int, started: list[dict]) -> None:
    """O1 重试接口的后台执行器；每个线程独立 Session 和凭证上下文。"""
    db = SessionLocal()
    try:
        account = db.get(CloudAccount, account_id)
        if account is None:
            return
        reason = _validate_account(account)
        if reason:
            for entry in started:
                if entry["capability"] != "assets":
                    _record_failure(db, account, entry, reason, retryable=False)
            return
        _execute_started(db, account, run_id, started)
        _finish_run(db, account)
    finally:
        db.close()


def reap_interrupted() -> int:
    """进程重启后收口中断状态，不自动访问云。

    - running 能力改为可重试 failed；
    - 停留在 probing/syncing/seeding 的账号说明上一进程死在接入中途：
      改写为 error 并留下原因，否则该账号永远不会被定时兜底选中，向导也一直转圈。
    """
    db = SessionLocal()
    recovered = 0
    try:
        accounts = db.query(CloudAccount).all()
        for account in accounts:
            run = onboarding.get_run(db, account.id)
            if run is None:
                continue
            account_recovered = 0
            for item in onboarding.capability_results(run, account):
                if item["execution"] != "running":
                    continue
                onboarding.record_capability(
                    db, account, item["capability"], scope=item["scope"],
                    expected_revision=item["revision"], revision=item["revision"],
                    execution="failed",
                    data_state="stale" if item["data_state"] == "available" else "unknown",
                    reason_code="interrupted", retryable=True, next_action="retry_failed",
                    checked_at=datetime.now())
                recovered += 1
                account_recovered += 1
                _report_capability_issue(
                    account, item["capability"], "interrupted",
                    "服务重启时接入任务尚未完成，已标记中断", scope=item.get("scope") or {},
                    retryable=True)
            if account.status in ("probing", "syncing", "seeding"):
                account.status = "error"
                account.updated_at = datetime.now()
                onboarding.record_runtime_error(
                    db, account, "服务重启时接入任务尚未完成，已标记中断；请重新接入或重试失败项")
                recovered += 1
                account_recovered += 1
            if account_recovered:
                run.status = "attention"
                run.finished_at = datetime.now()
        db.commit()
        return recovered
    finally:
        db.close()


from app.services import cost_job  # noqa: E402  放在函数定义后，避免采集模块反向依赖编排
