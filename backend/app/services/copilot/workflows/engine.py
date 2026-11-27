"""工作流引擎：声明式步骤串 + 中间校验 + 结果物。

职责：
  - 参数 schema 校验（params_schema -> 干净 params；不过 raise ParamError）
  - 步步执行：{step,status,detail,ms}，status ∈ ok/failed/skipped；
    单步失败不阻断后续步骤（可续跑），失败如实进 steps
  - 每步落 agent_workflow_runs：起跑建行 status=running，逐步更新 steps_json，
    收尾落 artifact_json/finished_at
  - 变更步骤统一走 build_change_ticket() -> app.services.copilot.change_tools.build_ticket；
    变更层采用 lazy import，ImportError -> ChangeLayerUnavailable，调用方步骤标
    "待确认变更（变更层未就绪）"继续；工作流本身绝不直接执行变更
  - 分析取数走 tools_read.execute（只读工具白名单），不执行 LLM/脚本原文

run_workflow() 返回：{run_id, status, steps, artifact, tickets}。
"""
from __future__ import annotations

import inspect
import json
import logging
import time
from datetime import datetime
from types import SimpleNamespace
from typing import Optional

from sqlalchemy.orm import Session

from app.services.copilot import tools_read

logger = logging.getLogger("qianshu.copilot.workflows")

# 步骤状态（steps[].status）
STEP_OK = "ok"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"


class ParamError(ValueError):
    """参数 schema 校验不过（API 层映射 400）。

    message 缺省中文（str(e) 保持原有口径）；i18n_key/kw 记 MSG 条目与占位参数，
    API 出口可按请求语言重渲染（message_for）。
    """

    def __init__(self, message: str, i18n_key: str = "", **kw):
        super().__init__(message)
        self.i18n_key = i18n_key
        self.kw = kw

    def message_for(self, lang: str = "zh") -> str:
        """按请求语言重渲染文案；无 i18n_key 的旧式构造回退 str(self)。"""
        if self.i18n_key:
            from app.core.i18n_msg import t
            return t(self.i18n_key, lang, **self.kw)
        return str(self)


class WorkflowNotFound(KeyError):
    """未知工作流 key（API 层映射 404）。"""


class ChangeLayerUnavailable(RuntimeError):
    """变更层（change_tools）未就绪——调用方按'待确认变更（变更层未就绪）'降级。"""


# ---------------- 参数校验 ----------------

def _param_error(key: str, **kw) -> ParamError:
    """按 MSG 条目构造 ParamError：message=中文原文（逐字锁定旧 f-string），key/kw 供出口按 lang 重渲染。"""
    from app.core.i18n_msg import t as _t
    return ParamError(_t(key, "zh", **kw), key, **kw)


def _coerce(field: str, spec: dict, raw):
    t = spec.get("type", "string")
    if t == "integer":
        try:
            v = int(str(raw).strip())
        except (TypeError, ValueError):
            raise _param_error("wf.param.int", field=field, raw=raw)
        if "min" in spec and v < spec["min"]:
            raise _param_error("wf.param.min", field=field, min=spec["min"])
        if "max" in spec and v > spec["max"]:
            raise _param_error("wf.param.max", field=field, max=spec["max"])
        return v
    if t == "number":
        try:
            v = float(str(raw).strip())
        except (TypeError, ValueError):
            raise _param_error("wf.param.num", field=field, raw=raw)
        if "min" in spec and v < spec["min"]:
            raise _param_error("wf.param.min", field=field, min=spec["min"])
        if "max" in spec and v > spec["max"]:
            raise _param_error("wf.param.max", field=field, max=spec["max"])
        return v
    if t == "boolean":
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in ("true", "1", "yes", "是"):
            return True
        if s in ("false", "0", "no", "否"):
            return False
        raise _param_error("wf.param.bool", field=field, raw=raw)
    # string（默认）
    v = str(raw).strip()
    if not v:
        raise _param_error("wf.param.empty", field=field)
    return v


def validate_params(schema: dict, params: dict) -> dict:
    """按 params_schema 校验/清洗参数。缺必填/类型不对/枚举外 -> ParamError。"""
    params = params or {}
    if not isinstance(params, dict):
        raise _param_error("wf.param.not_object")
    out: dict = {}
    for field, spec in (schema or {}).items():
        raw = params.get(field)
        missing = raw is None or (isinstance(raw, str) and not raw.strip())
        if missing:
            if spec.get("required"):
                raise _param_error("wf.param.required", field=field, desc=spec.get("description", ""))
            if "default" in spec:
                out[field] = spec["default"]
            continue
        v = _coerce(field, spec, raw)
        enum = spec.get("enum")
        if enum and v not in enum:
            raise _param_error("wf.param.enum", field=field, value=v,
                               choices=", ".join(map(str, enum)))
        out[field] = v
    return out


# ---------------- 只读工具取数（分析域复用 ai_tools/diag_tools） ----------------

def run_tool(db: Session, name: str, params: dict) -> dict:
    """执行一个只读工具（tools_read 白名单）。异常转 ToolError 语义抛出，由步骤层收口。"""
    return tools_read.execute(db, name, params or {})


# ---------------- 变更工单（唯一写出口：只建工单不执行） ----------------

def _normalize_ticket(out) -> dict:
    """build_ticket 返回值归一为 dict（dict / ORM 对象都认）。"""
    if isinstance(out, dict):
        tid = out.get("ticket_id", out.get("id"))
        d = dict(out)
        if tid is not None:
            d.setdefault("ticket_id", tid)
        return d
    tid = getattr(out, "id", None)
    return {"ticket_id": tid, "status": getattr(out, "status", "pending")}


def _resolve_ticket_user(ctx: dict, user=None):
    """工单构建用户：context.user（真用户对象）优先，防止替身越权；
    无真用户 → **最小权限替身**（role=viewer，绝不冒充 admin——平台管理域 check_fn/
    guard 按真角色把关，替身提权=越权风险）；返回 (user_like, minimal)。

    兼容三类 context.user 形态：真用户对象（User ORM / SimpleNamespace 带 username）、
    纯用户名字符串（scheduler/老调用）、缺省。替身 id/username 仍按 context 取
    （审计归属不丢），只有角色按最小权限。
    """
    u = user if user is not None else (ctx or {}).get("user")
    if u is not None and not isinstance(u, (str, bytes)) and hasattr(u, "username"):
        return u, False
    name = u if isinstance(u, str) else str((ctx or {}).get("user") or "")
    try:
        uid = int((ctx or {}).get("user_id") or 0)
    except (TypeError, ValueError):
        uid = 0
    stand = SimpleNamespace(id=uid, username=name or "workflow",
                           role="viewer", is_active=True)
    return stand, True


def build_change_ticket(db: Session, *, title: str, items: list,
                        context: Optional[dict] = None,
                        ack_required: bool = False,
                        user=None) -> dict:
    """建变更工单（产物=待确认工单，执行由用户在确认卡完成）。

    委托 app.services.copilot.change_tools.build_ticket（变更层）：
      - ImportError / 缺函数 -> ChangeLayerUnavailable（步骤标"待确认变更（变更层未就绪）"）
      - 签名差异适配：只传 build_ticket 声明的同名参数，避免可选参数差异破坏调用
      - user 传参：user= 真用户优先 → context.user（对象形态）→
        最小权限替身（role=viewer）；替身构建时返回值带 minimal_privilege=True，
        调用方（change_step）在步骤 detail 注明"以最小权限构建"。签名向后兼容
        （user 可省略，老三参/四参调用照常）。
      - 其余异常向上抛（步骤层标 failed，如实留痕）
    红线：工作流不执行任何变更写入，本函数只负责把变更清单包装成工单。
    """
    try:
        from app.services.copilot import change_tools
    except ImportError as e:
        raise ChangeLayerUnavailable(f"change_tools 未就绪: {e}")
    fn = getattr(change_tools, "build_ticket", None)
    if fn is None:
        raise ChangeLayerUnavailable("change_tools.build_ticket 不存在")
    ctx = context or {}
    ticket_user, minimal = _resolve_ticket_user(ctx, user)
    clean_items = [
        {"tool": it.get("tool"), "params": dict(it.get("params") or {})}
        for it in (items or []) if isinstance(it, dict)
    ]
    kwargs = {
        "title": title,
        "items": clean_items,
        "ack_required": bool(ack_required),
        "session_id": int(ctx.get("session_id") or 0),
        "msg_id": int(ctx.get("msg_id") or 0),
        "context": ctx,
        "user": ticket_user,
    }
    # 按签名过滤 kwargs：build_ticket 签名以变更层为准，同名参数才传
    try:
        sig = inspect.signature(fn)
        if not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
            kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
    except (TypeError, ValueError):
        pass
    out = _normalize_ticket(fn(db, **kwargs))
    out.setdefault("minimal_privilege", bool(minimal))
    return out


def _sanitize_ticket(ticket: dict) -> dict:
    """工单出参脱敏：change_list 里的 params（可能含 AK/SK/Token）只留键名——
    凭证不回显（红线：不落步骤/产物/日志明文）；执行用的完整参数在工单库里。"""
    safe = {k: v for k, v in ticket.items() if k != "change_list"}
    out_list = []
    for c in (ticket.get("change_list") or []):
        if not isinstance(c, dict):
            continue
        cc = {k: v for k, v in c.items() if k != "params"}
        cc["params_keys"] = sorted((c.get("params") or {}).keys())
        out_list.append(cc)
    safe["change_list"] = out_list
    return safe


def change_step(db: Session, ctx: dict, *, title: str, items: list,
                ack_required: bool = False,
                ok_detail: str = "") -> dict:
    """变更类步骤的统一收口：
    成功 -> ok（工单入 ctx["tickets"]，已脱敏）；变更层未就绪 -> skipped 待确认变更；其余 -> failed。"""
    try:
        ticket = build_change_ticket(db, title=title, items=items,
                                     context=ctx.get("context"), ack_required=ack_required)
    except ChangeLayerUnavailable:
        return {"status": STEP_SKIPPED,
                "detail": "待确认变更（变更层未就绪）：变更清单已生成但未建单，请在变更层就绪后重跑或人工操作"}
    except Exception as e:  # noqa: BLE001
        return {"status": STEP_FAILED,
                "detail": f"变更工单创建失败：{type(e).__name__}: {str(e)[:150]}"}
    tid = ticket.get("ticket_id")
    safe = _sanitize_ticket(ticket)
    safe.setdefault("title", title)
    safe.setdefault("ack_required", bool(ack_required))
    safe.setdefault("ticket_id", tid)
    ctx.setdefault("tickets", []).append(safe)
    detail = ok_detail or f"已生成待确认变更工单 #{tid}（{title}）——需用户在变更确认卡确认后才执行"
    if ticket.get("minimal_privilege"):
        detail += "（无真用户上下文，以最小权限构建）"
    return {"status": STEP_OK, "detail": detail, "data": {"ticket_id": tid}}


# ---------------- 执行引擎 ----------------

def _save_run(db: Session, run) -> None:
    try:
        db.commit()
    except Exception as e:  # noqa: BLE001  运行留痕失败不阻断工作流本身
        logger.warning("agent_workflow_runs 落库失败（工作流继续）: %s", str(e)[:120])
        db.rollback()


def execute(db: Session, spec: dict, key: str, params: dict,
            context: Optional[dict] = None) -> dict:
    """跑一个工作流：参数校验 -> 建运行行 -> 步步执行（逐步落库）-> 产物 -> 收尾。"""
    context = dict(context or {})
    clean = validate_params(spec.get("params_schema"), params)
    if spec.get("params_passthrough"):
        # 自定义工作流（workflows.custom）：无固定 params_schema，
        # 运行参数原样透传给步骤参数模板。只加不改：内置 spec 不带此键，行为零变化。
        for _k, _v in (params or {}).items():
            clean.setdefault(_k, _v)

    from app.models.agent_change import AgentWorkflowRun

    run = AgentWorkflowRun(
        session_id=int(context.get("session_id") or 0),
        workflow_key=key,
        steps_json="[]",
        status="running",
    )
    db.add(run)
    _save_run(db, run)
    run_id = getattr(run, "id", 0)

    ctx = {"context": context, "params": clean, "data": {}, "tickets": []}
    steps: list = []
    try:
        for step_name, fn in spec.get("steps", []):
            t0 = time.monotonic()
            try:
                r = fn(db, clean, ctx) or {}
            except Exception as e:  # noqa: BLE001  单步异常=failed，不裸抛（后续步骤照跑）
                r = {"status": STEP_FAILED,
                     "detail": f"{type(e).__name__}: {str(e)[:200]}"}
            ms = int((time.monotonic() - t0) * 1000)
            status = r.get("status") if r.get("status") in (STEP_OK, STEP_FAILED, STEP_SKIPPED) else STEP_FAILED
            step_rec = {
                "step": step_name,
                "status": status,
                "detail": str(r.get("detail") or "")[:2000],
                "ms": ms,
                # loop 轨迹兼容别名（loop._try_workflow 按 name/params_summary/result_summary 取）
                "name": step_name,
                "params_summary": "",
                "result_summary": str(r.get("detail") or "")[:500],
            }
            steps.append(step_rec)
            if r.get("data") is not None:
                ctx["data"][step_name] = r["data"]
            run.steps_json = json.dumps(steps, ensure_ascii=False, default=str)
            _save_run(db, run)

        build = spec.get("build_artifact")
        artifact = build(db, clean, ctx) if build else {}
    except Exception as e:  # noqa: BLE001  产物构建异常也要收尾落库
        logger.exception("工作流 %s 收尾异常", key)
        artifact = {"report_md": f"（产物生成失败：{type(e).__name__}: {str(e)[:150]}）",
                    "highlights": []}
        if not any(s["status"] == STEP_FAILED for s in steps):
            steps.append({"step": "生成产物", "status": STEP_FAILED,
                          "detail": f"{type(e).__name__}: {str(e)[:200]}", "ms": 0,
                          "name": "生成产物", "params_summary": "",
                          "result_summary": f"{type(e).__name__}: {str(e)[:200]}"})

    status = "done" if not any(s["status"] == STEP_FAILED for s in steps) else "failed"
    run.status = status
    run.steps_json = json.dumps(steps, ensure_ascii=False, default=str)
    run.artifact_json = json.dumps(artifact, ensure_ascii=False, default=str)
    run.finished_at = datetime.now()
    _save_run(db, run)

    # 对话回答文本（loop._try_workflow 消费 content_md；API 契约看 artifact）
    content_md = str(artifact.get("report_md") or "").strip()
    if not content_md:
        content_md = "\n".join(f"- {h}" for h in (artifact.get("highlights") or []))

    return {
        "run_id": run_id,
        "status": status,
        "steps": steps,
        "artifact": artifact,
        "tickets": ctx.get("tickets") or [],
        # ---- loop 兼容字段（loop._try_workflow 契约；不与 API 契约冲突） ----
        "content_md": content_md,
        "answer": content_md,
        # 工单已由本引擎经 change_tools.build_ticket 建好（见 tickets）；
        # 这里回空 change_items 防 loop 再建一张重复工单
        "change_items": [],
    }
