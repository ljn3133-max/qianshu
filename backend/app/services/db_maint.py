"""数据保留与库维护。

无界增长表滚动清理：
- llm_call_logs   RETENTION_LLM_DAYS 天（调用明细，用量统计看近 90 天足够）
- audit_logs      RETENTION_AUDIT_DAYS 天（审计是留痕不是账，半年后价值极低）
- in_app_messages RETENTION_MSG_DAYS 天（站内信是通知不是账）

0 = 永久保留。每日 MAINT 时刻跑一次；诊断历史另有 prune_sessions（保留 50 条）不动这里。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

from sqlalchemy.orm import Session

from app.models.audit import AuditLog
from app.models.in_app_message import InAppMessage
from app.models.alert_record import AlertRecord as _AR
from app.models.cost_anomaly import CostAnomaly as _CA
from app.models.inspection import InspectionItem, InspectionRun
from app.models.inspection_report import InspectionReport
from app.models.recheck_task import RecheckTask
from app.models.change_event import ChangeEvent
from app.models.llm_call_log import LlmCallLog
from app.services import retention

logger = logging.getLogger("qianshu.db_maint")


def prune_old_data(db: Session) -> dict:
    """按保留期删除过旧行。返回 {表: 删除行数}。调用方负责 commit。

    只删"事件/记录"类：历史告警、已收口异动、旧巡检、变更流水、复查完成件。
    **不删**仍活着的（firing/open/pending）与**时序类**（cdn/dba_space/画像样本
    ——那是基线历史，且 域名×天 增长有界）。
    """
    today = date.today()
    deleted: dict = {}
    # 保留天数每次动态读取，保存后热生效，不使用启动时的初值
    event_days = retention.event_days()

    def _cut(days: int, is_date: bool):
        d = today - timedelta(days=days)
        return d if is_date else datetime.combine(d, time.min)

    # 1) 纯龄期表
    for model, col, days, name, is_date in [
        (LlmCallLog, LlmCallLog.call_date, retention.llm_days(), "llm_call_logs", True),
        (AuditLog, AuditLog.created_at, retention.audit_days(), "audit_logs", False),
        (InAppMessage, InAppMessage.created_at, retention.msg_days(), "in_app_messages", False),
        (ChangeEvent, ChangeEvent.event_time, event_days, "change_events", False),
        (InspectionReport, InspectionReport.report_date, event_days, "inspection_reports", True),
    ]:
        if days and days > 0:
            n = db.query(model).filter(col < _cut(days, is_date)).delete(synchronize_session=False)
            deleted[name] = n

    # 2) 带状态条件的表：只删已收口的
    if event_days > 0:
        n = (db.query(_AR).filter(
            _AR.status == "recovered", _AR.fired_at < _cut(event_days, False))
            .delete(synchronize_session=False))
        deleted["alert_records"] = n
        n = (db.query(_CA).filter(
            _CA.ack_status.in_(("resolved", "ignored")),
            _CA.detect_date < _cut(event_days, True))
            .delete(synchronize_session=False))
        deleted["cost_anomalies"] = n
        n = (db.query(RecheckTask).filter(
            RecheckTask.status == "done", RecheckTask.created_at < _cut(retention.msg_days(), False))
            .delete(synchronize_session=False))
        deleted["recheck_tasks"] = n
        # 巡检：items 随所属 run 级联删（先删 items 再删 run）
        old_runs = (db.query(InspectionRun.id)
                    .filter(InspectionRun.run_date < _cut(event_days, True)).all())
        old_ids = [r[0] for r in old_runs]
        if old_ids:
            n_items = (db.query(InspectionItem)
                       .filter(InspectionItem.run_id.in_(old_ids))
                       .delete(synchronize_session=False))
            (db.query(InspectionRun).filter(InspectionRun.id.in_(old_ids))
             .delete(synchronize_session=False))
            deleted["inspection_items"] = n_items
            deleted["inspection_runs"] = len(old_ids)
        else:
            deleted["inspection_items"] = 0
            deleted["inspection_runs"] = 0

    for name, n in deleted.items():
        if n:
            logger.info("保留策略: %s 清理 %d 行", name, n)
    return deleted
