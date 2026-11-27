"""copilot 主循环：意图分类 → 快照 → 工具或变更路径 → 审查与渲染。

查询与分析：
  1. 回答前查询系统快照，外发 snapshot 事件并加入提示词；
  2. query/analyze 走工具循环，smalltalk 直答；
  3. 自由工具循环不超过 8 轮，每轮由 LLM 选择白名单工具，结果截断后入上下文；
  4. token 预算熔断，每轮检查 abort，最终渲染 markdown 与快照引用。

变更路径：
  intent=change 时，平台代码修改走 change_code 计划；其余变更由 LLM 生成白名单工具及参数，
  build_ticket 只创建 pending 工单，不直接执行。SSE change 事件供前端展示确认卡，执行必须
  经过 change_gate 确认门。白名单外的变更请求直接拒绝；删库删表、关机、重启服务器、格式化
  等危险操作恒拒绝，绝不执行 LLM 产出的 SQL 或命令原文。

工作流通过 planner.plan(db, text, intent) 选择；命中后调用 workflows.run_workflow(...)。
两者均延迟导入，未提供或执行异常时回退到自由工具循环。

查询与分析回答可由 reviewer.py 检查证据断言、遗漏工具、因果链与交付完整性。未通过时最多
修订一轮；启用审查期间先缓存 token，完成后统一外发。审查只覆盖含数据断言的 query/analyze，
并受 COPILOT_REVIEW_ENABLED 与独立 token 预算约束。

run_turn() 是事件生成器：yield (event, data)，event ∈ snapshot/token/tool/review/change；
调用方（api 层）负责落库与 done/error 收尾。全程 llm_call_logs 记账（copilot/llm.py，
审查 purpose=copilot_review）。
红线：查询工具只读（tools_read 包装），变更只走 change_tools 白名单，绝不执行 LLM 产出的命令/SQL 原文。
"""
from __future__ import annotations

import inspect
import json
import logging
import re
import threading
import time
from typing import Callable, Optional

from sqlalchemy.orm import Session

from app.services.ai_scenarios import resolve_model
from app.services.copilot import change_tools
from app.services.copilot import llm as copilot_llm
from app.services.copilot import renderer, snapshot as snapshot_mod, tools_read
from app.services.copilot import reviewer
from app.services.copilot.intent import classify_query

logger = logging.getLogger("qianshu.copilot.loop")

MAX_TOOL_ROUNDS = 8          # 工具循环 ≤8 轮/次（防跑飞）
REVISE_TOOL_ROUNDS = 4       # 修订轮工具循环上限（两轮交付制的第二轮，省成本）
TOKEN_BUDGET = 30000         # 单次回答 token 预算熔断
HISTORY_MAX = 12             # 携带的历史消息条数
RESULT_MAX_CHARS = tools_read.RESULT_MAX_CHARS


class NeedModel(RuntimeError):
    """未配置 AI 模型（前端引导跳 /settings?tab=ai）。"""


# ---------------- abort：进程内 per-session 中断标志 ----------------

_ABORT_FLAGS: dict = {}


def request_abort(session_id: int) -> bool:
    _ABORT_FLAGS.setdefault(int(session_id), threading.Event()).set()
    return True


def clear_abort(session_id: int) -> None:
    _ABORT_FLAGS.pop(int(session_id), None)


def is_aborted(session_id: int) -> bool:
    ev = _ABORT_FLAGS.get(int(session_id))
    return bool(ev and ev.is_set())


# ---------------- 工具调用协议（严格 JSON，防御式解析） ----------------

def try_tool_call(text: str) -> Optional[dict]:
    """从 LLM 输出提取 {"tool": ..., "params": {...}}；不是工具调用返回 None。"""
    s = (text or "").strip()
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        return None
    raw = m.group(0)
    obj = None
    for candidate in (raw, re.sub(r",\s*([}\]])", r"\1", raw)):
        try:
            obj = json.loads(candidate)
            break
        except ValueError:
            continue
    if not isinstance(obj, dict):
        return None
    # 兼容单工具 {"tool"} 与批量 {"tools":[...]}；单工具执行路径取第一个。
    if not obj.get("tool") and isinstance(obj.get("tools"), list) and obj["tools"]:
        obj = obj["tools"][0] if isinstance(obj["tools"][0], dict) else {}
    tool = obj.get("tool")
    if not isinstance(tool, str) or not tool.strip():
        return None
    params = obj.get("params")
    return {"tool": tool.strip(), "params": params if isinstance(params, dict) else {}}


class _TokenGate:
    """工具轮响应只缓冲不外发；回答轮增量外发。

    首个非空白字符嗅探：{ 或 ```json 开头 → hold（工具 JSON）；其余 → live。
    sniff 误判（hold 但解析不出工具调用）时 flush() 补发缓冲，不丢内容。
    外发文本进 pending 队列，由 run_turn 逐条 yield（生成器不能从回调里 yield）。
    """

    def __init__(self):
        self.mode: Optional[str] = None    # None / "hold" / "live"
        self.buffer = ""
        self.pending: list = []

    def feed(self, delta: str) -> None:
        self.buffer += delta
        if self.mode is None:
            s = self.buffer.lstrip()
            if not s:
                return
            if s.startswith("```"):
                if len(s) < 7:
                    return               # 等能分辨 ```json / ```python 再判
                self.mode = "hold" if s[:7].lower() == "```json" else "live"
            elif s[0] == "{":
                self.mode = "hold"
            else:
                self.mode = "live"
        if self.mode == "live":
            self.pending.append(delta)

    def drain(self) -> list:
        out, self.pending = self.pending, []
        return out

    def flush(self) -> None:
        if self.mode != "live" and self.buffer.strip():
            self.pending.append(self.buffer)


# ---------------- 提示词 ----------------

_SYS_CORE = """你是「千枢智能体」，运维平台的智能助手（只读）。

【系统快照（已查询，回答须引用其中事实，禁止编造平台外数据）】
{snapshot_brief}

【可用只读工具】
{tool_block}

【输出协议（严格遵守，每轮只输出下面两种之一）】
1. 需要查数据时：只输出一个 JSON 对象（不要代码块标记、不要任何其他文字）：
   {{"tool": "工具名", "params": {{...}}}}
2. 信息足够时：直接输出最终回答（markdown 中文，重点加粗，数据多用表格）。

【规则】
- 工具全部只读；若收到修改/删除/重启等变更请求（说明意图分流未命中），提示用户以
  「帮我改…/帮我新增…」句式重发即可进入变更计划流程（变更清单+确认门+可回滚），不要自称只读阶段。
- 同一工具不要重复调用（看历史里的工具结果）；一次只调一个工具。
- 回答必须基于快照与工具结果，为空就如实说没有；简短点评值得注意的点，不闲聊。
- 金额口径（硬规则）：以 pretax_amount（账单金额）为准，禁止用 payment_amount 推断花费。
- 工具结果里出现"（已截断）"属正常（单结果 4KB 上限），需要更多就换参数追问。"""

_SYS_CHANGE_PLAN = """你是「千枢智能体」的变更规划器。用户请求涉及变更类操作。
【系统快照（事实依据）】
{snapshot_brief}

【变更工具白名单（唯一可用，超出即拒绝）】
{tool_block}

【红线（压倒一切）】
- 只能用白名单工具生成变更计划；绝不生成/执行任意 SQL、shell、HTTP 原文。
- 白名单外的请求（删库删表/关机/重启服务器/格式化/改平台代码/删除云账号等）直接拒绝，
  说明为什么不能做 + 安全替代建议（人工在对应页面操作）。
- 变更不会立即执行：只生成变更清单，用户确认后才执行，每项带版本快照可一键回滚。

【输出协议（严格二选一）】
1. 可执行时：只输出一个 JSON 对象（不要代码块标记、不要任何其他文字）：
   {{"title": "一句话变更标题", "plan": [{{"tool": "工具名", "params": {{...}}}}, ...]}}
2. 不可执行/信息不足/拒绝时：直接输出最终回答（markdown 中文，简短说清原因+替代建议）。

【规则】
- 计划最多 3 项，只放确有必要的项；params 严格符合工具 schema，禁止 schema 外字段。
- 用户没给的可选参数就省略；云账号 ak/sk 必须由用户提供真实只读凭证，禁止编造、占位或假凭证。
  缺少任一凭证时直接请用户补充，不生成建账号计划。
- 凭证类变更（新增云账号/改凭证）会走「我已知晓」安全警示确认，属正常流程。"""

# 危险类变更恒拒绝（删库删表/服务器重启关机/删除类永远不执行）——
# 规则层先拦，不依赖 LLM 自觉；LLM 只在白名单内出计划。
_HARD_RED_RE = re.compile(
    r"(删库|删表|清库|清表|删掉[^。\n]{0,12}(表|库)|drop\s*(table|database)|truncate|"
    r"格式化|rm\s+-rf|关机|shutdown|重启服务器)", re.I)

_HARD_RED_ANSWER = """（已拒绝）删库/删表/关机/重启服务器/格式化等危险操作**永远不在智能体变更能力范围内**，也不会以任何形式代执行 SQL/命令原文。

安全替代：此类操作请由管理员在对应控制台人工执行并留变更记录。智能体变更只覆盖平台内配置类变更（云账号接入/归属登记/告警规则/到期关注/业务映射/画像确认），且必须走「变更清单 → 确认门 → 版本快照可回滚」。"""

_CHANGE_PLAN_FALLBACK = "（未能生成有效变更计划）请换个说法重试，或直接说明要变更的对象与目标；白名单外的变更无法执行。"
_CHANGE_REJECT_PREFIX = "（已拒绝）以下变更项不在白名单内或参数不合法，无法执行：\n"


def _extract_json_obj(text: str) -> Optional[dict]:
    """从 LLM 输出提取第一个 JSON 对象（容忍 ```json 围栏/尾逗号）；无则 None。"""
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


def parse_change_plan(text: str) -> Optional[dict]:
    """解析变更计划 JSON → {"title": str, "items": [{"tool","params"}, ...]}；不是计划返回 None。

    兼容 {"plan": [...]} / {"tools": [...]} / 单工具 {"tool": ...}（try_tool_call 同构）。
    """
    obj = _extract_json_obj(text)
    if obj is None:
        return None
    raw_items = obj.get("plan") or obj.get("tools")
    if raw_items is None and obj.get("tool"):
        raw_items = [obj]
    if not isinstance(raw_items, list) or not raw_items:
        return None
    items = []
    for it in raw_items:
        if isinstance(it, dict) and isinstance(it.get("tool"), str):
            params = it.get("params")
            items.append({"tool": it["tool"], "params": params if isinstance(params, dict) else {}})
    if not items:
        return None
    return {"title": str(obj.get("title") or "").strip(), "items": items}

_SYS_SMALLTALK = """你是「千枢智能体」，运维平台的只读智能助手。简短友好地回应（一两句，中文）。
你可以：查询资产/监控/成本/告警、链路与主机排障取证、成本与资源包分析、数据库体检、系统快照感知问答。
不要调用工具，不要展开长文。

【系统快照】
{snapshot_brief}"""

_USER_PROTOCOL_REMIND = "（提醒：按输出协议，要么只输出工具 JSON，要么直接输出最终回答）"
_FORCED_ANSWER = "（已达到工具调用上限/预算上限，请禁止再调用工具，基于已有工具结果直接给出最终回答）"


def _revision_prompt(issues: list) -> str:
    """两轮交付制修订提示词：审查 issues 注入，让主智能体重答/补工具调用。"""
    lines = []
    for i, it in enumerate(issues, 1):
        kind = reviewer.KIND_LABELS.get(str(it.get("kind") or ""), str(it.get("kind") or "问题"))
        lines.append(f"{i}. [{kind}] {it.get('detail', '')} → 建议：{it.get('suggestion', '')}")
    return (
        "【审查未通过，请修订重答】审查员发现以下问题：\n" + "\n".join(lines)
        + "\n\n要求：逐条修复；缺证据就补调工具（不要重复已查过的工具）；"
          "基于工具结果与系统快照事实重新给出完整最终回答（markdown 中文）。")


def _history_messages(db: Session, session_id: int) -> list:
    from app.models.agent_session import AgentMessage

    rows = (db.query(AgentMessage)
            .filter(AgentMessage.session_id == session_id)
            .order_by(AgentMessage.id.desc())
            .limit(HISTORY_MAX).all())
    out = []
    for r in reversed(rows):
        content = (r.content_md or "").strip()
        if not content:
            continue
        role = "assistant" if r.role == "assistant" else "user"
        out.append({"role": role, "content": content[:4000]})
    return out


def _user_content(user_message: str, ctx: Optional[dict]) -> str:
    content = user_message
    if ctx:
        try:
            content += "\n\n【页面上下文】" + json.dumps(ctx, ensure_ascii=False, default=str)[:1000]
        except Exception:
            pass
    return content


# ---------------- 变更路径：计划 → 工单 → 不执行 ----------------

def _change_plan_path(db: Session, session_id: int, user_message: str, ctx: Optional[dict],
                      out: dict, *, user, msg_id: int, snap: dict, cfg: dict, abort,
                      lang: str = "zh"):
    """intent=change：LLM 出变更计划（白名单工具+参数 JSON）→ build_ticket → **不执行**。

    事件：token（直答/拒绝/清单摘要）与 change（变更清单卡，data 契约见 api 层 docstring）。
    白名单外工具/参数不合格项剔除并拒绝说明；危险类请求（删库删表/关机等）恒拒绝。
    lang：请求界面语言——LLM 回答语言跟随界面。
    """
    started = time.monotonic()
    tokens, llm_calls = 0, 0
    model_name = cfg.get("model") or ""
    aborted = False

    def _finish(content_raw: str, tool_calls: list, ticket_id: int = 0):
        out.update({
            "content_raw": content_raw,
            "content_md": renderer.render_answer(content_raw, snap),
            "tool_calls": tool_calls,
            "model": model_name,
            "tokens": tokens,
            "intent": "change",
            "snapshot": snap,
            "aborted": aborted,
            "llm_calls": llm_calls,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "ticket_id": ticket_id,
        })

    if _HARD_RED_RE.search(user_message or ""):
        yield ("token", {"text": _HARD_RED_ANSWER})
        _finish(_HARD_RED_ANSWER, [])
        return

    brief = renderer.snapshot_brief(snap)
    system = _SYS_CHANGE_PLAN.format(snapshot_brief=brief, tool_block=change_tools.prompt_block())
    messages = [{"role": "system", "content": system}]
    history = _history_messages(db, session_id)
    if history and history[-1]["role"] == "user" and history[-1]["content"] == user_message[:4000]:
        history = history[:-1]
    messages += history
    messages.append({"role": "user", "content": _user_content(user_message, ctx)})

    gate = _TokenGate()
    usage_out: dict = {}
    stream = copilot_llm.chat_stream(cfg, messages, purpose="copilot", db=db, out=usage_out,
                                     lang=lang)
    try:
        for delta in stream:
            gate.feed(delta)
            for piece in gate.drain():
                yield ("token", {"text": piece})
            if abort():
                aborted = True
                break
    finally:
        stream.close()               # 中断/异常也走记账
    for piece in gate.drain():
        yield ("token", {"text": piece})
    llm_calls += 1
    tokens += int((usage_out.get("usage") or {}).get("total_tokens") or 0)
    model_name = usage_out.get("model") or model_name
    raw_text = (usage_out.get("content") or "").strip() or gate.buffer.strip()
    streamed_live = gate.mode == "live"

    content_raw = raw_text
    tool_calls: list = []
    ticket_id = 0

    if not aborted:
        plan = parse_change_plan(raw_text)
        if plan is not None:
            valid, rejected = [], []
            for it in plan["items"]:
                tool = (it.get("tool") or "").strip()
                spec = change_tools.CHANGE_TOOLS.get(tool)
                if spec is None:
                    rejected.append(f"{tool or '(空工具名)'}（不在白名单）")
                    continue
                try:
                    params = change_tools.validate_params(spec["params_schema"], it.get("params") or {})
                except change_tools.ChangeToolError as e:
                    rejected.append(f"{tool}（{e}）")
                    continue
                valid.append({"tool": tool, "params": params})
            ticket = None
            if valid:
                try:
                    ticket = change_tools.build_ticket(
                        db, session_id, msg_id, valid, user, title=plan.get("title") or "")
                except change_tools.ChangeToolError as e:
                    rejected.append(str(e))
                    ticket = None
            if ticket is not None:
                ticket_id = ticket["id"]
                tool_calls = [{
                    "name": c["tool"],
                    "params_summary": tools_read.params_summary(c["params"]),
                    "result_summary": "已列入变更清单（待确认，未执行）",
                    "ms": 0,
                } for c in ticket["change_list"]]
                lines = [f"{i + 1}. **{c['title']}**（影响：{c['impact']}；回滚点：{c['rollback']}）"
                         for i, c in enumerate(ticket["change_list"])]
                content_raw = (
                    f"已生成变更清单（{len(ticket['change_list'])} 项），**尚未执行**：\n"
                    + "\n".join(lines)
                    + "\n\n请在变更确认卡中确认执行（凭证类需输入「我已知晓」）；"
                      "每项执行前自动落版本快照，可一键回滚。")
                if rejected:
                    content_raw += "\n\n" + _CHANGE_REJECT_PREFIX + "\n".join(f"- {r}" for r in rejected)
                if not streamed_live:
                    yield ("token", {"text": content_raw})
                yield ("change", _change_event_data(ticket_id, ticket["title"],
                                                    ticket["change_list"]))
            else:
                content_raw = (_CHANGE_REJECT_PREFIX + "\n".join(f"- {r}" for r in rejected)) if rejected \
                    else _CHANGE_PLAN_FALLBACK
                if not streamed_live:
                    yield ("token", {"text": content_raw})
        elif not streamed_live:
            # 未解析出计划：形似 JSON 不把裸 JSON 糊给用户；空回复给引导
            content_raw = raw_text
            if not raw_text or raw_text[:1] in ("{", "`"):
                content_raw = _CHANGE_PLAN_FALLBACK
            yield ("token", {"text": content_raw})

    if aborted and not content_raw:
        content_raw = "（已中断）"
    _finish(content_raw, tool_calls, ticket_id)


# ---------------- change_code 路径：功能/页面/逻辑/字段/文案/样式变更 ----------------
# 意图细分：变更请求若涉及"改功能/改页面/改逻辑/加字段/换文案/改样式"（关键词+LLM 判断）走本路径：
#   code_search 检索上下文 → LLM 出 {title, plan:{files,edits|new,summary,impact}}（两种格式都收）
#   → 构建工单前逐处校验 old 逐字节存在于现文件（防幻觉，不过重试一轮/降级人工步骤）
#   → 审查（reviewer.review_code_plan 两项专项）→ issues 修订一轮 → 再审不过→放弃并解释
#   → build_ticket（tool=code_modify）→ SSE change 事件带 diff_summary 供前端预览。
# 执行只走 change_gate 确认门（handler：validate_plan→snapshot_and_apply→verify_gate→
# 不过 auto_rollback）。code_mod 未就绪→优雅降级给人工步骤。

_CODE_CHANGE_STRONG_RE = re.compile(
    r"(改功能|改页面|改逻辑|加字段|换文案|改样式|改代码|改界面|改展示|改文案|改交互|"
    r"改前端|改后端|改按钮|改菜单|改排序|改显示|改提示|改标签|页面改|界面改|样式改|"
    r"文案改|逻辑改|功能改|(?:改|换|调|修|加)[^。\n]{0,4}(?:页面|界面|样式|文案|逻辑|功能|字段|代码|前端|后端))")
_CODE_CHANGE_WEAK_RE = re.compile(
    r"(页面|界面|样式|文案|按钮|字段|排序|标签|菜单|弹窗|提示语|前端|后端|代码|展示|显示|列表页|详情页)")
_CHANGE_VERB_RE = re.compile(r"(改|换|调|加|删|修|增)")

_SYS_CODE_JUDGE = """你是意图判定器（只输出 true 或 false，不要任何其他文字）。
判断用户诉求是否要修改「千枢平台自身」的功能代码/页面/样式/文案/逻辑/字段/排序（平台自改）。
平台配置类变更（云账号/告警规则/归属登记/阈值/卡片/用户/权限/到期登记等）不是——那是配置级变更。
例："把告警列表改成按级别排"=true；"帮我改一下告警规则阈值"=false；"换一下页面文案"=true。"""

_SYS_CODE_PLAN = """你是「千枢智能体」的代码变更规划器（L3b 平台自改管道）。用户诉求=修改千枢平台自身的功能/页面/逻辑/字段/文案/样式。

【系统快照（事实依据）】
{snapshot_brief}

【代码检索结果（code_search 只读命中；old 片段必须逐字节复制自这里或【目标文件现状】）】
{search_brief}

【目标文件现状（节选）】
{files_brief}

【输出协议（严格二选一）】
1. 可执行时：只输出一个 JSON 对象（不要代码块标记、不要任何其他文字）：
   {{"title": "一句话变更标题", "plan": {{"files": [{{"path": "仓库根相对路径", "edits": [{{"old": "现文件逐字节原文", "new": "替换后原文"}}]}}], "summary": "改什么", "impact": "影响面"}}}}
   文件项两种格式都收：① {{"path", "edits": [{{"old","new"}}]}}（推荐，old 必须逐字节等于现文件片段且唯一）
   ② {{"path", "new": "全文新内容"}}（全文替换；差异行数须 ≤200）。
2. 不可执行/信息不足/超出边界时：直接输出最终回答（markdown 中文，简短说清原因+给人工步骤）。

【硬规则】
- 路径=仓库根相对路径，且只在可改域：frontend/src/**、backend/app/api/**、backend/app/services/**、docs/active/**、docs/tmp/**；docs/archive/** 及其他 docs 目录禁止修改。
- 禁改域（直接拒绝，不要试图改）：app/core 的 security/permissions/client_ip/deps、copilot 的
  change_tools/code_mod、app/copilotd/**、.env*、requirements.txt；只改不删（不删文件/表/列，
  不删除校验/权限/确认门逻辑）。
- old 必须逐字节复制（禁止凭记忆编造/省略空格）；old 必须在文件中唯一（带足上下文）。
- 每次最多改 {max_files} 个文件、合计变更行数 ≤ {max_lines}；不引入新依赖（不新增第三方 import）；不写明文凭证。
- 变更不会立即执行：只生成变更清单，用户确认后才执行；执行前自动落代码快照，验证门不过自动回滚，可一键回滚。"""

_CODE_NOT_READY = """（代码变更能力暂未就绪）本部署的代码修改管道（code_mod）尚未就绪，已停止自动修改（未产生任何改动）。

建议人工步骤：定位要改的文件后由人工修改并自测（代码级修改涉及验证门与回滚管道，管道就绪后可直接提给智能体代改）。"""

_CODE_MANUAL_STEPS = """（已降级为人工步骤）自动变更计划未能生成与现文件**逐字节一致**的编辑片段（防幻觉校验未过，已停止自动修改，未产生任何改动）。

建议人工步骤：
1. 定位要改的文件（可给出文件路径或贴出要替换的原文片段，我可以按逐字节片段重新出计划）；
2. 人工修改并自测；改坏了可用版本管理回退。

也可以换个更具体的说法重试（点名页面/字段/文件，或贴出要改的原文）。"""

_CODE_ABANDON = """（已放弃自动修改）代码变更计划经两轮审查仍未通过（按「再审不过→放弃并解释」停手），**未产生任何改动**。审查问题：

{issues}

请人工评估后修改；或调整诉求避开上述风险点（禁改域/删校验/绕确认门/明文凭证/引依赖不在智能体自改范围内）后重试。"""


def _change_event_data(ticket_id, title: str, change_list: list):
    """change 事件数据（前端契约）：事件级 diff_summary + 项级 tool/params/diff_summary/
    restart_required（normalizeTicket 全量映射）。非 code 项 diff_summary=null。"""
    items, diff_files = [], []
    for i, c in enumerate(change_list or []):
        if not isinstance(c, dict):
            continue
        ds = c.get("diff_summary") if isinstance(c.get("diff_summary"), dict) else None
        if ds:
            diff_files += [f for f in (ds.get("files") or []) if isinstance(f, dict)]
        items.append({
            "idx": c.get("idx", i), "title": c.get("title", ""),
            "impact": c.get("impact", ""), "rollback": c.get("rollback", ""),
            "ack_required": bool(c.get("ack_required")),
            "sensitive": bool(c.get("sensitive")),
            "tool": c.get("tool") or "", "params": c.get("params") or {},
            "diff_summary": ds, "restart_required": bool(c.get("restart_required")),
        })
    return {"ticket_id": ticket_id, "title": title or "",
            "diff_summary": {"files": diff_files}, "change_list": items}


def _eval_code_plan(raw: str) -> tuple:
    """计划调用结果判定 -> (plan|None, errors|None, is_answer)。

    解析出计划 → 结构/old 字节校验；形似 JSON 但解析失败/空回复 → errors（重试/降级）；
    纯 prose = LLM 直接给最终回答（拒绝/信息不足/人工步骤）→ 原样交付。"""
    stripped = (raw or "").strip()
    plan = parse_code_plan(stripped)
    if plan is not None:
        errors = (change_tools.plan_shape_errors(plan["plan"])
                  + change_tools.plan_olds_errors(plan["plan"]))
        return plan, errors, False
    if not stripped or "{" in stripped or "```" in stripped:
        return None, ["未解析出有效计划（形似 JSON 但结构不符）"], False
    return None, None, True


def parse_code_plan(text: str) -> Optional[dict]:
    """解析代码变更计划 JSON → {"title", "plan": {files, summary, impact}}；不是计划返回 None。

    兼容 plan 在顶层（{"title","files",...}）；文件项两种格式都收（edits / 全文 new）。
    """
    obj = _extract_json_obj(text)
    if obj is None:
        return None
    plan = obj.get("plan") if isinstance(obj.get("plan"), dict) else obj
    files = plan.get("files")
    if not isinstance(files, list) or not files:
        return None
    norm_files = []
    for f in files:
        if not isinstance(f, dict) or not f.get("path"):
            continue
        entry = {"path": str(f.get("path") or "").strip()}
        if isinstance(f.get("edits"), list):
            entry["edits"] = [{"old": e.get("old"), "new": e.get("new")}
                              for e in f["edits"] if isinstance(e, dict)]
        if isinstance(f.get("new"), str):
            entry["new"] = f["new"]
        norm_files.append(entry)
    if not norm_files:
        return None
    return {"title": str(obj.get("title") or plan.get("title") or "").strip()[:200],
            "plan": {"files": norm_files,
                     "summary": str(plan.get("summary") or "").strip()[:200],
                     "impact": str(plan.get("impact") or "").strip()[:200]}}


def _code_search_brief(hits: dict) -> str:
    lines = [f"- {h.get('path')}:{h.get('line_no')}: {h.get('snippet')}"
             for h in (hits or {}).get("hits") or [] if isinstance(h, dict)]
    return "\n".join(lines[:10]) or "（无命中——请从用户诉求推断文件，并让 old 逐字节取自文件现状）"


def _code_files_brief(plan: Optional[dict]) -> str:
    """目标文件现状节选（old 必须逐字节取自这里；供首答/重试提示词用）。"""
    blocks = []
    paths = []
    if plan:
        paths = [str(f.get("path") or "") for f in (plan.get("plan") or {}).get("files") or []
                 if isinstance(f, dict)]
    for p in paths[:3]:
        try:
            content = change_tools._resolve_code_file(p).read_text(encoding="utf-8")
        except Exception:   # noqa: BLE001
            continue
        blocks.append(f"### {p}\n```\n{content[:3000]}\n```")
    return "\n\n".join(blocks) or "（暂无——请先按检索结果给出路径与逐字节 old）"


def _code_search_terms(text: str) -> str:
    """检索词原样透传；code_search 已负责中文分词与命中排序，外层再切词会丢失关键词。"""
    return (text or "").strip()


def _is_code_change_request(db: Session, cfg: dict, text: str) -> bool:
    """change 意图细分：改功能/改页面/改逻辑/加字段/换文案/改样式 → change_code。

    关键词+LLM 判断：强关键词直接走；弱信号（UI 名词+变更动词）花一次小 LLM 判定，
    判不了按配置级走（安全默认）。"""
    t = text or ""
    if _CODE_CHANGE_STRONG_RE.search(t):
        return True
    if _CODE_CHANGE_WEAK_RE.search(t) and _CHANGE_VERB_RE.search(t):
        try:
            resp = copilot_llm.chat_once(
                cfg, [{"role": "system", "content": _SYS_CODE_JUDGE},
                      {"role": "user", "content": t[:1000]}],
                purpose="copilot", db=db, temperature=0.0, max_tokens=8)
            m = re.search(r"(true|false)", resp.get("content") or "", re.I)
            return bool(m and m.group(1).lower() == "true")
        except Exception:   # noqa: BLE001  判不了走配置级（安全默认）
            return False
    return False


def _change_code_plan_path(db: Session, session_id: int, user_message: str, ctx: Optional[dict],
                           out: dict, *, user, msg_id: int, snap: dict, cfg: dict, abort,
                           lang: str = "zh"):
    """change_code：code_search→计划→old 字节校验（一轮重试）→审查（修订一轮）→工单→diff 预览。

    事件：token（直答/降级/清单摘要）、review（diff 审查结论）、change（变更清单卡+diff_summary）。
    lang：请求界面语言——LLM 回答语言跟随界面。
    """
    started = time.monotonic()
    tokens, llm_calls = 0, 0
    model_name = cfg.get("model") or ""
    aborted = False

    def _finish(content_raw: str, tool_calls: list, ticket_id: int = 0):
        out.update({
            "content_raw": content_raw,
            "content_md": renderer.render_answer(content_raw, snap),
            "tool_calls": tool_calls,
            "model": model_name,
            "tokens": tokens,
            "intent": "change",
            "snapshot": snap,
            "aborted": aborted,
            "llm_calls": llm_calls,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "ticket_id": ticket_id,
        })

    if _HARD_RED_RE.search(user_message or ""):
        yield ("token", {"text": _HARD_RED_ANSWER})
        _finish(_HARD_RED_ANSWER, [])
        return

    code_mod = change_tools.code_mod_module()
    if code_mod is None:   # 未就绪优雅降级：给人工步骤，不建单
        yield ("token", {"text": _CODE_NOT_READY})
        _finish(_CODE_NOT_READY, [])
        return

    # 1. code_search 检索上下文（只读；查询失败不阻断，给空上下文继续）
    try:
        hits = code_mod.code_search(_code_search_terms(user_message), limit=10) or {}
    except Exception:   # noqa: BLE001
        hits = {}
    search_brief = _code_search_brief(hits)

    brief = renderer.snapshot_brief(snap)
    system = _SYS_CODE_PLAN.format(
        snapshot_brief=brief, search_brief=search_brief, files_brief="（暂无）",
        max_files=change_tools.MAX_CODE_FILES, max_lines=change_tools.MAX_CODE_LINES)
    messages = [{"role": "system", "content": system}]
    history = _history_messages(db, session_id)
    if history and history[-1]["role"] == "user" and history[-1]["content"] == user_message[:4000]:
        history = history[:-1]
    messages += history
    messages.append({"role": "user", "content": _user_content(user_message, ctx)})

    def _plan_call(msgs: list) -> str:
        """一次计划生成（流式但 JSON 只进缓冲不外发）。返回原始文本。"""
        nonlocal tokens, llm_calls, model_name, aborted
        gate = _TokenGate()
        usage_out: dict = {}
        stream = copilot_llm.chat_stream(cfg, msgs, purpose="copilot", db=db, out=usage_out,
                                         lang=lang)
        try:
            for delta in stream:
                gate.feed(delta)
                if abort():
                    aborted = True
                    break
        finally:
            stream.close()
        llm_calls += 1
        tokens += int((usage_out.get("usage") or {}).get("total_tokens") or 0)
        model_name = usage_out.get("model") or model_name
        return (usage_out.get("content") or "").strip() or gate.buffer.strip()

    raw = _plan_call(messages)
    plan, errors, is_answer = (_eval_code_plan(raw) if not aborted
                               else (None, ["已中断"], False))

    if errors and not aborted:
        # 幻觉 old/结构不合格：带文件现状重试一轮（防幻觉校验：old 逐字节=现文件片段）
        messages.append({"role": "assistant", "content": raw[:2000]})
        messages.append({"role": "user", "content":
                         "计划校验未过（防幻觉：old 必须逐字节等于现文件片段且唯一）：\n- "
                         + "\n- ".join(errors)[:800]
                         + "\n\n【目标文件现状（old 必须逐字节复制自这里）】\n" + _code_files_brief(plan)
                         + "\n请重新输出完整计划 JSON（按输出协议），或直接输出最终回答给人工步骤。"})
        raw = _plan_call(messages)
        plan, errors, is_answer = (_eval_code_plan(raw) if not aborted
                                   else (None, ["已中断"], False))

    if aborted:
        _finish("（已中断）", [])
        return
    if is_answer:
        # LLM 直接给最终回答（拒绝/信息不足/人工步骤）——原样交付，不建单
        yield ("token", {"text": raw})
        _finish(raw, [])
        return
    if errors:
        # 重试仍不过 → 降级为给人工步骤（不建单）
        yield ("token", {"text": _CODE_MANUAL_STEPS})
        _finish(_CODE_MANUAL_STEPS, [])
        return

    # 2. 审查加强：diff 两项专项（禁改域/风险模式）；不过 → issues 修订一轮 → 再审不过 → 放弃
    verdict = reviewer.review_code_plan(cfg, db, request=user_message, plan=plan["plan"],
                                        session_id=session_id, lang=lang)
    tokens += int(verdict.get("tokens") or 0)
    llm_calls += 1 if verdict.get("tokens") else 0
    yield ("review", {"round": 1, "pass": bool(verdict.get("pass")),
                      "issues": list(verdict.get("issues") or []), "revised": False,
                      "error": verdict.get("error") or ""})
    if not verdict.get("pass"):
        issues = list(verdict.get("issues") or [])
        lines = []
        for i, it in enumerate(issues, 1):
            kind = reviewer.KIND_LABELS.get(str(it.get("kind") or ""), str(it.get("kind") or "问题"))
            lines.append(f"{i}. [{kind}] {it.get('detail', '')} → 建议：{it.get('suggestion', '')}")
        messages.append({"role": "assistant", "content": raw[:2000]})
        messages.append({"role": "user", "content":
                         "【审查未通过，请修订计划】审查员发现：\n" + "\n".join(lines)
                         + "\n\n【目标文件现状（old 必须逐字节复制自这里）】\n" + _code_files_brief(plan)
                         + "\n逐条避开上述问题（禁改域/删校验/绕确认门/明文凭证/引依赖一律不能碰），"
                           "重新输出完整计划 JSON，或直接输出最终回答给人工步骤。"})
        raw = _plan_call(messages)
        plan2, errors2, is_answer2 = (_eval_code_plan(raw) if not aborted
                                      else (None, ["已中断"], False))
        if aborted:
            _finish("（已中断）", [])
            return
        if is_answer2:
            yield ("token", {"text": raw})
            _finish(raw, [])
            return
        if errors2:
            yield ("token", {"text": _CODE_MANUAL_STEPS})
            _finish(_CODE_MANUAL_STEPS, [])
            return
        verdict2 = reviewer.review_code_plan(cfg, db, request=user_message, plan=plan2["plan"],
                                             session_id=session_id, lang=lang)
        tokens += int(verdict2.get("tokens") or 0)
        llm_calls += 1 if verdict2.get("tokens") else 0
        yield ("review", {"round": 2, "pass": bool(verdict2.get("pass")),
                          "issues": list(verdict2.get("issues") or []), "revised": True,
                          "error": verdict2.get("error") or ""})
        if not verdict2.get("pass"):
            # 再审不过 → 放弃并解释（不建单）
            all_issues = list(verdict2.get("issues") or [])
            content_raw = _CODE_ABANDON.format(issues="\n".join(
                f"- [{reviewer.KIND_LABELS.get(str(it.get('kind') or ''), '问题')}] "
                f"{it.get('detail', '')}" for it in all_issues) or "（无明细）")
            yield ("token", {"text": content_raw})
            _finish(content_raw, [])
            return
        plan = plan2

    # 3. 建工单（tool=code_modify；计划全文入 change_list_json 不加密；执行只走确认门）
    plan["plan"]["diff_summary"] = change_tools.plan_diff_summary(plan["plan"])  # 入工单供展示/预览
    try:
        ticket = change_tools.build_ticket(
            db, session_id, msg_id,
            [{"tool": "code_modify", "params": {"plan": plan["plan"]}}],
            user, title=plan.get("title") or "")
    except change_tools.ChangeToolError as e:
        content_raw = f"（已拒绝）代码变更计划未过构建校验，无法建单：{e}"
        yield ("token", {"text": content_raw})
        _finish(content_raw, [])
        return

    ticket_id = ticket["id"]
    tool_calls = [{
        "name": "code_modify",
        "params_summary": tools_read.params_summary(
            {"files": [str(f.get("path") or "") for f in (plan["plan"].get("files") or [])],
             "summary": plan["plan"].get("summary") or ""}),
        "result_summary": "已列入变更清单（待确认，未执行）",
        "ms": 0,
    }]
    ds_files = (plan["plan"].get("diff_summary") or {}).get("files") or []
    ds_lines = [f"- {f.get('path')}: +{f.get('add_lines')}/-{f.get('del_lines')}" for f in ds_files]
    content_raw = (
        f"已生成代码变更清单（{len(ticket['change_list'])} 项），**尚未执行**：\n"
        f"**{ticket['title']}**（影响：{plan['plan'].get('impact') or '见 diff 预览'}；"
        f"回滚点：代码快照逐文件字节恢复）\n"
        + ("\n" + "\n".join(ds_lines) if ds_lines else "")
        + "\n\n请在变更确认卡中查看 diff 并确认执行（需输入「我已知晓」）；"
          "执行前自动落代码快照，验证门（py_compile/vite build/结构指纹）不过**自动回滚**，"
          "可随时一键回滚；代码改动**需重启生效**。")
    yield ("token", {"text": content_raw})
    yield ("change", _change_event_data(ticket_id, ticket["title"], ticket["change_list"]))
    _finish(content_raw, tool_calls, ticket_id)


# ---------------- 工作流接口 ----------------
# 延迟导入；接口不存在或执行异常时回退自由工具循环：
#   planner.plan(db, text, intent) -> {"workflow": str, "params": dict} | None
#   workflows.run_workflow(db, key, params, context) -> {
#       "content_md" | "content" | "answer": str,
#       "steps": [{"name","params_summary","result_summary","ms"}],
#       "change_items": [{"tool","params"}],
#       "title": str,
#   }
# context 透传 user/session_id/msg_id；change_items 仍走变更确认门。

def _try_workflow(db: Session, text: str, intent: str, out: dict, *,
                  user=None, msg_id: int = 0, snap: Optional[dict] = None,
                  session_id: int = 0):
    """命中工作流返回事件列表 [(event, data)] 并填 out；未命中/异常返回 None（回退原路径）。

    context={user, session_id, msg_id} 必须透传 run_workflow，确保工作流运行与变更工单
    归属真实会话。落库工具轨迹携带 run_id；workflow_run_messages 看到该引用后不再合成
    结果卡，避免真消息与合成卡重复显示。
    """
    try:
        from app.services.copilot import planner as _planner
    except Exception:
        return None                      # 未实现：回退自由工具循环
    try:
        plan = _planner.plan(db, text, intent)
    except Exception:
        logger.warning("planner.plan 失败，回退自由工具循环", exc_info=True)
        return None
    if not (isinstance(plan, dict) and plan.get("workflow")):
        return None
    try:
        from app.services.copilot import workflows as _workflows
        context = {"user": user, "user_id": getattr(user, "id", 0) or 0,
                   "session_id": int(session_id or 0), "msg_id": int(msg_id or 0),
                   "trigger": "chat"}
        result = _workflows.run_workflow(db, str(plan["workflow"]),
                                         dict(plan.get("params") or {}), context)
    except Exception as e:
        logger.warning("run_workflow 失败，回退自由工具循环", exc_info=True)
        # 未知工作流/参数不合法属于调用侧问题；其余异常按平台侧故障上报。
        if type(e).__name__ not in ("ParamError", "WorkflowNotFound"):
            from app.services.self_heal.sources import report_agent_failure
            report_agent_failure(f"workflow:{plan['workflow']}", e)
        return None
    if not isinstance(result, dict):
        return None

    started = time.monotonic()
    run_id = int(result.get("run_id") or 0)
    events: list = []
    tool_calls: list = []
    for step in result.get("steps") or []:
        if isinstance(step, dict) and step.get("name"):
            call = {"name": str(step["name"]),
                    "params_summary": str(step.get("params_summary") or "")[:300],
                    "result_summary": str(step.get("result_summary") or "")[:500],
                    "ms": int(step.get("ms") or 0)}
            events.append(("tool", call))                     # SSE 契约形状（不带 run_id）
            tool_calls.append({**call, "run_id": run_id} if run_id else call)
    if run_id and not any(c.get("run_id") == run_id for c in tool_calls):
        # 无步骤也要留 run_id 引用（防重锚点：真消息引用 run #N 后不再合成结果卡）
        tool_calls.append({"name": "workflow_run", "params_summary": "",
                           "result_summary": f"工作流运行 run #{run_id}（结果见工作流结果卡）",
                           "ms": 0, "run_id": run_id})
    content = str(result.get("content_md") or result.get("content")
                  or result.get("answer") or "").strip()
    ticket_id = 0
    change_items = [it for it in (result.get("change_items") or []) if isinstance(it, dict)]
    if change_items:
        try:
            ticket = change_tools.build_ticket(
                db, int(session_id or 0), msg_id, change_items, user,
                title=str(result.get("title") or ""))
        except change_tools.ChangeToolError as e:
            content = content or f"工作流产出的变更项被拒（{e}）"
        else:
            ticket_id = ticket["id"]
            content = content or f"工作流已完成，生成变更清单（{len(ticket['change_list'])} 项），请确认后执行。"
            events.append(("change", _change_event_data(ticket_id, ticket["title"],
                                                        ticket["change_list"])))
    # 工作流引擎已通过 build_ticket 建单；change_items 恒空以防重复建单，
    # 这里只为已建工单发 change 事件，让聊天流输出确认卡。
    wf_tickets = [t for t in (result.get("tickets") or []) if isinstance(t, dict) and t.get("ticket_id")]
    for t in wf_tickets:
        events.append(("change", _change_event_data(t["ticket_id"], t.get("title") or "",
                                                    t.get("change_list") or [])))
    if not content and wf_tickets:
        n_items = sum(len(t.get("change_list") or []) for t in wf_tickets)
        content = f"工作流已完成，生成变更清单（{n_items} 项），请在变更确认卡确认后执行。"
    if content:
        events.append(("token", {"text": content}))
    out.update({
        "content_raw": content,
        "content_md": renderer.render_answer(content, snap or {}),
        "tool_calls": tool_calls,
        "model": "", "tokens": 0, "intent": intent, "snapshot": snap or {},
        "aborted": False, "llm_calls": 0,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "ticket_id": ticket_id,
    })
    return events


# ---------------- 主循环 ----------------

def run_turn(db: Session, session_id: int, user_message: str,
             ctx: Optional[dict], out: dict,
             should_abort: Optional[Callable[[], bool]] = None,
             model_cfg: Optional[dict] = None,
             snapshot: Optional[dict] = None,
             user=None, msg_id: int = 0, lang: str = "zh"):
    """一次完整回答。事件生成器：yield ("snapshot"|"token"|"tool"|"change", data)。

    out（调用方传入 dict）结束后填充：
      {content_raw, content_md, tool_calls, model, tokens, intent, snapshot,
       aborted, llm_calls, duration_ms[, ticket_id]}
    user/msg_id：变更路径建工单用（msg_id=本条用户消息 id，回链工单↔消息）。
    lang：请求界面语言（zh/en）——穿透到各 LLM 调用做回答语言注入。
    """
    started = time.monotonic()
    abort = should_abort or (lambda: is_aborted(session_id))
    cfg = model_cfg if model_cfg is not None else resolve_model(db, "copilot")
    if not cfg:
        raise NeedModel("未配置 AI 模型，请先在 设置 → AI 模型 中配置并启用")

    snap = snapshot if snapshot is not None else snapshot_mod.build_snapshot(db)
    yield ("snapshot", snap)

    intent = classify_query(db, user_message)

    # 工作流优先；planner/workflows 延迟导入，未提供或异常时回退原路径。
    wf_events = _try_workflow(db, user_message, intent, out, user=user, msg_id=msg_id,
                              snap=snap, session_id=session_id)
    if wf_events is not None:
        for ev, data in wf_events:
            yield (ev, data)
        return

    if intent == "change":
        # 意图细分：改功能/页面/逻辑/字段/文案/样式 → change_code 代码变更计划；
        # 其余 → 配置级变更计划（build_ticket → 不执行，执行走 change_gate 确认门）
        if _is_code_change_request(db, cfg, user_message):
            yield from _change_code_plan_path(db, session_id, user_message, ctx, out,
                                              user=user, msg_id=msg_id, snap=snap, cfg=cfg,
                                              abort=abort, lang=lang)
        else:
            yield from _change_plan_path(db, session_id, user_message, ctx, out,
                                         user=user, msg_id=msg_id, snap=snap, cfg=cfg,
                                         abort=abort, lang=lang)
        return

    brief = renderer.snapshot_brief(snap)
    tool_block = tools_read.tool_prompt_block()
    use_tools = intent in ("query", "analyze")

    if intent == "smalltalk":
        system = _SYS_SMALLTALK.format(snapshot_brief=brief)
    else:
        system = _SYS_CORE.format(snapshot_brief=brief, tool_block=tool_block)

    messages = [{"role": "system", "content": system}]
    history = _history_messages(db, session_id)
    # 用户消息在调用前已落库（断线不丢问）——历史里会带出当前这问，去重后再补一次带 ctx 的
    if history and history[-1]["role"] == "user" and history[-1]["content"] == user_message[:4000]:
        history = history[:-1]
    messages += history
    messages.append({"role": "user", "content": _user_content(user_message, ctx)})

    tool_calls: list = []
    tokens = 0
    llm_calls = 0
    model_name = cfg.get("model") or ""
    answer_parts: list = []
    aborted = False
    forced = False

    # 两轮交付制：审查武装时回答文本"先扣后发"——审查通过/修订完成才交付，
    # 没证据的断言绝不与用户见面；未武装（开关关/预算熔断/非工具意图）走原增量外发。
    review_on = use_tools and reviewer.review_armed(session_id)
    held_tokens: list = []
    review_state: Optional[dict] = None

    rounds = MAX_TOOL_ROUNDS if use_tools else 0
    round_idx = 0
    retries = 0
    delivery_round = 1
    while True:                        # 交付轮循环（≤2：主答 + 一轮修订，修订后直接交付）
        while True:                    # 工具循环（≤8 轮，修订轮 ≤4 轮）
            if abort():
                aborted = True
                break
            budget_hit = tokens >= TOKEN_BUDGET
            # 轮次/预算用尽 → 强制直答（允许最后一次无工具调用）
            no_tools = (not use_tools) or forced or budget_hit or round_idx >= rounds
            call_messages = list(messages)
            if no_tools and use_tools:
                call_messages = call_messages + [{"role": "user", "content": _FORCED_ANSWER}]

            gate = _TokenGate()
            usage_out: dict = {}
            stream = copilot_llm.chat_stream(
                cfg, call_messages, purpose="copilot", db=db, out=usage_out, lang=lang,
            )
            try:
                for delta in stream:
                    gate.feed(delta)
                    pieces = gate.drain()
                    if review_on:
                        held_tokens.extend(pieces)
                    else:
                        for piece in pieces:
                            yield ("token", {"text": piece})
                    if abort():
                        aborted = True
                        break
            finally:
                stream.close()           # 中断/异常也走记账（GeneratorExit 路径）
            pieces = gate.drain()
            if review_on:
                held_tokens.extend(pieces)
            else:
                for piece in pieces:
                    yield ("token", {"text": piece})
            llm_calls += 1
            tokens += int((usage_out.get("usage") or {}).get("total_tokens") or 0)
            model_name = usage_out.get("model") or model_name
            raw_text = (usage_out.get("content") or "").strip() or gate.buffer.strip()

            if aborted:
                answer_parts.append(raw_text)
                break

            call = None if no_tools else try_tool_call(raw_text)
            if call is not None and call["tool"] in tools_read.tool_names():
                name, params = call["tool"], call["params"]
                t0 = time.monotonic()
                err = ""
                try:
                    # 会话用户透传（platform_api_query 权限随会话用户）；execute 不收 user
                    # 的替身（测试 monkeypatch）自动降级三参调用，保持兼容
                    if "user" in inspect.signature(tools_read.execute).parameters:
                        result = tools_read.execute(db, name, params, user=user)
                    else:
                        result = tools_read.execute(db, name, params)
                except Exception as e:
                    result, err = None, f"{type(e).__name__}: {str(e)[:200]}"
                    # 参数/权限类 ToolError 是用户用法问题，不上报；其余异常代表工具
                    # 背后的平台/集成故障，进入统一问题收件箱。
                    if not isinstance(e, tools_read.ToolError):
                        from app.services.self_heal.sources import report_agent_failure
                        report_agent_failure(name, e)
                ms = int((time.monotonic() - t0) * 1000)
                summary = tools_read.result_summary(result, err)
                tool_calls.append({
                    "name": name,
                    "params_summary": tools_read.params_summary(params),
                    "result_summary": summary,
                    "ms": ms,
                })
                yield ("tool", {"name": name,
                                "params_summary": tools_read.params_summary(params),
                                "result_summary": summary[:500],
                                "ms": ms})
                messages.append({"role": "assistant", "content": raw_text[:2000]})
                messages.append({"role": "user",
                                 "content": f"【工具结果】{name}（{ms}ms）：{summary[:RESULT_MAX_CHARS]}"})
                round_idx += 1
                if round_idx >= rounds or tokens >= TOKEN_BUDGET:
                    forced = True
                continue

            if call is not None and call["tool"] not in tools_read.tool_names():
                # 工具名不存在：纠偏重试（计 1 次重试，防跑飞）
                retries += 1
                if retries <= 2 and not no_tools:
                    messages.append({"role": "assistant", "content": raw_text[:2000]})
                    messages.append({"role": "user",
                                     "content": f"工具 {call['tool']} 不存在，请严格从工具清单选择。{_USER_PROTOCOL_REMIND}"})
                    continue

            if not raw_text and use_tools and not no_tools and retries <= 2:
                # 空回复：协议提醒重试（LLM 偶发空内容，一次空输出就放弃太脆）
                retries += 1
                messages.append({"role": "user", "content": _USER_PROTOCOL_REMIND})
                continue

            # 最终回答（或协议走偏但内容可用）
            gate.flush()
            pieces = gate.drain()
            if review_on:
                held_tokens.extend(pieces)
            else:
                for piece in pieces:
                    yield ("token", {"text": piece})
            answer_parts.append(raw_text)
            break

        content_raw = "\n\n".join(p for p in answer_parts if p).strip()

        # 两轮交付制审查门（只审第一交付轮；变更/工作流/小聊路径不经过这里）
        if (delivery_round == 1 and use_tools and not aborted
                and reviewer.should_review(intent, content_raw, session_id)):
            verdict = reviewer.review(
                cfg, db, question=user_message, answer=content_raw,
                tool_calls=tool_calls, snapshot=snap, session_id=session_id, lang=lang)
            tokens += int(verdict.get("tokens") or 0)
            llm_calls += 1
            issues = list(verdict.get("issues") or [])
            revise = (not verdict.get("pass")) and bool(issues)
            review_state = {
                "pass": not revise,
                "issues": issues,
                "assertions": list(verdict.get("assertions") or []),
                "revised": False,
                "rounds": 1,
                "error": verdict.get("error") or "",
            }
            yield ("review", {"round": 1, "pass": review_state["pass"],
                              "issues": issues, "revised": revise,
                              "error": review_state["error"]})
            if revise:
                # 一轮修订：issues 注入提示词，主智能体重答/补工具调用（不再审查）
                review_state["revised"] = True
                review_state["rounds"] = 2
                messages.append({"role": "assistant", "content": content_raw[:2000]})
                messages.append({"role": "user", "content": _revision_prompt(issues)})
                answer_parts = []
                held_tokens.clear()      # 第一轮文本不交付——修订版才是交付内容
                round_idx = 0
                retries = 0
                forced = False
                rounds = REVISE_TOOL_ROUNDS
                delivery_round = 2
                continue
        break

    content_raw = "\n\n".join(p for p in answer_parts if p).strip()
    if not content_raw and tool_calls:
        content_raw = "已完成查询，但模型没有返回文字总结。可继续追问下钻：" + renderer.tool_trace_line(tool_calls)
    if not content_raw:
        content_raw = "（本轮没有生成内容）"
    content_md = renderer.render_answer(content_raw, snap)

    # 交付：审查武装时回答文本统一在此外发；未武装时已增量外发
    if review_on:
        if held_tokens:
            for piece in held_tokens:
                yield ("token", {"text": piece})
        elif content_raw:
            yield ("token", {"text": content_raw})

    out.update({
        "content_raw": content_raw,
        "content_md": content_md,
        "tool_calls": tool_calls,
        "model": model_name,
        "tokens": tokens,
        "intent": intent,
        "snapshot": snap,
        "aborted": aborted,
        "llm_calls": llm_calls,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "review": review_state,
    })
