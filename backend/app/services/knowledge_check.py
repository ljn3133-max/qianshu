"""知识包一致性检查。

知识包 = playbooks.py（KNOWLEDGE_VERSION + DOCTRINE + PLAYBOOKS）+
agent/knowledge-changelog.md（变更日志）+ agent/skills/*.md（Skill 文档）+
agent/mcp/qianshu-tools.json（MCP 导出快照）+ agent/knowledge/*.md（自改经验沉淀，
由 code apply 自动追加）。检查四类一致性：

1. 版本号：playbooks.KNOWLEDGE_VERSION ↔ 变更日志最新条目（含"（当前）"标记）；
2. 变更日志条目：当前版本在日志里必须有条目（缺 → 给补写建议，不代改）；
3. 工具/守则引用：PLAYBOOKS 的 tools/evidence_chain、skill 文件名与内文引用的
   工具名必须存在于 diag_tools.TOOLS；PLAYBOOKS.doctrine 必须存在于 DOCTRINE——
   引用不存在的工具/守则 = 不一致；
4. 接口引用：知识文件里的 "METHOD /api/..." 必须是 FastAPI 真实路由——
   引用不存在的 API = 不一致（skill 生成模板里的 REST 等价行也在此查）。

输出：GET /api/tools/knowledge/check 检查报告。能自动修的（补变更日志条目、
重跑 gen_skills.py / gen_mcp_tools.py）只列建议**不自动改**（人工确认制）。
只读检查：不改文件、不写库。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

# snake_case 形态的工具引用（必须带下划线，过滤掉 data/status 之类普通词）
_SNAKE_RE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+")
_HEAD_REF_RE = re.compile(r"([a-z][a-z0-9_]+)\s*[：:]")     # 紧贴冒号前的引用
_API_REF_RE = re.compile(r"\b(GET|POST|PUT|DELETE)\s+(/[^\s{\"'`\\]+)")
_CHANGELOG_HEAD_RE = re.compile(r"^##\s+(\S+?)（当前）?\s*$", re.M)
_SKILL_TOOL_RE = re.compile(r"工具\s+(\w+)，")
_SKILL_API_TOOL_RE = re.compile(r'["{]\s*tool"?[:\s]+"(\w+)"')
_MD_LINK_RE = re.compile(r"\]\(([^)]+\.md)\)")
_README_COUNT_RE = re.compile(r"（(\d+)\s*条")


def _tool_refs(text: str) -> set:
    """抽取文本里的工具名引用（评估与检查同口径）：冒号前的 snake 名 + 首个冒号前
    整段里的 snake 名（覆盖 'tool_a + tool_b：...' 形态）。"""
    refs = set(_HEAD_REF_RE.findall(text))
    refs |= set(_SNAKE_RE.findall(text.split("：")[0].split(":")[0]))
    return refs


def _playbook_refs(PLAYBOOKS: dict) -> tuple:
    """PLAYBOOKS 的显式 tools 列表 + evidence_chain 文本引用。"""
    declared, chain = set(), set()
    for key, pb in (PLAYBOOKS or {}).items():
        declared |= set(pb.get("tools") or ())
        for step in pb.get("evidence_chain") or ():
            chain |= _tool_refs(str(step))
    return declared, chain


def run_check(root: str = None, routes: set = None, playbooks_mod=None) -> dict:
    """跑知识包一致性检查。返回检查报告（只读）。

    - root：知识包根目录（默认千枢仓库根）；agent/ 在其下；
    - routes：FastAPI 路由集合（"METHOD /path"），None=接口检查跳过；
    - playbooks_mod：知识模块覆盖（默认 app.services.playbooks，测试可注入样例）。
    """
    root_p = Path(root or Path(__file__).resolve().parents[3])
    agent = root_p / "agent"
    if playbooks_mod is None:
        from app.services import playbooks as playbooks_mod  # noqa: PLC0415
    from app.services.diag_tools import TOOLS  # noqa: PLC0415  真实工具注册表=唯一事实源

    version = str(getattr(playbooks_mod, "KNOWLEDGE_VERSION", ""))
    PLAYBOOKS = getattr(playbooks_mod, "PLAYBOOKS", {}) or {}
    DOCTRINE = getattr(playbooks_mod, "DOCTRINE", {}) or {}
    tool_names = set(TOOLS)

    checks: list = []
    inconsistencies: list = []
    suggestions: list = []

    def add_check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    # ---- 1. 版本号 + 变更日志条目 ----
    changelog_p = agent /"knowledge-changelog.md"
    entries = []
    if changelog_p.exists():
        for m in _CHANGELOG_HEAD_RE.finditer(changelog_p.read_text(encoding="utf-8")):
            entries.append(m.group(1))
    else:
        inconsistencies.append({"kind": "changelog_missing_file", "where": "agent/knowledge-changelog.md",
                                "detail": "变更日志文件不存在"})
    if entries:
        top = entries[0]
        ok = top == version
        add_check("版本号一致（KNOWLEDGE_VERSION ↔ 变更日志最新条目）", ok,
                  f"KNOWLEDGE_VERSION={version}，变更日志最新条目={top}")
        if not ok:
            inconsistencies.append({"kind": "version_mismatch", "where": "playbooks.KNOWLEDGE_VERSION",
                                    "detail": f"版本 {version} 与变更日志最新条目 {top} 不一致（改知识必须滚版本并登记）"})
    else:
        add_check("版本号一致（KNOWLEDGE_VERSION ↔ 变更日志最新条目）", False,
                  f"KNOWLEDGE_VERSION={version}，变更日志无条目")
    if version and version not in entries:
        add_check("变更日志条目（当前版本有登记）", False, f"变更日志缺 {version} 条目")
        inconsistencies.append({"kind": "changelog_entry_missing", "where": "agent/knowledge-changelog.md",
                                "detail": f"当前知识版本 {version} 在变更日志里没有条目"})
        suggestions.append({
            "kind": "changelog_entry", "where": "agent/knowledge-changelog.md",
            "detail": f"缺 {version} 的变更日志条目",
            "fix": f"在 agent/knowledge-changelog.md 顶部补一条 `## {version}（当前）` 并列出本次知识变更"
                   "（本检查只列建议，不代改）",
        })
    elif version:
        add_check("变更日志条目（当前版本有登记）", True, f"{version} 已登记")

    # ---- 2. 工具/守则引用 ----
    declared, chain = _playbook_refs(PLAYBOOKS)
    bad_tools = sorted(t for t in (declared | chain) if t not in tool_names)
    ok = not bad_tools
    add_check("PLAYBOOKS 工具引用（tools/evidence_chain ↔ diag_tools.TOOLS）", ok,
              f"引用 {len(declared | chain)} 个工具名，无效 {len(bad_tools)} 个")
    for t in bad_tools:
        where = sorted(k for k, pb in PLAYBOOKS.items()
                       if t in (pb.get("tools") or ()) or any(t in _tool_refs(str(s)) for s in (pb.get("evidence_chain") or ())))
        inconsistencies.append({"kind": "tool_ref", "where": f"playbooks.PLAYBOOKS[{','.join(where)}]",
                                "detail": f"引用了不存在的工具 `{t}`（TOOLS 共 {len(tool_names)} 个）"})

    bad_doctrine = sorted({d for pb in PLAYBOOKS.values() for d in (pb.get("doctrine") or ()) if d not in DOCTRINE})
    ok = not bad_doctrine
    add_check("PLAYBOOKS 守则引用（doctrine ↔ DOCTRINE）", ok, f"无效守则引用 {len(bad_doctrine)} 个")
    for d in bad_doctrine:
        inconsistencies.append({"kind": "doctrine_ref", "where": "playbooks.PLAYBOOKS[*].doctrine",
                                "detail": f"引用了不存在的守则 `{d}`"})

    # ---- 3. skill 文件（agent/skills）----
    skills_dir = agent /"skills"
    skill_files = sorted(p for p in skills_dir.glob("*.md") if p.name != "README.md") if skills_dir.exists() else []
    file_stems = {p.stem for p in skill_files}
    bad_refs = []   # (file, ref)
    for p in skill_files:
        text = p.read_text(encoding="utf-8")
        # 内文自称的工具引用（"工具 X，" / {"tool": "X"}）；生成文件（与 MCP/API
        # 同源生成）另按"文件名=工具名"约定查文件名。手写方法论 skill（如
        # asset-topology.md）不套用文件名约定，避免误报。
        refs = set(_SKILL_TOOL_RE.findall(text)) | set(_SKILL_API_TOOL_RE.findall(text))
        if "同源生成" in text:
            refs.add(p.stem)
        for r in sorted(refs):
            if r not in tool_names:
                bad_refs.append((p.name, r))
    dead_links: list = []
    if skills_dir.exists():
        readme = skills_dir / "README.md"
        if readme.exists():
            text = readme.read_text(encoding="utf-8")
            dead_links = [l for l in _MD_LINK_RE.findall(text) if not (skills_dir / l).exists()]
            m = _README_COUNT_RE.search(text)
            # README 由生成器按工具数出条数——只与"生成的工具 skill"数对账，
            # 手写方法论 skill 不计入（gen_skills.py 也不会生成/删除它们）
            gen_count = sum(1 for p in skill_files if p.stem in tool_names)
            if m and int(m.group(1)) != gen_count:
                suggestions.append({
                    "kind": "regen_skills", "where": "agent/skills/README.md",
                    "detail": f"README 声明 {m.group(1)} 条，生成类 skill 实际 {gen_count} 条",
                    "fix": "重跑 python3 backend/scripts/gen_skills.py 重新生成（本检查不代改）",
                })
    missing_files = sorted(tool_names - file_stems)
    if missing_files:
        suggestions.append({
            "kind": "regen_skills", "where": "agent/skills/",
            "detail": f"{len(missing_files)} 个工具缺 skill 文件：{', '.join(missing_files[:8])}"
                      + ("…" if len(missing_files) > 8 else ""),
            "fix": "重跑 python3 backend/scripts/gen_skills.py 重新生成（本检查不代改）",
        })
    ok = not bad_refs and not dead_links
    add_check("Skill 工具引用（内文 + 生成文件名约定）", ok,
              f"{len(skill_files)} 个 skill 文件，无效引用 {len(bad_refs)} 处、死链 {len(dead_links)} 处")
    for fname, ref in bad_refs:
        inconsistencies.append({"kind": "tool_ref", "where": f"agent/skills/{fname}",
                                "detail": f"引用了不存在的工具 `{ref}`"})
    for link in dead_links:
        inconsistencies.append({"kind": "skill_link", "where": "agent/skills/README.md",
                                "detail": f"README 索引死链 `{link}`（指向不存在的文件）"})

    # ---- 4. MCP 导出快照（agent/mcp）----
    mcp_p = agent /"mcp" / "qianshu-tools.json"
    if mcp_p.exists():
        try:
            mcp = json.loads(mcp_p.read_text(encoding="utf-8"))
            mcp_names = {t.get("name") for t in (mcp.get("tools") or []) if isinstance(t, dict)}
            allowed = tool_names | {"topology_arch"}
            ghost = sorted(n for n in mcp_names if n not in allowed)
            ok = not ghost
            add_check("MCP 导出快照 ↔ 工具注册表", ok,
                      f"{len(mcp_names)} 个工具，无效 {len(ghost)} 个")
            for n in ghost:
                inconsistencies.append({"kind": "tool_ref", "where": "agent/mcp/qianshu-tools.json",
                                        "detail": f"导出快照含不存在的工具 `{n}`"})
            try:
                from app.services.mcp_export import build_mcp_tools  # noqa: PLC0415
                live = {t["name"] for t in build_mcp_tools().get("tools", [])}
                if live != mcp_names:
                    suggestions.append({
                        "kind": "regen_mcp", "where": "agent/mcp/qianshu-tools.json",
                        "detail": f"快照与注册表现状不一致（快照 {len(mcp_names)} 个 / 现状 {len(live)} 个）",
                        "fix": "重跑 python3 backend/scripts/gen_mcp_tools.py > agent/mcp/qianshu-tools.json（本检查不代改）",
                    })
            except Exception as e:  # 注册表导入失败不影响名字检查
                add_check("MCP 导出快照 ↔ 注册表现状", True, f"比对跳过（注册表加载失败: {str(e)[:60]}）")
        except ValueError as e:
            inconsistencies.append({"kind": "mcp_json_invalid", "where": "agent/mcp/qianshu-tools.json",
                                    "detail": f"JSON 解析失败: {str(e)[:80]}"})
    else:
        add_check("MCP 导出快照 ↔ 工具注册表", False, "agent/mcp/qianshu-tools.json 不存在")
        suggestions.append({
            "kind": "regen_mcp", "where": "agent/mcp/qianshu-tools.json",
            "detail": "MCP 导出快照缺失",
            "fix": "运行 python3 backend/scripts/gen_mcp_tools.py > agent/mcp/qianshu-tools.json（本检查不代改）",
        })

    # ---- 5. 接口引用 ↔ FastAPI 真实路由（只扫知识文件：skills / mcp 快照 / 变更日志 /
    #      自改经验（agent/knowledge/*.md），不扫 docs/ 下的方案与评审
    #      文档——那是设计稿不是知识包）----
    api_refs = []   # (where, "METHOD /path")
    scan_files = [p for p in skill_files]
    if skills_dir.exists() and (skills_dir / "README.md").exists():
        scan_files.append(skills_dir / "README.md")
    if mcp_p.exists():
        scan_files.append(mcp_p)
    if changelog_p.exists():
        scan_files.append(changelog_p)
    knowledge_dir = agent /"knowledge"
    if knowledge_dir.exists():
        scan_files.extend(sorted(knowledge_dir.glob("*.md")))
    for p in scan_files:
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for m in _API_REF_RE.finditer(text):
            # 文档里的引用常带查询串（?redact=0|1）——与路由对账前先剥掉
            path = m.group(2).split("?")[0].split("#")[0]
            if path.count("/") > 1:   # 至少 /api/xxx 两段，过滤 "/scan/status" 之类相对片段
                api_refs.append((str(p.relative_to(root_p)), f"{m.group(1)} {path}"))
    seen = set()
    api_refs = [x for x in api_refs if not (x in seen or seen.add(x))]   # 去重保序
    if routes is None:
        add_check("接口引用 ↔ FastAPI 路由", True, "跳过（未注入路由表）")
    else:
        bad_api = [(w, ref) for w, ref in api_refs if ref not in routes]
        ok = not bad_api
        add_check("接口引用 ↔ FastAPI 路由", ok,
                  f"知识文件引用接口 {len({r for _, r in api_refs})} 个，"
                  f"无效 {len({r for _, r in bad_api})} 个")
        by_ref: dict = {}
        for w, ref in bad_api:
            by_ref.setdefault(ref, []).append(w)
        for ref, wheres in by_ref.items():
            where = wheres[0] if len(wheres) == 1 else f"{wheres[0]} 等 {len(wheres)} 处"
            inconsistencies.append({"kind": "api_ref", "where": where,
                                    "detail": f"引用了不存在的接口 `{ref}`（知识在说谎：照着调会 404）"})

    n_ok = sum(1 for c in checks if c["ok"])
    return {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "knowledge_version": version,
        "summary": {
            "checks": len(checks), "ok": n_ok,
            "inconsistencies": len(inconsistencies), "suggestions": len(suggestions),
        },
        "checks": checks,
        "inconsistencies": inconsistencies,
        "suggestions": suggestions,
    }
