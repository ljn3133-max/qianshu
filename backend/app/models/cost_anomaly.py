from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class CostAnomaly(Base):
    """成本/资源包异动检测结果。

    cost_anomalies 混装多域异动，type 全集与类型域（cost/monitor/cdn/profile）
    见 services.cost_anomaly.ANOMALY_TYPE_DOMAINS——各域页面按域过滤取数。

    type 取值（成本类，钱/资源包；其余域：traffic_spike/growth_accel/host_down/
    aux_spillover=监控类，profile_drift/chain_drift=画像类）：
    - payg_spike   按量费用突增（产品×实例×计费项 或 产品 或 全局）
    - payg_new     新增按量费用项（无历史基线且金额达阈值）
    - pack_spike   资源包消耗速率突增
    - pack_exhausting 抵扣能力池按当前速率即将耗尽（2026-09-22 起池级，target_key=池键）
    - pack_exhausted  资源包刚耗尽（余量>0 -> 0 转变，此后转按量）
    - pack_expiry  资源包即将到期但仍有较多余量（浪费风险）

    生命周期（ack_status）：
    - open         检测中且未处理（默认，出现在巡检与通知里）
    - acknowledged 用户已确认知晓（不再进巡检/通知；事件仍在持续时静默刷新）
    - ignored      用户判定无需处理（同上；级别升级到 critical 时自动重开）
    - resolved     持续型异动连续 3 日未复现自动关闭，或被池级记录替代的历史记录
    detect_date=首次发现日，last_seen=最近一次仍复现日（同一事件不重复入库）。
    """

    __tablename__ = "cost_anomalies"
    __table_args__ = (
        UniqueConstraint("detect_date", "type", "target_key", name="uq_cost_anomaly_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # 数据源归因：0=全局/无法归因；用于按账号优先级/去向过滤提醒
    account_id: Mapped[int] = mapped_column(Integer, default=0, server_default="0",
                                            index=True)
    detect_date: Mapped[date] = mapped_column(Date, index=True)
    type: Mapped[str] = mapped_column(String(32))
    level: Mapped[str] = mapped_column(String(16), default="warning")  # critical / warning / info
    target_key: Mapped[str] = mapped_column(String(255))   # 产品码 / 实例ID / 池键 / __total__
    target_name: Mapped[str] = mapped_column(String(255), default="")
    title: Mapped[str] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text, default="")
    value: Mapped[float] = mapped_column(Float, default=0)      # 异动日实际值
    baseline: Mapped[float] = mapped_column(Float, default=0)   # 基线值
    ratio: Mapped[float] = mapped_column(Float, default=0)      # 倍数
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    # ---- 生命周期（2026-09-22，旧库由 app.core.migrate 补列） ----
    last_seen: Mapped[date] = mapped_column(Date, default=None, server_default=None,
                                            nullable=True, index=True)
    ack_status: Mapped[str] = mapped_column(String(16), default="open", server_default="open",
                                            nullable=False)
    ack_note: Mapped[str] = mapped_column(Text, default="", server_default="")
    ack_by: Mapped[str] = mapped_column(String(64), default="", server_default="")
    # 身份引用：ack_by=username 快照，ack_by_id=users.id
    ack_by_id: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    ack_at: Mapped[datetime] = mapped_column(DateTime, default=None, server_default=None,
                                             nullable=True)
    # ---- 重复提醒：确认/忽略后到 next_notify_at 再重申 ----
    last_notified_at: Mapped[datetime] = mapped_column(DateTime, default=None,
                                                       nullable=True)
    notify_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_notify_at: Mapped[datetime] = mapped_column(DateTime, default=None,
                                                     nullable=True, index=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<CostAnomaly {self.detect_date} {self.type} {self.target_key} x{self.ratio}>"
