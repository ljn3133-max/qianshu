"""MCP Server 协议端点：集成中心配置的 /api/mcp 可直接连接。

实现 MCP Streamable HTTP（JSON-RPC 2.0 over POST）最小协议面：
  initialize / tools/list / tools/call / ping
鉴权：Authorization: Bearer <token>（与 REST 同一 token）。
Claude Desktop / Claude Code / 任何 MCP 客户端配置 url=<千枢>/api/mcp 即可直连。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.user import User

logger = logging.getLogger("qianshu.mcp_server")
router = APIRouter(tags=["mcp"])

PROTOCOL_VERSION = "2025-03-26"
SERVER_INFO = {"name": "qianshu", "version": "0.3.1"}


@router.post("/mcp")
async def mcp_endpoint(request: Request, db: Session = Depends(get_db)):
    """MCP JSON-RPC 端点。先鉴权（Bearer），再按 method 分发。"""
    # 鉴权：与 REST 相同 token（MCP 客户端在 headers 里带 Authorization）
    from app.core.deps import get_current_user as _gcu
    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(401, "MCP 需要 Authorization: Bearer <token>")
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=auth_header[7:])
    try:
        _gcu(creds, db)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(401, "token 无效")

    try:
        msg = await request.json()
    except Exception:
        raise HTTPException(400, "非法 JSON-RPC")

    method = msg.get("method", "")
    mid = msg.get("id")
    params = msg.get("params") or {}

    def ok(result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def err(code: int, text: str) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": text}}

    if method == "initialize":
        return ok({
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        })
    if method == "ping":
        return ok({})
    if method == "notifications/initialized":
        return {"jsonrpc": "2.0"}   # 通知无 id
    if method == "tools/list":
        from app.services.mcp_export import build_mcp_tools

        tools = build_mcp_tools()["tools"]
        return ok({"tools": [{"name": t["name"], "description": t["description"],
                              "inputSchema": t["inputSchema"]} for t in tools]})
    if method == "tools/call":
        from app.services.diag_tools import TOOLS, execute_tool

        name = params.get("name", "")
        args = params.get("arguments") or {}
        if name == "topology_arch":
            from app.services.topology import build_arch

            data = build_arch(db, redact=str(args.get("redact", "0")) == "1")
            return ok({"content": [{"type": "text", "text": __import__("json").dumps(data, ensure_ascii=False)[:20000]}],
                       "isError": False})
        if name not in TOOLS:
            return err(-32602, f"未知工具: {name}")
        try:
            result = execute_tool(db, name, args)
            return ok({"content": [{"type": "text", "text": __import__("json").dumps(result, ensure_ascii=False)[:20000]}],
                       "isError": False})
        except Exception as e:
            return ok({"content": [{"type": "text", "text": f"工具执行失败: {str(e)[:300]}"}],
                       "isError": True})
    return err(-32601, f"未知方法: {method}")
