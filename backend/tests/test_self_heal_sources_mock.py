#!/usr/bin/env python3
"""平台自愈 P4：全平台错误源接入（onboarding/cloud/copilotd/API 5xx）与聚合。

运行：cd backend && QIANSHU_TEST_PG_URL=... python3 tests/test_self_heal_sources_mock.py
所有外部动作替身；5xx 探针只返回合成错误响应，不触发真实业务。
"""
import asyncio
import importlib
import os
import pkgutil
import sys
import time
from datetime import datetime
from unittest.mock import patch

try:
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url

os.environ["DATABASE_URL"] = test_database_url("selfheal_src")
os.environ["MASTER_KEY"] = "ab" * 32
os.environ["EVENT_WEBHOOK_TOKEN"] = "test-token"
os.environ["METRIC_SOURCES"] = "[]"
os.environ["LOG_SOURCES"] = "[]"
os.environ["VM_URL"] = "http://127.0.0.1:1"
os.environ["SELF_HEAL_ENABLED"] = "true"
os.environ["SELF_HEAL_MODE"] = "observe"
os.environ["SELF_HEAL_LLM_ENABLED"] = "false"
os.environ["COPILOTD_BRIDGE_TOKEN"] = "synthetic-copilotd-token"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.core.migrate import migrate_self_heal_indexes  # noqa: E402
from app.main import app  # noqa: E402

import app.models as _models_pkg  # noqa: E402
for _m in pkgutil.iter_modules(_models_pkg.__path__):
    importlib.import_module(f"app.models.{_m.name}")

from app.models.audit import AuditLog  # noqa: E402
from app.models.cloud_account import CloudAccount  # noqa: E402
from app.models.in_app_message import InAppMessage  # noqa: E402
from app.models.issue import IssueCase, IssueEvent  # noqa: E402
from app.services.cloud.sync_job import report_cloud_issue  # noqa: E402
from app.services.onboarding_tasks import _report_capability_issue  # noqa: E402
from app.services.self_heal.sources import (  # noqa: E402
    report_agent_failure, report_job_failure,
)

Base.metadata.create_all(engine)
migrate_self_heal_indexes(engine)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


s = SessionLocal()
acc = CloudAccount(name="源测试账号", vendor="aliyun", ak_cipher="", secret_cipher="")
s.add(acc)
s.commit()
ACC = acc.id
s.close()


def _req(method, url, **kw):
    async def _go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://testserver") as c:
            return await c.request(method, url, **kw)
    return asyncio.run(_go())


print("A 接入失败：聚合、非阻断、厂商不支持不建单")
for i in range(5):
    _report_capability_issue(acc, "assets", "asset_sync_failed",
                             "RemoteDisconnected synthetic", scope={"region": "cn-hz"})
s = SessionLocal()
rows = (s.query(IssueCase).filter(IssueCase.source == "onboarding",
                                  IssueCase.capability == "assets").all())
check("A1 5 次同因失败只建 1 个问题单", len(rows) == 1, str(len(rows)))
check("A2 发生次数累计 5", rows[0].occurrences == 5, str(rows[0].occurrences))
msgs = s.query(InAppMessage).filter(InAppMessage.msg_type == "agent_issue",
                                    InAppMessage.ref_id == rows[0].id).count()
check("A3 只发 1 条聚合站内信", msgs == 1, str(msgs))
check("A4 未自动诊断（观察模式）", rows[0].status == "open", rows[0].status)
s.close()
_report_capability_issue(acc, "logs", "vendor_not_supported", "not supported")
s = SessionLocal()
check("A5 厂商不支持不进入问题单",
      s.query(IssueCase).filter(IssueCase.capability == "logs").count() == 0)
s.close()
with patch("app.services.self_heal.service.report_issue", side_effect=RuntimeError("boom")):
    try:
        _report_capability_issue(acc, "metrics", "metrics_failed", "boom", retryable=True)
        safe = True
    except Exception:
        safe = False
check("A6 上报异常不阻断接入", safe is True)

print("B 云同步失败进入同一总线")
for _ in range(3):
    report_cloud_issue(ACC, product="ecs", reason_code="asset_sync_failed",
                       error="synthetic sync failure")
s = SessionLocal()
rows = s.query(IssueCase).filter(IssueCase.source == "cloud_sync").all()
check("B1 同产品同步失败聚合 1 条", len(rows) == 1, str(len(rows)))
check("B2 次数累计 3", rows and rows[0].occurrences == 3, str(rows and rows[0].occurrences))
check("B3 带账号与能力", rows and rows[0].account_id == ACC
      and rows[0].capability == "assets")

print("C copilotd 生命周期桥：白名单、聚合、恢复、审计、站内信")
HDR = {"X-Copilotd-Token": "synthetic-copilotd-token"}
r = _req("POST", "/api/agent/lifecycle/report", headers={"X-Copilotd-Token": "wrong"},
         json={"action": "copilotd.detect", "title": "x"})
check("C1 token 错误 403", r.status_code == 403, str(r.status_code))
r = _req("POST", "/api/agent/lifecycle/report", headers=HDR,
         json={"action": "copilotd.rm_rf", "title": "x"})
check("C2 非白名单动作 400", r.status_code == 400, r.text[:200])
r1 = _req("POST", "/api/agent/lifecycle/report", headers=HDR,
          json={"action": "copilotd.detect", "title": "后端健康检查失败",
                "content_md": "database ping failed", "level": "critical"})
r2 = _req("POST", "/api/agent/lifecycle/report", headers=HDR,
          json={"action": "copilotd.heal", "title": "开始自愈",
                "content_md": "restart planned", "level": "warning"})
check("C3 detect 上报成功", r1.status_code == 200 and r1.json().get("audit") is True)
check("C4 heal 上报成功", r2.status_code == 200)
s = SessionLocal()
rows = s.query(IssueCase).filter(IssueCase.source == "copilotd").all()
check("C5 copilotd 事件聚合为 1 个平台问题", len(rows) == 1, str(len(rows)))
issue = rows[0]
check("C6 严重度取首次事件 critical", issue.severity == "critical", issue.severity)
check("C7 两次事件都留时间线",
      s.query(IssueEvent).filter(IssueEvent.issue_id == issue.id,
                                 IssueEvent.kind.in_(("detected", "copilotd.heal"))).count() >= 2)
check("C8 落 copilotd 审计",
      s.query(AuditLog).filter(AuditLog.action.in_(
          ("copilotd.detect", "copilotd.heal"))).count() >= 2)
check("C9 站内信提示问题单",
      s.query(InAppMessage).filter(InAppMessage.msg_type == "agent_issue",
                                   InAppMessage.ref_id == issue.id).count() >= 1)
issue_id = issue.id
s.close()
r3 = _req("POST", "/api/agent/lifecycle/report", headers=HDR,
          json={"action": "copilotd.recovered", "title": "平台已恢复"})
check("C10 recovered 上报成功", r3.status_code == 200)
s = SessionLocal()
issue = s.get(IssueCase, issue_id)
check("C11 活动问题标记恢复", issue.status == "recovered", issue.status)
check("C12 恢复写解决说明", "copilotd" in (issue.resolution_md or ""))
s.close()
r4 = _req("POST", "/api/agent/lifecycle/report", headers=HDR,
          json={"action": "copilotd.detect", "title": "再次异常"})
s = SessionLocal()
rows = (s.query(IssueCase).filter(IssueCase.source == "copilotd")
        .order_by(IssueCase.id.asc()).all())
check("C13 恢复后再次异常新建复发单", len(rows) == 2 and rows[-1].status == "recurring",
      f"{len(rows)}/{rows[-1].status if rows else '-'}")
check("C14 复发单关联来源", rows[-1].recurrence_of_id == issue_id,
      str(rows[-1].recurrence_of_id))
s.close()

print("D API 5xx 中间件：上报但不改响应、不递归")
@app.get("/api/_selfheal_500_probe")
def _probe_500():
    return JSONResponse({"ok": False, "synthetic": True}, status_code=500)


with patch("app.services.self_heal.service.report_issue_safe") as report:
    r = _req("GET", "/api/_selfheal_500_probe")
    for _ in range(30):
        if report.call_count:
            break
        time.sleep(0.05)
    check("D1 5xx 响应原样返回", r.status_code == 500
          and r.json().get("synthetic") is True, r.text[:200])
    check("D2 5xx 上报一次", report.call_count == 1, str(report.call_count))
    kw = report.call_args.kwargs if report.call_args else {}
    check("D3 来源 api_5xx / 严重度 critical",
          kw.get("source") == "api_5xx" and kw.get("severity") == "critical", str(kw)[:200])
    check("D4 上报为不可自动执行", kw.get("retryable") is False)
    _req("GET", "/api/_selfheal_500_probe")
    time.sleep(0.2)
    check("D6 同一路径 5xx 风暴窗口内不重复建线程", report.call_count == 1,
          str(report.call_count))

settings.SELF_HEAL_ENABLED = False
with patch("app.services.self_heal.service.report_issue_safe") as report:
    r = _req("GET", "/api/_selfheal_500_probe")
    time.sleep(0.3)
    check("D5 总开关关闭后 5xx 不上报", report.call_count == 0, str(report.call_count))
settings.SELF_HEAL_ENABLED = True

print("E 后台任务与智能体工具失败：稳定聚合、来源可追踪")
report_job_failure("metrics_collect", ConnectionError("synthetic one"))
report_job_failure("metrics_collect", TimeoutError("synthetic two"))
s = SessionLocal()
rows = (s.query(IssueCase).filter(IssueCase.source == "job",
                                  IssueCase.reason_code == "job_failed").all())
check("E1 同一任务不同消息聚合 1 条", len(rows) == 1, str(len(rows)))
check("E2 发生次数累计 2", rows and rows[0].occurrences == 2,
      str(rows and rows[0].occurrences))
check("E3 任务名进入 source_ref/摘要",
      rows and '"job": "metrics_collect"' in (rows[0].source_ref_json or "")
      and "metrics_collect" in (rows[0].title or ""))
s.close()
report_agent_failure("platform_api_query", RuntimeError("synthetic adapter down"))
report_agent_failure("wf:access_daily", RuntimeError("synthetic adapter down"))
s = SessionLocal()
rows = (s.query(IssueCase).filter(IssueCase.source == "agent")
        .order_by(IssueCase.id.asc()).all())
check("E4 不同工具分别建单", len(rows) == 2, str(len(rows)))
check("E5 工具名可追踪", rows and any("platform_api_query" in (x.source_ref_json or "")
                                      for x in rows)
      and any("wf:access_daily" in (x.source_ref_json or "") for x in rows),
      str([x.source_ref_json for x in rows]))
s.close()

print(f"\n结果: {len(PASS)} 通过, {len(FAIL)} 失败")
if FAIL:
    print("失败项: " + ", ".join(FAIL))
    sys.exit(1)
