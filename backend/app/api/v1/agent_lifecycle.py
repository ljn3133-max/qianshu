"""千枢生命周期通道的服务端桥。

qianshu-copilotd（独立守护进程）经此：
  - 把自愈事件写进平台审计（平台恢复后补报也走这里）；
  - 查询最近变更（agent_change_tickets）与审计流水，用于故障定位"最近改了什么、
    哪些可回滚"。

平台自愈事件统一进入问题总线（IssueCase），按新策略只发**站内信**：
检测/修复/恢复各最多一条，不推企微/钉钉/短信；完整动作仍落审计，排障可查。

安全：鉴权 X-Copilotd-Token == COPILOTD_BRIDGE_TOKEN（同机守护专属共享 token，
同 events_webhook 模式；未配置=403 拒收）；只读查询 + 审计写入，
**没有任何命令/变更通道**。动作码白名单收口，防止被当成通用写入口。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.client_ip import get_client_ip
from app.core.config import settings
from app.core.db import get_db
from app.models.agent_change import AgentChangeTicket
from app.models.audit import AuditLog
from app.services.audit import record as audit_record

logger = logging.getLogger("qianshu.agent_lifecycle")
router = APIRouter(prefix="/agent/lifecycle", tags=["agent-lifecycle"])

# 动作码白名单（守护只许上报这几类自愈事件）
ALLOWED_ACTIONS = ("copilotd.detect", "copilotd.heal", "copilotd.restart", "copilotd.recovered")
ALLOWED_LEVELS = ("info", "warning", "critical")


def _check_token(request: Request) -> None:
    token = request.headers.get("x-copilotd-token", "")
    if not settings.COPILOTD_BRIDGE_TOKEN or token != settings.COPILOTD_BRIDGE_TOKEN:
        raise HTTPException(403, "copilotd token 校验失败（配置 COPILOTD_BRIDGE_TOKEN）")


class LifecycleReportIn(BaseModel):
    action: str
    title: str = ""
    content_md: str = ""
    level: str = "warning"
    detail: Optional[dict] = None


@router.get("/recent")
def recent_changes(request: Request, db: Session = Depends(get_db)):
    """最近变更工单 + 审计流水（故障定位用，只读）。"""
    _check_token(request)
    tickets = (db.query(AgentChangeTicket)
               .order_by(AgentChangeTicket.id.desc()).limit(10).all())
    audits = db.query(AuditLog).order_by(AuditLog.id.desc()).limit(10).all()
    pending = (db.query(AgentChangeTicket)
               .filter(AgentChangeTicket.status == "pending").count())
    return {
        "ok": True,
        "pending_tickets": pending,
        "change_tickets": [
            {"id": t.id, "title": t.title, "status": t.status,
             "confirmed_by": t.confirmed_by,
             "created_at": t.created_at.isoformat() if t.created_at else None}
            for t in tickets
        ],
        "audit": [
            {"id": a.id, "action": a.action, "username": a.username,
             "result": a.result, "resource_type": a.resource_type,
             "created_at": a.created_at.isoformat() if a.created_at else None}
            for a in audits
        ],
    }


@router.post("/report")
def lifecycle_report(body: LifecycleReportIn, request: Request, db: Session = Depends(get_db)):
    """自愈事件 → 问题总线 + 审计 + 站内信（仅状态变化）。"""
    _check_token(request)
    if body.action not in ALLOWED_ACTIONS:
        raise HTTPException(400, f"action 不在白名单: {body.action}")
    level = body.level if body.level in ALLOWED_LEVELS else "warning"
    title = (body.title or "千枢自愈事件")[:255]

    audit_record(
        db,
        action=body.action,
        username="copilotd",
        resource_type="platform",
        resource_id="qianshu",
        detail={"title": title, "content_md": (body.content_md or "")[:2000],
                "level": level, **(body.detail or {})},
        result="ok",
        client_ip=get_client_ip(request),
    )
    db.commit()
    try:
        from app.models.issue import IssueCase
        from app.services.self_heal import service as issue_service
        from app.services.self_heal.fingerprint import sha256_text

        fingerprint = sha256_text("copilotd:platform")
        active = (db.query(IssueCase)
                  .filter(IssueCase.fingerprint == fingerprint,
                          IssueCase.status.in_(issue_service.ACTIVE_STATUSES))
                  .order_by(IssueCase.id.desc()).first())
        if body.action == "copilotd.recovered":
            if active is not None:
                issue_service.transition(
                    db, active, "recovered", kind="copilotd.recovered",
                    actor="copilotd",
                    payload={"title": title, "content": (body.content_md or "")[:500]},
                    resolution_md="copilotd 检测到平台已恢复")
        else:
            if active is None:
                reported = issue_service.report_issue(
                    source="copilotd", severity=level,
                    title=f"千枢守护事件：{title}",
                    reason_code=body.action, error="",
                    fingerprint=fingerprint,
                    target={"target": "qianshu-platform"},
                    source_ref={"action": body.action,
                                "detail": body.detail or {}},
                    evidence={"action": body.action,
                              "content": (body.content_md or "")[:1000]},
                    retryable=False,
                )
                active = reported
            elif active is not None:
                issue_service.add_event(
                    db, active.id, body.action, actor="copilotd",
                    payload={"title": title, "level": level,
                             "content": (body.content_md or "")[:1000]})
                db.commit()
    except Exception:
        db.rollback()
        logger.warning("copilotd 事件写入问题总线失败（审计已保留）", exc_info=True)
    logger.info("copilotd 上报 %s -> 问题总线/审计/站内信", body.action)
    return {"ok": True, "message_ids": [], "audit": True,
            "at": datetime.now().isoformat()}
