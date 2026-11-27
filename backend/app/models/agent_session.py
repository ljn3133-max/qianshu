"""千枢智能体（copilot）会话与消息。
会话可恢复/可搜索；工具轨迹存 JSON，供工具卡回溯结论依据。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text

from app.core.db import Base


class AgentSession(Base):
    __tablename__ = "agent_sessions"

    id = Column(Integer, primary_key=True)
    title = Column(String(200), default="新会话")
    user_id = Column(Integer, default=0)
    status = Column(String(16), default="active")       # active / archived
    pinned = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)
    last_active_at = Column(DateTime, default=datetime.now)


class AgentMessage(Base):
    __tablename__ = "agent_messages"

    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("agent_sessions.id"), index=True)
    role = Column(String(16))                            # user / assistant
    content_md = Column(Text, default="")
    tool_calls_json = Column(Text, default="[]")         # [{name,params_summary,result_summary,ms}]
    tool_results_json = Column(Text, default="[]")       # 完整工具结果（面板展开用，不截断）
    review_json = Column(Text, default="")               # 两轮交付制审查 verdict
    snapshot_json = Column(Text, default="")             # 首条消息附系统快照
    model = Column(String(80), default="")               # 实际用的模型（可回溯）
    tokens = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.now, index=True)
