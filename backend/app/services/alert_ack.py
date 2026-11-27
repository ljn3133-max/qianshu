"""告警确认/忽略状态机。

正交于 `status`（firing/recovered）：
- `ack_status=none`       正常提醒；
- `acknowledged`/`ignored` 用户已处理，本轮不再打扰；
- `next_notify_at`        到期后由 reminder_job 重新提醒（None=不重申）。

重申间隔按数据源优先级：high 默认不重申（直到恢复），medium 7 天，low 30 天；
账号 `repeat_days` 覆盖默认，0 表示永不重申。恢复后下一次触发是新 episode，
ack 状态自然清空（新记录行）。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.services.monitor_policy import repeat_days

ACTIONS = {"acknowledge": "acknowledged", "ignore": "ignored"}
ACK_LABELS = {"none": "", "acknowledged": "已确认", "ignored": "已忽略"}


def compute_next_notify(db: Session, account_id: Optional[int],
                        now: Optional[datetime] = None) -> Optional[datetime]:
    """确认/忽略后的重申时间；None=不按天重申（直到恢复）。"""
    days = repeat_days(db, account_id)
    if not days:
        return None
    return (now or datetime.now()) + timedelta(days=int(days))


def apply_ack(db: Session, rec, action: str, note: str, user,
              now: Optional[datetime] = None) -> dict:
    """给 AlertRecord 打确认/忽略标记；调用方负责 audit + commit。"""
    status = ACTIONS.get(action)
    if status is None:
        raise ValueError(f"未知操作: {action}")
    now = now or datetime.now()
    rec.ack_status = status
    rec.ack_note = (note or "")[:2000]
    rec.ack_by = user.username
    rec.ack_by_id = user.id
    rec.ack_at = now
    rec.next_notify_at = compute_next_notify(db, rec.account_id, now)
    return ack_payload(rec)


def clear_ack(rec) -> dict:
    """取消静默：回到正常提醒。"""
    rec.ack_status = "none"
    rec.ack_note = ""
    rec.ack_by = ""
    rec.ack_by_id = 0
    rec.ack_at = None
    rec.next_notify_at = None
    return ack_payload(rec)


def ack_payload(rec) -> dict:
    return {
        "id": rec.id,
        "ack_status": rec.ack_status or "none",
        "ack_note": rec.ack_note or "",
        "ack_by": rec.ack_by or "",
        "ack_at": rec.ack_at.isoformat() if rec.ack_at else "",
        "next_notify_at": rec.next_notify_at.isoformat() if rec.next_notify_at else "",
    }
