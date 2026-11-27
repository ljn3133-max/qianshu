"""保留天数动态解析：配置保存后各 worker 下次取值即生效。

所有保留天数消费处（清理循环/接口/查询口径）逐次经本模块取值，**不读 config 初值快照**：
保存（PUT /settings/retention）写 .env（env_store）+ 更新 settings 单例后，任一
worker 的清理循环下次取值即见新值（热生效；compose 4 worker 下 leader 不必是
保存落点）。取值优先级：.env 当前值 > settings（启动时 env>.env 口径）。

实时生效两层（docs/active/deployment.md）：
- 查询口径：clamp_start_ts() 把读路径的查询下界夹到保留窗口（指标/日志读路径都过这里）；
- 渐进清理：services/engine_retention.py 按 effective_days() 删超期数据。
上限层：vm/vl 另有部署时定的引擎上限（cap_days，=容器 -retentionPeriod），
effective_days() = min(配置, 上限)；配置 0=永久（至引擎上限）。

注意：环境变量显式注入的保留键在**重启后**仍会按 pydantic 口径盖回 .env 的 UI 保存值
（启动口径 env>.env）；产品推荐一律用 系统设置-系统参数 / .env 配置，不用环境变量。
0 = 永久保留。
"""
from __future__ import annotations

import time

from app.core.config import settings
from app.services.env_store import get_key

# 名称 -> (env 键, settings 兜底属性)
_KEYS = {
    "vm": ("VM_RETENTION_DAYS", "VM_RETENTION_DAYS"),
    "vl": ("VL_RETENTION_DAYS", "VL_RETENTION_DAYS"),
    "k8s": ("K8S_LOG_RETENTION_DAYS", "K8S_LOG_RETENTION_DAYS"),
    "llm": ("RETENTION_LLM_DAYS", "RETENTION_LLM_DAYS"),
    "audit": ("RETENTION_AUDIT_DAYS", "RETENTION_AUDIT_DAYS"),
    "msg": ("RETENTION_MSG_DAYS", "RETENTION_MSG_DAYS"),
    "event": ("RETENTION_EVENT_DAYS", "RETENTION_EVENT_DAYS"),
}


def days(name: str) -> int:
    """按名称取当前保留天数（0=永久）。非法/缺失回 settings 值。"""
    env_key, attr = _KEYS[name]
    raw = get_key(env_key)
    if raw is not None and str(raw).strip() != "":
        try:
            return max(0, int(str(raw).strip()))
        except ValueError:
            pass
    try:
        return max(0, int(getattr(settings, attr)))
    except (TypeError, ValueError):
        return 0


def vm_days() -> int:
    return days("vm")


def vl_days() -> int:
    return days("vl")


def k8s_days() -> int:
    return days("k8s")


def llm_days() -> int:
    return days("llm")


def audit_days() -> int:
    return days("audit")


def msg_days() -> int:
    return days("msg")


def event_days() -> int:
    return days("event")


def all_values() -> dict:
    """系统参数页回显：{vm_days, vl_days, k8s_days, llm_days, audit_days, msg_days, event_days}。"""
    return {f"{n}_days": days(n) for n in
            ("vm", "vl", "k8s", "llm", "audit", "msg", "event")}


# ---------------- 上限层 + 查询口径（存储保留实时生效） ----------------

def cap_days(name: str) -> int:
    """引擎上限（部署时定，=容器 -retentionPeriod）：vm/vl 有上限，0=未设上限。"""
    attr = {"vm": "VM_RETENTION_MAX_DAYS", "vl": "VL_RETENTION_MAX_DAYS"}.get(name)
    if not attr:
        return 0
    try:
        return max(0, int(getattr(settings, attr)))
    except (TypeError, ValueError):
        return 0


def caps() -> dict:
    """系统参数页回显上限：{vm: n, vl: n}（0=无上限）。"""
    return {"vm": cap_days("vm"), "vl": cap_days("vl")}


def effective_days(name: str) -> int:
    """实际生效保留天数 = min(用户配置, 引擎上限)；0=永久（至引擎上限）。"""
    d = days(name)
    c = cap_days(name)
    return min(d, c) if (c > 0 and d > 0) else d


def clamp_start_ts(name: str, start_s: float) -> float:
    """查询口径（实时生效）：把查询下界抬到保留窗口（now - effective_days）。

    0=永久不夹；夹出空窗（start >= end）由调用方短路返回空结果。
    所有 VM 读（collector/vm.query_range）与库表日志读都过这里——设置一改，
    下一次查询立即按新窗口走，无需重启任何服务。
    """
    d = effective_days(name)
    if d <= 0:
        return start_s
    return max(float(start_s), time.time() - d * 86400)
