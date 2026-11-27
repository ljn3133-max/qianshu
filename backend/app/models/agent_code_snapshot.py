"""平台自改管道的代码快照表。

与配置级 agent_change_snapshots 同哲学：内容寻址存档、不可覆盖按次留存。
files_json 每项存 {path, before, after, diff_summary, before_hash, after_hash, before_existed}——
before/after 为全文（代码不是凭证，可入快照；对外展示走 snapshot_out 截断口径）；
after_hash=应用结果指纹，回滚前比对现文件 hash 防覆盖第三方改动（旧快照无此键按 after 现算）。
status: applied（已应用，可回滚）/ rolled_back（已回滚）。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, Text

from app.core.db import Base


class AgentCodeSnapshot(Base):
    __tablename__ = "agent_code_snapshots"

    id = Column(Integer, primary_key=True)
    ticket_id = Column(Integer, default=0, index=True)
    files_json = Column(Text, default="[]")    # [{path,before,after,diff_summary,before_hash,after_hash,before_existed}]
    verify_json = Column(Text, default="")     # 最近一次 verify_gate 结果 {passed,results,phase}
    status = Column(String(16), default="applied", index=True)  # applied/rolled_back
    created_at = Column(DateTime, default=datetime.now, index=True)
