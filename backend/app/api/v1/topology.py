"""拓扑图 API：明细与汇报双视图共用数据源。

GET /topology/graph?redact=0|1&source=all|aliyun|private|tencent|unassigned|<provider>
  redact=1 → 脱敏版（实例 ID/内网 IP/规模数字抹掉），对外汇报导出用；
  source → 数据源分版（汇总/单云/私有云/未归属），非法值 400（不画空图被误读）。
  明细/汇报两种视图共用同一份图数据，布局与皮肤在前端做。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.core.client_ip import get_client_ip
from app.core.db import get_db
from app.core.deps import get_current_user, require_roles
from app.models.user import User
from app.services.topology import (SOURCE_ALL, build_arch, build_graph,
                                 last_scan_info, link_integrity, run_scan_async,
                                 scan_snapshot, scan_status, valid_sources)

logger = logging.getLogger("qianshu.topology_api")
router = APIRouter(prefix="/topology", tags=["topology"])

_SOURCE_DESC = "数据源：all=汇总；private=私有云；<provider>=阿里云/腾讯云等；unassigned=未归属"


def _check_source(source: str) -> str:
    key = (source or SOURCE_ALL).strip().lower()
    if key not in valid_sources():
        raise HTTPException(
            400, f"未知数据源: {source}（可选: {', '.join(sorted(valid_sources()))}）")
    return key


@router.get("/graph")
def topology_graph(
    redact: int = Query(0, description="1=脱敏版（对外导出用）"),
    source: str = Query(SOURCE_ALL, description=_SOURCE_DESC),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return build_graph(db, redact=bool(redact), source=_check_source(source))


@router.get("/arch")
def topology_arch(
    redact: int = Query(0, description="1=脱敏版（对外导出用）"),
    source: str = Query(SOURCE_ALL, description=_SOURCE_DESC),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """架构图数据：分组（架构层级）→ 组件/集群框 → 依赖连线（前端 SVG 框图渲染）。"""
    out = build_arch(db, redact=bool(redact), source=_check_source(source))
    out["last_scan"] = last_scan_info(db)
    return out


@router.post("/scan")
def topology_scan(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """手动全量扫描（异步）：立即返回，前端轮询 /scan/status 拿进度与结果。

    扫描会触发全量资产同步，属写路径；只读角色不应反复触发占满同步通道，
    权限与 /clouds/sync 保持一致。
    """
    from app.services import audit as audit_svc

    result = run_scan_async()
    audit_svc.record(
        db, action="topology.scan", user_id=user.id, username=user.username,
        resource_type="topology", resource_id="async",
        detail={"started": result.get("started")},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return result


@router.get("/scan/status")
def topology_scan_status(user: User = Depends(get_current_user)):
    """扫描状态（running/stage/last_result）——前端 2 秒轮询。"""
    st = scan_status()
    last = st.pop("last_result", None)
    if isinstance(last, dict) and isinstance(last.get("synced"), dict):
        # 同步摘要保留给前端（"扫描了什么"），只藏 diff 基线 _signature
        last["synced"] = {k: v for k, v in last["synced"].items() if k != "_signature"}
    st["last_result"] = last
    return st


@router.get("/scans")
def topology_scans(
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """扫描历史（含每次的组件变化 diff）。"""
    from app.models.topology_scan import TopologyScan

    rows = db.query(TopologyScan).order_by(TopologyScan.id.desc()).limit(limit).all()
    out = []
    for r in rows:
        import json as _json
        try:
            diff = _json.loads(r.diff_json or "{}")
        except Exception:
            diff = {}
        out.append({"id": r.id, "scanned_at": r.scanned_at.isoformat(timespec="seconds"),
                    "trigger": r.trigger, "groups": r.groups, "links": r.links,
                    "members": r.members, "duration_ms": r.duration_ms, "diff": diff,
                    "has_snapshot": bool(r.arch_json)})
    return out


@router.get("/scan/snapshot")
def topology_scan_snapshot(
    scan_id: int = Query(0, ge=0, description="扫描记录 ID；0=最近一次"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """完整历史架构快照（含组成员与连线）；老记录无快照时如实返回 arch=null。"""
    out = scan_snapshot(db, scan_id=scan_id)
    if out is None:
        if scan_id:
            raise HTTPException(404, "扫描记录不存在")
        return {"scan_id": None, "scanned_at": None, "trigger": None,
                "arch": None, "note": "尚未执行拓扑扫描，无架构快照"}
    return out


@router.get("/integrity")
def topology_integrity(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """链路完整性：断点清单 + 补齐方案（域名断头/组件孤岛/整层缺位）。"""
    return link_integrity(db)
