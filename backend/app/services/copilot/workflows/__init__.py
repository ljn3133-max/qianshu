"""内置工作流库：接入域 + 分析域 + 改进域 + 引导域。

对外契约（loop / api 层调用）：
  WORKFLOWS: dict[key -> {name, domain, description, params_schema, ...}]
  run_workflow(db, key, params, context) ->
      {"run_id", "status", "steps": [{step,status,detail,ms}], "artifact": dict, "tickets": [...]}

  context 含 user / session_id；artifact={report_md, highlights, ...}（报告 markdown+要点数组）。

安全边界：
  - 变更步骤只建待确认工单（engine.change_step -> change_tools.build_ticket），
    执行由用户在变更确认卡完成；变更层未就绪时步骤标"待确认变更（变更层未就绪）"继续
  - 分析取数全部走只读工具（tools_read.execute）/只读 ORM；不执行 LLM/脚本原文
  - 凭证不落步骤 detail / 产物 / 运行记录
"""
from __future__ import annotations

from app.services.copilot.workflows import engine
from app.services.copilot.workflows.access import ACCESS_WORKFLOWS
from app.services.copilot.workflows.analysis import ANALYSIS_WORKFLOWS
from app.services.copilot.workflows.engine import (
    ChangeLayerUnavailable,
    ParamError,
    STEP_FAILED,
    STEP_OK,
    STEP_SKIPPED,
    WorkflowNotFound,
    build_change_ticket,
    validate_params,
)
from app.services.copilot.workflows.improve import IMPROVE_WORKFLOWS
from app.services.copilot.workflows.onboard import ONBOARD_WORKFLOWS

# key -> spec（name/domain/description/params_schema 为契约字段，steps/build_artifact 为引擎私有）
# 双语载体：name/domain/description = {"zh":..,"en":..}；
# 出口按 Accept-Language 选文（app.core.i18n_msg.pick），逻辑/持久化固定 pick(x, "zh") 保持原行为。
WORKFLOWS: dict = {}
WORKFLOWS.update(ACCESS_WORKFLOWS)
WORKFLOWS.update(ANALYSIS_WORKFLOWS)
WORKFLOWS.update(IMPROVE_WORKFLOWS)
WORKFLOWS.update(ONBOARD_WORKFLOWS)   # 引导域（onboard_init AI 引导初始化）


def run_workflow(db, key: str, params: dict, context=None) -> dict:
    """执行一个内置工作流。未知 key -> WorkflowNotFound；参数不过 -> ParamError。

    context 可缺省（loop 三参调用兼容）：{user, user_id, session_id, msg_id}。
    """
    spec = WORKFLOWS.get(key)
    if spec is None:
        # 自定义工作流：key 不在内置注册表时
        # 回落查 workflow_defs 并编译成 spec（只加不改：内置注册表零改动）。
        # 不存在/已停用 -> WorkflowNotFound（与未知 key 同一契约）。
        from app.services.copilot.workflows import custom as _custom
        spec = _custom.load_spec(db, key)
    return engine.execute(db, spec, key, params or {}, context or {})


__all__ = [
    "WORKFLOWS",
    "run_workflow",
    "validate_params",
    "build_change_ticket",
    "ParamError",
    "WorkflowNotFound",
    "ChangeLayerUnavailable",
    "STEP_OK",
    "STEP_FAILED",
    "STEP_SKIPPED",
]
