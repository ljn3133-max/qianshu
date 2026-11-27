"""机器库：部署/加节点的目标机资产台账。

托管 SSH 加密凭证、角色标签、所属集群、水位采集（CPU/内存/磁盘）与连通检测；
管理任务从机器库选机，无需在表单重复填写 SSH 信息。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class K8sMachine(Base):
    __tablename__ = "k8s_machines"

    id: Mapped[int] = mapped_column(primary_key=True)
    host_name: Mapped[str] = mapped_column(String(64), default="")
    ip: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    ssh_port: Mapped[int] = mapped_column(Integer, default=22)
    ssh_user: Mapped[str] = mapped_column(String(64), default="root")
    ssh_password_cipher: Mapped[str] = mapped_column(Text, default="")   # 加密托管，空=密钥免密
    role: Mapped[str] = mapped_column(String(32), default="未分配")      # master/etcd/node/loadbalancer/未分配
    cluster_id: Mapped[int] = mapped_column(Integer, default=0, index=True)   # 0=未分配
    cluster_name: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="unknown")  # ok/error/unknown
    status_note: Mapped[str] = mapped_column(String(255), default="")
    cpu_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mem_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    disk_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    last_seen: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
