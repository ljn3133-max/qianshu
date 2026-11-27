from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class AlertRule(Base):
    """告警规则。source=metric 走 VictoriaMetrics 指标阈值；source=sls 走 SLS 聚合值阈值。

    metric 规则用 product+metric（自动拼指标名，对产品下全部实例逐个评估）；
    sls 规则用 project+logstore+query（检索取命中数；含 | select 取首行首个数值列）。
    comparator: > / >= / < / <=；duration 为持续/统计窗口（秒）。
    channel_ids / silences 为 JSON 数组字符串；silences 元素形如 "23:00-07:00"（每日静默时段）。
    """

    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    source: Mapped[str] = mapped_column(String(8))  # metric / sls
    level: Mapped[str] = mapped_column(String(16), default="warning")  # critical/warning/info
    # metric 专用
    product: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    metric: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # sls 专用
    project: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    logstore: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    query_expr: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 公共
    comparator: Mapped[str] = mapped_column(String(2), default=">")
    threshold: Mapped[float] = mapped_column(Float)
    duration: Mapped[int] = mapped_column(Integer, default=300)  # 持续窗口(秒)
    channel_ids: Mapped[str] = mapped_column(String(512), default="[]")
    silences: Mapped[str] = mapped_column(Text, default="[]")
    # 账号维度：0=全部账号（存量默认，行为不变）；
    # >0=绑定账号（cloud_accounts.id），评估只覆盖该账号资产、记录按目标账号打标
    account_id: Mapped[int] = mapped_column(Integer, default=0, index=True, server_default="0")
    # 规则来源：manual=人工创建；auto=接入云账号后自动播种的常见规则。
    # auto 规则与人工规则完全同权（可改阈值/可禁用/可删除），只用于前端标识；
    # 删除后不复活——账号级 alert_seeded_at 一次性标记，见 services/cloud/sync_job.py
    origin: Mapped[str] = mapped_column(String(8), default="manual", server_default="manual")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)
