"""云上变更事件，为排障提供"最近改了什么"的证据。

来源：统一 ChangeSource 拉取适配器及 GitLab/Jenkins 等 webhook；ActionTrail
只是其中一种只读实现。诊断可查询事发前后 N 小时谁动了什么，将变更证据
与指标、日志、账单、资产和进程构成结合用于归因。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class ChangeEvent(Base):
    __tablename__ = "change_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(default=1, index=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # source/account/external_id 的哈希写入 event_id；两列保留可查询的来源与原生 ID。
    source_type: Mapped[str] = mapped_column(String(32), default="", index=True)
    external_id: Mapped[str] = mapped_column(String(255), default="")
    event_time: Mapped[datetime] = mapped_column(DateTime, index=True)   # 本地时间
    service: Mapped[str] = mapped_column(String(64), default="")         # ecs/rds/slb...
    event_name: Mapped[str] = mapped_column(String(128), default="")     # RebootInstance...
    event_source: Mapped[str] = mapped_column(String(128), default="")   # ecs.aliyuncs.com / 控制台
    user: Mapped[str] = mapped_column(String(128), default="")           # 操作者
    resource: Mapped[str] = mapped_column(String(255), default="")       # 主要资源
    resource_type: Mapped[str] = mapped_column(String(64), default="")
    error_code: Mapped[str] = mapped_column(String(64), default="")      # 空=成功
    summary: Mapped[str] = mapped_column(Text, default="")               # 人话摘要
    raw: Mapped[str] = mapped_column(Text, default="")                   # 原始 JSON（取证用）
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
