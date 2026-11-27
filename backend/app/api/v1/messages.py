"""站内信 API：所有告警/巡检/系统通知的必达收口。"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import get_current_user, require_roles
from app.models.in_app_message import InAppMessage
from app.models.user import User

logger = logging.getLogger("qianshu.messages")
router = APIRouter(prefix="/messages", tags=["messages"])


@router.get("/inbox")
def inbox(
    unread_only: bool = Query(False),
    page: int = Query(1, ge=1),
    page_size: int = Query(15, ge=1, le=50),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """当前用户的站内信。"""
    q = db.query(InAppMessage).filter(InAppMessage.user_id == user.id)
    if unread_only:
        q = q.filter(InAppMessage.read_at.is_(None))
    total = q.count()
    unread = (
        db.query(InAppMessage)
        .filter(InAppMessage.user_id == user.id, InAppMessage.read_at.is_(None))
        .count()
    )
    rows = (
        q.order_by(InAppMessage.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "total": total,
        "unread": unread,
        "items": [
            {
                "id": r.id, "title": r.title, "content_md": r.content_md,
                "level": r.level, "msg_type": r.msg_type, "ref_id": r.ref_id,
                "read": r.read_at is not None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


@router.post("/{message_id}/read")
def mark_read(
    message_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = db.get(InAppMessage, message_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(404, "消息不存在")
    if row.read_at is None:
        row.read_at = datetime.now()
        db.commit()
    return {"ok": True}


@router.post("/read-all")
def mark_all_read(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    n = (
        db.query(InAppMessage)
        .filter(InAppMessage.user_id == user.id, InAppMessage.read_at.is_(None))
        .update({InAppMessage.read_at: datetime.now()})
    )
    db.commit()
    return {"ok": True, "marked": n}
