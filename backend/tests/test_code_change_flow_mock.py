#!/usr/bin/env python3
"""L3b 代码变更全链路 mock 测试：意图细分→计划→old 幻觉拒→审查→
工单→确认门→apply→验证门过/不过自动回滚→快照 API。

运行：cd /opt/qianshu/backend && python3 tests/test_code_change_flow_mock.py
隔离性：MASTER_KEY/DATABASE_URL 等在 import app.* 之前改写为临时值——不碰真实
qianshu.db；LLM 走本机 127.0.0.1 假 OpenAI 兼容服务（双角色：MAIN 主答/REVIEW 审查，
按 system 提示词分诊，流式/非流式按 body.stream 分支），零外部网络。
code_mod 用**真模块**（真快照/真写文件/真验证门 py_compile），只动自建测试文件
（docs/tmp/_tmp_code_change_*.py，跑完清理）——不碰生产代码文件。不启 8810。

验证项：
  A 端到端（edits 格式）：强关键词→change_code 计划→工单（全文入 change_list_json 不加密）
    →SSE change 带 diff_summary（事件级+项级）/tool/restart_required→确认（ack）
    →apply 真写文件→verify 过→done 回执含「需重启生效」→快照 API 列表（diff_summary）
    →一键回滚（admin）→文件字节恢复+审计
  B 验证门不过自动回滚（全文 new 格式）：坏代码→apply→verify 不过→auto_rollback→
    工单项 failed+回执含失败原因；文件恢复原状不留半成品
  C old 幻觉拒：old 与现文件不一致→重试一轮成功建单；两次都不过→降级人工步骤不建单
  D 禁改域拒（审查+validate 双保险）：规则审查拒（不建单，放弃并解释）+validate_plan 拒
    （单测）+build_ticket 拒；绕过审查的篡改工单在执行期被 validate_plan 拒（项 failed）
  E 意图细分（关键词+LLM 判断）：弱信号→LLM 判 false→走配置级；弱信号→判 true→走代码路径
  F code_mod 未就绪优雅降级：不建单、给人工步骤
  G ticket_out 展示口径：code 项 diff_summary/restart_required/截断 diff
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
_TMPDIR = tempfile.mkdtemp(prefix="qianshu-codechange-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("codechange")
os.environ["MASTER_KEY"] = "ab" * 32
os.environ["EVENT_WEBHOOK_TOKEN"] = "test-token"
os.environ["METRIC_SOURCES"] = "[]"
os.environ["LOG_SOURCES"] = "[]"
os.environ["VM_URL"] = "http://127.0.0.1:1"
# 自改经验沉淀落点重定向——不污染真知识包 agent/knowledge/
os.environ["QIANSHU_KNOWLEDGE_DIR"] = os.path.join(_TMPDIR, "knowledge")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.core.db import get_db  # noqa: E402
from app.core.deps import get_current_user  # noqa: E402
from app.core.security import encrypt_secret, hash_password  # noqa: E402

# 全量模型注册后再 create_all（不触发 app.main 的启动建表）
import importlib  # noqa: E402
import pkgutil  # noqa: E402
import app.models as _models_pkg  # noqa: E402
for _m in pkgutil.iter_modules(_models_pkg.__path__):
    importlib.import_module(f"app.models.{_m.name}")

from app.models.agent_change import AgentChangeTicket  # noqa: E402
from app.models.ai_setting import AiSetting  # noqa: E402
from app.models.audit import AuditLog  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.copilot import change_tools, reviewer  # noqa: E402
from app.services.copilot import code_mod  # noqa: E402  真模块（快照/验证门/回滚真跑）

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # /opt/qianshu
PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


# ---------------- 假 OpenAI 兼容服务（双角色：MAIN 主答 / REVIEW 审查） ----------------

MAIN_SCRIPT: list = []      # 主路径应答脚本（计划/判定器/直答，按序出栈）
REVIEW_SCRIPT: list = []    # 审查员 verdict 脚本（按序出栈）
REQUESTS: list = []         # {"role","stream","system","body"}


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        sys_msg = next((m.get("content", "") for m in body.get("messages", [])
                        if m.get("role") == "system"), "")
        role = "review" if "审查员" in sys_msg else "main"
        REQUESTS.append({"role": role, "stream": bool(body.get("stream")),
                         "system": sys_msg[:120], "body": body})
        queue = REVIEW_SCRIPT if role == "review" else MAIN_SCRIPT
        resp = queue.pop(0) if queue else {"chunks": ["（无脚本）"], "usage": {}}
        usage = resp.get("usage") or {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        self.send_response(200)
        try:
            if not body.get("stream"):
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


def _plan_script(plan: dict) -> dict:
    return {"chunks": [json.dumps(plan, ensure_ascii=False)]}


def _text_script(text: str) -> dict:
    return {"chunks": [text]}


def _verdict_script(verdict: dict) -> dict:
    return {"chunks": [json.dumps(verdict, ensure_ascii=False)]}


def sse_parse(text: str) -> list:
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


# ---------------- 启动假 LLM + 建表 + 种子 ----------------

server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
PORT = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()

Base.metadata.create_all(engine)

_db = SessionLocal()
admin = User(username="admin", password_hash=hash_password("Fixture-Pw-#2026!"), role="admin")
operator = User(username="operator", password_hash=hash_password("Fixture-Pw-#2026!"), role="operator")
viewer = User(username="viewer", password_hash=hash_password("Fixture-Pw-#2026!"), role="viewer")
_db.add_all([admin, operator, viewer])
_db.commit()
setting = AiSetting(
    provider_label="fake-llm",
    base_url=f"http://127.0.0.1:{PORT}",
    api_key_cipher=encrypt_secret("sk-test"),
    model="fake-model", temperature=0.2, max_tokens=2048, enabled=True,
)
_db.add(setting)
_db.commit()
ADMIN_ID, OPER_ID, VIEW_ID = admin.id, operator.id, viewer.id
_db.close()

from app.main import app  # noqa: E402  建表/种子后导入（路由全量注册）

STUBS = {
    "admin": SimpleNamespace(id=ADMIN_ID, username="admin", role="admin", is_active=True),
    "operator": SimpleNamespace(id=OPER_ID, username="operator", role="operator", is_active=True),
    "viewer": SimpleNamespace(id=VIEW_ID, username="viewer", role="viewer", is_active=True),
}
CURRENT = {"user": STUBS["admin"]}
def _ov_get_db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


app.dependency_overrides[get_db] = _ov_get_db
app.dependency_overrides[get_current_user] = lambda: CURRENT["user"]


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


def new_session() -> int:
    r = client.post("/api/agent/sessions", json={})
    return r.json()["id"]


def ask(sid: int, q: str) -> list:
    r = client.post(f"/api/agent/sessions/{sid}/messages", json={"q": q})
    return sse_parse(r.text)


def code_tickets() -> list:
    return [t for t in client.get("/api/agent/changes").json()["items"]
            if any(i.get("tool") == "code_modify" for i in t.get("change_list") or [])]


# ---------------- 自建测试文件（docs/tmp/ 可改域；跑完清理） ----------------

TMP_FILES = {}


def make_tmp(tag: str, content: str) -> str:
    rel = f"docs/tmp/_tmp_code_change_{tag}.py"
    path = os.path.join(ROOT, rel)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    TMP_FILES[tag] = path
    return rel


def cleanup_tmp():
    for p in TMP_FILES.values():
        try:
            os.unlink(p)
        except OSError:
            pass


for _f in os.listdir(os.path.join(ROOT, "docs/tmp")):   # 起步清理（防上次残留）
    if _f.startswith("_tmp_code_change_"):
        try:
            os.unlink(os.path.join(ROOT, "docs/tmp", _f))
        except OSError:
            pass

SEC_PATH = os.path.join(ROOT, "backend/app/core/security.py")
_sec_text = open(SEC_PATH, encoding="utf-8").read()
_sec_line = next(ln for ln in _sec_text.splitlines()
                 if len(ln.strip()) >= 10 and _sec_text.count(ln) == 1)

# ================ A. 端到端（edits 格式：计划→工单→确认→apply→verify 过→快照→回滚） ================
print("== A 端到端（edits 格式） ==")
rel_a = make_tmp("a", 'VALUE = 1\n\n\ndef hello():\n    return "hello"\n')
content_a0 = open(TMP_FILES["a"], encoding="utf-8").read()
sid_a = new_session()
PLAN_A = {"title": "改页面展示逻辑", "plan": {
    "files": [{"path": rel_a, "edits": [{"old": "VALUE = 1", "new": "VALUE = 2"}]}],
    "summary": "调整展示逻辑取值", "impact": "页面展示值"}}
MAIN_SCRIPT.append(_plan_script(PLAN_A))
REVIEW_SCRIPT.append(_verdict_script({"issues": [], "pass": True}))
events = ask(sid_a, "帮我改一下这个页面的展示逻辑")
kinds = [e for e, _ in events]
check("A 事件序 snapshot→…→change→done", kinds[0] == "snapshot" and "change" in kinds
      and kinds[-1] == "done", str(kinds))
change_ev = [d for e, d in events if e == "change"]
check("A change 事件存在", bool(change_ev), str(kinds))
if change_ev:
    ev = change_ev[0]
    check("A 事件级 diff_summary 契约 {files:[{path,add_lines,del_lines,diff_head}]}",
          isinstance(ev.get("diff_summary"), dict)
          and ev["diff_summary"].get("files")
          and {"path", "add_lines", "del_lines", "diff_head"} <= set(ev["diff_summary"]["files"][0])
          and ev["diff_summary"]["files"][0]["add_lines"] == 1
          and ev["diff_summary"]["files"][0]["del_lines"] == 1, str(ev.get("diff_summary"))[:300])
    it0 = (ev.get("change_list") or [{}])[0]
    check("A 项级字段 tool/params/diff_summary/restart_required/sensitive",
          it0.get("tool") == "code_modify" and isinstance(it0.get("params"), dict)
          and isinstance(it0.get("diff_summary"), dict) and it0.get("restart_required") is True
          and it0.get("sensitive") is True and it0.get("ack_required") is True, str(it0)[:300])
    check("A diff_head 含新旧原文", "VALUE = 1" in (it0.get("diff_summary") or {}).get("files", [{}])[0].get("diff_head", "")
          and "VALUE = 2" in it0["diff_summary"]["files"][0]["diff_head"])
    tid_a = ev["ticket_id"]
else:
    tid_a = 0

_db = SessionLocal()
row = _db.get(AgentChangeTicket, tid_a)
check("A 工单 pending 未执行", row is not None and row.status == "pending")
check("A 计划全文入 change_list_json（代码非凭证不加密）",
      row is not None and "VALUE = 1" in (row.change_list_json or "")
      and "VALUE = 2" in (row.change_list_json or "")
      and "enc:v1:" not in (row.change_list_json or ""), (row.change_list_json or "")[:200])
_db.close()
check("A 确认前文件未动", open(TMP_FILES["a"], encoding="utf-8").read() == content_a0)

r = client.post(f"/api/agent/changes/{tid_a}/confirm", json={"ack": "我已知晓"})
data = r.json()
check("A ack 确认执行成功（done）", r.status_code == 200 and data.get("status") == "done", r.text[:300])
check("A 文件已应用（VALUE = 2）", open(TMP_FILES["a"], encoding="utf-8").read() != content_a0
      and "VALUE = 2" in open(TMP_FILES["a"], encoding="utf-8").read())
res0 = (data.get("results") or [{}])[0]
receipt = json.dumps(res0.get("result") or {}, ensure_ascii=False)
check("A 回执含「需重启生效」提示 + snapshot_id", "需重启生效" in receipt and "snapshot_id" in receipt,
      receipt[:300])

r = client.get(f"/api/agent/code-snapshots?ticket_id={tid_a}")
snaps = r.json()
check("A 快照 API：列表+diff_summary", r.status_code == 200 and snaps.get("items")
      and snaps["items"][0].get("files")
      and {"path", "add_lines", "del_lines", "diff_head"} <= set(snaps["items"][0]["files"][0]),
      json.dumps(snaps, ensure_ascii=False)[:300])
sid_snap = snaps["items"][0]["id"] if snaps.get("items") else 0
check("A 快照 diff_summary 文件与行数", snaps["items"][0]["files"][0]["path"] == rel_a
      and snaps["items"][0]["files"][0]["add_lines"] == 1
      and snaps["items"][0]["files"][0]["del_lines"] == 1,
      json.dumps(snaps, ensure_ascii=False)[:300])

CURRENT["user"] = STUBS["operator"]
r = client.post(f"/api/agent/code-snapshots/{sid_snap}/rollback", json={})
check("A 非 admin 回滚快照 → 403", r.status_code == 403, r.text[:200])
CURRENT["user"] = STUBS["viewer"]
r = client.post(f"/api/agent/code-snapshots/{sid_snap}/rollback", json={})
check("A viewer 回滚快照 → 403", r.status_code == 403, r.text[:200])
CURRENT["user"] = STUBS["admin"]

r = client.post(f"/api/agent/code-snapshots/{sid_snap}/rollback", json={})
check("A admin 一键回滚快照成功", r.status_code == 200 and r.json().get("rolled_back") is True,
      r.text[:300])
check("A 回滚后文件字节恢复", open(TMP_FILES["a"], encoding="utf-8").read() == content_a0)
r = client.post(f"/api/agent/code-snapshots/{sid_snap}/rollback", json={})
check("A 再次回滚幂等（already）", r.status_code == 200 and r.json().get("already") is True, r.text[:200])

_db = SessionLocal()
actions = {a for (a,) in _db.query(AuditLog.action).all()}
check("A 审计全链（create/confirm/execute + code.apply + 回滚留痕）",
      {"agent.change.create", "agent.change.confirm", "agent.change.execute",
       "code.apply", "agent.change.rollback"} <= actions, str(sorted(actions)))
_db.close()

# ================ B. 验证门不过自动回滚（全文 new 格式） ================
print("== B 验证门不过自动回滚（new 全文格式） ==")
rel_b = make_tmp("b", 'VALUE_B = 10\n\n\ndef hello_b():\n    return "hi"\n')
content_b0 = open(TMP_FILES["b"], encoding="utf-8").read()
sid_b = new_session()
PLAN_B = {"title": "改提示文案", "plan": {
    "files": [{"path": rel_b, "new": "def broken(:\n"}],
    "summary": "改坏的语法（模拟验证门）", "impact": "无"}}
MAIN_SCRIPT.append(_plan_script(PLAN_B))
REVIEW_SCRIPT.append(_verdict_script({"issues": [], "pass": True}))
events = ask(sid_b, "帮我改一下这个页面的提示文案")
tid_b = [d for e, d in events if e == "change"][0]["ticket_id"]
r = client.post(f"/api/agent/changes/{tid_b}/confirm", json={"ack": "我已知晓"})
data = r.json()
res0 = (data.get("results") or [{}])[0]
err_text = json.dumps(res0.get("result") or {}, ensure_ascii=False)
check("B 项 failed + 回执含失败原因", r.status_code == 200 and res0.get("status") == "failed"
      and "验证门未过" in err_text and "自动回滚" in err_text, err_text[:300])
check("B 工单 failed", data.get("status") == "failed", r.text[:200])
check("B 不留半成品（文件字节恢复）", open(TMP_FILES["b"], encoding="utf-8").read() == content_b0)
r = client.get(f"/api/agent/code-snapshots?ticket_id={tid_b}")
snaps_b = r.json()
check("B 快照留痕且标 rolled_back", snaps_b.get("items")
      and snaps_b["items"][0].get("status") == "rolled_back",
      json.dumps(snaps_b, ensure_ascii=False)[:200])

# ================ C. old 幻觉拒（重试一轮成功 / 两次不过降级人工步骤） ================
print("== C old 幻觉拒 ==")
rel_c = make_tmp("c", 'VALUE_C = 3\n\n\ndef hello_c():\n    return "c"\n')
sid_c = new_session()
BAD_OLD = {"title": "幻觉计划", "plan": {
    "files": [{"path": rel_c, "edits": [{"old": "NO_SUCH_LINE_XYZ_12345", "new": "VALUE_C = 4"}]}],
    "summary": "幻觉 old", "impact": "无"}}
GOOD_PLAN = {"title": "改取值", "plan": {
    "files": [{"path": rel_c, "edits": [{"old": "VALUE_C = 3", "new": "VALUE_C = 4"}]}],
    "summary": "改取值", "impact": "无"}}
n_req_before = len([q for q in REQUESTS if "代码变更规划器" in q["system"]])
MAIN_SCRIPT.append(_plan_script(BAD_OLD))
MAIN_SCRIPT.append(_plan_script(GOOD_PLAN))
REVIEW_SCRIPT.append(_verdict_script({"issues": [], "pass": True}))
events = ask(sid_c, "帮我改一下这个页面的排序逻辑")
n_plan_calls = len([q for q in REQUESTS if "代码变更规划器" in q["system"]]) - n_req_before
check("C 幻觉 old 触发一轮重试（计划调用 2 次）", n_plan_calls == 2, f"calls={n_plan_calls}")
check("C 重试带文件现状上下文", any("目标文件现状" in json.dumps(q["body"], ensure_ascii=False)
      for q in REQUESTS if "代码变更规划器" in q["system"]))
check("C 重试后建单成功", any(e == "change" for e, _ in events), str([e for e, _ in events]))
check("C 文件未被幻觉计划改动", "VALUE_C = 3" in open(TMP_FILES["c"], encoding="utf-8").read())
tid_c = [d for e, d in events if e == "change"][0]["ticket_id"]
r = client.post(f"/api/agent/changes/{tid_c}/confirm", json={"ack": "我已知晓"})
check("C 修正后计划可执行", r.json().get("status") == "done", r.text[:200])

sid_c2 = new_session()
n_tickets_before = len(code_tickets())
content_c_before = open(TMP_FILES["c"], encoding="utf-8").read()
MAIN_SCRIPT.append(_plan_script(BAD_OLD))
MAIN_SCRIPT.append(_plan_script(BAD_OLD))
events = ask(sid_c2, "帮我改一下这个页面的展示样式")
token_txt = "".join(d.get("text", "") for e, d in events if e == "token")
check("C 两次幻觉 → 降级人工步骤", "人工步骤" in token_txt, token_txt[:200])
check("C 降级不建单", len(code_tickets()) == n_tickets_before)
check("C 降级不动文件", open(TMP_FILES["c"], encoding="utf-8").read() == content_c_before)

# ================ D. 禁改域拒（审查+validate 双保险） ================
print("== D 禁改域（审查+validate 双保险） ==")
FORBIDDEN_PLAN = {"files": [{"path": "backend/app/core/security.py",
                             "edits": [{"old": _sec_line, "new": _sec_line + "  # x"}]}],
                  "summary": "改鉴权", "impact": "越界"}
issues = reviewer.check_diff_rules(FORBIDDEN_PLAN)
check("D① 审查规则层拒禁改域（forbidden_domain）",
      any(i.get("kind") == "forbidden_domain" for i in issues), str(issues)[:200])
v = code_mod.validate_plan({"files": [{"path": "backend/app/core/security.py", "new": "x = 1\n"}]})
check("D② validate_plan 拒禁改域", v.get("ok") is False and v.get("violations"), str(v)[:200])
try:
    change_tools.build_ticket(SessionLocal(), 0, 0,
                              [{"tool": "code_modify", "params": {"plan": FORBIDDEN_PLAN}}],
                              STUBS["admin"])
    check("D② build_ticket 拒禁改域（check_fn）", False)
except change_tools.ChangeToolError as e:
    check("D② build_ticket 拒禁改域（check_fn）", True, str(e))
except Exception as e:   # noqa: BLE001
    check("D② build_ticket 拒禁改域（check_fn）", False, f"{type(e).__name__}: {e}")

# E2E 审查层：两轮都触碰禁改域 → 放弃并解释，不建单
sid_d = new_session()
n_tickets_before = len(code_tickets())
MAIN_SCRIPT.append(_plan_script({"title": "改鉴权", "plan": FORBIDDEN_PLAN}))
MAIN_SCRIPT.append(_plan_script({"title": "改鉴权", "plan": FORBIDDEN_PLAN}))
events = ask(sid_d, "帮我改一下权限逻辑")
review_evs = [d for e, d in events if e == "review"]
token_txt = "".join(d.get("text", "") for e, d in events if e == "token")
check("D 审查两轮均拒（review 事件 pass=false 含 forbidden_domain）",
      len(review_evs) == 2 and all(ev.get("pass") is False for ev in review_evs)
      and any(i.get("kind") == "forbidden_domain"
              for ev in review_evs for i in (ev.get("issues") or [])), str(review_evs)[:300])
check("D 再审不过 → 放弃并解释", "放弃" in token_txt, token_txt[:200])
check("D 审查层拒后不建单", len(code_tickets()) == n_tickets_before)

# E2E validate 层：绕过审查的篡改工单在执行期被 validate_plan 拒
rel_d = make_tmp("d", 'VALUE_D = 7\n\n\ndef hello_d():\n    return "d"\n')
_db = SessionLocal()
tid_d2 = change_tools.build_ticket(
    _db, 0, 0, [{"tool": "code_modify", "params": {"plan": {
        "files": [{"path": rel_d, "edits": [{"old": "VALUE_D = 7", "new": "VALUE_D = 8"}]}],
        "summary": "正常计划", "impact": "无"}}}], STUBS["admin"])["id"]
row = _db.get(AgentChangeTicket, tid_d2)
cl = json.loads(row.change_list_json)
cl[0]["params"]["plan"]["files"] = [{"path": "backend/app/core/security.py",
                                     "edits": [{"old": _sec_line, "new": _sec_line + "  # x"}]}]
row.change_list_json = json.dumps(cl, ensure_ascii=False)
_db.commit()
_db.close()
r = client.post(f"/api/agent/changes/{tid_d2}/confirm", json={"ack": "我已知晓"})
res0 = (r.json().get("results") or [{}])[0]
err_text = json.dumps(res0.get("result") or {}, ensure_ascii=False)
check("D 执行期 validate_plan 拒（篡改工单项 failed）", res0.get("status") == "failed"
      and ("计划校验未过" in err_text or "禁改域" in err_text), err_text[:300])
check("D 执行期不动禁改文件", _sec_text == open(SEC_PATH, encoding="utf-8").read())

# ================ E. 意图细分（关键词+LLM 判断） ================
print("== E 意图细分（关键词+LLM 判断） ==")
sid_e1 = new_session()
n_tickets_before = len(code_tickets())
MAIN_SCRIPT.append(_text_script("false"))                 # 判定器：非代码变更
MAIN_SCRIPT.append(_plan_script({"title": "调阈值", "plan": [
    {"tool": "save_alert_rule", "params": {
        "name": "_mock_code_flow_rule", "source": "metric", "product": "ecs",
        "metric": "cpu_usage", "threshold": 90}}]}))
events = ask(sid_e1, "帮我改一下这个列表的排序方式")
check("E 弱信号走 LLM 判定", any("意图判定器" in q["system"] for q in REQUESTS))
change_evs = [d for e, d in events if e == "change"]
check("E 判 false → 配置级工单（非 code_modify）", change_evs
      and change_evs[0]["change_list"][0].get("tool") == "save_alert_rule", str(change_evs)[:300])

sid_e2 = new_session()
n_tickets_before = len(code_tickets())
MAIN_SCRIPT.append(_text_script("true"))                  # 判定器：代码变更
MAIN_SCRIPT.append(_text_script("（人工步骤）这个诉求需要改前端组件与后端接口，建议人工分两步处理。"))
events = ask(sid_e2, "帮我改一下提示语的展示位置")
check("E 判 true → 进代码路径（规划器提示词）",
      any("代码变更规划器" in q["system"] for q in REQUESTS))
token_txt = "".join(d.get("text", "") for e, d in events if e == "token")
check("E 代码路径 prose 直答透传", "人工步骤" in token_txt, token_txt[:200])
check("E prose 直答不建单", len(code_tickets()) == n_tickets_before)

# ================ F. code_mod 未就绪优雅降级 ================
print("== F code_mod 未就绪降级 ==")
_real_cm = change_tools.code_mod_module
change_tools.code_mod_module = lambda: None
fallback_allow, _ = change_tools._code_boundaries()
check("F 降级白名单只允许 active/tmp",
      "docs/active/" in fallback_allow and "docs/tmp/" in fallback_allow
      and "docs/" not in fallback_allow, str(fallback_allow))
check("F 降级边界拒绝 archive",
      bool(change_tools.code_boundary_violations(["docs/archive/history.md"])))
sid_f = new_session()
n_tickets_before = len(code_tickets())
n_req_before = len(REQUESTS)
events = ask(sid_f, "帮我改一下这个页面的展示逻辑")
token_txt = "".join(d.get("text", "") for e, d in events if e == "token")
check("F 未就绪给人工步骤", "暂未就绪" in token_txt, token_txt[:200])
check("F 未就绪不建单", len(code_tickets()) == n_tickets_before)
check("F 未就绪零 LLM 调用", len(REQUESTS) == n_req_before,
      f"req {n_req_before}->{len(REQUESTS)}")
change_tools.code_mod_module = _real_cm

# ================ G. ticket_out 展示口径（截断/标记） ================
print("== G ticket_out 展示口径 ==")
rel_g = make_tmp("g", "a" * 500 + "\n")
c = change_tools._display_item({"tool": "code_modify", "idx": 0, "title": "t",
                                "params": {"plan": {"files": [
                                    {"path": rel_g, "edits": [
                                        {"old": "a" * 500, "new": "b" * 500}]}],
                                    "summary": "s", "impact": "i"}}})
ed = c["params"]["plan"]["files"][0]["edits"][0]
check("G diff 文本截断（全文仍在库）", "截断" in ed["old"] and "截断" in ed["new"]
      and len(ed["old"]) < 400, ed["old"][:80])
check("G 项级 diff_summary/restart_required", isinstance(c.get("diff_summary"), dict)
      and c.get("restart_required") is True
      and c["diff_summary"]["files"][0]["path"] == rel_g
      and c["diff_summary"]["files"][0]["add_lines"] == 1, str(c)[:300])
check("G 非 code 项原样", change_tools._display_item({"tool": "save_alert_rule"}) ==
      {"tool": "save_alert_rule"})

# ================ H. 工单一键回滚（rollback_fn → code_mod.rollback） ================
print("== H 工单一键回滚（rollback_fn） ==")
rel_h = make_tmp("h", 'VALUE_H = 1\n\n\ndef hello_h():\n    return "h"\n')
content_h0 = open(TMP_FILES["h"], encoding="utf-8").read()
sid_h = new_session()
PLAN_H = {"title": "改取值", "plan": {
    "files": [{"path": rel_h, "edits": [{"old": "VALUE_H = 1", "new": "VALUE_H = 2"}]}],
    "summary": "改取值", "impact": "无"}}
MAIN_SCRIPT.append(_plan_script(PLAN_H))
REVIEW_SCRIPT.append(_verdict_script({"issues": [], "pass": True}))
events = ask(sid_h, "帮我改一下这个页面的展示逻辑")
tid_h = [d for e, d in events if e == "change"][0]["ticket_id"]
r = client.post(f"/api/agent/changes/{tid_h}/confirm", json={"ack": "我已知晓"})
check("H 确认执行成功", r.json().get("status") == "done", r.text[:200])
check("H 文件已应用", "VALUE_H = 2" in open(TMP_FILES["h"], encoding="utf-8").read())
r = client.post(f"/api/agent/changes/{tid_h}/rollback", json={})
ok_h = r.status_code == 200 and r.json().get("rolled_back") is True
check("H 工单一键回滚成功", ok_h, r.text[:300])
check("H 回滚后文件字节恢复", open(TMP_FILES["h"], encoding="utf-8").read() == content_h0)
_db = SessionLocal()
t_h = _db.get(AgentChangeTicket, tid_h)
check("H 工单项 rolled_back", t_h is not None and t_h.status == "rolled_back"
      and json.loads(t_h.change_list_json)[0]["status"] == "rolled_back", "")
_db.close()

# ---------------- 收尾 ----------------
cleanup_tmp()
print(f"\n== 结果: {len(PASS)} 通过 / {len(FAIL)} 失败 ==")
if FAIL:
    print("失败项:", FAIL)
    sys.exit(1)
print("ALL PASS")
