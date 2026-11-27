from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import require_roles
from app.models.audit import AuditLog
from app.services.audit import ACTION_LABELS, humanize

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("/logs")
def list_audit_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    action: str = "",
    resource_type: str = "",
    db: Session = Depends(get_db),
    _=Depends(require_roles("admin")),
):
    q = select(AuditLog).order_by(AuditLog.id.desc())
    if action:
        # 支持中文过滤（"登录"）与代码过滤（"login"），也支持模糊（登录类动作一网打尽）
        codes = [c for c, label in ACTION_LABELS.items() if action in (c, label)]
        if codes:
            q = q.where(AuditLog.action.in_(codes))
        else:
            like = f"%{action}%"
            matched = [c for c, label in ACTION_LABELS.items()
                       if action in label or action in c]
            q = q.where(or_(AuditLog.action.in_(matched), AuditLog.action.ilike(like))) if matched \
                else q.where(AuditLog.action.ilike(like))
    if resource_type:
        q = q.where(AuditLog.resource_type == resource_type)
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    rows = db.execute(q.offset((page - 1) * page_size).limit(page_size)).scalars().all()
    return {
        "total": total,
        "items": [
            {
                "id": r.id, "user_id": r.user_id, "username": r.username,
                "action": r.action, "resource_type": r.resource_type,
                "resource_id": r.resource_id, "detail": r.detail,
                "result": r.result, "client_ip": r.client_ip,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                # 中文动作名与摘要让审计列表直接可读
                **humanize(r.action, r.resource_type, r.resource_id, r.detail, r.result),
            }
            for r in rows
        ],
    }
