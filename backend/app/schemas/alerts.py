"""告警渠道 API 的请求模型。"""
from typing import List, Optional

from pydantic import BaseModel, Field

CHANNEL_TYPES = ("wecom_bot", "dingtalk_bot", "webhook")
_CHANNEL_TYPE_PATTERN = "^(wecom_bot|dingtalk_bot|webhook)$"


class ChannelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    type: str = Field(pattern=_CHANNEL_TYPE_PATTERN)
    webhook_url: str = Field(default="", max_length=1024)
    at_mobiles: List[str] = Field(default_factory=list, max_length=20)
    enabled: bool = True
    verify_ssl: bool = True   # 自签证书可关校验，默认开


class ChannelUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    type: Optional[str] = Field(default=None, pattern=_CHANNEL_TYPE_PATTERN)
    webhook_url: Optional[str] = Field(default=None, max_length=1024)
    at_mobiles: Optional[List[str]] = Field(default=None, max_length=20)
    enabled: Optional[bool] = None
    verify_ssl: Optional[bool] = None   # 不传则不改


class RuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    source: str = Field(pattern="^(metric|sls)$")
    level: str = Field(default="warning", pattern="^(critical|warning|info)$")
    product: Optional[str] = None       # metric 必填
    metric: Optional[str] = None        # metric 必填
    project: Optional[str] = None       # sls 必填
    logstore: Optional[str] = None      # sls 必填
    query_expr: Optional[str] = Field(default=None, max_length=4000)
    comparator: str = Field(default=">", pattern="^(>|>=|<|<=)$")
    threshold: float
    duration: int = Field(default=300, ge=60, le=86400)
    channel_ids: List[int] = Field(default_factory=list, max_length=10)
    silences: List[str] = Field(default_factory=list, max_length=8)
    account_id: int = 0   # 0=全部账号；>0=绑定账号（cloud_accounts.id）
    enabled: bool = True


class RuleUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    level: Optional[str] = Field(default=None, pattern="^(critical|warning|info)$")
    product: Optional[str] = None
    metric: Optional[str] = None
    project: Optional[str] = None
    logstore: Optional[str] = None
    query_expr: Optional[str] = Field(default=None, max_length=4000)
    comparator: Optional[str] = Field(default=None, pattern="^(>|>=|<|<=)$")
    threshold: Optional[float] = None
    duration: Optional[int] = Field(default=None, ge=60, le=86400)
    channel_ids: Optional[List[int]] = Field(default=None, max_length=10)
    silences: Optional[List[str]] = Field(default=None, max_length=8)
    account_id: Optional[int] = None   # 不传则不改
    enabled: Optional[bool] = None


class RuleDraft(BaseModel):
    """试算用规则草稿：保存前的历史回放输入。校验口径与 RuleCreate
    一致（metric 必填 product/metric，sls 必填 project/logstore），仅 name 可空。"""
    name: Optional[str] = Field(default=None, max_length=128)
    source: str = Field(pattern="^(metric|sls)$")
    level: str = Field(default="warning", pattern="^(critical|warning|info)$")
    product: Optional[str] = None
    metric: Optional[str] = None
    project: Optional[str] = None
    logstore: Optional[str] = None
    query_expr: Optional[str] = Field(default=None, max_length=4000)
    comparator: str = Field(default=">", pattern="^(>|>=|<|<=)$")
    threshold: float
    duration: int = Field(default=300, ge=60, le=86400)
    silences: List[str] = Field(default_factory=list, max_length=8)
    account_id: int = 0   # 试算同口径按账号过滤


class RuleDryRunIn(BaseModel):
    """POST /api/alerts/rules/试算 请求体。"""
    rule_draft: RuleDraft
    days: int = Field(default=7, ge=1, le=30)


class RuleAutoCreateIn(BaseModel):
    """POST /api/alerts/rules/auto-create 请求体。

    只传候选 key，不传阈值——服务端重新分析后再建，避免前端篡改阈值或
    创建过期候选（分析后规则可能已被别人补上）。
    """
    keys: List[str] = Field(min_length=1, max_length=100)
