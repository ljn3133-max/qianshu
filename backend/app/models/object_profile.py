"""对象全景画像：将业务对象的多层信号放在同一时间轴上分析。

与旧 behavior_profiles 的区别（旧的按"数据源"分孤岛：process/cdn_traffic/bill/slb_uri/
log_app 各报各的漂移）：这里画像对象是**业务对象**（域名/主机/数据库），每个对象
一条时间轴挂多层信号，变化判定看**层间联动是否被打破**。

隔离与回退：
- 使用独立表，不改旧表；开关 OBJECT_PROFILE_V2=False 时不采集不检测（旧画像引擎原样运行）
- 弃用=停开关后 DROP 本模块两张表即可，旧画像不受影响

表：
- object_profiles        对象注册 + 基线（各层水位 + 联动比值）
- object_profile_daily   逐日多层快照（layers JSON，一层一节）
- 变化事件复用 cost_anomalies（type=chain_drift），共用事件生命周期与确认态
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class ObjectProfile(Base):
    """业务对象注册与基线。"""

    __tablename__ = "object_profiles"
    __table_args__ = (UniqueConstraint("object_type", "object_key", name="uq_object_profile"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    object_type: Mapped[str] = mapped_column(String(16), index=True)   # domain / host / db
    object_key: Mapped[str] = mapped_column(String(255), index=True)   # cdn.example.com / i-xxx / pgm-xxx
    label: Mapped[str] = mapped_column(String(255), default="")
    tier: Mapped[str] = mapped_column(String(16), default="core")      # core / aux（aux 不做联动告警）
    # 基线：{"layers": {层名: {"median": x, "mad": x, "samples": n}},
    #        "couplings": {"cdn_gb:slb_reqs": {"median": r, "mad": x, "samples": n}},
    #        "top_files": [uri...]}   learned=False 时只记录不告警
    baseline: Mapped[dict] = mapped_column(JSON, default=dict)
    learned: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="observing")  # observing/normal/known/drift
    last_drift_note: Mapped[str] = mapped_column(Text, default="")
    # ----「已知道」状态+分级静默（2026-11-04，旧库由 app.core.migrate 幂等补列） ----
    known_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)   # 最近一次「知道了」
    silence_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # 静默截止（到期恢复提醒）
    notify_state: Mapped[str] = mapped_column(String(16), default="")   # ''=正常流转 / silenced=静默中 / remind=静默到期恢复提醒
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ObjectProfile {self.object_type}:{self.object_key} [{self.status}]>"


class ObjectSnapshotDaily(Base):
    """对象的逐日多层快照。layers 结构示例（缺层=该对象未映射该层，如实留空）：

    {"cdn":  {"traffic_gb": 12.3, "requests": 100000, "hit_rate": 0.91,
              "top_files": [{"uri": "/x.bin", "gb": 9.1}], "code_5xx": 12},
     "slb":  {"requests": 50000, "cnt_5xx": 3, "rt_avg_ms": 21},
     "app":  {"errors": 120, "top_apps": [{"name": "www.example.com", "errors": 100}]},
     "host": {"cpu_peak": 44.0, "mem_peak": 61.0, "disk_peak": 70.0, "backends": ["web1"]},
     "db":   {"cpu_peak": 30.0, "disk_gb": 647.0, "slow_count": 3},
     "change": {"count": 1, "items": ["10-07 18:02 Deploy xxx"]},
     "cost": {"yuan": 12.3, "pack_gb": 9.1}}
    """

    __tablename__ = "object_profile_daily"
    __table_args__ = (UniqueConstraint("object_type", "object_key", "stat_date", name="uq_object_snap"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    object_type: Mapped[str] = mapped_column(String(16), index=True)
    object_key: Mapped[str] = mapped_column(String(255), index=True)
    stat_date: Mapped[datetime] = mapped_column(DateTime, index=True)   # 存 date（取 DateTime 承载）
    layers: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ObjectSnapshotDaily {self.object_type}:{self.object_key} {self.stat_date}>"
