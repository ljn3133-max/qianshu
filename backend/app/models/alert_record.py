from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class AlertRecord(Base):
    """告警记录。同一 (rule_id, target) 同时只有一条 firing。"""

    __tablename__ = "alert_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(default=1, index=True)
    # 归属提醒模块：alert=指标/SLS 规则；host=主机看护；synth=拨测。
    # 总览/站内信/外推按该模块 + account_id 的有效策略过滤。
    module: Mapped[str] = mapped_column(String(32), default="alert",
                                        server_default="alert", nullable=False)
    rule_id: Mapped[int] = mapped_column(Integer)
    rule_name: Mapped[str] = mapped_column(String(128))
    source: Mapped[str] = mapped_column(String(8))          # metric / sls
    target: Mapped[str] = mapped_column(String(192))        # 实例ID 或 project/logstore
    target_name: Mapped[str] = mapped_column(String(192), default="")
    level: Mapped[str] = mapped_column(String(16))          # critical/warning/info
    title: Mapped[str] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="firing")  # firing / recovered
    breach_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fired_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    last_eval_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    recovered_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # 业务影响：告警关联业务影响等级，用于说明受影响用户范围
    impact_level: Mapped[str] = mapped_column(String(16), default="")   # high/medium/low/unknown
    impact_desc: Mapped[str] = mapped_column(Text, default="")
    notified_channels: Mapped[str] = mapped_column(String(512), default="[]")
    silenced: Mapped[bool] = mapped_column(Boolean, default=False)
    silence_reason: Mapped[str] = mapped_column(String(16), default="")  # window=静默时段 / resuppress=抑制期
    # ---- 确认/忽略与重复提醒（与 status firing/recovered 正交） ----
    ack_status: Mapped[str] = mapped_column(String(16), default="none",
                                            server_default="none", nullable=False)
    ack_note: Mapped[str] = mapped_column(Text, default="", server_default="")
    ack_by: Mapped[str] = mapped_column(String(64), default="", server_default="")
    ack_by_id: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    ack_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_notified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    notify_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_notify_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True,
                                                               index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
