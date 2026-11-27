"""到期提醒忽略清单。

某些到期项（已计划续费/不关心）无需持续提醒，可在行内"忽略"；
默认列表与决策清单都跳过，且可随时恢复。
"""
from __future__ import annotations

from datetime import datetime

from typing import Optional

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class ExpiryIgnore(Base):
    __tablename__ = "expiry_ignores"

    id: Mapped[int] = mapped_column(primary_key=True)
    target_key: Mapped[str] = mapped_column(String(191), unique=True, index=True)  # category:instance_id/name
    note: Mapped[str] = mapped_column(Text, default="")
    # 身份引用：created_by=username 快照，created_by_id=users.id
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_by_id: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # NULL=永久忽略（保持现语义）；有值=到期自动失效（"忽略 30 天"）
    until_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
