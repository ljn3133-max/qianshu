from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Asset(Base):
    """云资源资产基表：所有产品共用，产品差异放 extra(JSON)。
    (product, instance_id) 唯一。raw_meta 存最近一次 API 原始快照(裁剪后)。
    """

    __tablename__ = "assets"
    __table_args__ = (
        UniqueConstraint("product", "instance_id", name="uq_asset_product_instance"),
        {"comment": "云资产统一表"},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # 账号维度（2026-11-09 双概念唯一化）：显式指向 cloud_accounts.id
    # （历史数据分组 accounts 已洗入云账号台账保 id，见 migrate_account_concept_unify）；
    # accounts 表退为纯「数据分组」标签，不再占用本列语义
    account_id: Mapped[int] = mapped_column(default=1, index=True)
    product: Mapped[str] = mapped_column(String(16), index=True)  # ecs/rds/redis/slb/...
    instance_id: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    region: Mapped[str] = mapped_column(String(32), default="", index=True)
    zone: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(32), default="")  # Running/Stopped/Deleted...
    tier: Mapped[str] = mapped_column(String(16), default="core")  # core=业务核心 / aux=辅助机器（只看关机+5xx外溢）
    instance_type: Mapped[str] = mapped_column(String(64), default="")
    charge_type: Mapped[str] = mapped_column(String(32), default="")  # PrePaid/PostPaid
    expire_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    tags: Mapped[Optional[str]] = mapped_column(Text, default="")  # JSON 字符串
    extra: Mapped[Optional[str]] = mapped_column(Text, default="")  # JSON 字符串，产品差异字段
    raw_meta: Mapped[Optional[str]] = mapped_column(Text, default="")  # JSON 字符串，API 快照
    synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)
