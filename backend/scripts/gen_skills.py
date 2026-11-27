#!/usr/bin/env python3
"""Skill 文件生成器：每个只读诊断工具一个 Markdown 文件 + 汇总。

    python3 scripts/gen_skills.py          # 生成到 agent/skills/<tool>.md
与 MCP/OpenAPI 同源（工具注册表）——能力三件套的 Skill 形态落盘。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.mcp_export import build_skills  # noqa: E402


TPL = """# Skill: {title}

> 千枢能力 · 场景: {scenarios} · 与 MCP 工具/API 同源生成

## 触发
用户问到：{trigger}

## 用法
`{usage}`

## 示例
```json
{example}
```

## 口径与边界
- 本清单工具仅用于只读取证；LLM 只产出工具名+参数，由代码白名单校验执行
- 本清单不提供外部写操作；平台其他受控写操作必须经过角色权限、明确确认、审计与回滚
- REST 等价: {api}
"""

if __name__ == "__main__":
    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "agent", "skills")
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    data = build_skills()
    for s in data["skills"]:
        path = os.path.join(out_dir, f"{s['name']}.md")
        with open(path, "w") as f:
            f.write(TPL.format(
                title=s["title"] or s["name"], scenarios=", ".join(s["scenarios"]) or "通用",
                trigger=s["trigger"], usage=s["usage"],
                example=s["example"], api=s["api"]))
    # 汇总 SKILL 索引
    with open(os.path.join(out_dir, "README.md"), "w") as f:
        f.write("# 千枢 Skill 清单（%d 条，按功能生成）\n\n" % data["count"])
        f.write("| Skill | 干什么 | 场景 |\n|---|---|---|\n")
        for s in data["skills"]:
            f.write(f"| [{s['name']}]({s['name']}.md) | {s['title']} | {', '.join(s['scenarios']) or '通用'} |\n")
    print(f"生成 {data['count']} 个 SKILL.md + README → {out_dir}")
