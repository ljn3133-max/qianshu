"""拓扑扫描记录：每次扫描留痕 + 与上次的组件变化（diff）。

记录扫描时间和组件变化，用于追踪资产拓扑演变。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class TopologyScan(Base):
    __tablename__ = "topology_scans"

    id: Mapped[int] = mapped_column(primary_key=True)
    scanned_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    trigger: Mapped[str] = mapped_column(String(16), default="manual")   # manual / timer / startup
    groups: Mapped[int] = mapped_column(Integer, default=0)
    links: Mapped[int] = mapped_column(Integer, default=0)
    members: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    diff_json: Mapped[Optional[str]] = mapped_column(Text, default="")   # {added:[], removed:[], grown:[], shrunk:[]}
    synced_json: Mapped[Optional[str]] = mapped_column(Text, default="")  # 各资产面同步结果摘要
    # 当次完整 build_arch 结果（含 groups/members/links），诊断按 scanned_at 读取历史事实；
    # 老记录为空时明确无快照，绝不能用当前实时架构冒充历史。
    arch_json: Mapped[Optional[str]] = mapped_column(Text, default="")
