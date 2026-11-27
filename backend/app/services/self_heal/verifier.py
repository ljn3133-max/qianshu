"""问题验证：只读取证，不以“步骤返回成功”冒充恢复。"""
from __future__ import annotations

import json
import logging

from app.models.issue import IssueCase, IssuePlan

logger = logging.getLogger("qianshu.self_heal.verifier")


def _expect_map(text: str) -> dict:
    out = {}
    for part in (text or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _capability_ok(db, issue: IssueCase, expect: dict) -> tuple[bool, str]:
    from app.models.cloud_account import CloudAccount
    from app.services import onboarding

    account = db.get(CloudAccount, int(issue.account_id or 0))
    if account is None:
        return False, f"账号 {issue.account_id} 不存在"
    run = onboarding.get_run(db, account.id)
    if run is None:
        return False, "接入任务不存在"
    capability = expect.get("capability") or issue.capability
    items = [i for i in onboarding.capability_results(run, account)
             if not capability or i.get("capability") == capability]
    if not items:
        return False, f"没有 {capability or '目标能力'} 的证据"
    want_exec = expect.get("execution")
    for item in items:
        if want_exec and item.get("execution") != want_exec:
            continue
        if want_exec == "succeeded":
            if item.get("data_state") in ("available", "no_data") or item.get("data_state") is None:
                return True, f"{capability} 已成功，data_state={item.get('data_state')}"
            continue
        return True, f"{capability} execution={item.get('execution')}"
    sample = items[0]
    return False, (f"{capability} 未达到 {want_exec or '预期'}："
                   f"execution={sample.get('execution')}, "
                   f"data_state={sample.get('data_state')}, "
                   f"reason={sample.get('reason_code')}")


def _probe_ok(db, issue: IssueCase) -> tuple[bool, str]:
    from app.core.security import decrypt_secret
    from app.models.cloud_account import CloudAccount
    from app.services.cloud.provider import capabilities_of, get_provider

    account = db.get(CloudAccount, int(issue.account_id or 0))
    if account is None:
        return False, "账号不存在"
    if not capabilities_of(account.vendor).probe:
        return False, f"{account.vendor} 本期不支持探活"
    try:
        ak = decrypt_secret(account.ak_cipher) if account.ak_cipher else ""
        sk = decrypt_secret(account.secret_cipher) if account.secret_cipher else ""
        result = get_provider(account.vendor).test_account(account, ak, sk)
    except Exception as exc:  # noqa: BLE001
        return False, f"只读探测异常：{type(exc).__name__}"
    if result.get("ok"):
        return True, "只读连通探测通过"
    return False, f"只读连通探测失败：{str(result.get('message') or '')[:200]}"


def _platform_health(db, expect: str) -> tuple[bool, str]:
    if expect == "settings_reloaded":
        from app.services import monitor_policy
        try:
            monitor_policy.ensure_defaults(db)
            rows = monitor_policy.modules_payload(db)
            return bool(rows), f"平台内设置已可用（{len(rows)} 个模块）"
        except Exception as exc:  # noqa: BLE001
            return False, f"设置重载验证失败：{type(exc).__name__}"
    try:
        from app.services.copilot import change_tools
        data = change_tools.copilotd_health()
        platform = data.get("platform") or {}
        healthy = bool(data.get("ok")) and bool(platform.get("healthy"))
        return healthy, ("平台健康检查通过" if healthy
                         else f"平台健康检查未通过：{platform.get('problems') or '未知'}")
    except Exception as exc:  # noqa: BLE001
        return False, f"健康检查不可用：{type(exc).__name__}"


def _alert_rules_ok(db, issue: IssueCase) -> tuple[bool, str]:
    from app.models.alert_rule import AlertRule

    n = (db.query(AlertRule)
         .filter(AlertRule.account_id == int(issue.account_id or 0),
                 AlertRule.origin == "auto", AlertRule.enabled == True)  # noqa: E712
         .count())
    return bool(n), f"账号默认告警规则 {n} 条"


def verify_issue(db, issue: IssueCase, plan: IssuePlan) -> tuple[bool, str]:
    """按计划的 verify 条件只读验证。任何一个明确条件不满足即失败。"""
    try:
        plan_data = json.loads(plan.plan_json or "{}")
    except ValueError:
        return False, "计划 JSON 损坏"
    steps = plan_data.get("steps") or []
    if not steps:
        return False, "计划没有可执行步骤"
    results = []
    for step in reversed(steps):
        recipe = step.get("recipe")
        verify = step.get("verify") or {}
        kind, expect_text = verify.get("kind"), verify.get("expect") or ""
        expect = _expect_map(expect_text)
        if recipe in ("retry_onboarding_capability", "refresh_cloud_assets",
                      "rediscover_logs", "revalidate_metric_ingest",
                      "recollect_cost_day"):
            ok, detail = _capability_ok(db, issue, expect)
        elif recipe in ("probe_integration", "reapply_account_endpoint"):
            ok, detail = _probe_ok(db, issue)
        elif recipe == "reseed_missing_alert_rules":
            ok, detail = _alert_rules_ok(db, issue)
        elif recipe in ("restart_platform", "reload_platform_settings"):
            ok, detail = _platform_health(db, expect.get("expect") or expect_text)
        elif recipe == "reset_own_issue_state":
            ok, detail = (issue.status not in ("repairing",),
                          f"问题单当前状态 {issue.status}")
        elif recipe == "patch_platform_code":
            from app.services.self_heal import code_repair
            ok, detail = code_repair.verify_patch(db, issue, plan)
        elif kind == "code_patch":
            from app.services.self_heal import code_repair
            ok, detail = code_repair.verify_patch(db, issue, plan)
        else:
            ok, detail = False, f"未知验证类型：{kind or recipe}"
        results.append(f"{recipe}: {'通过' if ok else '未通过'}（{detail}）")
        if not ok:
            return False, "；".join(results)
    return True, "；".join(results)
