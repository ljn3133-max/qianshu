"""权限策略：用户级 allow/deny 覆盖角色默认。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class PermissionPolicy(Base):
    __tablename__ = "permission_policies"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    code: Mapped[str] = mapped_column(String(64))
    effect: Mapped[str] = mapped_column(String(8))   # allow / deny
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
