"""归属登记：实体 → 厂商/账号/业务 人工登记。

拓扑公网入口、域名、未纳管主机等无采集来源的实体，通过本表登记归属；
入口包括系统设置-归属配置、拓扑「去归属」和 API。

实体键（key_type, key_value）：
  ip       IP（登记/匹配时自动去端口，IPv4:port → IPv4）
  domain   域名（小写、去尾点）
  instance 实例 ID（大小写不敏感）
  name     资产/主机名（大小写不敏感）
match_type：exact 精确（默认）/ pattern 通配（fnmatch，如 *.example.com / 10.0.* / i-*）。

归属字段（vendor / account_name / business）**按字段覆盖**：登记了才覆盖自动值，
没登记的字段保留自动来源（采集打标/云账号台账）；全无配置 → 展示面如实显示"未归属"（不造数据）。
account_id 可选绑定 cloud_accounts（绑定后与云账号台账同名同色）；不绑定走自由文本归属
（IDC/第三方等），展示层用稳定虚拟账号 ID 聚合，不污染云账号台账。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class OwnershipRule(Base):
    __tablename__ = "ownership_rules"
    __table_args__ = (
        UniqueConstraint("key_type", "key_value", name="uq_ownership_key"),
        {"comment": "归属登记：实体键 -> 厂商/账号/业务"},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    key_type: Mapped[str] = mapped_column(String(16), index=True)      # ip/domain/instance/name
    key_value: Mapped[str] = mapped_column(String(255), index=True)    # 归一后的键值
    match_type: Mapped[str] = mapped_column(String(16), default="exact")  # exact/pattern
    vendor: Mapped[str] = mapped_column(String(32), default="")        # 厂商码 aliyun/tencent/onprem/…
    account_name: Mapped[str] = mapped_column(String(64), default="")  # 账号名（文本）
    account_id: Mapped[int] = mapped_column(Integer, default=0)        # 可选绑定 cloud_accounts.id
    business: Mapped[str] = mapped_column(String(128), default="")     # 归属业务
    note: Mapped[str] = mapped_column(String(500), default="")
    # 身份引用：created_by=username 快照，created_by_id=users.id
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_by_id: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<OwnershipRule {self.key_type}={self.key_value} -> {self.account_name or self.business}>"
