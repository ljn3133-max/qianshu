"""域名登记 API：统一关联域名、证书、SLB/CDN 与业务。

  GET    /domains                   登记清单（证书到期排序/筛选，带关联 SLB/CDN/业务/归属）
  GET    /domains/{domain}/detail   聚合六处证据（链路/CDN/画像/拨测/诊断/业务线+证书+注册资产）
  PUT    /domains/{domain}          人工补业务/厂商/账号/备注/停用（审计；不提供删除）
  POST   /domains/backfill          手动触发六源回填（幂等；日常由域名/证书采集循环顺带跑）

字段语义（与归属登记同构）：PUT 传了的字段=人工固定（采集不覆盖）；
清空业务/厂商/账号=取消固定、回到自动派生（归属登记/业务线登记/采集账号事实）。
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.client_ip import get_client_ip
from app.core.db import get_db
from app.core.deps import require_roles
from app.models.domain_registry import DomainRegistry
from app.models.user import User
from app.services import audit
from app.services.domain_registry import backfill, detail, norm_domain, registry_list

logger = logging.getLogger("qianshu.domains_api")
router = APIRouter(prefix="/domains", tags=["domains"])

_STATUS = ("active", "disabled")


@router.get("")
def list_domains(
    q: str = Query("", description="域名/业务/备注模糊过滤"),
    status: str = Query("", description="active / disabled；空=全部"),
    level: str = Query("", description="证书到期分级 expired/critical/warning/ok/none"),
    source: str = Query("", description="来源标记过滤（link/cdn/profile/synth/diagnosis/biz/registrar/cert）"),
    refresh: int = Query(0, description="1=先回填六源再列（仅 admin/operator；viewer 只读）"),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """域名登记清单（默认只读）。默认按证书到期升序（无证书沉底）。

    GET 默认 refresh=0，viewer 也不能触发六源 upsert，保证只读角色打开页面没有写副作用；
    回填由每日任务或写角色的「刷新」按钮（POST /domains/backfill）负责。
    """
    if status and status not in _STATUS:
        raise HTTPException(400, "status 必须是 active / disabled")
    if refresh and user.role in ("admin", "operator"):
        backfill(db)
    return registry_list(db, q=q, status=status, level=level, source=source)


@router.post("/backfill")
def run_backfill(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """手动触发六源回填（幂等 upsert；自动字段=事实，登记字段不被覆盖）。"""
    stats = backfill(db)
    audit.record(
        db, action="domain.backfill", user_id=user.id, username=user.username,
        resource_type="domain_registry", resource_id="", detail=stats,
        client_ip=get_client_ip(request),
    )
    db.commit()
    return stats


@router.get("/{domain}/detail")
def domain_detail(
    domain: str,
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """域名详情：聚合六处证据。域名按 小写/去端口/去 scheme 归一。"""
    try:
        data = detail(db, domain)
    except ValueError:
        raise HTTPException(400, "请填写合法域名，如 example.com")
    if data is None:
        raise HTTPException(404, f"未收录进域名登记（无任何来源证据）: {domain}")
    return data


class DomainUpdateIn(BaseModel):
    business: Optional[str] = Field(None, max_length=128)
    vendor: Optional[str] = Field(None, max_length=32)
    account_name: Optional[str] = Field(None, max_length=64)
    note: Optional[str] = Field(None, max_length=500)
    status: Optional[str] = None


@router.put("/{domain}")
def update_domain(
    domain: str,
    body: DomainUpdateIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """人工登记字段（精确改：传了的字段才动）。

    - 业务/厂商/账号：非空=人工固定（采集不覆盖）；清空=取消固定回到自动派生
    - note：纯人工备注，清空即删备注
    - status：disabled=停用（不提供删除——证据行保留可追溯）
    """
    d = norm_domain(domain)
    if not d:
        raise HTTPException(400, "请填写合法域名，如 example.com")
    row = db.get(DomainRegistry, d)
    if row is None:
        raise HTTPException(404, f"未收录进域名登记: {d}")
    if body.status is not None and body.status not in _STATUS:
        raise HTTPException(400, "status 必须是 active / disabled")

    manual = {f for f in (row.manual_fields or "").split(",") if f}
    changed: dict = {}
    for f in ("business", "vendor", "account_name"):
        v = getattr(body, f)
        if v is None:
            continue
        v = v.strip()
        if v:
            setattr(row, f, v)
            manual.add(f)
        else:
            manual.discard(f)      # 清空=取消固定，下方回填立即回到派生值
        changed[f] = v
    if body.note is not None:
        row.note = body.note.strip()
        changed["note"] = row.note
    if body.status is not None:
        row.status = body.status
        changed["status"] = row.status
    if not changed:
        raise HTTPException(400, "没有要修改的字段")
    row.manual_fields = ",".join(sorted(manual))
    backfill(db, only_domain=d)   # 清空的字段立即回填派生值（人工固定项不被覆盖）
    audit.record(
        db, action="domain.update", user_id=user.id, username=user.username,
        resource_type="domain_registry", resource_id=d,
        detail={"changed": changed, "manual_fields": row.manual_fields},
        client_ip=get_client_ip(request),
    )
    db.commit()
    db.refresh(row)
    return {
        "domain": row.domain, "status": row.status, "business": row.business,
        "vendor": row.vendor, "account_name": row.account_name, "note": row.note,
        "manual_fields": [f for f in (row.manual_fields or "").split(",") if f],
    }
