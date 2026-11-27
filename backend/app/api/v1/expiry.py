"""到期总账 API：GET /expiry/items 合并资产/资源包到期时间。

外部注册域名到期支持人工登记：本账号 API 查不到的域名登记进
assets(status=Manual)，到期账照常预警；域名同步清扫会跳过 Manual 行。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.client_ip import get_client_ip
from app.core.db import get_db
from app.core.deps import require_roles
from app.models.asset import Asset
from app.models.user import User
from app.services import audit
from app.services.expiry import expiry_items

router = APIRouter(prefix="/expiry", tags=["expiry"])

_LEVELS = ("expired", "critical", "warning", "ok")


class ExternalDomainIn(BaseModel):
    domain: str
    expire_time: str          # YYYY-MM-DD
    note: str = ""


@router.post("/domains")
def register_external_domain(
    body: ExternalDomainIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """登记外部注册域名的到期时间（人工数据面）。"""
    domain = body.domain.strip().lower()
    if not domain or "." not in domain:
        raise HTTPException(400, "请填写合法域名，如 example.com")
    try:
        expire = datetime.strptime(body.expire_time.strip(), "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "到期时间格式：YYYY-MM-DD")
    row = db.query(Asset).filter(
        Asset.product == "domain", Asset.instance_id == domain).first()
    if row is None:
        row = Asset(product="domain", instance_id=domain, name=domain, status="Manual")
        db.add(row)
    row.name = domain
    row.status = "Manual"   # 人工登记行，域名同步不清扫
    row.expire_time = expire
    row.extra = body.note[:500] if body.note else row.extra
    audit.record(
        db, action="expiry.domain_register", user_id=user.id, username=user.username,
        resource_type="domain", resource_id=domain,
        detail={"expire_time": body.expire_time, "note": body.note},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return {"domain": domain, "expire_time": body.expire_time}


@router.delete("/domains/{domain}")
def remove_external_domain(
    domain: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """删除人工登记的外部域名（只允许 Manual 行，云同步资产删不了）。"""
    row = db.query(Asset).filter(
        Asset.product == "domain", Asset.instance_id == domain.strip().lower()).first()
    if row is None or row.status != "Manual":
        raise HTTPException(404, f"不是人工登记的域名: {domain}")
    audit.record(
        db, action="expiry.domain_remove", user_id=user.id, username=user.username,
        resource_type="domain", resource_id=row.instance_id, detail={},
        client_ip=get_client_ip(request),
    )
    db.delete(row)
    db.commit()
    return {"removed": row.instance_id}


@router.get("/items")
def list_expiry_items(
    include_ignored: int = 0,
    category: str = Query(""),
    level: str = Query(""),
    account_id: int = Query(0, ge=0, description="云账号 id，0=全部（跟随顶部账号切换）"),
    include_disabled: int = Query(0, description="1=连停用账号的旧资产一起看（默认不进账）"),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """到期总账（只读聚合）。category 传产品码（ecs/rds/domain/ssl_cert/pack…）；
    include_ignored=1 显示已忽略项（含 ignored 标记），默认排除。"""
    data = expiry_items(db, include_ignored=bool(include_ignored),
                        account_id=account_id, include_disabled=bool(include_disabled))
    items = data["items"]
    if category:
        items = [i for i in items if i["category"] == category]
    if level:
        if level not in _LEVELS:
            level = ""  # 非法级别当没传，不报错（前端下拉可控）
        items = [i for i in items if i["level"] == level]
    return {
        "date": data["date"],
        "summary": data["summary"],   # 含已忽略的全量汇总
        # 顶部数字与默认列表同口径，只统计未忽略项；visible/ignored 分开返回，
        # 避免汇总有可点击数量而列表为空。
        "visible_summary": data.get("visible_summary", data["summary"]),
        "ignored_summary": data.get("ignored_summary"),
        "ignored_count": data.get("ignored_count", 0),
        "total": len(items),
        "items": items,
        # 过期超过 30 天的历史证书不进账，只回条数（前端在统计行下方提示已排除多少）
        "stale_omitted": data.get("stale_omitted", {}),
    }


class IgnoreBody(BaseModel):
    target_key: str = Field(max_length=191)
    note: str = Field(default="", max_length=500)
    days: int = Field(default=0, ge=0, le=3650)   # 0=永久；N=忽略 N 天后自动恢复


@router.post("/ignores")
def ignore_item(body: IgnoreBody, request: Request, db: Session = Depends(get_db),
                user: User = Depends(require_roles("admin", "operator"))):
    """忽略某到期项的提醒（默认列表/决策清单/诊断报告都跳过）。

    days>0 时限时忽略，到期后自动恢复提醒。
    """
    from datetime import timedelta

    from app.models.expiry_ignore import ExpiryIgnore

    until = datetime.now() + timedelta(days=body.days) if body.days else None
    row = db.query(ExpiryIgnore).filter(ExpiryIgnore.target_key == body.target_key).first()
    if row is None:
        row = ExpiryIgnore(target_key=body.target_key, note=body.note,
                           created_by=user.username,
                           created_by_id=user.id)   # created_by_id 引用 users.id
        db.add(row)
    else:
        row.note = body.note
        row.created_by = user.username
        row.created_by_id = user.id
        row.created_at = datetime.now()
    row.until_at = until
    audit.record(
        db, action="expiry.ignore.update", user_id=user.id, username=user.username,
        resource_type="expiry_ignore", resource_id=body.target_key,
        detail={"ignore": True, "days": body.days, "note": body.note},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return {"ok": True, "target_key": body.target_key,
            "until_at": until.isoformat() if until else ""}


@router.delete("/ignores/{target_key:path}")
def unignore_item(target_key: str, request: Request, db: Session = Depends(get_db),
                  user: User = Depends(require_roles("admin", "operator"))):
    """恢复提醒（取消忽略）。"""
    from app.models.expiry_ignore import ExpiryIgnore

    db.query(ExpiryIgnore).filter(ExpiryIgnore.target_key == target_key).delete()
    audit.record(
        db, action="expiry.ignore.update", user_id=user.id, username=user.username,
        resource_type="expiry_ignore", resource_id=target_key,
        detail={"ignore": False}, client_ip=get_client_ip(request),
    )
    db.commit()
    return {"ok": True}
