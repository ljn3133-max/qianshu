"""系统快照：回答前必查，避免脱离系统事实作答。

聚合五面（全部本地查表/配置读取，不做网络探测，毫秒级）：
  云账号数与厂商 / 数据源启用清单 / 资产规模 / 拓扑覆盖 / 最近变更 5 条
renderer 用它生成"快照引用条"，loop 把它写进系统提示与首条消息快照留档。
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import func

from app.models.account import Account
from app.models.asset import Asset
from app.models.change_event import ChangeEvent
from app.models.cloud_account import CloudAccount
from app.models.link_edge import LinkEdge

VENDOR_LABELS = {
    "aliyun": "阿里云", "tencent": "腾讯云", "volc": "火山云", "baidu": "百度云",
    "huawei": "华为云", "aws": "AWS", "azure": "Azure", "onprem": "自建/IDC",
}


def _vendor_label(v: str) -> str:
    return VENDOR_LABELS.get((v or "").lower(), v or "未标注")


def _probe(db, fn, fallback):
    """在保存点内执行一段只读探测；失败只丢弃该段。

    直接 try/except 数据库异常会留下 aborted 事务，调用方随后 commit 会整单失败。
    保存点保证降级快照不会污染请求事务。
    """
    try:
        with db.begin_nested():
            return fn()
    except Exception:
        return fallback


def _cloud_accounts(db) -> dict:
    """云账号数与厂商 + 每账号资产规模/最近同步，供智能体建议使用。

    归属口径与总览资产底账一致：跳过迁移镜像行（无厂商）与无厂商台账行；
    这些账号资产不计入任何云账号，归入 unassigned_total，既不丢账也不冒充。
    """
    # 资产按账号聚合（Deleted 历史行不算在册资产）
    def _asset_counts() -> dict[int, dict]:
        out: dict[int, dict] = {}
        for aid, product, cnt in (
            db.query(Asset.account_id, Asset.product, func.count(Asset.id))
            .filter(Asset.status != "Deleted")
            .group_by(Asset.account_id, Asset.product)
            .all()
        ):
            d = out.setdefault(int(aid or 0), {"total": 0, "products": 0})
            d["total"] += int(cnt)
            d["products"] += 1
        return out

    acc_assets = _probe(db, _asset_counts, {})

    def _account_rows() -> tuple[list[dict], set[int]]:
        out: list[dict] = []
        ids: set[int] = set()
        for a in db.query(CloudAccount).order_by(CloudAccount.id).all():
            try:
                extra = json.loads(a.extra_json) if a.extra_json else {}
            except ValueError:
                extra = {}
            if extra.get("migrated_from") == "accounts_p22" or not a.vendor:
                continue   # 迁移镜像行/无厂商台账行：不是"已接入的云"
            ids.add(a.id)
            counts = acc_assets.get(a.id, {})
            out.append({
                "id": a.id, "name": a.name, "vendor": a.vendor or "",
                "status": a.status or "", "enabled": bool(a.enabled),
                "last_sync_at": (a.last_sync_at.isoformat(timespec="seconds")
                                 if a.last_sync_at else None),
                "asset_total": counts.get("total", 0),
                "product_count": counts.get("products", 0),
            })
        return out, ids

    rows, claimed = _probe(db, _account_rows, ([], set()))
    if not rows:
        # 老环境回退：数据分组（无厂商/无同步事实，仅回答"有几个分组"）
        claimed = set()

        def _legacy_rows() -> list[dict]:
            return [{"name": a.name, "vendor": "", "status": ""}
                    for a in db.query(Account).order_by(Account.id).all()]

        rows = _probe(db, _legacy_rows, [])
    vendors: dict = {}
    for r in rows:
        label = _vendor_label(r.get("vendor") or "")
        vendors[label] = vendors.get(label, 0) + 1
    unassigned = sum(v["total"] for aid, v in acc_assets.items() if aid not in claimed)
    return {
        "total": len(rows), "vendors": vendors,
        "unassigned_total": unassigned,
        "items": rows[:20],
    }


def _datasources(db) -> dict:
    """数据源启用清单（配置面，不探测连通——探测归集成中心）。

    日志源必须走 log_query.source_entries：SLS 在只登记云账号凭证、未单独登记
    集成中心日志源时会合成隐式源。只读 LOG_SOURCES 会把"日志能查"误报为
    "日志 0 类"，导致快照与日志页/数据完整度顾问的口径漂移。
    """
    from app.services.datasource import metric_sources
    from app.services.log_query import source_entries

    metrics = [{"name": s.get("name", ""), "type": s.get("type", "")} for s in metric_sources()]
    entries = _probe(db, lambda: source_entries(db), [])
    logs = [{"name": e.get("name", ""), "type": e.get("type", ""),
             "status": e.get("status", "")} for e in entries]
    return {
        "metric": metrics,
        "log": logs,
        # 与日志页同口径：只有真正可查（status=ready）的日志源才算"启用"
        "enabled_count": len(metrics) + sum(1 for e in logs if e["status"] == "ready"),
    }


def _assets(db) -> dict:
    def _count() -> tuple[int, dict]:
        rows = (db.query(Asset.product, func.count(Asset.id))
                .filter(Asset.status != "Deleted")
                .group_by(Asset.product).all())
        by_product = {p: n for p, n in rows}
        return sum(by_product.values()), by_product

    total, by_product = _probe(db, _count, (0, {}))
    return {"total": total, "by_product": by_product}


def _topology(db) -> dict:
    def _count() -> tuple[int, int, int, int]:
        total = db.query(func.count(LinkEdge.id)).scalar() or 0
        hosts = db.query(func.count(func.distinct(LinkEdge.host))).scalar() or 0
        apps = db.query(func.count(func.distinct(LinkEdge.app_name))).scalar() or 0
        confirmed = (db.query(func.count(LinkEdge.id))
                     .filter(LinkEdge.status == "confirmed").scalar() or 0)
        return total, hosts, apps, confirmed

    total, hosts, apps, confirmed = _probe(db, _count, (0, 0, 0, 0))
    return {"edges": total, "hosts": hosts, "apps": apps,
            "confirmed": confirmed, "draft": total - confirmed}


def _recent_changes(db) -> list:
    def _rows() -> list:
        out = []
        rows = (db.query(ChangeEvent)
                .order_by(ChangeEvent.event_time.desc())
                .limit(5).all())
        for r in rows:
            out.append({
                "time": r.event_time.strftime("%Y-%m-%d %H:%M") if r.event_time else "",
                "service": r.service or "",
                "event_name": r.event_name or "",
                "resource": (r.resource or "")[:80],
                "user": r.user or "",
                "summary": (r.summary or "")[:120],
            })
        return out

    return _probe(db, _rows, [])


def build_snapshot(db) -> dict:
    """系统快照 dict（GET /agent/system-snapshot 与 SSE snapshot 事件同一份）。"""
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "cloud_accounts": _cloud_accounts(db),
        "datasources": _datasources(db),
        "assets": _assets(db),
        "topology": _topology(db),
        "recent_changes": _recent_changes(db),
    }
