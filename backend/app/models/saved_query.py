from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class SavedQuery(Base):
    """常用查询（团队共享，全局可见）。time_range 存档位 key（如 1h/24h/7d）。

    多源与旧行兼容：source=日志源名（空=旧行语义 SLS），target=CLS 主题/ES 索引
    （SLS 行为空）；project/logstore 仍为 SLS 目标。
    """

    __tablename__ = "saved_queries"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    source: Mapped[str] = mapped_column(String(64), default="")
    target: Mapped[str] = mapped_column(String(256), default="")
    project: Mapped[str] = mapped_column(String(128))
    logstore: Mapped[str] = mapped_column(String(128))
    query: Mapped[str] = mapped_column(Text, default="")
    time_range: Mapped[str] = mapped_column(String(8), default="1h")
    created_by: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)
