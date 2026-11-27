"""重复提醒扫描：确认/忽略的问题到期后按级别重申。

用户确认/忽略一个问题后，按数据源优先级到期重申：

- high（默认）：`repeat_days` 为空 → 不重申，直到恢复或手动取消静默；
- medium：7 天后重申一次；low：30 天后重申一次；
- 账号 `repeat_days=0` → 永不重申（记录仍保留在列表页，可手动取消静默）。

到期时把问题重置为「未处理」（AlertRecord.ack_status=none /
CostAnomaly.ack_status=open）、按 (模块, 账号) 聚合成一条提醒，重新进入总览与
决策清单；`notify_count` 归零后重新计一次，保证低优先级「一次通知」语义不乱。

安全：只发一条聚合消息，逐组走 notify_all_channels（模块/账号去向全关就静默）。
绝不在此直接碰真实渠道——send 仍由 notify 统一出口决定。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.models.alert_record import AlertRecord
from app.models.cost_anomaly import CostAnomaly
from app.services.monitor_policy import module_of_anomaly, repeat_days

logger = logging.getLogger("qianshu.reminder")

# 异动只看近 7 天仍在复现的，避免对已消失的问题重申（与总览口径一致）
ANOMALY_LOOKBACK_DAYS = 7


def _clear_alert(rec: AlertRecord) -> None:
    rec.ack_status = "none"
    rec.ack_note = ""
    rec.ack_by = ""
    rec.ack_by_id = 0
    rec.ack_at = None
    rec.next_notify_at = None
    rec.notify_count = 0


def _clear_anomaly(a: CostAnomaly) -> None:
    a.ack_status = "open"
    a.ack_note = ""
    a.ack_by = ""
    a.ack_by_id = 0
    a.ack_at = None
    a.next_notify_at = None
    a.notify_count = 0


def _send_group(db: Session, module: str, account_id: int, lines: list[str],
                now: datetime) -> bool:
    """一组问题发一条聚合提醒；返回是否真的发出（站内信或外部渠道）。"""
    from app.services.notify import notify_all_channels

    level = "critical" if any("[CRITICAL]" in ln for ln in lines) else "warning"
    title = f"仍有 {len(lines)} 件你之前确认过的问题在持续"
    body = ("以下问题你确认/忽略过，按提醒策略到期重新提示；处理后可再次确认或忽略。\n\n"
            + "\n".join(lines))
    res = notify_all_channels(db, title, body, level=level,
                              module=module, account_id=account_id)
    if res.get("errors"):
        logger.warning("重复提醒发送失败 module=%s account=%s: %s",
                       module, account_id, res["errors"])
    return bool(res.get("sent"))


def run_reminders(db: Session, now: Optional[datetime] = None) -> dict:
    """扫描到期重申项，按 (模块, 账号) 聚合提醒。返回统计。"""
    now = now or datetime.now()
    out = {"alerts": 0, "anomalies": 0, "groups": 0, "notified": 0}
    touched = False

    alerts = (
        db.query(AlertRecord)
        .filter(
            AlertRecord.status == "firing",
            AlertRecord.ack_status.in_(("acknowledged", "ignored")),
            AlertRecord.next_notify_at.isnot(None),
            AlertRecord.next_notify_at <= now,
        )
        .all()
    )
    # 防配置变更残留：确认时算了 30 天，之后账号改成 repeat_days=0（永不），
    # 已排队的重申必须失效——以当前策略为准，不按旧快照打扰。
    for r in [x for x in alerts if not repeat_days(db, x.account_id)]:
        r.next_notify_at = None
        touched = True
    alerts = [x for x in alerts if repeat_days(db, x.account_id)]

    a_groups: dict[tuple[str, int], list[AlertRecord]] = {}
    for r in alerts:
        a_groups.setdefault(
            ((r.module or "alert"), int(r.account_id or 0)), []).append(r)

    for (module, account_id), items in a_groups.items():
        lines = [
            f"- [{'CRITICAL' if (r.level or '') == 'critical' else 'WARNING'}] "
            f"{r.title or r.rule_name} · 实例 {r.target}"
            for r in items
        ]
        sent = _send_group(db, module, account_id, lines, now)
        for r in items:
            _clear_alert(r)
            if sent:
                r.notify_count = 1
                r.last_notified_at = now
        out["alerts"] += len(items)
        out["groups"] += 1
        out["notified"] += 1 if sent else 0

    stale_since = date.today() - timedelta(days=ANOMALY_LOOKBACK_DAYS)
    anomalies = (
        db.query(CostAnomaly)
        .filter(
            CostAnomaly.ack_status.in_(("acknowledged", "ignored")),
            CostAnomaly.next_notify_at.isnot(None),
            CostAnomaly.next_notify_at <= now,
            CostAnomaly.last_seen >= stale_since,
        )
        .all()
    )
    for a in [x for x in anomalies if not repeat_days(db, x.account_id)]:
        a.next_notify_at = None
        touched = True
    anomalies = [x for x in anomalies if repeat_days(db, x.account_id)]

    c_groups: dict[tuple[str, int], list[CostAnomaly]] = {}
    for a in anomalies:
        c_groups.setdefault(
            (module_of_anomaly(a), int(getattr(a, "account_id", 0) or 0)),
            []).append(a)

    for (module, account_id), items in c_groups.items():
        lines = [
            f"- [{'CRITICAL' if (a.level or '') == 'critical' else 'WARNING'}] "
            f"{a.title}（{a.target_name or a.target_key}）"
            for a in items
        ]
        sent = _send_group(db, module, account_id, lines, now)
        for a in items:
            _clear_anomaly(a)
            if sent:
                a.notify_count = 1
                a.last_notified_at = now
        out["anomalies"] += len(items)
        out["groups"] += 1
        out["notified"] += 1 if sent else 0

    if touched or out["alerts"] or out["anomalies"]:
        db.commit()
        logger.info("重复提醒: 告警 %d / 异动 %d，聚合 %d 组，实际发出 %d 组",
                    out["alerts"], out["anomalies"], out["groups"], out["notified"])
    return out
