"""告警水位记忆：记录已确认的正常业务水位。

固定阈值会对常态高位机器持续报警，因此某台机器某指标被"设为常态"后：
- 触发线自动上调到 水位×1.05（不再贴线报）
- 但显著偏离常态仍报：超过新触发线（真异常）或跌破 水位×0.6（该忙反闲=业务停了/换进程了）
- 与行为画像同哲学：人确认一次，系统记住，之后只报偏离
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class AlertTargetBaseline(Base):
    __tablename__ = "alert_target_baselines"

    id: Mapped[int] = mapped_column(primary_key=True)
    rule_id: Mapped[int] = mapped_column(Integer, index=True)
    target: Mapped[str] = mapped_column(String(128), index=True)
    rule_name: Mapped[str] = mapped_column(String(128), default="")
    target_name: Mapped[str] = mapped_column(String(255), default="")
    baseline_value: Mapped[float] = mapped_column(Float, default=0.0)   # 确认时的水位
    threshold_override: Mapped[float] = mapped_column(Float, default=0.0)  # 新触发线
    # 身份引用：confirmed_by=username 快照，confirmed_by_id=users.id
    confirmed_by: Mapped[str] = mapped_column(String(64), default="")
    confirmed_by_id: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    confirmed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
