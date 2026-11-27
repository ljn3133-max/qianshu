"""组织架构与授权模型：部门树 + Namespace 授权。

departments          部门树（parent_id + 物化路径 path + 排序）
dept_permissions     部门级权限策略（部门 × 权限码 allow/deny，沿部门链继承、deny 优先）
namespace_grants     K8s Namespace 授权（dept/user × cluster × namespace × role[view/use/admin]）

与既有用户级 permission_policies 的叠加语义见 core/permissions.py
（用户 deny > 用户 allow > 部门 deny > 部门 allow > 角色默认）。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Department(Base):
    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    parent_id: Mapped[int] = mapped_column(Integer, default=0, index=True)   # 0=顶级
    # 物化路径 "/1/3/7/"（含自身 id，根为 "/1/"）：子树=LIKE '/1/%'，祖先链=拆路径
    path: Mapped[str] = mapped_column(String(512), default="", index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)   # 同级排序（拖动）
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class DeptPermission(Base):
    __tablename__ = "dept_permissions"

    id: Mapped[int] = mapped_column(primary_key=True)
    department_id: Mapped[int] = mapped_column(Integer, index=True)
    code: Mapped[str] = mapped_column(String(64))
    effect: Mapped[str] = mapped_column(String(8))   # allow / deny
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class NamespaceGrant(Base):
    __tablename__ = "namespace_grants"

    id: Mapped[int] = mapped_column(primary_key=True)
    subject_type: Mapped[str] = mapped_column(String(8), index=True)   # user / dept
    subject_id: Mapped[int] = mapped_column(Integer, index=True)       # users.id / departments.id
    cluster_id: Mapped[int] = mapped_column(Integer, index=True)
    # namespace='*' = 集群内全部命名空间（含 Node 等集群级资源）
    namespace: Mapped[str] = mapped_column(String(128), default="")
    role: Mapped[str] = mapped_column(String(8), default="view")       # view / use / admin
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
