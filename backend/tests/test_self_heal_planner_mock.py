#!/usr/bin/env python3
"""平台自愈 P1/P2 计划器与校验器：严格 schema/风险/越权拒绝、LLM 降级、哈希绑定。

运行：cd backend && QIANSHU_TEST_PG_URL=... python3 tests/test_self_heal_planner_mock.py
LLM 全部替身；不访问网络、不执行计划。
"""
import importlib
import os
import pkgutil
import sys
from datetime import datetime
from unittest.mock import patch

try:
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url

os.environ["DATABASE_URL"] = test_database_url("selfheal_plan")
os.environ["MASTER_KEY"] = "ab" * 32
os.environ["EVENT_WEBHOOK_TOKEN"] = "test-token"
os.environ["METRIC_SOURCES"] = "[]"
os.environ["LOG_SOURCES"] = "[]"
os.environ["VM_URL"] = "http://127.0.0.1:1"
os.environ["SELF_HEAL_ENABLED"] = "true"
os.environ["SELF_HEAL_MODE"] = "assist"
os.environ["SELF_HEAL_LLM_ENABLED"] = "false"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings  # noqa: E402
from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.core.migrate import migrate_self_heal_indexes  # noqa: E402

import app.models as _models_pkg  # noqa: E402
for _m in pkgutil.iter_modules(_models_pkg.__path__):
    importlib.import_module(f"app.models.{_m.name}")

from app.models.cloud_account import CloudAccount  # noqa: E402
from app.models.in_app_message import InAppMessage  # noqa: E402
from app.models.issue import IssueCase, IssueEvent, IssuePlan  # noqa: E402
from app.services.self_heal import catalog, planner, service, validator  # noqa: E402

Base.metadata.create_all(engine)
migrate_self_heal_indexes(engine)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def expect_error(name, fn, code=None):
    try:
        fn()
    except validator.PlanValidationError as exc:
        ok = code is None or exc.code == code
        check(name, ok, f"code={exc.code} {exc}")
        return
    check(name, False, "未抛出 PlanValidationError")


s = SessionLocal()
acct = CloudAccount(name="自愈测试账号", vendor="aliyun", ak_cipher="", secret_cipher="")
s.add(acct)
s.commit()
ACC = acct.id
s.close()


def make_issue(**kw):
    now = datetime.now()
    defaults = dict(source="onboarding", account_id=ACC, run_id=0, capability="assets",
                    severity="warning", status="open", title="资产同步失败",
                    reason_code="asset_sync_failed", error_class="ConnectionError",
                    occurrences=1, attempts=0, max_attempts=3, next_action="diagnose",
                    first_seen=now, last_seen=now)
    defaults.update(kw)
    defaults["fingerprint"] = kw.get("fingerprint") or f"fp-{datetime.now().timestamp()}"
    s = SessionLocal()
    issue = IssueCase(**defaults)
    s.add(issue)
    s.commit()
    s.refresh(issue)
    s.expunge(issue)
    s.close()
    return issue


def base_plan(**kw):
    plan = {
        "summary": "刷新资产", "root_cause": "资产同步连接中断", "impact": "仅该账号资产",
        "confidence": 0.6, "can_auto_fix": True, "need_user": False,
        "steps": [{"recipe": "refresh_cloud_assets", "params": {"account_id": ACC},
                   "reason": "只读重跑", "expected_effect": "资产可回读"}],
        "verify": "资产数量可读", "assist_steps": [], "questions": [],
    }
    plan.update(kw)
    return plan


# A 校验器：正常 R0 计划被规范化，风险由目录决定
p = validator.normalize_plan(base_plan(risk="R0"))
check("A1 合法 R0 计划通过", p["risk"] == "R0" and len(p["steps"]) == 1)
check("A2 步骤字段由目录补齐", p["steps"][0]["verify"]["kind"] == "onboarding_capability")
check("A3 计划 schema 固定", p["schema"] == "qianshu.self_heal.plan.v1")

# B 越权输出拒绝
bad = base_plan(steps=[{"recipe": "run_shell", "params": {"cmd": "ls"}}])
expect_error("B1 未知动作拒绝", lambda: validator.normalize_plan(bad), "unknown_recipe")
bad = base_plan(steps=[{"recipe": "restart_platform", "params": {}, "risk": "R0"}])
expect_error("B2 自报风险与目录不一致拒绝",
             lambda: validator.normalize_plan(bad), "risk_mismatch")
bad = base_plan(steps=[{"recipe": "refresh_cloud_assets",
                        "params": {"account_id": ACC, "command": "reboot"}}])
expect_error("B3 禁止字段拒绝", lambda: validator.normalize_plan(bad), "forbidden_field")
bad = base_plan(steps=[{"recipe": "refresh_cloud_assets",
                        "params": {"account_id": ACC, "note": "curl http://x"}}])
expect_error("B4 危险片段拒绝", lambda: validator.normalize_plan(bad), "forbidden_fragment")
bad = base_plan(steps=[{"recipe": "refresh_cloud_assets",
                        "params": {"account_id": ACC, "path": "/etc/passwd"}}])
expect_error("B5 禁止 path 字段拒绝", lambda: validator.normalize_plan(bad), "forbidden_field")
bad = base_plan(extra="x")
expect_error("B6 顶层未知字段拒绝", lambda: validator.normalize_plan(bad), "unknown_field")
expect_error("B7 超过 5 步拒绝",
             lambda: validator.normalize_plan(base_plan(steps=[
                 {"recipe": "refresh_cloud_assets", "params": {"account_id": ACC}}] * 6)),
             "too_many_steps")
restart_mid = base_plan(steps=[
    {"recipe": "refresh_cloud_assets", "params": {"account_id": ACC}},
    {"recipe": "restart_platform", "params": {}},
    {"recipe": "probe_integration", "params": {"account_id": ACC}},
])
expect_error("B8 重启不是最后一步拒绝",
             lambda: validator.normalize_plan(restart_mid), "restart_not_last")
dup = base_plan(steps=[
    {"recipe": "refresh_cloud_assets", "params": {"account_id": ACC}},
    {"recipe": "refresh_cloud_assets", "params": {"account_id": ACC}},
])
expect_error("B9 重复 recipe 拒绝", lambda: validator.normalize_plan(dup), "duplicate_recipe")
expect_error("B10 空步骤且不需要用户拒绝",
             lambda: validator.normalize_plan(base_plan(steps=[])), "empty_plan")
expect_error("B11 need_user 与 can_auto_fix 同时为真拒绝",
             lambda: validator.normalize_plan(base_plan(need_user=True)),
             "inconsistent_plan")
bad = base_plan(steps=[{"recipe": "refresh_cloud_assets",
                        "params": {"account_id": 999999}}])
expect_error("B12 不存在的账号拒绝", lambda: validator.normalize_plan(bad, db=s),
             "unknown_account")
check("B13 目录中不存在 R4 动作", not catalog.names_by_risk("R4"))

# C R3 代码补丁：允许路径通过，禁改域拒绝，绝对路径拒绝
rel = "backend/app/services/onboarding_tasks.py"
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
    current = fh.read()
patch_plan = base_plan(
    root_cause="平台代码缺陷", can_auto_fix=True, need_user=False,
    steps=[{"recipe": "patch_platform_code",
            "params": {"plan": {"summary": "追加注释",
                                "files": [{"path": rel,
                                           "new": current + "\n# self-heal-test\n"}]}}}])
p3 = validator.normalize_plan(patch_plan)
check("C1 允许路径 R3 补丁通过", p3["risk"] == "R3"
      and p3["steps"][0]["recipe"] == "patch_platform_code")
deny_plan = base_plan(steps=[{"recipe": "patch_platform_code",
                              "params": {"plan": {"files": [
                                  {"path": "backend/app/services/self_heal/service.py",
                                   "new": "print('x')\n"}]}}}])
expect_error("C2 自愈核心禁改域拒绝",
             lambda: validator.normalize_plan(deny_plan), "code_patch_rejected")
abs_plan = base_plan(steps=[{"recipe": "patch_platform_code",
                             "params": {"plan": {"files": [
                                 {"path": "/etc/passwd", "new": "x"}]}}}])
expect_error("C3 补丁绝对路径拒绝",
             lambda: validator.normalize_plan(abs_plan), "forbidden_path")

# D 计划哈希：绑定问题单与版本，同输入稳定
h1 = validator.plan_hash(p, issue_id=1, version=1)
h2 = validator.plan_hash(p, issue_id=1, version=1)
h3 = validator.plan_hash(p, issue_id=2, version=1)
h4 = validator.plan_hash(p, issue_id=1, version=2)
check("D1 同问题单同版本哈希稳定", h1 == h2)
check("D2 哈希绑定问题单", h1 != h3)
check("D3 哈希绑定版本", h1 != h4)

# E 确定性降级（不用模型）：资产失败 -> R0 刷新；凭证类 -> 等待用户
issue = make_issue()
row = planner.create_plan(s, issue, use_llm=False, reason="测试")
s.refresh(row)
check("E1 确定性计划风险 R0", row.risk == "R0", row.risk)
check("E2 判断为模型不可用/未启用", row.created_by == "deterministic", row.created_by)
plan_json = service._json(row.plan_json, {})
check("E3 选择固定只读刷新动作",
      plan_json["steps"][0]["recipe"] == "refresh_cloud_assets",
      str(plan_json.get("steps")))
s.refresh(issue)
check("E4 进入待审批", issue.status == "awaiting_approval", issue.status)
check("E5 站内信只写 agent_issue",
      s.query(InAppMessage).filter(InAppMessage.msg_type == "agent_issue",
                                   InAppMessage.ref_id == issue.id).count() >= 1)

cred = make_issue(fingerprint="fp-cred", error_class="AccessDenied",
                  reason_code="credential_invalid", title="凭证无效")
row = planner.create_plan(s, cred, use_llm=False)
s.refresh(cred)
check("E6 凭证类问题进入等待用户", cred.status == "awaiting_user", cred.status)
assist = service._json(row.assist_json, {})
check("E7 给出协助步骤而非执行", assist.get("steps") and not service._json(row.plan_json, {}).get("steps"))

# F LLM 路径：合法计划落库 lg 标记；非法计划记录 plan_rejected 并降级
settings.SELF_HEAL_LLM_ENABLED = True
llm_issue = make_issue(fingerprint="fp-llm")
valid_llm = validator.normalize_plan(base_plan())
with patch.object(planner, "_llm_plan", return_value=(valid_llm, "诊断叙述", "synthetic-model")):
    row = planner.create_plan(s, llm_issue, use_llm=True)
s.refresh(row)
check("F1 LLM 合法计划落库", row.created_by == "llm" and row.model == "synthetic-model",
      f"{row.created_by}/{row.model}")
check("F2 LLM 诊断叙述保留", row.diagnosis_md == "诊断叙述")

bad_issue = make_issue(fingerprint="fp-llm-bad")
err = validator.PlanValidationError("含禁止字段 command", code="forbidden_field")
with patch.object(planner, "_llm_plan", side_effect=err):
    row = planner.create_plan(s, bad_issue, use_llm=True)
s.refresh(row)
check("F3 非法模型计划降级为确定性", row.created_by == "deterministic", row.created_by)
check("F4 记录 plan_rejected 事件",
      s.query(IssueEvent).filter(IssueEvent.issue_id == bad_issue.id,
                                 IssueEvent.kind == "plan_rejected").count() == 1)
check("F5 非法计划写拒绝站内信",
      s.query(InAppMessage).filter(InAppMessage.ref_id == bad_issue.id,
                                   InAppMessage.title.like("%修复计划被拒绝%")).count() >= 1)

down_issue = make_issue(fingerprint="fp-llm-down")
with patch.object(planner, "_llm_plan", side_effect=RuntimeError("timeout")):
    row = planner.create_plan(s, down_issue, use_llm=True)
s.refresh(row)
check("F6 模型不可用降级", row.created_by == "deterministic")
check("F7 降级说明包含原因",
      "模型不可用" in service._json(row.plan_json, {}).get("root_cause", ""))

# G 每日计划上限：超限强制人工计划
old_max = settings.SELF_HEAL_MAX_PLANS_PER_DAY
settings.SELF_HEAL_MAX_PLANS_PER_DAY = 0
limit_issue = make_issue(fingerprint="fp-limit")
row = planner.create_plan(s, limit_issue, use_llm=False)
check("G1 超出日预算转确定性人工方案",
      row.created_by == "deterministic" and not service._json(row.plan_json, {}).get("steps"))
settings.SELF_HEAL_MAX_PLANS_PER_DAY = old_max

# G2 token 预算：今日自愈 token 超限，禁止调用模型，只出人工方案
from datetime import date as _date  # noqa: E402
from app.models.llm_call_log import LlmCallLog  # noqa: E402
settings.SELF_HEAL_LLM_ENABLED = True
old_tokens = settings.SELF_HEAL_MAX_TOKENS_PER_DAY
settings.SELF_HEAL_MAX_TOKENS_PER_DAY = 100
s.add(LlmCallLog(setting_id=1, provider_label="synthetic", model="synthetic",
                 purpose="self_heal_plan", total_tokens=500,
                 call_date=_date.today(), status="ok"))
s.commit()
token_issue = make_issue(fingerprint="fp-token")
with patch.object(planner, "_llm_plan", side_effect=AssertionError("不应调用模型")):
    row = planner.create_plan(s, token_issue, use_llm=True)
check("G2 超 token 预算不调用模型",
      row.created_by == "deterministic" and not service._json(row.plan_json, {}).get("steps"),
      row.created_by)
check("G3 超预算原因可读",
      "token" in service._json(row.plan_json, {}).get("root_cause", ""),
      service._json(row.plan_json, {}).get("root_cause", ""))
settings.SELF_HEAL_MAX_TOKENS_PER_DAY = old_tokens
settings.SELF_HEAL_LLM_ENABLED = False

# H 自动执行开关：observe 不自动；auto_r0 才自动且只对 R0/R1
auto_issue = make_issue(fingerprint="fp-auto")
settings.SELF_HEAL_MODE = "observe"
with patch("app.services.self_heal.executor.start_plan") as start:
    planner.create_plan(s, auto_issue, use_llm=False)
    check("H1 observe 不自动执行", start.call_count == 0)
settings.SELF_HEAL_MODE = "auto_r0"
auto_issue2 = make_issue(fingerprint="fp-auto2")
with patch("app.services.self_heal.executor.start_plan") as start:
    planner.create_plan(s, auto_issue2, use_llm=False)
    check("H2 auto_r0 自动执行 R0",
          start.call_count == 1 and start.call_args.kwargs.get("automatic") is True,
          str(start.call_args))
settings.SELF_HEAL_MODE = "assist"

print(f"\n结果: {len(PASS)} 通过, {len(FAIL)} 失败")
if FAIL:
    print("失败项: " + ", ".join(FAIL))
    sys.exit(1)
