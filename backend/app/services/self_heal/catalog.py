"""固定动作目录：LLM 只能从这里选 recipe 和结构化参数。

新增动作必须同时加入本文件、validator 测试和执行器测试；页面/模型都不能动态新增。
风险级别由目录决定，模型自报的风险一律忽略。R4（任意命令/SQL/主机/外部写/删库/
外部通知/改权限与自愈核心）不在目录内，任何输出命中直接拒绝。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Recipe:
    name: str
    risk: str
    title: str
    description: str
    params_schema: dict
    timeout_s: int = 300
    verify_kind: str = "generic"
    verify_expect: str = "step_ok"
    rollback_kind: str = "none"
    automatic: bool = False        # 是否允许在 auto_r0 模式自动执行
    extra: dict = field(default_factory=dict)


def _obj(properties: dict, required: list | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required or []),
        "additionalProperties": False,
    }


_ACCOUNT = {"type": "integer", "min": 1, "max": 2147483647}
_RUN = {"type": "integer", "min": 0, "max": 2147483647}
_BILL_DATE = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$", "max_length": 10}
_SCOPE = _obj({
    "region": {"type": "string", "max_length": 64},
    "source_id": {"type": "string", "max_length": 200},
    "resource_ids": {"type": "array", "max_items": 50,
                     "items": {"type": "string", "max_length": 200}},
    "bill_date": _BILL_DATE,
}, [])

_CAPABILITY = {"type": "string", "enum": ["assets", "metrics", "logs", "cost", "changes"]}

RECIPES: dict[str, Recipe] = {
    "retry_onboarding_capability": Recipe(
        name="retry_onboarding_capability", risk="R0",
        title="重试接入能力",
        description="重试失败/未完成的单个接入能力，保留其他已成功能力证据。",
        params_schema=_obj({
            "account_id": _ACCOUNT, "run_id": _RUN, "capability": _CAPABILITY,
            "scope": _SCOPE,
        }, ["account_id", "capability"]),
        verify_kind="onboarding_capability", verify_expect="execution=succeeded",
        automatic=True,
    ),
    "refresh_cloud_assets": Recipe(
        name="refresh_cloud_assets", risk="R0",
        title="刷新云资产",
        description="只读重跑单个云账号的资产同步，单产品失败不影响其他产品。",
        params_schema=_obj({"account_id": _ACCOUNT}, ["account_id"]),
        verify_kind="onboarding_capability",
        verify_expect="capability=assets;execution=succeeded",
        automatic=True,
    ),
    "rediscover_logs": Recipe(
        name="rediscover_logs", risk="R0",
        title="重新发现日志",
        description="重新枚举 SLS Project/Logstore 并做只读采样分类。",
        params_schema=_obj({"account_id": _ACCOUNT}, ["account_id"]),
        verify_kind="onboarding_capability",
        verify_expect="capability=logs;execution=succeeded",
        automatic=True,
    ),
    "revalidate_metric_ingest": Recipe(
        name="revalidate_metric_ingest", risk="R0",
        title="重新回读指标",
        description="重新采集并回读指标落库证据，只读校验，不创建云端资源。",
        params_schema=_obj({"account_id": _ACCOUNT}, ["account_id"]),
        verify_kind="onboarding_capability",
        verify_expect="capability=metrics;execution=succeeded",
        automatic=True,
    ),
    "recollect_cost_day": Recipe(
        name="recollect_cost_day", risk="R0",
        title="重采账单日",
        description="重新采集指定账单日，单日范围，不使用真实外部写接口。",
        params_schema=_obj({"account_id": _ACCOUNT, "bill_date": _BILL_DATE},
                            ["account_id", "bill_date"]),
        verify_kind="onboarding_capability",
        verify_expect="capability=cost;execution=succeeded",
        automatic=True,
    ),
    "probe_integration": Recipe(
        name="probe_integration", risk="R0",
        title="只读连通探测",
        description="复用现有云账号连通测试，只读探测权限与可达性。",
        params_schema=_obj({"account_id": _ACCOUNT}, ["account_id"]),
        verify_kind="integration_probe", verify_expect="probe_ok",
        automatic=True,
    ),
    "reset_own_issue_state": Recipe(
        name="reset_own_issue_state", risk="R0",
        title="重置本问题单中断状态",
        description="只清理本问题单自己的运行中标记，不删除任何数据库数据。",
        params_schema=_obj({"issue_id": _ACCOUNT}, ["issue_id"]),
        verify_kind="issue_state", verify_expect="not_running",
        automatic=True,
    ),
    "reseed_missing_alert_rules": Recipe(
        name="reseed_missing_alert_rules", risk="R1",
        title="补种缺失的默认告警规则",
        description="仅在该账号没有任何默认规则时补种，不覆盖用户已有规则。",
        params_schema=_obj({"account_id": _ACCOUNT}, ["account_id"]),
        verify_kind="alert_rules", verify_expect="rules_present",
        automatic=True,
    ),
    "restart_platform": Recipe(
        name="restart_platform", risk="R2",
        title="重启千枢后端",
        description="仅通过本机 copilotd token 通道重启千枢后端进程；不动主机/系统参数。",
        params_schema=_obj({"reason": {"type": "string", "max_length": 200}}),
        timeout_s=120, verify_kind="platform_health", verify_expect="restarted",
        rollback_kind="none", automatic=False,
    ),
    "reload_platform_settings": Recipe(
        name="reload_platform_settings", risk="R2",
        title="重载平台内设置",
        description="只重载平台内固定配置缓存，不修改数据库、不重启主机。",
        params_schema=_obj({
            "keys": {"type": "array", "max_items": 3,
                     "items": {"type": "string",
                               "enum": ["monitor_policy", "scheduler_state"]}},
        }, ["keys"]),
        verify_kind="platform_health", verify_expect="settings_reloaded",
        automatic=False,
    ),
    "reapply_account_endpoint": Recipe(
        name="reapply_account_endpoint", risk="R2",
        title="修正平台内账号 endpoint",
        description="只改千枢平台库中指定账号的 endpoint 字符串，不调用云侧写接口。",
        params_schema=_obj({
            "account_id": _ACCOUNT,
            "endpoint": {"type": "string", "max_length": 255,
                         "pattern": r"^[A-Za-z0-9._:-]+$"},
        }, ["account_id", "endpoint"]),
        verify_kind="integration_probe", verify_expect="probe_ok",
        automatic=False,
    ),
    "patch_platform_code": Recipe(
        name="patch_platform_code", risk="R3",
        title="修改千枢平台代码",
        description=("通过现有 code_mod/change_gate 修改允许路径下的平台代码；"
                     "只改不删，最多 5 文件/200 行，验证失败自动回滚。"),
        params_schema=_obj({
            "plan": {"type": "object"},
        }, ["plan"]),
        timeout_s=300, verify_kind="code_patch", verify_expect="applied",
        rollback_kind="code_snapshot", automatic=False,
    ),
}


# 出现即视为 R4/越权（无论出现在哪个层级的键或字符串参数中）。
FORBIDDEN_KEYS = {
    "shell", "bash", "sh", "cmd", "command", "commands", "exec", "execute",
    "sql", "dml", "ddl", "promql", "http", "https", "url", "uri", "request",
    "path", "file", "filepath", "filename", "systemctl", "service", "reboot",
    "shutdown", "kill", "sysctl", "iptables", "firewall", "crontab", "ssh",
    "docker", "podman", "kubernetes", "kubectl", "ak", "sk", "access_key",
    "secret", "password", "token", "private_key", "wecom", "dingtalk", "sms",
    "phone", "email", "delete", "drop", "truncate", "grant", "permission",
}
FORBIDDEN_FRAGMENTS = (
    "rm -rf", "systemctl", "service ", "reboot", "shutdown", "drop table",
    "truncate table", "delete from", "update ", "insert into", "grant ",
    "chmod ", "chown ", "curl ", "wget ", "bash -c", "sh -c", "eval(",
    "exec(", "subprocess", "os.system", "docker run", "kubectl ", "ssh ",
)
FORBIDDEN_PATH_FRAGMENTS = ("..", "\x00", "%2e%2e", "%00")


def get(name: str) -> Recipe | None:
    return RECIPES.get((name or "").strip())


def risk_of(name: str) -> str | None:
    recipe = get(name)
    return recipe.risk if recipe else None


def catalog_prompt() -> str:
    rows = []
    for r in RECIPES.values():
        rows.append(f"- {r.name} ({r.risk}): {r.description}")
    return "\n".join(rows)


def names_by_risk(risk: str) -> list[str]:
    return [r.name for r in RECIPES.values() if r.risk == risk]


def sanitize_plan_step(recipe: Recipe, params: dict, reason: str = "",
                       expected_effect: str = "") -> dict[str, Any]:
    return {
        "recipe": recipe.name,
        "params": params,
        "risk": recipe.risk,
        "reason": (reason or recipe.description)[:1000],
        "expected_effect": (expected_effect or recipe.verify_expect)[:500],
        "verify": {"kind": recipe.verify_kind, "expect": recipe.verify_expect},
        "rollback": {"kind": recipe.rollback_kind},
        "timeout_s": recipe.timeout_s,
    }
