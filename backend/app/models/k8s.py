"""K8s 容器监控数据模型。

k8s_clusters   集群接入（API 地址+SA Token 加密）
k8s_resources  资源清单快照（pod/svc/ingress/deployment/node，is_active=0 已销毁）
k8s_pod_logs   容器日志（持续采集落库——已销毁 pod 的日志靠销毁前采集覆盖）
k8s_failures   容器失败分析（pending/ignored/confirmed 状态机，批量操作留痕）
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class K8sCluster(Base):
    __tablename__ = "k8s_clusters"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    api_url: Mapped[str] = mapped_column(String(255))
    token_cipher: Mapped[str] = mapped_column(Text, default="")
    verify_tls: Mapped[bool] = mapped_column(Boolean, default=True)
    ns_scope: Mapped[str] = mapped_column(String(512), default="")   # 逗号分隔，空=全部
    # 管理面 SSH：备份/升级/清空/加节点走控制面 SSH，随集群登记
    ssh_host: Mapped[str] = mapped_column(String(64), default="")
    ssh_user: Mapped[str] = mapped_column(String(64), default="root")
    # 安全默认只读（生产集群不开写）：仅显式置 0 的登记测试集群可执行变更/高危任务
    read_only: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(16), default="unknown")  # ok/error/unknown
    status_note: Mapped[str] = mapped_column(String(255), default="")
    synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    rotated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)   # 凭据最近轮换
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class K8sResource(Base):
    __tablename__ = "k8s_resources"

    id: Mapped[int] = mapped_column(primary_key=True)
    cluster_id: Mapped[int] = mapped_column(index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)    # Pod/Service/Ingress/Deployment/Node
    namespace: Mapped[str] = mapped_column(String(128), default="", index=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    labels_json: Mapped[str] = mapped_column(Text, default="")
    status_json: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class K8sPodLog(Base):
    __tablename__ = "k8s_pod_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    cluster_id: Mapped[int] = mapped_column(Integer, index=True)
    namespace: Mapped[str] = mapped_column(String(128), index=True)
    pod: Mapped[str] = mapped_column(String(255), index=True)
    container: Mapped[str] = mapped_column(String(128), default="")
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    stream: Mapped[str] = mapped_column(String(16), default="stdout")
    line: Mapped[str] = mapped_column(Text, default="")


class K8sFailure(Base):
    __tablename__ = "k8s_failures"

    id: Mapped[int] = mapped_column(primary_key=True)
    cluster_id: Mapped[int] = mapped_column(Integer, index=True)
    namespace: Mapped[str] = mapped_column(String(128), index=True)
    pod: Mapped[str] = mapped_column(String(255))
    container: Mapped[str] = mapped_column(String(128), default="")
    failure_type: Mapped[str] = mapped_column(String(32), index=True)  # OOMKilled/CrashLoopBackOff/ImagePullBackOff/Pending
    diagnosis: Mapped[str] = mapped_column(Text, default="")           # 是什么+为什么+怎么办
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending/ignored/confirmed
    note: Mapped[str] = mapped_column(Text, default="")
    # 身份引用：confirmed_by=username 快照，confirmed_by_id=users.id
    confirmed_by: Mapped[str] = mapped_column(String(64), default="")
    confirmed_by_id: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
