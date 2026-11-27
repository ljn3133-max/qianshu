"""平台自愈问题单 API：收件箱、诊断、重试、协助、按哈希授权、回滚。

所有危险动作都没有“执行任意命令”的入口；执行器只认固定 recipe 和结构化参数。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import get_current_user, require_roles
from app.models.issue import IssueCase, IssueEvent, IssuePlan
from app.models.user import User
from app.services.self_heal import executor, planner, policy, service

router = APIRouter(prefix="/issues", tags=["issues"])


class ApproveIn(BaseModel):
    plan_hash: str = Field(min_length=16, max_length=128)
    ack: bool = False


class RejectIn(BaseModel):
    reason: str = Field(default="", max_length=500)


class AssistIn(BaseModel):
    note: str = Field(default="", max_length=2000)


def _issue_or_404(db: Session, issue_id: int) -> IssueCase:
    issue = db.get(IssueCase, int(issue_id))
    if issue is None:
        raise HTTPException(404, "问题单不存在")
    return issue


def _latest_plan(db: Session, issue: IssueCase) -> IssuePlan | None:
    if issue.current_plan_id:
        plan = db.get(IssuePlan, int(issue.current_plan_id))
        if plan is not None:
            return plan
    return (db.query(IssuePlan).filter(IssuePlan.issue_id == issue.id)
            .order_by(IssuePlan.version.desc()).first())


def _allowed(db: Session, issue: IssueCase, plan: IssuePlan | None, user: User) -> dict:
    role = getattr(user, "role", "")
    steps = []
    if plan is not None:
        try:
            steps = json.loads(plan.plan_json or "{}").get("steps") or []
        except ValueError:
            steps = []
    can_retry = bool(plan and plan.status in ("proposed", "expired", "failed")
                     and steps and plan.risk in ("R0", "R1")
                     and plan.expires_at and plan.expires_at > datetime.now()
                     and role in ("admin", "operator"))
    can_approve = bool(plan and plan.status == "proposed" and role == "admin"
                       and issue.status not in ("recovered", "ignored"))
    # 模式未开放 R2/R3 时不要给出“点了必失败”的授权按钮；返回明确阻塞原因。
    approve_allowed = bool(can_approve and plan
                           and policy.can_execute(plan.risk, automatic=False, user=user))
    return {
        "diagnose": issue.status not in ("recovered", "ignored")
                    and role in ("admin", "operator"),
        "retry": can_retry,
        "approve": approve_allowed,
        "approve_blocked": bool(can_approve and not approve_allowed),
        "reject": bool(plan and plan.status == "proposed" and role == "admin"),
        "assist": issue.status == "awaiting_user" and role in ("admin", "operator"),
        "ignore": role == "admin" and issue.status not in ("recovered", "ignored"),
        "rollback": role == "admin" and bool(plan and plan.risk == "R3"
                                             and plan.status in ("failed", "succeeded")),
    }


@router.get("/meta")
def meta(user: User = Depends(get_current_user)):
    return {
        "enabled": policy.enabled(),
        "mode": policy.mode(),
        "llm_enabled": policy.llm_allowed(),
        "breaker": executor.breaker_state(),
        "role": getattr(user, "role", ""),
        "risks": {
            "R0": "只读/重试，可在平台内自动执行",
            "R1": "平台内低风险写，可在平台内自动执行",
            "R2": "重启千枢后端/平台内配置，管理员按哈希授权",
            "R3": "修改千枢平台代码，管理员看 diff 后按哈希授权",
            "R4": "禁止：主机/系统/外部写/删库/改权限/改自愈核心",
        },
    }


def _issue_stats(db: Session) -> dict:
    """运营统计：全量口径，不受列表筛选影响（筛选只影响 total/items）。"""
    rows = (db.query(IssueCase.status, func.count(IssueCase.id))
            .group_by(IssueCase.status).all())
    by_status = {str(status): int(n) for status, n in rows}
    active = sum(n for status, n in by_status.items()
                 if status in service.ACTIVE_STATUSES)
    return {
        "total": sum(by_status.values()),
        "active": active,
        "recovered": by_status.get("recovered", 0),
        "awaiting_approval": by_status.get("awaiting_approval", 0),
        "manual_required": by_status.get("manual_required", 0),
        "recurring": by_status.get("recurring", 0),
        "ignored": by_status.get("ignored", 0),
    }


@router.get("")
def list_issues(
    status: str = Query("", description="open,awaiting_approval,recovered 等；空=全部"),
    severity: str = Query(""),
    source: str = Query(""),
    account_id: int = Query(0),
    run_id: int = Query(0),
    hours: int = Query(0, ge=0, le=8760, description="只看最近 N 小时内活跃的问题"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(IssueCase)
    if status:
        q = q.filter(IssueCase.status.in_([s.strip() for s in status.split(",") if s.strip()]))
    if severity:
        q = q.filter(IssueCase.severity == severity)
    if source:
        q = q.filter(IssueCase.source == source)
    if account_id:
        q = q.filter(IssueCase.account_id == int(account_id))
    if run_id:
        q = q.filter(IssueCase.run_id == int(run_id))
    if hours:
        q = q.filter(IssueCase.last_seen >= datetime.now() - timedelta(hours=int(hours)))
    total = q.count()
    rows = (q.order_by(IssueCase.last_seen.desc())
            .offset((page - 1) * page_size).limit(page_size).all())
    return {
        "total": total, "page": page, "page_size": page_size,
        "mode": policy.mode(),
        "stats": _issue_stats(db),
        "items": [service.issue_out(i) for i in rows],
    }


@router.get("/{issue_id}")
def get_issue(
    issue_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    issue = _issue_or_404(db, issue_id)
    plans = (db.query(IssuePlan).filter(IssuePlan.issue_id == issue.id)
             .order_by(IssuePlan.version.desc()).limit(20).all())
    events = (db.query(IssueEvent).filter(IssueEvent.issue_id == issue.id)
              .order_by(IssueEvent.id.asc()).limit(200).all())
    current = _latest_plan(db, issue)
    data = service.issue_out(issue, detail=True)
    data["plans"] = [service.plan_out(p) for p in plans]
    data["events"] = [service.event_out(e) for e in events]
    data["current_plan"] = service.plan_out(current) if current else None
    data["allowed"] = _allowed(db, issue, current, user)
    data["mode"] = policy.mode()
    return data


@router.post("/{issue_id}/diagnose", status_code=202)
def diagnose(
    issue_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    issue = _issue_or_404(db, issue_id)
    if issue.status in ("recovered", "ignored"):
        raise HTTPException(409, "该问题单已关闭")
    if issue.status == "diagnosing":
        raise HTTPException(409, "诊断已在进行中")
    if issue.status in ("repairing", "verifying"):
        raise HTTPException(409, "修复/验证进行中，请等待完成")
    last_plan = _latest_plan(db, issue)
    if last_plan is not None and last_plan.created_at \
            and last_plan.created_at > datetime.now() - timedelta(seconds=60):
        raise HTTPException(409, "刚生成过计划，请 60 秒后再试，避免重复消耗模型")
    accepted = planner.start_diagnosis(issue.id, user_id=user.id)
    if not accepted:
        raise HTTPException(409, "该问题单已有诊断任务在运行")
    return {"accepted": True, "issue_id": issue.id, "status": "diagnosing"}


@router.post("/{issue_id}/retry", status_code=202)
def retry(
    issue_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """人工触发一次固定 R0/R1 重试。不生成任意命令，也不允许 R2/R3。"""
    issue = _issue_or_404(db, issue_id)
    if issue.status in ("recovered", "ignored"):
        raise HTTPException(409, "该问题单已关闭")
    if issue.status in ("repairing", "verifying", "diagnosing"):
        raise HTTPException(409, "该问题单正在处理中")
    plan = _latest_plan(db, issue)
    if plan is not None and plan.status == "proposed" and plan.risk in ("R2", "R3"):
        raise HTTPException(403, "该计划需要管理员在问题详情按完整计划哈希授权")
    if plan is None or not plan.plan_json or plan.status in ("expired", "failed", "rejected"):
        plan = planner.create_plan(db, issue, user_id=user.id, use_llm=False,
                                   reason="用户手动重试，使用固定 R0 方案。")
    try:
        steps = json.loads(plan.plan_json or "{}").get("steps") or []
    except ValueError:
        steps = []
    if not steps:
        raise HTTPException(409, "当前计划没有可自动执行的 R0 步骤，请按人工协助步骤处理后点继续")
    if plan.risk not in ("R0", "R1"):
        raise HTTPException(403, "重试接口只允许 R0/R1；敏感计划请走授权")
    if plan.status == "proposed":
        try:
            executor.approve_plan(db, issue.id, plan.id, user,
                                  plan_hash=plan.plan_hash, ack=True)
        except executor.PlanApprovalError as exc:
            raise HTTPException(403 if exc.code == "forbidden" else 409, str(exc))
    executor.start_plan(plan.id, user_id=user.id)
    return {"accepted": True, "plan_id": plan.id, "risk": plan.risk}


@router.post("/{issue_id}/assist", status_code=202)
def assist(
    issue_id: int,
    body: AssistIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    issue = _issue_or_404(db, issue_id)
    try:
        service.resume_assist(db, issue.id, user, body.note)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    planner.start_diagnosis(issue.id, user_id=user.id)
    return {"accepted": True, "issue_id": issue.id, "status": "diagnosing"}


@router.post("/{issue_id}/ignore")
def ignore(
    issue_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    issue = _issue_or_404(db, issue_id)
    if issue.status == "ignored":
        return {"ok": True, "already": True}
    service.ignore_issue(db, issue.id, user)
    return {"ok": True, "issue_id": issue.id}


@router.post("/{issue_id}/plans/{plan_id}/approve")
def approve_plan(
    issue_id: int,
    plan_id: int,
    body: ApproveIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    try:
        out = executor.approve_plan(db, issue_id, plan_id, user,
                                    plan_hash=body.plan_hash, ack=body.ack)
    except executor.PlanApprovalError as exc:
        code = 404 if exc.code == "not_found" else (
            403 if exc.code in ("forbidden", "breaker_open") else
            410 if exc.code == "expired" else 409)
        raise HTTPException(code, str(exc))
    executor.start_plan(out["plan"].id, user_id=user.id)
    return {"ok": True, "plan": service.plan_out(out["plan"]),
            "status": "executing"}


@router.post("/{issue_id}/plans/{plan_id}/reject")
def reject_plan(
    issue_id: int,
    plan_id: int,
    body: RejectIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    try:
        plan = executor.reject_plan(db, issue_id, plan_id, user, body.reason)
    except executor.PlanApprovalError as exc:
        raise HTTPException(404 if exc.code == "not_found" else 409, str(exc))
    return {"ok": True, "plan": service.plan_out(plan)}


@router.post("/{issue_id}/plans/{plan_id}/rollback")
def rollback_plan(
    issue_id: int,
    plan_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    issue = _issue_or_404(db, issue_id)
    plan = db.get(IssuePlan, int(plan_id))
    if plan is None or plan.issue_id != issue.id:
        raise HTTPException(404, "计划不存在")
    if plan.risk != "R3":
        raise HTTPException(400, "只有代码补丁计划需要回滚")
    from app.services.self_heal import code_repair
    result = code_repair.rollback_patch(db, issue, plan, user, reason="admin_requested")
    if not result.get("ok"):
        raise HTTPException(409, f"回滚失败：{result.get('error') or result.get('conflicts')}")
    plan.status = "rolled_back"
    db.add(plan)
    try:
        from app.services import audit
        audit.record(db, action="issue.repair.rolled_back",
                     user_id=user.id, username=user.username,
                     resource_type="issue_plan", resource_id=str(plan.id),
                     detail={"issue_id": issue.id,
                             "snapshot_id": result.get("snapshot_id")})
    except Exception:
        pass
    service.transition(db, issue, "manual_required", kind="rolled_back",
                       actor=user.username, actor_id=user.id,
                       payload={"plan_id": plan.id, "reason": "admin_rollback"})
    return {"ok": True, "snapshot_id": result.get("snapshot_id"),
            "restored": result.get("restored")}


@router.post("/system/breaker/reset")
def reset_breaker(
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    executor.reset_breaker(actor=user.username)
    try:
        from app.services import audit
        audit.record(db, action="issue.breaker.reset", user_id=user.id,
                     username=user.username, resource_type="self_heal",
                     detail={"mode": policy.mode()})
        db.commit()
    except Exception:
        db.rollback()
    return {"ok": True, "breaker": executor.breaker_state()}
