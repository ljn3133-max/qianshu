"""首页答案页 API：GET /dashboard/summary 聚合待办。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import require_roles
from app.models.user import User
from app.services.dashboard import summary

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/summary")
def dashboard_summary(
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """今日答案：有没有事、管哪件、怎么办（纯只读聚合）。"""
    return summary(db)
