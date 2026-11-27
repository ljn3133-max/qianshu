#!/usr/bin/env python3
"""千枢智能体后端 mock 测试：copilot 引擎 + 会话 API 全链路。

运行：cd /opt/qianshu/backend && python3 tests/test_copilot_p1_mock.py
隔离性：MASTER_KEY/DATABASE_URL 等在 import app.* 之前改写为临时值——不碰真实
qianshu.db；LLM 走本机 127.0.0.1 假 OpenAI 兼容服务（真流式 SSE 解析 + 真记账），
零外部网络；工具循环调用真只读工具（临时库空表）。ASGITransport 壳不触发
lifespan/startup（不启调度器），get_db/get_current_user 走依赖覆盖。

验证项：
  A 会话 CRUD + 审计（agent.session.create/delete 落 audit）
  B NEED_MODEL 分支：未配模型 → error 事件 code=NEED_MODEL 后收尾（无 done）
  C 工具循环轨迹落库：tool 事件 + assistant.tool_calls_json(name/params_summary/
    result_summary/ms) + llm_call_logs(purpose=copilot) 记账 + done 事件 + 首问自动命名
    + 首条消息附 snapshot_json + token 流拼出回答
  D 8 轮熔断：工具循环上限 8 次后强制直答；token 预算熔断提前收口
  E 结果截断：工具结果 4KB 截断入上下文/轨迹（（已截断）标记）
  F abort 生效：/abort 端点置标志 → 流式中途停下，保留已生成内容
  G 快照接口五面齐全（云账号/数据源/资产/拓扑/最近变更）+ 路由注册
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

# ---- 隔离环境必须先于 app 导入设置（env 覆盖 .env）----
_TMPDIR = tempfile.mkdtemp(prefix="qianshu-copilot-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("copilot")
os.environ["MASTER_KEY"] = "ab" * 32
os.environ["EVENT_WEBHOOK_TOKEN"] = "test-token"
os.environ["METRIC_SOURCES"] = "[]"
os.environ["LOG_SOURCES"] = "[]"
os.environ["VM_URL"] = "http://127.0.0.1:1"
# 两轮交付制审查关断：本文件覆盖基础回归，审查路径由
# test_copilot_review_mock.py 专测——保持脚本序与断言零耦合
os.environ["COPILOT_REVIEW_ENABLED"] = "false"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.core.deps import get_current_user  # noqa: E402
from app.core.db import get_db  # noqa: E402
from app.core.security import encrypt_secret, hash_password  # noqa: E402
from app.models.agent_session import AgentMessage, AgentSession  # noqa: E402
from app.models.ai_setting import AiSetting  # noqa: E402
from app.models.audit import AuditLog  # noqa: E402
from app.models.llm_call_log import LlmCallLog  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.copilot import loop as copilot_loop  # noqa: E402
from app.services.copilot import tools_read  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


# ---------------- 假 OpenAI 兼容服务（真 SSE 流式） ----------------

SCRIPT: list = []       # 每次 LLM 调用的应答脚本（按序出栈）
REQUESTS: list = []     # 收到的请求（断言协议/参数用）


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        REQUESTS.append({"path": self.path, "body": body})
        resp = SCRIPT.pop(0) if SCRIPT else {"chunks": ["（无脚本）"], "usage": {}}
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for i, text in enumerate(resp.get("chunks") or []):
                payload = {"choices": [{"delta": {"content": text}}]}
                self.wfile.write(("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode())
                self.wfile.flush()
                if resp.get("block_after") == i:
                    resp["block_event"].wait(10)
            usage = resp.get("usage") or {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
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


# ---------------- 启动假 LLM 服务 + 建表 + 种子 ----------------

server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
PORT = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()

Base.metadata.create_all(engine)

_db = SessionLocal()
admin = User(username="admin", password_hash=hash_password("Fixture-Pw-#2026!"), role="admin")
_db.add(admin)
_db.commit()
setting = AiSetting(
    provider_label="fake-llm",
    base_url=f"http://127.0.0.1:{PORT}",   # 故意不带 /v1——验证自动补 /v1 兼容
    api_key_cipher=encrypt_secret("sk-test"),
    model="fake-model", temperature=0.2, max_tokens=2048, enabled=True,
)
_db.add(setting)
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
# starlette TestClient 与 httpx 0.28 不兼容；ASGITransport 只有异步接口——
# 套同步壳走 asyncio.run（不触发 lifespan/startup：不启调度器、零网络）
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
    patch=lambda url, **kw: _req("PATCH", url, **kw),
    delete=lambda url, **kw: _req("DELETE", url, **kw),
)

# A. 会话 CRUD
print("== A 会话 CRUD + 审计 ==")
r = client.post("/api/agent/sessions", json={})
check("A 新建会话", r.status_code == 200 and r.json().get("id") == 1, r.text[:200])
sid = r.json()["id"]
r = client.get("/api/agent/sessions")
check("A 会话列表", r.status_code == 200 and r.json()["total"] == 1
      and r.json()["items"][0]["message_count"] == 0, r.text[:200])
# PATCH 用例走一次性会话 sid2（避免显式改名影响 C 的"首问自动命名"断言——
# 显式重命名后不覆盖是正确行为）
sid2 = client.post("/api/agent/sessions", json={}).json()["id"]
r = client.patch(f"/api/agent/sessions/{sid2}", json={"title": "改名", "pinned": True, "status": "archived"})
check("A PATCH 重命名/置顶/归档", r.status_code == 200 and r.json()["title"] == "改名"
      and r.json()["pinned"] and r.json()["status"] == "archived", r.text[:200])
r = client.patch(f"/api/agent/sessions/{sid2}", json={"status": "bad"})
check("A PATCH 非法状态拒绝", r.status_code == 400)
r = client.get(f"/api/agent/sessions/{sid}/messages")
check("A 消息列表空", r.status_code == 200 and r.json()["total"] == 0)
r = client.delete(f"/api/agent/sessions/{sid2}")
check("A 删除会话", r.status_code == 200 and r.json()["deleted"])
_db = SessionLocal()
n_audit = (_db.query(AuditLog)
           .filter(AuditLog.action.in_(["agent.session.create", "agent.session.delete"])).count())
check("A 审计落库 create/delete", n_audit >= 3, f"audit rows={n_audit}")
_db.close()

# B. NEED_MODEL（用独立会话，不污染 C 的首条快照/命名断言）
print("== B NEED_MODEL 分支 ==")
sid_b = client.post("/api/agent/sessions", json={}).json()["id"]
_db = SessionLocal()
_db.query(AiSetting).update({"enabled": False})
_db.commit()
_db.close()
r = client.post(f"/api/agent/sessions/{sid_b}/messages", json={"q": "你好"})
events = sse_parse(r.text)
check("B 未配模型 error NEED_MODEL",
      any(e == "error" and d.get("code") == "NEED_MODEL" for e, d in events), r.text[:300])
check("B error 后收尾（无 done）", all(e != "done" for e, _ in events), r.text[:300])
_db = SessionLocal()
_db.query(AiSetting).update({"enabled": True})
_db.commit()
_db.close()

# C. 工具循环轨迹落库
print("== C 工具循环轨迹落库 ==")
SCRIPT.append(_tool_script("query_assets", {"product": "ecs", "limit": 5}))
SCRIPT.append(_answer_script("**当前资产概况**：ECS 49 台（快照事实）。"))
r = client.post(f"/api/agent/sessions/{sid}/messages",
                json={"q": "现在配了哪些云账号？资产规模如何？", "ctx": {"page": "dashboard"}})
events = sse_parse(r.text)
kinds = [e for e, _ in events]
check("C SSE 事件序 snapshot→tool→token→done",
      bool(kinds) and kinds[0] == "snapshot" and "tool" in kinds and "token" in kinds
      and kinds[-1] == "done", str(kinds))
check("C snapshot 事件五面", all(k in events[0][1] for k in
      ("cloud_accounts", "datasources", "assets", "topology", "recent_changes")))
tool_ev = [d for e, d in events if e == "tool"]
check("C tool 事件字段", bool(tool_ev) and {"name", "params_summary", "result_summary", "ms"} <= set(tool_ev[0])
      and tool_ev[0]["name"] == "query_assets", str(tool_ev)[:200])
done_ev = [d for e, d in events if e == "done"][0]
check("C done 事件带 message_id/model/tokens", bool(done_ev.get("message_id"))
      and done_ev.get("model") == "fake-model" and (done_ev.get("tokens") or 0) > 0, str(done_ev))
token_txt = "".join(d.get("text", "") for e, d in events if e == "token")
check("C token 流拼出回答", "当前资产概况" in token_txt, token_txt[:100])
_db = SessionLocal()
msgs = (_db.query(AgentMessage).filter(AgentMessage.session_id == sid)
        .order_by(AgentMessage.id).all())
check("C 用户+助手消息落库", len(msgs) == 2 and msgs[0].role == "user" and msgs[1].role == "assistant")
check("C 首条消息附 snapshot_json", bool(msgs[0].snapshot_json) and "cloud_accounts" in msgs[0].snapshot_json)
tc = json.loads(msgs[1].tool_calls_json or "[]")
check("C 工具轨迹字段齐全", len(tc) == 1 and {"name", "params_summary", "result_summary", "ms"} <= set(tc[0])
      and tc[0]["name"] == "query_assets" and isinstance(tc[0]["ms"], int), str(tc)[:200])
check("C 助手消息记 model/tokens", msgs[1].model == "fake-model" and (msgs[1].tokens or 0) > 0)
check("C 回答附快照引用条", "系统快照" in (msgs[1].content_md or ""))
sess = _db.get(AgentSession, sid)
check("C 首问自动命名", sess.title == "现在配了哪些云账号？资产规模如何？"[:20], sess.title)
logs = _db.query(LlmCallLog).filter(LlmCallLog.purpose == "copilot").all()
check("C llm_call_logs 记账 purpose=copilot", len(logs) >= 2
      and all(l.model == "fake-model" for l in logs)
      and all((l.total_tokens or 0) > 0 for l in logs), f"logs={len(logs)}")
_db.close()
r = client.get(f"/api/agent/sessions/{sid}/messages")
items = r.json()["items"]
check("C 消息接口 tool_calls 解析回读", items[-1]["tool_calls"][0]["name"] == "query_assets"
      and items[-1]["model"] == "fake-model", str(items)[:200])
check("C ctx 注入不影响用户消息落库", items[0]["content_md"].startswith("现在配了哪些云账号"))

# D. 8 轮熔断 + token 预算熔断
print("== D 8 轮熔断 + token 预算熔断 ==")
for i in range(8):
    SCRIPT.append(_tool_script("query_assets", {"limit": i + 1}))
SCRIPT.append(_answer_script("第 9 次调用直接总结。"))
r = client.post(f"/api/agent/sessions/{sid}/messages", json={"q": "反复查资产测试熔断"})
events = sse_parse(r.text)
tool_n = sum(1 for e, _ in events if e == "tool")
check("D 工具循环 ≤8 轮", tool_n == 8, f"tool events={tool_n}")
check("D 熔断后强制直答且 done", events[-1][0] == "done" and any(e == "token" for e, _ in events))
check("D 第 9 次请求带强制直答提示",
      any("禁止再调用工具" in json.dumps(x["body"], ensure_ascii=False) for x in REQUESTS[-9:]),
      str(REQUESTS[-1]["body"])[:200])

_real_budget = copilot_loop.TOKEN_BUDGET
copilot_loop.TOKEN_BUDGET = 1
SCRIPT.append(_tool_script("query_assets", {"limit": 99}))
SCRIPT.append(_answer_script("预算熔断后的总结。"))
r = client.post(f"/api/agent/sessions/{sid}/messages", json={"q": "预算熔断测试"})
events = sse_parse(r.text)
tool_n = sum(1 for e, _ in events if e == "tool")
check("D token 预算熔断提前收口", tool_n == 1 and events[-1][0] == "done", f"tool events={tool_n}")
copilot_loop.TOKEN_BUDGET = _real_budget

# E. 结果截断 4KB
print("== E 工具结果 4KB 截断 ==")
_real_exec = tools_read.execute


def _big_exec(db_, name, params):
    return {"items": ["x" * 10000]}


tools_read.execute = _big_exec
SCRIPT.append(_tool_script("query_assets", {}))
SCRIPT.append(_answer_script("截断测试回答。"))
r = client.post(f"/api/agent/sessions/{sid}/messages", json={"q": "截断测试"})
tools_read.execute = _real_exec
_db = SessionLocal()
msg = (_db.query(AgentMessage)
       .filter(AgentMessage.session_id == sid, AgentMessage.role == "assistant")
       .order_by(AgentMessage.id.desc()).first())
tc = json.loads(msg.tool_calls_json or "[]")[0]
check("E result_summary ≤4KB+截断标记", len(tc["result_summary"]) <= tools_read.RESULT_MAX_CHARS + 30
      and "（已截断" in tc["result_summary"], f"len={len(tc['result_summary'])}")
_db.close()

# F. abort 生效
print("== F abort 生效 ==")
block_ev = threading.Event()
abort_ok = []
SCRIPT.append({"chunks": ["第一段回答内容。", "第二段被中断。", "第三段不该到达。"],
               "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
               "block_after": 0, "block_event": block_ev})


def _do_abort():
    time.sleep(0.3)
    r2 = client.post(f"/api/agent/sessions/{sid}/abort")
    abort_ok.append(r2.status_code == 200 and r2.json() == {"aborted": True})
    block_ev.set()


t = threading.Thread(target=_do_abort)
t.start()
r = client.post(f"/api/agent/sessions/{sid}/messages", json={"q": "中断测试问题"})
t.join()
check("F abort 端点返回 aborted", bool(abort_ok) and abort_ok[0])
events = sse_parse(r.text)
token_txt = "".join(d.get("text", "") for e, d in events if e == "token")
check("F 中断保留已生成内容", "第一段回答内容" in token_txt and "第三段不该到达" not in token_txt,
      token_txt[:200])
check("F 中断后仍收尾 done", events[-1][0] == "done", str([e for e, _ in events]))
check("F 新一轮清除中断标志", not copilot_loop.is_aborted(sid))

# G. 快照接口 + 路由注册
print("== G 系统快照接口 + 路由注册 ==")
r = client.get("/api/agent/system-snapshot")
snap = r.json()
check("G 快照五面齐全", r.status_code == 200 and all(
    k in snap for k in ("cloud_accounts", "datasources", "assets", "topology", "recent_changes")),
    str(list(snap.keys())))
paths = {getattr(x, "path", "") for x in app.routes}
check("路由挂载 sessions/messages/abort/snapshot", all(
    p in paths for p in ("/api/agent/system-snapshot", "/api/agent/sessions",
                         "/api/agent/sessions/{session_id}/messages",
                         "/api/agent/sessions/{session_id}/abort")), str(sorted(paths)[:6]))

print(f"\n结果: {len(PASS)} 通过 / {len(FAIL)} 失败")
if FAIL:
    print("失败项:", FAIL)
sys.exit(1 if FAIL else 0)
