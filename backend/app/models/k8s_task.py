"""K8s 管理任务模型：任务中心底座——所有运维动作的任务化。

任务=长时操作的统一形态：状态机（pending/running/succeeded/failed/aborted）、
日志流（append-only，2s 轮询）、终止/重试、元数据（类型/版本/CIDR/提交人…）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class K8sTask(Base):
    __tablename__ = "k8s_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_type: Mapped[str] = mapped_column(String(32), index=True)   # add_node/backup/upgrade/wipe/create_cluster
    title: Mapped[str] = mapped_column(String(255), default="")
    # 集群引用：cluster_id=k8s_clusters.id（0=未关联），cluster_name=展示快照
    cluster_name: Mapped[str] = mapped_column(String(64), default="")
    cluster_id: Mapped[int] = mapped_column(Integer, default=0, index=True, server_default="0")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    meta_json: Mapped[str] = mapped_column(Text, default="")   # 任务类型/拓扑模式/版本/运行时/网络/CIDR/主机详情
    steps_json: Mapped[str] = mapped_column(Text, default="")  # 流程步骤状态（升级详情页复用）
    error: Mapped[str] = mapped_column(Text, default="")
    # 身份引用：submitted_by=username 快照，submitted_by_id=users.id
    submitted_by: Mapped[str] = mapped_column(String(64), default="")
    submitted_by_id: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    retries: Mapped[int] = mapped_column(Integer, default=0)


class K8sTaskLog(Base):
    __tablename__ = "k8s_task_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(Integer, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    line: Mapped[str] = mapped_column(Text, default="")
