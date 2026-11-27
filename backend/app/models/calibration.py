"""引导式校准：每周"我学到的"确认清单。

平台把新发现的机器、学到的常态水位和能力变化摊开给人确认，人只做拍板。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class CalibrationItem(Base):
    __tablename__ = "calibration_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    week: Mapped[str] = mapped_column(String(16), index=True)     # ISO 周，如 2026-W44
    item_type: Mapped[str] = mapped_column(String(32))            # new_host/new_domain/baseline/unlock/suggest
    ref: Mapped[str] = mapped_column(String(191), default="")     # 对象标识
    content: Mapped[str] = mapped_column(Text, default="")        # "我学到的：xxx（请确认）"
    status: Mapped[str] = mapped_column(String(16), default="pending")   # pending/confirmed
    # 身份引用：confirmed_by=username 快照，confirmed_by_id=users.id
    confirmed_by: Mapped[str] = mapped_column(String(64), default="")
    confirmed_by_id: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
