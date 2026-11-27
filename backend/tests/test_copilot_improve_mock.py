#!/usr/bin/env python3
"""千枢智能体改进域 mock 测试：四个工作流 + 变更工单 + 快照回滚 + planner。

运行：cd /opt/qianshu/backend && python3 tests/test_copilot_improve_mock.py
隔离性：DATABASE_URL/MASTER_KEY 在 import app.* 之前改写为临时值——不碰真实 qianshu.db；
env_store.set_keys 走假记录器（不写 .env）；零外部网络；API 直调函数（不启 8810）。

验证项：
  A 工作流注册：改进域 4 个 key 齐全，契约字段/域分布正确，未知 key 拒
  B ⑨拓扑重画：反馈→save_edge_status/save_ownership_rule 工单（tool/ack_required/sensitive）；
    只建单不执行（库内状态未变）；确认执行→快照落库→一键回滚恢复原状态
  C ⑩告警降噪调参：30 天告警取数→调参建议→save_alert_rule 工单；执行→阈值真改→回滚→回旧值
  D ⑪巡检规则定制：需求→save_inspection_rule 工单；执行→settings 真改（.env 走假记录器）→回滚→回旧值
  E ⑫仪表盘定制：偏好→update_dashboard_cards 工单；执行→PRODUCT_CARDS 真改→回滚→恢复
  F 白名单红线：白名单外工具拒建单；四工作流产出的 tool 全在 CHANGE_TOOLS 内
  G planner：改进域强短语命中/参数抽取（feedback 透传）/同分回退/弱信号回退/闲聊回退
"""
import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from types import SimpleNamespace

# ---- 隔离环境必须先于 app 导入设置（env 覆盖 .env）----
_TMPDIR = tempfile.mkdtemp(prefix="qianshu-improve-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("improve")
os.environ["MASTER_KEY"] = "ab" * 32
os.environ["EVENT_WEBHOOK_TOKEN"] = "test-token"
os.environ["METRIC_SOURCES"] = "[]"
os.environ["LOG_SOURCES"] = "[]"
os.environ["VM_URL"] = "http://127.0.0.1:1"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.db import Base, SessionLocal, engine as db_engine  # noqa: E402

import importlib  # noqa: E402
import pkgutil  # noqa: E402
import app.models as _models_pkg  # noqa: E402
for _m in pkgutil.iter_modules(_models_pkg.__path__):
    importlib.import_module(f"app.models.{_m.name}")

from app.models.agent_change import AgentChangeSnapshot, AgentChangeTicket  # noqa: E402
from app.models.alert_record import AlertRecord  # noqa: E402
from app.models.alert_rule import AlertRule  # noqa: E402
from app.models.asset import Asset  # noqa: E402
from app.models.link_edge import LinkEdge  # noqa: E402
from app.models.ownership import OwnershipRule  # noqa: E402

from app.services import env_store  # noqa: E402
from app.services.copilot import change_gate, change_tools  # noqa: E402
from app.services.copilot import planner as planner_mod  # noqa: E402
from app.services.copilot import workflows as wf  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


# env_store 假记录器：巡检/卡片配置写 .env 全走这里（绝不写真 .env）
ENV_CALLS: list = []
env_store.set_keys = lambda values: (ENV_CALLS.append(dict(values)), True)[1]

from app.main import app  # noqa: E402,F401  全量注册模型/路由（建表用）

Base.metadata.create_all(db_engine)
_now = datetime.now()
_db = SessionLocal()
_db.add_all([
    LinkEdge(chain="outer", host="www.topo-test.com", upstream_addr="10.0.0.1:80",
             ecs_name="ecs-a", app_name="topo-app", req_count=1000, cnt_5xx=0,
             rt_avg_ms=50.0, status="draft", generated_at=_now),
    LinkEdge(chain="outer", host="www.topo-test.com", upstream_addr="10.0.0.2:80",
             ecs_name="ecs-b", app_name="topo-app", req_count=500, cnt_5xx=0,
             rt_avg_ms=60.0, status="draft", note="旧备注", generated_at=_now),
    AlertRule(name="_noise_rule", source="metric", level="warning", product="ecs",
              metric="CPUUtilization", comparator=">", threshold=90.0, duration=300,
              channel_ids="[]", silences="[]", enabled=True),
    Asset(product="ecs", instance_id="i-ecs1", name="ecs-a", status="Running"),
    Asset(product="rds", instance_id="rm-1", name="db1", status="Running"),
])
for _i in range(12):
    _db.add(AlertRecord(account_id=1, rule_id=1, rule_name="_noise_rule", source="metric",
                        target="i-ecs1", level="warning", title=f"CPU 高 {_i}",
                        status="firing" if _i % 3 else "recovered",
                        breach_value=92.0, fired_at=_now - timedelta(days=_i)))
_db.commit()
_db.close()

STUB_ADMIN = SimpleNamespace(id=1, username="admin", role="admin", is_active=True)


def _run(key: str, params: dict) -> dict:
    return wf.run_workflow(SessionLocal(), key, params,
                           {"user": "tester", "user_id": 1, "session_id": 1})


def _execute(tid: int) -> dict:
    return change_gate.execute_ticket(SessionLocal(), tid, STUB_ADMIN, ack="我已知晓")


def _rollback(tid: int) -> dict:
    return change_gate.rollback_ticket(SessionLocal(), tid, STUB_ADMIN)


# ---------------- A 工作流注册 ----------------

print("== A 改进域工作流注册 ==")
IMPROVE_KEYS = {"topology_refine", "alert_tune", "inspection_custom", "dashboard_custom"}
check("A 改进域 4 个 key 齐全", IMPROVE_KEYS <= set(wf.WORKFLOWS), str(set(wf.WORKFLOWS) ^ IMPROVE_KEYS))
check("A 契约字段+域=改进域",
      all(wf.WORKFLOWS[k].get("name", {}).get("zh") and wf.WORKFLOWS[k].get("domain", {}).get("zh") == "改进域"
          and wf.WORKFLOWS[k].get("description", {}).get("zh")
          and isinstance(wf.WORKFLOWS[k].get("params_schema"), dict)
          and wf.WORKFLOWS[k].get("steps") and wf.WORKFLOWS[k].get("build_artifact")
          for k in IMPROVE_KEYS))
try:
    wf.run_workflow(SessionLocal(), "no_such_improve", {}, {})
    check("A 未知 key 抛 WorkflowNotFound", False)
except wf.WorkflowNotFound:
    check("A 未知 key 抛 WorkflowNotFound", True)
try:
    wf.run_workflow(SessionLocal(), "topology_refine", {}, {})
    check("A 缺必填 feedback 抛 ParamError", False)
except wf.ParamError:
    check("A 缺必填 feedback 抛 ParamError", True)

# ---------------- B ⑨ topology_refine 拓扑重画 ----------------

print("== B ⑨拓扑重画（反馈→工单→快照回滚） ==")
r = _run("topology_refine", {"feedback": "野域名 www.topo-test.com，忽略掉"})
check("B status/steps/产物契约", r["status"] == "done" and len(r["steps"]) == 2
      and r["artifact"].get("report_md") and isinstance(r["artifact"].get("suggestions"), list),
      str(r)[:300])
check("B 只建单不执行（边仍 draft）",
      all(e.status == "draft" for e in SessionLocal().query(LinkEdge).all()))
t = r["tickets"]
check("B save_edge_status 工单（tool/ack/sensitive）",
      len(t) == 1 and t[0]["ticket_id"]
      and t[0]["change_list"][0]["tool"] == "save_edge_status"
      and t[0]["change_list"][0]["ack_required"] is False
      and t[0]["change_list"][0]["sensitive"] is False
      and {"ids", "status"} <= set(t[0]["change_list"][0].get("params_keys", [])),
      str(t)[:300])
tid = t[0]["ticket_id"]
_db = SessionLocal()
row = _db.get(AgentChangeTicket, tid)
cl = json.loads(row.change_list_json)[0]
_db.close()
check("B 工单参数含 2 条边且 status=ignored（落库明文参数可查）",
      cl["tool"] == "save_edge_status" and cl["params"]["status"] == "ignored"
      and sorted(cl["params"]["ids"]) == sorted(
          [e.id for e in SessionLocal().query(LinkEdge).all()]),
      str(cl)[:300])
res = _execute(tid)
check("B 确认执行：2 条边→ignored + 快照落库",
      res["status"] == "done"
      and all(e.status == "ignored" for e in SessionLocal().query(LinkEdge).all())
      and SessionLocal().query(AgentChangeSnapshot)
          .filter(AgentChangeSnapshot.ticket_id == tid).count() == 1, str(res)[:300])
snap = SessionLocal().query(AgentChangeSnapshot).filter(
    AgentChangeSnapshot.ticket_id == tid).first()
before = json.loads(snap.before_blob)
check("B 快照含改前状态（2 边 draft + 旧备注）",
      len(before.get("edges") or []) == 2
      and {e["status"] for e in before["edges"]} == {"draft"}
      and any(e.get("note") == "旧备注" for e in before["edges"]), str(before)[:300])
rb = _rollback(tid)
check("B 回滚恢复原状态（draft + 旧备注）",
      rb.get("rolled_back") is True
      and all(e.status == "draft" for e in SessionLocal().query(LinkEdge).all())
      and SessionLocal().query(LinkEdge).filter(LinkEdge.note == "旧备注").count() == 1,
      str(rb)[:300])

# B2 归属登记变体
r2 = _run("topology_refine", {"feedback": "www.topo-test.com 归属到交易业务"})
t2 = r2["tickets"]
check("B2 归属动作→save_ownership_rule 工单",
      len(t2) == 1 and t2[0]["change_list"][0]["tool"] == "save_ownership_rule"
      and t2[0]["change_list"][0]["ack_required"] is False, str(t2)[:300])
tid2 = t2[0]["ticket_id"]
res2 = _execute(tid2)
rule = SessionLocal().query(OwnershipRule).filter(
    OwnershipRule.key_value == "www.topo-test.com").first()
check("B2 执行→归属登记真写入（business=交易业务）",
      res2["status"] == "done" and rule is not None and rule.business == "交易业务",
      str(res2)[:200])
rb2 = _rollback(tid2)
check("B2 回滚→登记删除",
      rb2.get("rolled_back") is True
      and SessionLocal().query(OwnershipRule).filter(
          OwnershipRule.key_value == "www.topo-test.com").first() is None, str(rb2)[:200])
r3 = _run("topology_refine", {"feedback": "今天天气不错"})
_b3_detail = (r3["steps"][0]["detail"] or "") + (r3["steps"][1]["detail"] or "")
check("B3 未定性/无目标反馈→不建单（绝不静默改）",
      r3["status"] == "done" and r3["tickets"] == []
      and ("未定性" in _b3_detail or "没识别出目标对象" in _b3_detail),
      str(r3["steps"])[:300])

# ---------------- C ⑩ alert_tune 告警降噪调参 ----------------

print("== C ⑩告警降噪调参 ==")
r = _run("alert_tune", {"days": 30})
check("C status/steps/产物", r["status"] == "done" and len(r["steps"]) == 3
      and r["artifact"].get("report_md") and r["artifact"].get("suggestions"), str(r)[:300])
t = r["tickets"]
check("C save_alert_rule 调参工单（tool/ack）",
      len(t) == 1 and t[0]["change_list"][0]["tool"] == "save_alert_rule"
      and t[0]["change_list"][0]["ack_required"] is False, str(t)[:300])
tid = t[0]["ticket_id"]
_db = SessionLocal()
cl = json.loads(_db.get(AgentChangeTicket, tid).change_list_json)[0]
_db.close()
check("C 调参参数：阈值上调+持续窗（90→~99）",
      cl["params"]["name"] == "_noise_rule" and cl["params"]["source"] == "metric"
      and cl["params"]["threshold"] > 90 and cl["params"]["duration"] >= 300, str(cl)[:300])
res = _execute(tid)
thr_now = SessionLocal().query(AlertRule).filter(AlertRule.name == "_noise_rule").first().threshold
check("C 执行→阈值真改", res["status"] == "done" and abs(thr_now - cl["params"]["threshold"]) < 0.01,
      f"threshold={thr_now}")
rb = _rollback(tid)
thr_back = SessionLocal().query(AlertRule).filter(AlertRule.name == "_noise_rule").first().threshold
check("C 回滚→阈值回旧值 90", rb.get("rolled_back") is True and thr_back == 90.0,
      f"threshold={thr_back}")

# ---------------- D ⑪ inspection_custom 巡检规则定制 ----------------

print("== D ⑪巡检规则定制 ==")
from app.core.config import settings as _settings  # noqa: E402

r = _run("inspection_custom", {"requirement": "磁盘预警阈值改成 75%，严重阈值 85%"})
check("D status/steps/产物", r["status"] == "done" and len(r["steps"]) == 3
      and r["artifact"].get("report_md") and r["artifact"].get("suggestions"), str(r)[:300])
t = r["tickets"]
check("D save_inspection_rule 工单（tool/ack）",
      len(t) == 1 and t[0]["change_list"][0]["tool"] == "save_inspection_rule"
      and t[0]["change_list"][0]["ack_required"] is False, str(t)[:300])
tid = t[0]["ticket_id"]
_db = SessionLocal()
cl = json.loads(_db.get(AgentChangeTicket, tid).change_list_json)[0]
_db.close()
check("D 需求解析出 health_warn=75/health_crit=85",
      cl["params"].get("health_warn") == 75 and cl["params"].get("health_crit") == 85,
      str(cl)[:300])
ENV_CALLS.clear()
res = _execute(tid)
check("D 执行→settings 真改（warn=75/crit=85）+ .env 写入（假记录器）",
      res["status"] == "done" and _settings.INSPECTION_HEALTH_WARN == 75.0
      and _settings.INSPECTION_HEALTH_CRIT == 85.0 and len(ENV_CALLS) == 1
      and "INSPECTION_HEALTH_WARN" in str(ENV_CALLS), f"{res} {ENV_CALLS}")
rb = _rollback(tid)
check("D 回滚→阈值回旧值（warn=80/crit=90）+ .env 再写",
      rb.get("rolled_back") is True and _settings.INSPECTION_HEALTH_WARN == 80.0
      and _settings.INSPECTION_HEALTH_CRIT == 90.0 and len(ENV_CALLS) == 2,
      f"{rb} {ENV_CALLS}")

# ---------------- E ⑫ dashboard_custom 仪表盘定制 ----------------

print("== E ⑫仪表盘定制 ==")
r = _run("dashboard_custom", {"role": "dba", "preference": "数据库相关"})
check("E status/steps/产物", r["status"] == "done" and len(r["steps"]) == 3
      and r["artifact"].get("report_md") and r["artifact"].get("suggestions"), str(r)[:300])
t = r["tickets"]
check("E update_dashboard_cards 工单（tool/ack）",
      len(t) == 1 and t[0]["change_list"][0]["tool"] == "update_dashboard_cards"
      and t[0]["change_list"][0]["ack_required"] is False, str(t)[:300])
tid = t[0]["ticket_id"]
_db = SessionLocal()
cl = json.loads(_db.get(AgentChangeTicket, tid).change_list_json)[0]
_db.close()
check("E DBA 偏好→卡片组合含 rds/redis（顺序在前）",
      [c["key"] for c in cl["params"]["cards"]][:2] == ["rds", "redis"]
      and all({"key", "label"} <= set(c) for c in cl["params"]["cards"]), str(cl)[:300])
ENV_CALLS.clear()
res = _execute(tid)
cards_now = json.loads(_settings.PRODUCT_CARDS)
check("E 执行→PRODUCT_CARDS 真改",
      res["status"] == "done" and [c[0] for c in cards_now] == [c["key"] for c in cl["params"]["cards"]]
      and len(ENV_CALLS) == 1, f"{res} {cards_now}")
rb = _rollback(tid)
cards_back = json.loads(_settings.PRODUCT_CARDS)
check("E 回滚→恢复原卡片组合（默认 9 张，ecs 打头）",
      rb.get("rolled_back") is True and len(cards_back) == 9 and cards_back[0][0] == "ecs",
      f"{rb} {cards_back}")

# ---------------- F 白名单红线 ----------------

print("== F 白名单红线 ==")
try:
    change_tools.build_ticket(SessionLocal(), 0, 0,
                              [{"tool": "exec_sql", "params": {"sql": "drop table users"}}],
                              STUB_ADMIN)
    check("F 白名单外工具拒建单", False)
except change_tools.ChangeToolError:
    check("F 白名单外工具拒建单", True)
try:
    change_tools.build_ticket(SessionLocal(), 0, 0,
                              [{"tool": "platform_api_call",
                                "params": {"method": "DELETE", "path": "/api/users/1"}}],
                              STUB_ADMIN)
    check("F 黑名单路径拒建单（删除类恒拒）", False)
except change_tools.ChangeToolError as e:
    check("F 黑名单路径拒建单（删除类恒拒）", "删除类" in str(e), str(e))
tools_seen = set()
for key in IMPROVE_KEYS:
    rr = wf.run_workflow(SessionLocal(), key, {
        "topology_refine": {"feedback": "野域名 www.topo-test.com 忽略"},
        "alert_tune": {"days": 30},
        "inspection_custom": {"requirement": "闲置阈值 10%"},
        "dashboard_custom": {"cards": "ecs,rds"},
    }[key], {"user": "tester", "user_id": 1})
    for tk in rr.get("tickets") or []:
        for c in json.loads(
                SessionLocal().get(AgentChangeTicket, tk["ticket_id"]).change_list_json):
            tools_seen.add(c["tool"])
check("F 四工作流产出工具全在白名单内", tools_seen <= set(change_tools.tool_names()),
      str(tools_seen - set(change_tools.tool_names())))

# ---------------- G planner ----------------

print("== G planner 命中/回退 ==")
p = planner_mod.plan(None, "拓扑重画一下 www.a.com 的后端不对", "change")
check("G topology_refine 命中+target 抽取+feedback 透传",
      p["mode"] == "workflow" and p["workflow_key"] == "topology_refine"
      and p.get("params", {}).get("target") == "www.a.com"
      and p.get("params", {}).get("feedback"), str(p))
p = planner_mod.plan(None, "野域名 cdn.x.com 忽略掉", "change")
check("G 野域名反馈命中 topology_refine",
      p["mode"] == "workflow" and p["workflow_key"] == "topology_refine", str(p))
p = planner_mod.plan(None, "帮我做告警降噪调参", "change")
check("G alert_tune 命中", p["mode"] == "workflow" and p["workflow_key"] == "alert_tune", str(p))
p = planner_mod.plan(None, "定制巡检规则，磁盘预警 75%", "change")
check("G inspection_custom 命中+requirement 透传",
      p["mode"] == "workflow" and p["workflow_key"] == "inspection_custom"
      and p.get("params", {}).get("requirement"), str(p))
p = planner_mod.plan(None, "仪表盘定制一下", "change")
check("G dashboard_custom 命中", p["mode"] == "workflow"
      and p["workflow_key"] == "dashboard_custom", str(p))
p = planner_mod.plan(None, "告警调参和巡检定制", "change")
check("G 同分并列回退 tools", p["mode"] == "tools" and p["workflow_key"] is None
      and ("同分" in p["reason"] or "多个工作流" in p["reason"]), str(p))
p = planner_mod.plan(None, "看下拓扑", "query")
check("G 弱信号回退 tools", p["mode"] == "tools", str(p))
p = planner_mod.plan(None, "你好", "smalltalk")
check("G 闲聊回退 tools", p["mode"] == "tools", str(p))

print(f"\n== 结果: {len(PASS)} 通过 / {len(FAIL)} 失败 ==")
if FAIL:
    print("失败项: " + " | ".join(FAIL))
    raise SystemExit(1)
raise SystemExit(0)
