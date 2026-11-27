"""自定义工作流定义：可视化编排的持久化载体。

一张表存一条自定义工作流：steps_json 为有序步骤声明
[{tool,label,params_template,continue_on_fail}]——**只含白名单工具调用声明，
不含任何可执行代码**（定义永远不携带代码）。version 每次保存 +1（单行存最新）。

workflow_def_versions 历史表支持版本历史与一键回滚。
每个版本状态一条记录（key/version/def_json/created_by/created_at），**只增不改**——
行一旦写入不再修改/删除（定义删除也不清历史，同 key 重建续用 max(version)+1，
删除前的版本仍可恢复）；def_json = 该版本的导出载荷
{key,name,description,domain,steps,enabled}，restore 直接按它回填。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text

from app.core.db import Base


class WorkflowDef(Base):
    __tablename__ = "workflow_defs"

    id = Column(Integer, primary_key=True)
    key = Column(String(64), unique=True, index=True, nullable=False)
    name = Column(String(100), default="")
    description = Column(String(500), default="")
    domain = Column(String(50), default="")
    steps_json = Column(Text, default="[]")   # [{tool,label,params_template,continue_on_fail}]
    # 身份引用：created_by=username 快照，created_by_id=users.id
    created_by = Column(String(64), default="")
    created_by_id = Column(Integer, default=0, server_default="0")
    enabled = Column(Boolean, default=True)
    version = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.now, index=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class WorkflowDefVersion(Base):
    """版本历史（只增）：(key, version) 唯一语义由写入方保证（_log_version 查重）。"""

    __tablename__ = "workflow_def_versions"

    id = Column(Integer, primary_key=True)
    key = Column(String(64), index=True, nullable=False)
    version = Column(Integer, nullable=False)
    def_json = Column(Text, default="{}")     # {key,name,description,domain,steps,enabled}
    # 身份引用：created_by=username 快照，created_by_id=users.id
    created_by = Column(String(64), default="")
    created_by_id = Column(Integer, default=0, server_default="0")
    created_at = Column(DateTime, default=datetime.now, index=True)
