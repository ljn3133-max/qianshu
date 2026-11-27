from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class CloudCredential(Base):
    """阿里云凭证。secret_cipher 为 AES-256-GCM 密文，master key 只在环境变量。"""

    __tablename__ = "cloud_credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    vendor: Mapped[str] = mapped_column(String(16), default="aliyun")
    access_key_id: Mapped[str] = mapped_column(String(64))  # AK ID 本身不视为机密，明文存
    secret_cipher: Mapped[str] = mapped_column(Text)
    remark: Mapped[str] = mapped_column(String(255), default="")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)
