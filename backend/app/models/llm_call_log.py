from __future__ import annotations

from datetime import datetime

from sqlalchemy import Date, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class LlmCallLog(Base):
    """LLM 调用留痕。所有经过 services/llm.py 的调用逐条落库：token 用量可审计。

    purpose 取值：test(连通性测试) / nl_query(自然语言查询) /
    inspection(巡检) / diagnosis(诊断)。
    """

    __tablename__ = "llm_call_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    setting_id: Mapped[int] = mapped_column(Integer, index=True)
    provider_label: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(128))
    purpose: Mapped[str] = mapped_column(String(32), default="test")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="ok")  # ok / error
    error: Mapped[str] = mapped_column(Text, default="")           # 失败原因（截断）
    call_date: Mapped[datetime.date] = mapped_column(Date, index=True)  # 按日统计用
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<LlmCallLog {self.model} {self.purpose} {self.total_tokens}t {self.status}>"
