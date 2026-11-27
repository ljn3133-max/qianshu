from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class CostDaily(Base):
    """每日成本（T+1 拉取，按 产品×实例×计费项 聚合）。

    两个来源（source 列区分，重拉同日同源整体覆盖，幂等）：
    - bill：QueryInstanceBill DAILY，即控制台"账单明细"口径（按量消费 + 当日订单/退款事件）；
    - amortized：DescribeInstanceAmortizedCostByAmortizationPeriodDate，包年包月按日摊销
      （需用户在费用中心开通"摊销成本"后才有数据，未开通时采集端软跳过）。

    金额口径：pretax_amount=账单金额（判断"花了多少钱"的唯一口径）、
    pretax_gross_amount=原价、payment_amount=按量挂账现金支付字段（常 0，次月账单统一扣款，
    不作花费依据，也不得据此推断资源包抵扣）。
    真实每日成本 = bill(PayAsYouGo) + amortized（bill 里的 Subscription 是一次性订单事件，不重复计入）。
    """

    __tablename__ = "cost_daily"
    __table_args__ = (
        UniqueConstraint("stat_date", "product_code", "instance_id", "billing_item",
                          "subscription_type", "source", name="uq_cost_daily_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(default=1, index=True)
    stat_date: Mapped[date] = mapped_column(Date, index=True)
    source: Mapped[str] = mapped_column(String(16), default="bill")      # bill / amortized
    product_code: Mapped[str] = mapped_column(String(64), default="")
    product_name: Mapped[str] = mapped_column(String(128), default="")
    instance_id: Mapped[str] = mapped_column(String(192), default="")     # 空=无实例维度（订单/平台类）
    instance_name: Mapped[str] = mapped_column(String(255), default="")
    billing_item: Mapped[str] = mapped_column(String(128), default="")
    subscription_type: Mapped[str] = mapped_column(String(16), default="")  # Subscription / PayAsYouGo
    region: Mapped[str] = mapped_column(String(32), default="")
    pretax_amount: Mapped[float] = mapped_column(Float, default=0)        # 账单金额（判断花费的唯一口径）
    pretax_gross_amount: Mapped[float] = mapped_column(Float, default=0)  # 原价
    payment_amount: Mapped[float] = mapped_column(Float, default=0)       # 挂账现金字段（常 0，不作依据）
    usage: Mapped[float] = mapped_column(Float, default=0)                # 用量（可无）
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<CostDaily {self.stat_date} {self.source} {self.product_code} {self.instance_id} {self.pretax_amount}>"
