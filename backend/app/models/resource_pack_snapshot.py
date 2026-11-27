from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class ResourcePackSnapshot(Base):
    """资源包每日余量快照。每天采集成本后调用 QueryResourcePackageInstances 落一份。"""

    __tablename__ = "resource_pack_snapshots"
    __table_args__ = (
        UniqueConstraint("snapshot_date", "instance_id", name="uq_pack_snapshot_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(Integer, default=1, index=True)  # 多账号打标
    snapshot_date: Mapped[date] = mapped_column(Date, index=True)
    instance_id: Mapped[str] = mapped_column(String(128), index=True)
    commodity_code: Mapped[str] = mapped_column(String(64), default="")
    package_type: Mapped[str] = mapped_column(String(64), default="")     # FlowPack / StoragePackage ...
    applicable_products: Mapped[str] = mapped_column(Text, default="")    # 逗号分隔产品码
    region: Mapped[str] = mapped_column(String(64), default="")
    deduct_type: Mapped[str] = mapped_column(String(32), default="")      # Absolute / DeadlineAcc
    total_amount: Mapped[float] = mapped_column(Float, default=0)
    total_unit: Mapped[str] = mapped_column(String(32), default="")
    remaining_amount: Mapped[float] = mapped_column(Float, default=0)
    remaining_unit: Mapped[str] = mapped_column(String(32), default="")
    remaining_pct: Mapped[float] = mapped_column(Float, default=0)        # remaining/total * 100
    status: Mapped[str] = mapped_column(String(32), default="")           # Active / Expired ...
    effective_time: Mapped[str] = mapped_column(String(64), default="")
    expiry_time: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<PackSnapshot {self.snapshot_date} {self.instance_id} {self.remaining_amount}{self.remaining_unit}>"
