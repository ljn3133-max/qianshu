"""归属配置 API：实体 → 厂商/账号/业务 人工登记。

配置入口：前端系统设置-归属配置 / 拓扑详情「去归属」直达；自动化走本 API。

  GET    /ownership/rules            登记列表（?key_type= 过滤）
  POST   /ownership/rules            新建登记（(key_type,key_value) 唯一）
  PUT    /ownership/rules/{rid}      编辑登记
  DELETE /ownership/rules/{rid}      删除登记
  GET    /ownership/preview          试算：键 → 命中登记 + 识别线索（只读，不落库）
  GET    /ownership/unattributed     未归属清单（仍显示"未归属"的实体 + 建议登记键）
  GET    /ownership/options          选项（键类型/厂商码），产品化：换环境不改代码
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.client_ip import get_client_ip
from app.core.db import get_db
from app.core.deps import require_roles
from app.models.user import User
from app.services.ownership import (
    KEY_TYPES,
    KEY_TYPE_LABELS,
    hints,
    load_rules,
    norm_key,
    resolve,
    unattributed,
)

logger = logging.getLogger("qianshu.ownership_api")
router = APIRouter(prefix="/ownership", tags=["ownership"])


class OwnershipRuleIn(BaseModel):
    key_type: str = Field(..., description="ip / domain / instance / name")
    key_value: str = Field(..., min_length=1, max_length=255)
    match_type: str = Field("exact", description="exact / pattern（fnmatch 通配）")
    vendor: str = ""
    account_name: str = ""
    account_id: int = 0
    business: str = ""
    note: str = ""


def _out(r) -> dict:
    return {
        "id": r.id, "key_type": r.key_type, "key_value": r.key_value,
        "match_type": r.match_type, "vendor": r.vendor, "account_name": r.account_name,
        "account_id": r.account_id, "business": r.business, "note": r.note,
        "created_by": r.created_by,
        "created_at": r.created_at.isoformat(timespec="seconds") if r.created_at else None,
        "updated_at": r.updated_at.isoformat(timespec="seconds") if r.updated_at else None,
    }


def _validate(body: OwnershipRuleIn) -> tuple:
    if body.key_type not in KEY_TYPES:
        raise HTTPException(400, f"key_type 必须是 {KEY_TYPES}")
    if (body.match_type or "exact") not in ("exact", "pattern"):
        raise HTTPException(400, "match_type 必须是 exact / pattern")
    value = norm_key(body.key_type, body.key_value)
    if not value:
        raise HTTPException(400, "key_value 不能为空")
    return value, (body.match_type or "exact")


@router.get("/rules")
def list_rules(
    key_type: str = Query("", description="按键类型过滤"),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    rows = load_rules(db)
    if key_type:
        rows = [r for r in rows if r.key_type == key_type]
    return [_out(r) for r in rows]


@router.post("/rules")
def create_rule(
    body: OwnershipRuleIn, request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    from app.models.ownership import OwnershipRule
    from app.services import audit as audit_svc

    value, match_type = _validate(body)
    exists = (db.query(OwnershipRule)
              .filter(OwnershipRule.key_type == body.key_type,
                      OwnershipRule.key_value == value).first())
    if exists is not None:
        raise HTTPException(400, "该键已登记过，请编辑原记录")
    row = OwnershipRule(
        key_type=body.key_type, key_value=value, match_type=match_type,
        vendor=body.vendor or "", account_name=body.account_name or "",
        account_id=body.account_id or 0, business=body.business or "",
        note=body.note or "", created_by=user.username,
        created_by_id=user.id,   # created_by_id 引用 users.id
    )
    db.add(row)
    audit_svc.record(
        db, action="ownership.create", user_id=user.id, username=user.username,
        resource_type="ownership", resource_id=f"{row.key_type}:{row.key_value}",
        detail={"vendor": row.vendor, "account_name": row.account_name,
                "account_id": row.account_id, "business": row.business,
                "match_type": row.match_type},
        client_ip=get_client_ip(request),
    )
    db.commit()
    db.refresh(row)
    return _out(row)


@router.put("/rules/{rid}")
def update_rule(
    rid: int, body: OwnershipRuleIn, request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    from app.models.ownership import OwnershipRule
    from app.services import audit as audit_svc

    row = db.get(OwnershipRule, rid)
    if row is None:
        raise HTTPException(404, "登记不存在")
    value, match_type = _validate(body)
    dup = (db.query(OwnershipRule)
           .filter(OwnershipRule.key_type == body.key_type,
                   OwnershipRule.key_value == value,
                   OwnershipRule.id != rid).first())
    if dup is not None:
        raise HTTPException(400, "该键已被其他记录登记")
    row.key_type = body.key_type
    row.key_value = value
    row.match_type = match_type
    row.vendor = body.vendor or ""
    row.account_name = body.account_name or ""
    row.account_id = body.account_id or 0
    row.business = body.business or ""
    row.note = body.note or ""
    audit_svc.record(
        db, action="ownership.update", user_id=user.id, username=user.username,
        resource_type="ownership", resource_id=f"{row.key_type}:{row.key_value}",
        detail={"vendor": row.vendor, "account_name": row.account_name,
                "account_id": row.account_id, "business": row.business},
        client_ip=get_client_ip(request),
    )
    db.commit()
    db.refresh(row)
    return _out(row)


@router.delete("/rules/{rid}")
def delete_rule(
    rid: int, request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    from app.models.ownership import OwnershipRule
    from app.services import audit as audit_svc

    row = db.get(OwnershipRule, rid)
    if row is None:
        raise HTTPException(404, "登记不存在")
    audit_svc.record(
        db, action="ownership.delete", user_id=user.id, username=user.username,
        resource_type="ownership", resource_id=f"{row.key_type}:{row.key_value}",
        detail={"vendor": row.vendor, "account_name": row.account_name},
        client_ip=get_client_ip(request),
    )
    db.delete(row)
    db.commit()
    return {"ok": True}


@router.get("/preview")
def preview(
    key_type: str = Query(...),
    key_value: str = Query(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """试算：该键会命中哪条登记 + 识别线索（"它实际上是谁"，人工确认前不落库）。"""
    if key_type not in KEY_TYPES:
        raise HTTPException(400, f"key_type 必须是 {KEY_TYPES}")
    value = norm_key(key_type, key_value)
    hit = resolve(load_rules(db), **{
        {"ip": "ips"}.get(key_type, key_type): value})
    return {
        "key_type": key_type, "key_value": value,
        "hit": hit, "hints": hints(db, key_type, value),
    }


@router.get("/unattributed")
def unattributed_list(
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """仍显示"未归属"的实体清单（配置后自动消失=回流生效）。"""
    return unattributed(db)


@router.get("/options")
def options(user: User = Depends(require_roles("admin", "operator", "viewer"))):
    """登记选项（键类型/厂商码）——厂商表来自 Provider 注册表，换环境加厂商不改代码。"""
    vendors = []
    try:
        from app.services.cloud.provider import PROVIDERS, vendor_label

        vendors = [{"code": k, "label": vendor_label(k)} for k in sorted(PROVIDERS)]
    except Exception:   # pragma: no cover - Provider 导入异常时给基础集
        vendors = [{"code": v, "label": v} for v in
                   ("aliyun", "tencent", "volc", "baidu", "huawei", "aws", "azure", "onprem")]
    if not any(v["code"] == "onprem" for v in vendors):
        vendors.append({"code": "onprem", "label": "自建/IDC"})
    return {
        "key_types": [{"code": k, "label": KEY_TYPE_LABELS[k]} for k in KEY_TYPES],
        "match_types": [{"code": "exact", "label": "精确"},
                        {"code": "pattern", "label": "通配（如 *.example.com / 10.0.*）"}],
        "vendors": vendors,
    }
