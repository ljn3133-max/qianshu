"""云账号首次接入运行：向导、智能体与后台同步共享的持久任务状态。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class OnboardingRun(Base):
    __tablename__ = "onboarding_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 一个账号只有一条当前接入任务；重新接入复用 Run 并递增 attempts，不并发另建任务。
    account_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    # running/waiting/ready/attention/failed；attention=已有可用结果但存在缺口。
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    stage: Mapped[str] = mapped_column(String(32), default="credential")
    # 凭证、各云产品与规则播种的结构化证据；绝不写 AK/SK 或原始日志。
    items_json: Mapped[str] = mapped_column(Text, default="[]")
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
