"""域名登记表：统一关联域名→证书→SLB/CDN→业务。

同一域名散在 link_edges.host / cdn_domain_daily.domain / object_profiles（域名类）/
synth_checks.target / diagnosis_sessions.host / biz_identifiers，
形态各异（带端口/带路径/带 scheme），需要统一主权键串联证书、SLB、CDN 与业务。

本表 = 域名主权键（统一小写、去端口、去 scheme/路径）：
- 自动字段=事实（回填管道 upsert，采集可覆盖）：sources 来源标记、
  cert_serial/cert_not_after/cert_source 证书证据、updated_at
- 登记字段=人工/归属登记（采集永不覆盖，manual_fields 标记人工固定项）：
  business / vendor / account_name / note / status
  清空人工值=回到自动派生（归属登记/业务线登记/采集账号事实），登记优先
- 不提供删除：域名下线用 status=disabled 停用（或仅 note 备注），
  证据行保留可追溯（与清扫守卫同一保守口径）
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class DomainRegistry(Base):
    __tablename__ = "domain_registry"
    __table_args__ = (
        {"comment": "域名登记表：域名→证书→SLB/CDN→业务 一处收口"},
    )

    domain: Mapped[str] = mapped_column(String(255), primary_key=True)   # 归一键：小写去端口
    # ---- 自动字段=事实（回填管道维护） ----
    cert_serial: Mapped[str] = mapped_column(String(128), default="")    # 证书序列号（证据里有才填）
    cert_not_after: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # 证书到期
    cert_source: Mapped[str] = mapped_column(String(32), default="")     # tls_probe(拨测实测) / cas_order(证书订单)（统一解析口径）
    sources: Mapped[str] = mapped_column(String(128), default="")        # 来源标记，逗号分隔
    # ---- 登记字段=人工/归属登记（manual_fields 标记人工固定项，采集不覆盖） ----
    business: Mapped[str] = mapped_column(String(128), default="")       # 归属业务
    vendor: Mapped[str] = mapped_column(String(32), default="")          # 厂商码 aliyun/tencent/onprem/…
    account_name: Mapped[str] = mapped_column(String(64), default="")    # 账号名
    note: Mapped[str] = mapped_column(String(500), default="")           # 人工备注
    status: Mapped[str] = mapped_column(String(16), default="active")    # active / disabled（不提供删除）
    manual_fields: Mapped[str] = mapped_column(String(64), default="")   # 人工固定字段，如 "business,note"
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<DomainRegistry {self.domain} cert={self.cert_not_after} [{self.status}]>"
