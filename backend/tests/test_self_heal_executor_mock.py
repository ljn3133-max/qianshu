#!/usr/bin/env python3
"""平台自愈 P2/P3 执行器：审批哈希/角色/模式/过期/并发、R0 自动、R2 委托重启、
R3 真实 change_gate+code_mod 补丁与回滚、失败转人工、熔断。

运行：cd backend && QIANSHU_TEST_PG_URL=... python3 tests/test_self_heal_executor_mock.py
外部动作全部替身；R3 只在 docs/tmp 合成文本文件上执行并回滚。
"""
import importlib
import json
import os
import pkgutil
import shutil
import sys
import threading
from datetime import datetime, timedelta
from unittest.mock import patch

try:
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url

os.environ["DATABASE_URL"] = test_database_url("selfheal_exec")
os.environ["MASTER_KEY"] = "ab" * 32
os.environ["EVENT_WEBHOOK_TOKEN"] = "test-token"
os.environ["METRIC_SOURCES"] = "[]"
os.environ["LOG_SOURCES"] = "[]"
os.environ["VM_URL"] = "http://127.0.0.1:1"
os.environ["SELF_HEAL_ENABLED"] = "true"
os.environ["SELF_HEAL_MODE"] = "approved_r3"
os.environ["SELF_HEAL_LLM_ENABLED"] = "false"
os.environ["SELF_HEAL_BREAKER_FAILS"] = "5"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings  # noqa: E402
from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.core.migrate import migrate_self_heal_indexes  # noqa: E402
from app.core.security import hash_password  # noqa: E402

import app.models as _models_pkg  # noqa: E402
for _m in pkgutil.iter_modules(_models_pkg.__path__):
    importlib.import_module(f"app.models.{_m.name}")

from app.models.cloud_account import CloudAccount  # noqa: E402
from app.models.issue import IssueCase, IssueEvent, IssuePlan  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.self_heal import code_repair, executor, service, validator, verifier  # noqa: E402

Base.metadata.create_all(engine)
migrate_self_heal_indexes(engine)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def expect_approval_error(name, fn, code):
    try:
        fn()
    except executor.PlanApprovalError as exc:
        check(name, exc.code == code, f"code={exc.code} {exc}")
        return
    check(name, False, "未抛出 PlanApprovalError")


s = SessionLocal()
admin = User(username="sh_exec_admin", password_hash=hash_password("SynthPass!234"),
             role="admin", is_active=True, must_change_password=False)
operator = User(username="sh_exec_op", password_hash=hash_password("SynthPass!234"),
                role="operator", is_active=True, must_change_password=False)
viewer = User(username="sh_exec_view", password_hash=hash_password("SynthPass!234"),
              role="viewer", is_active=True, must_change_password=False)
acct = CloudAccount(name="执行器测试账号", vendor="aliyun", ak_cipher="", secret_cipher="")
s.add_all([admin, operator, viewer, acct])
s.commit()
ADMIN = s.get(User, admin.id)
OP = s.get(User, operator.id)
VIEW = s.get(User, viewer.id)
ACC = acct.id
s.close()


def make_issue(**kw):
    now = datetime.now()
    defaults = dict(source="job", account_id=ACC, run_id=0, capability="",
                    severity="warning", status="repairing", title="平台任务失败",
                    reason_code="job_failed", error_class="RuntimeError",
                    occurrences=1, attempts=0, max_attempts=3, next_action="wait",
                    first_seen=now, last_seen=now)
    defaults.update(kw)
    defaults["fingerprint"] = kw.get("fingerprint") or f"exfp-{datetime.now().timestamp()}"
    db = SessionLocal()
    issue = IssueCase(**defaults)
    db.add(issue)
    db.commit()
    db.refresh(issue)
    db.expunge(issue)
    db.close()
    return issue


def make_plan(issue_id, steps, *, risk, status="proposed", expired=False,
              approved_by=None, approved_by_id=None, result=None):
    raw = {"summary": "测试计划", "root_cause": "测试根因", "impact": "测试影响",
           "confidence": 0.5, "can_auto_fix": True, "need_user": False,
           "steps": steps, "verify": "验证", "assist_steps": [], "questions": []}
    plan = validator.normalize_plan(raw)
    assert plan["risk"] == risk, (plan["risk"], risk)
    db = SessionLocal()
    version = service.new_plan_version(db, issue_id)
    row = IssuePlan(issue_id=issue_id, version=version,
                    status=status, risk=risk, plan_json=service._dumps(plan),
                    plan_hash=validator.plan_hash(plan, issue_id=issue_id, version=version),
                    expires_at=datetime.now() + (-timedelta(minutes=1) if expired
                                                 else timedelta(hours=1)),
                    approved_by=approved_by, approved_by_id=approved_by_id,
                    approved_at=datetime.now() if approved_by else None,
                    result_json=service._dumps(result or {}))
    db.add(row)
    db.commit()
    db.refresh(row)
    pid, phash = row.id, row.plan_hash
    db.close()
    return pid, phash


def step(recipe, params=None):
    return {"recipe": recipe, "params": params or {}}


# A 审批闸门：ack/哈希/过期/角色/模式/熔断
i1 = make_issue()
p1, h1 = make_plan(i1.id, [step("reset_own_issue_state", {"issue_id": i1.id})], risk="R0")
db = SessionLocal()
expect_approval_error("A1 未勾选确认拒绝",
                      lambda: executor.approve_plan(db, i1.id, p1, OP, plan_hash=h1, ack=False),
                      "ack_required")
expect_approval_error("A2 哈希不匹配拒绝",
                      lambda: executor.approve_plan(db, i1.id, p1, OP, plan_hash="bad", ack=True),
                      "hash_mismatch")
i_exp = make_issue()
p_exp, h_exp = make_plan(i_exp.id, [step("reset_own_issue_state", {"issue_id": i_exp.id})],
                         risk="R0", expired=True)
expect_approval_error("A3 过期计划拒绝",
                      lambda: executor.approve_plan(db, i_exp.id, p_exp, OP,
                                                    plan_hash=h_exp, ack=True), "expired")
p2, h2 = make_plan(i1.id, [step("reset_own_issue_state", {"issue_id": i1.id})], risk="R0")
expect_approval_error("A4 viewer 无执行权",
                      lambda: executor.approve_plan(db, i1.id, p2, VIEW,
                                                    plan_hash=h2, ack=True), "forbidden")
out = executor.approve_plan(db, i1.id, p2, OP, plan_hash=h2, ack=True)
check("A5 operator 可授权 R0", out["plan"].status == "approved")
expect_approval_error("A6 已批准计划不可重复批准",
                      lambda: executor.approve_plan(db, i1.id, p2, OP,
                                                    plan_hash=h2, ack=True), "not_proposed")
db.close()

i_r2 = make_issue(fingerprint="ex-r2-role")
p_r2, h_r2 = make_plan(i_r2.id, [step("restart_platform", {"reason": "健康异常"})], risk="R2")
db = SessionLocal()
settings.SELF_HEAL_MODE = "assist"
expect_approval_error("A7 未开放 approved_r2 时拒绝重启计划",
                      lambda: executor.approve_plan(db, i_r2.id, p_r2, ADMIN,
                                                    plan_hash=h_r2, ack=True), "forbidden")
settings.SELF_HEAL_MODE = "approved_r2"
expect_approval_error("A8 operator 无 R2 授权权",
                      lambda: executor.approve_plan(db, i_r2.id, p_r2, OP,
                                                    plan_hash=h_r2, ack=True), "forbidden")
out = executor.approve_plan(db, i_r2.id, p_r2, ADMIN, plan_hash=h_r2, ack=True)
check("A9 admin 在 approved_r2 可授权 R2", out["plan"].status == "approved")
db.close()

# B 并发批准：只有一个成功
i_cc = make_issue(fingerprint="ex-concurrent")
p_cc, h_cc = make_plan(i_cc.id, [step("reset_own_issue_state", {"issue_id": i_cc.id})], risk="R0")
results = []
barrier = threading.Barrier(2)


def do_approve():
    d = SessionLocal()
    u = d.get(User, OP.id)
    barrier.wait()
    try:
        executor.approve_plan(d, i_cc.id, p_cc, u, plan_hash=h_cc, ack=True)
        results.append("ok")
    except executor.PlanApprovalError as exc:
        results.append(exc.code)
    finally:
        d.close()


threads = [threading.Thread(target=do_approve) for _ in range(2)]
[t.start() for t in threads]
[t.join() for t in threads]
check("B1 并发批准只有一个成功", sorted(results) == ["not_proposed", "ok"], str(results))

# C R0 自动执行成功 -> 恢复
executor.reset_breaker()
i_auto = make_issue(fingerprint="ex-auto-r0", status="awaiting_approval")
p_auto, _ = make_plan(i_auto.id, [step("reset_own_issue_state", {"issue_id": i_auto.id})],
                      risk="R0", status="proposed")
settings.SELF_HEAL_MODE = "auto_r0"
with patch.object(verifier, "verify_issue", return_value=(True, "证据已恢复")):
    out = executor.execute_plan(p_auto, automatic=True)
db = SessionLocal()
row = db.get(IssuePlan, p_auto)
issue_row = db.get(IssueCase, i_auto.id)
check("C1 R0 自动执行成功", out.get("ok") is True and out.get("verified") is True, str(out))
check("C2 计划标记成功", row.status == "succeeded", row.status)
check("C3 问题恢复", issue_row.status == "recovered", issue_row.status)
check("C4 attempts 递增", issue_row.attempts == 1, str(issue_row.attempts))
db.close()

# D 执行后验证失败 -> 人工 + 事件 + 保留原因
i_bad = make_issue(fingerprint="ex-verify-bad", status="awaiting_approval")
p_bad, _ = make_plan(i_bad.id, [step("reset_own_issue_state", {"issue_id": i_bad.id})],
                     risk="R0", status="approved", approved_by="sh_exec_admin",
                     approved_by_id=ADMIN.id)
executor.reset_breaker()
with patch.object(verifier, "verify_issue", return_value=(False, "指标仍不可读")):
    out = executor.execute_plan(p_bad)
db = SessionLocal()
row = db.get(IssuePlan, p_bad)
issue_row = db.get(IssueCase, i_bad.id)
check("D1 验证失败返回错误", out.get("ok") is False)
check("D2 计划失败", row.status == "failed", row.status)
check("D3 问题转人工", issue_row.status == "manual_required", issue_row.status)
check("D4 失败原因落库", "指标仍不可读" in (row.result_json or ""))
check("D5 失败事件写入",
      db.query(IssueEvent).filter(IssueEvent.issue_id == i_bad.id,
                                  IssueEvent.kind == "escalated").count() >= 1)
db.close()

# E R2：委托 copilotd 重启 + 启动收口验证恢复
i_restart = make_issue(fingerprint="ex-r2-exec", status="awaiting_approval")
p_restart, h_restart = make_plan(
    i_restart.id, [step("restart_platform", {"reason": "平台健康检查异常"})], risk="R2")
settings.SELF_HEAL_MODE = "approved_r2"
db = SessionLocal()
executor.approve_plan(db, i_restart.id, p_restart, ADMIN, plan_hash=h_restart, ack=True)
db.close()
with patch("app.services.copilot.change_tools.copilotd_health", return_value={"ok": True}), \
     patch("app.services.copilot.change_tools.copilotd_restart_delegated",
           return_value={"ok": True, "receipt": "synthetic"}) as restart_call:
    out = executor.execute_plan(p_restart)
check("E1 重启委托成功", out.get("ok") is True and out.get("deferred") is True, str(out))
check("E2 确实调用 copilotd 重启通道", restart_call.call_count == 1)
db = SessionLocal()
row = db.get(IssuePlan, p_restart)
issue_row = db.get(IssueCase, i_restart.id)
check("E3 计划等待启动收口", row.status == "executing", row.status)
check("E4 问题进入验证中", issue_row.status == "verifying", issue_row.status)
db.close()
with patch.object(verifier, "verify_issue", return_value=(True, "重启后健康")):
    service.reconcile_self_heal()
db = SessionLocal()
row = db.get(IssuePlan, p_restart)
issue_row = db.get(IssueCase, i_restart.id)
check("E5 启动收口后计划成功", row.status == "succeeded", row.status)
check("E6 启动收口后问题恢复", issue_row.status == "recovered", issue_row.status)
db.close()

# F copilotd 不可用：拒绝重启，不假装成功
i_noch = make_issue(fingerprint="ex-r2-nochannel", status="awaiting_approval")
p_noch, h_noch = make_plan(i_noch.id, [step("restart_platform", {})], risk="R2")
db = SessionLocal()
executor.approve_plan(db, i_noch.id, p_noch, ADMIN, plan_hash=h_noch, ack=True)
db.close()
with patch("app.services.copilot.change_tools.copilotd_health",
           return_value={"ok": False, "error": "connection refused"}):
    out = executor.execute_plan(p_noch)
db = SessionLocal()
check("F1 通道不可用时拒绝重启", out.get("ok") is False
      and db.get(IssuePlan, p_noch).status == "failed")
db.close()

# G R3：真实 change_gate/code_mod 在 docs/tmp 合成文件上应用并回滚
tmp_dir = os.path.join(ROOT, "docs", "tmp")
probe = os.path.join(tmp_dir, "selfheal_patch_probe.txt")
os.makedirs(tmp_dir, exist_ok=True)
with open(probe, "w", encoding="utf-8") as fh:
    fh.write("before\n")
try:
    rel = "docs/tmp/selfheal_patch_probe.txt"
    i_code = make_issue(fingerprint="ex-r3-patch", status="awaiting_approval")
    p_code, h_code = make_plan(i_code.id, [step("patch_platform_code", {"plan": {
        "summary": "合成探针补丁",
        "files": [{"path": rel, "new": "after\n"}]}})], risk="R3")
    settings.SELF_HEAL_MODE = "approved_r3"
    db = SessionLocal()
    executor.approve_plan(db, i_code.id, p_code, ADMIN, plan_hash=h_code, ack=True)
    db.close()
    with patch("app.services.copilot.change_tools.copilotd_restart_delegated",
               return_value={"ok": True, "receipt": "synthetic"}):
        out = executor.execute_plan(p_code)
    with open(probe, encoding="utf-8") as fh:
        applied = fh.read()
    db = SessionLocal()
    row = db.get(IssuePlan, p_code)
    snap_id = int((json.loads(row.result_json or "{}") or {}).get("snapshot_id") or 0)
    issue_row = db.get(IssueCase, i_code.id)
    check("G1 补丁真实落盘", applied == "after\n", applied)
    check("G2 返回等待重启", out.get("ok") is True and out.get("deferred") is True, str(out))
    check("G3 快照 id 已保存", snap_id > 0 and row.status == "executing",
          f"{snap_id}/{row.status}")
    check("G4 问题进入验证中", issue_row.status == "verifying", issue_row.status)
    u = db.get(User, ADMIN.id)
    rb = code_repair.rollback_patch(db, issue_row, row, u, reason="测试回滚")
    db.close()
    with open(probe, encoding="utf-8") as fh:
        restored = fh.read()
    check("G5 回滚恢复原内容", restored == "before\n", restored)
    check("G6 回滚成功", rb.get("ok") is True, str(rb))
finally:
    os.remove(probe)

# I 自动失败冷却重试：R0 失败不立刻转人工，克隆新计划；达到 max_attempts 才转人工
executor.reset_breaker()
settings.SELF_HEAL_MODE = "auto_r0"
i_retry = make_issue(fingerprint="ex-auto-retry", status="awaiting_approval")
p_retry, _ = make_plan(i_retry.id, [step("reset_own_issue_state", {"issue_id": i_retry.id})],
                       risk="R0", status="proposed")
db = SessionLocal()
db.get(IssueCase, i_retry.id).current_plan_id = p_retry
db.commit()
db.close()
with patch.object(verifier, "verify_issue", return_value=(False, "暂时不可读")):
    out = executor.execute_plan(p_retry, automatic=True)
db = SessionLocal()
issue_row = db.get(IssueCase, i_retry.id)
check("I1 自动 R0 失败不立即转人工", issue_row.status == "open" and out.get("ok") is False,
      f"{issue_row.status}/{out}")
check("I2 已安排冷却重试时间", issue_row.next_retry_at is not None,
      str(issue_row.next_retry_at))
check("I3 记录 retry_scheduled 事件",
      db.query(IssueEvent).filter(IssueEvent.issue_id == i_retry.id,
                                  IssueEvent.kind == "retry_scheduled").count() >= 1)
issue_row.next_retry_at = datetime.now() - timedelta(seconds=1)
db.commit()
with patch.object(executor, "start_plan", return_value=True) as start:
    n = executor.retry_due_auto()
    check("I4 到期后克隆新计划并自动执行",
          n == 1 and start.call_count == 1
          and start.call_args.kwargs.get("automatic") is True, f"{n}/{start.call_count}")
db.expire_all()
issue_row = db.get(IssueCase, i_retry.id)
new_plan = db.get(IssuePlan, issue_row.current_plan_id)
check("I5 新计划指向克隆版本",
      new_plan.id != p_retry and new_plan.status == "proposed"
      and new_plan.created_by == "auto_retry", f"{new_plan.id}/{new_plan.status}")
check("I6 重试时间已清空", issue_row.next_retry_at is None)
issue_row.attempts = int(issue_row.max_attempts)
db.commit()
with patch.object(verifier, "verify_issue", return_value=(False, "仍失败")):
    out = executor.execute_plan(new_plan.id, automatic=True)
db.expire_all()
issue_row = db.get(IssueCase, i_retry.id)
check("I7 达到 max_attempts 后转人工", issue_row.status == "manual_required",
      f"{issue_row.status}/{issue_row.attempts}")
check("I8 达上限后不再调度重试", issue_row.next_retry_at is None)
db.close()

# H 熔断：连续 5 个计划失败后自动执行关闭，管理员可重置
settings.SELF_HEAL_BREAKER_FAILS = 5
executor.reset_breaker()
db = SessionLocal()
for n in range(5):
    db.add(IssuePlan(issue_id=i_bad.id, version=100 + n, status="failed", risk="R0",
                     plan_json="{}", plan_hash=f"breaker-{n}",
                     created_at=datetime.now()))
db.commit()
executor._maybe_trip_breaker(db)
state = executor.breaker_state()
check("H1 连续失败触发熔断", state["open"] is True, str(state))
p_block, h_block = make_plan(i_bad.id, [step("reset_own_issue_state", {"issue_id": i_bad.id})],
                             risk="R0")
try:
    executor.approve_plan(db, i_bad.id, p_block, OP, plan_hash=h_block, ack=True)
    check("H2 熔断后拒绝新授权", False, "未拒绝")
except executor.PlanApprovalError as exc:
    check("H2 熔断后拒绝新授权", exc.code == "breaker_open", exc.code)
executor.reset_breaker("test")
check("H3 管理员重置熔断", executor.breaker_state()["open"] is False)
db.close()

print(f"\n结果: {len(PASS)} 通过, {len(FAIL)} 失败")
if FAIL:
    print("失败项: " + ", ".join(FAIL))
    sys.exit(1)
