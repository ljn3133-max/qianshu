from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


class Business(Base):
    """业务线定义，如 OKR / 绩效 / 360评估。

    业务与物理资源是多对多且混合部署（一个 ECS/RDS 承载多个业务），
    归属只能靠逻辑标识（域名/应用名/库名/实例名）识别，挂在 BizIdentifier 上。
    """

    __tablename__ = "businesses"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)   # 业务线名称
    code: Mapped[str] = mapped_column(String(64), default="")     # 短码（可选）
    description: Mapped[str] = mapped_column(String(500), default="")
    # 自动草稿只是待核对建议；只有 confirmed 才参与告警、巡检、成本和域名归属。
    # 存量人工映射迁移时默认 confirmed，旧自动草稿由迁移显式降为 draft。
    status: Mapped[str] = mapped_column(String(16), default="confirmed", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    identifiers: Mapped[list["BizIdentifier"]] = relationship(
        back_populates="business", cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Business {self.id} {self.name}>"


class BizIdentifier(Base):
    """业务逻辑标识（biz_identifiers）：业务 -> 域名/应用名/库名/实例名/IP/实例 ID。

    - ident_type 白名单：host(SLB域名) / app_name(AppName) / db_name(RDS库名)
      / ecs_name(ECS实例名) / rds_instance(RDS实例) / ip(IP) / instance(实例 ID)。
      ip/instance 与归属登记（ownership_rules）同一套实体键（IP 去端口、大小写不敏感），
      业务统一挂接后按登记优先合并展示。
    - host/app_name 可对 link_edges 自动解析出承载的后端；db_name/实例名仅登记展示。
    - 同一标识可挂多个业务（多对多事实，不做唯一约束到单业务）——这也是业务登记
      不并入 ownership_rules（(key_type,key_value) 唯一、单 business 文本）的原因。
    """

    __tablename__ = "biz_identifiers"
    __table_args__ = (
        UniqueConstraint("business_id", "ident_type", "ident_value", name="uq_biz_ident"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    ident_type: Mapped[str] = mapped_column(String(32), index=True)
    ident_value: Mapped[str] = mapped_column(String(255))
    note: Mapped[str] = mapped_column(String(500), default="")
    # 身份引用：created_by=username 快照，created_by_id=users.id
    created_by: Mapped[Optional[str]] = mapped_column(String(64), default="")
    created_by_id: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    business: Mapped[Business] = relationship(back_populates="identifiers")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<BizIdentifier {self.business_id} {self.ident_type}={self.ident_value}>"
