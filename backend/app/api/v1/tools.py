"""能力清单 API：工具注册表 → MCP tools JSON（Agent 经 MCP 消费千枢能力）。

GET /tools/mcp             MCP tools schema（与 scripts/gen_mcp_tools.py 同源）
GET /tools/skills          Skill 清单（每工具一条）
GET /tools/knowledge/check 知识包一致性检查报告（只读）
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request

from app.core.deps import get_current_user
from app.models.user import User

logger = logging.getLogger("qianshu.tools_api")
router = APIRouter(prefix="/tools", tags=["tools"])


@router.get("/mcp")
def mcp_tools(user: User = Depends(get_current_user)):
    from app.services.mcp_export import build_mcp_tools

    return build_mcp_tools()


@router.get("/skills")
def mcp_skills(user: User = Depends(get_current_user)):
    """按功能生成的 Skill 清单（每工具一条：触发/参数/示例/口径）。"""
    from app.services.mcp_export import build_skills

    return build_skills()


@router.get("/knowledge/check")
def knowledge_check(request: Request, user: User = Depends(get_current_user)):
    """知识包一致性检查：版本号/变更日志条目/知识文件引用的
    工具名与接口是否与真实注册表、真实路由对得上（引用不存在的 API/工具=不一致）。

    只读报告：能自动修的（补变更日志条目、重跑生成脚本）只列建议，不自动改。
    """
    from app.services.knowledge_check import run_check

    routes = {
        f"{method} {route.path}"
        for route in request.app.routes
        if hasattr(route, "path") and hasattr(route, "methods")
        for method in route.methods
        if method not in ("HEAD", "OPTIONS")
    }
    return run_check(routes=routes)
