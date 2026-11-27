#!/usr/bin/env python3
"""平台自愈 API：角色契约、列表/详情、诊断/重试/协助/忽略、哈希授权/拒绝/回滚、并发。

运行：cd backend && QIANSHU_TEST_PG_URL=... python3 tests/test_self_heal_api_mock.py
使用 ASGI 直连 + 一次性测试用户；诊断/执行线程全部替身，不访问外部。
"""
import asyncio
import importlib
import os
import pkgutil
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

try:
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url

os.environ["DATABASE_URL"] = test_database_url("selfheal_api")
os.environ["MASTER_KEY"] = "ab" * 32
os.environ["CAPTCHA_ENABLED"] = "false"
os.environ["EVENT_WEBHOOK_TOKEN"] = "test-token"
os.environ["METRIC_SOURCES"] = "[]"
os.environ["LOG_SOURCES"] = "[]"
os.environ["VM_URL"] = "http://127.0.0.1:1"
os.environ["SELF_HEAL_ENABLED"] = "true"
os.environ["SELF_HEAL_MODE"] = "assist"
os.environ["SELF_HEAL_LLM_ENABLED"] = "false"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.core.migrate import migrate_self_heal_indexes  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.main import app  # noqa: E402

import app.models as _models_pkg  # noqa: E402
for _m in pkgutil.iter_modules(_models_pkg.__path__):
    importlib.import_module(f"app.models.{_m.name}")

from app.models.issue import IssueCase, IssueEvent, IssuePlan  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.self_heal import executor, service, validator  # noqa: E402

Base.metadata.create_all(engine)
migrate_self_heal_indexes(engine)

PASS, FAIL = [], []
PW = "SynthPass!234"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


db = SessionLocal()
for uname, role in (("api_admin", "admin"), ("api_op", "operator"), ("api_view", "viewer")):
    db.add(User(username=uname, password_hash=hash_password(PW), role=role,
                is_active=True, must_change_password=False))
db.commit()
db.close()


def _req(method, url, **kw):
    async def _go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://testserver") as c:
            return await c.request(method, url, **kw)
    return asyncio.run(_go())


def login(username):
    r = _req("POST", "/api/auth/login", json={"username": username, "password": PW})
    assert r.status_code == 200, (username, r.status_code, r.text[:300])
    return {"Authorization": f"Bearer {r.json()['token']}"}


ADMIN = login("api_admin")
OP = login("api_op")
VIEW = login("api_view")


def make_issue(**kw):
    now = datetime.now()
    defaults = dict(source="onboarding", account_id=0, run_id=0, capability="assets",
                    severity="warning", status="awaiting_approval", title="接入失败",
                    reason_code="asset_sync_failed", error_class="ConnectionError",
                    occurrences=2, attempts=0, max_attempts=3, next_action="approve",
                    summary_md="资产同步失败", first_seen=now, last_seen=now)
    defaults.update(kw)
    defaults["fingerprint"] = kw.get("fingerprint") or f"apifp-{datetime.now().timestamp()}"
    s = SessionLocal()
    issue = IssueCase(**defaults)
    s.add(issue)
    s.commit()
    s.refresh(issue)
    s.expunge(issue)
    s.close()
    return issue


def make_plan(issue_id, recipe="reset_own_issue_state", params=None, risk="R0",
              status="proposed", expired=False):
    if params is None:
        params = ({"issue_id": issue_id}
                  if recipe == "reset_own_issue_state" else {})
    plan = validator.normalize_plan({
        "summary": "测试计划", "root_cause": "测试根因", "impact": "测试影响",
        "confidence": 0.5, "can_auto_fix": True, "need_user": False,
        "steps": [{"recipe": recipe, "params": params}],
        "verify": "验证", "assist_steps": [], "questions": []})
    assert plan["risk"] == risk
    s = SessionLocal()
    version = service.new_plan_version(s, issue_id)
    row = IssuePlan(issue_id=issue_id, version=version, status=status, risk=risk,
                    plan_json=service._dumps(plan),
                    plan_hash=validator.plan_hash(plan, issue_id=issue_id, version=version),
                    expires_at=datetime.now() + (-timedelta(minutes=1) if expired
                                                 else timedelta(hours=1)))
    s.add(row)
    s.commit()
    s.refresh(row)
    pid, phash = row.id, row.plan_hash
    s.close()
    return pid, phash


print("A 元信息与列表")
r = _req("GET", "/api/issues/meta", headers=OP)
d = r.json()
check("A1 meta 200", r.status_code == 200 and d.get("mode") == "assist", r.text[:200])
check("A2 meta 暴露 R0-R4 风险说明", set(d.get("risks") or {}) == {"R0", "R1", "R2", "R3", "R4"})
check("A3 meta 返回角色", d.get("role") == "operator")

i_list = make_issue(fingerprint="api-list-1", account_id=1, source="onboarding")
make_issue(fingerprint="api-list-2", account_id=2, source="cloud_sync",
           status="recovered", severity="critical")
r = _req("GET", "/api/issues?page=1&page_size=50", headers=VIEW)
d = r.json()
check("A4 viewer 可读列表", r.status_code == 200 and d.get("total") >= 2, r.text[:200])
check("A5 列表按来源过滤",
      all(x["source"] == "cloud_sync" for x in _req(
          "GET", "/api/issues?source=cloud_sync", headers=VIEW).json()["items"]))
check("A6 列表按账号过滤",
      all(x["account_id"] == 1 for x in _req(
          "GET", "/api/issues?account_id=1", headers=VIEW).json()["items"]))
check("A7 列表按状态过滤",
      all(x["status"] == "recovered" for x in _req(
          "GET", "/api/issues?status=recovered", headers=VIEW).json()["items"]))
i_run = make_issue(fingerprint="api-run-77", account_id=3, run_id=77)
d = _req("GET", "/api/issues?run_id=77", headers=VIEW).json()
check("A8 列表按接入任务过滤", d.get("total") == 1
      and d["items"][0]["run_id"] == 77, str(d.get("total")))
old = make_issue(fingerprint="api-old", last_seen=datetime.now() - timedelta(days=3))
d = _req("GET", "/api/issues?hours=24", headers=VIEW).json()
check("A9 列表按最近时间窗过滤",
      all(x["id"] != old.id for x in d.get("items") or []),
      str([x["id"] for x in d.get("items") or []]))
d = _req("GET", "/api/issues", headers=VIEW).json()
st = d.get("stats") or {}
check("A10 列表返回运营统计",
      st.get("total", -1) >= 3 and {"active", "recovered", "awaiting_approval",
                                    "manual_required", "recurring"} <= set(st),
      str(st))
# 合成凭证必须只在库中存在，详情响应不得回显
i_secret = make_issue(
    fingerprint="api-secret",
    source_ref_json='{"password": "synthetic-password", "note": "AKIAABCDEFGHIJKLMNOP"}',
    evidence_json='{"token": "synthetic-token-value", "ok": 1}')
r_secret = _req("GET", f"/api/issues/{i_secret.id}", headers=ADMIN)
check("A11 详情回显前脱敏凭证",
      "synthetic-password" not in r_secret.text
      and "synthetic-token-value" not in r_secret.text
      and "AKIAABCDEFGHIJKLMNOP" not in r_secret.text, r_secret.text[:300])

print("B 详情与按钮权限")
p_r0, h_r0 = make_plan(i_list.id)
i_list.current_plan_id = p_r0
s = SessionLocal()
s.merge(i_list)
s.commit()
s.close()
r = _req("GET", f"/api/issues/{i_list.id}", headers=OP)
d = r.json()
check("B1 详情返回计划/事件/按钮", r.status_code == 200 and d.get("current_plan")
      and "allowed" in d and "events" in d, r.text[:300])
check("B2 operator 可诊断/重试，不可授权",
      d["allowed"]["diagnose"] and d["allowed"]["retry"]
      and not d["allowed"]["approve"] and not d["allowed"]["ignore"], str(d["allowed"]))
r_view = _req("GET", f"/api/issues/{i_list.id}", headers=VIEW).json()
check("B3 viewer 无操作按钮", not any(v for k, v in r_view["allowed"].items()
                                     if k != "approve_blocked"), str(r_view["allowed"]))
r_admin = _req("GET", f"/api/issues/{i_list.id}", headers=ADMIN).json()
check("B4 admin 可授权 R0", r_admin["allowed"]["approve"] is True, str(r_admin["allowed"]))
r404 = _req("GET", "/api/issues/999999", headers=ADMIN)
check("B5 不存在问题 404", r404.status_code == 404)

i_r2 = make_issue(fingerprint="api-r2-plan")
p_r2, h_r2 = make_plan(i_r2.id, recipe="restart_platform", params={}, risk="R2")
s = SessionLocal()
row = s.get(IssueCase, i_r2.id)
row.current_plan_id = p_r2
s.commit()
s.close()
settings.SELF_HEAL_MODE = "assist"
d = _req("GET", f"/api/issues/{i_r2.id}", headers=ADMIN).json()
check("B6 模式未开 R2：不显示可点授权，给出阻塞原因",
      d["allowed"]["approve"] is False and d["allowed"]["approve_blocked"] is True,
      str(d["allowed"]))

print("C 诊断/重试/协助/忽略")
i_diag = make_issue(fingerprint="api-diag", status="open", next_action="diagnose")
r = _req("POST", f"/api/issues/{i_diag.id}/diagnose", headers=VIEW)
check("C1 viewer 不能诊断", r.status_code == 403, str(r.status_code))
with patch("app.services.self_heal.planner.start_diagnosis", return_value=True) as start:
    r = _req("POST", f"/api/issues/{i_diag.id}/diagnose", headers=OP)
check("C2 operator 诊断受理", r.status_code == 202 and start.call_count == 1, r.text[:200])
p_diag, _ = make_plan(i_diag.id)
s = SessionLocal()
s.get(IssueCase, i_diag.id).current_plan_id = p_diag
s.commit()
s.close()
r = _req("POST", f"/api/issues/{i_diag.id}/diagnose", headers=OP)
check("C3 模型刚跑过 60 秒内拒绝重复诊断", r.status_code == 409)
with patch("app.services.self_heal.planner.start_diagnosis", return_value=False):
    s = SessionLocal()
    row = s.get(IssueCase, i_diag.id)
    row.status = "open"
    s.commit()
    s.close()
    r = _req("POST", f"/api/issues/{i_diag.id}/diagnose", headers=OP)
check("C4 已有诊断线程时 409", r.status_code == 409, r.text[:200])

i_retry = make_issue(fingerprint="api-retry", status="manual_required")
p_retry, _ = make_plan(i_retry.id)
s = SessionLocal()
row = s.get(IssueCase, i_retry.id)
row.current_plan_id = p_retry
s.commit()
s.close()
r = _req("POST", f"/api/issues/{i_retry.id}/retry", headers=VIEW)
check("C5 viewer 不能重试", r.status_code == 403)
with patch("app.services.self_heal.executor.start_plan", return_value=True) as start:
    r = _req("POST", f"/api/issues/{i_retry.id}/retry", headers=OP)
check("C6 operator R0 重试受理", r.status_code == 202 and start.call_count == 1,
      r.text[:200])
r = _req("POST", f"/api/issues/{i_retry.id}/retry", headers=OP)
check("C7 修复中拒绝重复重试", r.status_code == 409, r.text[:200])

i_r2retry = make_issue(fingerprint="api-r2-retry", status="awaiting_approval")
p_r2r, _ = make_plan(i_r2retry.id, recipe="restart_platform", params={}, risk="R2")
s = SessionLocal()
row = s.get(IssueCase, i_r2retry.id)
row.current_plan_id = p_r2r
s.commit()
s.close()
r = _req("POST", f"/api/issues/{i_r2retry.id}/retry", headers=OP)
check("C8 重试接口拒绝 R2", r.status_code == 403, r.text[:200])

i_assist = make_issue(fingerprint="api-assist", status="awaiting_user")
with patch("app.services.self_heal.planner.start_diagnosis", return_value=True) as start:
    r = _req("POST", f"/api/issues/{i_assist.id}/assist", headers=OP,
             json={"note": "已完成网络放通"})
check("C9 协助回传后继续诊断", r.status_code == 202 and start.call_count == 1,
      r.text[:200])
s = SessionLocal()
row = s.get(IssueCase, i_assist.id)
check("C10 协助结果作为证据保存", "已完成网络放通" in (row.evidence_json or ""))
check("C11 状态回到诊断中", row.status == "diagnosing", row.status)
s.close()

i_ignore = make_issue(fingerprint="api-ignore", status="open")
r = _req("POST", f"/api/issues/{i_ignore.id}/ignore", headers=OP)
check("C12 operator 不能忽略", r.status_code == 403)
r = _req("POST", f"/api/issues/{i_ignore.id}/ignore", headers=ADMIN)
check("C13 admin 可忽略", r.status_code == 200 and r.json().get("ok") is True)
s = SessionLocal()
check("C14 忽略后状态 ignored", s.get(IssueCase, i_ignore.id).status == "ignored")
s.close()
r = _req("POST", f"/api/issues/{i_ignore.id}/ignore", headers=ADMIN)
check("C15 重复忽略幂等", r.status_code == 200 and r.json().get("already") is True)

print("D 授权/拒绝/回滚/熔断")
settings.SELF_HEAL_MODE = "approved_r2"
i_appr = make_issue(fingerprint="api-approve", status="awaiting_approval")
p_appr, h_appr = make_plan(i_appr.id)
s = SessionLocal()
s.get(IssueCase, i_appr.id).current_plan_id = p_appr
s.commit()
s.close()
r = _req("POST", f"/api/issues/{i_appr.id}/plans/{p_appr}/approve", headers=VIEW,
         json={"plan_hash": h_appr, "ack": True})
check("D1 viewer 不能授权", r.status_code == 403)
r = _req("POST", f"/api/issues/{i_appr.id}/plans/{p_appr}/approve", headers=OP,
         json={"plan_hash": h_appr, "ack": True})
check("D2 operator 不能授权", r.status_code == 403)
r = _req("POST", f"/api/issues/{i_appr.id}/plans/{p_appr}/approve", headers=ADMIN,
         json={"plan_hash": "0" * 64, "ack": True})
check("D3 哈希不匹配 409", r.status_code == 409, r.text[:200])
r = _req("POST", f"/api/issues/{i_appr.id}/plans/{p_appr}/approve", headers=ADMIN,
         json={"plan_hash": h_appr, "ack": False})
check("D4 未勾选确认 409", r.status_code == 409, r.text[:200])
with patch("app.services.self_heal.executor.start_plan", return_value=True) as start:
    r = _req("POST", f"/api/issues/{i_appr.id}/plans/{p_appr}/approve", headers=ADMIN,
             json={"plan_hash": h_appr, "ack": True})
check("D5 admin 按哈希授权成功", r.status_code == 200 and start.call_count == 1,
      r.text[:300])
s = SessionLocal()
check("D6 计划 approved / 问题 repairing",
      s.get(IssuePlan, p_appr).status == "approved"
      and s.get(IssueCase, i_appr.id).status == "repairing")
s.close()

i_exp = make_issue(fingerprint="api-expire", status="awaiting_approval")
p_exp, h_exp = make_plan(i_exp.id, expired=True)
r = _req("POST", f"/api/issues/{i_exp.id}/plans/{p_exp}/approve", headers=ADMIN,
         json={"plan_hash": h_exp, "ack": True})
check("D7 过期计划 410", r.status_code == 410, r.text[:200])

i_cc = make_issue(fingerprint="api-concurrent", status="awaiting_approval")
p_cc, h_cc = make_plan(i_cc.id)

def do_approve(_):
    return _req("POST", f"/api/issues/{i_cc.id}/plans/{p_cc}/approve", headers=ADMIN,
                json={"plan_hash": h_cc, "ack": True}).status_code

with patch("app.services.self_heal.executor.start_plan", return_value=True):
    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(do_approve, range(2)))
check("D8 并发授权只有一个成功", sorted(codes) == [200, 409], str(codes))

i_rej = make_issue(fingerprint="api-reject", status="awaiting_approval")
p_rej, _ = make_plan(i_rej.id)
r = _req("POST", f"/api/issues/{i_rej.id}/plans/{p_rej}/reject", headers=OP,
         json={"reason": "不同意"})
check("D9 operator 不能拒绝", r.status_code == 403)
r = _req("POST", f"/api/issues/{i_rej.id}/plans/{p_rej}/reject", headers=ADMIN,
         json={"reason": "风险过大"})
check("D10 admin 拒绝计划", r.status_code == 200 and r.json()["plan"]["status"] == "rejected")
s = SessionLocal()
check("D11 拒绝后问题转人工", s.get(IssueCase, i_rej.id).status == "manual_required")
s.close()

i_rb = make_issue(fingerprint="api-rollback", status="awaiting_approval")
p_rb, _ = make_plan(i_rb.id)
r = _req("POST", f"/api/issues/{i_rb.id}/plans/{p_rb}/rollback", headers=OP)
check("D12 operator 不能回滚", r.status_code == 403)
r = _req("POST", f"/api/issues/{i_rb.id}/plans/{p_rb}/rollback", headers=ADMIN)
check("D13 非 R3 计划拒绝回滚", r.status_code == 400, r.text[:200])

executor.reset_breaker("test")
r = _req("POST", "/api/issues/system/breaker/reset", headers=OP)
check("D14 operator 不能重置熔断", r.status_code == 403)
r = _req("POST", "/api/issues/system/breaker/reset", headers=ADMIN)
check("D15 admin 重置熔断", r.status_code == 200 and r.json()["breaker"]["open"] is False)

print(f"\n结果: {len(PASS)} 通过, {len(FAIL)} 失败")
if FAIL:
    print("失败项: " + ", ".join(FAIL))
    sys.exit(1)
