"""受控执行器：只执行已批准/自动允许的计划步骤，每步独立 Session + 超时。

R0/R1 只能自动或 operator/admin 手动；R2/R3 必须 admin 按计划哈希批准。
R3 复用 change_tools/code_mod；R2 只通过 copilotd token 通道重启千枢后端。
"""
from __future__ import annotations

import hmac
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import date, datetime, timedelta

from app.core.config import settings
from app.core.db import SessionLocal
from app.models.issue import IssueCase, IssuePlan
from app.services.self_heal import policy, service, validator

logger = logging.getLogger("qianshu.self_heal.executor")

_LOCK = threading.Lock()
_ACTIVE: dict[int, threading.Thread] = {}
_BREAKER_OPEN = False
_BREAKER_REASON = ""
_STEP_TIMEOUT_MAX = 300


class PlanApprovalError(ValueError):
    def __init__(self, reason: str, *, code: str = "plan_rejected"):
        super().__init__(reason)
        self.code = code


class PlanExecutionError(RuntimeError):
    pass


def breaker_state() -> dict:
    return {"open": _BREAKER_OPEN, "reason": _BREAKER_REASON}


def reset_breaker(actor: str = "") -> None:
    global _BREAKER_OPEN, _BREAKER_REASON
    _BREAKER_OPEN = False
    _BREAKER_REASON = ""
    logger.warning("自愈熔断已被 %s 重置", actor or "admin")


def _maybe_trip_breaker(db) -> None:
    """连续 5 个计划执行失败 -> 自动执行熔断，保留诊断/站内信。"""
    global _BREAKER_OPEN, _BREAKER_REASON
    if _BREAKER_OPEN:
        return
    since = datetime.now() - timedelta(hours=2)
    rows = (db.query(IssuePlan)
            .filter(IssuePlan.created_at >= since,
                    IssuePlan.status.in_(("failed", "rolled_back", "succeeded")))
            .order_by(IssuePlan.id.desc()).limit(int(settings.SELF_HEAL_BREAKER_FAILS)).all())
    if len(rows) < int(settings.SELF_HEAL_BREAKER_FAILS):
        return
    if all(r.status in ("failed", "rolled_back") for r in rows):
        _BREAKER_OPEN = True
        _BREAKER_REASON = f"连续 {len(rows)} 个计划失败，自动执行已熔断，等待管理员重置"
        logger.error("自愈熔断打开：%s", _BREAKER_REASON)


def _get_user(db, user_id):
    if not user_id:
        return None
    from app.models.user import User
    return db.get(User, int(user_id))


def approve_plan(db, issue_id: int, plan_id: int, user, *, plan_hash: str,
                 ack: bool = False) -> dict:
    """管理员/操作员按完整计划哈希批准。批准与执行分离，批准只落状态和事件。"""
    if not ack:
        raise PlanApprovalError("必须勾选确认后才能授权执行", code="ack_required")
    issue = (db.query(IssueCase).filter(IssueCase.id == int(issue_id))
             .with_for_update().first())
    if issue is None:
        raise PlanApprovalError("问题单不存在", code="not_found")
    plan = db.get(IssuePlan, int(plan_id))
    if plan is None or plan.issue_id != issue.id:
        raise PlanApprovalError("计划不存在或不属于该问题单", code="not_found")
    if plan.status != "proposed":
        raise PlanApprovalError(f"计划状态 {plan.status} 不可批准", code="not_proposed")
    if plan.expires_at and plan.expires_at < datetime.now():
        plan.status = "expired"
        db.add(plan)
        service.add_event(db, issue.id, "plan_expired", payload={"plan_id": plan.id})
        try:
            from app.services import audit
            audit.record(db, action="issue.plan.expired",
                         user_id=getattr(user, "id", None),
                         username=getattr(user, "username", "") or "self_heal",
                         resource_type="issue_plan", resource_id=str(plan.id),
                         detail={"issue_id": issue.id, "reason": "approval_after_expiry"})
        except Exception:
            logger.warning("自愈计划过期审计写入失败", exc_info=True)
        db.commit()
        raise PlanApprovalError("计划已过期，请重新诊断", code="expired")
    if not isinstance(plan_hash, str) or not hmac.compare_digest(
            plan_hash.strip().lower(), (plan.plan_hash or "").lower()):
        raise PlanApprovalError("计划哈希不匹配，计划可能已变化", code="hash_mismatch")
    if not policy.can_execute(plan.risk, automatic=False, user=user):
        raise PlanApprovalError(
            f"当前账号或模式无权执行 {plan.risk} 计划（需要管理员与对应模式）",
            code="forbidden")
    if breaker_state()["open"]:
        raise PlanApprovalError(breaker_state()["reason"], code="breaker_open")
    plan.status = "approved"
    plan.approved_by = getattr(user, "username", "") or ""
    plan.approved_by_id = getattr(user, "id", None)
    plan.approved_at = datetime.now()
    issue.status = "repairing"
    issue.next_action = "wait"
    issue.updated_at = datetime.now()
    db.add(plan)
    db.add(issue)
    service.add_event(db, issue.id, "plan_approved", actor=plan.approved_by,
                      actor_id=plan.approved_by_id,
                      payload={"plan_id": plan.id, "risk": plan.risk,
                               "plan_hash": plan.plan_hash})
    try:
        from app.services import audit
        audit.record(db, action="issue.plan.approved",
                     user_id=plan.approved_by_id, username=plan.approved_by,
                     resource_type="issue_plan", resource_id=str(plan.id),
                     detail={"issue_id": issue.id, "risk": plan.risk,
                             "plan_hash": plan.plan_hash})
    except Exception:
        logger.warning("自愈审批审计写入失败", exc_info=True)
    db.commit()
    return {"plan": plan, "issue": issue}


def reject_plan(db, issue_id: int, plan_id: int, user, reason: str = "") -> IssuePlan:
    issue = db.get(IssueCase, int(issue_id))
    plan = db.get(IssuePlan, int(plan_id))
    if issue is None or plan is None or plan.issue_id != issue.id:
        raise PlanApprovalError("计划不存在", code="not_found")
    if plan.status != "proposed":
        raise PlanApprovalError(f"计划状态 {plan.status} 不可拒绝", code="not_proposed")
    plan.status = "rejected"
    plan.rejected_by = getattr(user, "username", "") or ""
    plan.rejected_at = datetime.now()
    plan.reject_reason = (reason or "")[:500]
    db.add(plan)
    service.add_event(db, issue.id, "plan_rejected", actor=plan.rejected_by,
                      actor_id=getattr(user, "id", None),
                      payload={"plan_id": plan.id, "reason": plan.reject_reason})
    service.transition(db, issue, "manual_required", kind="escalated",
                       actor=plan.rejected_by, actor_id=getattr(user, "id", None),
                       payload={"reason": "plan_rejected",
                                "reject_reason": plan.reject_reason},
                       notify=False)
    try:
        from app.services import audit
        audit.record(db, action="issue.plan.rejected",
                     user_id=getattr(user, "id", None), username=plan.rejected_by,
                     resource_type="issue_plan", resource_id=str(plan.id),
                     detail={"issue_id": issue.id, "reason": plan.reject_reason})
    except Exception:
        pass
    db.commit()
    return plan


def expire_plans(db=None) -> int:
    own = db is None
    db = db or SessionLocal()
    n = 0
    try:
        rows = (db.query(IssuePlan)
                .filter(IssuePlan.status == "proposed",
                        IssuePlan.expires_at != None,  # noqa: E711
                        IssuePlan.expires_at < datetime.now()).all())
        for plan in rows:
            plan.status = "expired"
            db.add(plan)
            service.add_event(db, plan.issue_id, "plan_expired",
                              payload={"plan_id": plan.id})
            try:
                from app.services import audit
                audit.record(db, action="issue.plan.expired",
                             username="self_heal", resource_type="issue_plan",
                             resource_id=str(plan.id),
                             detail={"issue_id": plan.issue_id,
                                     "reason": "ttl"})
            except Exception:
                logger.warning("自愈计划过期审计写入失败", exc_info=True)
            n += 1
        db.commit()
        return n
    finally:
        if own:
            db.close()


# ---------------- 步骤执行 ----------------
def _retry_capability(db, issue, plan, step):
    from app.services import onboarding_tasks
    p = step["params"]
    dates = None
    scope = p.get("scope") or {}
    if scope.get("bill_date"):
        try:
            dates = [date.fromisoformat(scope["bill_date"])]
        except ValueError:
            raise PlanExecutionError("账单日期格式不合法")
    out = onboarding_tasks.prepare_account(
        int(p["account_id"]), capabilities=[p["capability"]],
        dates=dates, run_id=int(p["run_id"]) if p.get("run_id") else None)
    if not out.get("accepted"):
        raise PlanExecutionError(f"重试未被受理：{out.get('reason_code') or 'unknown'}")
    return {"accepted": True, "run_id": out.get("run_id")}


def _refresh_assets(db, issue, plan, step):
    from app.services.cloud import sync_job
    p = step["params"]
    # 本次重试的失败由原问题单的执行结果/验证统一收口，不再新建 cloud_sync 问题单。
    out = sync_job.sync_accounts(db, [int(p["account_id"])], force=True,
                                 report_issues=False)
    results = out.get("results") or []
    if not results:
        raise PlanExecutionError("资产同步没有返回结果")
    failed = [r for r in results if not r.get("ok") and not r.get("skipped")]
    if failed:
        raise PlanExecutionError(f"资产同步失败：{str(failed[0].get('error') or '')[:200]}")
    return {"results": len(results)}


def _rediscover_logs(db, issue, plan, step):
    from app.services import onboarding_tasks
    p = step["params"]
    out = onboarding_tasks.prepare_account(int(p["account_id"]), capabilities=["logs"])
    if not out.get("accepted"):
        raise PlanExecutionError(f"日志发现未被受理：{out.get('reason_code') or 'unknown'}")
    return {"accepted": True}


def _revalidate_metrics(db, issue, plan, step):
    from app.services import onboarding_tasks
    p = step["params"]
    out = onboarding_tasks.prepare_account(int(p["account_id"]), capabilities=["metrics"])
    if not out.get("accepted"):
        raise PlanExecutionError(f"指标回读未被受理：{out.get('reason_code') or 'unknown'}")
    return {"accepted": True}


def _recollect_cost(db, issue, plan, step):
    from app.services import onboarding_tasks
    p = step["params"]
    try:
        day = date.fromisoformat(p["bill_date"])
    except (KeyError, ValueError):
        raise PlanExecutionError("账单日期格式不合法")
    out = onboarding_tasks.prepare_account(int(p["account_id"]), capabilities=["cost"],
                                           dates=[day])
    if not out.get("accepted"):
        raise PlanExecutionError(f"账单重采未被受理：{out.get('reason_code') or 'unknown'}")
    return {"accepted": True, "bill_date": p["bill_date"]}


def _probe(db, issue, plan, step):
    from app.core.security import decrypt_secret
    from app.models.cloud_account import CloudAccount
    from app.services.cloud.provider import capabilities_of, get_provider
    p = step["params"]
    acc = db.get(CloudAccount, int(p["account_id"]))
    if acc is None:
        raise PlanExecutionError("账号不存在")
    if not capabilities_of(acc.vendor).probe:
        raise PlanExecutionError("该厂商本期不支持只读探活")
    ak = decrypt_secret(acc.ak_cipher) if acc.ak_cipher else ""
    sk = decrypt_secret(acc.secret_cipher) if acc.secret_cipher else ""
    result = get_provider(acc.vendor).test_account(acc, ak, sk)
    if not result.get("ok"):
        raise PlanExecutionError(f"只读探测失败：{str(result.get('message') or '')[:200]}")
    return {"message": str(result.get("message") or "")[:200]}


def _reset_own_state(db, issue, plan, step):
    service.add_event(db, issue.id, "step_done", payload={
        "plan_id": plan.id, "recipe": "reset_own_issue_state", "noop": True})
    db.commit()
    return {"reset": True}


def _reseed_alert_rules(db, issue, plan, step):
    from app.models.cloud_account import CloudAccount
    from app.services.cloud.sync_job import seed_rules_for_account
    p = step["params"]
    acc = db.get(CloudAccount, int(p["account_id"]))
    if acc is None:
        raise PlanExecutionError("账号不存在")
    if acc.alert_seeded_at is not None:
        return {"already_seeded": True}
    created = seed_rules_for_account(db, acc)
    if not created:
        raise PlanExecutionError("没有可播种的默认规则（可能账号暂无资产）")
    return {"created": len(created)}


def _reload_settings(db, issue, plan, step):
    from app.services import monitor_policy
    monitor_policy.ensure_defaults(db)
    rows = monitor_policy.modules_payload(db)
    db.commit()
    return {"modules": len(rows)}


def _reapply_endpoint(db, issue, plan, step):
    from app.models.cloud_account import CloudAccount
    p = step["params"]
    acc = db.get(CloudAccount, int(p["account_id"]))
    if acc is None:
        raise PlanExecutionError("账号不存在")
    try:
        extra = json.loads(acc.extra_json or "{}")
    except ValueError:
        extra = {}
    if not isinstance(extra, dict):
        extra = {}
    extra["endpoint"] = p["endpoint"]
    acc.extra_json = json.dumps(extra, ensure_ascii=False)
    acc.updated_at = datetime.now()
    db.add(acc)
    db.commit()
    return {"account_id": acc.id, "endpoint": p["endpoint"]}


def _restart_platform(db, issue, plan, step):
    from app.services.copilot import change_tools
    health = change_tools.copilotd_health(timeout=6.0)
    if not health.get("ok"):
        raise PlanExecutionError("copilotd 通道不可用，拒绝重启")
    receipt = change_tools.copilotd_restart_delegated("self_heal_plan")
    if not receipt.get("ok", False):
        raise PlanExecutionError(f"copilotd 拒绝重启：{str(receipt)[:200]}")
    return {"deferred": True, "receipt": receipt}


def _patch_code(db, issue, plan, step):
    from app.services.self_heal import code_repair
    user = _get_user(db, plan.approved_by_id)
    if user is None:
        raise PlanExecutionError("批准用户不存在，拒绝执行代码补丁")
    return code_repair.execute_patch(db, issue, plan, user)


_HANDLERS = {
    "retry_onboarding_capability": _retry_capability,
    "refresh_cloud_assets": _refresh_assets,
    "rediscover_logs": _rediscover_logs,
    "revalidate_metric_ingest": _revalidate_metrics,
    "recollect_cost_day": _recollect_cost,
    "probe_integration": _probe,
    "reset_own_issue_state": _reset_own_state,
    "reseed_missing_alert_rules": _reseed_alert_rules,
    "reload_platform_settings": _reload_settings,
    "reapply_account_endpoint": _reapply_endpoint,
    "restart_platform": _restart_platform,
    "patch_platform_code": _patch_code,
}


def _run_step_isolated(issue_id: int, plan_id: int, step: dict) -> dict:
    db = SessionLocal()
    try:
        issue = db.get(IssueCase, int(issue_id))
        plan = db.get(IssuePlan, int(plan_id))
        if issue is None or plan is None:
            raise PlanExecutionError("问题单或计划不存在")
        handler = _HANDLERS.get(step.get("recipe"))
        if handler is None:
            raise PlanExecutionError(f"动作 {step.get('recipe')} 未实现")
        return handler(db, issue, plan, step) or {}
    finally:
        db.close()


def _execute_step(issue_id: int, plan_id: int, step: dict) -> dict:
    timeout = min(int(step.get("timeout_s") or 300), _STEP_TIMEOUT_MAX)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="self-heal-step") as pool:
        future = pool.submit(_run_step_isolated, issue_id, plan_id, step)
        try:
            return future.result(timeout=timeout)
        except FutureTimeout as exc:
            raise PlanExecutionError(f"步骤 {step.get('recipe')} 超时（>{timeout}s）") from exc


def _auto_retry_allowed(issue, plan) -> bool:
    """自动失败后是否允许冷却重试：仅 R0/R1、未复发、未到 max_attempts、证据可重试。"""
    if not policy.enabled() or not policy.auto_risk_allowed(plan.risk):
        return False
    if plan.risk not in ("R0", "R1"):
        return False
    if issue.recurrence_of_id:
        return False
    if int(issue.attempts or 0) >= int(issue.max_attempts or 3):
        return False
    try:
        evidence = json.loads(issue.evidence_json or "{}")
    except ValueError:
        evidence = {}
    if isinstance(evidence, dict) and evidence.get("retryable") is False:
        return False
    return True


def _mark_failed(db, issue, plan, reason: str, *, rollback_patch: bool = False,
                 user=None, automatic: bool = False) -> None:
    try:
        db.refresh(plan)
    except Exception:
        pass
    plan.status = "failed"
    # 保留已落盘快照/工单信息：失败后自动回滚必须靠 result_json.snapshot_id，
    # 不能在这里把结果覆盖成只剩错误文本（否则回滚永远找不到快照）。
    try:
        result = json.loads(plan.result_json or "{}")
        if not isinstance(result, dict):
            result = {}
    except ValueError:
        result = {}
    result.update({"error": reason[:1000],
                   "failed_at": datetime.now().isoformat()})
    plan.result_json = service._dumps(result)
    db.add(plan)
    if rollback_patch:
        try:
            from app.services.self_heal import code_repair
            if user is not None:
                code_repair.rollback_patch(db, issue, plan, user, reason=reason)
        except Exception:
            logger.warning("补丁失败后回滚异常", exc_info=True)
    issue.attempts = int(issue.attempts or 0)
    db.add(issue)
    retry = bool(automatic and _auto_retry_allowed(issue, plan))
    if retry:
        issue.status = "open"
        issue.next_action = "wait"
        issue.next_retry_at = datetime.now() + timedelta(
            seconds=max(5, int(settings.SELF_HEAL_RETRY_COOLDOWN_S or 60)))
        issue.updated_at = datetime.now()
        db.add(issue)
        service.add_event(db, issue.id, "retry_scheduled", payload={
            "plan_id": plan.id, "attempt": issue.attempts,
            "max_attempts": issue.max_attempts,
            "next_retry_at": issue.next_retry_at.isoformat(),
            "reason": reason[:600]})
    else:
        service.add_event(db, issue.id, "escalated", payload={
            "plan_id": plan.id, "reason": reason[:800]})
        service.transition(db, issue, "manual_required", kind="escalated",
                           payload={"plan_id": plan.id, "reason": reason[:800]})
    try:
        from app.services import audit
        audit.record(db, action="issue.repair.failed",
                     user_id=getattr(user, "id", None),
                     username=getattr(user, "username", "") if user else "",
                     resource_type="issue_plan", resource_id=str(plan.id),
                     result="failed", detail={"issue_id": issue.id,
                                              "reason": reason[:500],
                                              "auto_retry": retry})
    except Exception:
        pass
    db.commit()
    _maybe_trip_breaker(db)


def execute_plan(plan_id: int, *, user_id: int | None = None,
                 automatic: bool = False) -> dict:
    """执行单个计划；调用方负责在线程中运行。返回执行摘要。"""
    db = SessionLocal()
    issue = plan = None
    try:
        issue = (db.query(IssueCase)
                 .filter(IssueCase.id == db.query(IssuePlan.issue_id)
                         .filter(IssuePlan.id == int(plan_id)).scalar_subquery())
                 .with_for_update().first())
        plan = db.get(IssuePlan, int(plan_id))
        if issue is None or plan is None:
            return {"ok": False, "error": "计划不存在"}
        if plan.status not in ("approved",) and not (automatic and plan.status == "proposed"):
            return {"ok": False, "error": f"计划状态 {plan.status} 不可执行"}
        if plan.expires_at and plan.expires_at < datetime.now():
            plan.status = "expired"
            service.add_event(db, issue.id, "plan_expired",
                              payload={"plan_id": plan.id, "at": "execute"})
            try:
                from app.services import audit
                audit.record(db, action="issue.plan.expired",
                             username="self_heal", resource_type="issue_plan",
                             resource_id=str(plan.id),
                             detail={"issue_id": issue.id, "reason": "execute_after_expiry"})
            except Exception:
                logger.warning("自愈计划过期审计写入失败", exc_info=True)
            db.commit()
            return {"ok": False, "error": "计划已过期"}
        if breaker_state()["open"]:
            return {"ok": False, "error": breaker_state()["reason"]}
        if not policy.enabled():
            return {"ok": False, "error": "自愈总开关已关闭"}
        if automatic:
            if not policy.auto_risk_allowed(plan.risk):
                return {"ok": False, "error": "当前模式不允许自动执行"}
            if int(issue.attempts or 0) >= int(issue.max_attempts or 3):
                service.transition(db, issue, "manual_required", kind="escalated",
                                   payload={"reason": "max_attempts"})
                return {"ok": False, "error": "自动尝试次数已达上限"}
            plan.status = "approved"
            plan.approved_by = "self_heal"
            plan.approved_by_id = None
            plan.approved_at = datetime.now()
        if not policy.can_execute(plan.risk, automatic=False,
                                  user=_get_user(db, plan.approved_by_id)) and not automatic:
            return {"ok": False, "error": "批准人权限已失效"}

        plan.status = "executing"
        issue.attempts = int(issue.attempts or 0) + 1
        issue.status = "repairing"
        issue.next_action = "wait"
        issue.updated_at = datetime.now()
        db.add(plan)
        db.add(issue)
        service.add_event(db, issue.id, "repair_started", payload={
            "plan_id": plan.id, "risk": plan.risk,
            "steps": [s.get("recipe") for s in json.loads(plan.plan_json or "{}").get("steps") or []],
            "automatic": automatic,
        })
        db.commit()
        try:
            from app.services import audit
            audit.record(db, action="issue.repair.started",
                         user_id=plan.approved_by_id,
                         username=plan.approved_by or "",
                         resource_type="issue_plan", resource_id=str(plan.id),
                         detail={"issue_id": issue.id, "risk": plan.risk,
                                 "automatic": automatic})
            db.commit()
        except Exception:
            db.rollback()

        data = json.loads(plan.plan_json or "{}")
        steps = data.get("steps") or []
        patch_step = next((s for s in steps if s.get("recipe") == "patch_platform_code"), None)
        for step in steps:
            service.add_event(db, issue.id, "step_started",
                              payload={"plan_id": plan.id, "recipe": step.get("recipe")})
            db.commit()
            try:
                result = _execute_step(issue.id, plan.id, step)
            except Exception as exc:  # noqa: BLE001
                logger.warning("自愈步骤失败 issue=%s plan=%s recipe=%s: %s",
                               issue.id, plan.id, step.get("recipe"), exc)
                _mark_failed(db, issue, plan,
                             f"{step.get('recipe')}: {type(exc).__name__}: {str(exc)[:400]}",
                             rollback_patch=bool(patch_step and
                                                 step.get("recipe") == "patch_platform_code"),
                             user=_get_user(db, plan.approved_by_id),
                             automatic=automatic)
                return {"ok": False, "error": str(exc)[:400]}
            service.add_event(db, issue.id, "step_done", payload={
                "plan_id": plan.id, "recipe": step.get("recipe"),
                "result": {k: v for k, v in (result or {}).items() if k != "receipt"}})
            db.commit()
            if result.get("deferred"):
                # copilotd 已受理重启：进程即将退出，验证由启动收口接续。
                plan.status = "executing"
                plan.result_json = service._dumps({"restart": "delegated"})
                issue.status = "verifying"
                issue.next_action = "wait"
                db.add(plan)
                db.add(issue)
                service.add_event(db, issue.id, "restart_delegated", payload={
                    "plan_id": plan.id, "channel": "qianshu-copilotd"})
                db.commit()
                try:
                    from app.services import audit
                    audit.record(db, action="issue.repair.restart",
                                 user_id=plan.approved_by_id,
                                 username=plan.approved_by or "",
                                 resource_type="issue_plan", resource_id=str(plan.id),
                                 detail={"issue_id": issue.id, "channel": "qianshu-copilotd"})
                    db.commit()
                except Exception:
                    db.rollback()
                return {"ok": True, "deferred": True, "plan_id": plan.id}

        if patch_step:
            from app.services.self_heal import code_repair
            db.refresh(plan)
            ok, detail = code_repair.verify_patch(db, issue, plan)
            if not ok:
                _mark_failed(db, issue, plan, f"代码补丁验证失败：{detail}",
                             rollback_patch=True, user=_get_user(db, plan.approved_by_id),
                             automatic=automatic)
                return {"ok": False, "error": detail}
            if policy.mode_at_least("approved_r2"):
                plan.status = "executing"
                plan.result_json = service._dumps({**json.loads(plan.result_json or "{}"),
                                                   "restart": "delegated_after_patch"})
                issue.status = "verifying"
                issue.next_action = "wait"
                db.add(plan)
                db.add(issue)
                service.add_event(db, issue.id, "restart_delegated",
                                  payload={"plan_id": plan.id,
                                           "reason": "code_patch"})
                db.commit()
                try:
                    from app.services.copilot import change_tools
                    change_tools.copilotd_restart_delegated("self_heal_code_patch")
                except Exception as exc:  # noqa: BLE001
                    # 重启没发起：补丁已落盘，明确转人工，不假装成功。
                    _mark_failed(db, issue, plan,
                                 f"代码补丁已应用但重启失败，需人工重启：{str(exc)[:250]}",
                                 user=_get_user(db, plan.approved_by_id))
                    return {"ok": False, "error": "重启失败"}
                return {"ok": True, "deferred": True, "plan_id": plan.id}
            plan.status = "succeeded"
            plan.result_json = service._dumps({**json.loads(plan.result_json or "{}"),
                                               "note": "补丁已应用，需重启生效"})
            db.add(plan)
            service.add_event(db, issue.id, "verify_done",
                              payload={"plan_id": plan.id,
                                       "detail": "补丁已应用，需重启生效"})
            db.commit()
            service.transition(db, issue, "manual_required", kind="escalated",
                               payload={"reason": "patch_applied_restart_required"})
            return {"ok": True, "restart_required": True}

        from app.services.self_heal import verifier
        db.refresh(plan)
        db.refresh(issue)
        ok, detail = verifier.verify_issue(db, issue, plan)
        service.add_event(db, issue.id, "verify_done",
                          payload={"plan_id": plan.id, "ok": ok,
                                   "detail": detail[:1000]})
        db.commit()
        if ok:
            plan.status = "succeeded"
            plan.result_json = service._dumps({"verified": True, "detail": detail})
            db.add(plan)
            service.transition(db, issue, "recovered", kind="recovered",
                               payload={"plan_id": plan.id, "detail": detail},
                               resolution_md="自动修复后验证通过")
            try:
                from app.services import audit
                audit.record(db, action="issue.repair.verified",
                             user_id=plan.approved_by_id,
                             username=plan.approved_by or "",
                             resource_type="issue_plan", resource_id=str(plan.id),
                             detail={"issue_id": issue.id, "detail": detail[:500]})
                db.commit()
            except Exception:
                db.rollback()
            return {"ok": True, "verified": True, "detail": detail}
        _mark_failed(db, issue, plan, f"执行后验证未通过：{detail}",
                     rollback_patch=bool(patch_step),
                     user=_get_user(db, plan.approved_by_id),
                     automatic=automatic)
        return {"ok": False, "error": detail}
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.warning("自愈执行器异常 plan=%s", plan_id, exc_info=True)
        try:
            if plan is not None:
                plan.status = "failed"
                plan.result_json = service._dumps(
                    {"error": f"executor_exception: {type(exc).__name__}: {str(exc)[:300]}"})
                db.add(plan)
                if issue is not None:
                    service.transition(db, issue, "manual_required", kind="escalated",
                                       payload={"reason": "executor_exception"})
                db.commit()
        except Exception:
            db.rollback()
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    finally:
        db.close()
        with _LOCK:
            _ACTIVE.pop(int(plan_id), None)


def retry_due_auto(limit: int = 3) -> int:
    """维护循环：把到冷却时间的自动失败 R0/R1 计划克隆为新版本并重新执行。

    只克隆原计划（不重新调用 LLM），次数受 max_attempts 限制；任何异常只记日志。
    """
    db = SessionLocal()
    started = 0
    try:
        rows = (db.query(IssueCase)
                .filter(IssueCase.status == "open",
                        IssueCase.next_retry_at != None,  # noqa: E711
                        IssueCase.next_retry_at <= datetime.now(),
                        IssueCase.attempts < IssueCase.max_attempts,
                        IssueCase.recurrence_of_id == None)  # noqa: E711
                .order_by(IssueCase.next_retry_at.asc()).limit(int(limit))
                .with_for_update().all())
        for issue in rows:
            issue.next_retry_at = None
            plan = db.get(IssuePlan, int(issue.current_plan_id or 0))
            if (plan is None or plan.status != "failed"
                    or plan.risk not in ("R0", "R1")
                    or not policy.auto_risk_allowed(plan.risk)):
                db.commit()
                continue
            data = json.loads(plan.plan_json or "{}")
            version = service.new_plan_version(db, issue.id)
            ttl = int(settings.SELF_HEAL_PLAN_TTL_MIN or 1440)
            clone = IssuePlan(
                issue_id=issue.id, version=version, status="proposed",
                risk=plan.risk, plan_json=plan.plan_json,
                plan_hash=validator.plan_hash(data, issue_id=issue.id, version=version),
                diagnosis_md=plan.diagnosis_md, impact_md=plan.impact_md,
                assist_json=plan.assist_json,
                expires_at=datetime.now() + timedelta(minutes=ttl),
                created_by="auto_retry", model=plan.model or "",
            )
            db.add(clone)
            db.flush()
            issue.current_plan_id = clone.id
            issue.status = "awaiting_approval"
            issue.next_action = "approve"
            issue.updated_at = datetime.now()
            db.add(issue)
            service.add_event(db, issue.id, "retry_scheduled", payload={
                "plan_id": clone.id, "from_plan_id": plan.id,
                "attempt": int(issue.attempts or 0) + 1})
            db.commit()
            if start_plan(clone.id, automatic=True):
                started += 1
    except Exception:
        db.rollback()
        logger.warning("自愈自动重试调度异常（下一轮继续）", exc_info=True)
    finally:
        db.close()
    return started


def _job(plan_id: int, user_id: int | None, automatic: bool) -> None:
    try:
        execute_plan(plan_id, user_id=user_id, automatic=automatic)
    finally:
        with _LOCK:
            _ACTIVE.pop(int(plan_id), None)


def start_plan(plan_id: int, *, user_id: int | None = None,
               automatic: bool = False) -> bool:
    """后台启动执行；同一计划单进程内只执行一次。"""
    plan_id = int(plan_id)
    with _LOCK:
        thread = _ACTIVE.get(plan_id)
        if thread is not None and thread.is_alive():
            return False
        thread = threading.Thread(target=_job, args=(plan_id, user_id, automatic),
                                  name=f"self-heal-exec-{plan_id}", daemon=True)
        _ACTIVE[plan_id] = thread
        thread.start()
        return True
