"""变更记录 API：云上变更时间线（排障第一问的数据面）。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.core.client_ip import get_client_ip
from app.core.db import get_db
from app.core.deps import require_roles
from app.models.change_event import ChangeEvent
from app.models.user import User
from app.services import audit

router = APIRouter(prefix="/changes", tags=["changes"])


@router.get("")
def list_changes(
    hours: int = Query(48, ge=1, le=24 * 30),
    service: str = Query(""),
    keyword: str = Query("", max_length=64),
    account_id: int = Query(0, ge=0),
    source: str = Query("", max_length=32),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """近 N 小时的云上变更（谁在什么时候改了什么）。"""
    from datetime import datetime, timedelta
    from sqlalchemy import or_

    since = datetime.now() - timedelta(hours=hours)
    q = db.query(ChangeEvent).filter(ChangeEvent.event_time >= since)
    if account_id:
        q = q.filter(ChangeEvent.account_id == account_id)
    if source:
        q = q.filter(ChangeEvent.source_type == source)
    if service:
        q = q.filter(ChangeEvent.service == service)
    if keyword:
        like = f"%{keyword}%"
        q = q.filter(or_(
            # ilike 使用户关键词搜索不区分大小写（PostgreSQL）
            ChangeEvent.event_name.ilike(like), ChangeEvent.resource.ilike(like),
            ChangeEvent.user.ilike(like), ChangeEvent.summary.ilike(like),
        ))
    total = q.count()
    rows = q.order_by(ChangeEvent.event_time.desc()).offset((page - 1) * page_size).limit(page_size).all()
    # 归属标识：account_id -> 账号名（云账号优先/数据分组兜底，无则空）
    from app.services.tenant import account_index

    acc_map = account_index(db)
    from app.services.change_source import change_sources_payload, source_label, stored_source_key
    source_options = {x["key"]: {"key": x["key"], "label": x["label"]}
                      for x in change_sources_payload()["items"]}
    stored_keys = {stored_source_key(key)
                   for (key,) in db.query(ChangeEvent.source_type).distinct().all()}
    for key in stored_keys:
        source_options.setdefault(key, {"key": key, "label": source_label(key)})
    return {
        "sources": sorted(source_options.values(), key=lambda x: x["key"]),
        "total": total,
        "items": [{
            "id": r.id, "event_time": r.event_time.isoformat(), "service": r.service,
            "event_name": r.event_name, "event_source": r.event_source,
            "user": r.user, "resource": r.resource,
            "resource_type": r.resource_type, "error_code": r.error_code,
            "summary": r.summary,
            "source": stored_source_key(r.source_type),
            "source_label": source_label(stored_source_key(r.source_type)),
            "external_id": r.external_id or "",
            "account_id": r.account_id,
            "account": (acc_map.get(r.account_id) or {}).get("name", ""),
        } for r in rows],
    }


@router.get("/sources")
def list_change_sources(
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    from app.services.change_source import change_sources_payload
    return change_sources_payload()


@router.post("/sync")
def trigger_sync(
    request: Request,
    days: int = Query(2, ge=1, le=30),
    account_id: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """手动拉取近 N 天变更（账号已配置变更源，只读）。"""
    from app.services.change_source import sync_changes
    r = sync_changes(db, days=days, account_id=account_id)
    audit.record(
        db, action="change.sync", user_id=user.id, username=user.username,
        resource_type="change", resource_id=str(account_id or ""), detail=r,
        client_ip=get_client_ip(request),
    )
    db.commit()
    return r
