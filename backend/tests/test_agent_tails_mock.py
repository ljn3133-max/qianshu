#!/usr/bin/env python3
"""智能体域 mock 回归：自改重启生效 / 跨快照冲突检测 /
自定义工作流挂定时 / 自改经验沉淀 / WebShell API Token。

运行：cd /opt/qianshu/backend && python3 tests/test_agent_tails_mock.py   （退出码 0=全过）
隔离性：MASTER_KEY/DATABASE_URL/QIANSHU_KNOWLEDGE_DIR 在 import app.* 之前改写为临时值
——不碰真实 qianshu.db 与真知识包；copilotd 只 mock 不真重启（真通道只读断言 token/端口
配置，绝不 POST 真 18810）；code_mod 用真模块但只动 docs/tmp/ 自建测试文件（跑完即删）。
不启 8810（httpx ASGITransport + app.main app，同各 mock 测试惯例）。

验证项（五件各断言）：
  A 重启生效（委托 copilotd）：
    A0 通道线格式（本地假 copilotd HTTP 服务）：POST /restart + X-Copilotd-Token 头；
       真通道只读断言 127.0.0.1:18810 + token_file 口径
    A1 拒绝路径：非 admin 403 / 未二次确认 403（零委托）/ 快照不存在 400 /
       快照已回滚 400 / copilotd 预检失败 400（不发 POST /restart）
    A2 成功路径：admin+confirm → 委托 POST /restart（trigger=code_apply_restart）+
       审计 code.restart 落库 + ACTION_LABELS 登记
  B 跨快照冲突检测：应用后被第三方改过 → code_mod.rollback/auto_rollback 拒绝
    （ok=False+conflicts，不覆盖）→ API rollback 400 说明冲突 → force=true 才还原；
    工单回滚（_rb_code_modify）遇冲突抛错不标 rolled_back
  C 自定义工作流挂定时：enabled=true 的 custom 可建定时（旧行为 400 的回归）→ 调度循环照跑
    （run_due 真跑引擎落 agent_workflow_runs）；停用 custom 400；非分析域内置仍 400；
    表里塞已停用 custom 的定时被调度循环跳过
  D 自改经验沉淀：code apply 成功 → agent/knowledge/自改经验.md 落「诉求→改动摘要→文件」；
    知识包一致性检查收录 agent/knowledge/*.md（假接口引用被抓 api_ref）
  E WebShell 认 API Token：ws._auth 认 qs_ 头（哈希/启用/过期校验复用 core/deps）+
    JWT 老路径不破
"""
import asyncio
import hashlib
import http.server
import json
import os
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

# ---- 隔离环境必须先于 app 导入设置（env 覆盖 .env）----
_TMPDIR = tempfile.mkdtemp(prefix="qianshu-tails-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("tails")
os.environ["MASTER_KEY"] = "ab" * 32
os.environ["EVENT_WEBHOOK_TOKEN"] = "test-token"
os.environ["METRIC_SOURCES"] = "[]"
os.environ["LOG_SOURCES"] = "[]"
os.environ["VM_URL"] = "http://127.0.0.1:1"
os.environ["QIANSHU_KNOWLEDGE_DIR"] = os.path.join(_TMPDIR, "knowledge")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.core.db import get_db  # noqa: E402
from app.core.deps import get_current_user  # noqa: E402
from app.core.security import (API_TOKEN_PREFIX, create_access_token,  # noqa: E402
                               hash_api_token)

# 全量模型注册后再 create_all（不触发 app.main 的启动建表/后台任务）
import importlib  # noqa: E402
import pkgutil  # noqa: E402
import app.models as _models_pkg  # noqa: E402

for _m in pkgutil.iter_modules(_models_pkg.__path__):
    importlib.import_module(f"app.models.{_m.name}")
Base.metadata.create_all(bind=engine)

from app.models.agent_change import AgentWorkflowRun  # noqa: E402
from app.models.agent_code_snapshot import AgentCodeSnapshot  # noqa: E402
from app.models.agent_schedule import AgentWorkflowSchedule  # noqa: E402
from app.models.audit import AuditLog  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.user_api_token import UserApiToken  # noqa: E402
from app.models.workflow_def import WorkflowDef  # noqa: E402

from app.services import agent_schedule as sched  # noqa: E402
from app.services import knowledge_log  # noqa: E402
from app.services.audit import ACTION_LABELS  # noqa: E402
from app.services.copilot import change_tools, code_mod  # noqa: E402
from app.services.copilot import tools_read  # noqa: E402
from app.services.copilot import workflows as wf_mod  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # /opt/qianshu
PASS, FAIL = [], []
TMP_FILES: list = []   # docs/tmp/ 自建测试文件，收尾即删


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def make_tmp(tag: str, content: str) -> str:
    """docs/tmp/ 自建测试文件（可改域）-> 返回仓库相对路径。"""
    rel = f"docs/tmp/_tmp_agent_tail_{tag}.md"
    with open(os.path.join(ROOT, rel), "w", encoding="utf-8") as fh:
        fh.write(content)
    TMP_FILES.append(rel)
    return rel


USER = SimpleNamespace(id=1, username="tester")

# ---------------- 假工具（自定义工作流执行用） ----------------


def fake_execute(db, name, params):
    return {"items": [{"product": "cdn", "amount": 90.0}], "total": 1}


tools_read.execute = fake_execute

# ---------------- 种子用户 + ASGI 客户端（httpx，同各 mock 测试惯例） ----------------

_db = SessionLocal()
_admin = User(username="tail_admin", password_hash="x", role="admin", is_active=True)
_db.add(_admin)
_db.commit()
_db.refresh(_admin)
ADMIN_ID = _admin.id
_db.close()

STUB = SimpleNamespace(id=ADMIN_ID, username="tail_admin", role="admin", is_active=True)


def _ov_get_db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


from app.main import app  # noqa: E402  建表/种子后导入（路由全量注册）

app.dependency_overrides[get_db] = _ov_get_db
app.dependency_overrides[get_current_user] = lambda: STUB


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
)


# ============================================================
# A. 重启生效：委托 copilotd 本地通道 POST /restart
# ============================================================
print("== A 重启生效（委托 copilotd）==")

# ---- A0 通道线格式：本地假 copilotd HTTP 服务（绝不打真 18810）----
SEEN: list = []


class _FakeCopilotd(http.server.BaseHTTPRequestHandler):
    def _reply(self):
        SEEN.append({"method": self.command, "path": self.path,
                     "token": self.headers.get("X-Copilotd-Token", ""),
                     "body": self.rfile.read(int(self.headers.get("Content-Length") or 0))})
        body = json.dumps({"ok": True, "receipt": {"ok": True}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _reply   # noqa: N815
    do_POST = _reply   # noqa: N815

    def log_message(self, *a):
        pass


_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeCopilotd)
threading.Thread(target=_srv.serve_forever, daemon=True).start()
real_channel = change_tools._copilotd_channel
change_tools._copilotd_channel = lambda: (f"http://127.0.0.1:{_srv.server_address[1]}", "tok-abc123")
try:
    r = change_tools._copilotd_call("POST", "/restart", {"trigger": "t"})
    check("A0 POST /restart 线格式（假 copilotd 收到）",
          r.get("ok") is True and SEEN and SEEN[-1]["method"] == "POST"
          and SEEN[-1]["path"] == "/restart" and SEEN[-1]["token"] == "tok-abc123"
          and b'"trigger"' in SEEN[-1]["body"], str(SEEN[-1]) if SEEN else "无请求")
    change_tools._copilotd_call("GET", "/health")
    check("A0 GET /health 带 X-Copilotd-Token",
          SEEN[-1]["method"] == "GET" and SEEN[-1]["path"] == "/health"
          and SEEN[-1]["token"] == "tok-abc123", str(SEEN[-1]))
finally:
    change_tools._copilotd_channel = real_channel
    _srv.shutdown()

# ---- 真通道只读断言（配置/token 口径；不发任何请求到真 18810）----
with open(os.path.join(ROOT, "config", "qianshu-copilotd.json"), encoding="utf-8") as fh:
    real_cfg = json.load(fh)
with open(real_cfg["token_file"], encoding="utf-8") as fh:
    real_token = fh.read().strip()
base, token = change_tools._copilotd_channel()
check("A0 真通道口径 127.0.0.1:18810 + token 文件",
      base == f"http://{real_cfg['listen_host']}:{real_cfg['listen_port']}"
      and real_cfg["listen_port"] == 18810 and token == real_token and bool(token),
      f"base={base} token_len={len(token)}")

# ---- 委托 mock（A1/A2 用）----
CALLS: list = []
MODE = {"health_ok": True}


def fake_call(method, path, payload=None, timeout=8.0):
    CALLS.append({"method": method, "path": path, "payload": payload or {}})
    if method == "GET" and path == "/health":
        if not MODE["health_ok"]:
            raise change_tools.ChangeToolError("copilotd 通道不可用: fake down")
        return {"ok": True}
    return {"ok": True, "receipt": {"ok": True}}


change_tools._copilotd_call = fake_call

# ---- 两个测试快照：applied 一个 / rolled_back 一个 ----
rel_a1 = make_tmp("a1", "A1-before\n")
r = code_mod.snapshot_and_apply(0, {"files": [{"path": rel_a1, "new": "A1-after\n"}]}, USER)
check("A1 测试快照 applied", r.get("ok") is True and r.get("snapshot_id"), str(r)[:200])
SID_APPLIED = r["snapshot_id"]

rel_a2 = make_tmp("a2", "A2-before\n")
r = code_mod.snapshot_and_apply(0, {"files": [{"path": rel_a2, "new": "A2-after\n"}]}, USER)
SID_RB = r["snapshot_id"]
code_mod.rollback(SID_RB, USER)

# ---- A1 拒绝路径 ----
STUB.role = "operator"
r = client.post(f"/api/agent/code-snapshots/{SID_APPLIED}/apply-restart", json={"confirm": True})
STUB.role = "admin"
check("A1 非 admin 403", r.status_code == 403, f"{r.status_code} {r.text[:120]}")

CALLS.clear()
r = client.post(f"/api/agent/code-snapshots/{SID_APPLIED}/apply-restart", json={"confirm": False})
check("A1 未二次确认 403（零委托）",
      r.status_code == 403 and not any(c["path"] == "/restart" for c in CALLS),
      f"{r.status_code} {r.text[:120]} calls={CALLS}")
r = client.post(f"/api/agent/code-snapshots/{SID_APPLIED}/apply-restart", json={})
check("A1 缺 confirm 同拒 403", r.status_code == 403, f"{r.status_code} {r.text[:120]}")

r = client.post("/api/agent/code-snapshots/999999/apply-restart", json={"confirm": True})
check("A1 快照不存在 400", r.status_code == 400 and "不存在" in r.text, f"{r.status_code} {r.text[:120]}")

r = client.post(f"/api/agent/code-snapshots/{SID_RB}/apply-restart", json={"confirm": True})
check("A1 快照已回滚 400（只有 applied 才重启生效）",
      r.status_code == 400 and "rolled_back" in r.text, f"{r.status_code} {r.text[:120]}")

CALLS.clear()
MODE["health_ok"] = False
r = client.post(f"/api/agent/code-snapshots/{SID_APPLIED}/apply-restart", json={"confirm": True})
MODE["health_ok"] = True
check("A1 copilotd 预检失败 400（不发 POST /restart）",
      r.status_code == 400 and "copilotd" in r.text
      and not any(c["path"] == "/restart" for c in CALLS),
      f"{r.status_code} {r.text[:120]} calls={CALLS}")

# ---- A2 成功路径 ----
CALLS.clear()
r = client.post(f"/api/agent/code-snapshots/{SID_APPLIED}/apply-restart", json={"confirm": True})
body = r.json() if r.status_code == 200 else {}
check("A2 admin+confirm 委托成功",
      r.status_code == 200 and body.get("ok") is True and body.get("snapshot_id") == SID_APPLIED
      and any(c["method"] == "POST" and c["path"] == "/restart"
              and c["payload"].get("trigger") == "code_apply_restart" for c in CALLS),
      f"{r.status_code} {r.text[:200]} calls={CALLS}")
check("A2 回执含重启提示", "copilotd" in str(body.get("note") or ""), str(body)[:200])

_db = SessionLocal()
rows = _db.query(AuditLog).filter(AuditLog.action == "code.restart").all()
_db.close()
check("A2 审计 code.restart 落库（含快照号/文件清单）",
      rows and str(SID_APPLIED) in (rows[-1].detail or "")
      and rel_a1 in (rows[-1].detail or "") and '"confirm": true' in (rows[-1].detail or ""),
      rows[-1].detail[:200] if rows else "无审计")
check("A2 ACTION_LABELS 登记 code.restart", bool(ACTION_LABELS.get("code.restart")),
      str(ACTION_LABELS.get("code.restart")))

# ============================================================
# B. 跨快照冲突检测（应用后被第三方改动 → 拒绝强还原；force 才覆盖）
# ============================================================
print("== B 跨快照冲突检测 ==")
rel_b = make_tmp("b", "B-before\n")
r = code_mod.snapshot_and_apply(0, {"files": [{"path": rel_b, "new": "B-after\n"}]}, USER)
SID_B = r["snapshot_id"]
with open(os.path.join(ROOT, rel_b), "w", encoding="utf-8") as fh:
    fh.write("B-third-party-edit\n")   # 模拟第三方中途改动

r = code_mod.rollback(SID_B, USER)
check("B code_mod.rollback 拒（ok=False+conflicts）",
      r.get("ok") is False and r.get("conflicts") and "拒绝回滚" in str(r.get("error") or ""),
      str(r)[:300])
check("B 拒绝时不覆盖第三方改动",
      open(os.path.join(ROOT, rel_b), encoding="utf-8").read() == "B-third-party-edit\n")

# auto_rollback 同拒（第二个快照制造同类冲突）
rel_b2 = make_tmp("b2", "B2-before\n")
r = code_mod.snapshot_and_apply(0, {"files": [{"path": rel_b2, "new": "B2-after\n"}]}, USER)
SID_B2 = r["snapshot_id"]
with open(os.path.join(ROOT, rel_b2), "w", encoding="utf-8") as fh:
    fh.write("B2-third-party\n")
r = code_mod.auto_rollback(SID_B2, USER)
check("B auto_rollback 同拒（不覆盖）",
      r.get("ok") is False and r.get("conflicts")
      and open(os.path.join(ROOT, rel_b2), encoding="utf-8").read() == "B2-third-party\n",
      str(r)[:200])

# API rollback：冲突 400 说明 → force=true 才还原
r = client.post(f"/api/agent/code-snapshots/{SID_B}/rollback", json={})
check("B API 回滚冲突拒 400（说明冲突文件）",
      r.status_code == 400 and "拒绝回滚" in r.text and rel_b in r.text
      and open(os.path.join(ROOT, rel_b), encoding="utf-8").read() == "B-third-party-edit\n",
      f"{r.status_code} {r.text[:250]}")
r = client.post(f"/api/agent/code-snapshots/{SID_B}/rollback", json={"force": True})
check("B force=true 强制还原成功",
      r.status_code == 200 and r.json().get("rolled_back") is True
      and open(os.path.join(ROOT, rel_b), encoding="utf-8").read() == "B-before\n",
      f"{r.status_code} {r.text[:250]}")

# 工单回滚（rollback_fn）遇冲突抛错，不静默标 rolled_back
rel_b3 = make_tmp("b3", "B3-before\n")
r = code_mod.snapshot_and_apply(0, {"files": [{"path": rel_b3, "new": "B3-after\n"}]}, USER)
SID_B3 = r["snapshot_id"]
with open(os.path.join(ROOT, rel_b3), "w", encoding="utf-8") as fh:
    fh.write("B3-third-party\n")
raised = ""
try:
    change_tools._rb_code_modify(SessionLocal(), {"plan": {}}, {},
                                 {"snapshot_id": SID_B3}, USER)
except change_tools.ChangeToolError as e:
    raised = str(e)
check("B 工单回滚遇冲突抛错（rollback_ticket 记 error 不标 rolled_back）",
      "拒绝回滚" in raised and rel_b3 in raised, raised[:200])

# ============================================================
# C. 自定义工作流挂定时（enabled=true 的 custom；调度循环照跑）
# ============================================================
print("== C 自定义工作流挂定时 ==")
from app.services.copilot.workflows import custom as wf_custom  # noqa: E402

db = SessionLocal()
wf_custom.create_def(db, key="tail_custom", name="尾巴测试自定义",
                     description="测试用", domain="自定义",
                     steps=[{"tool": "query_cost", "label": "查成本",
                             "params_template": {}, "continue_on_fail": True}])
wf_custom.create_def(db, key="tail_custom_off", name="尾巴停用",
                     steps=[{"tool": "query_cost", "label": "查成本",
                             "params_template": {}, "continue_on_fail": True}])
db.close()

r = client.post("/api/agent/schedules",
                json={"workflow_key": "tail_custom", "interval_days": 1, "hour": 9})
item = r.json() if r.status_code == 200 else {}
check("C 自定义（enabled）可挂定时（200）",
      r.status_code == 200 and item.get("workflow_key") == "tail_custom"
      and item.get("workflow_name") == "尾巴测试自定义", f"{r.status_code} {r.text[:200]}")

r = client.post("/api/agent/schedules", json={"workflow_key": "access_cloud"})
check("C 非分析域内置仍 400", r.status_code == 400, f"{r.status_code} {r.text[:120]}")

db = SessionLocal()
row = db.query(WorkflowDef).filter(WorkflowDef.key == "tail_custom_off").first()
row.enabled = False
db.commit()
db.close()
r = client.post("/api/agent/schedules", json={"workflow_key": "tail_custom_off"})
check("C 已停用 custom 400（不可定时）",
      r.status_code == 400 and "停用" in r.text, f"{r.status_code} {r.text[:150]}")

# 调度循环照跑：到点 run_due 真跑引擎（假工具），运行记录落 agent_workflow_runs
now = datetime.now()
if now.hour < 10:
    now = now.replace(hour=10)
db = SessionLocal()
res = sched.run_due(db, now=now)
ran_keys = [x["workflow_key"] for x in res.get("ran") or []]
run_row = (db.query(AgentWorkflowRun)
           .filter(AgentWorkflowRun.workflow_key == "tail_custom")
           .order_by(AgentWorkflowRun.id.desc()).first())
sched_row = (db.query(AgentWorkflowSchedule)
             .filter(AgentWorkflowSchedule.workflow_key == "tail_custom").first())
db.close()
check("C 调度循环照跑 custom（run_due 触发+运行记录落库）",
      "tail_custom" in ran_keys and run_row is not None and run_row.status in ("done", "failed"),
      f"ran={ran_keys} run={run_row.status if run_row else None}")
check("C last_run_at 幂等锚点已置", sched_row is not None and sched_row.last_run_at is not None,
      str(sched_row.last_run_at if sched_row else None))

# 防御：表里塞已停用 custom 的定时 → 调度循环跳过
db = SessionLocal()
db.add(AgentWorkflowSchedule(workflow_key="tail_custom_off", interval_days=1,
                             hour=0, enabled=True, created_by="tester"))
db.commit()
res = sched.run_due(db, now=now)
db.close()
check("C 停用 custom 的存量定时被跳过",
      "tail_custom_off" not in [x["workflow_key"] for x in res.get("ran") or []], str(res)[:200])

# ============================================================
# D. 自改经验沉淀（code apply 成功 → 知识包；一致性检查收录）
# ============================================================
print("== D 自改经验沉淀 ==")
rel_d = make_tmp("d", "VALUE = v1\n")
r = change_tools._h_code_modify(SessionLocal(), {"plan": {
    "files": [{"path": rel_d, "edits": [{"old": "VALUE = v1", "new": "VALUE = v2"}]}],
    "summary": "把尾巴测试值改成 v2", "impact": "无"}}, USER, "")
check("D code apply 成功", r.get("result", {}).get("snapshot_id"), str(r)[:200])
check("D 文件已应用", open(os.path.join(ROOT, rel_d), encoding="utf-8").read() == "VALUE = v2\n")

kpath = os.path.join(os.environ["QIANSHU_KNOWLEDGE_DIR"], knowledge_log.LOG_NAME)
ktext = open(kpath, encoding="utf-8").read() if os.path.isfile(kpath) else ""
check("D 知识包落档（诉求→改动摘要→文件）",
      "诉求：把尾巴测试值改成 v2" in ktext and "改动摘要" in ktext
      and f"文件：`{rel_d}`" in ktext and "自改经验" in ktext, ktext[-400:])
check("D 落档含快照/工单号", "快照 #" in ktext and "工单 #" in ktext, ktext[-200:])

# 知识包一致性检查收录 agent/knowledge/*.md（假接口引用被 api_ref 抓出）
from app.services import knowledge_check  # noqa: E402

kroot = os.path.join(_TMPDIR, "kcheck")
os.makedirs(os.path.join(kroot, "agent", "knowledge"), exist_ok=True)
with open(os.path.join(kroot, "agent", "knowledge", "x.md"), "w", encoding="utf-8") as fh:
    fh.write("## 2026-11-09 示例\n- 诉求：示例\n- 改动摘要：示例\n- 文件：`docs/x.md`\n"
             "GET /api/nope_nothing\n")
rep = knowledge_check.run_check(root=kroot, routes={"GET /api/agent/sessions"})
bad = [i for i in rep["inconsistencies"] if i["kind"] == "api_ref" and "x.md" in i["where"]]
check("D 一致性检查收录 agent/knowledge（假接口引用被抓）",
      any("nope_nothing" in i["detail"] for i in bad), str(rep["inconsistencies"])[:300])

# ============================================================
# E. WebShell 认 API Token（qs_ 前缀）+ JWT 老路径不破
# ============================================================
print("== E WebShell API Token ==")
from app.api import ws as ws_mod  # noqa: E402

_db = SessionLocal()
tok_plain = API_TOKEN_PREFIX + "tailtoken" + "x" * 20
tok_row = UserApiToken(user_id=ADMIN_ID, name="tail", token_hash=hash_api_token(tok_plain),
                       token_prefix=tok_plain[:12], enabled=True)
_db.add(tok_row)
expired_plain = API_TOKEN_PREFIX + "tailexpired" + "y" * 16
_db.add(UserApiToken(user_id=ADMIN_ID, name="exp", token_hash=hash_api_token(expired_plain),
                     token_prefix=expired_plain[:12], enabled=True,
                     expires_at=datetime.now() - timedelta(days=1)))
_db.commit()
_db.close()

u = ws_mod._auth(tok_plain)
check("E qs_ API Token 鉴权通过（含角色）",
      u and u["id"] == ADMIN_ID and u["role"] == "admin", str(u))

_db = SessionLocal()
_db.query(UserApiToken).filter(UserApiToken.token_hash == hash_api_token(tok_plain)).update(
    {"enabled": False})
_db.commit()
_db.close()
check("E 吊销 token 拒（None）", ws_mod._auth(tok_plain) is None)
check("E 过期 token 拒（None）", ws_mod._auth(expired_plain) is None)
check("E 垃圾 token 拒（None）", ws_mod._auth("garbage-token") is None)
check("E qs_ 假 token 拒（None）", ws_mod._auth(API_TOKEN_PREFIX + "notreal") is None)

jwt = create_access_token(ADMIN_ID, "admin")
u = ws_mod._auth(jwt)
check("E JWT 老路径不破", u and u["id"] == ADMIN_ID and u["role"] == "admin", str(u))

# ---------------- 收尾：清 docs/tmp/ 自建测试文件 ----------------
for rel in TMP_FILES:
    try:
        os.unlink(os.path.join(ROOT, rel))
    except OSError:
        pass
check("收尾 docs/tmp/ 自建测试文件已清理",
      not any(os.path.exists(os.path.join(ROOT, rel)) for rel in TMP_FILES))

print(f"\n== 结果: {len(PASS)} 通过 / {len(FAIL)} 失败 ==")
if FAIL:
    print("失败项: " + " | ".join(FAIL))
    raise SystemExit(1)
raise SystemExit(0)
