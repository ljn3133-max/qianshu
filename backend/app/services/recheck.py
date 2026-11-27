"""处置复查执行：验证建议处置后的信号是否消除。

复查是**规则判定**不烧 LLM：按 kind 查当前信号是否已消——
- auto_diag：看相关事件（host_down/growth_accel/alert 等 target_key）是否仍 open/firing
- alert：alert_records 是否仍 firing
- anomaly：cost_anomalies 是否仍 open
结论站内信："已恢复（…）/ 仍未处理（当前 …）"。24h 后到期执行，每天一轮。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models.alert_record import AlertRecord
from app.models.cost_anomaly import CostAnomaly
from app.models.recheck_task import RecheckTask
from app.services.notify import notify_inapp

logger = logging.getLogger("qianshu.recheck")


def enqueue_recheck(db: Session, kind: str, target_key: str, title: str,
                    detail: str = "", hours: int = 24) -> None:
    db.add(RecheckTask(
        due_at=datetime.now() + timedelta(hours=hours),
        kind=kind, target_key=target_key, title=title[:255], detail=detail[:2000],
    ))


def _check_status(db: Session, target_key: str) -> tuple:
    """返回 (recovered: bool, 说明)。看该目标是否还有未已处理的信号。"""
    firing = (db.query(AlertRecord)
              .filter(AlertRecord.target == target_key, AlertRecord.status == "firing")
              .count())
    open_ev = (db.query(CostAnomaly)
               .filter(CostAnomaly.target_key == target_key,
                       CostAnomaly.ack_status.in_(("open", "acknowledged")))
               .count())
    if firing == 0 and open_ev == 0:
        return True, "相关告警/事件均已已处理"
    return False, f"仍有 {firing} 条告警触发中、{open_ev} 条事件未处理"


def run_rechecks(db: Session) -> dict:
    """执行到期复查任务，结论发站内信。"""
    due = (db.query(RecheckTask)
           .filter(RecheckTask.status == "pending", RecheckTask.due_at <= datetime.now())
           .all())
    done = 0
    for t in due:
        recovered, note = _check_status(db, t.target_key)
        verdict = "已恢复" if recovered else "仍未已处理"
        t.status = "done"
        t.done_at = datetime.now()
        t.result = f"{verdict}：{note}"
        body = (
            f"**复查结论：{verdict}**（事发 24h 后自动复查）\n\n"
            f"- 当时的问题：{t.title}\n"
            f"- 复查结果：{note}\n"
            f"- 处理：{'可以关单了' if recovered else '仍未已处理——继续处置或重新诊断'}"
        )
        notify_inapp(db, f"[复查] {t.title[:80]}", body, level="info",
                     msg_type="recheck", ref_id=t.id, audience="active")
        done += 1
    db.commit()
    return {"due": len(due), "done": done}
