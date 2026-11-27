"""监控与提醒策略 API：模块开关、账号优先级、提醒去向与重申间隔。

两层策略的唯一配置入口：

- 模块提醒开关：`monitor_module_settings`（这个模块的事能不能进总览/站内信/外推）；
- 数据源策略：`cloud_accounts.priority` + 三个去向 + `repeat_days`。

最终去向 = 模块开关 AND 账号开关；模块开关只停提醒，不停采集。
账号增删仍走 /clouds；这里只改策略列，不触发重新同步。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.client_ip import get_client_ip
from app.core.config import settings
from app.core.db import get_db
from app.core.deps import require_roles
from app.models.cloud_account import CloudAccount
from app.models.monitor_policy import MonitorModuleSetting
from app.models.user import User
from app.services import audit
from app.services import monitor_policy as policy

logger = logging.getLogger("qianshu.policy")
router = APIRouter(prefix="/policy", tags=["policy"])


class ModuleUpdate(BaseModel):
    module: str = Field(min_length=1, max_length=32)
    enabled: bool = True
    overview: bool = True
    inapp: bool = True
    push: bool = True


class ModuleUpdateIn(BaseModel):
    modules: list[ModuleUpdate] = Field(min_length=1, max_length=50)


class AccountPolicyIn(BaseModel):
    priority: str | None = Field(default=None, pattern="^(high|medium|low)$")
    notify_overview: bool | None = None
    notify_inapp: bool | None = None
    notify_push: bool | None = None
    repeat_days: int | None = Field(default=None, ge=0, le=3650)


class BulkPriorityIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=200)
    priority: str = Field(pattern="^(high|medium|low)$")


@router.get("/overview")
def overview(
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """一屏渲染：模块矩阵 + 数据源策略 + 三档默认值。"""
    accounts = (db.query(CloudAccount)
                .order_by(CloudAccount.enabled.desc(), CloudAccount.id).all())
    return {
        "policy_enabled": bool(settings.MONITOR_POLICY_ENABLED),
        "modules": policy.modules_payload(db),
        "accounts": [policy.account_payload(a, db) for a in accounts],
        "priorities": policy.priority_defaults(),
        "destinations": [
            {"key": k, "label": policy.DEST_LABELS[k]}
            for k in policy.DESTINATIONS
        ],
    }


@router.put("/modules")
def update_modules(
    body: ModuleUpdateIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    """批量保存模块提醒矩阵。不支持的去向（如到期提醒的站内信）忽略不写。"""
    policy.ensure_defaults(db)
    changed = []
    for item in body.modules:
        spec = policy.MODULES.get(item.module)
        if spec is None:
            raise HTTPException(400, f"未知监控模块: {item.module}")
        row = db.get(MonitorModuleSetting, item.module)
        if row is None:
            row = MonitorModuleSetting(module=item.module)
            db.add(row)
        row.enabled = item.enabled
        for dest in policy.DESTINATIONS:
            if dest in spec.destinations:
                setattr(row, dest, getattr(item, dest))
        changed.append({"module": item.module, "enabled": item.enabled,
                        "overview": row.overview, "inapp": row.inapp,
                        "push": row.push})
    audit.record(
        db, action="policy.module.update", user_id=user.id, username=user.username,
        resource_type="monitor_policy", resource_id="modules",
        detail={"changed": changed}, client_ip=get_client_ip(request),
    )
    db.commit()
    return {"ok": True, "modules": policy.modules_payload(db)}


def _apply_account(row: CloudAccount, body: AccountPolicyIn) -> None:
    if body.priority:
        row.priority = body.priority
    for dest in policy.DESTINATIONS:
        val = getattr(body, f"notify_{dest}")
        if val is not None:
            setattr(row, f"notify_{dest}", val)
    if "repeat_days" in body.model_fields_set:
        row.repeat_days = body.repeat_days     # 显式 null=继承优先级


@router.put("/accounts/{aid}")
def update_account_policy(
    aid: int,
    body: AccountPolicyIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    row = db.get(CloudAccount, aid)
    if row is None:
        raise HTTPException(404, "云账号不存在")
    _apply_account(row, body)
    audit.record(
        db, action="policy.account.update", user_id=user.id, username=user.username,
        resource_type="cloud_account", resource_id=str(aid),
        detail={"account": row.name, **body.model_dump()},
        client_ip=get_client_ip(request),
    )
    db.commit()
    db.refresh(row)
    return policy.account_payload(row, db)


@router.post("/accounts/bulk")
def bulk_priority(
    body: BulkPriorityIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    """批量设优先级（设置页「批量设为中/低」；不改去向/重申间隔）。"""
    rows = db.query(CloudAccount).filter(CloudAccount.id.in_(body.ids)).all()
    if len(rows) != len(set(body.ids)):
        raise HTTPException(404, "部分云账号不存在")
    names = []
    for row in rows:
        row.priority = body.priority
        names.append(row.name)
    audit.record(
        db, action="policy.account.update", user_id=user.id, username=user.username,
        resource_type="cloud_account", resource_id="bulk",
        detail={"ids": body.ids, "priority": body.priority, "accounts": names},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return {"ok": True, "updated": len(rows)}
