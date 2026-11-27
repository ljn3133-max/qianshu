"""运行时可写配置键值表。

compose 形态容器重建会丢失容器内 .env，安装向导、系统参数、保留策略和游标等
运行时配置需要持久化。本表是 env_store 的第二落点：set_keys 双写（.env + 本表），
读取库优先（services/env_store），容器重建后启动时从库回填 .env。

只存运行时可写配置（env_store.set_keys 的键）；密钥类（SECRET_KEY/MASTER_KEY/
DATABASE_URL/口令/token）由 env_store 拒写名单挡在库外，密钥作用域不变（仍只在 .env）。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class EnvKv(Base):
    __tablename__ = "env_kv"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<EnvKv {self.key}>"
