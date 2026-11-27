"""千枢智能体 · 工作流定时体检 API。

与 agent_workflows.py 同构并共用 /api/agent 前缀，独立成文以避免与会话 SSE 模块耦合。

  GET   /api/agent/schedules          定时清单（含上次/下次执行）
  POST  /api/agent/schedules          新增定时（分析域内置 + 启用中的自定义工作流；
                                      同 key 已有定时 409）
  PATCH /api/agent/schedules/{id}     修改（interval_days/hour/enabled；enabled=false=停用，
                                      停用即"删除"口径——表留痕不丢，故不设 DELETE）

执行不在本文件：main 后台循环小时级检查直调 services/agent_schedule.run_due；
有异动才发站内信（msg_type=inspection），无异动静默。审计：agent.schedule.create/update。
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import require_roles
from app.core.i18n_msg import lang_of, pick
from app.models.agent_schedule import AgentWorkflowSchedule
from app.models.user import User
from app.services import agent_schedule as sched
from app.services.audit import record as audit_record

logger = logging.getLogger("qianshu.agent_schedules")
router = APIRouter(prefix="/agent", tags=["agent-schedules"])

_ROLES = ("admin", "operator", "viewer")
_WRITE_ROLES = ("admin", "operator")


def _item_out(db: Session, s: AgentWorkflowSchedule, now=None, lang: str = "zh") -> dict:
    from app.models.workflow_def import WorkflowDef
    from app.services.copilot import workflows as wf_mod

    spec = wf_mod.WORKFLOWS.get(s.workflow_key) or {}
    domain = pick(spec.get("domain"), lang) or ""
    if not domain:   # 自定义工作流：域名（域）字段回落 workflow_defs
        row = db.query(WorkflowDef).filter(WorkflowDef.key == s.workflow_key).first()
        domain = (row.domain or "") if row is not None else ""
    nxt = sched.next_run_at(s.last_run_at, s.interval_days, s.hour, now)
    return {
        "id": s.id,
        "workflow_key": s.workflow_key,
        "workflow_name": sched.workflow_label(db, s.workflow_key, lang),
        "domain": domain,
        "interval_days": s.interval_days,
        "hour": s.hour,
        "enabled": bool(s.enabled),
        "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None,
        "next_run_at": nxt.isoformat() if nxt else None,
        "created_by": s.created_by or "",
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }


def _validate(db: Session, workflow_key: str,
              interval_days: Optional[int], hour: Optional[int]) -> tuple:
    if workflow_key not in sched.schedulable_keys(db):
        raise HTTPException(
            400, f"仅分析域工作流与启用中的自定义工作流可定时（{workflow_key} 不可定时或已停用）")
    iv = 1 if interval_days is None else int(interval_days)
    if iv < 1 or iv > 365:
        raise HTTPException(400, "interval_days 取值 1~365")
    h = 9 if hour is None else int(hour)
    if h < 0 or h > 23:
        raise HTTPException(400, "hour 取值 0~23")
    return iv, h


class ScheduleIn(BaseModel):
    workflow_key: str
    interval_days: Optional[int] = None
    hour: Optional[int] = None
    enabled: Optional[bool] = None


class SchedulePatchIn(BaseModel):
    interval_days: Optional[int] = None
    hour: Optional[int] = None
    enabled: Optional[bool] = None


@router.get("/schedules")
def list_schedules(request: Request, db: Session = Depends(get_db),
                   _user: User = Depends(require_roles(*_ROLES))):
    """定时清单（含上次/下次执行；下次=待触发时为 null）。workflow_name/domain 按 Accept-Language 出文。"""
    lang = lang_of(request)
    rows = db.query(AgentWorkflowSchedule).order_by(AgentWorkflowSchedule.id.asc()).all()
    return {"items": [_item_out(db, s, lang=lang) for s in rows], "total": len(rows)}


@router.post("/schedules")
def create_schedule(body: ScheduleIn, request: Request,
                    db: Session = Depends(get_db),
                    user: User = Depends(require_roles(*_WRITE_ROLES))):
    """新增定时体检（分析域内置 + 启用中的自定义）。同工作流只允许一条定时（修改走 PATCH）。"""
    iv, h = _validate(db, body.workflow_key, body.interval_days, body.hour)
    exists = (db.query(AgentWorkflowSchedule)
              .filter(AgentWorkflowSchedule.workflow_key == body.workflow_key).first())
    if exists is not None:
        raise HTTPException(409, "该工作流已有定时，请用 PATCH 修改（或先停用旧的）")
    row = AgentWorkflowSchedule(
        workflow_key=body.workflow_key, interval_days=iv, hour=h,
        enabled=True if body.enabled is None else bool(body.enabled),
        created_by=user.username or "",
        created_by_id=user.id,   # created_by_id 引用 users.id
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    audit_record(db, action="agent.schedule.create", user_id=user.id, username=user.username,
                 resource_type="agent_workflow_schedule", resource_id=row.id,
                 detail={"workflow_key": row.workflow_key, "interval_days": row.interval_days,
                         "hour": row.hour, "enabled": bool(row.enabled)})
    db.commit()
    return _item_out(db, row, lang=lang_of(request))


@router.patch("/schedules/{schedule_id}")
def patch_schedule(schedule_id: int, body: SchedulePatchIn, request: Request,
                   db: Session = Depends(get_db),
                   user: User = Depends(require_roles(*_WRITE_ROLES))):
    """修改定时（间隔/时刻/启用）。enabled=false=停用（删除口径：表留痕不丢）。"""
    row = db.get(AgentWorkflowSchedule, schedule_id)
    if row is None:
        raise HTTPException(404, "定时不存在")
    changed: dict = {}
    if body.interval_days is not None:
        iv = int(body.interval_days)
        if iv < 1 or iv > 365:
            raise HTTPException(400, "interval_days 取值 1~365")
        row.interval_days = iv
        changed["interval_days"] = iv
    if body.hour is not None:
        h = int(body.hour)
        if h < 0 or h > 23:
            raise HTTPException(400, "hour 取值 0~23")
        row.hour = h
        changed["hour"] = h
    if body.enabled is not None:
        row.enabled = bool(body.enabled)
        changed["enabled"] = bool(body.enabled)
    db.commit()
    db.refresh(row)
    audit_record(db, action="agent.schedule.update", user_id=user.id, username=user.username,
                 resource_type="agent_workflow_schedule", resource_id=row.id,
                 detail={"workflow_key": row.workflow_key, **changed})
    db.commit()
    return _item_out(db, row, lang=lang_of(request))
