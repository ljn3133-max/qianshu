"""链路拓扑 API：草稿生成、边列表、域名健康汇总、逐边确认/忽略、业务线与逻辑标识挂接。"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.client_ip import get_client_ip
from app.core.db import get_db
from app.core.deps import require_roles
from app.models.business import Business, BizIdentifier
from app.models.link_edge import LinkEdge
from app.models.user import User
from app.schemas.link import BusinessIn, EdgeStatusIn, IdentifierIn
from app.services import audit
from app.services.link_draft import generate_link_draft, get_status

logger = logging.getLogger("qianshu.link")
router = APIRouter(prefix="/links", tags=["links"])

# 健康判定阈值（域名级）
_WARN_5XX_RATE = 0.001     # 5xx 率 >= 0.1% 预警
_CRIT_5XX_RATE = 0.01      # 5xx 率 >= 1% 严重
_WARN_RT_MS = 500.0        # 平均 RT >= 500ms 预警
_WARN_APP_ERR = 100_000    # 应用 ERROR 日志 >= 10万/天 预警
_WARN_CPU = 90.0           # 后端 ECS CPU 峰值 >= 90% 预警
_MIN_REQ = 1000            # 请求数低于此值不做 5xx 率判定（样本太小）


def _edge_out(e: LinkEdge) -> dict:
    try:
        paths = json.loads(e.path_summary) if e.path_summary else []
    except ValueError:
        paths = []
    return {
        "id": e.id,
        "chain": e.chain,
        "host": e.host,
        "path_pattern": e.path_pattern or "",
        "upstream_addr": e.upstream_addr,
        "rule_name": e.rule_name or "",
        "rule_source": e.rule_source or "log",
        "ecs_name": e.ecs_name,
        "app_name": e.app_name,
        "req_count": e.req_count,
        "cnt_4xx": e.cnt_4xx,
        "cnt_5xx": e.cnt_5xx,
        "rt_avg_ms": e.rt_avg_ms,
        "ecs_cpu": e.ecs_cpu,
        "app_error_cnt": e.app_error_cnt,
        "paths": paths,
        "status": e.status,
        "note": e.note,
        "confirmed_by": e.confirmed_by,
        "confirmed_at": e.confirmed_at.isoformat() if e.confirmed_at else None,
        "generated_at": e.generated_at.isoformat() if e.generated_at else None,
    }


def _host_health(host: str, edges: list) -> dict:
    """把一个域名的全部边汇总成健康画像（与巡检阈值风格一致）。"""
    total = sum(e.req_count for e in edges)
    c5xx = sum(e.cnt_5xx for e in edges)
    c4xx = sum(e.cnt_4xx for e in edges)
    # 无请求/无 RT 样本时 RT 是"没有样本"，不是 0ms。
    # 只对真有 RT 样本的边做流量加权，避免 0 占位把域名均值拽低。
    rt_edges = [e for e in edges if e.req_count and (e.rt_avg_ms or 0) > 0]
    rt_den = sum(e.req_count for e in rt_edges)
    rt = (sum(e.rt_avg_ms * e.req_count for e in rt_edges) / rt_den) if rt_den else None
    cpu = max((e.ecs_cpu for e in edges if e.ecs_cpu is not None), default=None)
    app_err = max((e.app_error_cnt for e in edges), default=0)
    rate = c5xx / total if total else 0.0

    signals: list = []
    level = "ok"
    if total >= _MIN_REQ and rate >= _CRIT_5XX_RATE:
        level = "critical"
        signals.append(f"5xx率 {rate * 100:.2f}%")
    elif total >= _MIN_REQ and rate >= _WARN_5XX_RATE:
        level = "warning"
        signals.append(f"5xx率 {rate * 100:.2f}%")
    if rt is not None and rt >= _WARN_RT_MS:
        level = "critical" if level == "critical" else "warning"
        signals.append(f"平均RT {rt:.0f}ms")
    if cpu is not None and cpu >= _WARN_CPU:
        level = "critical" if level == "critical" else "warning"
        signals.append(f"后端CPU峰值 {cpu:.0f}%")
    if app_err >= _WARN_APP_ERR:
        level = "critical" if level == "critical" else "warning"
        signals.append(f"ERROR日志 {app_err:,}/天")

    return {
        "host": host,
        "chains": sorted({e.chain for e in edges}),
        "req_count": total,
        "cnt_4xx": c4xx,
        "cnt_5xx": c5xx,
        "rate_5xx": round(rate, 6),
        "rt_avg_ms": round(rt, 1) if rt is not None else None,
        "ecs_cpu": cpu,
        "app_name": next((e.app_name for e in edges if e.app_name), ""),
        "app_error_cnt": app_err,
        "backends": sorted({e.ecs_name or e.upstream_addr for e in edges}),
        "level": level,
        "signals": signals,
    }


@router.get("/status")
def link_status(
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """草稿生成状态 + 库内边统计。"""
    s = get_status()
    by_status = dict(
        db.query(LinkEdge.status, func.count(LinkEdge.id))
        .group_by(LinkEdge.status).all()
    )
    return {
        "running": s.get("running", False),
        "last_run_at": s.get("last_run_at"),
        "last_result": s.get("last_result"),
        "counts": by_status,
    }


@router.post("/draft/generate")
def trigger_generate(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """手动生成/刷新链路草稿（同步执行，约 10~30s）。

    skipped（SLS 未配置）/ busy（生成中）不报错、不进审计，返回带 message 的
    结果体由前端提示，避免前端从缺字段的响应里拼出 undefined 文案。
    """
    result = generate_link_draft(db)
    if result.get("busy") or result.get("skipped"):
        return result
    audit.record(
        db, action="link.draft.generate", user_id=user.id, username=user.username,
        resource_type="link_edge", resource_id="",
        detail=result,
        client_ip=get_client_ip(request),
    )
    db.commit()
    return result


@router.get("")
def list_edges(
    chain: str = Query("", max_length=16),
    status: str = Query("", max_length=16),
    host: str = Query("", max_length=255, description="模糊匹配"),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """链路边列表（链路草稿）。按 chain 分组、请求数降序。"""
    q = db.query(LinkEdge)
    if chain:
        q = q.filter(LinkEdge.chain == chain)
    if status:
        q = q.filter(LinkEdge.status == status)
    if host:
        q = q.filter(LinkEdge.host.ilike(f"%{host}%"))
    rows = q.order_by(
        LinkEdge.chain,
        LinkEdge.host,
        LinkEdge.req_count.desc(),
    ).limit(1000).all()
    return {"total": len(rows), "items": [_edge_out(e) for e in rows]}


@router.get("/hosts")
def list_hosts(
    level: str = Query("", max_length=16, description="健康级别过滤 ok/warning/critical"),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """域名级健康汇总（链路 + 24h 请求/5xx/RT/后端CPU/应用ERROR 一览）。

    诊断 Agent 与 前端"域名健康一览"共用的入口。
    """
    rows = db.query(LinkEdge).order_by(LinkEdge.req_count.desc()).limit(2000).all()
    by_host: dict = {}
    for e in rows:
        by_host.setdefault(e.host, []).append(e)
    hosts = [_host_health(h, es) for h, es in by_host.items()]
    hosts.sort(key=lambda x: (-x["req_count"]))
    if level:
        hosts = [h for h in hosts if h["level"] == level]
    return {"total": len(hosts), "items": hosts}


# ==================== 逐边确认 ====================

@router.post("/edges/status")
def set_edge_status(
    body: EdgeStatusIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """批量确认/忽略/恢复草稿。重新生成草稿只覆盖 draft，已确认/忽略状态保留。"""
    rows = db.query(LinkEdge).filter(LinkEdge.id.in_(body.ids)).all()
    found = {r.id for r in rows}
    missing = set(body.ids) - found
    if missing:
        raise HTTPException(404, f"边不存在: {sorted(missing)[:5]}")
    now = datetime.now()
    for r in rows:
        r.status = body.status
        if body.note:
            r.note = body.note
        if body.status == "draft":
            r.confirmed_by, r.confirmed_at = "", None
            r.confirmed_by_id = 0   # 恢复草稿时清空确认人 id
        else:
            r.confirmed_by, r.confirmed_at = user.username, now
            r.confirmed_by_id = user.id
    audit.record(
        db, action="link.edge.status", user_id=user.id, username=user.username,
        resource_type="link_edge", resource_id=",".join(map(str, body.ids)),
        detail={"ids": body.ids, "status": body.status, "note": body.note},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return {"updated": len(rows), "status": body.status}


# ==================== 业务线与逻辑标识（biz_identifiers） ====================

def _biz_out(b: Business, counts: dict) -> dict:
    return {
        "id": b.id,
        "name": b.name,
        "code": b.code,
        "description": b.description,
        "status": getattr(b, "status", "confirmed"),
        "created_at": b.created_at.isoformat() if b.created_at else None,
        "ident_counts": counts,
    }


def _get_business(db: Session, business_id: int) -> Business:
    b = db.get(Business, business_id)
    if b is None:
        raise HTTPException(404, f"业务不存在: {business_id}")
    return b


@router.get("/businesses")
def list_businesses(
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """业务线列表（含标识类型计数）。"""
    out = []
    for b in db.query(Business).order_by(Business.id).all():
        counts = dict(
            db.query(BizIdentifier.ident_type, func.count(BizIdentifier.id))
            .filter(BizIdentifier.business_id == b.id)
            .group_by(BizIdentifier.ident_type).all()
        )
        out.append(_biz_out(b, counts))
    return {"total": len(out), "items": out}


@router.post("/businesses/draft")
def generate_biz_draft_api(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """根据已确认日志流量边和明确业务标签生成业务线草稿。

    通用域名首段、未知标签和共享标识冲突不自动归属；同名业务跳过，不覆盖人工数据。
    自动结果保持 draft，须通过专用确认接口逐条确认后才参与业务归属。
    """
    from app.services.biz_map import generate_biz_draft

    result = generate_biz_draft(db, created_by=user.username, created_by_id=user.id)
    audit.record(
        db, action="business.draft_generate", user_id=user.id, username=user.username,
        resource_type="business", resource_id="",
        detail=result, client_ip=get_client_ip(request),
    )
    db.commit()
    return result


@router.post("/businesses")
def create_business(
    body: BusinessIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """新建业务线。"""
    if db.query(Business.id).filter(Business.name == body.name).first():
        raise HTTPException(409, f"业务已存在: {body.name}")
    b = Business(name=body.name, code=body.code, description=body.description,
                 status="confirmed")
    db.add(b)
    db.flush()
    audit.record(
        db, action="business.create", user_id=user.id, username=user.username,
        resource_type="business", resource_id=str(b.id), detail=body.model_dump(),
        client_ip=get_client_ip(request),
    )
    db.commit()
    return _biz_out(b, {})


@router.put("/businesses/{business_id}")
def update_business(
    business_id: int,
    body: BusinessIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """编辑业务线。"""
    b = _get_business(db, business_id)
    dup = db.query(Business.id).filter(
        Business.name == body.name, Business.id != business_id).first()
    if dup:
        raise HTTPException(409, f"业务名已被占用: {body.name}")
    b.name, b.code, b.description = body.name, body.code, body.description
    audit.record(
        db, action="business.update", user_id=user.id, username=user.username,
        resource_type="business", resource_id=str(business_id), detail=body.model_dump(),
        client_ip=get_client_ip(request),
    )
    db.commit()
    return _biz_out(b, {})


@router.post("/businesses/{business_id}/confirm")
def confirm_business_api(
    business_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """确认自动草稿；确认前它不会出现在任何业务归属消费者中。"""
    b = _get_business(db, business_id)
    if b.status == "confirmed":
        return {"confirmed": False, "business": _biz_out(b, {})}
    b.status = "confirmed"
    audit.record(
        db, action="business.confirm", user_id=user.id, username=user.username,
        resource_type="business", resource_id=str(business_id),
        detail={"name": b.name}, client_ip=get_client_ip(request),
    )
    db.commit()
    return {"confirmed": True, "business": _biz_out(b, {})}


@router.delete("/businesses/{business_id}")
def delete_business(
    business_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """删除业务线（标识级联删除）。"""
    b = _get_business(db, business_id)
    name = b.name
    db.delete(b)
    audit.record(
        db, action="business.delete", user_id=user.id, username=user.username,
        resource_type="business", resource_id=str(business_id), detail={"name": name},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return {"deleted": business_id, "name": name}


@router.post("/businesses/{business_id}/identifiers")
def add_identifier(
    business_id: int,
    body: IdentifierIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """给业务线挂逻辑标识（域名/应用名/库名/实例名/IP/实例 ID）。

    全部键位都从本入口挂接；ip/instance 新键按归属登记同一套归一规则入库
    （IP 去端口/大小写不敏感），去重用归一值。
    """
    _get_business(db, business_id)
    value = body.ident_value.strip()
    if body.ident_type in ("ip", "instance"):
        from app.services.ownership import norm_key

        value = norm_key(body.ident_type, value)
    if not value:
        raise HTTPException(400, "ident_value 不能为空")
    dup = db.query(BizIdentifier.id).filter(
        BizIdentifier.business_id == business_id,
        BizIdentifier.ident_type == body.ident_type,
        BizIdentifier.ident_value == value,
    ).first()
    if dup:
        raise HTTPException(409, f"标识已存在: {body.ident_type} = {value}")
    ident = BizIdentifier(
        business_id=business_id, ident_type=body.ident_type,
        ident_value=value, note=body.note,
        created_by=user.username, created_by_id=user.id,   # created_by_id 引用 users.id
    )
    db.add(ident)
    db.flush()
    audit.record(
        db, action="business.ident.add", user_id=user.id, username=user.username,
        resource_type="biz_identifier", resource_id=str(ident.id),
        detail=body.model_dump() | {"ident_value": value, "business_id": business_id},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return {"id": ident.id, "business_id": business_id,
            **{**body.model_dump(), "ident_value": value}}


@router.delete("/businesses/{business_id}/identifiers/{ident_id}")
def delete_identifier(
    business_id: int,
    ident_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """摘除业务逻辑标识。"""
    _get_business(db, business_id)
    ident = db.get(BizIdentifier, ident_id)
    if ident is None or ident.business_id != business_id:
        raise HTTPException(404, f"标识不存在或不属于该业务: {ident_id}")
    db.delete(ident)
    audit.record(
        db, action="business.ident.delete", user_id=user.id, username=user.username,
        resource_type="biz_identifier", resource_id=str(ident_id),
        detail={"business_id": business_id,
                "ident": f"{ident.ident_type}={ident.ident_value}"},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return {"deleted": ident_id}


@router.get("/businesses/{business_id}/map")
def business_map(
    business_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """业务视图：标识 -> 链路自动解析（域名健康/承载后端/应用）+ 其余标识登记展示。

    host/app_name 类型复用 link_edges 证据解析；db_name/实例名仅登记。
    """
    b = _get_business(db, business_id)
    idents = (
        db.query(BizIdentifier)
        .filter(BizIdentifier.business_id == business_id)
        .order_by(BizIdentifier.ident_type, BizIdentifier.ident_value)
        .all()
    )
    out_idents = [{
        "id": i.id, "ident_type": i.ident_type, "ident_value": i.ident_value,
        "note": i.note, "created_by": i.created_by,
        "created_at": i.created_at.isoformat() if i.created_at else None,
    } for i in idents]

    # host 标识 -> 链路边聚合出后端与应用
    host_values = {i.ident_value.lower() for i in idents if i.ident_type == "host"}
    app_values = {i.ident_value for i in idents if i.ident_type == "app_name"}
    edges = db.query(LinkEdge).order_by(LinkEdge.req_count.desc()).limit(2000).all()

    matched = [e for e in edges
               if e.host.lower() in host_values or e.app_name in app_values]
    by_host: dict = {}
    for e in matched:
        by_host.setdefault(e.host, []).append(e)
    domains = [_host_health(h, es) for h, es in by_host.items()]
    domains.sort(key=lambda x: (-x["req_count"]))

    backends = sorted({e.ecs_name or e.upstream_addr for e in matched})
    apps = sorted({e.app_name for e in matched if e.app_name})
    chains = sorted({e.chain for e in matched})
    edge_status = {}
    for e in matched:
        edge_status[e.status] = edge_status.get(e.status, 0) + 1

    return {
        "business": _biz_out(b, {}),
        "identifiers": out_idents,
        "domains": domains,
        "backends": backends,
        "apps": apps,
        "chains": chains,
        "edge_status": edge_status,
        "total_req_24h": sum(e.req_count for e in matched),
    }
