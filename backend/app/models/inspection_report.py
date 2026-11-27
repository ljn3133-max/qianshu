from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class InspectionReport(Base):
    """巡检日报：LLM 对一次巡检事实清单的汇总。

    report_date 全表唯一，重跑当日报告按天覆盖。inspection_items 是原始事实，
    本表是面向人的 markdown 日报（站内信正文 / 页面展示 / 机器人推送共用）。
    """

    __tablename__ = "inspection_reports"
    __table_args__ = (UniqueConstraint("report_date", name="uq_inspection_report_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    report_date: Mapped[date] = mapped_column(Date, index=True)
    run_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 基于的巡检 run
    summary: Mapped[str] = mapped_column(String(255), default="")  # 一句话结论
    content_md: Mapped[str] = mapped_column(Text, default="")
    level: Mapped[str] = mapped_column(String(16), default="info")  # 取事实清单最高级别
    provider_label: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    token_usage: Mapped[str] = mapped_column(Text, default="")  # JSON
    status: Mapped[str] = mapped_column(String(16), default="generating")  # generating/ok/error
    error: Mapped[str] = mapped_column(Text, default="")
    pushed_channels: Mapped[str] = mapped_column(Text, default="[]")  # JSON 数组：已推送渠道名
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<InspectionReport {self.report_date} {self.status}>"


class PushSetting(Base):
    """推送开关：巡检日报推送到哪些机器人渠道。

    scene 预留多场景（当前仅 inspection），每场景一行。站内信不受此开关影响（必达）。
    """

    __tablename__ = "push_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    scene: Mapped[str] = mapped_column(String(32), unique=True, default="inspection")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    channel_ids: Mapped[str] = mapped_column(Text, default="[]")  # JSON 数组字符串
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<PushSetting {self.scene} enabled={self.enabled}>"
