"""资产关键度分级：按业务重要程度区分监测强度。

辅助机器不需要反复指标告警，但服务器关机、SLB 5xx 外溢归因仍须覆盖。
监测强度跟业务关键度对齐（云监控对所有实例一视同仁）：
- core（业务核心，默认）：全套监测（指标告警/画像/基线）
- aux（辅助机器）：指标告警与画像全部豁免；只监测 host_down（关机）与
  SLB 5xx 外溢归因
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.asset import Asset

TIER_CORE = "core"
TIER_AUX = "aux"


def aux_instance_ids(db: Session) -> set:
    """当前标记为 aux 的实例 ID 集合（告警/画像豁免名单）。"""
    try:
        return {
            r[0] for r in db.query(Asset.instance_id)
            .filter(Asset.tier == TIER_AUX, Asset.status != "Deleted").all()
        }
    except Exception:
        return set()   # 迁移前的库（无 tier 列）不豁免任何东西


def tier_of(db: Session, instance_id: str) -> str:
    row = db.query(Asset.tier).filter(Asset.instance_id == instance_id).first()
    return (row[0] if row else TIER_CORE) or TIER_CORE
