"""AI 配置 API：LLM 多配置 CRUD / 连通性测试 / token 用量统计；
POST /ai/ask 自然语言查询 + 历史留痕。"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.client_ip import get_client_ip
from app.core.db import get_db
from app.core.deps import require_roles
from app.core.i18n_msg import lang_of, pick, t
from app.core.security import encrypt_secret
from app.models.ai_query_log import AiQueryLog
from app.models.ai_scenario import AiScenarioModel
from app.models.ai_setting import AiSetting
from app.models.llm_call_log import LlmCallLog
from app.models.user import User
from app.schemas.ai import (
    AiScenarioOut, AiScenarioUpdate,
    AiSettingCreate, AiSettingOut, AiSettingUpdate, AiTestIn, AiTestOut, AskRequest,
)
from app.services.ai_scenarios import SCENARIOS
from app.services import audit
from app.services.ask import run_ask
from app.services.llm import LLMError, chat
from app.services.ai_tools import ToolError

logger = logging.getLogger("qianshu.ai")
router = APIRouter(prefix="/ai", tags=["ai"])


def _mask_key(cipher: str) -> str:
    """密文不可逆推 key，展示固定掩码提示。"""
    return "••••••••（已加密存储）"


def _out(r: AiSetting) -> AiSettingOut:
    return AiSettingOut(
        id=r.id, provider_label=r.provider_label, base_url=r.base_url, model=r.model,
        temperature=r.temperature, max_tokens=r.max_tokens, enabled=r.enabled,
        remark=r.remark, api_key_masked=_mask_key(r.api_key_cipher),
        verify_ssl=bool(getattr(r, "verify_ssl", True)),
        created_at=r.created_at.isoformat(), updated_at=r.updated_at.isoformat(),
    )


def _switch_exclusive(db: Session, row: AiSetting) -> None:
    """启用即切换：全表仅一个 enabled=True。"""
    if row.enabled:
        db.query(AiSetting).filter(AiSetting.enabled == True, AiSetting.id != row.id).update(  # noqa: E712
            {AiSetting.enabled: False}
        )


@router.get("/settings", response_model=list[AiSettingOut])
def list_settings(
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator")),
):
    return [_out(r) for r in db.query(AiSetting).order_by(AiSetting.id).all()]


@router.post("/settings", response_model=AiSettingOut)
def create_setting(
    body: AiSettingCreate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    row = AiSetting(
        provider_label=body.provider_label,
        base_url=body.base_url.strip(),
        api_key_cipher=encrypt_secret(body.api_key),
        model=body.model.strip(),
        temperature=body.temperature,
        max_tokens=body.max_tokens,
        enabled=body.enabled,
        remark=body.remark,
        verify_ssl=bool(getattr(body, "verify_ssl", True)),
    )
    _switch_exclusive(db, row)
    db.add(row)
    db.flush()
    audit.record(
        db, action="ai_setting.create", user_id=user.id, username=user.username,
        resource_type="ai_setting", resource_id=row.id,
        detail={"provider_label": row.provider_label, "model": row.model, "enabled": row.enabled},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return _out(row)


@router.put("/settings/{setting_id}", response_model=AiSettingOut)
def update_setting(
    setting_id: int,
    body: AiSettingUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    row = db.query(AiSetting).filter(AiSetting.id == setting_id).first()
    if not row:
        raise HTTPException(404, t("ai.setting_not_found", lang_of(request)))
    if body.provider_label is not None:
        row.provider_label = body.provider_label
    if body.base_url is not None:
        row.base_url = body.base_url.strip()
    if body.api_key:  # 空串/None 都视为不改
        row.api_key_cipher = encrypt_secret(body.api_key)
    if body.model is not None:
        row.model = body.model.strip()
    if body.temperature is not None:
        row.temperature = body.temperature
    if body.max_tokens is not None:
        row.max_tokens = body.max_tokens
    if body.enabled is not None:
        row.enabled = body.enabled
    if body.remark is not None:
        row.remark = body.remark
    if getattr(body, "verify_ssl", None) is not None:
        row.verify_ssl = body.verify_ssl
    _switch_exclusive(db, row)
    audit.record(
        db, action="ai_setting.update", user_id=user.id, username=user.username,
        resource_type="ai_setting", resource_id=row.id,
        detail={"provider_label": row.provider_label, "model": row.model,
                "enabled": row.enabled, "key_changed": bool(body.api_key)},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return _out(row)


@router.delete("/settings/{setting_id}")
def delete_setting(
    setting_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    row = db.query(AiSetting).filter(AiSetting.id == setting_id).first()
    if not row:
        raise HTTPException(404, t("ai.setting_not_found", lang_of(request)))
    name = row.provider_label
    db.delete(row)
    audit.record(
        db, action="ai_setting.delete", user_id=user.id, username=user.username,
        resource_type="ai_setting", resource_id=setting_id,
        detail={"provider_label": name},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return {"deleted": setting_id}


@router.post("/settings/{setting_id}/test", response_model=AiTestOut)
def test_setting(
    setting_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """连通性测试：发一条极小对话（max_tokens 压到 32），返回回复/耗时/token。"""
    row = db.query(AiSetting).filter(AiSetting.id == setting_id).first()
    if not row:
        raise HTTPException(404, t("ai.setting_not_found", lang_of(request)))
    try:
        r = chat(
            row,
            [{"role": "user", "content": "连通性测试：请只回复四个字「连接成功」，不要输出其他内容。"}],
            purpose="test",
            db=db,
            max_tokens=32,
        )
    except Exception as e:
        logger.warning("LLM 连通性测试失败 setting=%s: %s", setting_id, str(e)[:200])
        return AiTestOut(ok=False, model=row.model, error=str(e)[:300])
    audit.record(
        db, action="ai_setting.test", user_id=user.id, username=user.username,
        resource_type="ai_setting", resource_id=row.id,
        detail={"model": row.model, "duration_ms": r["duration_ms"]},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return AiTestOut(
        ok=True, reply=r["content"], model=row.model,
        duration_ms=r["duration_ms"], usage=r["usage"],
    )


@router.post("/test", response_model=AiTestOut)
def test_unsaved_setting(
    body: AiTestIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """弹窗「测试连通」：按表单当前值测试，无需先保存。

    api_key 留空且 editing_id 有值时回退该配置已保存的 Key；探测性测试不落 llm_call_logs。
    """
    api_key = body.api_key.strip()
    if not api_key and body.editing_id:
        row = db.query(AiSetting).filter(AiSetting.id == body.editing_id).first()
        if row is not None:
            from app.core.security import decrypt_secret

            try:
                api_key = decrypt_secret(row.api_key_cipher)
            except Exception:
                api_key = ""
    if not api_key:
        raise HTTPException(400, t("ai.api_key_required", lang_of(request)))
    probe = AiSetting(   # 未保存的探测配置不入库，只复用 chat() 的请求逻辑
        provider_label="dialog-test", base_url=body.base_url.strip(),
        api_key_cipher=encrypt_secret(api_key), model=body.model.strip(),
        temperature=body.temperature, max_tokens=body.max_tokens, enabled=False,
        verify_ssl=body.verify_ssl,
    )
    try:
        r = chat(
            probe,
            [{"role": "user", "content": "连通性测试：请只回复四个字「连接成功」，不要输出其他内容。"}],
            purpose="test",
            db=None,
            max_tokens=32,
        )
    except Exception as e:
        logger.warning("LLM 弹窗连通性测试失败: %s", str(e)[:200])
        return AiTestOut(ok=False, model=body.model, error=str(e)[:300])
    return AiTestOut(
        ok=True, reply=r["content"], model=body.model,
        duration_ms=r["duration_ms"], usage=r["usage"],
    )


def _scenario_out(db: Session, key: str, row: AiScenarioModel | None, lang: str = "zh") -> AiScenarioOut:
    """场景矩阵一行视图。未绑定（setting_id=0）或绑定的连接已删 → 前端显示「兜底：默认连接」，
    与 resolve_model 运行时的兜底行为一致。label 按 Accept-Language 出文（双语载体 pick）。"""
    setting_id = int((row.setting_id if row else 0) or 0)
    enabled = bool(row.enabled) if row and row.enabled is not None else True
    setting_label = model = ""
    if setting_id:
        s = db.get(AiSetting, setting_id)
        if s is not None:
            setting_label, model = s.provider_label, s.model
        else:
            setting_id = 0   # 绑定的连接已删除：如实回退兜底展示
    return AiScenarioOut(
        scenario=key, label=pick(SCENARIOS[key], lang), setting_id=setting_id,
        enabled=enabled, setting_label=setting_label, model=model,
    )


@router.get("/scenarios", response_model=list[AiScenarioOut])
def list_scenarios(
    request: Request,
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator")),
):
    """场景×模型矩阵：4 场景全量（含未绑定行）；label 按 Accept-Language 出文。"""
    rows = {r.scenario: r for r in db.query(AiScenarioModel).all()}
    return [_scenario_out(db, key, rows.get(key), lang_of(request)) for key in SCENARIOS]


@router.put("/scenarios/{scenario}", response_model=AiScenarioOut)
def update_scenario(
    scenario: str,
    body: AiScenarioUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    """保存场景绑定（admin）：setting_id（0=解除绑定回兜底）/ enabled（场景开关）。
    行不存在则创建——幂等，重复保存同值无副作用。"""
    if scenario not in SCENARIOS:
        raise HTTPException(404, t("ai.scenario_not_found", lang_of(request)))
    row = db.get(AiScenarioModel, scenario)
    if body.setting_id is not None and body.setting_id > 0:
        if db.get(AiSetting, body.setting_id) is None:
            raise HTTPException(400, t("ai.model_not_found", lang_of(request)))
    if row is None:
        row = AiScenarioModel(scenario=scenario)
        db.add(row)
    if body.setting_id is not None:
        row.setting_id = body.setting_id
    if body.enabled is not None:
        row.enabled = body.enabled
    audit.record(
        db, action="ai_scenario.update", user_id=user.id, username=user.username,
        resource_type="ai_scenario", resource_id=scenario,
        detail={"scenario": scenario, "label": pick(SCENARIOS[scenario], "zh"),
                "setting_id": int(row.setting_id or 0), "enabled": bool(row.enabled)},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return _scenario_out(db, scenario, row, lang_of(request))


@router.get("/usage/summary")
def usage_summary(
    days: int = Query(30, ge=1, le=366),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator")),
):
    """token 用量统计：区间总量 + 按日明细 + 按 purpose 拆分。"""
    start = date.today() - timedelta(days=days - 1)
    q = db.query(LlmCallLog).filter(LlmCallLog.call_date >= start)
    rows = q.with_entities(
        LlmCallLog.call_date, LlmCallLog.purpose, LlmCallLog.status,
        LlmCallLog.total_tokens, LlmCallLog.duration_ms,
    ).all()
    by_day: dict = {}
    by_purpose: dict = {}
    total_calls = ok_calls = 0
    total_tokens = prompt_tokens = completion_tokens = 0
    total_ms = 0
    for d, purpose, status, tokens, ms in rows:
        day = by_day.setdefault(d, {"calls": 0, "errors": 0, "total_tokens": 0})
        day["calls"] += 1
        day["errors"] += 0 if status == "ok" else 1
        day["total_tokens"] += int(tokens or 0)
        p = by_purpose.setdefault(purpose, {"calls": 0, "errors": 0, "total_tokens": 0})
        p["calls"] += 1
        p["errors"] += 0 if status == "ok" else 1
        p["total_tokens"] += int(tokens or 0)
        total_calls += 1
        ok_calls += 1 if status == "ok" else 0
        total_tokens += int(tokens or 0)
        total_ms += int(ms or 0)
    days_list = []
    cur = start
    while cur <= date.today():  # 无调用日补零，曲线连续
        v = by_day.get(cur)
        days_list.append({
            "date": cur.isoformat(),
            "calls": v["calls"] if v else 0,
            "errors": v["errors"] if v else 0,
            "total_tokens": v["total_tokens"] if v else 0,
        })
        cur += timedelta(days=1)
    return {
        "days": days,
        "total_calls": total_calls,
        "ok_calls": ok_calls,
        "error_calls": total_calls - ok_calls,
        "total_tokens": total_tokens,
        "avg_duration_ms": round(total_ms / total_calls) if total_calls else 0,
        "by_purpose": [
            {"purpose": k, **v} for k, v in sorted(by_purpose.items(), key=lambda x: -x[1]["total_tokens"])
        ],
    }


@router.get("/usage/logs")
def usage_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator")),
):
    """最近调用留痕（含失败原因）。"""
    q = db.query(LlmCallLog)
    total = q.count()
    rows = (
        q.order_by(LlmCallLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "total": total,
        "items": [
            {
                "id": r.id,
                "setting_id": r.setting_id,
                "provider_label": r.provider_label,
                "model": r.model,
                "purpose": r.purpose,
                "prompt_tokens": r.prompt_tokens,
                "completion_tokens": r.completion_tokens,
                "total_tokens": r.total_tokens,
                "duration_ms": r.duration_ms,
                "status": r.status,
                "error": r.error,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ],
    }


@router.post("/ask")
def ask(
    body: AskRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """自然语言查询：路由 -> 只读工具 -> LLM 汇总回答。全程留痕。
    require_model 引导与 LLM 回答语言跟随界面（Accept-Language）。"""
    lang = lang_of(request)
    try:
        result = run_ask(db, user.id, body.question.strip(), lang=lang)
    except LLMError as e:
        db.add(AiQueryLog(user_id=user.id, question=body.question, status="error", error=str(e)[:500]))
        db.commit()
        raise HTTPException(502, str(e)[:300])
    except ToolError as e:
        db.add(AiQueryLog(user_id=user.id, question=body.question, status="error", error=str(e)[:500]))
        db.commit()
        raise HTTPException(502, t("ai.tool_exec_failed", lang, detail=str(e)[:200]))
    return result


@router.get("/ask/history")
def ask_history(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """当前用户的查询历史（回放问答与工具轨迹）。"""
    q = db.query(AiQueryLog).filter(AiQueryLog.user_id == user.id)
    total = q.count()
    rows = (
        q.order_by(AiQueryLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    def _j(s, default):
        try:
            return json.loads(s) if s else default
        except ValueError:
            return default
    return {
        "total": total,
        "items": [
            {
                "id": r.id,
                "question": r.question,
                "answer": r.answer,
                "tools_used": _j(r.tools_used, []),
                "token_usage": _j(r.token_usage, {}),
                "status": r.status,
                "error": r.error,
                "duration_ms": r.duration_ms,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ],
    }


class ToolExecIn(BaseModel):
    """单工具调用（REST 等价入口）：skills 文档承诺的 POST /api/ai/tools/execute。"""
    tool: str = Field(min_length=1, max_length=64)
    params: dict = Field(default_factory=dict)


@router.post("/tools/execute")
def tools_execute(
    body: ToolExecIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """REST 等价：执行单个只读查询工具（与 MCP /api/mcp tools/call 同一注册表）。
    工具全部只读（ai_tools/diag_tools 注册表），无变更能力。"""
    from app.services.copilot.tools_read import execute as _exec
    from app.services.ai_tools import ToolError

    try:
        out = _exec(db, body.tool.strip(), body.params or {}, user=user)
    except ToolError as e:
        raise HTTPException(400, str(e)[:300])
    except KeyError:
        raise HTTPException(404, t("ai.tool_not_found", lang_of(request), tool=body.tool))
    return {"tool": body.tool, "result": out}
