"""千枢智能体变更工单/快照/工作流运行。
变更门载体：工单未确认不执行（服务端强制）；快照 before/after 支撑一键回滚。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, Text

from app.core.db import Base


class AgentChangeTicket(Base):
    __tablename__ = "agent_change_tickets"

    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, default=0, index=True)
    msg_id = Column(Integer, default=0)
    title = Column(String(200), default="")
    change_list_json = Column(Text, default="[]")   # [{idx,title,impact,rollback,tool,params,status,result}]
    status = Column(String(16), default="pending", index=True)  # pending/confirmed/executing/done/failed/rolled_back/dismissed
    # 身份引用：confirmed_by=username 快照，confirmed_by_id=users.id
    confirmed_by = Column(String(64), default="")
    confirmed_by_id = Column(Integer, default=0, server_default="0")
    confirmed_at = Column(DateTime, nullable=True)
    result_json = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.now, index=True)


class AgentChangeSnapshot(Base):
    __tablename__ = "agent_change_snapshots"

    id = Column(Integer, primary_key=True)
    ticket_id = Column(Integer, index=True)
    item_idx = Column(Integer, default=0)
    target_type = Column(String(64), default="")   # cloud_account / ownership_rule / alert_rule ...
    target_ref = Column(String(200), default="")   # 对象标识（id/name）
    before_blob = Column(Text, default="")          # JSON：变更前状态（新建=空对象）
    after_blob = Column(Text, default="")           # JSON：变更后状态
    version = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.now)


class AgentWorkflowRun(Base):
    __tablename__ = "agent_workflow_runs"

    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, default=0, index=True)
    workflow_key = Column(String(64), default="")
    steps_json = Column(Text, default="[]")        # [{step,status,detail,ms}]
    status = Column(String(16), default="running")  # running/done/failed/aborted
    artifact_json = Column(Text, default="")        # 产物（报告/配置摘要）
    created_at = Column(DateTime, default=datetime.now, index=True)
    finished_at = Column(DateTime, nullable=True)
