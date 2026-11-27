"""值班闭环：值班表 + 告警认领 + 无人响应升级链。

产品语义："替人值班"必须有最后一公里——critical 告警 N 分钟无人认领 →
自动升级到下一级联系人（短信/电话渠道），直到有人接。认领（ack）≠恢复：
ack=有人在管了，recovered=问题没了。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class OncallContact(Base):
    __tablename__ = "oncall_contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(64), default="")       # 主备/职能描述
    channel: Mapped[str] = mapped_column(String(32), default="wecom")  # wecom/sms/phone/email
    contact: Mapped[str] = mapped_column(String(128), default="")  # 手机号/邮箱/webhook
    level: Mapped[int] = mapped_column(Integer, default=1)          # 升级顺序：1 最先接
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class AlertAck(Base):
    __tablename__ = "alert_acks"

    id: Mapped[int] = mapped_column(primary_key=True)
    alert_id: Mapped[int] = mapped_column(Integer, index=True)
    # 身份引用：ack_by=username 展示快照，ack_by_id=users.id
    ack_by: Mapped[str] = mapped_column(String(64), default="")
    ack_by_id: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    ack_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    note: Mapped[str] = mapped_column(Text, default="")


class AlertEscalation(Base):
    """升级留痕：谁被升级、第几级、是否已有人认领。"""
    __tablename__ = "alert_escalations"

    id: Mapped[int] = mapped_column(primary_key=True)
    alert_id: Mapped[int] = mapped_column(Integer, index=True)
    level: Mapped[int] = mapped_column(Integer, default=1)
    contact_name: Mapped[str] = mapped_column(String(64), default="")
    channel: Mapped[str] = mapped_column(String(32), default="")
    notified_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    acked: Mapped[bool] = mapped_column(Boolean, default=False)
    detail: Mapped[str] = mapped_column(Text, default="")
