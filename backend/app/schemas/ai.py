"""AI 配置 API 请求/响应模型。"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class AiSettingCreate(BaseModel):
    provider_label: str = Field(min_length=1, max_length=64)
    base_url: str = Field(min_length=8, max_length=255)   # https://host/v1
    api_key: str = Field(min_length=1, max_length=512)
    model: str = Field(min_length=1, max_length=128)
    temperature: float = Field(default=0.2, ge=0, le=2)
    max_tokens: int = Field(default=2048, ge=1, le=393216)
    enabled: bool = False
    remark: str = Field(default="", max_length=255)
    verify_ssl: bool = True   # 自签证书可关校验，默认开


class AiSettingUpdate(BaseModel):
    provider_label: Optional[str] = Field(default=None, min_length=1, max_length=64)
    base_url: Optional[str] = Field(default=None, min_length=8, max_length=255)
    api_key: Optional[str] = Field(default=None, min_length=1, max_length=512)  # 不传则不改
    model: Optional[str] = Field(default=None, min_length=1, max_length=128)
    temperature: Optional[float] = Field(default=None, ge=0, le=2)
    max_tokens: Optional[int] = Field(default=None, ge=1, le=393216)
    enabled: Optional[bool] = None
    remark: Optional[str] = Field(default=None, max_length=255)
    verify_ssl: Optional[bool] = None   # 不传则不改


class AiSettingOut(BaseModel):
    id: int
    provider_label: str
    base_url: str
    model: str
    temperature: float
    max_tokens: int
    enabled: bool
    remark: str
    api_key_masked: str
    verify_ssl: bool = True   # 默认校验证书
    created_at: str
    updated_at: str


class AiTestOut(BaseModel):
    ok: bool
    reply: str = ""
    model: str = ""
    duration_ms: int = 0
    usage: dict = {}
    error: str = ""


class AiTestIn(BaseModel):
    """弹窗「测试连通」用：按表单当前值测试，无需先保存。

    api_key 留空且 editing_id 有值时，回退该配置已保存的 Key。
    """

    base_url: str = Field(min_length=8, max_length=255)
    model: str = Field(min_length=1, max_length=128)
    api_key: str = Field(default="", max_length=512)
    temperature: float = Field(default=0.2, ge=0, le=2)
    max_tokens: int = Field(default=2048, ge=1, le=393216)
    verify_ssl: bool = True
    editing_id: Optional[int] = None


class AskRequest(BaseModel):
    """自然语言查询。"""

    question: str = Field(min_length=2, max_length=2000)


class AiScenarioOut(BaseModel):
    """场景×模型矩阵一行。

    setting_id=0 / setting_label="" 表示未绑定——运行时兜底默认连接（resolve_model）。
    """

    scenario: str
    label: str
    setting_id: int = 0
    enabled: bool = True
    setting_label: str = ""
    model: str = ""


class AiScenarioUpdate(BaseModel):
    """保存场景绑定（PUT /ai/scenarios/{scenario}）：两字段都可选，传什么改什么。"""

    setting_id: Optional[int] = None   # 0=解除绑定（回兜底默认连接）
    enabled: Optional[bool] = None
