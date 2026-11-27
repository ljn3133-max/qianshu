"""用户级个人 API Token。

明文只在创建时回显一次，库内只存 SHA-256 哈希（token_hash）；
token_prefix 仅用于列表辨识（如 qs_ab12cd34…），不作鉴权依据。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class UserApiToken(Base):
    __tablename__ = "user_api_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    token_prefix: Mapped[str] = mapped_column(String(16), default="")
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # 空=永不过期
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
