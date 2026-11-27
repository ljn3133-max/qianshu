from __future__ import annotations

from typing import Optional
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(String(64), default="")
    role: Mapped[str] = mapped_column(String(16), default="viewer")  # admin/operator/viewer
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # 首次登录强制改密（初始密码必须换掉才能用系统）
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    otp_secret: Mapped[str] = mapped_column(String(64), default="")   # TOTP 密钥（base32）
    otp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # 所属部门（NULL=未分配，权限走角色默认+用户策略）
    department_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=None)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
