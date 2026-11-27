"""统一问题漏斗：去重、留证、状态机、站内信、并发收口。

上报始终使用独立短事务：调用方的业务事务回滚不能回滚问题单；问题单写失败也不能
反向阻断主业务。同一活动指纹由 PostgreSQL 部分唯一索引 + SELECT FOR UPDATE 双保险。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.db import SessionLocal
from app.models.issue import IssueCase, IssueEvent, IssuePlan
from app.services.self_heal import fingerprint as fp
from app.services.self_heal import notifier, policy

logger = logging.getLogger("qianshu.self_heal")

ACTIVE_STATUSES = (
    "open", "diagnosing", "awaiting_approval", "awaiting_user",
    "repairing", "verifying", "recurring", "manual_required",
)
FINAL_STATUSES = ("recovered", "ignored")
_SEVERITY_RANK = {"info": 1, "warning": 2, "critical": 3}
_STATUS_NEXT_ACTION = {
    "open": "diagnose",
    "diagnosing": "wait",
    "awaiting_approval": "approve",
    "awaiting_user": "assist",
    "repairing": "wait",
    "verifying": "wait",
    "recovered": "none",
    "recurring": "manual_review",
    "manual_required": "manual_review",
    "ignored": "none",
}


def _dumps(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)[:65535]
    except Exception:
        return "{}"


def _loads(raw, default):
    try:
        return json.loads(raw) if raw else default
    except (TypeError, ValueError):
        return default


def add_event(db, issue_id: int, kind: str, *, actor: str = "self_heal",
              actor_id: int | None = None, payload=None) -> IssueEvent:
    event = IssueEvent(issue_id=int(issue_id), kind=(kind or "")[:32],
                       actor=(actor or "")[:64], actor_id=actor_id,
                       payload_json=_dumps(payload or {}))
    db.add(event)
    return event


def _storm_active(db, source: str) -> bool:
    since = datetime.now() - timedelta(minutes=1)
    n = (db.query(func.count(IssueCase.id))
         .filter(IssueCase.source == source, IssueCase.created_at >= since).scalar() or 0)
    return int(n) >= int(settings.SELF_HEAL_STORM_PER_MIN)


def _find_active(db, fingerprint: str) -> IssueCase | None:
    q = (db.query(IssueCase)
         .filter(IssueCase.fingerprint == fingerprint,
                 IssueCase.status.in_(ACTIVE_STATUSES)))
    try:
        q = q.with_for_update()
    except Exception:
        pass
    return q.order_by(IssueCase.id.desc()).first()


def _find_storm_case(db, source: str) -> IssueCase | None:
    since = datetime.now() - timedelta(hours=1)
    return (db.query(IssueCase)
            .filter(IssueCase.source == source,
                    IssueCase.reason_code == "error_storm",
                    IssueCase.status.in_(ACTIVE_STATUSES),
                    IssueCase.last_seen >= since)
            .order_by(IssueCase.id.desc()).first())


def report_issue(
    db=None,
    *,
    source: str,
    fingerprint_parts=None,
    severity: str = "warning",
    title: str,
    reason_code: str = "",
    error="",
    account_id: int = 0,
    run_id: int = 0,
    capability: str = "",
    source_ref=None,
    evidence=None,
    retryable: bool = True,
    fingerprint: str = "",
    target=None,
) -> IssueCase | None:
    """记录/合并一个问题单。返回合并后的问题单；总开关关闭时返回 None。

    db 参数只为兼容调用约定：本函数始终另开短事务，避免与业务回滚边界共享。
    """
    if not policy.enabled():
        return None
    source = (source or "platform").strip().lower()[:32]
    severity = severity if severity in _SEVERITY_RANK else "warning"
    now = datetime.now()
    parts = dict(fingerprint_parts or {})
    if target is None:
        target = {k: parts.get(k) for k in ("region", "source_id", "bill_date",
                                            "product", "vendor", "target", "host")
                  if parts.get(k) is not None}
    finger = fingerprint or fp.make_fingerprint(
        source=source, account_id=account_id, run_id=run_id, capability=capability,
        reason_code=reason_code, error=error, target=target,
        error_class=str(parts.get("error_class") or ""),
    )
    _, signature = fp.error_signature(error)
    error_class = str(parts.get("error_class") or "") or fp.error_signature(error)[0]
    evidence_obj = evidence if isinstance(evidence, dict) else {"detail": evidence or ""}
    evidence_obj = {
        "error_signature": signature,
        "retryable": bool(retryable),
        "captured_at": now.isoformat(),
        **{k: v for k, v in evidence_obj.items() if k not in ("secret", "token", "password")},
    }
    # db 参数只为兼容旧调用约定；上报必须使用独立短事务，绝不能提交/回滚调用方
    # 尚未完成的业务事务（例如接入任务正在写能力证据）。
    session = SessionLocal()
    try:
        existing = _find_active(session, finger)
        if existing is not None:
            existing.occurrences = int(existing.occurrences or 0) + 1
            existing.last_seen = now
            if _SEVERITY_RANK.get(severity, 0) > _SEVERITY_RANK.get(existing.severity, 0):
                existing.severity = severity
            if evidence_obj:
                existing.evidence_json = _dumps(evidence_obj)
            add_event(session, existing.id, "deduped", payload={
                "occurrences": existing.occurrences, "last_seen": now.isoformat()})
            session.commit()
            return _detach(session, existing)

        # 单来源风暴：超过阈值后合并为一条稳定的“错误风暴”，并暂停该来源 LLM。
        if _storm_active(session, source):
            storm = _find_storm_case(session, source)
            if storm is not None:
                storm.occurrences = int(storm.occurrences or 0) + 1
                storm.last_seen = now
                add_event(session, storm.id, "deduped", payload={
                    "storm": True, "occurrences": storm.occurrences})
                session.commit()
                return _detach(session, storm)
            finger = fp.sha256_text("storm:" + source)
            title = f"错误风暴：{source} 短时间内大量失败"
            reason_code = "error_storm"
            severity = "critical"
            retryable = False
            evidence_obj["storm_threshold"] = settings.SELF_HEAL_STORM_PER_MIN

        recurrence_of_id = None
        recurrence = (session.query(IssueCase)
                      .filter(IssueCase.fingerprint == finger,
                              IssueCase.status == "recovered",
                              IssueCase.resolved_at >= now - timedelta(
                                  hours=int(settings.SELF_HEAL_RECURRENCE_HOURS)))
                      .order_by(IssueCase.id.desc()).first())
        if recurrence is not None:
            recurrence_of_id = recurrence.id
            severity = "critical"

        issue = IssueCase(
            fingerprint=finger, source=source,
            source_ref_json=_dumps(source_ref or {}),
            account_id=int(account_id or 0), run_id=int(run_id or 0),
            capability=(capability or "")[:32], severity=severity,
            status="recurring" if recurrence_of_id else "open",
            title=(title or "平台问题")[:255], reason_code=(reason_code or "")[:64],
            error_class=(error_class or "")[:128],
            summary_md=(title or "")[:2000],
            evidence_json=_dumps(evidence_obj),
            occurrences=1, attempts=0,
            max_attempts=int(settings.SELF_HEAL_MAX_ATTEMPTS),
            next_action=("manual_review" if recurrence_of_id
                         else "diagnose" if retryable else "manual_review"),
            first_seen=now, last_seen=now,
            recurrence_of_id=recurrence_of_id,
        )
        session.add(issue)
        try:
            session.flush()
        except IntegrityError:
            # 并发插入：另一个请求已建同指纹活动问题单 -> 合并到那条。
            session.rollback()
            existing = _find_active(session, finger)
            if existing is None:
                raise
            existing.occurrences = int(existing.occurrences or 0) + 1
            existing.last_seen = now
            add_event(session, existing.id, "deduped", payload={"concurrent": True})
            session.commit()
            return _detach(session, existing)
        add_event(session, issue.id, "detected", payload={
            "severity": severity, "source": source, "fingerprint": finger})
        if recurrence_of_id:
            add_event(session, issue.id, "recurred",
                      payload={"recurrence_of_id": recurrence_of_id})
            issue.attempts = issue.max_attempts   # 复发停止自动 R0/R1
        session.commit()
        issue = _detach(session, issue)
        try:
            if recurrence_of_id:
                notifier.notify_issue(session, issue, "recurring")
            else:
                notifier.notify_issue(session, issue, "detected")
            session.commit()
        except Exception:
            session.rollback()
            logger.warning("自愈问题站内信写入失败（问题单已保留）", exc_info=True)
        if (not recurrence_of_id and reason_code != "error_storm"
                and retryable and policy.llm_allowed()):
            try:
                from app.services.self_heal import planner as planner_svc
                planner_svc.start_diagnosis(issue.id, user_id=0)
            except Exception:
                logger.warning("自动诊断启动失败（问题单已保留）", exc_info=True)
        return issue
    except Exception:
        session.rollback()
        logger.warning("自愈问题上报失败（不影响主业务）", exc_info=True)
        return None
    finally:
        session.close()


def _detach(session, obj):
    """提交后把对象摘离会话，调用方读属性不会触发已关闭会话查询。"""
    try:
        session.refresh(obj)
        session.expunge(obj)
    except Exception:
        pass
    return obj


def report_issue_safe(**kwargs) -> IssueCase | None:
    """集成点专用：任何失败只记日志，绝不抛回业务路径。

    返回合并后的问题单（可用于把后续诊断结论写回同一张单）；总开关关闭或
    上报失败时返回 None。
    """
    try:
        return report_issue(**kwargs)
    except Exception:
        logger.warning("自愈上报失败（已忽略，不阻断业务）", exc_info=True)
        return None


def transition(db, issue, status: str, *, kind: str, actor: str = "self_heal",
               actor_id: int | None = None, payload=None, notify: bool = True,
               resolution_md: str = "") -> IssueCase:
    """状态变化唯一入口；必须写 IssueEvent。同一状态不重复发站内信。"""
    if not isinstance(issue, IssueCase):
        issue = db.get(IssueCase, int(issue))
    if issue is None:
        raise ValueError("问题单不存在")
    db.refresh(issue)
    old = issue.status
    issue.status = status
    issue.next_action = _STATUS_NEXT_ACTION.get(status, issue.next_action)
    issue.updated_at = datetime.now()
    if status == "recovered":
        issue.resolved_at = datetime.now()
        issue.resolution_md = resolution_md or issue.resolution_md or "验证通过，已恢复"
    if status == "ignored":
        issue.resolved_at = datetime.now()
    db.add(issue)
    add_event(db, issue.id, kind, actor=actor, actor_id=actor_id,
              payload={"from": old, "to": status, **(payload or {})})
    db.commit()
    if notify and old != status:
        kind_map = {
            "awaiting_approval": "awaiting_approval",
            "awaiting_user": "awaiting_user",
            "recovered": "recovered",
            "recurring": "recurring",
            "manual_required": "escalated",
            "ignored": "ignored",
        }
        message_kind = kind_map.get(status)
        if message_kind:
            try:
                notifier.notify_issue(db, issue, message_kind)
                db.commit()
            except Exception:
                db.rollback()
                logger.warning("自愈状态站内信写入失败", exc_info=True)
    return issue


def ignore_issue(db, issue_id: int, user) -> IssueCase:
    issue = db.get(IssueCase, int(issue_id))
    if issue is None:
        raise ValueError("问题单不存在")
    return transition(db, issue, "ignored", kind="ignored", actor=user.username,
                      actor_id=user.id, payload={"role": user.role})


def resume_assist(db, issue_id: int, user, note: str = "") -> IssueCase:
    """用户回传协助结果：文字只作为证据，不作为指令；状态回到 diagnosing。"""
    issue = db.get(IssueCase, int(issue_id))
    if issue is None:
        raise ValueError("问题单不存在")
    if issue.status != "awaiting_user":
        raise ValueError("问题单不在等待用户处理状态")
    evidence = _loads(issue.evidence_json, {})
    if note:
        evidence["user_assist_note"] = str(note)[:2000]
    issue.evidence_json = _dumps(evidence)
    db.add(issue)
    # transition() 会 refresh 以拿到最新状态；证据必须先提交，否则协助内容会被刷新丢弃。
    db.commit()
    return transition(db, issue, "diagnosing", kind="assist_resumed",
                      actor=user.username, actor_id=user.id,
                      payload={"note_sha256": fp.sha256_text(str(note))[:16]})


def new_plan_version(db, issue_id: int) -> int:
    n = (db.query(func.max(IssuePlan.version))
         .filter(IssuePlan.issue_id == int(issue_id)).scalar() or 0)
    return int(n) + 1


def reconcile_self_heal() -> dict:
    """进程启动后收口中断状态；不重复执行任何动作。

    - repairing 且没有活动 executing 计划：执行器已死，转人工；
    - executing R2 计划：平台已重新启动，标记计划成功并接续问题验证；
    - verifying 问题：尝试重新取证验证（只读），失败转人工。
    """
    # 总开关/熔断关闭时冻结一切自动写：不复查（可能含外部只读探测）、不改状态。
    if not policy.enabled():
        return {"disabled": True}
    from app.services.self_heal import verifier

    db = SessionLocal()
    recovered = escalated = resumed = 0
    try:
        plans = (db.query(IssuePlan)
                 .filter(IssuePlan.status == "executing").all())
        for plan in plans:
            issue = db.get(IssueCase, plan.issue_id)
            if issue is None:
                plan.status = "failed"
                plan.result_json = _dumps({"interrupted": True})
                continue
            if plan.risk == "R2" and issue.status in ("repairing", "verifying"):
                plan.status = "succeeded"
                plan.result_json = _dumps({"restart": "resumed_after_startup"})
                add_event(db, issue.id, "verify_done", payload={
                    "plan_id": plan.id, "resumed": True, "risk": "R2"})
                resumed += 1
            else:
                plan.status = "failed"
                plan.result_json = _dumps({"interrupted": True,
                                           "note": "进程中断，未重复执行"})
                add_event(db, issue.id, "escalated", payload={
                    "plan_id": plan.id, "reason": "executor_interrupted"})
                transition(db, issue, "manual_required", kind="escalated",
                           payload={"reason": "executor_interrupted"})
                escalated += 1
        db.commit()

        for issue in db.query(IssueCase).filter(
                IssueCase.status.in_(("repairing", "verifying"))).all():
            plan = (db.query(IssuePlan)
                    .filter(IssuePlan.issue_id == issue.id,
                            IssuePlan.status.in_(("approved", "succeeded", "executing")))
                    .order_by(IssuePlan.version.desc()).first())
            if plan is None:
                transition(db, issue, "manual_required", kind="escalated",
                           payload={"reason": "no_plan_after_restart"})
                escalated += 1
                continue
            try:
                ok, detail = verifier.verify_issue(db, issue, plan)
            except Exception as exc:  # noqa: BLE001
                ok, detail = False, f"验证异常：{type(exc).__name__}: {str(exc)[:200]}"
            if ok:
                plan.status = "succeeded"
                plan.result_json = _dumps({"verified_after_restart": True})
                transition(db, issue, "recovered", kind="recovered",
                           payload={"plan_id": plan.id, "detail": detail},
                           resolution_md="重启后验证通过")
                recovered += 1
            else:
                transition(db, issue, "manual_required", kind="escalated",
                           payload={"plan_id": plan.id, "detail": detail})
                escalated += 1
        db.commit()
        return {"recovered": recovered, "escalated": escalated, "resumed_plans": resumed}
    except Exception:
        db.rollback()
        logger.warning("自愈启动收口异常（不阻塞启动）", exc_info=True)
        return {"error": True}
    finally:
        db.close()


def _json(raw, default):
    return _loads(raw, default)


def plan_out(plan: IssuePlan, *, include_plan: bool = True) -> dict:
    data = {
        "id": plan.id, "issue_id": plan.issue_id, "version": plan.version,
        "status": plan.status, "risk": plan.risk, "plan_hash": plan.plan_hash,
        "diagnosis_md": plan.diagnosis_md or "", "impact_md": plan.impact_md or "",
        "assist": _json(plan.assist_json, {}),
        "expires_at": plan.expires_at.isoformat() if plan.expires_at else None,
        "created_by": plan.created_by, "model": plan.model or "",
        "approved_by": plan.approved_by or "",
        "approved_at": plan.approved_at.isoformat() if plan.approved_at else None,
        "rejected_by": plan.rejected_by or "",
        "reject_reason": plan.reject_reason or "",
        "result": _json(plan.result_json, {}),
        "created_at": plan.created_at.isoformat() if plan.created_at else None,
    }
    if include_plan:
        data["plan"] = _json(plan.plan_json, {})
    return data


def issue_out(issue: IssueCase, *, detail: bool = False) -> dict:
    data = {
        "id": issue.id, "fingerprint": issue.fingerprint, "source": issue.source,
        "source_ref": _json(issue.source_ref_json, {}),
        "account_id": issue.account_id, "run_id": issue.run_id,
        "capability": issue.capability or "", "severity": issue.severity,
        "status": issue.status, "title": issue.title,
        "reason_code": issue.reason_code or "", "error_class": issue.error_class or "",
        "summary_md": issue.summary_md or "", "occurrences": issue.occurrences or 1,
        "attempts": issue.attempts or 0, "max_attempts": issue.max_attempts or 3,
        "current_plan_id": issue.current_plan_id,
        "next_action": issue.next_action or "",
        "next_retry_at": issue.next_retry_at.isoformat() if issue.next_retry_at else None,
        "first_seen": issue.first_seen.isoformat() if issue.first_seen else None,
        "last_seen": issue.last_seen.isoformat() if issue.last_seen else None,
        "resolved_at": issue.resolved_at.isoformat() if issue.resolved_at else None,
        "resolution_md": issue.resolution_md or "",
        "recurrence_of_id": issue.recurrence_of_id,
        "created_at": issue.created_at.isoformat() if issue.created_at else None,
        "updated_at": issue.updated_at.isoformat() if issue.updated_at else None,
    }
    # 与模型证据包同一套脱敏：问题单页面对管理员展示，但不应回显凭证/长 Token。
    from app.services.self_heal import evidence as evidence_svc
    data["source_ref"] = evidence_svc.redact(data["source_ref"])
    if detail:
        data["root_cause_md"] = evidence_svc._scrub_string(issue.root_cause_md or "", 8000)
        data["impact_md"] = evidence_svc._scrub_string(issue.impact_md or "", 4000)
        data["evidence"] = evidence_svc.redact(_json(issue.evidence_json, {}))
    return data


def event_out(event: IssueEvent) -> dict:
    return {
        "id": event.id, "kind": event.kind, "actor": event.actor,
        "actor_id": event.actor_id, "payload": _json(event.payload_json, {}),
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }
