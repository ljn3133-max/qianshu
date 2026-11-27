"""平台自愈问题单、修复计划与追加式事件时间线。

三张表只在 create_all 时创建，不改既有表；索引由 migrate_self_heal_indexes 补齐。
IssuePlan 落库后不可修改：内容变化必须生成新版本；plan_hash 是规范化 JSON 的
SHA-256，审批与执行都以哈希绑定，防止“用户看到的计划”和“实际执行的计划”不一致。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, Index, Integer, String, Text

from app.core.db import Base


class IssueCase(Base):
    """运行中的问题单：去重、分级、留证、状态机。"""

    __tablename__ = "issue_cases"

    id = Column(Integer, primary_key=True)
    fingerprint = Column(String(64), default="", index=True)
    source = Column(String(32), default="", index=True)  # onboarding/cloud_sync/job/api/alert/agent/copilotd
    source_ref_json = Column(Text, default="{}")
    account_id = Column(Integer, default=0, index=True)
    run_id = Column(Integer, default=0)
    capability = Column(String(32), default="")
    severity = Column(String(16), default="warning", index=True)  # info/warning/critical
    # open/diagnosing/awaiting_approval/awaiting_user/repairing/verifying/
    # recovered/recurring/manual_required/ignored
    status = Column(String(32), default="open", index=True)
    title = Column(String(255), default="")
    reason_code = Column(String(64), default="")
    error_class = Column(String(128), default="")
    summary_md = Column(Text, default="")
    root_cause_md = Column(Text, default="")
    impact_md = Column(Text, default="")
    evidence_json = Column(Text, default="{}")
    occurrences = Column(Integer, default=1)
    attempts = Column(Integer, default=0)
    max_attempts = Column(Integer, default=3)
    current_plan_id = Column(Integer, nullable=True)
    next_action = Column(String(64), default="")
    next_retry_at = Column(DateTime, nullable=True)
    first_seen = Column(DateTime, default=datetime.now, index=True)
    last_seen = Column(DateTime, default=datetime.now, index=True)
    resolved_at = Column(DateTime, nullable=True)
    resolution_md = Column(Text, default="")
    recurrence_of_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("ix_issue_cases_source_last", "source", "last_seen"),
        Index("ix_issue_cases_status_last", "status", "last_seen"),
    )


class IssuePlan(Base):
    """不可变修复计划；审批/执行按 plan_hash 绑定。"""

    __tablename__ = "issue_plans"

    id = Column(Integer, primary_key=True)
    issue_id = Column(Integer, index=True)
    version = Column(Integer, default=1)
    # proposed/approved/rejected/expired/executing/succeeded/failed/rolled_back
    status = Column(String(24), default="proposed", index=True)
    risk = Column(String(4), default="R0")
    plan_json = Column(Text, default="{}")
    plan_hash = Column(String(64), default="", unique=True, index=True)
    diagnosis_md = Column(Text, default="")
    impact_md = Column(Text, default="")
    assist_json = Column(Text, default="{}")
    expires_at = Column(DateTime, nullable=True)
    created_by = Column(String(32), default="deterministic")  # llm/deterministic/manual
    model = Column(String(128), default="")
    approved_by = Column(String(64), default="")
    approved_by_id = Column(Integer, nullable=True)
    approved_at = Column(DateTime, nullable=True)
    rejected_by = Column(String(64), default="")
    rejected_at = Column(DateTime, nullable=True)
    reject_reason = Column(String(500), default="")
    result_json = Column(Text, default="{}")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("ix_issue_plans_issue_status", "issue_id", "status"),
    )


class IssueEvent(Base):
    """追加式时间线；任何状态变化都必须留事件。"""

    __tablename__ = "issue_events"

    id = Column(Integer, primary_key=True)
    issue_id = Column(Integer, index=True)
    kind = Column(String(32), default="")
    actor = Column(String(64), default="")
    actor_id = Column(Integer, nullable=True)
    payload_json = Column(Text, default="{}")
    created_at = Column(DateTime, default=datetime.now, index=True)
