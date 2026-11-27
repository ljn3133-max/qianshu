"""故障诊断 API：发起/历史、案例库沉淀/检索/复盘编辑与历史自动清理。"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core.client_ip import get_client_ip
from app.core.db import get_db
from app.core.deps import require_roles
from app.core.i18n_msg import lang_of
from app.models.diagnosis import DiagnosisSession
from app.models.diagnosis_case import DiagnosisCase
from app.models.user import User
from app.services import audit
from app.services.diagnosis import prune_sessions, run_diagnosis, session_out
from app.services.llm import LLMError

logger = logging.getLogger("qianshu.diagnosis")
router = APIRouter(prefix="/diagnosis", tags=["diagnosis"])


class DiagnosisRequest(BaseModel):
    question: str = Field(..., min_length=2, max_length=500, description="故障现象描述，如 www.example.com 打不开")


class CaseCreate(BaseModel):
    session_id: int = Field(..., description="要沉淀的诊断会话 ID")
    root_cause: str = Field("", max_length=500, description="真实根因（选填）")
    solution: str = Field("", max_length=5000, description="实际解决措施（选填）")
    tags: str = Field("", max_length=255, description="标签，逗号分隔，如 oauth,5xx（选填）")


class CaseUpdate(BaseModel):
    root_cause: str = Field("", max_length=500)
    solution: str = Field("", max_length=5000)
    tags: str = Field("", max_length=255)


def _case_out(c: DiagnosisCase, with_body: bool = False) -> dict:
    """案例序列化；with_body=False 供列表（不含长文本），True 供详情。"""
    out = {
        "id": c.id,
        "session_id": c.session_id,
        "question": c.question,
        "host": c.host,
        "level": c.level,
        "tool_calls": c.tool_calls,
        "duration_ms": c.duration_ms,
        "diagnosed_at": c.diagnosed_at.isoformat() if c.diagnosed_at else None,
        "root_cause": c.root_cause,
        "solution": c.solution,
        "tags": c.tags,
        "status": c.status,
        "source": c.source,
        "created_by": c.created_by,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }
    if with_body:
        out["conclusion_md"] = c.conclusion_md
        try:
            out["steps"] = json.loads(c.steps or "[]")
        except ValueError:
            out["steps"] = []
    return out


@router.post("/run")
def trigger_diagnosis(
    body: DiagnosisRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """发起一次故障诊断（Agent 逐层取证后给结论，同步执行，约 1~3 分钟）。
    require_model 引导与 LLM 回答语言跟随界面（Accept-Language）。"""
    try:
        result = run_diagnosis(db, user.id, body.question.strip(), lang=lang_of(request))
    except LLMError as e:
        raise HTTPException(502, str(e)[:300])
    audit.record(
        db, action="diagnosis.run", user_id=user.id, username=user.username,
        resource_type="diagnosis", resource_id=str(result["id"]),
        detail={"question": result["question"][:200], "level": result["level"],
                "tool_calls": result["tool_calls"]},
        client_ip=get_client_ip(request),
    )
    # 历史只保留最近 _KEEP_SESSIONS 条（已入库案例的会话永不清理）
    removed = prune_sessions(db)
    if removed:
        logger.info("诊断历史自动清理：%s 条", removed)
    db.commit()
    return result


@router.get("/sessions")
def list_sessions(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """诊断历史（列表，不含结论正文；带 case_id 供前端切换"已入库"状态）。"""
    q = db.query(DiagnosisSession)
    total = q.count()
    rows = q.order_by(DiagnosisSession.id.desc()).offset(
        (page - 1) * page_size).limit(page_size).all()
    case_ids = {c.session_id: c.id for c in db.query(DiagnosisCase)
                .filter(DiagnosisCase.status != "discarded").all()}
    items = [{
        "id": s.id, "question": s.question, "host": s.host, "status": s.status,
        "scenario": s.scenario or "link_fault",
        "level": s.level, "tool_calls": s.tool_calls, "duration_ms": s.duration_ms,
        "case_id": case_ids.get(s.id),
        "created_at": s.created_at.isoformat() if s.created_at else None,
    } for s in rows]
    return {"total": total, "items": items}


@router.get("/sessions/{session_id}")
def session_detail(
    session_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """诊断会话详情（含步骤轨迹与结论文本；带 case_id）。"""
    s = db.get(DiagnosisSession, session_id)
    if s is None:
        raise HTTPException(404, "诊断记录不存在")
    out = session_out(s)
    case = db.query(DiagnosisCase).filter(DiagnosisCase.session_id == session_id).first()
    out["case_id"] = case.id if case else None
    return out


# ==================== 案例库 ====================

@router.post("/cases")
def create_case(
    body: CaseCreate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """把一次诊断报告沉淀为案例（快照入库，复盘字段选填）。"""
    s = db.get(DiagnosisSession, body.session_id)
    if s is None:
        raise HTTPException(404, "诊断记录不存在")
    if s.status != "ok":
        raise HTTPException(400, "诊断未成功完成，不建议入库（无有效结论）")
    existed = (db.query(DiagnosisCase)
               .filter(DiagnosisCase.session_id == body.session_id,
                       DiagnosisCase.status != "discarded").first())
    if existed:
        raise HTTPException(409, f"该诊断已入库（案例 #{existed.id}）")

    case = DiagnosisCase(
        session_id=s.id,
        question=s.question,
        host=s.host,
        level=s.level,
        conclusion_md=s.conclusion_md,
        steps=s.steps,
        tool_calls=s.tool_calls,
        duration_ms=s.duration_ms,
        diagnosed_at=s.created_at or datetime.now(),
        root_cause=body.root_cause.strip(),
        solution=body.solution.strip(),
        tags=",".join(t.strip() for t in body.tags.split(",") if t.strip()),
        created_by=user.id,
    )
    db.add(case)
    db.flush()
    audit.record(
        db, action="diagnosis.case.create", user_id=user.id, username=user.username,
        resource_type="diagnosis_case", resource_id=str(case.id),
        detail={"session_id": s.id, "question": s.question[:200], "level": s.level,
                "tags": case.tags},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return _case_out(case, with_body=True)


@router.get("/cases")
def list_cases(
    keyword: str = Query("", max_length=100, description="关键词：问题/根因/措施/结论/标签"),
    host: str = Query("", max_length=255),
    level: str = Query("", max_length=16),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """案例库检索（永久保留；AI 自动候选/已丢弃的不进库，必须经人工把关）。"""
    q = db.query(DiagnosisCase).filter(DiagnosisCase.status != "discarded")
    if host:
        q = q.filter(DiagnosisCase.host.ilike(f"%{host}%"))
    if level:
        q = q.filter(DiagnosisCase.level == level)
    if keyword:
        like = f"%{keyword}%"
        cols = (DiagnosisCase.question, DiagnosisCase.host, DiagnosisCase.root_cause,
                DiagnosisCase.solution, DiagnosisCase.conclusion_md, DiagnosisCase.tags)
        q = q.filter(or_(*[c.ilike(like) for c in cols]))
    total = q.count()
    rows = q.order_by(DiagnosisCase.id.desc()).offset(
        (page - 1) * page_size).limit(page_size).all()
    return {"total": total, "items": [_case_out(c) for c in rows]}


@router.get("/cases/{case_id}")
def case_detail(
    case_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """案例详情（含完整排查轨迹与结论，展示形态与诊断报告一致）。"""
    c = db.get(DiagnosisCase, case_id)
    if c is None:
        raise HTTPException(404, "案例不存在")
    return _case_out(c, with_body=True)


@router.post("/cases/{case_id}/accept")
def accept_case(
    case_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """人工采纳为经验：AI 结论经确认才入库，RAG 才检索它。"""
    c = db.get(DiagnosisCase, case_id)
    if c is None:
        raise HTTPException(404, "案例不存在")
    c.status = "accepted"
    audit.record(
        db, action="diagnosis.case.accept", user_id=user.id, username=user.username,
        resource_type="diagnosis_case", resource_id=str(case_id),
        detail={"question": c.question[:80]},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return _case_out(c)


@router.put("/cases/{case_id}")
def update_case(
    case_id: int,
    body: CaseUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """编辑案例复盘字段（根因/解决措施/标签）。"""
    c = db.get(DiagnosisCase, case_id)
    if c is None:
        raise HTTPException(404, "案例不存在")
    c.root_cause = body.root_cause.strip()
    c.solution = body.solution.strip()
    c.tags = ",".join(t.strip() for t in body.tags.split(",") if t.strip())
    audit.record(
        db, action="diagnosis.case.update", user_id=user.id, username=user.username,
        resource_type="diagnosis_case", resource_id=str(case_id),
        detail={"tags": c.tags},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return _case_out(c, with_body=True)
