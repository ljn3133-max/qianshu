"""处置复查任务：跟踪建议执行后的恢复结果。

诊断/自动诊断产生建议后入队，到期（默认 24h）自动复查相关信号是否已消，
结论回信站内信（"已恢复/仍未处理"）。人只做决策，跟踪也不用人盯。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class RecheckTask(Base):
    __tablename__ = "recheck_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    due_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    kind: Mapped[str] = mapped_column(String(32), default="auto_diag")   # auto_diag/alert/anomaly
    target_key: Mapped[str] = mapped_column(String(128), default="")
    title: Mapped[str] = mapped_column(String(255), default="")
    detail: Mapped[str] = mapped_column(Text, default="")               # 当时的问题/建议摘要
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/done
    result: Mapped[str] = mapped_column(Text, default="")               # 复查结论
    done_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
