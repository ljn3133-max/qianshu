"""千枢智能体 · 工作流 API。

  GET    /api/agent/workflows                  工作流清单（内置 12 + 自定义，含最近运行状态）
  GET    /api/agent/workflows/{key}/detail     工作流详情（内置/自定义统一出步骤结构化视图，只读）
  POST   /api/agent/workflows/{key}/save-as    内置工作流「另存为自定义」（admin；key 加 -copy 后缀）
  POST   /api/agent/workflows/{key}/run        触发工作流（body: {params, session_id?}）
  GET    /api/agent/workflows/runs             最近运行记录
  GET    /api/agent/workflows/custom           自定义定义列表（admin）
  POST   /api/agent/workflows/custom           创建/幂等导入（admin；overwrite=true 同 key 覆盖）
  PUT    /api/agent/workflows/custom/{key}     修改（admin；version+1）
  DELETE /api/agent/workflows/custom/{key}     删除（admin）
  POST   /api/agent/workflows/custom/preview   预览执行：逐步展开计划（admin，零副作用）
  GET    /api/agent/workflows/custom/catalog   工具目录（admin，编辑器工具选择器）
  GET    /api/agent/workflows/custom/{key}/versions           版本历史（admin；当前版标注）
  POST   /api/agent/workflows/custom/{key}/versions/{v}/restore  一键回滚（admin；恢复=v 内容→version+1 新行，审计）
  GET    /api/agent/workflows/custom/{key}/export?format=yaml|json  导出定义（admin，敏感占位符原样）
  POST   /api/agent/workflows/custom/import    导入 YAML/JSON（admin；schema 同创建校验，key 冲突 409+overwrite）

与 agent_copilot.py（会话 SSE）同挂 /api/agent 前缀，两组路由各自独立。
权限：读 admin/operator/viewer；run 要求 admin/operator（viewer 只能看）；
自定义定义的管理（custom* 与 save-as 另存为）仅 admin——operator 可跑不可改。
红线：工作流只产报告与待确认变更工单，不直接执行变更（run 里没有执行通道）；
自定义步骤只能从 tools_read 只读工具 + change_tools 变更白名单里选（custom.py 硬校验）。
"""
from __future__ import annotations

import json
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import require_roles
from app.core.i18n_msg import lang_of, pick, t
from app.models.agent_change import AgentWorkflowRun
from app.models.user import User
from app.services import audit
from app.services.copilot import workflows as wf
from app.services.copilot.workflows import custom as wf_custom
from app.services.copilot.workflows import def_io as wf_def_io
from app.services.copilot.workflows import summary as wf_summary

logger = logging.getLogger("qianshu.agent_workflows")
router = APIRouter(prefix="/agent", tags=["agent-workflows"])

_ROLES = ("admin", "operator", "viewer")
_RUN_ROLES = ("admin", "operator")
_ADMIN = ("admin",)


def _last_run(db: Session, key: str) -> Optional[dict]:
    r = (db.query(AgentWorkflowRun)
         .filter(AgentWorkflowRun.workflow_key == key)
         .order_by(AgentWorkflowRun.id.desc()).first())
    if r is None:
        return None
    return {
        "run_id": r.id, "status": r.status, "session_id": r.session_id,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
    }


@router.get("/workflows")
def list_workflows(request: Request,
                   db: Session = Depends(get_db),
                   user: User = Depends(require_roles(*_ROLES))):
    """工作流清单：key/name/domain/description/params_schema + 最近运行。
    name/domain/description 按 Accept-Language 出文（双语载体 pick）。
    steps_summary 为步骤名摘要，列表卡片直接显示步骤链。"""
    lang = lang_of(request)
    # 可定时与否由调度侧统一口径给出，不能依赖 domain 的本地化文案，
    # 否则启用中的自定义工作流会拿不到定时开关。
    from app.services.agent_schedule import schedulable_keys
    schedulable = schedulable_keys(db)
    items = []
    for key, spec in wf.WORKFLOWS.items():
        items.append({
            "key": key,
            "name": pick(spec.get("name", key), lang),
            "domain": pick(spec.get("domain", ""), lang),
            "description": pick(spec.get("description", ""), lang),
            "params_schema": spec.get("params_schema") or {},
            "steps_summary": wf_summary.steps_summary(spec),
            "schedulable": key in schedulable,
            "last_run": _last_run(db, key),
        })
    # 自定义工作流与内置工作流同面板互通（可运行/看运行记录）；
    # 含已停用项（enabled 标记，前端禁运行按钮）——清单即真相，不藏定义
    for d in wf_custom.list_defs(db):
        items.append({
            "key": d["key"],
            "name": d["name"],
            "domain": d["domain"],
            "description": d["description"],
            "params_schema": {},
            "custom": True,
            "enabled": d["enabled"],
            "schedulable": d["key"] in schedulable,
            "params_hint": d["params_hint"],
            "steps_summary": [str(s.get("label") or s.get("tool") or "")
                              for s in (d.get("steps") or [])],
            "last_run": _last_run(db, d["key"]),
        })
    return {"items": items}


@router.get("/workflows/{key}/detail")
def workflow_detail(key: str, request: Request,
                    db: Session = Depends(get_db),
                    user: User = Depends(require_roles(*_ROLES))):
    """工作流结构化详情（内置/自定义统一出步骤视图；只读）。
    内置：从 WORKFLOWS spec 渲染 steps 摘要（步骤序列/工具/失败策略）+ params_schema；
    自定义：步骤定义（tool/params_template/continue_on_fail）。敏感参数掩码，凭证不出详情。"""
    if key in wf_custom.RESERVED_KEYS:
        raise HTTPException(404, f"工作流「{key}」不存在（保留路由名）")
    lang = lang_of(request)
    spec = wf.WORKFLOWS.get(key)
    if spec is not None:
        return wf_summary.builtin_detail(key, spec, lang)
    row = next((d for d in wf_custom.list_defs(db) if d["key"] == key), None)
    if row is None:
        raise HTTPException(404, f"工作流「{key}」不存在")
    return wf_summary.custom_detail(row)


class SaveAsIn(BaseModel):
    overwrite: bool = False   # 幂等：同 key 副本已存在时覆盖更新（version+1），不产生重复行


@router.post("/workflows/{key}/save-as")
def save_workflow_as(key: str, body: SaveAsIn, request: Request,
                     db: Session = Depends(get_db),
                     user: User = Depends(require_roles(*_ADMIN))):
    """内置工作流「另存为自定义」：以内置为模板生成可编辑副本（key 加 -copy 后缀，进自定义区）。
    只迁移白名单工具步骤（内建逻辑步骤如实剔除，omitted_labels 说明）——不放宽自定义步骤白名单。
    副本已存在且未带 overwrite → 409（防覆盖用户改过的副本）；overwrite=true 幂等覆盖（version+1）。"""
    if key in wf_custom.RESERVED_KEYS:
        raise HTTPException(404, f"工作流「{key}」不存在（保留路由名）")
    spec = wf.WORKFLOWS.get(key)
    if spec is None:
        raise HTTPException(400, f"「{key}」不是内置工作流——另存为仅支持内置；自定义工作流可直接编辑")
    lang = lang_of(request)
    steps, omitted = wf_summary.derive_custom_steps(spec)
    if not steps:
        raise HTTPException(400,
                            f"内置工作流「{pick(spec.get('name', key), lang)}」的步骤全部是平台内建逻辑"
                            f"（{'、'.join(omitted)}），没有可迁移的白名单工具步骤——"
                            f"要定制请点「新建工作流」自行编排")
    name = f"{pick(spec.get('name', key), lang)}{pick({'zh': '（副本）', 'en': ' (copy)'}, lang)}"
    note = pick({
        "zh": f"（另存自内置工作流 {key}，仅迁移工具类步骤，可自由增删改）",
        "en": f" (Saved as a copy of built-in workflow {key}; tool steps only — freely editable)",
    }, lang)
    description = (pick(spec.get("description", ""), lang) + "\n" + note)[:500]
    new_key = f"{key}-copy"
    try:
        out = wf_custom.create_def(
            db, key=new_key, name=name, description=description,
            domain=pick(spec.get("domain", ""), lang), steps=steps,
            enabled=True, created_by=user.username, created_by_id=user.id,
            overwrite=body.overwrite)
    except Exception as e:  # noqa: BLE001  校验类异常统一映射（409 冲突/400 schema）
        raise _def_http_error(e)
    _audit_def(db, user, "agent.workflow.save_as", new_key,
               {"from": key, "steps": len(steps), "omitted": omitted,
                "overwrite": body.overwrite})
    return {**out, "source_key": key,
            "copied_steps": len(steps), "omitted_labels": omitted}


class RunIn(BaseModel):
    params: Optional[dict] = None
    session_id: Optional[int] = None


@router.post("/workflows/{key}/run")
def run_workflow(key: str, body: RunIn, request: Request,
                 db: Session = Depends(get_db),
                 user: User = Depends(require_roles(*_RUN_ROLES))):
    """触发工作流。产物=结构化报告（artifact）+ 待确认变更工单（tickets，不自动执行）。
    body.session_id 可选：带了则运行记录挂会话（listMessages 合成工作流结果卡，重开仍在）。
    参数校验文案按 Accept-Language 出文（MSG: wf.param.*）。"""
    context = {"user": user, "user_id": user.id,
               "session_id": int(body.session_id or 0)}
    try:
        result = wf.run_workflow(db, key, body.params or {}, context)
    except wf.WorkflowNotFound as e:
        raise HTTPException(404, str(e))
    except wf.ParamError as e:
        raise HTTPException(400, t("wf.param.invalid", lang_of(request),
                                   detail=e.message_for(lang_of(request))))
    # 产物补 ticket_ids 引用（listMessages 合成工作流卡时带工单引用；失败不影响运行结果）
    try:
        tids = [t.get("ticket_id") for t in (result.get("tickets") or []) if t.get("ticket_id")]
        if tids:
            run = db.get(AgentWorkflowRun, result.get("run_id"))
            if run is not None:
                art = json.loads(run.artifact_json or "{}")
                art["ticket_ids"] = tids
                run.artifact_json = json.dumps(art, ensure_ascii=False, default=str)
                db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("ticket_ids 落产物失败（不影响运行结果）", exc_info=True)
    return result


def _run_out(r: AgentWorkflowRun) -> dict:
    try:
        steps = json.loads(r.steps_json or "[]")
    except ValueError:
        steps = []
    try:
        artifact = json.loads(r.artifact_json or "{}")
    except ValueError:
        artifact = {}
    return {
        "id": r.id, "workflow_key": r.workflow_key, "session_id": r.session_id,
        "status": r.status, "steps": steps,
        "highlights": artifact.get("highlights") or [],
        "artifact": artifact,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
    }


@router.get("/workflows/runs")
def list_runs(limit: int = Query(20, ge=1, le=100), workflow_key: str = "",
              db: Session = Depends(get_db),
              user: User = Depends(require_roles(*_ROLES))):
    """最近运行记录（steps/产物可回溯）。"""
    q = db.query(AgentWorkflowRun)
    if workflow_key:
        q = q.filter(AgentWorkflowRun.workflow_key == workflow_key)
    rows = q.order_by(AgentWorkflowRun.id.desc()).limit(limit).all()
    return {"items": [_run_out(r) for r in rows], "total": len(rows)}


# ---------------- 自定义工作流定义（admin 管理） ----------------
# steps 契约（custom.validate_steps 硬校验）：[{tool, label, params_template, continue_on_fail}]

class CustomDefIn(BaseModel):
    key: str
    name: str
    description: str = ""
    domain: str = ""
    steps: List[dict] = []
    enabled: bool = True
    overwrite: bool = False   # 幂等导入：同 key 覆盖更新（version+1）


class CustomDefPatch(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    domain: Optional[str] = None
    steps: Optional[List[dict]] = None
    enabled: Optional[bool] = None


class PreviewIn(BaseModel):
    key: str = ""                 # 预览已存定义；空=用下方 draft steps
    steps: Optional[List[dict]] = None
    params: Optional[dict] = None


def _def_http_error(e: Exception) -> HTTPException:
    if isinstance(e, wf_custom.DuplicateKey):
        return HTTPException(409, str(e))
    if isinstance(e, wf_custom.WorkflowDefError):
        return HTTPException(400, str(e))
    if isinstance(e, wf_custom.DefNotFound):
        return HTTPException(404, str(e))
    return HTTPException(500, f"{type(e).__name__}: {str(e)[:150]}")


def _audit_def(db: Session, user: User, action: str, key: str, detail) -> None:
    try:
        audit.record(db, action=action, user_id=user.id, username=user.username,
                     resource_type="workflow_def", resource_id=key, detail=detail)
        db.commit()
    except Exception:  # noqa: BLE001  审计失败不阻断管理动作
        logger.warning("工作流定义审计落库失败（不影响操作）", exc_info=True)


@router.get("/workflows/custom")
def list_custom_defs(db: Session = Depends(get_db),
                     user: User = Depends(require_roles(*_ADMIN))):
    """自定义定义列表（含 steps/version/params_hint——定义可审、非黑盒）。"""
    return {"items": wf_custom.list_defs(db)}


@router.post("/workflows/custom")
def create_custom_def(body: CustomDefIn,
                      db: Session = Depends(get_db),
                      user: User = Depends(require_roles(*_ADMIN))):
    """创建自定义工作流（overwrite=true 幂等覆盖导入）。schema 校验不过 400，key 冲突 409。"""
    try:
        out = wf_custom.create_def(
            db, key=body.key, name=body.name, description=body.description,
            domain=body.domain, steps=body.steps, enabled=body.enabled,
            created_by=user.username, created_by_id=user.id, overwrite=body.overwrite)
    except Exception as e:  # noqa: BLE001  校验类异常统一映射
        raise _def_http_error(e)
    _audit_def(db, user, "agent.workflow.create", out["key"],
               {"name": out["name"], "steps": len(out["steps"]), "overwrite": body.overwrite})
    return out


@router.put("/workflows/custom/{key}")
def update_custom_def(key: str, body: CustomDefPatch,
                      db: Session = Depends(get_db),
                      user: User = Depends(require_roles(*_ADMIN))):
    """修改（version+1）；steps 传了就整表替换。"""
    try:
        out = wf_custom.update_def(
            db, key, name=body.name, description=body.description,
            domain=body.domain, steps=body.steps, enabled=body.enabled,
            updated_by=user.username, updated_by_id=user.id)
    except Exception as e:  # noqa: BLE001
        raise _def_http_error(e)
    _audit_def(db, user, "agent.workflow.update", key,
               {"name": out["name"], "enabled": out["enabled"], "version": out["version"]})
    return out


@router.delete("/workflows/custom/{key}")
def delete_custom_def(key: str,
                      db: Session = Depends(get_db),
                      user: User = Depends(require_roles(*_ADMIN))):
    """删除自定义定义（运行记录保留可回溯）。"""
    try:
        out = wf_custom.delete_def(db, key)
    except Exception as e:  # noqa: BLE001
        raise _def_http_error(e)
    _audit_def(db, user, "agent.workflow.delete", key, {"name": out.get("name", "")})
    return {"deleted": True, "key": key}


@router.post("/workflows/custom/preview")
def preview_custom_def(body: PreviewIn, request: Request,
                       db: Session = Depends(get_db),
                       user: User = Depends(require_roles(*_ADMIN))):
    """预览执行（零副作用）：逐步展开"将调用什么工具、解析出什么参数、是否建工单"。
    不执行任何工具、不建工单；敏感参数值掩码。"""
    steps = body.steps
    if body.key:
        row = next((d for d in wf_custom.list_defs(db) if d["key"] == body.key), None)
        if row is None:
            raise HTTPException(404, f"自定义工作流「{body.key}」不存在")
        steps = row["steps"]
    if not steps:
        raise HTTPException(400, t("wf.steps_empty", lang_of(request)))
    try:
        return wf_custom.preview_steps(steps, body.params or {})
    except wf_custom.WorkflowDefError as e:
        raise HTTPException(400, str(e))


@router.get("/workflows/custom/catalog")
def custom_tool_catalog(db: Session = Depends(get_db),
                        user: User = Depends(require_roles(*_ADMIN))):
    """工具目录（只读工具 + 变更工具两组）：编辑器工具选择器与参数提示的数据源。"""
    return {"items": wf_custom.tool_catalog()}


# ---------------- 版本历史 + 一键回滚 + YAML/JSON 导入导出 ----------------

@router.get("/workflows/custom/{key}/versions")
def list_custom_versions(key: str,
                         db: Session = Depends(get_db),
                         user: User = Depends(require_roles(*_ADMIN))):
    """版本历史（新在前）：version/时间/操作人/步骤数/定义全文 + 当前版标注（current）。"""
    try:
        return wf_custom.list_versions(db, key)
    except Exception as e:  # noqa: BLE001
        raise _def_http_error(e)


@router.post("/workflows/custom/{key}/versions/{version}/restore")
def restore_custom_version(key: str, version: int,
                           db: Session = Depends(get_db),
                           user: User = Depends(require_roles(*_ADMIN))):
    """一键回滚（admin）：恢复为该版本内容 → 当前 version+1 落新行（历史只增），审计。

    幂等：重复恢复同一版本内容收敛不变、各落一条新版本行；历史版本需过当前
    白名单复校（工具下架的历史版本 400 拒绝恢复）。"""
    try:
        out = wf_custom.restore_version(db, key, version, by=user.username, by_id=user.id)
    except Exception as e:  # noqa: BLE001
        raise _def_http_error(e)
    _audit_def(db, user, "agent.workflow.restore", key,
               {"restored_from": version, "version": out.get("version"),
                "name": out.get("name", "")})
    return out


class ImportIn(BaseModel):
    content: str = Field(..., min_length=1, max_length=500_000)   # YAML 或 JSON 文本
    overwrite: bool = False   # 同 key 覆盖更新（version+1）；不带则 key 冲突 409


@router.post("/workflows/custom/import")
def import_custom_defs(body: ImportIn,
                       db: Session = Depends(get_db),
                       user: User = Depends(require_roles(*_ADMIN))):
    """导入自定义工作流（YAML/JSON 双格式；schema 校验同创建，key 冲突 409+overwrite 语义照抄）。

    载荷兼容导出包 {workflows:[...]} / 定义数组 / 单条定义对象；全量先校验后写入
    （要么全进要么全不进）。敏感占位符 {{运行参数}} 原样往返，凭证仍不落定义。"""
    try:
        obj = wf_def_io.loads_def_text(body.content)
        entries = wf_def_io.extract_defs(obj)
        out = wf_custom.import_defs(db, entries, overwrite=body.overwrite,
                                    by=user.username, by_id=user.id)
    except wf_def_io.DefFormatError as e:
        raise HTTPException(400, f"导入内容解析失败：{e}")
    except Exception as e:  # noqa: BLE001
        raise _def_http_error(e)
    for d in out["items"]:
        _audit_def(db, user, "agent.workflow.import", d["key"],
                   {"name": d.get("name", ""), "steps": len(d.get("steps") or []),
                    "version": d.get("version"), "overwrite": body.overwrite})
    return out


@router.get("/workflows/custom/{key}/export")
def export_custom_def(key: str, format: str = Query("yaml"),
                      db: Session = Depends(get_db),
                      user: User = Depends(require_roles(*_ADMIN))):
    """导出定义（yaml 或 json 两格式都提供）：导出包形态 {format,exported_at,workflows:[…]}。

    载荷含步骤/参数模板/失败策略（continue_on_fail）；敏感参数占位符 {{运行参数}}
    原样导出（定义本就不含凭证）。PyYAML 不引新依赖——YAML 为 JSON 兼容子集
    手工序列化（def_io），导入端同解析器闭环。"""
    fmt = (format or "yaml").strip().lower()
    if fmt not in ("yaml", "json"):
        raise HTTPException(400, "format 只支持 yaml / json")
    try:
        out = wf_custom.get_def(db, key)
    except Exception as e:  # noqa: BLE001
        raise _def_http_error(e)
    envelope = wf_def_io.export_envelope([wf_def_io.payload_from_out(out)])
    if fmt == "json":
        text = json.dumps(envelope, ensure_ascii=False, indent=2) + "\n"
        media = "application/json"
    else:
        text = wf_def_io.dumps_yaml(envelope)
        media = "application/yaml"
    return Response(
        content=text, media_type=f"{media}; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="qianshu-workflow-{key}.{fmt}"'})
