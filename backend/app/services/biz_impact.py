"""业务影响评估：告警挂业务影响等级。

口径：影响 = 目标映射到的业务线（biz_map）× 资产分级（tier）× 告警级别。
产出人话 impact_desc："影响【核心业务 web-prod】（域名级，24h 96 万请求）"——
运维向老板解释"影响多少用户"的武器。无业务映射时如实 unknown，不硬猜。
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

logger = logging.getLogger("qianshu.biz_impact")


def assess(db: Session, target: str, target_name: str = "", level: str = "warning") -> dict:
    """→ {impact_level, impact_desc}。评估失败/无数据时 unknown（不硬凑）。"""
    try:
        from app.models.asset import Asset
        from app.models.link_edge import LinkEdge
        from app.services.biz_map import resolve_biz_label

        biz = resolve_biz_label(db, target=target, target_name=target_name) or ""
        tier = ""
        req24h = 0
        a = (db.query(Asset).filter(Asset.instance_id == target).first()
             or db.query(Asset).filter(Asset.name == target_name).first()) if (target or target_name) else None
        if a is not None:
            tier = a.tier or ""
        for e in db.query(LinkEdge).filter(LinkEdge.host == (target_name or target)).all():
            req24h += e.req_count or 0

        parts = []
        if biz:
            parts.append(f"业务【{biz}】")
        if tier == "core":
            parts.append("核心资产")
        elif tier == "aux":
            parts.append("辅助资产")
        if req24h:
            parts.append(f"域名 24h {req24h:,} 请求")

        if not parts:
            return {"impact_level": "unknown", "impact_desc": "未映射业务（可在业务映射补充后自动提升影响判断）"}

        if level == "critical" and (tier == "core" or req24h > 10000):
            impact = "high"
        elif tier == "core" or req24h > 1000:
            impact = "medium"
        else:
            impact = "low"
        return {"impact_level": impact, "impact_desc": "，".join(parts)}
    except Exception as e:
        logger.warning("业务影响评估失败: %s", str(e)[:80])
        return {"impact_level": "unknown", "impact_desc": ""}
