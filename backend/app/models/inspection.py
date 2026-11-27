from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class InspectionRun(Base):
    """巡检执行记录。一次流水线 = 一行；同一天可多次（手动重跑）。

    日报取「当日最新一次 ok 的 run」。
    """

    __tablename__ = "inspection_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_date: Mapped[date] = mapped_column(Date, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running / ok / error
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")  # 单个扫描器失败不影响整体，记在此处

    def __repr__(self) -> str:  # pragma: no cover
        return f"<InspectionRun {self.id} {self.run_date} {self.status}>"


class InspectionItem(Base):
    """巡检事实清单。纯规则扫描产出的事实，供 LLM 汇总成日报。

    category: cost(成本异动) / health(资源健康) / slb(SLB摘要) / logs(日志摘要) / idle(闲置识别)
    level: critical / warning / info
    detail: 多行文本事实描述（人可直接读，LLM 可直接消化）
    """

    __tablename__ = "inspection_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(Integer, ForeignKey("inspection_runs.id"), index=True)
    category: Mapped[str] = mapped_column(String(16), index=True)
    level: Mapped[str] = mapped_column(String(16), default="info")
    target_key: Mapped[str] = mapped_column(String(255), default="")   # 实例ID/产品码/资源包ID
    target_name: Mapped[str] = mapped_column(String(255), default="")
    title: Mapped[str] = mapped_column(String(500))   # 2026-11-06 起长度放宽：sqlite 不校验长度，实测最长 330 字（pg 切换后 255 会截断报错）
    detail: Mapped[str] = mapped_column(Text, default="")
    value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    unit: Mapped[str] = mapped_column(String(16), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<InspectionItem {self.category}/{self.level} {self.title}>"
