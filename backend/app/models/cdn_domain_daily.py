from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import BigInteger, Date, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class CdnDomainDaily(Base):
    """CDN/DCDN 按域名每日流量，用于定位流量包消耗来源。

    数据源（都是只读 API，北京自然日窗口 = 前一日 16:00Z ~ 当日 15:59Z）：
    - cdn:  cdn.aliyuncs.com 2014-11-11 DescribeDomainFlowData（小时点求和；
             注意该 action 在 2018-05-10 版本 404，必须用老版本）
    - dcdn: dcdn.aliyuncs.com 2018-01-15 DescribeDcdnDomainTrafficData

    域名清单：DescribeUserDomains（cdn 2018-05-10）+ DescribeDcdnUserDomains。
    唯一键 (stat_date, domain)，重拉同日覆盖（幂等）。
    """

    __tablename__ = "cdn_domain_daily"
    __table_args__ = (
        UniqueConstraint("stat_date", "domain", name="uq_cdn_domain_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(Integer, default=1, index=True)  # 多账号打标
    stat_date: Mapped[date] = mapped_column(Date, index=True)
    domain: Mapped[str] = mapped_column(String(255), index=True)
    product: Mapped[str] = mapped_column(String(16), default="cdn")  # cdn / dcdn
    traffic_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    collected_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.now)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<CdnDomainDaily {self.stat_date} {self.domain} {self.traffic_bytes}>"
