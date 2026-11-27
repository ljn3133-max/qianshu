"""MCP 能力导出器：工具注册表 → MCP tools schema。

脚本（scripts/gen_mcp_tools.py）与 API（/api/tools/mcp）双端共用。
产品红线：导出的工具全部只读注解（readOnlyHint）。
"""

import json
import os
import sys

from app.services.diag_tools import TOOLS


def build_mcp_tools() -> dict:
    tools = []
    for name, spec in sorted(TOOLS.items()):
        params = spec.get("params") or {}
        tools.append({
            "name": name,
            "description": spec.get("desc", ""),
            "inputSchema": {
                "type": "object",
                "properties": {k: {"type": "string", "description": v}
                               for k, v in params.items()},
                "additionalProperties": False,
            },
            "annotations": {
                "readOnlyHint": True,          # 千枢 MCP 工具全部只读
                "scenarios": list(spec.get("scenarios") or ()),
            },
        })
    # 拓扑能力（架构图数据）
    tools.append({
        "name": "topology_arch",
        "description": "资产架构图数据：分层分组（接入层/网关层/应用层/大数据/中间件/数据库/第三方）"
                       " + 组件/集群框 + 依赖连线 + 数据新鲜度。redact=1 为脱敏版（对外）",
        "inputSchema": {
            "type": "object",
            "properties": {"redact": {"type": "string", "description": "1=脱敏版，默认 0"}},
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "scenarios": ["topology"]},
    })
    return {
        "server": "qianshu",
        "version": "0.1",
        "principles": ["MCP 导出工具全部只读", "LLM 只产出工具名+参数，代码侧白名单执行",
                       "外部写操作不在 MCP 导出范围，必须另走角色权限、明确确认、审计与回滚"],
        "tools": tools,
    }


def build_skills() -> dict:
    """按工具注册表生成 Skill 清单，保证 MCP、REST 与文档使用同一份能力说明。"""
    from app.services.diag_tools import TOOLS

    skills = []
    for name, spec in sorted(TOOLS.items()):
        params = spec.get("params") or {}
        scenarios = list(spec.get("scenarios") or ())
        param_str = ", ".join(f"{k}（{v}）" for k, v in params.items())
        example_args = ", ".join(f'"{k}": "..."' for k in params)
        desc = str(spec.get("desc") or "").strip()
        skills.append({
            "name": name,
            # 标题取完整首句，摘要保留完整说明；固定字符数会截出半句或丢失参数约束。
            "title": desc.split("。", 1)[0].strip(),
            "scenarios": scenarios,
            "trigger": desc,
            "params": params,
            "usage": f"工具 {name}，参数：{param_str}" if params else f"工具 {name}，无参数",
            "example": f'{name}({{{example_args}}})' if params else f"{name}()",
            "api": f"POST /api/ai/tools/execute {{\"tool\": \"{name}\", \"params\": {{...}}}}",
        })
    return {
        "count": len(skills),
        "principles": ["本清单工具全部只读取证", "LLM 只产出工具名+参数，白名单执行",
                       "外部写操作不在本清单范围，必须另走角色权限、明确确认、审计与回滚"],
        "skills": skills,
    }
