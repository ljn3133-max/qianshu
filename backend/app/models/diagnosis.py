from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class DiagnosisSession(Base):
    """故障诊断会话：用户提一个现象（如 "xxx.com 打不开"），
    诊断 Agent 沿 链路->SLB->后端->应用->下游 逐层取证，LLM 归纳根因。

    安全红线：诊断工具全部只读；LLM 只产出工具选择参数与结论文本，
    绝不执行 LLM 生成的 SQL/PromQL 原文；建议动作由人工执行。

    steps: JSON 数组 [{thought, tool, params, summary, duration_ms, error}]
    level: 结论级别 critical/warning/ok/unknown
    """

    __tablename__ = "diagnosis_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    question: Mapped[str] = mapped_column(String(500))
    host: Mapped[str] = mapped_column(String(255), default="")   # 解析出的目标域名（可空）
    scenario: Mapped[str] = mapped_column(String(32), default="link_fault")
    trigger: Mapped[str] = mapped_column(String(16), default="manual")  # auto=事件自动触发 / manual=人工  # 意图分诊场景
    status: Mapped[str] = mapped_column(String(16), default="running")  # running/ok/error
    level: Mapped[str] = mapped_column(String(16), default="unknown")
    conclusion_md: Mapped[str] = mapped_column(Text, default="")
    steps: Mapped[str] = mapped_column(Text, default="[]")
    token_usage: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str] = mapped_column(Text, default="")
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<DiagnosisSession {self.id} [{self.status}] {self.question[:30]}>"
