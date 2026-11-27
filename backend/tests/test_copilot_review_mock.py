#!/usr/bin/env python3
"""千枢智能体两轮交付制 mock 测试：主智能体→审查智能体 四维审查 + 一轮修订。

运行：cd /opt/qianshu/backend && python3 tests/test_copilot_review_mock.py
隔离性：MASTER_KEY/DATABASE_URL 等在 import app.* 之前改写为临时值——不碰真实
qianshu.db；LLM 走本机 127.0.0.1 假 OpenAI 兼容服务（真流式 SSE + 真 chat_once 记账），
零外部网络；工具结果走 monkeypatch 假 execute（证据可控）。ASGITransport 壳不触发
lifespan/startup，get_db/get_current_user 走依赖覆盖。不启 8810。

假 LLM 双角色（同一服务按 system 提示词分诊）：MAIN 队列扮主智能体（工具调用/回答/修订），
REVIEW 队列扮审查员（输出四维 verdict JSON）。

验证项：
  A 无证据断言：mock 数据 49 台、回答编 999 台 → 审查挑出（unsupported_assertion）
    → 一轮修订 → 交付修订版；第一轮文本不外发（token 流/落库均无 999）；审查请求带证据
  B 漏调工具：问成本没调 query_cost 就报数 → 审查挑出（missing_tool）→ 修订补调
    query_cost → 交付；工具轨迹含 query_cost
  C 完整好答：调了工具、结论有据 → pass 直通（无修订，审查只 1 次，交付原答）
  D 两轮封顶：修订版仍被挑刺 → 不再审查（审查全程只 1 次）→ 修订版直接交付
  E 开关关=不审：COPILOT_REVIEW_ENABLED=False → 无审查调用/无 review 事件/原样交付
  F 预算熔断=不审：会话审查 token 预算耗尽后跳审；purpose=copilot_review 记账
  G 简单问答不审：回答无数据断言不触发审查；has_data_assertion/review_armed 口径
  H fail-open：审查输出非法 JSON → pass 放行（error 留痕），不修订不阻断
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

# ---- 隔离环境必须先于 app 导入设置（env 覆盖 .env）----
_TMPDIR = tempfile.mkdtemp(prefix="qianshu-review-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("review")
os.environ["MASTER_KEY"] = "ab" * 32
os.environ["EVENT_WEBHOOK_TOKEN"] = "test-token"
os.environ["METRIC_SOURCES"] = "[]"
os.environ["LOG_SOURCES"] = "[]"
os.environ["VM_URL"] = "http://127.0.0.1:1"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.db import Base, SessionLocal, engine  # noqa: E402

import importlib  # noqa: E402
import pkgutil  # noqa: E402
import app.models as _models_pkg  # noqa: E402
for _m in pkgutil.iter_modules(_models_pkg.__path__):
    importlib.import_module(f"app.models.{_m.name}")
from app.core.deps import get_current_user  # noqa: E402
from app.core.db import get_db  # noqa: E402
from app.core.security import encrypt_secret, hash_password  # noqa: E402
from app.models.agent_session import AgentMessage, AgentSession  # noqa: E402
from app.models.ai_setting import AiSetting  # noqa: E402
from app.models.llm_call_log import LlmCallLog  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.copilot import reviewer, tools_read  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


# ---------------- 假 OpenAI 兼容服务（双角色：MAIN 主答 / REVIEW 审查） ----------------

MAIN_SCRIPT: list = []      # 主智能体应答脚本（按序出栈）
REVIEW_SCRIPT: list = []    # 审查员 verdict 脚本（按序出栈）
REQUESTS: list = []         # {"role","path","body"}


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        sys_msg = next((m.get("content", "") for m in body.get("messages", [])
                        if m.get("role") == "system"), "")
        role = "review" if "审查员" in sys_msg else "main"
        REQUESTS.append({"role": role, "path": self.path, "body": body})
        queue = REVIEW_SCRIPT if role == "review" else MAIN_SCRIPT
        resp = queue.pop(0) if queue else {"chunks": ["（无脚本）"], "usage": {}}
        usage = resp.get("usage") or {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        self.send_response(200)
        try:
            if not body.get("stream"):
                # 非流式（审查 chat_once）：普通 JSON 响应
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                payload = {"choices": [{"message": {"content": "".join(resp.get("chunks") or [])}}],
                           "usage": usage}
                self.wfile.write(json.dumps(payload, ensure_ascii=False).encode())
                self.wfile.flush()
                return
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for text in resp.get("chunks") or []:
                payload = {"choices": [{"delta": {"content": text}}]}
                self.wfile.write(("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode())
                self.wfile.flush()
            self.wfile.write(("data: " + json.dumps(
                {"choices": [{"delta": {}}], "usage": usage}, ensure_ascii=False) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *a):
        pass


def _tool_script(name: str, params: dict) -> dict:
    return {"chunks": [json.dumps({"tool": name, "params": params}, ensure_ascii=False)]}


def _answer_script(text: str) -> dict:
    return {"chunks": [text[i:i + 8] for i in range(0, len(text), 8)]}


def _verdict_script(verdict: dict) -> dict:
    return {"chunks": [json.dumps(verdict, ensure_ascii=False)]}


def sse_parse(text: str) -> list:
    """SSE 文本 → [(event, data_dict)]。"""
    events, cur_ev, cur_data = [], "", []
    for line in text.splitlines():
        if line.startswith("event:"):
            cur_ev = line[6:].strip()
        elif line.startswith("data:"):
            cur_data.append(line[5:].strip())
        elif line == "" and (cur_ev or cur_data):
            raw = "".join(cur_data)
            try:
                data = json.loads(raw) if raw else {}
            except ValueError:
                data = {"_raw": raw}
            events.append((cur_ev, data))
            cur_ev, cur_data = "", []
    return events


def review_reqs() -> list:
    return [r for r in REQUESTS if r["role"] == "review"]


def req_text(r: dict) -> str:
    return json.dumps(r["body"], ensure_ascii=False)


# ---------------- 启动假 LLM 服务 + 建表 + 种子 ----------------

server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
PORT = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()

Base.metadata.create_all(engine)

_db = SessionLocal()
_db.add(User(username="admin", password_hash=hash_password("Fixture-Pw-#2026!"), role="admin"))
_db.commit()
_db.add(AiSetting(
    provider_label="fake-llm",
    base_url=f"http://127.0.0.1:{PORT}",
    api_key_cipher=encrypt_secret("sk-test"),
    model="fake-model", temperature=0.2, max_tokens=2048, enabled=True,
))
_db.commit()
_db.close()

from app.main import app  # noqa: E402  建表/种子后导入（路由全量注册）

STUB_USER = SimpleNamespace(id=1, username="admin", role="admin", is_active=True)
def _ov_get_db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


app.dependency_overrides[get_db] = _ov_get_db
app.dependency_overrides[get_current_user] = lambda: STUB_USER


def _req(method: str, url: str, **kw):
    async def _go():
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver") as c:
            return await c.request(method, url, **kw)
    return asyncio.run(_go())


client = SimpleNamespace(
    get=lambda url, **kw: _req("GET", url, **kw),
    post=lambda url, **kw: _req("POST", url, **kw),
)


def new_session() -> int:
    return client.post("/api/agent/sessions", json={}).json()["id"]


def ask(sid: int, q: str) -> list:
    r = client.post(f"/api/agent/sessions/{sid}/messages", json={"q": q})
    return sse_parse(r.text)


def last_assistant(sid: int):
    _db = SessionLocal()
    msg = (_db.query(AgentMessage)
           .filter(AgentMessage.session_id == sid, AgentMessage.role == "assistant")
           .order_by(AgentMessage.id.desc()).first())
    _db.close()
    return msg


# 工具结果假 execute（证据可控：mock 数据 49 台 ECS / 成本 3200.5 元）
def fake_execute(db_, name, params):
    if name == "query_assets":
        return {"total": 49, "by_product": {"ecs": 49}}
    if name == "query_cost":
        return {"days": 7, "total_pretax": 3200.5, "currency": "CNY"}
    return {"ok": True}


tools_read.execute = fake_execute

# 工作流路由关断：本测试专测自由工具循环的两轮交付制（cost 等问题不进 workflows 域）
from app.services.copilot import planner as planner_mod  # noqa: E402
planner_mod.plan = lambda *a, **k: None

# ================ G0 口径单元：断言嗅探 / 审查武装 ================
print("== G0 has_data_assertion / review_armed 口径 ==")
check("G0 数据断言嗅探（数字+单位）", reviewer.has_data_assertion("ECS 共 999 台"))
check("G0 金额断言嗅探", reviewer.has_data_assertion("成本约 5000 元"))
check("G0 简单问答无断言", not reviewer.has_data_assertion("平台提供资产、成本、告警等查询能力"))
check("G0 默认开关开", reviewer.review_armed(999) is True)

# ================ A 无证据断言 → 挑出 → 一轮修订 → 交付修订版 ================
print("== A 无证据断言（编造数字）→ 审查挑出 → 修订交付 ==")
MAIN_SCRIPT.clear(); REVIEW_SCRIPT.clear()
sid_a = new_session()
MAIN_SCRIPT.append(_tool_script("query_assets", {"product": "ecs"}))
MAIN_SCRIPT.append(_answer_script("**ECS 共 999 台**，平台成本 12345 元。"))
REVIEW_SCRIPT.append(_verdict_script({
    "assertions": [
        {"text": "ECS 共 999 台", "status": "unsupported", "evidence": "query_assets 结果为 49 台"},
        {"text": "成本 12345 元", "status": "unsupported", "evidence": "未查任何成本数据"},
    ],
    "issues": [
        {"kind": "unsupported_assertion", "detail": "「ECS 共 999 台」无证据：工具结果是 49 台",
         "suggestion": "改为 49 台"},
        {"kind": "unsupported_assertion", "detail": "「成本 12345 元」无证据：没查过成本数据",
         "suggestion": "删除或补查 query_cost"},
    ],
    "pass": False,
}))
MAIN_SCRIPT.append(_answer_script("ECS 共 49 台（来源 query_assets 工具结果）。"))
n_review_before = len(review_reqs())
events = ask(sid_a, "现在有多少台 ECS？")
kinds = [e for e, _ in events]
check("A SSE 事件序 snapshot→tool→review→token→done",
      kinds[0] == "snapshot" and kinds[-1] == "done"
      and kinds.index("tool") < kinds.index("review") < kinds.index("token"), str(kinds))
rev_ev = [d for e, d in events if e == "review"]
check("A review 事件契约字段", bool(rev_ev)
      and {"round", "pass", "issues"} <= set(rev_ev[0]), str(rev_ev)[:200])
check("A 审查判定 fail + 2 条 issues", bool(rev_ev) and rev_ev[0]["pass"] is False
      and len(rev_ev[0]["issues"]) == 2
      and rev_ev[0]["issues"][0]["kind"] == "unsupported_assertion"
      and rev_ev[0]["revised"] is True, str(rev_ev)[:300])
token_txt = "".join(d.get("text", "") for e, d in events if e == "token")
check("A 交付修订版（token 流无编造的 999）", "49 台" in token_txt and "999" not in token_txt,
      token_txt[:200])
check("A 审查全程只 1 次", len(review_reqs()) == n_review_before + 1,
      f"review reqs={len(review_reqs())}")
check("A 修订请求注入 issues（含编造数字与建议）",
      any("审查未通过" in req_text(r) and "999" in req_text(r) and "没证据的断言" in req_text(r)
          for r in REQUESTS if r["role"] == "main"),
      str([req_text(r)[:120] for r in REQUESTS if r["role"] == "main"][-2:]))
rev_req = review_reqs()[-1]
check("A 审查请求带证据（回答 999 + 工具结果 49）",
      "999" in req_text(rev_req) and "49" in req_text(rev_req)
      and "审查员" in (rev_req["body"]["messages"][0].get("content") or ""),
      req_text(rev_req)[:200])
msg = last_assistant(sid_a)
check("A 落库为修订版且 review_json 完整", msg is not None
      and "49 台" in (msg.content_md or "") and "999" not in (msg.content_md or "")
      and msg.review_json, (msg.content_md or "")[:120])
rj = json.loads(msg.review_json) if msg and msg.review_json else {}
check("A review_json：pass=false/revised/rounds=2/断言逐条标注",
      rj.get("pass") is False and rj.get("revised") is True and rj.get("rounds") == 2
      and len(rj.get("issues") or []) == 2
      and {a.get("status") for a in (rj.get("assertions") or [])} == {"unsupported"},
      json.dumps(rj, ensure_ascii=False)[:300])
items = client.get(f"/api/agent/sessions/{sid_a}/messages").json()["items"]
check("A 消息接口 review 字段回读", items[-1].get("review") is not None
      and items[-1]["review"]["revised"] is True, str(items[-1].get("review"))[:120])
done_ev = [d for e, d in events if e == "done"][0]
check("A done 收尾 + token 记账含审查", bool(done_ev.get("message_id")) and (done_ev.get("tokens") or 0) > 0)

# ================ B 漏调工具 → 挑出 → 修订补调 → 交付 ================
print("== B 应调没调的工具（问成本没查）→ 审查挑出 → 补调交付 ==")
MAIN_SCRIPT.clear(); REVIEW_SCRIPT.clear()
sid_b = new_session()
MAIN_SCRIPT.append(_answer_script("最近成本约 5000 元，变化不大。"))
REVIEW_SCRIPT.append(_verdict_script({
    "assertions": [{"text": "成本约 5000 元", "status": "unsupported", "evidence": "未调用 query_cost"}],
    "issues": [{"kind": "missing_tool", "detail": "问题问成本但轨迹里没调用 query_cost",
                "suggestion": "补调 query_cost 后再下结论"}],
    "pass": False,
}))
MAIN_SCRIPT.append(_tool_script("query_cost", {"days": 7}))
MAIN_SCRIPT.append(_answer_script("近 7 天成本 3200.5 元（query_cost 工具结果）。"))
events = ask(sid_b, "最近成本花了多少钱？")
kinds = [e for e, _ in events]
tool_names = [d.get("name") for e, d in events if e == "tool"]
check("B 审查挑出 missing_tool 且修订补调工具",
      any(d.get("issues") and d["issues"][0]["kind"] == "missing_tool" and d.get("revised")
          for e, d in events if e == "review")
      and "query_cost" in tool_names
      and kinds.index("review") < kinds.index("tool"), str(kinds))
token_txt = "".join(d.get("text", "") for e, d in events if e == "token")
check("B 交付补查后的版本（3200.5，无编造 5000）", "3200.5" in token_txt and "5000" not in token_txt,
      token_txt[:200])
msg = last_assistant(sid_b)
tc = json.loads(msg.tool_calls_json or "[]") if msg else []
check("B 工具轨迹含 query_cost", any(t.get("name") == "query_cost" for t in tc), str(tc)[:200])

# ================ C 完整好答 → pass 直通 ================
print("== C 完整好答 → pass 直通（不修订）==")
MAIN_SCRIPT.clear(); REVIEW_SCRIPT.clear()
sid_c = new_session()
MAIN_SCRIPT.append(_tool_script("query_cost", {"days": 7}))
MAIN_SCRIPT.append(_answer_script("近 7 天成本 3200.5 元（query_cost 结果）。"))
REVIEW_SCRIPT.append(_verdict_script({
    "assertions": [{"text": "3200.5 元", "status": "supported", "evidence": "query_cost.total_pretax"}],
    "issues": [],
    "pass": True,
}))
n_main_before = sum(1 for r in REQUESTS if r["role"] == "main")
events = ask(sid_c, "最近成本怎么样？")
rev_ev = [d for e, d in events if e == "review"]
check("C review 事件 pass=true/无 issues/未修订", bool(rev_ev) and rev_ev[0]["pass"] is True
      and rev_ev[0]["issues"] == [] and rev_ev[0]["revised"] is False, str(rev_ev)[:200])
token_txt = "".join(d.get("text", "") for e, d in events if e == "token")
check("C 原答直接交付", "3200.5 元" in token_txt, token_txt[:100])
check("C 主答+审查共 3 次调用（2 主 1 审，无修订）",
      sum(1 for r in REQUESTS if r["role"] == "main") == n_main_before + 2
      and len(REVIEW_SCRIPT) == 0, f"main={sum(1 for r in REQUESTS if r['role'] == 'main')}")
msg = last_assistant(sid_c)
rj = json.loads(msg.review_json) if msg and msg.review_json else {}
check("C review_json pass=true/rounds=1", rj.get("pass") is True
      and rj.get("revised") is False and rj.get("rounds") == 1
      and (rj.get("assertions") or [{}])[0].get("status") == "supported", json.dumps(rj, ensure_ascii=False)[:200])

# ================ D 两轮封顶：修订仍被挑刺 → 不再审 → 直接交付 ================
print("== D 两轮封顶（修订后直接交付，不再审查）==")
MAIN_SCRIPT.clear(); REVIEW_SCRIPT.clear()
sid_d = new_session()
MAIN_SCRIPT.append(_tool_script("query_assets", {"product": "ecs"}))
MAIN_SCRIPT.append(_answer_script("ECS 共 999 台。"))
REVIEW_SCRIPT.append(_verdict_script({
    "issues": [{"kind": "unsupported_assertion", "detail": "999 台无证据", "suggestion": "改为 49 台"}],
    "pass": False,
}))
REVIEW_SCRIPT.append(_verdict_script({"issues": [{"kind": "walkthrough", "detail": "又编了", "suggestion": "x"}], "pass": False}))
MAIN_SCRIPT.append(_answer_script("ECS 仍是 999 台（修订版）。"))
n_review_before = len(review_reqs())
events = ask(sid_d, "现在有多少台 ECS？")
check("D 审查全程只 1 次（修订不再审查）", len(review_reqs()) == n_review_before + 1,
      f"review reqs={len(review_reqs())}")
check("D 修订版直接交付（最多两轮）", len(REVIEW_SCRIPT) == 1
      and "999 台（修订版）" in "".join(d.get("text", "") for e, d in events if e == "token"),
      str(REVIEW_SCRIPT)[:100])
msg = last_assistant(sid_d)
rj = json.loads(msg.review_json) if msg and msg.review_json else {}
check("D review_json rounds=2/revised", rj.get("rounds") == 2 and rj.get("revised") is True)

# ================ E 开关关 = 不审 ================
print("== E COPILOT_REVIEW_ENABLED=False → 不审 ==")
MAIN_SCRIPT.clear(); REVIEW_SCRIPT.clear()
sid_e = new_session()
settings.COPILOT_REVIEW_ENABLED = False
MAIN_SCRIPT.append(_tool_script("query_assets", {"product": "ecs"}))
MAIN_SCRIPT.append(_answer_script("ECS 共 999 台。"))
n_review_before = len(review_reqs())
events = ask(sid_e, "现在有多少台 ECS？")
check("E 无审查调用/无 review 事件", len(review_reqs()) == n_review_before
      and all(e != "review" for e, _ in events), str([e for e, _ in events]))
token_txt = "".join(d.get("text", "") for e, d in events if e == "token")
check("E 原样交付（不经审查）", "999 台" in token_txt, token_txt[:100])
msg = last_assistant(sid_e)
check("E review_json 为空", msg is not None and not (msg.review_json or ""), str(msg.review_json if msg else ""))
settings.COPILOT_REVIEW_ENABLED = True

# ================ F 预算熔断 = 不审 + 记账 ================
print("== F 审查 token 预算熔断 → 跳审 ==")
MAIN_SCRIPT.clear(); REVIEW_SCRIPT.clear()
sid_f = new_session()
settings.COPILOT_REVIEW_TOKEN_BUDGET = 1       # 极小预算：第一次审查即耗尽
MAIN_SCRIPT.append(_tool_script("query_assets", {"product": "ecs"}))
MAIN_SCRIPT.append(_answer_script("ECS 共 49 台。"))
REVIEW_SCRIPT.append(_verdict_script({"issues": [], "pass": True}))
events = ask(sid_f, "现在有多少台 ECS？")
check("F 首轮审查真跑且记账（purpose=copilot_review）",
      any(e == "review" for e, _ in events) and reviewer.budget_used(sid_f) > 0,
      f"used={reviewer.budget_used(sid_f)}")
_db = SessionLocal()
n_logs = (_db.query(LlmCallLog).filter(LlmCallLog.purpose == "copilot_review").count())
check("F llm_call_logs 记账 purpose=copilot_review", n_logs >= 1, f"logs={n_logs}")
_db.close()
check("F 预算耗尽后 review_armed 断电", reviewer.review_armed(sid_f) is False)
MAIN_SCRIPT.append(_tool_script("query_assets", {"product": "ecs"}))
MAIN_SCRIPT.append(_answer_script("ECS 共 49 台（二轮）。"))
n_review_before = len(review_reqs())
events = ask(sid_f, "再说一遍有多少台 ECS？")
check("F 熔断后跳审（无审查调用/无 review 事件）", len(review_reqs()) == n_review_before
      and all(e != "review" for e, _ in events), str([e for e, _ in events]))
settings.COPILOT_REVIEW_TOKEN_BUDGET = 8000

# ================ G 简单问答不审 ================
print("== G 无数据断言不触发审查 ==")
MAIN_SCRIPT.clear(); REVIEW_SCRIPT.clear()
sid_g = new_session()
MAIN_SCRIPT.append(_answer_script("平台提供资产、成本、告警等查询与分析能力，欢迎体验。"))
n_review_before = len(review_reqs())
events = ask(sid_g, "这个平台能做什么？")
check("G 无断言不审（无调用/无事件）", len(review_reqs()) == n_review_before
      and all(e != "review" for e, _ in events)
      and "平台提供资产" in "".join(d.get("text", "") for e, d in events if e == "token"))

# ================ H fail-open：审查输出非法 JSON → 放行 ================
print("== H 审查异常 fail-open 放行 ==")
MAIN_SCRIPT.clear(); REVIEW_SCRIPT.clear()
sid_h = new_session()
MAIN_SCRIPT.append(_tool_script("query_assets", {"product": "ecs"}))
MAIN_SCRIPT.append(_answer_script("ECS 共 999 台。"))
REVIEW_SCRIPT.append({"chunks": ["这根本不是 JSON"]})
events = ask(sid_h, "现在有多少台 ECS？")
rev_ev = [d for e, d in events if e == "review"]
check("H 解析失败 fail-open（pass=true + error 留痕 + 不修订）",
      bool(rev_ev) and rev_ev[0]["pass"] is True and rev_ev[0]["revised"] is False
      and rev_ev[0].get("error"), str(rev_ev)[:200])
token_txt = "".join(d.get("text", "") for e, d in events if e == "token")
check("H 原样放行交付", "999 台" in token_txt, token_txt[:100])

print(f"\n结果: {len(PASS)} 通过 / {len(FAIL)} 失败")
if FAIL:
    print("失败项:", FAIL)
sys.exit(1 if FAIL else 0)
