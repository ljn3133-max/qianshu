#!/usr/bin/env python3
"""MCP tools JSON 导出：python3 scripts/gen_mcp_tools.py > agent/mcp/qianshu-tools.json"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.mcp_export import build_mcp_tools

if __name__ == "__main__":
    print(json.dumps(build_mcp_tools(), ensure_ascii=False, indent=2))
