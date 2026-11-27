"""Onboarding 能力证据与按范围重试契约。"""
from __future__ import annotations

from datetime import datetime
import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

CapabilityName = Literal["assets", "metrics", "logs", "cost", "changes"]
ExecutionState = Literal["waiting", "running", "succeeded", "failed", "skipped"]
DataState = Literal["unknown", "available", "no_data", "stale"]


class ScopeSelector(BaseModel):
    region: str = Field(default="", max_length=64)
    source_id: str = Field(default="", max_length=128)
    resource_ids: list[str] = Field(default_factory=list, max_length=100)
    bill_date: str = Field(default="", max_length=10)

    @field_validator("resource_ids")
    @classmethod
    def resource_identity_is_bounded(cls, value):
        if any(not str(item).strip() or len(str(item)) > 256 for item in value):
            raise ValueError("资源身份不能为空或超过 256 字符")
        return sorted(set(str(item).strip() for item in value))

    @field_validator("bill_date")
    @classmethod
    def bill_date_is_valid(cls, value):
        if value and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("bill_date 必须为 YYYY-MM-DD")
        if value:
            datetime.strptime(value, "%Y-%m-%d")
        return value


class RetryScope(ScopeSelector):
    capability: CapabilityName


class RetryRequest(BaseModel):
    scopes: list[RetryScope] = Field(min_length=1, max_length=20)

    @field_validator("scopes")
    @classmethod
    def scopes_are_unique(cls, value):
        identities = [(item.capability, item.region, item.source_id,
                       tuple(item.resource_ids), item.bill_date) for item in value]
        if len(identities) != len(set(identities)):
            raise ValueError("同一能力与范围不能重复提交")
        return value


class CapabilityScope(ScopeSelector):
    account_id: int = Field(ge=1)
    vendor: str = Field(default="", max_length=16)


class CapabilityCoverage(BaseModel):
    discovered: Optional[int] = Field(default=None, ge=0)
    checked: Optional[int] = Field(default=None, ge=0)
    failed: Optional[int] = Field(default=None, ge=0)
    unknown: Optional[int] = Field(default=None, ge=0)
    total_known: bool = False
    truncated: bool = False


class CapabilityResult(BaseModel):
    capability: CapabilityName
    scope: CapabilityScope
    execution: ExecutionState = "waiting"
    data_state: DataState = "unknown"
    coverage: CapabilityCoverage = Field(default_factory=CapabilityCoverage)
    checked_at: Optional[datetime] = None
    observed_until: Optional[datetime] = None
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)
    # 采集/回读失败的原始摘要（最多保留前 20 条）；保证“未就绪”在 UI 上可定位，
    # 而不是只有一句“回读未通过”，用户还得去翻容器日志。
    errors: list[str] = Field(default_factory=list, max_length=20)
    reason_code: str = Field(default="not_verified", max_length=128)
    retryable: bool = True
    next_action: str = Field(default="continue_onboarding", max_length=128)
    # 同一范围每次重试递增；执行器回写时必须匹配，阻断晚到旧响应。
    revision: int = Field(default=0, ge=0)
