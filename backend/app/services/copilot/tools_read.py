"""只读工具注册表：包装 ai_tools/diag_tools，零改动复用。

- 合并两处 TOOLS 成统一清单：name / desc / params / schema / source / scenarios。
- execute() 按来源分发到各自的 execute_tool——**不修改** ai_tools.py / diag_tools.py 本体。
- 全部只读：智能体只读查询的工具通道，不提供任意 SQL/HTTP/shell 工具。
- platform_api_query：任意平台 **GET**（只读）——只允许 openapi 注册路径、
  黑名单恒拒（platform_api._BLACKLIST）、权限随会话用户（execute 的 user 可选参数；
  缺省=viewer 只读档）。
- data_completeness_check：包装 services/data_advisor，做六面数据覆盖体检
  （真实探测，零写库）；onboard 工作流 readiness 步骤可直接引用。
"""
from __future__ import annotations

import json

from app.services import ai_tools, diag_tools

# 工具结果入上下文的截断长度（设计 §六：4KB；完整语义看结果摘要，长尾靠追问下钻）
RESULT_MAX_CHARS = 4096

# ai_tools 自带 4 工具的场景标签（diag_tools 自带 scenarios 原样透传）
_AI_SCENARIOS = {
    "query_assets": ("资产", "query"),
    "query_metric": ("监控", "query"),
    "query_cost": ("成本", "query"),
    "query_alert_history": ("告警", "query"),
}

# 平台只读接口工具：任意平台 GET，但黑名单与越权请求恒拒
_PLATFORM_QUERIES = {
    "platform_api_query": {
        "desc": "查询千枢平台自身接口（任意 GET，只读）：path=平台 API 路径（如 /api/dashboard/summary、"
                "/api/users）。只允许 openapi 注册路径；危险端点黑名单恒拒；权限随会话用户（viewer 只读）",
        "params": {
            "method": "固定 GET（只读；写操作走变更工具 platform_api_call）",
            "path": "平台 API 路径，如 /api/dashboard/summary 或 /api/audit/logs?page=1",
        },
    },
}

# 数据完整性顾问：包装 services/data_advisor，无参数只读体检
_ADVISOR_TOOLS = {
    "data_completeness_check": {
        "desc": "数据完整性体检（只读，无参数）：全平台数据覆盖六面真实探测——应用日志/指标"
                "（CMS/VMware/Agent/K8s 覆盖哪些机器）/账单采集健康与实例级对账/告警规则覆盖/"
                "画像拓扑链路未生成未确认/证书域名缺登记，输出完整度得分 0-100 与缺口建议"
                "（每条带 impact/怎么补/跳转入口）。用户问『还缺什么数据/怎么接入/数据全不全』"
                "时先跑它",
        "params": {},
    },
}


def _schema(params: dict) -> dict:
    """params 描述表 → 与工作流/变更工具同构的 JSON-Schema 风格。"""
    return {
        "type": "object",
        "properties": {
            k: {"type": "string", "description": str(v)} for k, v in (params or {}).items()
        },
    }


def catalog() -> list:
    """统一工具清单（顺序稳定：ai_tools 在前，diag_tools 按注册顺序）。"""
    out = []
    for name, spec in ai_tools.TOOLS.items():
        out.append({
            "name": name,
            "desc": spec.get("desc", ""),
            "params": dict(spec.get("params") or {}),
            "schema": _schema(spec.get("params") or {}),
            "source": "ai_tools",
            "scenarios": list(_AI_SCENARIOS.get(name, ("query",))),
        })
    for name, spec in diag_tools.TOOLS.items():
        out.append({
            "name": name,
            "desc": spec.get("desc", ""),
            "params": dict(spec.get("params") or {}),
            "schema": _schema(spec.get("params") or {}),
            "source": "diag_tools",
            "scenarios": list(spec.get("scenarios") or ("diag",)),
        })
    for name, spec in _PLATFORM_QUERIES.items():
        out.append({
            "name": name,
            "desc": spec.get("desc", ""),
            "params": dict(spec.get("params") or {}),
            "schema": _schema(spec.get("params") or {}),
            "source": "platform",
            "scenarios": ["platform", "query"],
        })
    for name, spec in _ADVISOR_TOOLS.items():
        out.append({
            "name": name,
            "desc": spec.get("desc", ""),
            "params": dict(spec.get("params") or {}),
            "schema": _schema(spec.get("params") or {}),
            "source": "data_advisor",
            "scenarios": ["data", "query"],
        })
    return out


def tool_names() -> list:
    return [t["name"] for t in catalog()]


def tool_prompt_block() -> str:
    """给 LLM 系统提示用的工具表（紧凑：名/描述/参数）。"""
    lines = []
    for t in catalog():
        params = "; ".join(f"{k}: {v}" for k, v in t["params"].items()) or "（无参数）"
        lines.append(f"- {t['name']}: {t['desc']}\n  params: {params}")
    return "\n".join(lines)


def params_summary(params: dict) -> str:
    try:
        s = json.dumps(params or {}, ensure_ascii=False, default=str)
    except Exception:
        s = str(params)
    return s[:300]


def result_summary(result, error: str = "") -> str:
    """工具结果摘要：JSON 截断 4KB 入上下文/轨迹，控制上下文与 token 消耗。"""
    if error:
        return f"执行失败：{error}"[:RESULT_MAX_CHARS]
    try:
        s = json.dumps(result, ensure_ascii=False, default=str)
    except Exception:
        s = str(result)
    if len(s) > RESULT_MAX_CHARS:
        return s[:RESULT_MAX_CHARS] + f"...（已截断，共{len(s)}字符）"
    return s


def execute(db, name: str, params: dict, user=None) -> dict:
    """执行一个只读工具（按来源分发）。未知工具/参数错误抛 ToolError。

    user 可选：会话用户（platform_api_query 按其角色做权限校验；缺省=viewer 只读档）。
    """
    name = (name or "").strip()
    if not isinstance(params, dict):
        raise ai_tools.ToolError("params 必须是对象")
    if name == "platform_api_query":
        return _platform_api_query(db, params, user)
    if name in _ADVISOR_TOOLS:
        from app.services import data_advisor
        return data_advisor.check(db)
    if name in ai_tools.TOOLS:
        return ai_tools.execute_tool(db, name, params)
    if name in diag_tools.TOOLS:
        return diag_tools.execute_tool(db, name, params)
    raise ai_tools.ToolError(f"未知工具 {name}，可选: {', '.join(tool_names())}")


def _platform_api_query(db, params: dict, user=None) -> dict:
    """任意平台 GET（只读）：openapi 注册路径 + 黑名单恒拒 + 权限随会话用户。"""
    from app.services.copilot import platform_api

    method = str(params.get("method") or "GET").upper()
    if method != "GET":
        return {"error": f"platform_api_query 只读（GET），收到 {method}——写操作走变更工具 platform_api_call"}
    path = str(params.get("path") or "").strip()
    if not path:
        return {"error": "path 不能为空（平台 API 路径，如 /api/dashboard/summary）"}
    try:
        return platform_api.call_get(path, db=db, user=user)
    except platform_api.PlatformApiError as e:
        return {"error": f"拒绝执行：{e}"}
    except Exception as e:   # noqa: BLE001  查询失败如实报错，不打断对话
        return {"error": f"{type(e).__name__}: {str(e)[:150]}"}


ToolError = ai_tools.ToolError
