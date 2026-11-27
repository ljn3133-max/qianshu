"""合成监控/拨测：HTTP/TCP/DNS 拨测任务与结果。

定位：外部可用性探测；HTTP 层记录状态码、证书和耗时，不包含渲染级浏览器探测。
结果存业务库（量小：任务数×频率），失败转告警（复用 AlertRecord rule_id=0 模式）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class SynthCheck(Base):
    __tablename__ = "synth_checks"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    check_type: Mapped[str] = mapped_column(String(16), default="http")   # http / tcp / dns
    target: Mapped[str] = mapped_column(String(512))                      # URL / host:port / 域名
    interval_sec: Mapped[int] = mapped_column(Integer, default=60)
    timeout_sec: Mapped[int] = mapped_column(Integer, default=10)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # HTTP 期望：状态码集合、证书到期预警天数
    expect_status: Mapped[str] = mapped_column(String(64), default="200-399")
    cert_warn_days: Mapped[int] = mapped_column(Integer, default=30)
    # 自签证书：True=TLS 握手跳过证书校验（内网自签目标）；默认 False 严格校验
    accept_self_signed: Mapped[bool] = mapped_column(Boolean, default=False)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_ok: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    last_latency_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    consecutive_fail: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class SynthResult(Base):
    __tablename__ = "synth_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    check_id: Mapped[int] = mapped_column(Integer, index=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    latency_ms: Mapped[float] = mapped_column(Float, default=0)
    status_code: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[str] = mapped_column(Text, default="")    # 错误信息/证书信息
    timing: Mapped[str] = mapped_column(Text, default="")    # JSON 分段耗时：dns/tcp/tls/ttfb/transfer/total
