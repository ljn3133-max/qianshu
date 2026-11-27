from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class DbaSpaceDaily(Base):
    """RDS 空间每日快照：DescribeResourceUsage 每日一采，支撑空间趋势图。

    趋势自采集启用日起累积（ResourceUsage 只有当前值，无历史可回填）。
    唯一键 (stat_date, instance_id)，重拉同日覆盖，幂等。
    """

    __tablename__ = "dba_space_daily"
    __table_args__ = (
        UniqueConstraint("stat_date", "instance_id", name="uq_dba_space_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    stat_date: Mapped[date] = mapped_column(Date, index=True)
    instance_id: Mapped[str] = mapped_column(String(64), index=True)
    instance_name: Mapped[str] = mapped_column(String(255), default="")
    engine: Mapped[str] = mapped_column(String(32), default="")
    # 以下均为字节（API 返回原始字节数）
    data_size: Mapped[int] = mapped_column(BigInteger, default=0)       # 数据文件大小
    log_size: Mapped[int] = mapped_column(BigInteger, default=0)        # 日志大小
    backup_size: Mapped[int] = mapped_column(BigInteger, default=0)     # 备份总大小
    disk_used: Mapped[int] = mapped_column(BigInteger, default=0)       # 磁盘总使用
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<DbaSpaceDaily {self.stat_date} {self.instance_id} disk={self.disk_used}>"
