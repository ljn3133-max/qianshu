"""行为画像 API：构成快照/正常基线/漂移事件/人工确认。

云监控看数值，这里看构成变化并记住"正常是什么样"；支持「知道了」+分级静默
（known 状态机）以及关联事件/变更时间线。
"""
from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.core.client_ip import get_client_ip
from app.core.db import get_db
from app.core.deps import require_roles
from app.models.behavior_profile import BehaviorProfile
from app.models.cost_anomaly import CostAnomaly
from app.models.user import User
from app.services import audit
from app.services.profile_engine import (
    accept_profile, know_profile, related_events_timeline, run_profiles, tier_of_profile,
)

router = APIRouter(prefix="/profiles", tags=["profiles"])

# 展示文案与前端 i18n 统一为「已确认」，避免同一状态在不同画像页面使用不同叫法。
_STATUS_LABELS = {"observing": "观察中", "normal": "正常", "known": "已确认", "drift": "漂移",
                  "context_stable": "构成变化·上下文稳定", "unverified": "变化待核实"}


def _out(db: Session, p: BehaviorProfile, with_detail: bool = False) -> dict:
    base = json.loads(p.baseline or "{}")
    latest = json.loads(p.latest or "{}")
    silence_until = p.silence_until
    silenced = bool(silence_until and silence_until > datetime.now())
    out = {
        "profile_key": p.profile_key, "kind": p.kind, "subject": p.subject,
        "label": p.label, "learned": p.learned, "status": p.status,
        "status_label": _STATUS_LABELS.get(p.status, p.status),
        "drift_note": p.drift_note,
        "top1": (latest.get("members") or [{}])[0] if latest.get("members") else None,
        "members": latest.get("members") or [],   # 现在的 top 构成（卡片逐条展示）
        "baseline_top1": base.get("top1"),
        "n_samples": base.get("n", len(json.loads(p.samples or "[]"))),
        "confirmed_by": p.confirmed_by,
        "confirmed_at": p.confirmed_at.isoformat() if p.confirmed_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        # ---- 已确认与分级静默 ----
        "tier": tier_of_profile(db, p),
        "known_at": p.known_at.isoformat() if p.known_at else None,
        "silence_until": silence_until.isoformat() if silence_until else None,
        "silence_active": silenced,
        "silence_remaining_min": (
            int((silence_until - datetime.now()).total_seconds() // 60) if silenced else 0),
        "notify_state": p.notify_state or "",
    }
    if with_detail:
        out["samples"] = json.loads(p.samples or "[]")
        out["baseline"] = base
        out["latest"] = latest
        out["event_samples"] = json.loads(p.event_samples or "[]")
    return out


@router.get("")
def list_profiles(
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    rows = db.query(BehaviorProfile).order_by(BehaviorProfile.status.desc(), BehaviorProfile.id).all()
    return {"total": len(rows), "items": [_out(db, p) for p in rows]}


@router.get("/detail")
def profile_detail(
    profile_key: str = Query(min_length=1),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    p = db.query(BehaviorProfile).filter(BehaviorProfile.profile_key == profile_key).first()
    if p is None:
        raise HTTPException(404, f"画像不存在: {profile_key}")
    events = (db.query(CostAnomaly)
              .filter(CostAnomaly.type == "profile_drift",
                      CostAnomaly.target_key.in_((profile_key, f"{profile_key}#fast")))
              .order_by(CostAnomaly.id.desc()).limit(10).all())
    out = _out(db, p, with_detail=True)
    out["events"] = [{
        "id": e.id, "title": e.title, "content": e.content, "level": e.level,
        "ack_status": e.ack_status, "detect_date": e.detect_date.isoformat(),
        "last_seen": e.last_seen.isoformat() if e.last_seen else None,
    } for e in events]
    # 关联事件时间线：该对象近 7 天的告警/异动/变更（时间/系统/操作/人）
    out["related_events"] = related_events_timeline(db, [p.profile_key, p.subject], days=7)
    return out


@router.post("/accept")
def accept(
    request: Request,
    profile_key: str = Query(min_length=1),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """接受当前构成为正常业务（重学基线并关闭漂移事件）。"""
    try:
        r = accept_profile(db, profile_key, user.username, by_id=user.id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit.record(
        db, action="profile.accept", user_id=user.id, username=user.username,
        resource_type="profile", resource_id=profile_key, detail=r,
        client_ip=get_client_ip(request),
    )
    db.commit()
    return r


@router.post("/know")
def know(
    request: Request,
    profile_key: str = Query(min_length=1),
    silence_3d: bool = Query(False, description="aux 辅助机器：勾选才静默 3 天（core 固定静默 1 天）"),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """「知道了」：确认知晓当前变化（不改基线），按关键度静默——
    core 静默 1 天后恢复提醒（漂移仍在）；aux 勾选"静默 3 天"才生效，否则持续通知。"""
    try:
        r = know_profile(db, profile_key, user.username, silence_3d=silence_3d)
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit.record(
        db, action="profile.know", user_id=user.id, username=user.username,
        resource_type="profile", resource_id=profile_key, detail=r,
        client_ip=get_client_ip(request),
    )
    db.commit()
    return r


@router.post("/run")
def trigger_run(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """手动跑一轮画像采样+漂移检测。"""
    r = run_profiles(db)
    audit.record(
        db, action="profile.run", user_id=user.id, username=user.username,
        resource_type="profile", resource_id="", detail=r,
        client_ip=get_client_ip(request),
    )
    db.commit()
    return r


# ---------------- 对象全景画像 ----------------

@router.get("/objects")
def list_objects(
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """全景对象清单（对象×多层×联动状态）。开关关闭时如实告知。"""
    from app.core.config import settings
    from app.models.object_profile import ObjectProfile
    if not settings.OBJECT_PROFILE_V2:
        return {"enabled": False, "items": [], "note": "对象全景画像未启用（OBJECT_PROFILE_V2=false）"}
    rows = db.query(ObjectProfile).order_by(
        ObjectProfile.object_type, ObjectProfile.object_key).all()
    now = datetime.now()
    return {
        "enabled": True,
        "items": [{
            "object_type": p.object_type, "object_key": p.object_key, "label": p.label,
            "status": p.status, "learned": p.learned, "tier": p.tier or "core",
            "known_at": p.known_at.isoformat() if p.known_at else None,
            "silence_until": p.silence_until.isoformat() if p.silence_until else None,
            "silence_active": bool(p.silence_until and p.silence_until > now),
            "silence_remaining_min": (
                int((p.silence_until - now).total_seconds() // 60)
                if p.silence_until and p.silence_until > now else 0),
            "notify_state": p.notify_state or "",
            "baseline_layers": sorted({k for by_kind in ((p.baseline or {}).get("layers_by_kind") or {}).values() for k in by_kind}),
            "couplings": sorted({k for by_kind in ((p.baseline or {}).get("couplings_by_kind") or {}).values() for k in by_kind}),
            "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        } for p in rows],
    }


@router.get("/objects/chain")
def object_chain_api(
    object: str = Query(..., max_length=255, description="域名或实例ID/名称"),
    days: int = Query(7, ge=1, le=30),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """对象全景时间轴：近 N 天各层同窗快照 + 基线 + 联动变化事件（因果链）。"""
    from app.services.object_profile import build_chain
    out = build_chain(db, object, days)
    if "error" in out:
        raise HTTPException(404, out["error"])
    return out


@router.post("/objects/know")
def know_object_api(
    request: Request,
    object: str = Query(..., max_length=255),
    silence_3d: bool = Query(False, description="aux 辅助对象：勾选才静默 3 天（core 固定静默 1 天）"),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """对象全景「知道了」：确认知晓联动失衡，按对象关键度静默。"""
    from app.services.object_profile import know_object
    try:
        r = know_object(db, object, user.username, silence_3d=silence_3d)
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit.record(
        db, action="profile.object_know", user_id=user.id, username=user.username,
        resource_type="object_profile", resource_id=object, detail=r,
        client_ip=get_client_ip(request),
    )
    db.commit()
    return r


@router.post("/objects/run")
def run_objects(
    request: Request,
    backfill_days: int = Query(0, ge=0, le=30, description="顺带回填近 N 天（学基线用）"),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """手动跑一轮全景采集+学习+检测（每日 11:25 自动跑；调试/补数用）。"""
    from app.services.object_profile import run_object_profiles
    r = run_object_profiles(db, backfill_days=backfill_days)
    audit.record(
        db, action="profile.object_run", user_id=user.id, username=user.username,
        resource_type="object_profile", resource_id="", detail=r,
        client_ip=get_client_ip(request),
    )
    db.commit()
    return r
