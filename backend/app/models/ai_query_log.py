from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class AiQueryLog(Base):
    """自然语言查询留痕。tools_used/tool_usage 为 JSON 字符串，供页面回放工具轨迹。"""

    __tablename__ = "ai_query_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text, default="")          # markdown
    tools_used: Mapped[str] = mapped_column(Text, default="[]")    # [{tool, params, rows, duration_ms, summary}]
    token_usage: Mapped[str] = mapped_column(Text, default="{}")   # {routing: {...}, answer: {...}}
    status: Mapped[str] = mapped_column(String(16), default="ok")  # ok / error
    error: Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AiQueryLog {self.id} {self.status} {self.question[:30]}>"
