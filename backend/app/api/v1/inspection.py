"""巡检 API：手动巡检/运行历史/事实清单。"""
from __future__ import annotations

import logging

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.core.client_ip import get_client_ip
from app.core.db import get_db
from app.core.deps import require_roles
from app.core.i18n_msg import lang_of
from app.models.inspection import InspectionItem, InspectionRun
from app.models.inspection_report import InspectionReport, PushSetting
from app.models.user import User
from app.services import audit
from app.services.biz_map import resolve_biz
from app.services.inspection import get_status, run_inspection
from app.services.llm import LLMError
from app.services.report import generate_report, get_status as report_status

logger = logging.getLogger("qianshu.inspection")
router = APIRouter(prefix="/inspections", tags=["inspections"])


def _run_out(r: InspectionRun) -> dict:
    return {
        "id": r.id,
        "run_date": r.run_date.isoformat(),
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "status": r.status,
        "item_count": r.item_count,
        "error": r.error,
    }


def _item_out(i: InspectionItem, db) -> dict:
    return {
        "id": i.id,
        "category": i.category,
        "level": i.level,
        "target_key": i.target_key,
        "target_name": i.target_name,
        "title": i.title,
        "detail": i.detail,
        "value": i.value,
        "unit": i.unit,
        "biz": resolve_biz(db, i.target_key, i.target_name, f"{i.title} {(i.detail or '')[:200]}"),
    }


@router.get("/status")
def inspection_status(
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """巡检流水线状态（进程内 + 库内最近一次运行）。"""
    status = get_status()
    latest = db.query(InspectionRun).order_by(InspectionRun.id.desc()).first()
    return {
        "running": status.get("running", False),
        "last_result": status.get("last_result"),
        "latest_run": _run_out(latest) if latest else None,
        "total_runs": db.query(func.count(InspectionRun.id)).scalar(),
    }


@router.post("/run")
def trigger_inspection(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """手动触发一轮巡检（同步执行，约 10~30s）。"""
    result = run_inspection(db)
    if result.get("busy"):
        raise HTTPException(409, "巡检正在执行中，请稍候")
    audit.record(
        db, action="inspection.run", user_id=user.id, username=user.username,
        resource_type="inspection", resource_id=str(result["run_id"]),
        detail={"items": result["item_count"], "scanners": result["scanners"]},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return result


@router.get("/runs")
def list_runs(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    q = db.query(InspectionRun)
    total = q.count()
    rows = q.order_by(InspectionRun.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "items": [_run_out(r) for r in rows]}


@router.get("/runs/{run_id}")
def run_detail(
    run_id: int,
    category: str = Query("", max_length=16),
    level: str = Query("", max_length=16),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """单次巡检的事实清单（可按 category/level 过滤）。"""
    run = db.get(InspectionRun, run_id)
    if run is None:
        raise HTTPException(404, "巡检记录不存在")
    q = db.query(InspectionItem).filter(InspectionItem.run_id == run_id)
    if category:
        q = q.filter(InspectionItem.category == category)
    if level:
        q = q.filter(InspectionItem.level == level)
    items = q.order_by(
        # critical > warning > info，同类按 id
        case((InspectionItem.level == "critical", 0), (InspectionItem.level == "warning", 1), else_=2),
        InspectionItem.id,
    ).all()
    # 各类目计数（不受过滤影响，用于页签徽标）
    counts = dict(
        db.query(InspectionItem.category, func.count(InspectionItem.id))
        .filter(InspectionItem.run_id == run_id)
        .group_by(InspectionItem.category).all()
    )
    return {"run": _run_out(run), "counts": counts, "items": [_item_out(i, db) for i in items]}


# ---------------- 巡检日报 + 推送设置 ----------------

class ReportGenerateRequest(BaseModel):
    notify: bool = Field(False, description="是否发送站内信/机器人（手动重生成默认不打扰）")


class PushSettingUpdate(BaseModel):
    enabled: bool
    channel_ids: list[int] = Field(default_factory=list)


def _report_out(r: InspectionReport) -> dict:
    def _j(s, default):
        try:
            return json.loads(s) if s else default
        except ValueError:
            return default
    return {
        "id": r.id,
        "report_date": r.report_date.isoformat(),
        "run_id": r.run_id,
        "summary": r.summary,
        "content_md": r.content_md,
        "level": r.level,
        "provider_label": r.provider_label,
        "model": r.model,
        "token_usage": _j(r.token_usage, {}),
        "status": r.status,
        "error": r.error,
        "pushed_channels": _j(r.pushed_channels, []),
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


@router.get("/report")
def get_report(
    date: str = Query("", description="YYYY-MM-DD，缺省最新一份"),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """巡检日报（缺省取最新一份）。"""
    q = db.query(InspectionReport)
    if date:
        try:
            from datetime import date as _d
            q = q.filter(InspectionReport.report_date == _d.fromisoformat(date))
        except ValueError:
            raise HTTPException(400, f"日期格式不正确: {date}")
    report = q.order_by(InspectionReport.report_date.desc()).first()
    return {"report": _report_out(report) if report else None,
            "generating": report_status().get("running", False)}


@router.post("/report/generate")
def trigger_report(
    body: ReportGenerateRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """手动生成/重生成当日日报（notify 可选是否推送）。
    require_model 引导文案按 Accept-Language 出文。"""
    try:
        result = generate_report(db, notify=body.notify, lang=lang_of(request))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except LLMError as e:
        raise HTTPException(502, str(e)[:300])
    if result.get("busy"):
        raise HTTPException(409, "日报生成中，请稍候")
    audit.record(
        db, action="inspection.report.generate", user_id=user.id, username=user.username,
        resource_type="inspection_report", resource_id=str(result.get("report_id", "")),
        detail={"notify": body.notify, "summary": result.get("summary", "")[:200]},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return result


@router.get("/push-settings")
def get_push_settings(
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """巡检推送设置（开关 + 机器人渠道选择）。"""
    ps = db.query(PushSetting).filter(PushSetting.scene == "inspection").first()
    try:
        channel_ids = json.loads(ps.channel_ids) if ps else []
    except ValueError:
        channel_ids = []
    return {"enabled": bool(ps and ps.enabled), "channel_ids": channel_ids}


@router.put("/push-settings")
def update_push_settings(
    body: PushSettingUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    """更新巡检推送设置（仅 admin）。站内信不受影响（必达）。"""
    ps = db.query(PushSetting).filter(PushSetting.scene == "inspection").first()
    if ps is None:
        ps = PushSetting(scene="inspection")
        db.add(ps)
    ps.enabled = body.enabled
    ps.channel_ids = json.dumps(body.channel_ids)
    db.commit()
    audit.record(
        db, action="inspection.push_settings.update", user_id=user.id, username=user.username,
        resource_type="push_settings", resource_id="inspection",
        detail={"enabled": body.enabled, "channel_ids": body.channel_ids},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return {"enabled": ps.enabled, "channel_ids": body.channel_ids}
