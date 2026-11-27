from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class AiSetting(Base):
    """LLM 接入配置。OpenAI 兼容协议：base_url 需含版本前缀（如 https://api.deepseek.com/v1）。

    api_key_cipher 为 AES-256-GCM 密文；enabled 全表仅一个为 True（启用即切换），
    NL 查询、巡检报告等消费方统一取当前启用的配置。
    """

    __tablename__ = "ai_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_label: Mapped[str] = mapped_column(String(64))       # 显示名：DeepSeek / 通义 / 自建网关…
    base_url: Mapped[str] = mapped_column(String(255))            # https://host/v1
    api_key_cipher: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(128))               # deepseek-chat / qwen-max…
    temperature: Mapped[float] = mapped_column(Float, default=0.2)
    max_tokens: Mapped[int] = mapped_column(Integer, default=2048)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    remark: Mapped[str] = mapped_column(String(255), default="")
    # 自签证书 verify 开关：默认 True 校验；自建网关自签证书可关
    verify_ssl: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AiSetting {self.provider_label} {self.model} enabled={self.enabled}>"
