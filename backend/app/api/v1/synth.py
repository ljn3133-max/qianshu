"""拨测/合成监控 API。

GET    /synth/checks            任务列表
POST   /synth/checks            创建任务
PUT    /synth/checks/{id}       更新（启停/参数）
DELETE /synth/checks/{id}       删除
POST   /synth/checks/{id}/run   手动执行一次
GET    /synth/results?check_id  结果历史
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import bearer, get_current_user, require_roles
from app.models.synth import SynthCheck, SynthResult
from app.models.user import User
from app.services.synth import run_check

logger = logging.getLogger("qianshu.synth_api")
router = APIRouter(prefix="/synth", tags=["synth"])


class CheckCreate(BaseModel):
    name: str = Field(max_length=128)
    check_type: str = Field(default="http", pattern="^(http|tcp|dns|browser)$")
    target: str = Field(max_length=512)
    interval_sec: int = Field(default=60, ge=15, le=86400)
    timeout_sec: int = Field(default=10, ge=1, le=60)
    expect_status: str = Field(default="200-399", max_length=64)
    cert_warn_days: int = Field(default=30, ge=1, le=365)
    accept_self_signed: bool = False   # 内网自签证书目标可显式跳过 TLS 校验
    enabled: bool = True


def _out(c: SynthCheck) -> dict:
    return {
        "id": c.id, "name": c.name, "check_type": c.check_type, "target": c.target,
        "interval_sec": c.interval_sec, "timeout_sec": c.timeout_sec, "enabled": c.enabled,
        "expect_status": c.expect_status, "cert_warn_days": c.cert_warn_days,
        "accept_self_signed": bool(getattr(c, "accept_self_signed", False)),   # 旧记录缺字段时默认不允许自签证书
        "last_run_at": c.last_run_at.isoformat(timespec="seconds") if c.last_run_at else None,
        "last_ok": c.last_ok, "last_latency_ms": c.last_latency_ms,
        "consecutive_fail": c.consecutive_fail,
    }


@router.get("/checks")
def list_checks(db: Session = Depends(get_db),
                user: User = Depends(require_roles("admin", "operator", "viewer"))):
    return [_out(c) for c in db.query(SynthCheck).order_by(SynthCheck.id).all()]


@router.post("/checks")
def create_check(body: CheckCreate, db: Session = Depends(get_db),
                 user: User = Depends(require_roles("admin", "operator"))):
    c = SynthCheck(**body.model_dump())
    db.add(c)
    db.commit()
    return _out(c)


@router.put("/checks/{check_id}")
def update_check(check_id: int, body: CheckCreate, db: Session = Depends(get_db),
                 user: User = Depends(require_roles("admin", "operator"))):
    c = db.get(SynthCheck, check_id)
    if c is None:
        raise HTTPException(404, "任务不存在")
    for k, v in body.model_dump().items():
        setattr(c, k, v)
    db.commit()
    return _out(c)


@router.delete("/checks/{check_id}")
def delete_check(check_id: int, db: Session = Depends(get_db),
                 user: User = Depends(require_roles("admin"))):
    c = db.get(SynthCheck, check_id)
    if c is None:
        raise HTTPException(404, "任务不存在")
    db.query(SynthResult).filter(SynthResult.check_id == check_id).delete()
    db.delete(c)
    db.commit()
    return {"ok": True}


@router.post("/checks/{check_id}/run")
def run_one(check_id: int, db: Session = Depends(get_db),
            user: User = Depends(require_roles("admin", "operator"))):
    c = db.get(SynthCheck, check_id)
    if c is None:
        raise HTTPException(404, "任务不存在")
    r = run_check(db, c)
    return {"ok": r.ok, "latency_ms": r.latency_ms, "status_code": r.status_code,
            "detail": r.detail, "checked_at": r.checked_at.isoformat(timespec="seconds")}


@router.get("/results")
def list_results(check_id: int = Query(...), limit: int = Query(50, ge=1, le=500),
                 db: Session = Depends(get_db),
                 user: User = Depends(require_roles("admin", "operator", "viewer"))):
    rows = (db.query(SynthResult).filter(SynthResult.check_id == check_id)
            .order_by(SynthResult.checked_at.desc()).limit(limit).all())
    import json as _json

    out = []
    for r in rows:
        try:
            timing = _json.loads(r.timing) if r.timing else {}
        except Exception:
            timing = {}
        out.append({
            "id": r.id, "checked_at": r.checked_at.isoformat(timespec="seconds"),
            "ok": r.ok, "latency_ms": r.latency_ms, "status_code": r.status_code,
            "detail": r.detail, "timing": timing,
        })
    return out


def _screenshot_auth(
    request: Request,
    token: str = Query(""),
    cred: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    """截图鉴权：Authorization 头 或 ?token= 二选一（img 标签只能走 query）。"""
    if token:
        from app.core.security import decode_access_token

        try:
            payload = decode_access_token(token)
            user = db.get(User, int(payload["sub"]))
            if user is not None and user.is_active:
                return user
        except Exception:
            pass
        raise HTTPException(401, "token 无效")
    if cred is None:
        raise HTTPException(401, "需要登录（?token= 或 Authorization 头）")
    return get_current_user(cred, db)


@router.get("/screenshots/{fname}")
def synth_screenshot(
    fname: str,
    token: str = Query("", description="img 标签无法带 Authorization 头，用 ?token= 鉴权"),
    user: User = Depends(_screenshot_auth),
):
    """渲染级探测截图。鉴权：Bearer 头 或 ?token=（img 标签路径）。"""
    import os

    from fastapi.responses import FileResponse

    if "/" in fname or ".." in fname:
        raise HTTPException(400, "非法文件名")
    path = os.path.join(os.getcwd(), "data", "screenshots", fname)
    if not os.path.exists(path):
        raise HTTPException(404, "截图不存在")
    return FileResponse(path, media_type="image/png")
