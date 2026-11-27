#!/usr/bin/env python3
"""平台自愈 P0：问题单去重/风暴/复发/状态机/独立事务/启动收口 mock 测试。

运行：cd backend && QIANSHU_TEST_PG_URL=... python3 tests/test_self_heal_service_mock.py
隔离性：独立一次性 PostgreSQL；不访问云、不发外部通知、不启 copilotd。
"""
import importlib
import os
import pkgutil
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

try:
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url

os.environ["DATABASE_URL"] = test_database_url("selfheal_svc")
os.environ["MASTER_KEY"] = "ab" * 32
os.environ["EVENT_WEBHOOK_TOKEN"] = "test-token"
os.environ["METRIC_SOURCES"] = "[]"
os.environ["LOG_SOURCES"] = "[]"
os.environ["VM_URL"] = "http://127.0.0.1:1"
os.environ["SELF_HEAL_ENABLED"] = "true"
os.environ["SELF_HEAL_MODE"] = "observe"
os.environ["SELF_HEAL_LLM_ENABLED"] = "false"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings  # noqa: E402
from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.core.migrate import migrate_self_heal_indexes  # noqa: E402
from app.core.security import hash_password  # noqa: E402

import app.models as _models_pkg  # noqa: E402
for _m in pkgutil.iter_modules(_models_pkg.__path__):
    importlib.import_module(f"app.models.{_m.name}")

from app.models.in_app_message import InAppMessage  # noqa: E402
from app.models.issue import IssueCase, IssueEvent, IssuePlan  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.self_heal import service, verifier  # noqa: E402

Base.metadata.create_all(engine)
migrate_self_heal_indexes(engine)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


db = SessionLocal()
admin = User(username="sh_admin", password_hash=hash_password("SynthPass!234"), role="admin",
             is_active=True, must_change_password=False)
db.add(admin)
db.commit()
ADMIN_ID = admin.id
db.close()


def make_issue(**kw):
    now = datetime.now()
    defaults = dict(fingerprint=kw.pop("fingerprint"), source="onboarding",
                    account_id=0, run_id=0, capability="", severity="warning",
                    status="open", title="测试问题", reason_code="test",
                    error_class="TestError", first_seen=now, last_seen=now,
                    occurrences=1, attempts=0, max_attempts=3)
    defaults.update(kw)
    s = SessionLocal()
    issue = IssueCase(**defaults)
    s.add(issue)
    s.commit()
    s.refresh(issue)
    s.expunge(issue)
    s.close()
    return issue


def make_plan(issue_id, *, risk="R0", status="proposed", recipe="reset_own_issue_state",
              version=1, result=None):
    s = SessionLocal()
    ph = f"hash-{issue_id}-{version}-{risk}-{status}"
    plan = IssuePlan(issue_id=issue_id, version=version, status=status, risk=risk,
                     plan_json=service._dumps({"steps": [{"recipe": recipe, "risk": risk,
                                                          "params": {}, "verify": {}}]}),
                     plan_hash=ph, expires_at=datetime.now() + timedelta(hours=1),
                     result_json=service._dumps(result or {}))
    s.add(plan)
    s.commit()
    s.refresh(plan)
    pid = plan.id
    s.close()
    return pid


# A 去重：同指纹只保留一条活动问题单，occurrences 累加，事件追加
r1 = service.report_issue(source="onboarding", account_id=11, capability="assets",
                          reason_code="asset_sync_failed", error="RemoteDisconnected invalid status",
                          title="资产同步失败")
r2 = service.report_issue(source="onboarding", account_id=11, capability="assets",
                          reason_code="asset_sync_failed", error="RemoteDisconnected invalid status",
                          title="资产同步失败")
check("A1 首次上报创建问题单", r1 is not None and r1.id > 0)
check("A2 同指纹合并为同一问题单", r2 is not None and r2.id == r1.id)
s = SessionLocal()
row = s.get(IssueCase, r1.id)
check("A3 occurrences 累加为 2", row.occurrences == 2, str(row.occurrences))
ev = s.query(IssueEvent).filter(IssueEvent.issue_id == r1.id,
                                IssueEvent.kind == "deduped").count()
check("A4 合并写 deduped 事件", ev == 1, str(ev))
msgs = s.query(InAppMessage).filter(InAppMessage.msg_type == "agent_issue",
                                    InAppMessage.ref_id == r1.id).count()
check("A5 只写站内信，不写外部渠道", msgs >= 1, str(msgs))
s.close()

# B 风暴：单来源每分钟超过阈值，合并为稳定风暴单，不再逐条建单
settings.SELF_HEAL_STORM_PER_MIN = 2
created = []
for i in range(5):
    created.append(service.report_issue(source="stormtest", title=f"风暴错误 {i}",
                                        reason_code=f"err_{i}", error=f"boom {i}"))
ids = {x.id for x in created if x is not None}
s = SessionLocal()
storms = (s.query(IssueCase)
          .filter(IssueCase.source == "stormtest",
                  IssueCase.reason_code == "error_storm").all())
check("B1 阈值前的单条 + 阈值后的风暴单各保留一条",
      len(ids) == 3 and len(storms) == 1, f"ids={ids} storms={len(storms)}")
storm = storms[0]
check("B2 风暴原因码固定", storm.reason_code == "error_storm", storm.reason_code)
check("B3 阈值后 3 次错误合并进风暴单", storm.occurrences == 3, str(storm.occurrences))
check("B4 风暴单不再自动诊断", storm.next_action == "manual_review", storm.next_action)
s.close()
settings.SELF_HEAL_STORM_PER_MIN = 20

# C 复发：恢复后同指纹再次出现 -> 新问题单 recurring + critical，停止自动执行
rec = service.report_issue(source="onboarding", account_id=12, capability="metrics",
                           reason_code="metrics_failed", error="read back failed",
                           title="指标失败")
s = SessionLocal()
svc = s.get(IssueCase, rec.id)
service.transition(s, svc, "recovered", kind="recovered", notify=False)
s.close()
rec2 = service.report_issue(source="onboarding", account_id=12, capability="metrics",
                            reason_code="metrics_failed", error="read back failed",
                            title="指标失败")
check("C1 复发创建新问题单", rec2.id != rec.id)
check("C2 复发状态 recurring", rec2.status == "recurring", rec2.status)
check("C3 复发严重度升级 critical", rec2.severity == "critical", rec2.severity)
check("C4 复发保留来源问题单 id", rec2.recurrence_of_id == rec.id, str(rec2.recurrence_of_id))

# D 独立事务：调用方业务回滚不影响已上报问题单
s = SessionLocal()
proof = service.report_issue(source="onboarding", account_id=13, capability="cost",
                             reason_code="cost_failed", error="cost failed",
                             title="账单失败")
s.rollback()
s.close()
s = SessionLocal()
check("D1 业务事务回滚后问题单仍在", s.get(IssueCase, proof.id) is not None)
s.close()

# E 启动收口：R2 executing 计划在重启后接续验证，不永久 repairing
i2 = make_issue(fingerprint="reconcile-r2", status="repairing", capability="platform")
p2 = make_plan(i2.id, risk="R2", status="executing", recipe="restart_platform")
with patch.object(verifier, "verify_issue", return_value=(True, "重启后健康检查通过")):
    out = service.reconcile_self_heal()
s = SessionLocal()
issue2 = s.get(IssueCase, i2.id)
plan2 = s.get(IssuePlan, p2)
check("E1 重启后 R2 计划标记成功", plan2.status == "succeeded", plan2.status)
check("E2 重启后验证通过问题恢复", issue2.status == "recovered", issue2.status)
check("E3 收口统计返回", out.get("recovered", 0) >= 1, str(out))
s.close()

# F 中断的 R0 executing 计划不重复执行，转人工
i3 = make_issue(fingerprint="reconcile-r0", status="repairing")
p3 = make_plan(i3.id, risk="R0", status="executing", recipe="reset_own_issue_state")
service.reconcile_self_heal()
s = SessionLocal()
plan3 = s.get(IssuePlan, p3)
issue3 = s.get(IssueCase, i3.id)
check("F1 中断 R0 计划标记失败", plan3.status == "failed", plan3.status)
check("F2 中断 R0 问题转人工", issue3.status == "manual_required", issue3.status)
check("F3 中断计划未被执行（保留 result 说明）", "interrupted" in (plan3.result_json or ""))
s.close()

# G 关闭总开关：不再上报（主业务不受影响）
settings.SELF_HEAL_ENABLED = False
off = service.report_issue(source="onboarding", title="关闭时不应上报")
check("G1 关闭总开关后返回 None", off is None)
settings.SELF_HEAL_ENABLED = True

# H report_issue_safe 吞掉异常，绝不反向阻断业务
with patch.object(service, "report_issue", side_effect=RuntimeError("boom")):
    try:
        service.report_issue_safe(source="x", title="x")
        safe_ok = True
    except Exception:
        safe_ok = False
check("H1 上报异常被 safe 包装吞掉", safe_ok)

# I 指纹稳定：相同输入跨调用一致，地址/UUID/时间戳归一
from app.services.self_heal import fingerprint as fp  # noqa: E402
a = fp.make_fingerprint(source="s", reason_code="r", error="boom at 0x7f1234 2026-01-02T03:04:05")
b = fp.make_fingerprint(source="s", reason_code="r", error="boom at 0x8e9999 2026-01-03T04:05:06")
check("I1 指纹抹掉地址/时间戳后稳定", a == b, f"{a[:8]} != {b[:8]}")

print(f"\n结果: {len(PASS)} 通过, {len(FAIL)} 失败")
if FAIL:
    print("失败项: " + ", ".join(FAIL))
    sys.exit(1)
