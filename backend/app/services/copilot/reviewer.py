"""审查智能体：主回答与修订两轮交付，执行四维审查并输出结构化 verdict。

审查流程：主智能体跑完 → 审查 LLM 审（同一模型，复用 copilot 场景配置）→ 四维检查：
  1. 没证据的断言（unsupported_assertion）：回答里的数字/结论逐条对应工具结果或快照
     事实，逐条标 supported/unsupported，unsupported 必须进 issues
  2. 应调没调的工具（missing_tool）：问题明显需要某类数据但轨迹里没查
  3. 断掉的因果链（broken_causal）：结论与证据之间推理跳跃
  4. 走查交付（walkthrough）：回答是否真完成了用户要的事（答非所问/漏答/只答一半）
输出结构化 verdict：{pass, issues:[{kind,detail,suggestion}]}。最多两轮交付——
修订轮不再审查（由 loop.py 接入，本模块只出 verdict）。

成本护栏（审查不改变成本量纲，只在划算时审）：
  1. COPILOT_REVIEW_ENABLED 开关（默认开）
  2. 只审 query/analyze 意图且回答含数据断言（smalltalk/简单问答不审）
  3. 审查 token 预算：COPILOT_REVIEW_TOKEN_BUDGET=单会话累计上限（进程内记账），
     用尽熔断跳审（fail-open 直接交付）；单次审查另设 REVIEW_MAX_TOKENS 上限。

误杀控制（宁缺勿滥，误杀比漏杀更贵）：prompt 明示"证据能对上即 supported，不因文风/
格式挑刺"；审查调用失败/输出解析失败一律 fail-open 放行交付（审查是护栏不是闸门），
异常留 verdict.error 可追查。

红线：本模块只发起 chat completions（purpose=copilot_review 记账），不执行任何
LLM 产出的命令/SQL；verdict 只作为修订提示词文本注入主智能体，不作为执行依据。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

from app.core.config import settings
from app.services.copilot import llm as copilot_llm

logger = logging.getLogger("qianshu.copilot.reviewer")

KINDS = ("unsupported_assertion", "missing_tool", "broken_causal", "walkthrough")

KIND_LABELS = {
    "unsupported_assertion": "没证据的断言",
    "missing_tool": "应调没调的工具",
    "broken_causal": "因果链断裂",
    "walkthrough": "走查交付（答非所问/没做完）",
}

# 代码变更计划专项审查：禁改域与风险模式（标签只做增量登记）
KIND_LABELS.update({
    "forbidden_domain": "触碰禁改域",
    "risk_pattern": "风险模式（删校验/绕确认门/明文凭证/引依赖）",
})

REVIEW_MAX_TOKENS = 2000        # 单次审查调用 token 上限
_ANSWER_MAX = 6000              # 待审回答入提示词上限（字符）
_RESULT_MAX = 1200              # 单工具结果入证据上限（字符）
_EVIDENCE_MAX = 8000            # 工具轨迹证据总上限（字符）

# 数据断言嗅探（成本护栏第二道）：数字+量词/金额/百分比才值得审查
_NUM = r"\d+(?:[.,]\d+)*"
_UNIT = (r"(?:%|％|台|个|元|万元|亿元|万|亿|GB|MB|TB|KB|GiB|TiB|条|次|天|日|小时|分钟|"
         r"秒|毫秒|ms|张|家|人|倍|分|核心|核|QPS|req/s|rpm)")
_DATA_ASSERTION_RE = re.compile(
    rf"{_NUM}\s*{_UNIT}|[¥￥$]\s*{_NUM}|{_NUM}\s*[¥￥$]", re.I)

# 单会话审查 token 累计（进程内成本护栏记账；重启归零，护栏非账本）
_BUDGET_USED: dict = {}


def has_data_assertion(text: str) -> bool:
    """回答含数据断言（数字/金额/百分比/量词）才审——简单问答不审。"""
    return bool(_DATA_ASSERTION_RE.search(text or ""))


def budget_used(session_id: int) -> int:
    return int(_BUDGET_USED.get(int(session_id or 0), 0))


def review_armed(session_id: int = 0) -> bool:
    """审查武装（成本护栏）：开关开 + 会话审查预算未耗尽。"""
    if not getattr(settings, "COPILOT_REVIEW_ENABLED", True):
        return False
    budget = int(getattr(settings, "COPILOT_REVIEW_TOKEN_BUDGET", 8000) or 0)
    return budget_used(session_id) < budget


def should_review(intent: str, answer: str, session_id: int = 0) -> bool:
    """审查触发条件：query/analyze 意图 + 回答含数据断言 + 已武装。"""
    return (intent in ("query", "analyze")
            and has_data_assertion(answer)
            and review_armed(session_id))


def _note_tokens(session_id: int, tokens: int) -> None:
    sid = int(session_id or 0)
    _BUDGET_USED[sid] = _BUDGET_USED.get(sid, 0) + max(0, int(tokens or 0))


# ---------------- 审查提示词（四维 + 误杀控制） ----------------

_SYS = """你是「千枢智能体」的审查员（只读审计：不改答案、不执行任何操作）。
对主智能体的回答做四维检查：

1. 没证据的断言（unsupported_assertion）：回答中的每个数字/结论是否都能对应到【工具轨迹】
   的结果或【系统快照】事实——可核对的数字/结论逐条放进 assertions（status=supported/
   unsupported）；凡 unsupported 的断言必须同时进 issues。
2. 应调没调的工具（missing_tool）：问题明显需要某类数据（成本/资产/告警/指标/日志等），
   但工具轨迹里没查对应数据就下结论或回避问题。
3. 断掉的因果链（broken_causal）：结论与证据之间推理跳跃——证据撑不起结论的因果判断，
   或把相关当因果、把个例当整体。
4. 走查交付（walkthrough）：对照【用户问题】逐项走查——答非所问、漏答用户明确要的项、
   只答一半、该给清单却给空话，都算。

【输出协议】只输出一个 JSON 对象（不要代码块标记、不要任何其他文字）：
{"assertions": [{"text": "回答中的断言原文", "status": "supported|unsupported", "evidence": "对应证据（unsupported 写缺什么证据）"}],
 "issues": [{"kind": "unsupported_assertion|missing_tool|broken_causal|walkthrough", "detail": "问题描述（引用回答原文）", "suggestion": "可执行的修订建议"}],
 "pass": true|false}

【误杀控制（宁缺勿滥，误杀比漏杀更贵）】证据能对上就标 supported；不要因文风/格式/
详略挑刺；拿不准的断言不算 unsupported；issues 只收确凿问题。pass 仅当 issues 为空。"""

_USER = """【用户问题】
{question}

【工具轨迹（证据）】
{evidence}

【系统快照（事实）】
{snapshot_brief}

【待审回答】
{answer}"""


def _evidence(tool_calls: Optional[list]) -> str:
    lines, total = [], 0
    calls = tool_calls or []
    for i, c in enumerate(calls):
        name = str(c.get("name") or "")
        params = str(c.get("params_summary") or "")[:200]
        result = str(c.get("result_summary") or "")[:_RESULT_MAX]
        line = f"- {name}（{params}）→ {result}"
        if total + len(line) > _EVIDENCE_MAX:
            lines.append(f"- …（其余 {len(calls) - i} 条工具结果略）")
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines) or "（未调用任何工具）"


def _extract_json(text: str) -> Optional[dict]:
    """从审查输出提取第一个 JSON 对象（容忍 ```json 围栏/尾逗号）；无则 None。"""
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        return None
    raw = m.group(0)
    for candidate in (raw, re.sub(r",\s*([}\]])", r"\1", raw)):
        try:
            obj = json.loads(candidate)
            return obj if isinstance(obj, dict) else None
        except ValueError:
            continue
    return None


def _normalize(raw: dict, tokens: int) -> dict:
    """verdict 归一：issues 为权威（pass ⟺ issues 为空），逐条标注进 assertions。"""
    issues = []
    for it in (raw.get("issues") or []):
        if not isinstance(it, dict):
            continue
        issues.append({
            "kind": str(it.get("kind") or "walkthrough")[:40],
            "detail": str(it.get("detail") or "")[:500],
            "suggestion": str(it.get("suggestion") or "")[:300],
        })
    assertions = []
    for a in (raw.get("assertions") or []):
        if not isinstance(a, dict):
            continue
        status = str(a.get("status") or "").strip().lower()
        assertions.append({
            "text": str(a.get("text") or "")[:300],
            "status": status if status in ("supported", "unsupported") else "unknown",
            "evidence": str(a.get("evidence") or "")[:300],
        })
    return {"pass": not issues, "issues": issues, "assertions": assertions,
            "tokens": tokens, "error": ""}


def review(cfg: dict, db=None, *, question: str, answer: str,
           tool_calls: Optional[list] = None, snapshot: Optional[dict] = None,
           session_id: int = 0, lang: str = "zh") -> dict:
    """审查一次主回答（一次 chat_once，purpose=copilot_review 记账）。

    返回 verdict：{pass, issues:[{kind,detail,suggestion}],
    assertions:[{text,status,evidence}], tokens, error}。
    任何失败 fail-open（pass=True 放行交付），error 留痕可追查。
    lang：请求界面语言——穿透 chat_once 的回答语言注入。
    """
    from app.services.copilot import renderer

    answer = (answer or "").strip()
    messages = [
        {"role": "system", "content": _SYS},
        {"role": "user", "content": _USER.format(
            question=(question or "").strip()[:2000],
            evidence=_evidence(tool_calls),
            snapshot_brief=renderer.snapshot_brief(snapshot or {}),
            answer=answer[:_ANSWER_MAX])},
    ]
    try:
        resp = copilot_llm.chat_once(
            cfg, messages, purpose="copilot_review", db=db,
            temperature=0.0, max_tokens=REVIEW_MAX_TOKENS, lang=lang)
    except Exception as e:   # noqa: BLE001  审查失败 fail-open：放行交付
        logger.warning("审查调用失败（fail-open 放行）: %s", e)
        return {"pass": True, "issues": [], "assertions": [], "tokens": 0,
                "error": f"{type(e).__name__}: {str(e)[:200]}"}
    tokens = int((resp.get("usage") or {}).get("total_tokens") or 0)
    _note_tokens(session_id, tokens)
    raw = _extract_json(resp.get("content") or "")
    if raw is None:
        return {"pass": True, "issues": [], "assertions": [], "tokens": tokens,
                "error": "审查输出不是合法 JSON（fail-open 放行）"}
    return _normalize(raw, tokens)


# ---------------- 代码变更计划审查（diff 禁改域与风险模式专项检查） ----------------
# diff 类变更清单审查两项：①是否触碰禁改域 ②风险模式（删除校验/绕过确认门/明文凭证/引依赖）。
# 双层：规则层（确定性，命中即 fail 不再花 LLM）+ LLM 层（细粒度补检，失败 fail-open——
# 审查是护栏不是闸门，但规则命中是硬问题不放行）。不过 → issues 进修订（一轮，
# loop.py change_code 路径接「修 diff 一轮→再审不过→放弃并解释」）。

DIFF_KINDS = ("forbidden_domain", "risk_pattern")

_DIFF_MAX_TOKENS = 1500        # 单次 diff 审查 token 上限
_DIFF_TEXT_MAX = 8000          # 入审查提示词的 diff 总上限（字符）
_DIFF_LINE_MAX = 300

# 风险模式规则（②的确定性子集；细粒度由 LLM 补）
_RISK_REMOVE_RE = re.compile(
    r"(require_roles|get_current_user|_require_ack|ACK_TEXT|校验|鉴权|权限|确认门|"
    r"\back\b|confirm|validate|verify|check_password|assert |raise\s+\w*Error|HTTPException)", re.I)
_RISK_CRED_RE = re.compile(
    r"(password|passwd|pwd|secret|token|access[_-]?key|api[_-]?key)\s*[:=]\s*[\"'][^\"']{6,}[\"']", re.I)
_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)")

# 引依赖判定允许集：Python 3.9 stdlib 根 + requirements.txt 顶层名 + 本地包
_STDLIB_ROOTS = frozenset("""
__future__ abc aifc argparse array ast asynchat asyncio asyncore atexit audioop base64 bdb
binascii binhex bisect builtins bz2 calendar cgi cgitb chunk cmath cmd code codecs codeop
collections colorsys compileall concurrent configparser contextlib contextvars copy copyreg
cProfile crypt csv ctypes curses dataclasses datetime dbm decimal difflib dis distutils doctest
email encodings ensurepip enum errno faulthandler fcntl filecmp fileinput fnmatch fractions
ftplib functools gc getopt getpass gettext glob graphlib grp gzip hashlib heapq hmac html http
idlelib imaplib imghdr imp importlib inspect io ipaddress itertools json keyword lib2to3
linecache locale logging lzma mailbox mailcap marshal math mimetypes mmap modulefinder msilib
msvcrt multiprocessing netrc nis nntplib ntpath numbers operator optparse os ossaudiodev pathlib
pdb pickle pickletools pipes pkgutil platform plistlib poplib posix posixpath pprint profile
pstats pty pwd py_compile pyclbr pydoc queue quopri random re readline reprlib resource
rlcompleter runpy sched secrets select selectors shelve shlex shutil signal site smtplib sndhdr
socket socketserver sqlite3 ssl stat statistics string stringprep struct subprocess sunau
symtable sys sysconfig syslog tabnanny tarfile telnetlib tempfile termios test textwrap threading
time timeit tkinter token tokenize trace traceback tracemalloc tty turtle turtledemo types typing
unicodedata unittest urllib uu uuid venv warnings wave weakref webbrowser winreg winsound wsgiref
xdrlib xml xmlrpc zipapp zipfile zipimport zlib zoneinfo
""".split())
_LOCAL_ROOTS = frozenset({"app", "services", "tests", "copilotd", "backend", "frontend", "docs"})
_requirements_cache: dict = {"modules": None}


def _known_import_roots() -> set:
    """允许 import 的模块根：stdlib ∪ requirements.txt 顶层名 ∪ 本地包。"""
    if _requirements_cache["modules"] is None:
        mods = set()
        from pathlib import Path

        for cand in (Path(__file__).resolve().parents[3] / "requirements.txt",   # backend/
                     Path(__file__).resolve().parents[4] / "requirements.txt"):  # 仓库根
            try:
                for line in cand.read_text(encoding="utf-8").splitlines():
                    name = re.split(r"[=<>!\[;]", line.strip(), 1)[0].strip()
                    if name and not name.startswith(("#", "-")):
                        mods.add(name.replace("-", "_").split(".")[0].lower())
            except OSError:
                continue
        _requirements_cache["modules"] = mods
    return _STDLIB_ROOTS | _LOCAL_ROOTS | _requirements_cache["modules"]


def _plan_diff_lines(plan: dict) -> tuple:
    """plan -> (added_lines, removed_lines)。edits/new 两种格式都收（与交付口径一致）。"""
    from app.services.copilot import change_tools

    added, removed = [], []
    for f in (plan or {}).get("files") or []:
        if not isinstance(f, dict):
            continue
        try:
            before, new = change_tools._file_new_content(str(f.get("path") or ""), f)
        except Exception:   # noqa: BLE001  算不了统一 diff 时按 edits 近似
            for e in f.get("edits") or []:
                if isinstance(e, dict):
                    removed += str(e.get("old") or "").splitlines()
                    added += str(e.get("new") or "").splitlines()
            continue
        import difflib

        d = difflib.unified_diff(before.splitlines(), new.splitlines(), lineterm="", n=0)
        for ln in d:
            if ln.startswith("+++") or ln.startswith("---"):
                continue
            if ln.startswith("+"):
                added.append(ln[1:])
            elif ln.startswith("-"):
                removed.append(ln[1:])
    return added, removed


def check_diff_rules(plan: dict) -> list:
    """diff 两项审查的规则层（确定性硬问题）：
    ① 触碰禁改域/非可改域/越界路径；② 风险模式——删除校验/安全门逻辑、明文凭证、引新依赖。
    返回 issues（[{kind,detail,suggestion}]，kind ∈ DIFF_KINDS）。"""
    from app.services.copilot import change_tools

    issues = []
    paths = [str(f.get("path") or "") for f in (plan or {}).get("files") or [] if isinstance(f, dict)]
    for v in change_tools.code_boundary_violations(paths):
        issues.append({"kind": "forbidden_domain", "detail": v[:300],
                       "suggestion": "只改可改域（frontend/src、backend/app/api|services、docs）；"
                                     "禁改域（安全/权限/鉴权/白名单本体/自愈运行时/环境与依赖文件）"
                                     "由人工处理，不代改"})
    added, removed = _plan_diff_lines(plan)
    known = _known_import_roots()
    seen = set()
    for ln in removed:
        if _RISK_REMOVE_RE.search(ln):
            key = ("rm", ln.strip()[:80])
            if key not in seen:
                seen.add(key)
                issues.append({"kind": "risk_pattern",
                               "detail": f"疑似删除校验/安全门逻辑：{ln.strip()[:120]}",
                               "suggestion": "保留校验/确认门/权限/异常处理逻辑；安全门段落是硬边界，"
                                             "如确需调整请给人工步骤"})
    for ln in added:
        m = _IMPORT_RE.match(ln)
        if m:
            root = m.group(1).split(".")[0].lower()
            if root not in known:
                key = ("imp", root)
                if key not in seen:
                    seen.add(key)
                    issues.append({"kind": "risk_pattern",
                                   "detail": f"疑似引入新依赖：{ln.strip()[:120]}",
                                   "suggestion": "不引入新依赖（requirements.txt 禁改）；"
                                                 "需要新依赖=出提案由人工安装"})
        if _RISK_CRED_RE.search(ln):
            key = ("cred", ln.strip()[:80])
            if key not in seen:
                seen.add(key)
                issues.append({"kind": "risk_pattern",
                               "detail": f"疑似明文凭证：{ln.strip()[:120]}",
                               "suggestion": "不写明文凭证/密钥；凭据走加密配置或环境注入"})
    return issues


_DIFF_SYS = """你是「千枢智能体」的审查员（只读审计：不改计划、不执行任何操作）。
对这份**代码变更计划**（diff 形态）做两项专项检查：

1. 触碰禁改域（forbidden_domain）：改动路径是否落在禁改域——加密/口令、权限模型、真实 IP、
   鉴权依赖、变更白名单与平台接口黑名单本体、自愈运行时、.env*/requirements.txt、
   确认门/ack/危险类拒绝逻辑的安全门段落；以及删除文件/表/列类操作。
2. 风险模式（risk_pattern）：是否删除校验/安全检查、绕过确认门（ack/权限/审计）、
   引入明文凭证、引入新依赖（import 非既有依赖）。

【输出协议】只输出一个 JSON 对象（不要代码块标记、不要任何其他文字）：
{"issues": [{"kind": "forbidden_domain|risk_pattern", "detail": "问题描述（引用 diff 原文）", "suggestion": "可执行的修订建议"}], "pass": true|false}

【误杀控制（宁缺勿滥，误杀比漏杀更贵）】确凿才报；改功能/文案/样式/展示逻辑的正常改动不算风险；
拿不准不报；不要因文风/格式挑刺。pass 仅当 issues 为空。"""

_DIFF_USER = """【用户诉求】
{request}

【代码变更计划】
标题：{title}
影响面：{impact}
摘要：{summary}

【diff（- 删除行 / + 新增行，首 {max_lines} 行）】
{diff}"""


def _plan_diff_text(plan: dict) -> str:
    """入审查提示词的紧凑 diff 文本（old→new 片段 + 全文 diff 首段，总上限截断）。"""
    lines, total = [], 0
    for f in (plan or {}).get("files") or []:
        if not isinstance(f, dict):
            continue
        path = str(f.get("path") or "")
        block = [f"### {path}"]
        for e in f.get("edits") or []:
            if not isinstance(e, dict):
                continue
            for ln in str(e.get("old") or "").splitlines()[:20]:
                block.append(f"-{ln[:_DIFF_LINE_MAX]}")
            for ln in str(e.get("new") or "").splitlines()[:20]:
                block.append(f"+{ln[:_DIFF_LINE_MAX]}")
        if not f.get("edits") and isinstance(f.get("new"), str):
            block.append("（全文替换，new 首 30 行）")
            for ln in f.get("new", "").splitlines()[:30]:
                block.append(f"+{ln[:_DIFF_LINE_MAX]}")
        text = "\n".join(block)
        if total + len(text) > _DIFF_TEXT_MAX:
            lines.append(f"### {path}\n…（diff 超上限截断）")
            break
        lines.append(text)
        total += len(text)
    return "\n".join(lines) or "（空 diff）"


def review_code_plan(cfg: dict, db=None, *, request: str, plan: dict,
                     session_id: int = 0, lang: str = "zh") -> dict:
    """diff 类变更清单审查（两项专项）：规则层 + LLM 层合并 verdict。

    返回 {pass, issues:[{kind,detail,suggestion}], tokens, error}。
    规则命中=硬问题直接 fail（省 LLM）；LLM 层失败 fail-open（error 留痕）。
    lang：请求界面语言——穿透 chat_once 的回答语言注入。
    """
    rule_issues = check_diff_rules(plan)
    if rule_issues:
        return {"pass": False, "issues": rule_issues, "tokens": 0, "error": ""}

    messages = [
        {"role": "system", "content": _DIFF_SYS},
        {"role": "user", "content": _DIFF_USER.format(
            request=(request or "").strip()[:1500],
            title=str((plan or {}).get("title") or "")[:200],
            impact=str((plan or {}).get("impact") or "")[:300],
            summary=str((plan or {}).get("summary") or "")[:300],
            max_lines=30,
            diff=_plan_diff_text(plan))},
    ]
    try:
        resp = copilot_llm.chat_once(
            cfg, messages, purpose="copilot_review", db=db,
            temperature=0.0, max_tokens=_DIFF_MAX_TOKENS, lang=lang)
    except Exception as e:   # noqa: BLE001  审查失败 fail-open（规则层已过）
        logger.warning("diff 审查调用失败（fail-open 放行）: %s", e)
        return {"pass": True, "issues": [], "tokens": 0,
                "error": f"{type(e).__name__}: {str(e)[:200]}"}
    tokens = int((resp.get("usage") or {}).get("total_tokens") or 0)
    _note_tokens(session_id, tokens)
    raw = _extract_json(resp.get("content") or "")
    if raw is None:
        return {"pass": True, "issues": [], "tokens": tokens,
                "error": "审查输出不是合法 JSON（fail-open 放行）"}
    issues = []
    for it in (raw.get("issues") or []):
        if not isinstance(it, dict):
            continue
        kind = str(it.get("kind") or "risk_pattern")[:40]
        issues.append({
            "kind": kind if kind in DIFF_KINDS else "risk_pattern",
            "detail": str(it.get("detail") or "")[:500],
            "suggestion": str(it.get("suggestion") or "")[:300],
        })
    return {"pass": not issues, "issues": issues, "tokens": tokens, "error": ""}
