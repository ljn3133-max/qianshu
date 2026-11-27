#!/usr/bin/env python3
"""千枢智能体变更层 mock 测试：白名单+确认门+快照+回滚全链路。

运行：cd /opt/qianshu/backend && python3 tests/test_copilot_change_mock.py
隔离性：MASTER_KEY/DATABASE_URL 等在 import app.* 之前改写为临时值——不碰真实
qianshu.db；LLM 走本机 127.0.0.1 假 OpenAI 兼容服务（变更计划 JSON 由脚本喂给），
零外部网络。ASGITransport 壳不触发 lifespan/startup，get_db/get_current_user 走依赖覆盖。

验证项：
  A 变更计划路径：SSE change 事件 + pending 工单落库（不执行）
  B 变更红线：白名单外工具拒绝 / 危险类（删掉这个表）恒拒绝 / build_ticket 拒未知工具
  C 确认门 403 多种绕法：无 ack / 错 ack / 重放 / 非 pending（dismissed/rolled_back/done）
    / 直调执行端点不存在（404）/ 服务层直调非 pending 403 / 非 admin 确认 403
  D ack 确认执行：云账号+归属真写入；快照 before/after 落库；cloud.account.ack 审计
  E 回滚真能回：新建→回滚后不存在；修改→回滚后回旧值
  F dismiss：未执行工单可取消；取消后确认 403
  G 部分失败续跑：单项失败不阻断后续项；失败项可续跑重试；快照按次留存（版本递增）
  H 审计全链 + ACTION_LABELS 中文标签登记 + 列表 operator/viewer 可读
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
_TMPDIR = tempfile.mkdtemp(prefix="qianshu-change-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("change")
os.environ["MASTER_KEY"] = "ab" * 32
os.environ["EVENT_WEBHOOK_TOKEN"] = "test-token"
os.environ["METRIC_SOURCES"] = "[]"
os.environ["LOG_SOURCES"] = "[]"
os.environ["VM_URL"] = "http://127.0.0.1:1"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.core.db import get_db  # noqa: E402
from app.core.deps import get_current_user  # noqa: E402
from app.core.security import encrypt_secret, hash_password  # noqa: E402

# 全量模型注册后再 create_all（app/models/__init__ 未覆盖 llm_call_logs/alert_rules 等，
# 不注册就建表会漏表——本测试不触发 app.main 的启动建表）
import importlib  # noqa: E402
import pkgutil  # noqa: E402
import app.models as _models_pkg  # noqa: E402
for _m in pkgutil.iter_modules(_models_pkg.__path__):
    importlib.import_module(f"app.models.{_m.name}")

from app.models.agent_change import AgentChangeSnapshot, AgentChangeTicket  # noqa: E402
from app.models.agent_session import AgentMessage  # noqa: E402
from app.models.ai_setting import AiSetting  # noqa: E402
from app.models.audit import AuditLog  # noqa: E402
from app.models.cloud_account import CloudAccount  # noqa: E402
from app.models.ownership import OwnershipRule  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services import audit as audit_svc  # noqa: E402
from app.services.copilot import change_gate, change_tools  # noqa: E402
from app.services.cloud import sync_job as _sync_job  # noqa: E402

# 本测试只验证确认工单和账号落库，不执行建号后的真实云后台接入。
_sync_job.bootstrap_account = lambda _account_id: None

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


# ---------------- 假 OpenAI 兼容服务（真 SSE 流式） ----------------

SCRIPT: list = []       # 每次 LLM 调用的应答脚本（按序出栈）
REQUESTS: list = []     # 收到的请求


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
            for text in resp.get("chunks") or []:
                payload = {"choices": [{"delta": {"content": text}}]}
                self.wfile.write(("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode())
                self.wfile.flush()
            usage = resp.get("usage") or {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
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


def _answer_script(text: str) -> dict:
    return {"chunks": [text]}


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


def _ticket(tid: int):
    _db = SessionLocal()
    t = _db.get(AgentChangeTicket, tid)
    out = change_tools.ticket_out(t) if t else None
    _db.close()
    return out


# ================ A. 变更计划路径（SSE change + 工单落库，不执行） ================
print("== A 变更计划路径 ==")
sid = new_session()
PLAN_A = {"title": "新增云账号并登记归属", "plan": [
    {"tool": "create_cloud_account", "params": {
        "name": "_mock_acct", "vendor": "aliyun",
        "ak": "AKmock123456", "sk": "SKmock123456", "region": "cn-hangzhou"}},
    {"tool": "save_ownership_rule", "params": {
        "key_type": "domain", "key_value": "_mock.example.com", "business": "测试业务"}},
]}
SCRIPT.append(_plan_script(PLAN_A))
events = ask(sid, "帮我新增一个名为 _mock_acct 的阿里云云账号，并登记归属")
kinds = [e for e, _ in events]
check("A 事件序 snapshot→token→change→done", kinds[0] == "snapshot" and "change" in kinds
      and kinds[-1] == "done", str(kinds))
change_ev = [d for e, d in events if e == "change"]
check("A change 事件字段", bool(change_ev) and {"ticket_id", "title", "change_list"} <= set(change_ev[0])
      and len(change_ev[0]["change_list"]) == 2, str(change_ev)[:300])
cl = change_ev[0]["change_list"][0] if change_ev else {}
check("A change_list 项要素齐全", {"idx", "title", "impact", "rollback", "ack_required"} <= set(cl)
      and cl["ack_required"] is True and "云账号" in cl["title"], str(cl)[:300])
check("A 第二项无需 ack", change_ev[0]["change_list"][1]["ack_required"] is False)
tid = change_ev[0]["ticket_id"] if change_ev else 0
t = _ticket(tid)
check("A 工单 pending 未执行", t and t["status"] == "pending"
      and all(i["status"] == "pending" and i["result"] is None for i in t["change_list"]),
      str(t)[:300])
check("A 工单项含 idx/title/impact/rollback/tool/params/ack_required/status/result",
      all({"idx", "title", "impact", "rollback", "tool", "params", "ack_required",
           "status", "result"} <= set(i) for i in t["change_list"]))
check("A 工单回链消息 msg_id", t["msg_id"] > 0)
_db = SessionLocal()
check("A 确认前无云账号写入（只建单不执行）",
      _db.query(CloudAccount).filter(CloudAccount.name == "_mock_acct").first() is None)
msg = (_db.query(AgentMessage).filter(AgentMessage.session_id == sid,
                                      AgentMessage.role == "assistant")
       .order_by(AgentMessage.id.desc()).first())
check("A 助手消息带变更清单摘要", msg is not None and "变更清单" in (msg.content_md or ""), str(msg)[:200])
_db.close()

# ================ B. 变更红线：白名单外拒绝 ================
print("== B 变更红线 ==")
sid_b = new_session()
SCRIPT.append(_plan_script({"title": "删表", "plan": [
    {"tool": "drop_table", "params": {"table": "users"}}]}))
events = ask(sid_b, "帮我删除 users 表，换个新结构")
kinds_b = [e for e, _ in events]
check("B 白名单外工具不建工单", all(e != "change" for e in kinds_b), str(kinds_b))
token_txt = "".join(d.get("text", "") for e, d in events if e == "token")
check("B 白名单外拒绝有解释", "拒绝" in token_txt and "白名单" in token_txt, token_txt[:200])
bad_tickets = [t for t in client.get("/api/agent/changes").json()["items"]
               if "drop_table" in json.dumps(t, ensure_ascii=False)]
check("B 无新工单", not bad_tickets)

n_req = len(REQUESTS)
events = ask(sid_b, "帮我删掉这个表")
token_txt = "".join(d.get("text", "") for e, d in events if e == "token")
check("B 危险类（删掉这个表）恒拒绝", "拒绝" in token_txt and "永远不在" in token_txt, token_txt[:200])
check("B 危险类不调用 LLM（规则层先拦）", len(REQUESTS) == n_req, f"req {n_req}->{len(REQUESTS)}")
check("B 危险类不建工单", all(e != "change" for e, _ in events))

try:
    change_tools.build_ticket(SessionLocal(), sid_b, 0,
                              [{"tool": "exec_sql", "params": {"sql": "drop table users"}}],
                              STUBS["admin"])
    check("B build_ticket 拒未知工具", False)
except change_tools.ChangeToolError:
    check("B build_ticket 拒未知工具", True)
except Exception as e:   # noqa: BLE001
    check("B build_ticket 拒未知工具", False, f"{type(e).__name__}: {e}")

# ================ C. 确认门 403 家族（多种绕法） ================
print("== C 确认门 403 ==")
r = client.post(f"/api/agent/changes/{tid}/confirm", json={})
check("C 无 ack 直调 confirm → 403", r.status_code == 403 and "我已知晓" in r.text, r.text[:200])
r = client.post(f"/api/agent/changes/{tid}/confirm", json={"ack": "已知晓"})
check("C 错 ack → 403", r.status_code == 403, r.text[:200])
r = client.post(f"/api/agent/changes/{tid}/execute", json={"ack": "我已知晓"})
check("C 直调执行端点不存在（无旁路）", r.status_code == 404, f"status={r.status_code}")
r = client.post(f"/api/agent/changes/{tid}/confirm", json={"items": [99], "ack": "我已知晓"})
check("C 非法 idx → 400", r.status_code == 400, r.text[:200])
CURRENT["user"] = STUBS["operator"]
r = client.post(f"/api/agent/changes/{tid}/confirm", json={"ack": "我已知晓"})
check("C operator 确认 → 403", r.status_code == 403, r.text[:200])
CURRENT["user"] = STUBS["viewer"]
r = client.post(f"/api/agent/changes/{tid}/confirm", json={"ack": "我已知晓"})
check("C viewer 确认 → 403", r.status_code == 403, r.text[:200])
r = client.get("/api/agent/changes")
check("C viewer/operator 列表可读", r.status_code == 200 and "items" in r.json(), r.text[:100])
CURRENT["user"] = STUBS["admin"]

# ================ D. ack 确认执行 + 快照 before/after ================
print("== D 确认执行 + 快照 ==")
r = client.post(f"/api/agent/changes/{tid}/confirm", json={"ack": "我已知晓"})
data = r.json()
check("D ack 确认执行成功", r.status_code == 200 and data.get("status") == "done"
      and all(x["status"] == "done" for x in data["results"]), r.text[:300])
check("D 云账号真创建", client.get("/api/clouds/accounts").json() is not None and
      any(a["name"] == "_mock_acct" for a in client.get("/api/clouds/accounts").json()["items"]))
_db = SessionLocal()
rule = (_db.query(OwnershipRule)
        .filter(OwnershipRule.key_type == "domain",
                OwnershipRule.key_value == "_mock.example.com").first())
check("D 归属登记真写入", rule is not None and rule.business == "测试业务")
snaps = (_db.query(AgentChangeSnapshot)
         .filter(AgentChangeSnapshot.ticket_id == tid).order_by(AgentChangeSnapshot.item_idx).all())
check("D 快照两行 before/after 落库", len(snaps) == 2
      and all(s.before_blob and s.after_blob for s in snaps), f"snaps={len(snaps)}")
if snaps:
    before0, after0 = json.loads(snaps[0].before_blob), json.loads(snaps[0].after_blob)
    check("D 新建类 before=空 after=存在", before0 == {} and after0.get("exists") is True
          and after0.get("name") == "_mock_acct", f"{before0} / {str(after0)[:120]}")
    check("D 快照不含凭证密文", "ak_cipher" not in snaps[0].after_blob
          and "AKmock" not in snaps[0].after_blob, snaps[0].after_blob[:200])
    check("D 快照定位字段", snaps[0].target_type == "cloud_account"
          and snaps[0].target_ref == "_mock_acct" and snaps[0].version == 1)
actions = {a for (a,) in _db.query(AuditLog.action).all()}
check("D 审计：agent.change.confirm/execute + cloud.account.ack + cloud_account.create + ownership.create",
      {"agent.change.create", "agent.change.confirm", "agent.change.execute",
       "cloud.account.ack", "cloud_account.create", "ownership.create"} <= actions,
      str(sorted(actions)))
_db.close()
r = client.post(f"/api/agent/changes/{tid}/confirm", json={"ack": "我已知晓"})
check("D 重放（done 工单）→ 403", r.status_code == 403, r.text[:200])
_db = SessionLocal()
try:
    change_gate.execute_ticket(_db, tid, STUBS["admin"], ack="我已知晓")
    check("D 服务层直调重放 → 403", False)
except HTTPException as e:
    check("D 服务层直调重放 → 403", e.status_code == 403, str(e.detail))
_db.close()

# ================ E. 回滚真能回（新建→不存在；修改→回旧值） ================
print("== E 回滚 ==")
sid_e = new_session()
SCRIPT.append(_plan_script({"title": "改云账号区域", "plan": [
    {"tool": "update_cloud_account", "params": {"account_id": 0, "region": "cn-test-rollback"}}]}))
# account_id 真值从库里取（计划由脚本给出占位 0，这里用真实 id 重新建工单更贴近生产 LLM 会带真参）
_db = SessionLocal()
acct_id = _db.query(CloudAccount).filter(CloudAccount.name == "_mock_acct").first().id
_db.close()
events = ask(sid_e, "帮我改一下 _mock_acct 的区域")
tid_e = [d for e, d in events if e == "change"][0]["ticket_id"]
t_e = _ticket(tid_e)
# 用真 account_id 重建计划参数（mock 脚本占位 0 的替身处理：直接改工单参数再执行，
# 等价于 LLM 给了真参；确认门/快照/回滚逻辑不受影响）
_db = SessionLocal()
row = _db.get(AgentChangeTicket, tid_e)
cl = json.loads(row.change_list_json)
cl[0]["params"]["account_id"] = acct_id
row.change_list_json = json.dumps(cl, ensure_ascii=False)
_db.commit()
_db.close()
r = client.post(f"/api/agent/changes/{tid_e}/confirm", json={})
check("E 修改执行成功", r.status_code == 200 and r.json()["status"] == "done", r.text[:200])
_db = SessionLocal()
check("E 修改已生效（region=cn-test-rollback）",
      _db.query(CloudAccount).filter(CloudAccount.name == "_mock_acct").first().region == "cn-test-rollback")
snap_e = (_db.query(AgentChangeSnapshot)
          .filter(AgentChangeSnapshot.ticket_id == tid_e).first())
before_e = json.loads(snap_e.before_blob)
check("E 修改类 before 存在原值", before_e.get("exists") is True
      and before_e.get("region") == "cn-hangzhou", str(before_e)[:200])
_db.close()
r = client.post(f"/api/agent/changes/{tid_e}/rollback", json={})
check("E 修改回滚返回", r.status_code == 200 and r.json().get("rolled_back") is True, r.text[:200])
_db = SessionLocal()
check("E 回滚后回旧值（region=cn-hangzhou）",
      _db.query(CloudAccount).filter(CloudAccount.name == "_mock_acct").first().region == "cn-hangzhou")
check("E 工单状态 rolled_back", _db.get(AgentChangeTicket, tid_e).status == "rolled_back")
_db.close()
r = client.post(f"/api/agent/changes/{tid_e}/confirm", json={})
check("E 已回滚工单重放 → 403", r.status_code == 403, r.text[:200])

r = client.post(f"/api/agent/changes/{tid}/rollback", json={})
check("E 新建回滚返回", r.status_code == 200 and r.json().get("rolled_back") is True, r.text[:200])
_db = SessionLocal()
check("E 新建→回滚后云账号不存在",
      _db.query(CloudAccount).filter(CloudAccount.name == "_mock_acct").first() is None)
check("E 新建→回滚后归属登记不存在",
      _db.query(OwnershipRule).filter(OwnershipRule.key_value == "_mock.example.com").first() is None)
check("E 回滚审计落账", (_db.query(AuditLog)
                         .filter(AuditLog.action == "agent.change.rollback").count() >= 2))
_db.close()
r = client.post(f"/api/agent/changes/{tid}/rollback", json={})
check("E 无已执行项再回滚 → 400", r.status_code == 400, r.text[:200])
r = client.post(f"/api/agent/changes/{tid}/confirm", json={"ack": "我已知晓"})
check("E rolled_back 工单确认 → 403", r.status_code == 403, r.text[:200])

# ================ F. dismiss（取消） ================
print("== F dismiss ==")
sid_f = new_session()
SCRIPT.append(_plan_script({"title": "加告警规则", "plan": [
    {"tool": "save_alert_rule", "params": {
        "name": "_mock_rule", "source": "metric", "product": "ecs", "metric": "cpu_usage",
        "threshold": 90, "duration": 300}}]}))
events = ask(sid_f, "帮我配置一条 CPU 超 90 的告警规则")
tid_f = [d for e, d in events if e == "change"][0]["ticket_id"]
r = client.post(f"/api/agent/changes/{tid_f}/dismiss", json={})
check("F 未执行工单可取消", r.status_code == 200 and r.json().get("dismissed") is True, r.text[:200])
r = client.post(f"/api/agent/changes/{tid_f}/confirm", json={})
check("F dismissed 工单确认 → 403", r.status_code == 403, r.text[:200])
r = client.post(f"/api/agent/changes/{tid_f}/rollback", json={})
check("F dismissed 工单无可回滚项 → 400", r.status_code == 400, r.text[:200])
_db = SessionLocal()
check("F dismiss 审计落账", (_db.query(AuditLog)
                             .filter(AuditLog.action == "agent.change.dismiss").count() == 1))
_db.close()

# ================ G. 部分失败续跑 + 快照按次留存 ================
print("== G 部分失败续跑 ==")
sid_g = new_session()
SCRIPT.append(_plan_script({"title": "加规则并建第二个账号", "plan": [
    {"tool": "save_alert_rule", "params": {
        "name": "_mock_rule_bad", "source": "metric", "product": "nosuchproduct",
        "metric": "cpu_usage", "threshold": 90}},
    {"tool": "create_cloud_account", "params": {
        "name": "_mock_acct2", "vendor": "aliyun", "ak": "AKmock654321", "sk": "SKmock654321"}},
]}))
events = ask(sid_g, "帮我新增一个告警规则和一个云账号 _mock_acct2")
tid_g = [d for e, d in events if e == "change"][0]["ticket_id"]
r = client.post(f"/api/agent/changes/{tid_g}/confirm", json={"ack": "我已知晓"})
data = r.json()
res = {x["idx"]: x for x in data["results"]}
check("G 首项失败不阻断后续（续跑）", res[0]["status"] == "failed" and res[1]["status"] == "done"
      and data["status"] == "failed", r.text[:300])
check("G 失败项有错误信息", "error" in (res[0].get("result") or {}), str(res[0])[:200])
_db = SessionLocal()
check("G 后续项真执行（_mock_acct2 已建）",
      _db.query(CloudAccount).filter(CloudAccount.name == "_mock_acct2").first() is not None)
_db.close()

# 失败项续跑（可重试）：handler 首次失败、第二次成功（验证"可续"而非一次性判死）
_real_handler = change_tools.CHANGE_TOOLS["save_alert_rule"]["handler"]
_calls = {"n": 0}


def _flaky_handler(db_, params, user, ack):
    _calls["n"] += 1
    if _calls["n"] == 1:
        raise RuntimeError("模拟瞬时故障")
    params = dict(params, product="ecs", metric="CPUUtilization")   # 换成合法指标再重试
    return _real_handler(db_, params, user, ack)


change_tools.CHANGE_TOOLS["save_alert_rule"]["handler"] = _flaky_handler
sid_g2 = new_session()
SCRIPT.append(_plan_script({"title": "瞬时故障规则", "plan": [
    {"tool": "save_alert_rule", "params": {
        "name": "_mock_rule_flaky", "source": "metric", "product": "ecs",
        "metric": "cpu_usage", "threshold": 95}}]}))
events = ask(sid_g2, "帮我配置一条 CPU 超 95 的告警规则")
tid_g2 = [d for e, d in events if e == "change"][0]["ticket_id"]
r1 = client.post(f"/api/agent/changes/{tid_g2}/confirm", json={})
check("G 瞬时故障→首跑失败", r1.json()["status"] == "failed", r1.text[:200])
r2 = client.post(f"/api/agent/changes/{tid_g2}/confirm", json={})
check("G 失败项续跑重试（非 403）", r2.status_code == 200
      and r2.json()["results"][0]["status"] == "done", r2.text[:200])
change_tools.CHANGE_TOOLS["save_alert_rule"]["handler"] = _real_handler
_db = SessionLocal()
n_snap = (_db.query(AgentChangeSnapshot)
          .filter(AgentChangeSnapshot.ticket_id == tid_g2,
                  AgentChangeSnapshot.item_idx == 0).count())
check("G 快照按次留存（重试不覆盖，版本递增）", n_snap == 2, f"snapshot rows={n_snap}")
vers = sorted(s.version for s in _db.query(AgentChangeSnapshot)
              .filter(AgentChangeSnapshot.ticket_id == tid_g2).all())
check("G 快照版本 1→2", vers == [1, 2], str(vers))
_db.close()

# G 组回滚清理（_mock_acct2 新建→回滚后不存在）
r = client.post(f"/api/agent/changes/{tid_g}/rollback", json={})
check("G 部分失败工单回滚成功项", r.status_code == 200 and r.json().get("rolled_back") is True, r.text[:200])
_db = SessionLocal()
check("G 回滚后 _mock_acct2 不存在",
      _db.query(CloudAccount).filter(CloudAccount.name == "_mock_acct2").first() is None)
check("G 失败项状态保留 failed 供查",
      _ticket(tid_g)["change_list"][0]["status"] == "failed")
_db.close()

# ================ G2. sync_cloud（工作流移交补项：触发云资产同步） ================
print("== G2 sync_cloud ==")
import app.api.v1.clouds as _clouds_mod  # noqa: E402
from app.models.asset import Asset  # noqa: E402

_real_sync = _clouds_mod.sync_cloud_assets


def _fake_sync(request, account_id=0, product="", db=None, user=None):
    """假采集（零网络）：入库 1 行资产模拟同步结果。"""
    from datetime import datetime
    db.add(Asset(product="ecs", instance_id="_mock_sync_ecs", name="_mock_sync_ecs",
                 status="Running"))
    db.commit()
    return {"results": [], "summary": {"accounts": 1, "total": 1, "created": 1,
                                       "updated": 0, "errors": []}}


_clouds_mod.sync_cloud_assets = _fake_sync
sid_s = new_session()
SCRIPT.append(_plan_script({"title": "触发云资产同步", "plan": [
    {"tool": "sync_cloud", "params": {"account_id": 0}}]}))
events = ask(sid_s, "帮我执行一次云资产同步")
tid_s = [d for e, d in events if e == "change"][0]["ticket_id"]
r = client.post(f"/api/agent/changes/{tid_s}/confirm", json={})
check("G2 sync_cloud 确认执行成功", r.status_code == 200 and r.json()["status"] == "done", r.text[:200])
_db = SessionLocal()
snap_s = (_db.query(AgentChangeSnapshot)
          .filter(AgentChangeSnapshot.ticket_id == tid_s).first())
before_s, after_s = json.loads(snap_s.before_blob), json.loads(snap_s.after_blob)
check("G2 同步前后资产计数入快照", "asset_total" in before_s and "asset_total" in after_s
      and after_s["asset_total"] == before_s["asset_total"] + 1,
      f"{before_s.get('asset_total')} -> {after_s.get('asset_total')}")
_db.close()
r = client.post(f"/api/agent/changes/{tid_s}/rollback", json={})
check("G2 同步回滚=幂等说明不删资产", r.status_code == 200
      and "无需回滚" in json.dumps(r.json(), ensure_ascii=False), r.text[:200])
_db = SessionLocal()
check("G2 回滚不删同步资产（幂等采集）",
      _db.query(Asset).filter(Asset.instance_id == "_mock_sync_ecs").first() is not None)
_db.close()
_clouds_mod.sync_cloud_assets = _real_sync

# ================ H. 审计全链 + 标签登记 + 工单列表 ================
print("== H 审计全链 ==")
_db = SessionLocal()
actions = {a for (a,) in _db.query(AuditLog.action).all()}
_db.close()
need = {"agent.change.create", "agent.change.confirm", "agent.change.execute",
        "agent.change.rollback", "agent.change.dismiss"}
check("H agent.change.* 审计全链可查", need <= actions, str(sorted(need - actions)))
check("H ACTION_LABELS 中文标签登记", all(k in audit_svc.ACTION_LABELS for k in need)
      and "cloud.account.ack" in audit_svc.ACTION_LABELS,
      str([k for k in need | {"cloud.account.ack"} if k not in audit_svc.ACTION_LABELS]))
r = client.get("/api/agent/changes?status=rolled_back")
items = r.json()["items"]
check("H 工单列表按状态过滤", r.status_code == 200 and all(i["status"] == "rolled_back" for i in items)
      and all(isinstance(i["change_list"], list) for i in items), r.text[:200])

print(f"\n结果: {len(PASS)} 通过 / {len(FAIL)} 失败")
if FAIL:
    print("失败项:", FAIL)
sys.exit(1 if FAIL else 0)
