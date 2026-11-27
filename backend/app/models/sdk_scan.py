"""SDK 版本扫描记录：每周一定时 + 手动触发，保留最近 12 次。

产品语义：云 SDK 锁版本不自动升级，但要知道"什么时候扫的、哪些包可升级/有废弃信号"。
明细存 results_json（[{pkg, current, latest, outdated, deprecated_hint}, ...]），
汇总列平铺便于快速看历史趋势。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class SdkScanRecord(Base):
    __tablename__ = "sdk_scan_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    scanned_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    trigger: Mapped[str] = mapped_column(String(16), default="manual")   # manual / timer
    total: Mapped[int] = mapped_column(Integer, default=0)
    up_to_date: Mapped[int] = mapped_column(Integer, default=0)
    outdated: Mapped[int] = mapped_column(Integer, default=0)
    deprecated: Mapped[int] = mapped_column(Integer, default=0)
    not_installed: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    results_json: Mapped[Optional[str]] = mapped_column(Text, default="")   # 扫描明细

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SdkScanRecord {self.scanned_at} outdated={self.outdated} deprecated={self.deprecated}>"
