#!/usr/bin/env python3
"""自定义工作流（workflow_defs）mock 测试。

运行：cd /opt/qianshu/backend && python3 tests/test_workflow_custom_mock.py
隔离性：DATABASE_URL/MASTER_KEY 在 import app.* 之前改写为临时值——不碰真实 qianshu.db；
工具取数走假工具（monkeypatch tools_read.execute），变更层走真 change_tools（验"只建单不执行"），
零外部网络。不重启 8810（TestClient/ASGITransport）。

验证项：
  A CRUD+schema 校验：创建/幂等覆盖导入/内置 key 冲突/白名单外工具拒/未知参数拒/
    敏感字面量拒（占位符放行）/必填缺失拒/字面量类型拒/更新 version+1/删除
  B 执行：占位符代入（保类型）/缺参数失败+失败策略中止后续/失败继续/变更步骤只建单不执行/
    停用不可跑/凭证不落 steps/artifact
  C 面板互通+权限：合并清单（内置 12 + 自定义）/admin 管 operator 跑 viewer 只读/
    loop 三参兼容
  D 预览执行：逐步展开（工具/解析参数/是否建单）+ 敏感掩码 + 缺参如实标——零副作用
  E 内置回归：13 key 不变、内置照跑、未知 key 404
"""
import asyncio
import json
import os
import sys
import tempfile
from types import SimpleNamespace

# ---- 隔离环境必须先于 app 导入设置（env 覆盖 .env）----
_TMPDIR = tempfile.mkdtemp(prefix="qianshu-wfc-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("wfc")
os.environ["MASTER_KEY"] = "ab" * 32
os.environ["EVENT_WEBHOOK_TOKEN"] = "test-token"
os.environ["METRIC_SOURCES"] = "[]"
os.environ["LOG_SOURCES"] = "[]"
os.environ["VM_URL"] = "http://127.0.0.1:1"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from app.core.db import Base, SessionLocal, engine as db_engine  # noqa: E402
from app.core.deps import get_current_user  # noqa: E402
from app.core.db import get_db  # noqa: E402
from app.models.agent_change import AgentChangeTicket, AgentWorkflowRun  # noqa: E402
from app.models.workflow_def import WorkflowDef  # noqa: E402

from app.services.copilot import tools_read  # noqa: E402
from app.services.copilot import workflows as wf  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


# ---------------- 假工具（monkeypatch tools_read.execute） ----------------

FAKE_CALLS: list = []


def fake_execute(db, name, params):
    FAKE_CALLS.append((name, dict(params or {})))
    if name == "host_metrics" and str(params.get("instance")) == "boom":
        raise RuntimeError("模拟工具异常 boom")
    if name == "query_cost":
        return {"items": [{"product": "cdn", "name": "CDN", "amount": 90.0}], "total": 1,
                "days": params.get("days")}
    if name == "pack_pools":
        return {"items": [{"type_label": "CDN 流量包", "remaining": "50GB"}]}
    return {"items": []}


tools_read.execute = fake_execute

# ---------------- 启动隔离应用（全量注册模型/路由后建表） ----------------

from app.main import app  # noqa: E402

Base.metadata.create_all(db_engine)
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
    put=lambda url, **kw: _req("PUT", url, **kw),
    delete=lambda url, **kw: _req("DELETE", url, **kw),
)

BASE = "/api/agent/workflows"
DEMO_STEPS = [
    {"tool": "query_cost", "label": "查成本", "params_template": {"days": "{{days}}"}},
    {"tool": "pack_pools", "label": "看资源包", "params_template": {}, "continue_on_fail": True},
]

# ---------------- A CRUD + schema 校验 ----------------

print("== A 自定义定义 CRUD + schema 校验 ==")
r = client.post(f"{BASE}/custom", json={
    "key": "wf_demo", "name": "成本巡检（自定义）", "domain": "自定义域",
    "description": "查成本 + 看资源包", "steps": DEMO_STEPS})
d = r.json()
check("A 创建成功（version=1/custom/params_hint）",
      r.status_code == 200 and d.get("key") == "wf_demo" and d.get("version") == 1
      and d.get("custom") is True and d.get("params_hint") == ["days"]
      and d.get("created_by") == "admin" and len(d.get("steps") or []) == 2,
      r.text[:300])

r = client.post(f"{BASE}/custom", json={"key": "wf_demo", "name": "重复", "steps": DEMO_STEPS})
check("A 同 key 重复创建 409", r.status_code == 409, r.text[:200])
r = client.post(f"{BASE}/custom", json={
    "key": "wf_demo", "name": "成本巡检（自定义）", "domain": "自定义域",
    "steps": DEMO_STEPS, "overwrite": True})
check("A overwrite 幂等覆盖（version=2，不产生重复行）",
      r.status_code == 200 and r.json().get("version") == 2,
      r.text[:200])
_db = SessionLocal()
n_defs = _db.query(WorkflowDef).filter(WorkflowDef.key == "wf_demo").count()
_db.close()
check("A 幂等导入不产生重复行", n_defs == 1, f"rows={n_defs}")

r = client.post(f"{BASE}/custom", json={"key": "cost_checkup", "name": "撞内置", "steps": DEMO_STEPS})
check("A 内置 key 冲突拒（400）", r.status_code == 400 and "内置" in r.text, r.text[:200])
r = client.post(f"{BASE}/custom", json={
    "key": "wf_bad_tool", "name": "非法工具", "steps": [{"tool": "run_shell", "label": "执行命令"}]})
check("A 白名单外工具拒（无任意代码/命令步骤）",
      r.status_code == 400 and "白名单" in r.text, r.text[:200])
r = client.post(f"{BASE}/custom", json={
    "key": "wf_bad_param", "name": "未知参数", "steps": [
        {"tool": "query_cost", "label": "查成本", "params_template": {"nope": "1"}}]})
check("A 未知参数名拒", r.status_code == 400 and "参数" in r.text, r.text[:200])
r = client.post(f"{BASE}/custom", json={
    "key": "wf_secret", "name": "敏感字面量", "steps": [
        {"tool": "reset_user_password", "label": "重置密码",
         "params_template": {"new_password": "abc12345"}}]})
check("A 敏感参数字面量拒（凭证不落定义）",
      r.status_code == 400 and "敏感" in r.text, r.text[:200])
r = client.post(f"{BASE}/custom", json={
    "key": "wf_secret", "name": "重置密码流程", "domain": "自定义域",
    "steps": [{"tool": "reset_user_password", "label": "重置密码",
               "params_template": {"new_password": "{{pwd}}"}}]})
check("A 敏感参数占位符放行", r.status_code == 200, r.text[:200])
r = client.post(f"{BASE}/custom", json={
    "key": "wf_bad_req", "name": "缺必填", "steps": [
        {"tool": "save_alert_rule", "label": "存告警规则", "params_template": {"name": "n"}}]})
check("A 变更工具缺必填参数拒", r.status_code == 400 and "必填" in r.text, r.text[:200])
r = client.post(f"{BASE}/custom", json={
    "key": "wf_bad_type", "name": "字面量类型", "steps": [
        {"tool": "sync_cloud", "label": "同步", "params_template": {"account_id": "not-int"}}]})
check("A 字面量类型不符拒（schema 校验）", r.status_code == 400, r.text[:200])
r = client.post(f"{BASE}/custom", json={
    "key": "wf_change", "name": "变更建单流程", "domain": "自定义域",
    "steps": [{"tool": "sync_cloud", "label": "触发全量同步",
               "params_template": {"account_id": "{{acc}}", "product": ""}}]})
check("A 变更步骤+占位符创建成功（has_change_steps）",
      r.status_code == 200 and r.json().get("has_change_steps") is True, r.text[:200])

r = client.put(f"{BASE}/custom/wf_demo", json={
    "name": "成本巡检 v2", "steps": DEMO_STEPS[:1]})
d = r.json()
check("A PUT 更新 version+1（3）且步骤整表替换",
      r.status_code == 200 and d.get("version") == 3 and len(d.get("steps") or []) == 1,
      r.text[:200])
r = client.put(f"{BASE}/custom/wf_demo", json={"steps": DEMO_STEPS})
check("A PUT 恢复步骤（version=4）", r.status_code == 200 and r.json().get("version") == 4)

r = client.get(f"{BASE}/custom")
items = r.json().get("items") or []
keys = {i["key"] for i in items}
check("A GET 列表含全部自定义定义（steps/version 可审）",
      r.status_code == 200 and {"wf_demo", "wf_secret", "wf_change"} <= keys
      and all({"steps", "version", "params_hint", "enabled"} <= set(i) for i in items),
      r.text[:300])

# ---------------- B 执行 ----------------

print("== B 自定义工作流执行（假工具 + 真变更层） ==")
FAKE_CALLS.clear()
r = client.post(f"{BASE}/wf_demo/run", json={"params": {"days": 7}})
d = r.json()
check("B 运行成功（占位符保类型代入）",
      r.status_code == 200 and d.get("status") == "done" and len(d.get("steps") or []) == 2
      and ("query_cost", {"days": 7}) in FAKE_CALLS, r.text[:300])
check("B 产物=步骤表报告+要点", "自定义工作流运行报告" in (d.get("artifact") or {}).get("report_md", "")
      and "查成本" in (d.get("artifact") or {}).get("report_md", "")
      and (d.get("artifact") or {}).get("highlights"), r.text[:300])
_db = SessionLocal()
run_row = _db.query(AgentWorkflowRun).filter(AgentWorkflowRun.workflow_key == "wf_demo").first()
run_ok = run_row is not None and json.loads(run_row.steps_json or "[]")
_db.close()
check("B 运行记录落库（agent_workflow_runs 可解析）", bool(run_ok))

FAKE_CALLS.clear()
r = client.post(f"{BASE}/wf_demo/run", json={"params": {}})
d = r.json()
by_step = {s["step"]: s for s in d.get("steps") or []}
check("B 缺占位参数=失败+失败策略中止后续（默认 continue_on_fail=False）",
      r.status_code == 200 and d.get("status") == "failed"
      and by_step.get("查成本", {}).get("status") == "failed"
      and "缺少运行参数" in by_step.get("查成本", {}).get("detail", "")
      and by_step.get("看资源包", {}).get("status") == "skipped"
      and not FAKE_CALLS, str(by_step)[:300])

r = client.post(f"{BASE}/custom", json={
    "key": "wf_keep", "name": "失败继续", "steps": [
        {"tool": "host_metrics", "label": "查主机", "params_template": {"instance": "{{inst}}"},
         "continue_on_fail": True},
        {"tool": "recent_changes", "label": "查变更", "params_template": {"hours": "24"}}]})
check("B 失败继续策略定义创建", r.status_code == 200, r.text[:200])
r = client.post(f"{BASE}/wf_keep/run", json={"params": {"inst": "boom"}})
d = r.json()
by_step = {s["step"]: s for s in d.get("steps") or []}
check("B continue_on_fail=True：失败后后续照跑",
      d.get("status") == "failed" and by_step.get("查主机", {}).get("status") == "failed"
      and by_step.get("查变更", {}).get("status") == "ok", str(by_step)[:300])

_db = SessionLocal()
n_before = _db.query(AgentChangeTicket).count()
_db.close()
FAKE_CALLS.clear()
r = client.post(f"{BASE}/wf_change/run", json={"params": {"acc": 7}})
d = r.json()
check("B 变更步骤只建待确认工单（不执行）",
      r.status_code == 200 and len(d.get("tickets") or []) == 1
      and d["tickets"][0].get("change_list", [{}])[0].get("tool") == "sync_cloud"
      and "待确认变更工单" in (d.get("steps") or [{}])[0].get("detail", ""), r.text[:300])
_db = SessionLocal()
ticket = (_db.query(AgentChangeTicket)
          .order_by(AgentChangeTicket.id.desc()).first())
n_after = _db.query(AgentChangeTicket).count()
ticket_ok = (ticket is not None and ticket.status == "pending"
             and json.loads(ticket.change_list_json or "[]")[0]["tool"] == "sync_cloud")
_db.close()
check("B 工单真落库且 pending（执行走变更确认门）",
      n_after == n_before + 1 and ticket_ok, f"before={n_before} after={n_after}")

r = client.post(f"{BASE}/wf_secret/run", json={"params": {"pwd": "S3CRETVALUE999"}})
d = r.json()
leak_src = json.dumps(d.get("steps") or [], ensure_ascii=False) + \
    json.dumps(d.get("artifact") or {}, ensure_ascii=False) + \
    json.dumps(d.get("tickets") or [], ensure_ascii=False)
check("B 凭证不落 steps/artifact/tickets 出参",
      r.status_code == 200 and "S3CRETVALUE999" not in leak_src, leak_src[:300])

r = client.put(f"{BASE}/custom/wf_change", json={"enabled": False})
check("B 停用（enabled=false）", r.status_code == 200 and r.json().get("enabled") is False)
r = client.post(f"{BASE}/wf_change/run", json={"params": {"acc": 7}})
check("B 停用后不可运行（404）", r.status_code == 404 and "停用" in r.text, r.text[:200])
r = client.put(f"{BASE}/custom/wf_change", json={"enabled": True})
check("B 重新启用", r.status_code == 200 and r.json().get("enabled") is True)

# ---------------- C 面板互通 + 权限 ----------------

print("== C 合并清单/权限/loop 兼容 ==")
r = client.get(f"{BASE}")
items = r.json().get("items") or []
builtin = [i for i in items if not i.get("custom")]
customs = [i for i in items if i.get("custom")]
check("C 合并清单：内置 13 + 自定义 4（面板互通可运行）",
      r.status_code == 200 and len(builtin) == 13 and len(customs) == 4
      and all({"key", "name", "domain", "description", "last_run"} <= set(i) for i in items)
      and all(i.get("enabled") is not None for i in customs),
      f"builtin={len(builtin)} custom={len(customs)}")
check("C 内置项不受影响（无 custom 标记）",
      all("custom" not in i for i in builtin))

STUB_USER.role = "viewer"
r1 = client.post(f"{BASE}/custom", json={"key": "wf_x", "name": "x", "steps": DEMO_STEPS})
r2 = client.get(f"{BASE}/custom")
r3 = client.post(f"{BASE}/wf_demo/run", json={"params": {"days": 3}})
r4 = client.get(f"{BASE}")
check("C viewer：管理 403 / 清单 200 / run 403",
      r1.status_code == 403 and r2.status_code == 403
      and r3.status_code == 403 and r4.status_code == 200,
      f"{r1.status_code}/{r2.status_code}/{r3.status_code}/{r4.status_code}")
STUB_USER.role = "operator"
r1 = client.post(f"{BASE}/custom", json={"key": "wf_x", "name": "x", "steps": DEMO_STEPS})
r2 = client.post(f"{BASE}/wf_demo/run", json={"params": {"days": 3}})
check("C operator：不可管理（403）/ 可跑（200）",
      r1.status_code == 403 and r2.status_code == 200, f"{r1.status_code}/{r2.status_code}")
STUB_USER.role = "admin"

r3 = wf.run_workflow(SessionLocal(), "wf_demo", {"days": 5})
check("C loop 三参兼容（run_workflow 直调自定义 key）",
      isinstance(r3, dict) and r3.get("content_md") and r3.get("run_id")
      and all(s.get("name") == s.get("step") for s in r3["steps"]), str(r3)[:200])

# ---------------- D 预览执行（零副作用） ----------------

print("== D 预览执行（不执行/不建单/敏感掩码） ==")
_db = SessionLocal()
n_tickets_before = _db.query(AgentChangeTicket).count()
_db.close()
FAKE_CALLS.clear()
r = client.post(f"{BASE}/custom/preview", json={
    "steps": [
        {"tool": "query_cost", "label": "查成本", "params_template": {"days": "{{days}}"}},
        {"tool": "reset_user_password", "label": "重置密码",
         "params_template": {"new_password": "{{pwd}}"}},
        {"tool": "host_metrics", "label": "查主机", "params_template": {"instance": "{{inst}}"}}],
    "params": {"days": 3, "pwd": "S3CRETVALUE999"}})
d = r.json()
plan = d.get("steps") or []
check("D 预览逐步展开（解析参数/建单标记）",
      r.status_code == 200 and len(plan) == 3
      and plan[0]["params"] == {"days": 3} and plan[0]["status"] == "ready"
      and plan[1]["will_ticket"] is True and plan[2]["status"] == "missing_param"
      and "inst" in plan[2].get("error", ""), r.text[:300])
check("D 预览敏感参数掩码", plan[1]["params"].get("new_password") == "****"
      and "S3CRETVALUE999" not in r.text, r.text[:300])
_db = SessionLocal()
n_tickets_after = _db.query(AgentChangeTicket).count()
_db.close()
check("D 预览零副作用（不建工单/不调工具）",
      n_tickets_after == n_tickets_before and not FAKE_CALLS,
      f"{n_tickets_before}->{n_tickets_after}")

r = client.post(f"{BASE}/custom/preview", json={
    "steps": [{"tool": "run_shell", "label": "bad"}]})
check("D 预览同样拒白名单外工具", r.status_code == 400, r.text[:200])
r = client.post(f"{BASE}/custom/preview", json={"key": "wf_demo", "params": {"days": 2}})
check("D 按 key 预览已存定义", r.status_code == 200
      and len(r.json().get("steps") or []) == 2, r.text[:200])

# ---------------- E 内置回归 ----------------

print("== E 内置工作流回归（只加不改） ==")
EXPECTED = {"access_cloud", "access_datasource", "access_k8s", "access_virt",
            "cost_checkup", "fault_diagnosis", "capacity_checkup", "change_impact",
            "topology_refine", "alert_tune", "inspection_custom", "dashboard_custom",
            "onboard_init"}
check("E 内置注册表 13 key 不变", set(wf.WORKFLOWS) == EXPECTED, str(set(wf.WORKFLOWS) ^ EXPECTED))
r = client.post(f"{BASE}/cost_checkup/run", json={"params": {"days": 10}})
d = r.json()
check("E 内置工作流照跑（引擎改动零影响）",
      r.status_code == 200 and d.get("status") == "done"
      and len(d.get("steps") or []) == 4 and d.get("artifact", {}).get("report_md"),
      r.text[:300])
r = client.post(f"{BASE}/cost_checkup/run", json={"params": {"days": 999}})
check("E 内置 params_schema 照常强校验（params_passthrough 不旁路内置）",
      r.status_code == 400, r.text[:200])
r = client.post(f"{BASE}/no_such_wf/run", json={"params": {}})
check("E 未知 key 404（含自定义回落）", r.status_code == 404, r.text[:200])

print(f"\n== 结果: {len(PASS)} 通过 / {len(FAIL)} 失败 ==")
if FAIL:
    print("失败项: " + " | ".join(FAIL))
    raise SystemExit(1)
raise SystemExit(0)
