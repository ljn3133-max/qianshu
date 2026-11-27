"""工作流例行体检定时：定时跑、产物落库、有异动才推人。

表口径（由 create_all 自动建，不进 migrate.py）：
  agent_workflow_schedules(id, workflow_key, interval_days, hour, enabled,
                           last_run_at, created_by, created_at)
执行由 app/services/agent_schedule.run_due 直调（main 后台循环挂载，测试不启服务）。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String

from app.core.db import Base


class AgentWorkflowSchedule(Base):
    __tablename__ = "agent_workflow_schedules"

    id = Column(Integer, primary_key=True)
    workflow_key = Column(String(64), default="", index=True)
    interval_days = Column(Integer, default=1)      # 例行间隔（天）
    hour = Column(Integer, default=9)               # 执行时刻（小时 0-23，小时级检查到点即跑）
    enabled = Column(Boolean, default=True)
    last_run_at = Column(DateTime, nullable=True)   # 上次执行（幂等锚点：间隔未到不重跑）
    # 身份引用：created_by=username 快照，created_by_id=users.id
    created_by = Column(String(64), default="")
    created_by_id = Column(Integer, default=0, server_default="0")
    created_at = Column(DateTime, default=datetime.now)
