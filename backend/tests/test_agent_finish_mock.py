#!/usr/bin/env python3
"""千枢智能体域 mock 回归：#2 跳过态落服务端 / #3 工作流结果归属会话 /
#4 引擎用户替身最小权限 / #5 场景开关加固（None 四消费面）/ #14 gitlab·gitea 归因。

运行：cd /opt/qianshu/backend && python3 tests/test_agent_finish_mock.py
隔离性：MASTER_KEY/DATABASE_URL/EVENT_WEBHOOK_TOKEN 在 import app.* 之前改写为临时值——
不碰真实 qianshu.db、不调任何网络（工作流走只读空库、LLM 零调用——#3 工作流路径无 LLM）、
绝不向任何真实/本地通知渠道发消息（send_to_channel 钉死抛错）。

验证项：
  A 跳过状态机（#2）：POST /agent/changes/{id}/skip {idx} pending→skipped；
    skipped 不可执行（execute 403）；restore 恢复 skipped→pending 可再执行；
    工单聚合（全收口→done；恢复→重开）；非 pending 项/坏 idx 拒绝；非 admin 403；
    审计 agent.change.skip/unskip + 中文标签登记。
  B 会话归属+合成卡不双显（#3）：聊天链路工作流 run.session_id 落真值；真消息带 run_id
    引用→listMessages 不合成（不双显）；无真消息的 run 合成卡只出现一次。
  C 引擎用户（#4）：context.user 真用户优先（minimal_privilege=False）；
    无真用户→最小权限替身（role=viewer，绝不 admin）+ 步骤 detail 注明"以最小权限构建"；
    最小权限替身建平台管理域工单被 guard 越权拒（越权风险封堵），真 admin 可建。
  D 场景开关加固（#5）：场景显式 enabled=False → resolve_model=None；
    四消费面 None 路径不崩：copilot=NEED_MODEL 引导 / ask·inspection·diagnosis=LLMError 引导。
  E 归因（#14）：gitlab payload（带 object_kind）入库 service=gitlab；gitea payload（不带）
    入库 service=gitea（含打到 /gitlab 端点的 gitea payload 也记 gitea）；eid/event_source
    前缀一致；集成管理证据口径新旧数据都对（历史 service=gitlab 老 gitea 行按 raw 拆分）。
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace

# ---- 隔离环境必须先于 app 导入设置（env 覆盖 .env）----
_TMPDIR = tempfile.mkdtemp(prefix="qianshu-finish-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("finish")
os.environ["MASTER_KEY"] = "ef" * 32          # 64 hex 测试主密钥
os.environ["EVENT_WEBHOOK_TOKEN"] = "test-token"
os.environ["METRIC_SOURCES"] = "[]"
os.environ["LOG_SOURCES"] = "[]"
os.environ["VM_URL"] = "http://127.0.0.1:1"
os.environ["PUBLIC_BASE_URL"] = ""

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.core.db import get_db  # noqa: E402
from app.core.deps import get_current_user  # noqa: E402
from app.core.security import encrypt_secret  # noqa: E402

# 全量模型注册后再 create_all（同 test_copilot_change_mock：防漏表）
import importlib  # noqa: E402
import pkgutil  # noqa: E402
import app.models as _models_pkg  # noqa: E402
for _m in pkgutil.iter_modules(_models_pkg.__path__):
    importlib.import_module(f"app.models.{_m.name}")

from app.models.agent_change import AgentChangeTicket, AgentWorkflowRun  # noqa: E402
from app.models.agent_session import AgentSession  # noqa: E402
from app.models.ai_setting import AiSetting  # noqa: E402
from app.models.audit import AuditLog  # noqa: E402
from app.models.change_event import ChangeEvent  # noqa: E402
from app.models.inspection import InspectionRun  # noqa: E402
from app.models.ownership import OwnershipRule  # noqa: E402
from app.services import audit as audit_svc  # noqa: E402
from app.services.copilot import change_gate, change_tools  # noqa: E402
from app.services.copilot.workflows import engine as wf_engine  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


# ---- 建表 + 测试账号/模型连接（隔离自建的 /tmp 临时库）----
Base.metadata.create_all(engine)
db = SessionLocal()

STUB_USER = SimpleNamespace(id=1, username="tester", role="admin", is_active=True)

db.add(AiSetting(provider_label="conn-main", base_url="https://llm.example/v1",
                 api_key_cipher=encrypt_secret("sk-test"), model="model-a", enabled=True))
db.commit()

# ---- 真外发防线：send_to_channel 钉死（红线：绝不对外发消息）----
import app.services.notify as _notify  # noqa: E402
import app.services.report as _report  # noqa: E402


def _forbid_send(*a, **k):
    raise AssertionError("红线触发：测试中不得对外发送任何消息")


_notify.send_to_channel = _forbid_send
_report.send_to_channel = _forbid_send

# ---- TestClient 壳（同 test_integrations_mock：不触发 lifespan/startup，零网络）----
from app.main import app  # noqa: E402

app.dependency_overrides[get_db] = lambda: db
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
    put=lambda url, **kw: _req("PUT", url, **kw),
)


def _own_item(key: str) -> dict:
    return {"tool": "save_ownership_rule",
            "params": {"key_type": "ip", "key_value": key}}


def _ticket(items, title="收尾测试工单", session_id=0):
    return change_tools.build_ticket(db, session_id, 0, items, STUB_USER, title=title)


# ================= A 跳过状态机（#2） =================
print("A 跳过状态机（#2 跳过态落服务端）")
t = _ticket([_own_item("10.1.0.1"), _own_item("10.1.0.2")], title="两件套", session_id=0)
tid = t["id"]
assert t["change_list"][0]["status"] == "pending"

# 路由注册
paths = {r.path for r in app.routes}
check("skip 路由已注册", "/api/agent/changes/{ticket_id}/skip" in paths, str(sorted(paths)[:6]))

r = client.post(f"/api/agent/changes/{tid}/skip", json={"idx": 0})
d = r.json()
check("POST skip pending→skipped", r.status_code == 200 and d["status"] == "skipped"
      and d["ticket_status"] == "pending", str(d))
t2 = db.get(AgentChangeTicket, tid)
check("跳过态落服务端（change_list_json 刷新后仍在）",
      json.loads(t2.change_list_json)[0]["status"] == "skipped", t2.change_list_json)
check("跳过审计 agent.change.skip",
      db.query(AuditLog).filter(AuditLog.action == "agent.change.skip").count() == 1)

# 幂等：再跳一次不报错
r = client.post(f"/api/agent/changes/{tid}/skip", json={"idx": 0})
check("重复跳过幂等", r.status_code == 200 and r.json()["status"] == "skipped", str(r.json()))

# skipped 不可执行
try:
    change_gate.execute_ticket(db, tid, STUB_USER, items=[0])
    check("skipped 项不可执行（execute 403）", False, "未抛异常")
except HTTPException as e:
    check("skipped 项不可执行（execute 403）", e.status_code == 403 and "跳过" in str(e.detail), str(e.detail))

# 恢复：skipped→pending，可执行
r = client.post(f"/api/agent/changes/{tid}/skip", json={"idx": 0, "restore": True})
d = r.json()
check("restore 恢复 skipped→pending", r.status_code == 200 and d["status"] == "pending"
      and d["restored"] is True, str(d))
check("恢复审计 agent.change.unskip",
      db.query(AuditLog).filter(AuditLog.action == "agent.change.unskip").count() == 1)
try:
    change_gate.execute_ticket(db, tid, STUB_USER, items=[0])
    check("恢复后可执行（真执行成功）", True)
except HTTPException as e:
    check("恢复后可执行（真执行成功）", False, str(e.detail))

# 跳过第二件 → 工单收口 done；skipped 件仍不可执行；恢复 → 工单重开 pending
r = client.post(f"/api/agent/changes/{tid}/skip", json={"idx": 1})
check("余项跳过后工单收口 done", r.status_code == 200 and r.json()["ticket_status"] == "done",
      str(r.json()))
try:
    change_gate.execute_ticket(db, tid, STUB_USER)
    check("全收口后 execute 403（含 skipped 不可执行）", False, "未抛异常")
except HTTPException as e:
    check("全收口后 execute 403（含 skipped 不可执行）", e.status_code == 403, str(e.detail))
r = client.post(f"/api/agent/changes/{tid}/skip", json={"idx": 1, "restore": True})
check("done 工单恢复跳过项→工单重开 pending", r.status_code == 200
      and r.json()["ticket_status"] == "pending", str(r.json()))

# 状态机边界：坏 idx / 非 pending 项跳过 / 非 skipped 恢复
check("坏 idx 400", client.post(f"/api/agent/changes/{tid}/skip", json={"idx": 99}).status_code == 400)
check("非 skipped 恢复 400",
      client.post(f"/api/agent/changes/{tid}/skip", json={"idx": 1, "restore": True}).status_code == 400)
tid2 = _ticket([_own_item("10.2.0.1")], title="单件执行")["id"]
change_gate.execute_ticket(db, tid2, STUB_USER)
check("done 项不可跳过（pending→skipped 状态机外）",
      client.post(f"/api/agent/changes/{tid2}/skip", json={"idx": 0}).status_code == 400)

# 权限：非 admin 403
STUB_USER.role = "operator"
check("非 admin 跳过 403",
      client.post(f"/api/agent/changes/{tid}/skip", json={"idx": 1}).status_code == 403)
STUB_USER.role = "admin"

from app.services.audit import ACTION_LABELS  # noqa: E402
check("skip/unskip 中文标签登记",
      ACTION_LABELS.get("agent.change.skip") == "跳过智能体变更项"
      and ACTION_LABELS.get("agent.change.unskip") == "恢复智能体变更项")

# ================= B 会话归属 + 合成卡不双显（#3） =================
print("B 工作流结果归属会话（#3）")
import app.services.copilot.planner as _planner  # noqa: E402

_orig_plan = _planner.plan
_planner.plan = lambda db_, text, intent: {"workflow": "cost_checkup", "params": {}}

sess = client.post("/api/agent/sessions", json={}).json()
sid = sess["id"]
r = client.post(f"/api/agent/sessions/{sid}/messages", json={"q": "帮我做个体检"})
body = r.text
check("聊天链路工作流走通（SSE done）", r.status_code == 200 and "event: done" in body, body[:300])
run = (db.query(AgentWorkflowRun).filter(AgentWorkflowRun.session_id == sid)
       .order_by(AgentWorkflowRun.id.desc()).first())
check("run.session_id 落真值（归属会话）", run is not None and run.session_id == sid,
      str(run and run.session_id))

msgs = client.get(f"/api/agent/sessions/{sid}/messages").json()["items"]
synth = [m for m in msgs if str(m["id"]).startswith("wf-run-")]
asst = [m for m in msgs if m.get("role") == "assistant" and not str(m["id"]).startswith("wf-run-")]
check("真消息+合成卡不双显（run 已被真消息引用不合成）",
      len(synth) == 0 and len(asst) == 1, f"synth={len(synth)} asst={len(asst)}")
check("真消息带 run_id 引用（防重锚点）",
      bool(asst) and f'"run_id": {run.id}' in json.dumps(asst[0].get("tool_calls") or []),
      str(asst and asst[0].get("tool_calls")))

# 正向对照：无真消息引用的 run（API 直跑挂会话）→ 合成卡恰好一张
from app.services.copilot import workflows as wf_mod  # noqa: E402

sess2 = client.post("/api/agent/sessions", json={}).json()
sid2 = sess2["id"]
res = wf_mod.run_workflow(db, "cost_checkup", {},
                          {"user": STUB_USER, "user_id": STUB_USER.id, "session_id": sid2})
msgs2 = client.get(f"/api/agent/sessions/{sid2}/messages").json()["items"]
synth2 = [m for m in msgs2 if str(m["id"]) == f"wf-run-{res['run_id']}"]
check("无引用 run 合成卡恰好一张（不双显）", len(synth2) == 1
      and synth2[0].get("msg_kind") == "workflow_run", f"n={len(synth2)}")
check("API 直跑 run.session_id 也落真值", synth2 and synth2[0].get("run_id") == res["run_id"])

_planner.plan = _orig_plan

# ================= C 引擎用户：真用户优先 / 最小权限（#4） =================
print("C 工作流引擎用户替身（#4）")
real_user = SimpleNamespace(id=77, username="boss", role="admin", is_active=True)
out = wf_engine.build_change_ticket(
    db, title="真用户建单", items=[_own_item("10.3.0.1")],
    context={"user": real_user, "user_id": 77, "session_id": 555, "msg_id": 3})
check("context.user 真用户优先（非最小权限）", out.get("minimal_privilege") is False, str(out))
check("真用户建单归属会话（ticket.session_id）",
      db.get(AgentChangeTicket, out["ticket_id"]).session_id == 555)

u, minimal = wf_engine._resolve_ticket_user({"user": "scheduler", "user_id": 0})
check("无真用户→最小权限替身（role=viewer，绝不 admin）",
      minimal is True and getattr(u, "role", "") == "viewer", f"{getattr(u, 'role', None)} minimal={minimal}")

ctx = {"context": {"user": "scheduler", "user_id": 0, "session_id": 0}}
step = wf_engine.change_step(db, ctx, title="最小权限建单", items=[_own_item("10.3.0.2")])
check("最小权限构建在步骤 detail 注明", step["status"] == "ok"
      and "以最小权限构建" in step.get("detail", ""), str(step))
step2 = wf_engine.change_step(db, {"context": {"user": real_user, "user_id": 77}},
                              title="真用户建单2", items=[_own_item("10.3.0.3")])
check("真用户步骤 detail 无最小权限标注", step2["status"] == "ok"
      and "以最小权限构建" not in step2.get("detail", ""), str(step2))

# 越权封堵：最小权限替身建平台管理域工单（check_fn→guard）被拒；真 admin 可建
api_item = {"tool": "platform_api_call",
            "params": {"method": "POST", "path": "/api/agent/schedules", "body": {}}}
try:
    wf_engine.build_change_ticket(db, title="越权测试", items=[api_item],
                                  context={"user": "scheduler", "user_id": 0})
    check("最小权限替身建平台管理域工单被拒（越权封堵）", False, "未抛异常")
except Exception as e:
    check("最小权限替身建平台管理域工单被拒（越权封堵）", "越权" in str(e) or "拒绝" in str(e), str(e)[:120])
out3 = wf_engine.build_change_ticket(db, title="admin 建平台工单", items=[api_item],
                                     context={"user": real_user, "user_id": 77})
check("真 admin 建平台管理域工单成功", out3.get("ticket_id") and out3.get("minimal_privilege") is False,
      str(out3))

# ================= D 场景开关加固（#5）：resolve None 四消费面 =================
print("D 场景开关加固（#5 resolve None 四消费面）")
from app.services.ai_scenarios import require_model, resolve_model  # noqa: E402
from app.services.llm import LLMError  # noqa: E402

check("场景启用：resolve_model 有值（控制组）", resolve_model(db, "ask") is not None)
r = client.put("/api/ai/scenarios/ask", json={"enabled": False})
check("PUT 场景停用", r.status_code == 200 and r.json()["enabled"] is False, str(r.json()))
check("显式停用 → resolve_model 返回 None（不兜底）", resolve_model(db, "ask") is None)

# 消费面①ask
try:
    from app.services.ask import run_ask  # noqa: E402

    run_ask(db, 1, "查一下成本")
    check("ask None 路径不崩（LLMError 引导）", False, "未抛异常")
except LLMError as e:
    check("ask None 路径不崩（LLMError 引导）", "设置" in str(e) or "停用" in str(e), str(e))
except Exception as e:  # noqa: BLE001
    check("ask None 路径不崩（LLMError 引导）", False, f"{type(e).__name__}: {e}")

# 消费面②diagnosis
client.put("/api/ai/scenarios/diagnosis", json={"enabled": False})
check("diagnosis 场景停用 → resolve_model=None", resolve_model(db, "diagnosis") is None)
try:
    from app.services.diagnosis import run_diagnosis  # noqa: E402

    run_diagnosis(db, 1, "为什么磁盘满了")
    check("diagnosis None 路径不崩（LLMError 引导）", False, "未抛异常")
except LLMError as e:
    check("diagnosis None 路径不崩（LLMError 引导）", "设置" in str(e) or "停用" in str(e), str(e))
except Exception as e:  # noqa: BLE001
    check("diagnosis None 路径不崩（LLMError 引导）", False, f"{type(e).__name__}: {e}")

# 消费面③inspection（巡检日报）
client.put("/api/ai/scenarios/inspection", json={"enabled": False})
db.add(InspectionRun(run_date=datetime.now().date(), status="ok"))
db.commit()
try:
    _report.generate_report(db)
    check("inspection None 路径不崩（LLMError 引导）", False, "未抛异常")
except LLMError as e:
    check("inspection None 路径不崩（LLMError 引导）", "设置" in str(e) or "停用" in str(e), str(e))
except Exception as e:  # noqa: BLE001
    check("inspection None 路径不崩（LLMError 引导）", False, f"{type(e).__name__}: {e}")

# 消费面④copilot loop（NEED_MODEL 引导）
r = client.put("/api/ai/scenarios/copilot", json={"enabled": False})
check("copilot 场景停用", r.status_code == 200, str(r.json()))
check("copilot resolve_model=None", resolve_model(db, "copilot") is None)
sid3 = client.post("/api/agent/sessions", json={}).json()["id"]
r = client.post(f"/api/agent/sessions/{sid3}/messages", json={"q": "查一下资产"})
check("copilot None 路径不崩（SSE NEED_MODEL 引导）",
      r.status_code == 200 and "event: error" in r.text and "NEED_MODEL" in r.text, r.text[:300])

# 回开场景（控制组恢复）：require_model 正常返回
client.put("/api/ai/scenarios/copilot", json={"enabled": True})
check("场景回开 → require_model 可用", require_model(db, "copilot") is not None)

# ================= E gitlab / gitea 归因（#14） =================
print("E gitlab/gitea 归因（#14）")
H_GL_PUSH = {"x-gitlab-token": "test-token", "x-gitlab-event": "Push Hook"}
H_GL_MR = {"x-gitlab-token": "test-token", "x-gitlab-event": "Merge Request Hook"}
H_GT_PUSH = {"x-gitea-token": "test-token", "x-gitea-event": "push"}
H_GT_PR = {"x-gitea-token": "test-token", "x-gitea-event": "pull_request"}
GITLAB_PUSH = {"object_kind": "push", "project": {"name": "proj"},
               "ref": "refs/heads/main", "checkout_sha": "abc123",
               "user_name": "alice", "commits": [{"message": "fix bug"}]}
GITEA_PUSH = {"ref": "refs/heads/main", "after": "def456",
              "repository": {"name": "repo"}, "sender": {"login": "bob"},
              "commits": [{"message": "add feat"}]}   # 无 object_kind

r = client.post("/api/events/webhook/gitlab", json=GITLAB_PUSH, headers=H_GL_PUSH)
check("gitlab push 入库", r.status_code == 200 and r.json()["ingested"] == 1, r.text[:200])
ev = db.query(ChangeEvent).filter(ChangeEvent.event_id == "gitlab:push:abc123").first()
check("gitlab 归因：service/event_source/eid 记 gitlab",
      ev is not None and ev.service == "gitlab" and ev.event_source == "gitlab/proj"
      and ev.event_id.startswith("gitlab:"), str(ev and (ev.service, ev.event_source, ev.event_id)))

r = client.post("/api/events/webhook/gitea", json=GITEA_PUSH, headers=H_GT_PUSH)
check("gitea push 入库", r.status_code == 200 and r.json()["ingested"] == 1, r.text[:200])
ev = db.query(ChangeEvent).filter(ChangeEvent.event_id == "gitea:push:def456").first()
check("gitea 归因：service/event_source/eid 记 gitea（不再硬编码 gitlab）",
      ev is not None and ev.service == "gitea" and ev.event_source == "gitea/repo"
      and ev.event_id.startswith("gitea:"), str(ev and (ev.service, ev.event_source, ev.event_id)))

# 混用：gitea 形态 payload 打到 /gitlab 端点 → 仍记 gitea（payload 判定）
GITEA_PUSH2 = dict(GITEA_PUSH, after="fff789")
r = client.post("/api/events/webhook/gitlab", json=GITEA_PUSH2, headers=H_GL_PUSH)
check("gitea payload 打 /gitlab 端点也记 gitea", r.status_code == 200 and r.json()["ingested"] == 1
      and db.query(ChangeEvent).filter(ChangeEvent.event_id == "gitea:push:fff789").first().service == "gitea",
      r.text[:200])

# MR/PR 两家都解析（覆盖 gitea PR 入库回归）
GITLAB_MR = {"object_kind": "merge_request", "project": {"name": "proj"}, "user_name": "alice",
             "object_attributes": {"iid": 7, "action": "open", "title": "feat x"}}
GITEA_PR = {"action": "opened", "pull_request": {"number": 3, "title": "fix y", "state": "open"},
            "repository": {"name": "repo"}, "sender": {"login": "bob"}}
check("gitlab MR 归因", client.post("/api/events/webhook/gitlab", json=GITLAB_MR, headers=H_GL_MR).json()["ingested"] == 1
      and db.query(ChangeEvent).filter(ChangeEvent.event_id == "gitlab:mr:7:open").first().service == "gitlab")
check("gitea PR 解析+归因", client.post("/api/events/webhook/gitea", json=GITEA_PR, headers=H_GT_PR).json()["ingested"] == 1
      and db.query(ChangeEvent).filter(ChangeEvent.event_id == "gitea:mr:3:opened").first().service == "gitea")

# 集成管理证据口径：新数据（service=gitea）+ 历史数据（service=gitlab 无 object_kind）都算 gitea
from app.api.v1 import integrations as intg  # noqa: E402

db.add(ChangeEvent(
    account_id=1, event_id="gitlab:push:gitea-legacy", event_time=datetime.now() - timedelta(days=30),
    service="gitlab", event_name="Push", event_source="gitlab/repo", user="u",
    resource="r", summary="s", raw=json.dumps({"repository": {"name": "repo"}, "ref": "refs/heads/main"})))
db.commit()
g = intg._event_evidence(db, "gitea")
check("gitea 证据=新数据 3 条+历史数据 1 条", g["count_total"] == 4 and g["count_recent"] == 3, str(g))
gl = intg._event_evidence(db, "gitlab")
check("gitlab 证据=带 object_kind（2 条）", gl["count_total"] == 2, str(gl))
v = intg._event_verdict(db, "gitea")
check("registry 判定取到 gitea 真实证据", v["evidence"]["count_total"] == 4
      and v["evidence"]["count_recent"] == 3, str(v))

db.close()
print(f"\n智能体收尾五件 mock 测试: {len(PASS)}/{len(PASS) + len(FAIL)} 通过")
if FAIL:
    print("失败项:", FAIL)
    raise SystemExit(1)
