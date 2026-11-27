from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class EcsOpsLock(Base):
    """ECS 操作互斥锁：同一实例同时只允许一个在途操作，防止重复下发。"""

    __tablename__ = "ecs_ops_locks"

    id: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[int] = mapped_column(Integer, index=True)
    instance_id: Mapped[str] = mapped_column(String(128), index=True)
    op: Mapped[str] = mapped_column(String(16))  # reboot/stop/start
    locked_by: Mapped[int] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime)  # 过期自动失效，防死锁
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
