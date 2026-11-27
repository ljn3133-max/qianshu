from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class DiagnosisCase(Base):
    """故障案例库：诊断报告的一键沉淀，永久保留、可检索。

    设计要点：
    - 入库即快照（question/steps/conclusion_md 等从 session 复制），
      不引用 session 行内容 -- 诊断历史只保留最近 _KEEP_SESSIONS=50 条，
      已入库的 session 行会被保留用于溯源，但即使被清理案例也自洽可看。
    - 复盘三字段（root_cause/solution/tags）由人工补填，是案例库的核心价值：
      报告里是 AI 推断的根因，这里记录实际怎么解决的。
    - session_id 唯一约束：一个诊断会话只能入库一次（前端按钮据此切换状态）。
    """

    __tablename__ = "diagnosis_cases"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, unique=True)
    question: Mapped[str] = mapped_column(String(500))              # 快照：故障现象
    host: Mapped[str] = mapped_column(String(255), default="")
    level: Mapped[str] = mapped_column(String(16), default="unknown")
    conclusion_md: Mapped[str] = mapped_column(Text, default="")     # 快照：报告全文
    steps: Mapped[str] = mapped_column(Text, default="[]")           # 快照：排查轨迹
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    diagnosed_at: Mapped[datetime] = mapped_column(DateTime)          # 快照：诊断时间
    # ---- 人工复盘（选填） ----
    root_cause: Mapped[str] = mapped_column(String(500), default="")  # 真实根因（一句话）
    solution: Mapped[str] = mapped_column(Text, default="")           # 实际解决措施
    tags: Mapped[str] = mapped_column(String(255), default="")        # 逗号分隔，如 oauth,5xx
    source: Mapped[str] = mapped_column(String(16), default="manual")  # auto=诊断自动沉淀
    status: Mapped[str] = mapped_column(String(16), default="accepted")  # pending=待确认候选 / accepted=已入库经验
    note: Mapped[str] = mapped_column(Text, default="")               # 其他备注
    created_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<DiagnosisCase {self.id} [{self.level}] {self.question[:30]}>"
