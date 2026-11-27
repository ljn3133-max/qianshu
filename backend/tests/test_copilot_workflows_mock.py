#!/usr/bin/env python3
"""千枢智能体工作流 mock 测试：引擎 + 8 内置工作流 + planner + API 全链路。

运行：cd /opt/qianshu/backend && python3 tests/test_copilot_workflows_mock.py
隔离性：DATABASE_URL/MASTER_KEY 在 import app.* 之前改写为临时值——不碰真实 qianshu.db；
工具取数走假工具（monkeypatch tools_read.execute），云账号只出受保护表单或既有 Run 引用，
零外部网络（探活打 127.0.0.1:1 秒拒）。

验证项：
  A 工作流注册：13 个 key 齐全（含改进域⑨-⑫ + 引导域 onboard_init），契约字段齐全，域分布正确
  B 参数 schema 校验：缺必填/枚举外/超上限 -> ParamError；类型强转（"45"->45）；默认值生效
  C ⑤成本洞察：与 C1 同账号同窗口比较完整响应、金额、状态、三个答案和证据；无旧工具/采购建议
  D ⑧变更影响：假工具 + 真漂移事件 -> ±24h 时间窗对齐出 pair
  E ①接入云账号：schema 不收凭证，新账号只出受保护表单（ack_required），不建账号或工单；
    占位/秘密文本不回显；既有账号返回真实 Run task_ref、对象/时间/证据深链，无 Run 不伪造引用
  F planner：强短语命中/参数抽取/同分回退/弱信号回退/闲聊回退
  G API：清单/run/400/404/runs 记录/权限（viewer 只读、operator 可跑）
  H 8 工作流全量真跑一遍（假工具+本地秒拒探活）：全部出 artifact，运行记录落库
"""
import asyncio
import copy
import json
import os
import sys
import tempfile
import threading
import time
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

# ---- 隔离环境必须先于 app 导入设置（env 覆盖 .env）----
_TMPDIR = tempfile.mkdtemp(prefix="qianshu-wf-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import reset_pk_sequences, test_database_url
except ImportError:
    from pg_test_env import reset_pk_sequences, test_database_url
os.environ["DATABASE_URL"] = test_database_url("wf")
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
from app.models.agent_change import AgentWorkflowRun  # noqa: E402  create_all 前注册表
from app.models.cost_anomaly import CostAnomaly  # noqa: E402
from app.models.cost_daily import CostDaily  # noqa: E402
from app.models.cloud_account import CloudAccount  # noqa: E402
from app.services.cost_insights import get_cost_insights  # noqa: E402

from app.services.copilot import planner as planner_mod  # noqa: E402
from app.services.copilot import tools_read  # noqa: E402
from app.services.copilot import workflows as wf  # noqa: E402
from app.services.copilot.workflows import analysis as analysis_wf  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


# ---------------- 假工具（monkeypatch tools_read.execute） ----------------

_NOW = datetime.now()
_NOW_STR = _NOW.strftime("%m-%d %H:%M")
_TODAY = date.today()
FAKE_CALLS: list = []


def fake_execute(db, name, params):
    FAKE_CALLS.append((name, dict(params or {})))
    if name == "query_cost":
        return {"items": [{"product": "cdn", "name": "CDN", "amount": 90.0},
                          {"product": "oss", "name": "OSS", "amount": 60.0}],
                "total": 2, "days": params.get("days")}
    if name == "pack_pools":
        return {"items": [{"type_label": "CDN 流量包", "commodity_code": "cdnpack",
                           "pack_count": 2, "remaining": "50GB", "remaining_pct": 12.5}]}
    if name == "cost_anomaly_list":
        return {"items": [{"type": "pack_exhausting", "level": "warning", "status": "open",
                           "title": "CDN 流量池将耗尽", "value": 1.0,
                           "last_seen": _TODAY.isoformat()}]}
    if name == "recent_changes":
        return {"items": [{"time": _NOW_STR, "service": "ECS", "event": "ModifyInstanceAttribute",
                           "user": "ops", "resource": "i-abc", "summary": "改了安全组", "result": "成功"}]}
    if name == "query_alert_history":
        return {"items": [{"fired_at": _NOW_STR, "rule_name": "cpu-rule", "level": "critical",
                           "status": "firing", "target": "i-abc", "breach_value": 90,
                           "title": "CPU 高"}]}
    if name == "find_domain":
        return {"items": [{"host": "www.example.com", "cnt_5xx": 12, "req_count": 1000,
                           "chains": ["外层SLB"], "backends": ["ecs-1"], "rt_avg_ms": 120,
                           "app_name": "www.example.com", "note": "近24h请求 1,000，5xx 12 条"}]}
    if name == "object_chain":
        return {"items": [{"layer": "slb", "note": "fake"}], "note": "假对象因果链"}
    if name == "query_metric":
        return {"items": [{"instance_id": "i-abc", "name": "node1", "region": "cn-hangzhou",
                           "disk_max_7d": 88.0, "unit": "%"}]}
    if name == "expiry_list":
        return {"items": [{"name": "www.example.com", "category": "ssl_cert",
                           "category_label": "SSL证书", "days_left": 5}]}
    if name == "rds_list":
        return {"items": [{"instance_id": "rm-1", "name": "db1", "status": "Running"}]}
    if name == "dba_space_trend":
        return {"items": [{"date": _TODAY.isoformat(), "instance": "db1",
                           "data_gb": 100, "log_gb": 10, "backup_gb": 30, "disk_used_gb": 120}],
                "net_growth_gb_per_day": 3.2, "disk_growth_gb_14d": 44.8, "days_to_full": 20,
                "growth_attribution": "净增长以 data 日增量中位数为准：+3.20GB/天"}
    return {"items": []}


tools_read.execute = fake_execute

# ---------------- 种子（假数据落真 ORM，验证三口径/漂移对齐） ----------------

from app.main import app  # noqa: E402  全量注册模型/路由后再建表（cloud_accounts/link_edges 等齐全）

Base.metadata.create_all(db_engine)
_db = SessionLocal()
_db.add_all([
    CloudAccount(id=1, name="成本测试账号", vendor="aliyun", enabled=True, status="ok"),
    CostDaily(account_id=1, stat_date=_TODAY - timedelta(days=3), source="amortized",
              product_code="cdn", product_name="CDN", instance_id="cdn-1",
              subscription_type="Subscription",
              pretax_amount=100.0, pretax_gross_amount=120.0, payment_amount=0),
    CostDaily(account_id=1, stat_date=_TODAY - timedelta(days=2), source="bill",
              product_code="oss", product_name="OSS", instance_id="oss-1",
              subscription_type="PayAsYouGo",
              pretax_amount=50.0, pretax_gross_amount=60.0, payment_amount=0),
    CostAnomaly(detect_date=_TODAY, type="profile_drift", level="warning",
                target_key="cdn#top", title="CDN 域名构成漂移", content="cdn.example.com 进入 top3",
                value=1, last_seen=_TODAY, ack_status="open"),
])
_db.commit()
_db.close()
reset_pk_sequences(db_engine)  # 显式 id 造数后校准序列，后续 ORM 自增插入不撞主键

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

# ---------------- A 工作流注册 ----------------

print("== A 工作流注册 ==")
EXPECTED = {"access_cloud", "access_datasource", "access_k8s", "access_virt",
            "cost_checkup", "fault_diagnosis", "capacity_checkup", "change_impact",
            "topology_refine", "alert_tune", "inspection_custom", "dashboard_custom",
            "onboard_init"}
check("A 13 个工作流 key 齐全", set(wf.WORKFLOWS) == EXPECTED, str(set(wf.WORKFLOWS) ^ EXPECTED))
# 契约：name/domain/description 双语载体 {"zh","en"} 双语齐全
fields_ok = all(
    isinstance(wf.WORKFLOWS[k].get("name"), dict) and wf.WORKFLOWS[k]["name"].get("zh") and wf.WORKFLOWS[k]["name"].get("en")
    and wf.WORKFLOWS[k].get("domain", {}).get("zh") in ("接入域", "分析域", "改进域", "引导域")
    and wf.WORKFLOWS[k].get("domain", {}).get("en")
    and wf.WORKFLOWS[k].get("description", {}).get("zh") and wf.WORKFLOWS[k].get("description", {}).get("en")
    and isinstance(wf.WORKFLOWS[k].get("params_schema"), dict)
    for k in EXPECTED)
check("A 契约字段齐全（name/domain/description 双语 + params_schema）", fields_ok)
check("A 域分布 4+4+4+1",
      sum(1 for k in EXPECTED if wf.WORKFLOWS[k]["domain"]["zh"] == "接入域") == 4
      and sum(1 for k in EXPECTED if wf.WORKFLOWS[k]["domain"]["zh"] == "分析域") == 4
      and sum(1 for k in EXPECTED if wf.WORKFLOWS[k]["domain"]["zh"] == "改进域") == 4
      and sum(1 for k in EXPECTED if wf.WORKFLOWS[k]["domain"]["zh"] == "引导域") == 1)
try:
    wf.run_workflow(SessionLocal(), "no_such_wf", {}, {})
    check("A 未知 key 抛 WorkflowNotFound", False)
except wf.WorkflowNotFound:
    check("A 未知 key 抛 WorkflowNotFound", True)
except Exception as e:  # noqa: BLE001
    check("A 未知 key 抛 WorkflowNotFound", False, f"{type(e).__name__}: {e}")

# ---------------- B 参数 schema 校验 ----------------

print("== B 参数 schema 校验 ==")
try:
    wf.run_workflow(SessionLocal(), "access_cloud", {"vendor": "aliyun"}, {})
    check("B 缺必填抛 ParamError", False)
except wf.ParamError:
    check("B 缺必填抛 ParamError", True)
except Exception as e:  # noqa: BLE001
    check("B 缺必填抛 ParamError", False, f"{type(e).__name__}: {e}")
try:
    wf.run_workflow(SessionLocal(), "access_cloud", {"vendor": "badcloud", "name": "x"}, {})
    check("B 枚举外抛 ParamError", False)
except wf.ParamError:
    check("B 枚举外抛 ParamError", True)
except Exception as e:  # noqa: BLE001
    check("B 枚举外抛 ParamError", False, f"{type(e).__name__}: {e}")
try:
    wf.run_workflow(SessionLocal(), "cost_checkup", {"days": 999}, {})
    check("B 超上限抛 ParamError", False)
except wf.ParamError:
    check("B 超上限抛 ParamError", True)
except Exception as e:  # noqa: BLE001
    check("B 超上限抛 ParamError", False, f"{type(e).__name__}: {e}")
r = wf.run_workflow(SessionLocal(), "cost_checkup", {"days": "45"}, {"user": "tester"})
requested = (r.get("artifact", {}).get("insights") or {}).get("requested") or {}
check("B 类型强转 str->int + 生效", r["status"] == "done" and requested
      and (date.fromisoformat(requested["end"]) - date.fromisoformat(requested["start"])).days + 1 == 45,
      str(requested))
r = wf.run_workflow(SessionLocal(), "cost_checkup", {}, {"user": "tester"})
requested = r["artifact"]["insights"]["requested"]
check("B 缺省默认值 days=30 且截至昨日",
      (date.fromisoformat(requested["end"]) - date.fromisoformat(requested["start"])).days + 1 == 30
      and requested["end"] == (_TODAY - timedelta(days=1)).isoformat())

# ---------------- C ⑤成本体检 ----------------

print("== C ⑤成本体检（与 C1 同账号同窗口内容断言） ==")
FAKE_CALLS.clear()
window = {"account_id": 1, "start": (_TODAY - timedelta(days=10)).isoformat(),
          "end": (_TODAY - timedelta(days=1)).isoformat()}
r = wf.run_workflow(SessionLocal(), "cost_checkup", window,
                    {"user": "tester", "session_id": 7})
steps = r["steps"]
check("C status/steps 结构", r["status"] == "done" and len(steps) == 4
      and all({"step", "status", "detail", "ms"} <= set(s) and isinstance(s["ms"], int) for s in steps),
      str(steps)[:300])
check("C 步骤名按序", [s["step"] for s in steps] ==
      ["成本事实", "钱主要花在哪里", "哪里不正常", "现在先做什么"])
art = r["artifact"]
check("C artifact=三个答案+报告+要点", all(x in art.get("report_md", "") for x in
      ("钱主要花在哪里", "哪里不正常", "现在先做什么")) and len(art.get("highlights") or []) >= 3,
      str(art)[:300])
_db = SessionLocal()
expected = get_cost_insights(_db, 1, date.fromisoformat(window["start"]), date.fromisoformat(window["end"]))
_db.close()
check("C 与页面共用完整 C1 响应", art.get("insights") == expected
      and art["insights"]["requested"] == {"account_id": 1, "start": window["start"], "end": window["end"]},
      str(art.get("insights", {}).get("requested")))
answer = (art.get("answers") or [{}])[0]
check("C 金额/状态/排行与 C1 一致", answer.get("spend", {}).get("total") == 150.0
      and answer.get("status") == expected["accounts"][0]["status"]
      and answer["spend"]["top_product"] == expected["accounts"][0]["products"][0]
      and "¥150.00" in art.get("report_md", ""), str(answer)[:500])
check("C 结构化证据与历史窗口原样保留",
      answer.get("range") == expected["accounts"][0]["range"]
      and answer.get("coverage") == expected["accounts"][0]["coverage"]
      and answer.get("anomaly", {}).get("findings") == expected["accounts"][0]["anomalies"])
healthy = copy.deepcopy(expected)
healthy_item = healthy["accounts"][0]
healthy_item["anomalies"] = []
healthy_item["observations"] = []
healthy_item["recommendations"] = []
healthy_item["analysis"] = {"anomaly_status": "no_anomaly_in_verified_scope",
                            "recommendation_status": "insufficient_optimization_evidence"}
healthy_art = analysis_wf._cost_artifact(None, window, {"data": {"成本事实": healthy}})
day_refs = [ref for day in healthy_item["days"] for ref in day.get("evidence_refs", [])]
check("C 无异常建议时报告仍引用逐日账单证据", bool(day_refs)
      and all(ref in healthy_art["report_md"] for ref in day_refs)
      and "当前没有足够证据引用" not in healthy_art["report_md"])
recommendations = [rec for x in art.get("answers", []) for rec in x["action"]["recommendations"]]
check("C 无采购动作或估计节省", all(x.get("estimated_savings") is None for x in recommendations)
      and all(x.get("action") in {"investigate_cost_growth", "verify_first_observed_cost",
                                  "evaluate_sustained_payg"} for x in recommendations)
      and "提前续购" not in art.get("report_md", "") and "预计净节省" not in art.get("report_md", ""))
check("C 不再调用旧成本工具或采购建议", FAKE_CALLS == [], str(FAKE_CALLS))
check("C 分析域零变更工单", r["tickets"] == [])

# ---------------- D ⑧变更影响分析 ----------------

print("== D ⑧变更影响分析（假工具+真漂移事件） ==")
r = wf.run_workflow(SessionLocal(), "change_impact", {"hours": 48},
                    {"user": "tester", "session_id": 8})
check("D status/steps", r["status"] == "done" and len(r["steps"]) == 4, str(r["steps"])[:200])
art = r["artifact"]
pairs = (art.get("alignment") or {}).get("pairs") or []
check("D ±24h 时间窗对齐出 pair（变更×漂移）",
      any(p.get("event_type") == "profile_drift" for p in pairs)
      and any("ModifyInstanceAttribute" in (p.get("change") or "") for p in pairs),
      str(pairs)[:300])
check("D 报告含画像漂移与孤立口径", "画像漂移" in art.get("report_md", "")
      and "孤立" in art.get("report_md", ""))

# ---------------- E ①接入云账号（受保护表单 / 既有 Run） ----------------

print("== E ①接入云账号 受保护表单与任务引用 ==")
from app.models.agent_change import AgentChangeTicket  # noqa: E402
from app.services import onboarding  # noqa: E402
from app.services.copilot.workflows import access as access_wf  # noqa: E402

ACCESS_PARAMS = {"vendor": "aliyun", "name": "tc-prod", "region": "cn-hangzhou"}

# E1 新账号只返回受保护表单，不创建账号、工单或伪造 Run。
r = wf.run_workflow(SessionLocal(), "access_cloud", dict(ACCESS_PARAMS),
                    {"user": "tester", "user_id": 1, "session_id": 9})
by_step = {s["step"]: s for s in r["steps"]}
form = r["artifact"].get("credential_form") or {}
form_query = {"new": "1", "vendor": "aliyun", "name": "tc-prod", "region": "cn-hangzhou"}
form_href = urlsplit(form.get("href") or "")
check("E1 新账号返回受保护表单结构化引用",
      form.get("kind") == "cloud_credential_form" and form.get("ack_required") is True
      and form.get("route") == {"path": "/clouds", "query": form_query}
      and form_href.path == "/clouds"
      and parse_qs(form_href.query, keep_blank_values=True) == {k: [v] for k, v in form_query.items()}
      and "task_ref" not in r["artifact"], str(form))
form_step = access_wf._cloud_form_step(None, ACCESS_PARAMS, {"data": {"校验参数": {}}})
check("E1 表单步骤 data 与 artifact 一致，仍说明自动接入",
      form_step.get("data") == form and by_step["受保护凭证表单"]["status"] == "ok"
      and by_step["接入任务状态"]["status"] == "ok"
      and "无需再测试或创建同步工单" in by_step["接入任务状态"]["detail"])
_db = SessionLocal()
n_tickets = _db.query(AgentChangeTicket).count()
n_accounts = _db.query(CloudAccount).filter(CloudAccount.name == "tc-prod").count()
_db.close()
check("E1 智能体不创建建号/同步工单或账号", r["tickets"] == [] and n_tickets == 0 and n_accounts == 0,
      f"tickets={n_tickets}, accounts={n_accounts}")

# E2 schema 不再收集任何凭证，也不保留旧建单步骤。
schema = wf.WORKFLOWS["access_cloud"]["params_schema"]
check("E2 access_cloud schema 仅含厂商、账号名和地域",
      set(schema) == {"vendor", "name", "region"}
      and not ({"ak", "sk", "secret", "token", "password"} & set(schema))
      and "建变更工单" not in by_step, str(schema))

# E3 即使旧调用方额外传入占位值或秘密，也会被 schema 清洗，不进入任何出参。
secret_params = dict(ACCESS_PARAMS, name="tc-placeholder", ak="agent-pending-ak123",
                     sk="SKTESTSECRETDATA9999", token="TOKENTESTSECRET9999")
r_secret = wf.run_workflow(SessionLocal(), "access_cloud", secret_params,
                           {"user": "tester", "user_id": 1, "session_id": 9})
secret_out = json.dumps(r_secret, ensure_ascii=False)
check("E3 占位值与秘密文本不进入 steps/artifact/task_ref/tickets",
      all(value not in secret_out for value in
          ("agent-pending-ak123", "SKTESTSECRETDATA9999", "TOKENTESTSECRET9999"))
      and r_secret["tickets"] == [] and "task_ref" not in r_secret["artifact"]
      and not ({"ak", "sk", "secret", "token"} & set(
          r_secret["artifact"]["credential_form"]["route"]["query"])), secret_out[:400])

# E4 未填地域也显式携带空 region，不自行补默认地域或凭证字段。
r_no_region = wf.run_workflow(SessionLocal(), "access_cloud",
                              {"vendor": "aliyun", "name": "tc-no-region"}, {"user": "tester"})
no_region_form = r_no_region["artifact"]["credential_form"]
no_region_href = urlsplit(no_region_form["href"])
check("E4 表单深链显式携带空 region 且无 secret",
      no_region_form["route"]["query"] == {"new": "1", "vendor": "aliyun",
                                           "name": "tc-no-region", "region": ""}
      and parse_qs(no_region_href.query, keep_blank_values=True)["region"] == [""]
      and "secret" not in no_region_href.query and "task_ref" not in r_no_region["artifact"])

# E5 重复运行只恢复同一表单引用，不产生重复账号或工单。
r_repeat = wf.run_workflow(SessionLocal(), "access_cloud", dict(ACCESS_PARAMS), {"user": "tester"})
_db = SessionLocal()
n_tickets = _db.query(AgentChangeTicket).count()
n_accounts = _db.query(CloudAccount).filter(CloudAccount.name == "tc-prod").count()
_db.close()
check("E5 重复运行幂等返回表单且不建单", r_repeat["artifact"]["credential_form"] == form
      and r_repeat["tickets"] == [] and n_tickets == 0 and n_accounts == 0)

# E6 既有账号 + Run：任务引用只能来自该 Run 的真实能力证据，且可深链回同一账号。

checked_at = datetime(2026, 11, 17, 9, 30, 0)
observed_until = datetime(2026, 11, 17, 9, 29, 45)
evidence_ref = "metrics:readback:u1-mock"
_db = SessionLocal()
existing_account = CloudAccount(name="tc-existing-run", vendor="aliyun",
                                region="cn-mock", status="ok", enabled=True)
_db.add(existing_account)
_db.flush()
existing_run = onboarding.ensure_run(_db, existing_account)
onboarding.record_capability(
    _db, existing_account, "metrics", scope={"region": "cn-mock"},
    execution="succeeded", data_state="available",
    coverage={"discovered": 1, "checked": 1, "failed": 0, "unknown": 0,
              "total_known": True, "truncated": False},
    checked_at=checked_at, observed_until=observed_until,
    evidence_refs=[evidence_ref], reason_code="metrics_ready")
_db.commit()
existing_account_id, existing_run_id = existing_account.id, existing_run.id
_db.close()

existing_params = dict(ACCESS_PARAMS, name="tc-existing-run")
r = wf.run_workflow(SessionLocal(), "access_cloud", existing_params,
                    {"user": "tester", "user_id": 1, "session_id": 9})
by_step = {s["step"]: s for s in r["steps"]}
check("E6 既有账号只读 Run，不重新收集凭证或建单",
      r["status"] == "done" and r["tickets"] == []
      and by_step["受保护凭证表单"]["status"] == "skipped"
      and "无需重新提交凭证" in by_step["受保护凭证表单"]["detail"]
      and "credential_form" not in r["artifact"]
      and "凭证已收进工单" not in json.dumps(r["artifact"], ensure_ascii=False), str(by_step)[:400])
task_ref = r["artifact"].get("task_ref") or {}
check("E6 artifact 结构化 task_ref 指向真实账号与 Run",
      task_ref.get("kind") == "onboarding_run"
      and task_ref.get("account_id") == existing_account_id
      and task_ref.get("run_id") == existing_run_id
      and task_ref.get("object") == {"kind": "cloud_account", "id": existing_account_id,
                                     "name": "tc-existing-run", "vendor": "aliyun"},
      str(task_ref)[:500])
_db = SessionLocal()
step_result = access_wf._cloud_run_status(
    _db, existing_params, {"data": {"校验参数": {"existing_account": True}}})
_db.close()
check("E6 接入步骤 data 与 artifact 返回同一 task_ref",
      step_result.get("data", {}).get("task_ref") == task_ref)
capabilities = task_ref.get("capabilities") or []
check("E6 仅引用有证据的 capability，时间与证据原样保留",
      len(capabilities) == 1 and capabilities[0].get("capability") == "metrics"
      and capabilities[0].get("scope", {}).get("region") == "cn-mock"
      and capabilities[0].get("time_range") == {
          "checked_at": checked_at.isoformat(timespec="seconds"),
          "observed_from": None,
          "observed_until": observed_until.isoformat(timespec="seconds")}
      and capabilities[0].get("evidence_refs") == [evidence_ref]
      and task_ref.get("evidence_refs") == [evidence_ref], str(capabilities))
route = task_ref.get("route") or {}
href = urlsplit(task_ref.get("href") or "")
href_query = parse_qs(href.query, keep_blank_values=True)
expected_query = {"account_id": str(existing_account_id), "run_id": str(existing_run_id),
                  "object": "tc-existing-run", "object_kind": "cloud_account",
                  "object_id": str(existing_account_id), "evidence": evidence_ref,
                  "to": observed_until.isoformat(timespec="seconds")}
check("E6 /clouds 深链携带真实观测截止，不用采集时间伪造观测起点",
      route.get("path") == "/clouds" and route.get("query") == expected_query
      and href.path == "/clouds"
      and all(href_query.get(key) == [value] for key, value in expected_query.items()),
      str(task_ref.get("href")))
leak_src = json.dumps({"steps": r["steps"], "artifact": r["artifact"],
                       "step_data": step_result.get("data")}, ensure_ascii=False)
check("E6 凭证字段不进入 steps/artifact/task_ref",
      not ({"ak", "sk", "secret", "token", "password"} & set(task_ref))
      and not ({"ak", "sk", "secret", "token", "password"} & set(task_ref["route"]["query"]))
      and "AKEXISTINGRUN12345" not in leak_src and "SKEXISTINGRUNSECRET9876" not in leak_src)

# E7 既有账号但没有 Run：可以说明缺口，但不得伪造 task_ref。
_db = SessionLocal()
no_run_account = CloudAccount(name="tc-existing-no-run", vendor="aliyun",
                              status="untested", enabled=True)
_db.add(no_run_account)
_db.commit()
_db.close()
no_run_params = dict(ACCESS_PARAMS, name="tc-existing-no-run")
r_no_run = wf.run_workflow(SessionLocal(), "access_cloud", no_run_params,
                           {"user": "tester", "user_id": 1, "session_id": 9})
_db = SessionLocal()
no_run_step = access_wf._cloud_run_status(
    _db, no_run_params, {"data": {"校验参数": {"existing_account": True}}})
_db.close()
check("E7 无 Run 时步骤与 artifact 均无 task_ref",
      "task_ref" not in r_no_run["artifact"]
      and r_no_run["artifact"].get("onboarding_run") is None
      and "credential_form" not in r_no_run["artifact"]
      and "task_ref" not in no_run_step.get("data", {})
      and "还没有接入任务" in no_run_step.get("detail", ""), str(r_no_run["artifact"])[:400])
no_run_leak_src = json.dumps({"steps": r_no_run["steps"], "artifact": r_no_run["artifact"],
                              "step_data": no_run_step.get("data")}, ensure_ascii=False)
check("E7 无 Run 分支仍不泄露凭证",
      "AKNORUN123456789" not in no_run_leak_src and "SKNORUNSECRET123456" not in no_run_leak_src)

# E8 有 Run 但没有任何能力证据：保留任务身份，不编造 capability、证据或时间窗。
_db = SessionLocal()
empty_evidence_account = CloudAccount(name="tc-empty-evidence", vendor="aliyun",
                                      status="untested", enabled=True)
_db.add(empty_evidence_account)
_db.flush()
empty_evidence_run = onboarding.ensure_run(_db, empty_evidence_account)
_db.commit()
empty_evidence_run_id = empty_evidence_run.id
_db.close()
r_empty = wf.run_workflow(SessionLocal(), "access_cloud",
                          dict(ACCESS_PARAMS, name="tc-empty-evidence"), {"user": "tester"})
empty_ref = r_empty["artifact"].get("task_ref") or {}
empty_query = empty_ref.get("route", {}).get("query") or {}
empty_href_query = parse_qs(urlsplit(empty_ref.get("href") or "").query, keep_blank_values=True)
check("E8 零证据 Run 不编造 capability、evidence 或 from/to",
      empty_ref.get("run_id") == empty_evidence_run_id
      and empty_ref.get("capabilities") == [] and empty_ref.get("evidence_refs") == []
      and "evidence" not in empty_query and "from" not in empty_query and "to" not in empty_query
      and "from" not in empty_href_query and "to" not in empty_href_query
      and empty_query.get("object_kind") == "cloud_account"
      and empty_query.get("object_id") == str(empty_ref.get("account_id")), str(empty_ref))

# ---------------- F planner ----------------

print("== F planner 匹配/回退 ==")
p = planner_mod.plan(None, "帮我做个成本体检", "analyze")
check("F 强短语命中 cost_checkup（含 loop 兼容别名 workflow）",
      p["mode"] == "workflow" and p["workflow_key"] == "cost_checkup"
      and p.get("workflow") == "cost_checkup", str(p))
p = planner_mod.plan(None, "近45天成本体检", "analyze")
check("F 参数抽取 days", p.get("params", {}).get("days") == 45, str(p))
p = planner_mod.plan(None, "帮我接入云账号", "change")
check("F 接入云账号命中", p["mode"] == "workflow" and p["workflow_key"] == "access_cloud", str(p))
p = planner_mod.plan(None, "诊断一下 www.example.com", "analyze")
check("F 故障诊断命中+target 抽取", p["mode"] == "workflow"
      and p["workflow_key"] == "fault_diagnosis"
      and p.get("params", {}).get("target") == "www.example.com", str(p))
p = planner_mod.plan(None, "变更影响分析一下这次发版", "analyze")
check("F 变更影响命中", p["mode"] == "workflow" and p["workflow_key"] == "change_impact", str(p))
p = planner_mod.plan(None, "帮我做成本和容量体检", "analyze")
check("F 多工作流点名回退 tools", p["mode"] == "tools" and p["workflow_key"] is None
      and "多个工作流" in p["reason"], str(p))
p = planner_mod.plan(None, "成本体检容量体检", "analyze")
check("F 同分歧义回退 tools", p["mode"] == "tools" and p["workflow_key"] is None
      and "同分" in p["reason"], str(p))
p = planner_mod.plan(None, "成本为什么涨了", "analyze")
check("F 弱信号回退 tools", p["mode"] == "tools" and "弱信号" in p["reason"], str(p))
p = planner_mod.plan(None, "查一下现在有多少台机器", "query")
check("F 无命中回退 tools", p["mode"] == "tools", str(p))
p = planner_mod.plan(None, "你好", "smalltalk")
check("F 闲聊回退 tools", p["mode"] == "tools" and "闲聊" in p["reason"], str(p))
p = planner_mod.plan(None, "接入k8s集群", "change")
check("F K8s 接入命中", p["mode"] == "workflow" and p["workflow_key"] == "access_k8s", str(p))
p = planner_mod.plan(None, "接入vcenter", "change")
check("F 虚拟化接入命中", p["mode"] == "workflow" and p["workflow_key"] == "access_virt", str(p))
p = planner_mod.plan(None, "容量体检", "analyze")
check("F 容量体检命中", p["mode"] == "workflow" and p["workflow_key"] == "capacity_checkup", str(p))

# ---------------- G API ----------------

print("== G 工作流 API（TestClient，不碰 8810） ==")
r = client.get("/api/agent/workflows")
data = r.json()
check("G GET /workflows 清单", r.status_code == 200 and len(data.get("items") or []) == 13
      and all({"key", "name", "domain", "description", "last_run"} <= set(i) for i in data["items"]),
      r.text[:300])
r = client.post("/api/agent/workflows/cost_checkup/run", json={"params": {"days": 10}})
d = r.json()
check("G POST run 成功", r.status_code == 200 and d.get("run_id")
      and d.get("status") == "done" and len(d.get("steps") or []) == 4
      and d.get("artifact", {}).get("report_md") and isinstance(d.get("tickets"), list),
      r.text[:300])
run_id = d["run_id"]
r = client.post("/api/agent/workflows/cost_checkup/run", json={"params": {"days": 999}})
check("G 参数不过 400", r.status_code == 400, r.text[:200])
r = client.post("/api/agent/workflows/no_such/run", json={"params": {}})
check("G 未知工作流 404", r.status_code == 404, r.text[:200])
r = client.get("/api/agent/workflows/runs?limit=5")
items = r.json().get("items") or []
check("G GET /workflows/runs 记录", r.status_code == 200 and items
      and items[0]["id"] == run_id and items[0]["workflow_key"] == "cost_checkup"
      and items[0]["artifact"].get("highlights"), r.text[:300])

session_resp = client.post("/api/agent/sessions", json={"title": "成本页同窗口验证"})
session_id = session_resp.json().get("id")
linked_resp = client.post("/api/agent/workflows/cost_checkup/run",
                          json={"params": window, "session_id": session_id})
linked = linked_resp.json()
messages_resp = client.get(f"/api/agent/sessions/{session_id}/messages")
workflow_messages = [m for m in (messages_resp.json().get("items") or [])
                     if m.get("msg_kind") == "workflow_run" and m.get("run_id") == linked.get("run_id")]
check("G 成本页结构化运行可从会话恢复原范围报告", session_resp.status_code == 200
      and linked_resp.status_code == 200 and messages_resp.status_code == 200
      and len(workflow_messages) == 1
      and (workflow_messages[0].get("artifact", {}).get("insights", {}).get("requested")
           == {"account_id": 1, "start": window["start"], "end": window["end"]}),
      f"session={session_resp.status_code} run={linked_resp.status_code} "
      f"messages={messages_resp.status_code} cards={len(workflow_messages)}")

STUB_USER.role = "viewer"
r1 = client.get("/api/agent/workflows")
r2 = client.post("/api/agent/workflows/cost_checkup/run", json={"params": {}})
check("G viewer：读 200 / run 403", r1.status_code == 200 and r2.status_code == 403,
      f"{r1.status_code}/{r2.status_code}")
STUB_USER.role = "operator"
r = client.post("/api/agent/workflows/cost_checkup/run", json={"params": {"days": 5}})
check("G operator 可 run", r.status_code == 200, r.text[:200])
STUB_USER.role = "admin"

# loop 兼容契约（loop._try_workflow 三参调用 + content_md/name 轨迹字段）
r3 = wf.run_workflow(SessionLocal(), "cost_checkup", {"days": 5})
check("G loop 兼容：三参 run_workflow + content_md/name 字段",
      isinstance(r3, dict) and r3.get("content_md") and "成本洞察" in r3["content_md"]
      and all(s.get("name") == s.get("step") for s in r3["steps"])
      and r3.get("change_items") == [], str(r3)[:200])

# ---------------- H 8 工作流全量真跑 + 运行记录落库 ----------------

print("== H 8 工作流全量真跑（真变更层+假工具+秒拒探活） ==")
_SMOKE_PARAMS = {
    "access_cloud": dict(ACCESS_PARAMS),
    "access_datasource": {"type": "victoriametrics", "name": "vm-test", "url": "http://127.0.0.1:1"},
    "access_k8s": {"name": "k8s-test", "api_url": "https://127.0.0.1:1", "token": "TOKENTEST12345678"},
    "access_virt": {"kind": "esxi", "host": "127.0.0.1:1", "name": "esxi-test"},
    "cost_checkup": {"days": 10},
    "fault_diagnosis": {"target": "www.example.com", "window": "24h"},
    "capacity_checkup": {},
    "change_impact": {"hours": 48},
}
smoke_ok = True
for key, params in _SMOKE_PARAMS.items():
    rr = wf.run_workflow(SessionLocal(), key, params, {"user": "tester", "session_id": 1})
    spec_steps = wf.WORKFLOWS[key]["steps"]
    ok = (rr["status"] == "done" and len(rr["steps"]) == len(spec_steps)
          and rr["artifact"].get("report_md") and rr["artifact"].get("highlights"))
    smoke_ok = smoke_ok and ok
    print(f"    - {key}: {rr['status']} steps={len(rr['steps'])}/{len(spec_steps)}"
          f" artifact={'有' if rr['artifact'].get('report_md') else '无'}")
check("H 8 工作流全量跑通出 artifact", smoke_ok)

_db = SessionLocal()
n_runs = _db.query(AgentWorkflowRun).count()
parsed_ok = True
for row in _db.query(AgentWorkflowRun).all():
    try:
        json.loads(row.steps_json or "[]")
        json.loads(row.artifact_json or "{}")
        if row.status not in ("done", "failed", "running"):
            parsed_ok = False
    except ValueError:
        parsed_ok = False
check("H 运行记录落库（steps_json/artifact_json 可解析）",
      n_runs >= 13 and parsed_ok, f"runs={n_runs}")   # B:2 C:1 D:1 E:2 G:3 H:8 -> 至少 13 未计入 B 前
_db.close()

print(f"\n== 结果: {len(PASS)} 通过 / {len(FAIL)} 失败 ==")
if FAIL:
    print("失败项: " + " | ".join(FAIL))
    raise SystemExit(1)
raise SystemExit(0)
