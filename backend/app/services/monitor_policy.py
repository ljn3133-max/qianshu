"""按数据源分级监控/提醒的唯一策略入口。

两个正交轴：

1. **模块提醒开关**（`monitor_module_settings`）：这个模块的事能不能出现在
   总览待办 / 站内信 / 外部渠道。关闭只停提醒，不停采集。
2. **数据源策略**（`cloud_accounts.priority` + 三个去向 + `repeat_days`）：
   不同环境重要程度不同；优先级决定采集/评估节奏和确认后的重申间隔。

最终去向 = 模块开关 AND 账号开关（账号级模块）。任何需要判断「该不该提醒/该不该
现在采」的地方都只调本模块，不各自复制规则。

逃生门：`MONITOR_POLICY_ENABLED=false` 时所有 gate 返回 True、所有节奏回到固定间隔，
行为与升级前一致。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.cloud_account import CloudAccount
from app.models.monitor_policy import MonitorModuleSetting

logger = logging.getLogger("qianshu.monitor_policy")

PRIORITIES = ("high", "medium", "low")
PRIORITY_LABELS = {"high": "高（核心）", "medium": "中（标准）", "low": "低（辅助）"}
DESTINATIONS = ("overview", "inapp", "push")
DEST_LABELS = {"overview": "总览页", "inapp": "站内信", "push": "外部推送"}


@dataclass(frozen=True)
class ModuleSpec:
    key: str
    name: str
    scope: str                  # account / global
    destinations: tuple[str, ...]
    desc: str


# 模块清单唯一事实源：前端的矩阵从 GET /policy/overview 取，不在前端硬编码。
# 只列「今天真的有提醒出口」的模块；K8s/DBA/数据源连通性没有通知出口，不做空开关。
MODULES: dict[str, ModuleSpec] = {
    "alert": ModuleSpec(
        "alert", "指标/SLS 告警", "account", ("overview", "inapp", "push"),
        "指标规则与 SLS 规则越限（alert_eval）"),
    "host": ModuleSpec(
        "host", "主机看护", "account", ("overview", "inapp", "push"),
        "关机、非运行状态、辅助机器 5xx 外溢（host_watch）"),
    "cost": ModuleSpec(
        "cost", "成本异动", "account", ("overview", "inapp", "push"),
        "按量突增、资源包消耗/耗尽/到期（cost_anomaly）"),
    "cdn": ModuleSpec(
        "cdn", "CDN/域名流量", "account", ("overview", "inapp", "push"),
        "域名流量、磁盘空间增速异常（cost_anomaly: traffic_spike/growth_accel）"),
    "profile": ModuleSpec(
        "profile", "画像漂移", "global", ("overview", "inapp", "push"),
        "行为构成变化、链路多层数据不匹配（cost_anomaly: profile_drift/chain_drift）"),
    "expiry": ModuleSpec(
        "expiry", "到期提醒", "account", ("overview",),
        "实例/域名/证书/资源包到期（当前只进总览与决策清单，无独立发送）"),
    "inspection": ModuleSpec(
        "inspection", "巡检日报", "global", ("inapp", "push"),
        "每日巡检报告与定时工作流体检（report/agent_schedule）"),
    "digest": ModuleSpec(
        "digest", "今日决策清单", "global", ("inapp", "push"),
        "每天一条聚合清单（decision_digest）"),
    "synth": ModuleSpec(
        "synth", "拨测", "global", ("overview", "inapp", "push"),
        "HTTP/TCP/DNS 拨测失败（synth）"),
    "oncall": ModuleSpec(
        "oncall", "值班升级链", "global", ("inapp", "push"),
        "critical 告警超时未认领逐级通知（oncall）"),
    "object_profile": ModuleSpec(
        "object_profile", "对象画像观测", "global", ("inapp",),
        "每日对象全景画像观测报告（object_profile）"),
    "calibration": ModuleSpec(
        "calibration", "校准周报", "global", ("inapp", "push"),
        "每周一『我学到的』确认清单（calibration）"),
}

# 异动 type -> 模块。与 cost_anomaly.ANOMALY_TYPE_DOMAINS 同源，但不在
# monitor_policy 里反向 import cost_anomaly（后者会 import 本模块，避免循环）。
_ANOMALY_MODULE = {
    "profile_drift": "profile",
    "chain_drift": "profile",
    "traffic_spike": "cdn",
    "growth_accel": "cdn",
    "host_down": "host",
    "aux_spillover": "host",
}

# 重复提醒默认值：None=不按天重申（高优先级保持现状，直到恢复）
_REPEAT_DEFAULT = {"high": None, "medium": 7, "low": 30}

# 固定间隔型节奏（秒）。high 用现有 settings 值，medium/low 用产品定义值。
_MEDIUM_CADENCE = {
    "metrics": 300,
    "alert_eval": 300,
    "host": 1800,
    "cost": 7200,
    "assets": 43200,
}

_LOW_METRIC_WINDOW = (time(2, 0), time(3, 0))
_LOW_EVAL_WINDOW = (time(2, 0), time(3, 5))
_LOW_HOST_AT = time(3, 20)
_LOW_COST_AT = time(3, 30)
_LOW_ASSETS_AT = time(4, 0)
_LOW_SAMPLE_GAP = 300


def module_of_anomaly(a) -> str:
    """CostAnomaly -> 提醒模块。未知类型归成本域，保持可提醒。"""
    return _ANOMALY_MODULE.get(getattr(a, "type", ""), "cost")


def _row(db: Session, module: str) -> Optional[MonitorModuleSetting]:
    return db.get(MonitorModuleSetting, module)


def ensure_defaults(db: Session) -> int:
    """幂等补齐 12 个模块行（新库 create_all 不会执行 update-data 的种子 SQL）。"""
    added = 0
    for key in MODULES:
        if db.get(MonitorModuleSetting, key) is None:
            db.add(MonitorModuleSetting(module=key))
            added += 1
    if added:
        try:
            db.commit()
        except Exception:
            # 启动瞬间多 worker 并发补种子时 PK 冲突：以库里已有的行为准
            db.rollback()
            logger.warning("监控模块种子并发补齐冲突，回退读库", exc_info=True)
            return 0
    return added


def _account(db: Session, account_id: Optional[int]) -> Optional[CloudAccount]:
    if not account_id:
        return None
    return db.get(CloudAccount, int(account_id))


def _enabled(dest: str, module: str, account_id: Optional[int],
             db: Session) -> bool:
    if not settings.MONITOR_POLICY_ENABLED:
        return True
    spec = MODULES.get(module)
    if spec is None:
        return True                     # 未登记的模块不拦，保持旧行为
    if dest not in spec.destinations:
        return False                    # 该模块没有这个出口，不允许误发
    row = _row(db, module)
    if row is not None:
        if not row.enabled:
            return False
        if not getattr(row, dest, True):
            return False
    # 账号级模块：再 AND 账号自己的去向开关
    if spec.scope == "account":
        acc = _account(db, account_id)
        if acc is not None and not getattr(acc, f"notify_{dest}", True):
            return False
    return True


def module_enabled(db: Session, module: str) -> bool:
    """模块提醒总开关是否开启（不含出口能力判断）。

    用于「统计卡/概览数字」这类不占具体去向的场景：模块没有 overview 出口
    （如巡检日报只有站内信/外推）时不能拿 can_overview 判，否则永远为假。
    """
    if not settings.MONITOR_POLICY_ENABLED:
        return True
    if module not in MODULES:
        return True
    row = _row(db, module)
    return True if row is None else bool(row.enabled)


def can_overview(db: Session, module: str, account_id: Optional[int] = None) -> bool:
    return _enabled("overview", module, account_id, db)


def can_inapp(db: Session, module: str, account_id: Optional[int] = None) -> bool:
    return _enabled("inapp", module, account_id, db)


def can_push(db: Session, module: str, account_id: Optional[int] = None) -> bool:
    return _enabled("push", module, account_id, db)


def repeat_days(db: Session, account_id: Optional[int]) -> Optional[int]:
    """确认/忽略后的重申天数。None=不按天重申（直到恢复或永久静默）。"""
    acc = _account(db, account_id)
    priority = getattr(acc, "priority", None) or "high"
    override = getattr(acc, "repeat_days", None)
    if override is not None:
        return None if int(override) <= 0 else int(override)
    return _REPEAT_DEFAULT.get(priority, None)


def priority_of(db: Session, account_id: Optional[int]) -> str:
    acc = _account(db, account_id)
    p = getattr(acc, "priority", None) or "high"
    return p if p in PRIORITIES else "high"


def cadence_seconds(db: Session, job: str, account_id: Optional[int]) -> Optional[int]:
    """返回该账号该 job 的固定间隔秒数；low 的日窗口型 job 返回 None。"""
    p = priority_of(db, account_id)
    if p == "high":
        if job == "metrics":
            return max(1, int(settings.METRICS_COLLECT_INTERVAL))
        if job == "alert_eval":
            return max(1, int(settings.ALERT_EVAL_INTERVAL))
        if job == "host":
            return max(60, int(settings.HOST_WATCH_INTERVAL_MIN) * 60)
        if job == "cost":
            return 1800
        if job == "assets":
            return max(1, int(settings.CLOUD_SYNC_INTERVAL_S))
    if p == "medium":
        return _MEDIUM_CADENCE.get(job)
    return None      # low 走日窗口/每日一次


def _in_window(now: datetime, start: time, end: time) -> bool:
    return start <= now.time() < end


def _daily_due(last: Optional[datetime], now: datetime, at: time) -> bool:
    if last is None:
        return True
    if last.date() >= now.date():
        return False
    if now.time() >= at:
        return True
    # 已错过一整天（例如停机）→ 立即补一次，不必等下一个窗口
    return last.date() < now.date() - timedelta(days=1)


def _low_due(job: str, last: Optional[datetime], now: datetime) -> bool:
    if last is None:
        return True
    if job == "metrics":
        if _in_window(now, *_LOW_METRIC_WINDOW):
            return (now - last).total_seconds() >= _LOW_SAMPLE_GAP
        if now.time() >= _LOW_METRIC_WINDOW[1]:
            return last.date() < now.date()
        return False
    if job == "alert_eval":
        if _in_window(now, *_LOW_EVAL_WINDOW):
            return (now - last).total_seconds() >= _LOW_SAMPLE_GAP
        if now.time() >= _LOW_EVAL_WINDOW[1]:
            return last.date() < now.date()
        return False
    if job == "host":
        return _daily_due(last, now, _LOW_HOST_AT)
    if job == "cost":
        return _daily_due(last, now, _LOW_COST_AT)
    if job == "assets":
        return _daily_due(last, now, _LOW_ASSETS_AT)
    return True


def _last_at(acc: CloudAccount, job: str) -> Optional[datetime]:
    if job == "metrics":
        return acc.last_metrics_at
    if job == "alert_eval":
        return acc.last_alert_eval_at
    if job == "host":
        return acc.last_host_check_at
    if job == "cost":
        return acc.last_cost_at
    if job == "assets":
        return acc.last_sync_at
    return None


def collect_due(db: Session, job: str, account_id: Optional[int],
                force: bool = False, now: Optional[datetime] = None) -> bool:
    """该账号的该 job 现在是否该跑。

    force=True（用户手动触发）永远为真；无账号上下文（全局/旧凭证兜底）也视为该跑。
    last_* 为空一律视为立即到期，避免新账号/新装机等到下一个窗口。
    """
    if force or not settings.MONITOR_POLICY_ENABLED:
        return True
    if not account_id:
        return True
    acc = _account(db, account_id)
    if acc is None:
        return True
    p = priority_of(db, account_id)
    # 高优先级：完全沿用现有循环周期，不做二次到期判断（保证升级前后逐字节一致，
    # 也避免「循环 sleep 与游标间隔几乎相等」造成的偶发跳过）
    if p == "high":
        return True
    now = now or datetime.now()
    last = _last_at(acc, job)
    if last is None:
        return True
    if p == "low":
        return _low_due(job, last, now)
    interval = cadence_seconds(db, job, account_id)
    if interval is None:
        return True
    return (now - last).total_seconds() >= interval


def mark_collected(db: Session, job: str, account_id: Optional[int],
                   now: Optional[datetime] = None) -> None:
    """回写采集游标；不 commit，由调用方统一提交。"""
    if not account_id:
        return
    acc = _account(db, account_id)
    if acc is None:
        return
    now = now or datetime.now()
    if job == "metrics":
        acc.last_metrics_at = now
    elif job == "alert_eval":
        acc.last_alert_eval_at = now
    elif job == "host":
        acc.last_host_check_at = now
    elif job == "cost":
        acc.last_cost_at = now
    elif job == "assets":
        acc.last_sync_at = now


def cadence_label(db: Session, account_id: Optional[int]) -> str:
    """给页面的一行可读节奏说明（不暴露任意秒数输入）。"""
    p = priority_of(db, account_id)
    if p == "high":
        return (f"指标 {settings.METRICS_COLLECT_INTERVAL // 60} 分钟 / "
                f"评估 {settings.ALERT_EVAL_INTERVAL} 秒 / "
                f"主机 {settings.HOST_WATCH_INTERVAL_MIN} 分钟 / "
                f"资产 {settings.CLOUD_SYNC_INTERVAL_S // 3600} 小时（现状）")
    if p == "medium":
        return "指标 5 分钟 / 评估 5 分钟 / 主机 30 分钟 / 成本 2 小时 / 资产 12 小时"
    return "每天 02:00–03:00 采样 / 主机 03:20 / 成本 03:30 / 资产 04:00"


def modules_payload(db: Session) -> list[dict]:
    ensure_defaults(db)
    out = []
    for key, spec in MODULES.items():
        row = _row(db, key)
        out.append({
            "module": key,
            "name": spec.name,
            "scope": spec.scope,
            "desc": spec.desc,
            "destinations": list(spec.destinations),
            "enabled": True if row is None else bool(row.enabled),
            "overview": True if row is None else bool(row.overview),
            "inapp": True if row is None else bool(row.inapp),
            "push": True if row is None else bool(row.push),
            "updated_at": (row.updated_at.isoformat() if row and row.updated_at else ""),
        })
    return out


def account_payload(acc: CloudAccount, db: Session) -> dict:
    return {
        "id": acc.id,
        "name": acc.name,
        "vendor": acc.vendor,
        "enabled": bool(acc.enabled),
        "priority": acc.priority or "high",
        "notify_overview": bool(acc.notify_overview),
        "notify_inapp": bool(acc.notify_inapp),
        "notify_push": bool(acc.notify_push),
        "repeat_days": acc.repeat_days,
        "repeat_effective": repeat_days(db, acc.id),
        "cadence": cadence_label(db, acc.id),
        "last_metrics_at": acc.last_metrics_at.isoformat() if acc.last_metrics_at else "",
        "last_alert_eval_at": (acc.last_alert_eval_at.isoformat()
                                if acc.last_alert_eval_at else ""),
        "last_host_check_at": (acc.last_host_check_at.isoformat()
                               if acc.last_host_check_at else ""),
        "last_cost_at": acc.last_cost_at.isoformat() if acc.last_cost_at else "",
        "last_sync_at": acc.last_sync_at.isoformat() if acc.last_sync_at else "",
    }


def priority_defaults() -> dict:
    return {
        "high": {"repeat_days": None, "label": PRIORITY_LABELS["high"],
                 "cadence": "指标 5 分钟 / 评估 1 分钟 / 主机 10 分钟 / 资产 6 小时"},
        "medium": {"repeat_days": 7, "label": PRIORITY_LABELS["medium"],
                   "cadence": "指标 5 分钟 / 评估 5 分钟 / 主机 30 分钟 / 资产 12 小时"},
        "low": {"repeat_days": 30, "label": PRIORITY_LABELS["low"],
                "cadence": "每天 02:00–03:00 采样 / 其余每日一次"},
    }
