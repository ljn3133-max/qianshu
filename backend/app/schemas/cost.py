"""成本 API 请求与只读洞察响应模型。"""
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class CollectRequest(BaseModel):
    """手动采集成本。默认 T-1 一天；三选一：days / date / start+end。"""

    days: Optional[int] = Field(default=None, ge=1, le=366)
    date: Optional[str] = None          # YYYY-MM-DD
    start: Optional[str] = None         # YYYY-MM-DD（与 end 连用）
    end: Optional[str] = None


class DetectRequest(BaseModel):
    """手动重跑异动检测（默认 T-1）。"""

    date: Optional[str] = None          # YYYY-MM-DD，检测该日按量异动
    packs: bool = Field(default=False, description="同时刷新资源包快照并检测")


class AnomalyAckRequest(BaseModel):
    """异动生命周期操作：确认/忽略/重开。"""

    ids: list[int] = Field(min_length=1, max_length=500)
    status: str = Field(..., pattern="^(acknowledged|ignored|open)$")
    note: Optional[str] = Field(default=None, max_length=500)


class CostInsightObject(BaseModel):
    kind: str = "billing_item"
    id: str = ""
    product_code: str = ""
    product_name: str = ""
    instance_id: str = ""
    instance_name: str = ""
    billing_item: str = ""
    region: str = ""


class CostInsightTimeRange(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(default="", alias="from")
    to: str = ""


class CostInsightDay(BaseModel):
    date: str
    payg: Optional[float] = None
    amortized: Optional[float] = None
    total: Optional[float] = None
    order: Optional[float] = None
    refund: Optional[float] = None
    has_data: bool = False
    complete: bool = False
    bill_complete: bool = False
    amortized_complete: bool = False
    collected_at: str = ""
    evidence_refs: list[str] = Field(default_factory=list)


class CostInsightRank(BaseModel):
    object: CostInsightObject
    payg: Optional[float] = None
    amortized: Optional[float] = None
    total: Optional[float] = None
    order: Optional[float] = None
    refund: Optional[float] = None
    share_pct: Optional[float] = None
    time_range: CostInsightTimeRange
    evidence_refs: list[str] = Field(default_factory=list)


class CostInsightFinding(BaseModel):
    id: str
    account_id: int
    kind: str
    object: CostInsightObject
    value: Optional[float] = None
    usage: Optional[float] = None
    date: str = ""
    time_range: CostInsightTimeRange
    history_range: CostInsightTimeRange = Field(default_factory=CostInsightTimeRange)
    source: str = "cost_daily"
    evidence_refs: list[str] = Field(default_factory=list)
    next_action: str = ""
    baseline: Optional[float] = None
    mad: Optional[float] = None
    ratio: Optional[float] = None
    delta: Optional[float] = None
    level: str = ""
    n_samples: int = 0
    notes: list[str] = Field(default_factory=list)
    history_verified: bool = False
    history_days: int = 0


class CostInsightRecommendation(BaseModel):
    id: str
    action: str
    title: str
    reason: str
    impact_amount: Optional[float] = None
    estimated_savings: Optional[float] = None
    evidence_sufficient: bool = False
    risk: str = "read_only"
    object: CostInsightObject
    time_range: CostInsightTimeRange
    history_range: CostInsightTimeRange = Field(default_factory=CostInsightTimeRange)
    evidence_refs: list[str] = Field(default_factory=list)
    next_action: dict[str, Any] = Field(default_factory=dict)


class CostInsightCoverage(BaseModel):
    requested_days: int = 0
    bill_complete_days: int = 0
    amortized_complete_days: int = 0
    complete_days: int = 0
    observed_start: str = ""
    observed_end: str = ""
    missing_bill_dates: list[str] = Field(default_factory=list)
    missing_amortized_dates: list[str] = Field(default_factory=list)
    complete: bool = False
    total_known: bool = False
    collected_at: str = ""


class CostInsightTotals(BaseModel):
    payg: Optional[float] = None
    amortized: Optional[float] = None
    total: Optional[float] = None
    order: Optional[float] = None
    refund: Optional[float] = None
    complete: bool = False


class CostInsightAccountRef(BaseModel):
    id: int
    name: str
    vendor: str
    enabled: bool
    billing_applicable: bool
    unattributed: bool = False


class CostInsightAnalysis(BaseModel):
    anomaly_status: str
    recommendation_status: str


class CostInsightRange(BaseModel):
    start: str
    end: str
    days: int


class CostInsightAccount(BaseModel):
    account: CostInsightAccountRef
    range: CostInsightRange
    status: str
    reason_code: str
    coverage: CostInsightCoverage
    totals: CostInsightTotals
    days: list[CostInsightDay] = Field(default_factory=list)
    products: list[CostInsightRank] = Field(default_factory=list)
    instances: list[CostInsightRank] = Field(default_factory=list)
    anomalies: list[CostInsightFinding] = Field(default_factory=list)
    observations: list[CostInsightFinding] = Field(default_factory=list)
    recommendations: list[CostInsightRecommendation] = Field(default_factory=list)
    analysis: CostInsightAnalysis


class CostInsightsRequested(BaseModel):
    account_id: int
    start: str
    end: str


class CostInsightsResponse(BaseModel):
    requested: CostInsightsRequested
    accounts: list[CostInsightAccount] = Field(default_factory=list)
    unattributed: list[CostInsightAccount] = Field(default_factory=list)
    selected_account_ids: list[int] = Field(default_factory=list)
