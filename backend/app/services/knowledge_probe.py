"""知识保鲜 · 探针自检。

云 API 口径会变化（CreateDownload 2026-03 才上线、BillingItem 恒空、
CLB 后端在 VServerGroup），知识是快照，需要持续验证，避免输出过期结论。

机制：对已知资产定期跑一遍关键工具，验证"还能出预期形状的数据"；
统计方式变化（空结果/报错/形状变化）→ 站内信告警"知识包可能过期"。
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

logger = logging.getLogger("qianshu.knowledge_probe")


def _probe_pack_pools(db: Session) -> dict:
    from app.services.pack_impact import pool_key  # noqa: F401  （导入即口径）
    from app.models.resource_pack_snapshot import ResourcePackSnapshot
    n = db.query(ResourcePackSnapshot).count()
    return {"ok": n > 0, "detail": f"资源包快照 {n} 条"}


def _probe_topology(db: Session) -> dict:
    from app.services.topology import build_arch
    arch = build_arch(db)
    n = arch["stats"]["groups"]
    return {"ok": n > 0, "detail": f"拓扑组件 {n} 个"}


def _probe_expiry(db: Session) -> dict:
    from app.services.expiry import expiry_items
    d = expiry_items(db)
    return {"ok": "items" in d, "detail": f"到期项 {len(d.get('items', []))} 条"}


def _probe_bill(db: Session) -> dict:
    from app.models.cost_daily import CostDaily
    from datetime import timedelta
    n = (db.query(CostDaily)
         .filter(CostDaily.stat_date >= datetime.now().date() - timedelta(days=3)).count())
    return {"ok": n > 0, "detail": f"近 3 天账单 {n} 行"}


def _probe_sls_tool(db: Session) -> dict:
    """验证 SLS 目标配置后工具口径仍活（未配置=跳过不算漂移）。"""
    from app.services.sls_targets import configured
    if not configured():
        return {"ok": True, "detail": "SLS 未配置（跳过，不判漂移）"}
    from app.services.diag_tools import execute_tool
    try:
        r = execute_tool(db, "find_domain", {"keyword": "t"})   # 链路库烟测（不打 SLS）
        return {"ok": isinstance(r, dict), "detail": "诊断工具烟测通过"}
    except Exception as e:
        return {"ok": False, "detail": f"诊断工具烟测失败: {str(e)[:100]}"}


PROBES = [
    ("资源包口径", _probe_pack_pools),
    ("拓扑构建", _probe_topology),
    ("到期账口径", _probe_expiry),
    ("账单采集", _probe_bill),
    ("诊断工具烟测", _probe_sls_tool),
]


def run_probes(db: Session) -> dict:
    """跑全部知识探针。任一 drift → 站内信告警。"""
    results = []
    drifted = []
    for name, fn in PROBES:
        try:
            r = fn(db)
        except Exception as e:
            r = {"ok": False, "detail": f"探针异常: {str(e)[:120]}"}
        results.append({"probe": name, **r})
        if not r["ok"]:
            drifted.append(name)

    if drifted:
        try:
            from app.services.notify import notify_inapp

            notify_inapp(
                db, f"知识探针告警：{len(drifted)} 项统计方式变化",
                ("探针自检发现异常（知识包可能过期）：\n"
                 + "\n".join(f"- {n}: {next(x['detail'] for x in results if x['probe'] == n)}"
                             for n in drifted)
                 + "\n\n请核对云 API 口径是否变化（知识保鲜流程）。"),
                level="warning", msg_type="system")
            db.commit()
        except Exception as e:
            logger.warning("知识探针告警写入失败: %s", str(e)[:80])
    logger.info("知识探针完成: %d 项，漂移 %d 项", len(results), len(drifted))
    return {"ran_at": datetime.now().isoformat(timespec="seconds"),
            "results": results, "drifted": drifted}
