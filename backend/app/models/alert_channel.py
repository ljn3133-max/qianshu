from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class AlertChannel(Base):
    """告警渠道（企微/钉钉机器人/通用 webhook/邮件）。

    webhook_url_cipher 与 smtp_password_cipher 均为 AES-256-GCM 密文，只进不出。
    邮件渠道自带宽 SMTP 配置：不引入全局 SMTP env（密码需入 DB、改配置需重启、
    单收件人组与多渠道模型冲突），邮箱收件人挂在渠道行上。
    """

    __tablename__ = "alert_channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    type: Mapped[str] = mapped_column(String(16))  # wecom_bot / dingtalk_bot / webhook / email
    webhook_url_cipher: Mapped[str] = mapped_column(Text)
    at_mobiles: Mapped[str] = mapped_column(String(512), default="[]")  # JSON 数组字符串，钉钉 @ 用
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 自签证书 verify 开关：默认 True 校验；内网自签 webhook 可关
    verify_ssl: Mapped[bool] = mapped_column(Boolean, default=True)
    # ---- 邮件渠道：仅 type=email 使用；其余类型保持空/默认 ----
    smtp_host: Mapped[str] = mapped_column(String(255), default="")
    smtp_port: Mapped[int] = mapped_column(Integer, default=0)  # 0=按 TLS 模式推导（465/587）
    smtp_user: Mapped[str] = mapped_column(String(255), default="")
    smtp_password_cipher: Mapped[str] = mapped_column(Text, default="")
    smtp_starttls: Mapped[bool] = mapped_column(Boolean, default=False)  # False=SMTP_SSL 465
    email_from: Mapped[str] = mapped_column(String(255), default="")     # 空则用 smtp_user
    email_to: Mapped[str] = mapped_column(Text, default="[]")            # JSON 收件人数组，≤20
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)
