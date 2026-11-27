"""自愈开关、模式与角色边界。

默认 observe：问题可见、可人工 R0 重试，但不调用 LLM、不自动执行；管理员通过
环境变量逐级打开 assist → auto_r0 → approved_r2 → approved_r3。R2/R3 永远不自动。
"""
from __future__ import annotations

from app.core.config import settings

MODES = ("observe", "assist", "auto_r0", "approved_r2", "approved_r3")
_MODE_RANK = {name: i for i, name in enumerate(MODES)}


def mode() -> str:
    value = (getattr(settings, "SELF_HEAL_MODE", "observe") or "observe").strip().lower()
    return value if value in _MODE_RANK else "observe"


def enabled() -> bool:
    return bool(getattr(settings, "SELF_HEAL_ENABLED", True)) and not bool(
        getattr(settings, "SELF_HEAL_KILL_SWITCH", False))


def mode_at_least(required: str) -> bool:
    return _MODE_RANK.get(mode(), 0) >= _MODE_RANK.get(required, 99)


def llm_allowed() -> bool:
    return enabled() and bool(getattr(settings, "SELF_HEAL_LLM_ENABLED", False)) \
        and mode_at_least("assist")


def auto_risk_allowed(risk: str) -> bool:
    """自动执行只允许 R0/R1，且必须显式打开 auto_r0。"""
    return enabled() and risk in ("R0", "R1") and mode_at_least("auto_r0")


def approve_role_allowed(user) -> bool:
    return getattr(user, "role", "") == "admin"


def execute_role_allowed(user, risk: str) -> bool:
    role = getattr(user, "role", "")
    if risk in ("R0", "R1"):
        return role in ("admin", "operator")
    return role == "admin"


def can_execute(risk: str, *, automatic: bool, user=None) -> bool:
    if not enabled():
        return False
    if automatic:
        return auto_risk_allowed(risk)
    if risk in ("R0", "R1"):
        return execute_role_allowed(user, risk)
    if risk == "R2":
        return enabled() and mode_at_least("approved_r2") and approve_role_allowed(user)
    if risk == "R3":
        return enabled() and mode_at_least("approved_r3") and approve_role_allowed(user)
    return False


def can_generate_plan(risk: str) -> bool:
    """敏感风险可以生成“待授权计划”，但执行能力由 can_execute + 授权时间再验。"""
    if not enabled() or risk == "R4" or risk not in ("R0", "R1", "R2", "R3"):
        return False
    if risk in ("R2",):
        return mode_at_least("approved_r2")
    if risk in ("R3",):
        return mode_at_least("approved_r3")
    return True


def auto_plan_allowed(risk: str) -> bool:
    return auto_risk_allowed(risk)
