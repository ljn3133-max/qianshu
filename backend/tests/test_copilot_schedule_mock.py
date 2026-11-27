#!/usr/bin/env python3
"""千枢智能体工作流例行化 mock 测试：调度 + 有异动才推人 + 运行结果持久化。

运行：cd /opt/qianshu/backend && python3 tests/test_copilot_schedule_mock.py
隔离性：DATABASE_URL/MASTER_KEY 在 import app.* 之前改写为临时值——不碰真实 qianshu.db；
工具取数走假工具（monkeypatch tools_read.execute），零外部网络；调度器逻辑直调函数
（agent_schedule.run_due）验证——不启动 8810/不碰运行中的服务。

验证项：
  A 调度到点触发 / 未到点不触发（直调 run_due，固定 now）：间隔未到不重跑、间隔到再跑、
    7 天间隔口径；到点真跑一遍（默认 runner=工作流引擎，假工具）：运行记录落 agent_workflow_runs
  B 有异动才发信（无异动零消息）：低余量/待处理异动/容量风险/7 天内到期/大额变化触发
    站内信（msg_type=inspection）；干净 highlights 零消息；花费平稳零消息
  C listMessages 拼接 workflow_run 不重复：合成消息含 steps/artifact/tickets 引用、
    msg_kind=workflow_run、重开（二次拉取）不翻倍；真消息已引 run_id 的不合成（防重复）；
    session_id=0 的运行（聊天链路）不合成
  D schedules CRUD + 审计：POST/GET/PATCH（enabled=false=停用）、409 重复、400 非分析域/
    参数越界、viewer 只读 403、审计 agent.schedule.create/update 落库 + ACTION_LABELS 登记
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace

# ---- 隔离环境必须先于 app 导入设置（env 覆盖 .env）----
_TMPDIR = tempfile.mkdtemp(prefix="qianshu-sched-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("sched")
os.environ["MASTER_KEY"] = "ab" * 32
os.environ["EVENT_WEBHOOK_TOKEN"] = "test-token"
os.environ["METRIC_SOURCES"] = "[]"
os.environ["LOG_SOURCES"] = "[]"
os.environ["VM_URL"] = "http://127.0.0.1:1"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from app.core.db import Base, SessionLocal, engine as db_engine  # noqa: E402
from app.core.db import get_db  # noqa: E402
from app.core.deps import get_current_user  # noqa: E402

# 全量模型注册后再 create_all（不触发 app.main 的启动建表/后台任务）
import importlib  # noqa: E402
import pkgutil  # noqa: E402
import app.models as _models_pkg  # noqa: E402

for _m in pkgutil.iter_modules(_models_pkg.__path__):
    importlib.import_module(f"app.models.{_m.name}")

from app.models.agent_change import AgentWorkflowRun  # noqa: E402
from app.models.agent_schedule import AgentWorkflowSchedule  # noqa: E402
from app.models.agent_session import AgentMessage, AgentSession  # noqa: E402
from app.models.audit import AuditLog  # noqa: E402
from app.models.in_app_message import InAppMessage  # noqa: E402
from app.models.user import User  # noqa: E402

from app.services import agent_schedule as sched  # noqa: E402
from app.services.audit import ACTION_LABELS  # noqa: E402
from app.services.copilot import tools_read  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


# ---------------- 假工具（monkeypatch tools_read.execute） ----------------

FAKE = {"packs": [], "anomalies": []}


def fake_execute(db, name, params):
    if name == "query_cost":
        return {"items": [{"product": "cdn", "name": "CDN", "amount": 90.0}], "total": 1}
    if name == "pack_pools":
        return {"items": list(FAKE["packs"])}
    if name == "cost_anomaly_list":
        return {"items": list(FAKE["anomalies"])}
    return {"items": []}


tools_read.execute = fake_execute

# ---------------- 种子（真 ORM：用户/会话） ----------------

from app.main import app  # noqa: E402  全量注册模型/路由后再建表

Base.metadata.create_all(db_engine)
_db = SessionLocal()
_admin = User(username="admin", password_hash="x", role="admin", is_active=True)
_db.add(_admin)
_db.commit()
_db.refresh(_admin)
_admin_id = _admin.id
_db.close()

STUB_USER = SimpleNamespace(id=_admin_id, username="admin", role="admin", is_active=True)
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
    patch=lambda url, **kw: _req("PATCH", url, **kw),
)


# ---------------- 假 runner（调度编排层测试用；真跑见 A 尾） ----------------

_HL_CLEAN = ["近 30 天花费 ¥100.00（账单口径；原价 ¥100.00，优惠 ¥0.00）",
             "资源包 1 个池，低余量（≤30%）0 个",
             "近 14 天异动 0 条（待处理 0 条）"]
_HL_LOW = ["近 30 天花费 ¥100.00（账单口径；原价 ¥100.00，优惠 ¥0.00）",
           "资源包 2 个池，低余量（≤30%）2 个",
           "近 14 天异动 1 条（待处理 1 条）"]
_HL_SWING = ["近 30 天花费 ¥250.00（账单口径；原价 ¥250.00，优惠 ¥0.00）",
             "资源包 2 个池，低余量（≤30%）0 个",
             "近 14 天异动 0 条（待处理 0 条）"]
_HL_CAP = ["容量风险 3 项（critical 1）", "低余量资源包 1 个",
           "90 天内到期 2 项（7 天内 1）", "RDS 磁盘增速检查 2 实例"]


def make_runner(highlights_list):
    """依次返回 highlights 的假 runner（每次调用落一条 agent_workflow_runs 真记录）。"""
    state = {"i": 0}

    def _runner(db, key, schedule):
        hl = highlights_list[min(state["i"], len(highlights_list) - 1)]
        state["i"] += 1
        run = AgentWorkflowRun(session_id=0, workflow_key=key, steps_json="[]", status="running")
        db.add(run)
        db.commit()
        art = {"report_md": "（假报告）", "highlights": list(hl), "generated_at": "2026-11-05"}
        run.steps_json = json.dumps([{"step": "体检", "status": "ok", "detail": "ok", "ms": 1,
                                      "name": "体检", "params_summary": "", "result_summary": "ok"}])
        run.status = "done"
        run.artifact_json = json.dumps(art, ensure_ascii=False)
        run.finished_at = datetime.now()
        db.commit()
        return {"run_id": run.id, "status": "done", "steps": [], "artifact": art, "tickets": []}

    return _runner


def msg_count(db):
    return db.query(InAppMessage).filter(InAppMessage.msg_type == "inspection").count()


# ================= A 调度到点触发 / 未到点不触发（直调 run_due） =================

print("== A 调度到点触发 / 未到点不触发 ==")
T0 = datetime(2026, 11, 5, 10, 30)
_db = SessionLocal()
# ① 到点（hour=10 <= now.hour=10，无 last_run_at）→ 触发
s1 = AgentWorkflowSchedule(workflow_key="cost_checkup", interval_days=1, hour=10,
                           enabled=True, created_by="admin")
_db.add(s1)
_db.commit()
r = sched.run_due(_db, now=T0, runner=make_runner([_HL_CLEAN]))
check("A 到点即跑（首跑 hour=now.hour）", len(r["ran"]) == 1 and r["ran"][0]["workflow_key"] == "cost_checkup", str(r))
_db.refresh(s1)
check("A 跑完落 last_run_at（幂等锚点）", s1.last_run_at == T0, str(s1.last_run_at))
# ② 未到点（hour=11 > 10）→ 不触发
s2 = AgentWorkflowSchedule(workflow_key="capacity_checkup", interval_days=1, hour=11,
                           enabled=True, created_by="admin")
_db.add(s2)
_db.commit()
r = sched.run_due(_db, now=T0, runner=make_runner([_HL_CLEAN]))
check("A 未到点不触发（hour=11）", len(r["ran"]) == 0, str(r))
# ③ 间隔未到（同日 10:45 再查）→ 不重跑
r = sched.run_due(_db, now=T0 + timedelta(minutes=15), runner=make_runner([_HL_CLEAN]))
check("A 间隔未到不重跑", len(r["ran"]) == 0, str(r))
# ④ 间隔到（+1 天，hour 到）→ 再跑
r = sched.run_due(_db, now=T0 + timedelta(days=1), runner=make_runner([_HL_CLEAN]))
check("A 间隔到再跑（interval_days=1）", len(r["ran"]) == 1, str(r))
# ⑤ 7 天间隔：+2 天不跑、+7 天跑（last_run_at=T0 假装 T0 刚跑过）
s3 = AgentWorkflowSchedule(workflow_key="change_impact", interval_days=7, hour=10,
                           enabled=True, created_by="admin", last_run_at=T0)
_db.add(s3)
_db.commit()
T1 = T0 + timedelta(days=2)
r = sched.run_due(_db, now=T1, runner=make_runner([_HL_CLEAN]))
check("A 7 天间隔未到不跑", not any(x["workflow_key"] == "change_impact" for x in r["ran"]), str(r))
r = sched.run_due(_db, now=T0 + timedelta(days=7), runner=make_runner([_HL_CLEAN]))
check("A 7 天间隔到跑", any(x["workflow_key"] == "change_impact" for x in r["ran"]), str(r))
# ⑥ 停用不跑
_db.query(AgentWorkflowSchedule).update({"enabled": False})
_db.commit()
r = sched.run_due(_db, now=T0 + timedelta(days=30), runner=make_runner([_HL_CLEAN]))
check("A 停用（enabled=false）不跑", len(r["ran"]) == 0, str(r))
_db.query(AgentWorkflowSchedule).delete()
_db.commit()

# ⑦ 到点真跑一遍（默认 runner=工作流引擎 + 假工具）：运行记录落 agent_workflow_runs
FAKE["packs"] = [{"type_label": "CDN 流量包", "commodity_code": "cdnpack",
                  "pack_count": 2, "remaining": "50GB", "remaining_pct": 12.5}]
s4 = AgentWorkflowSchedule(workflow_key="cost_checkup", interval_days=1, hour=0,
                           enabled=True, created_by="admin")
_db.add(s4)
_db.commit()
r = sched.run_due(_db, now=T0)   # 不传 runner：走真引擎
ok_run = r["ran"] and r["ran"][0]["status"] in ("done", "failed")
run_row = (_db.query(AgentWorkflowRun).filter(AgentWorkflowRun.workflow_key == "cost_checkup")
           .order_by(AgentWorkflowRun.id.desc()).first())
check("A 真跑（默认 runner）运行记录落 agent_workflow_runs",
      ok_run and run_row is not None and bool(json.loads(run_row.artifact_json or "{}").get("report_md")),
      str(r))
FAKE["packs"] = []

# ================= B 有异动才发信（无异动零消息） =================

print("== B 有异动才发信（无异动零消息） ==")
_db.query(AgentWorkflowSchedule).delete()
_db.query(InAppMessage).delete()
_db.commit()
s = AgentWorkflowSchedule(workflow_key="cost_checkup", interval_days=1, hour=0,
                          enabled=True, created_by="admin")
_db.add(s)
_db.commit()

# ① 干净 highlights（含花费）→ 零消息
sched.run_due(_db, now=T0, runner=make_runner([_HL_CLEAN]))
check("B 无异动零消息（干净 highlights）", msg_count(_db) == 0, f"count={msg_count(_db)}")

# ② 低余量 + 待处理异动 → 发信（msg_type=inspection）
_db.query(AgentWorkflowSchedule).update({"last_run_at": None})
_db.commit()
sched.run_due(_db, now=T0, runner=make_runner([_HL_LOW]))
rows = _db.query(InAppMessage).filter(InAppMessage.msg_type == "inspection").all()
check("B 有异动才发信（低余量/待处理异动）",
      len(rows) >= 1 and "低余量" in (rows[0].content_md or "") and "例行体检" in (rows[0].title or ""),
      f"rows={len(rows)}")
check("B 站内信 msg_type=inspection 且发给活跃用户",
      all(r.msg_type == "inspection" and r.user_id == _admin_id for r in rows), str([(r.user_id, r.msg_type) for r in rows]))

# ③ 花费平稳（同额）→ 零新消息
n_before = msg_count(_db)
_db.query(AgentWorkflowSchedule).update({"last_run_at": None})
_db.commit()
sched.run_due(_db, now=T0, runner=make_runner([_HL_CLEAN]))   # 花费 100 -> 100，其余干净
check("B 花费平稳零消息", msg_count(_db) == n_before, f"{n_before} -> {msg_count(_db)}")

# ④ 大额变化（100 -> 250，+150%）→ 发信
_db.query(AgentWorkflowSchedule).update({"last_run_at": None})
_db.commit()
sched.run_due(_db, now=T0, runner=make_runner([_HL_SWING]))
rows = _db.query(InAppMessage).filter(InAppMessage.msg_type == "inspection").all()
check("B 大额变化发信（环比 +150%）",
      any("大额变化" in (r.content_md or "") for r in rows), str([r.content_md[:40] for r in rows]))

# ⑤ 容量口径（容量风险/7 天内到期/低余量资源包）→ 发信
_db.query(AgentWorkflowSchedule).update({"last_run_at": None})
_db.commit()
n_before = msg_count(_db)
sched.run_due(_db, now=T0, runner=make_runner([_HL_CAP]))
rows = _db.query(InAppMessage).filter(InAppMessage.msg_type == "inspection").all()
check("B 容量风险/到期发信（capacity 口径）",
      msg_count(_db) > n_before and any("7 天内到期" in (r.content_md or "") for r in rows),
      f"{n_before} -> {msg_count(_db)}")

# ⑥ detect_anomalies 直判（口径快检）
a_clean = sched.detect_anomalies({"highlights": _HL_CLEAN}, {"highlights": _HL_CLEAN})
a_low = sched.detect_anomalies({"highlights": _HL_CLEAN}, {"highlights": _HL_LOW})
check("B detect_anomalies：干净=空 / 低余量=命中", not a_low == [] and a_clean == [], str(a_low))

# ================= C listMessages 拼接 workflow_run 不重复 =================

print("== C listMessages 拼接 workflow_run 不重复 ==")
sess = AgentSession(title="定时体检会话", user_id=_admin_id)
_db.add(sess)
_db.commit()
_db.refresh(sess)
sid = sess.id

FAKE["packs"] = []
r1 = client.post("/api/agent/workflows/cost_checkup/run",
                 json={"params": {"days": 5}, "session_id": sid})
check("C API run 带 session_id 落库", r1.status_code == 200 and r1.json().get("run_id"), r1.text[:200])
run1_id = r1.json().get("run_id")
_db.expire_all()
row1 = _db.get(AgentWorkflowRun, run1_id)
check("C 运行记录 session_id 归属会话", row1 is not None and row1.session_id == sid,
      str(row1.session_id if row1 else None))

msgs = client.get(f"/api/agent/sessions/{sid}/messages").json()["items"]
wf_items = [m for m in msgs if m.get("msg_kind") == "workflow_run"]
check("C 合成 workflow_run 消息存在且仅一条",
      len(wf_items) == 1 and wf_items[0]["id"] == f"wf-run-{run1_id}", str([m.get("id") for m in msgs]))
it = wf_items[0] if wf_items else {}
check("C 合成消息含 steps/artifact/tickets 引用",
      isinstance(it.get("steps"), list) and len(it["steps"]) >= 1
      and (it.get("artifact") or {}).get("report_md") and isinstance(it.get("tickets"), list)
      and it.get("workflow_name") == "成本体检" and it.get("status") in ("done", "failed"),
      json.dumps({k: it.get(k) for k in ("steps", "artifact", "tickets", "workflow_name", "status")}, ensure_ascii=False)[:200])

# 重开（二次拉取）不翻倍
msgs2 = client.get(f"/api/agent/sessions/{sid}/messages").json()["items"]
check("C 重开不翻倍（二次拉取幂等）",
      len([m for m in msgs2 if m.get("msg_kind") == "workflow_run"]) == 1, str(len(msgs2)))

# 真消息已引 run_id 的运行不合成（防与真消息重复）
r2 = client.post("/api/agent/workflows/cost_checkup/run",
                 json={"params": {"days": 6}, "session_id": sid})
run2_id = r2.json().get("run_id")
_db.expire_all()
_db.add(AgentMessage(
    session_id=sid, role="assistant", content_md=f"工作流已完成（run #{run2_id}）",
    tool_calls_json=json.dumps([{"name": "账单三口径对账", "run_id": run2_id}]),
))
_db.commit()
msgs3 = client.get(f"/api/agent/sessions/{sid}/messages").json()["items"]
wf_ids = {m.get("id") for m in msgs3 if m.get("msg_kind") == "workflow_run"}
check("C 真消息已引 run_id 不再合成（防重复）",
      wf_ids == {f"wf-run-{run1_id}"} and f"wf-run-{run2_id}" not in wf_ids, str(wf_ids))

# session_id=0 的运行（聊天链路/例行体检）不合成
_db.add(AgentWorkflowRun(session_id=0, workflow_key="cost_checkup",
                         steps_json="[]", status="done", artifact_json="{}"))
_db.commit()
msgs4 = client.get(f"/api/agent/sessions/{sid}/messages").json()["items"]
check("C session_id=0 运行不合成", len([m for m in msgs4 if m.get("msg_kind") == "workflow_run"]) == 1,
      str([m.get("id") for m in msgs4]))

# ================= D schedules CRUD + 审计 =================

print("== D schedules CRUD + 审计 ==")
_db.query(AgentWorkflowSchedule).delete()   # 清掉 B 段残留，D 从零建
_db.commit()
r = client.post("/api/agent/schedules",
                json={"workflow_key": "cost_checkup", "interval_days": 3, "hour": 8})
item = r.json() if r.status_code == 200 else {}
check("D POST 建定时（分析域）",
      r.status_code == 200 and item.get("workflow_key") == "cost_checkup"
      and item.get("interval_days") == 3 and item.get("hour") == 8 and item.get("enabled") is True
      and "workflow_name" in item and "next_run_at" in item and item.get("created_by") == "admin",
      r.text[:300])
sid_sched = item.get("id")

r = client.post("/api/agent/schedules", json={"workflow_key": "cost_checkup"})
check("D POST 重复 key 409", r.status_code == 409, f"{r.status_code} {r.text[:100]}")
r = client.post("/api/agent/schedules", json={"workflow_key": "access_cloud"})
check("D POST 非分析域 400（仅分析域可定时）", r.status_code == 400, f"{r.status_code} {r.text[:100]}")
r = client.post("/api/agent/schedules", json={"workflow_key": "capacity_checkup", "hour": 24})
check("D POST hour 越界 400", r.status_code == 400, f"{r.status_code}")
r = client.post("/api/agent/schedules", json={"workflow_key": "capacity_checkup", "interval_days": 0})
check("D POST interval_days 越界 400", r.status_code == 400, f"{r.status_code}")

r = client.get("/api/agent/schedules")
items = r.json().get("items") or []
check("D GET 清单（含上次/下次执行字段）",
      r.status_code == 200 and any(x["id"] == sid_sched for x in items)
      and all("last_run_at" in x and "next_run_at" in x for x in items), r.text[:200])

r = client.patch(f"/api/agent/schedules/{sid_sched}", json={"interval_days": 2, "hour": 7})
check("D PATCH 改间隔/时刻", r.status_code == 200 and r.json().get("interval_days") == 2
      and r.json().get("hour") == 7, r.text[:200])
r = client.patch(f"/api/agent/schedules/{sid_sched}", json={"enabled": False})
check("D PATCH 停用（enabled=false=删除口径）", r.status_code == 200 and r.json().get("enabled") is False,
      r.text[:200])

# 审计：agent.schedule.create/update 落库 + ACTION_LABELS 中文登记
_db.expire_all()
acts = [a.action for a in _db.query(AuditLog).all() if (a.action or "").startswith("agent.schedule.")]
check("D 审计落库（create/update）",
      "agent.schedule.create" in acts and "agent.schedule.update" in acts, str(acts))
check("D ACTION_LABELS 中文登记",
      ACTION_LABELS.get("agent.schedule.create") and ACTION_LABELS.get("agent.schedule.update"),
      str({k: ACTION_LABELS.get(k) for k in ("agent.schedule.create", "agent.schedule.update")}))

# viewer 只读：POST 403 / GET 200
STUB_USER.role = "viewer"
r = client.post("/api/agent/schedules", json={"workflow_key": "capacity_checkup"})
r2 = client.get("/api/agent/schedules")
STUB_USER.role = "admin"
check("D viewer 只读（POST 403 / GET 200）", r.status_code == 403 and r2.status_code == 200,
      f"post={r.status_code} get={r2.status_code}")

# ================= E 到点口径快检（is_due / next_run_at） =================

print("== E 到点口径快检 ==")
now = datetime(2026, 11, 5, 9, 40)
check("E hour 未到不触发", not sched.is_due(None, 1, 10, now))
check("E hour 到即触发（首跑）", sched.is_due(None, 1, 9, now))
check("E 重启补跑（hour 已过也触发）", sched.is_due(None, 1, 3, now))
check("E 间隔未到不触发", not sched.is_due(now - timedelta(hours=1), 3, 3, now))
check("E 间隔到触发", sched.is_due(now - timedelta(days=3), 3, 3, now))
nxt = sched.next_run_at(None, 1, 10, now)
check("E next_run_at：当天 hour 点", nxt == datetime(2026, 11, 5, 10, 0), str(nxt))
check("E next_run_at：已到点=待触发(None)", sched.next_run_at(None, 1, 9, now) is None)

_db.close()

print(f"\n== 结果: {len(PASS)} 通过 / {len(FAIL)} 失败 ==")
if FAIL:
    print("失败项: " + " | ".join(FAIL))
    raise SystemExit(1)
raise SystemExit(0)
