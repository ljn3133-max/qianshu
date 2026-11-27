from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class InAppMessage(Base):
    """站内信统一入口：巡检报告/告警/系统通知先落这里，推送（机器人）是可选项。"""

    __tablename__ = "in_app_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(255))
    content_md: Mapped[str] = mapped_column(Text, default="")
    level: Mapped[str] = mapped_column(String(16), default="info")        # info/warning/critical
    msg_type: Mapped[str] = mapped_column(String(16), default="system")   # alert/inspection/system
    ref_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 关联 alert_record.id 等
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
