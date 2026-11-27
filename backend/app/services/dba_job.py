"""RDS 空间每日采集任务：DescribeResourceUsage 快照落 dba_space_daily。

- 每日 DBA_COLLECT_HOUR:MINUTE 采一次（默认 09:40，错开成本 10:00）；
- 幂等：重拉同日按 (stat_date, instance_id) 覆盖；
- 单实例失败不连累其他实例（逐台记录错误）；
- 与成本采集同款互斥锁 + 状态字典模式。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.collector.credential import resolve_aliyun_credential
from app.collector.dba import fetch_space, list_rds_instances
from app.models.dba_space import DbaSpaceDaily

logger = logging.getLogger("qianshu.dba_job")

_run_lock = threading.Lock()
_last_status: dict = {
    "running": False,
    "last_started_at": None,
    "last_finished_at": None,
    "last_duration_s": None,
    "stat_date": None,
    "instances": 0,
    "errors": [],
}


def get_status() -> dict:
    return dict(_last_status)


def run_collection(db: Session, day: Optional[date] = None) -> dict:
    """采集所有 RDS 实例当日空间快照。day 缺省=今天（趋势只向前累积，不回填历史）。"""
    if not _run_lock.acquire(blocking=False):
        return {**dict(_last_status), "skipped": "上一轮空间采集仍在进行"}
    try:
        _last_status["running"] = True
        _last_status["last_started_at"] = datetime.now().isoformat()
        _last_status["errors"] = []
        started = time.monotonic()
        day = day or date.today()
        _last_status["stat_date"] = day.isoformat()

        ak, sk, _src = resolve_aliyun_credential(db)
        instances = list_rds_instances(db)
        ok = 0
        for inst in instances:
            try:
                usage = fetch_space(ak, sk, inst["region"], inst["instance_id"])
                db.query(DbaSpaceDaily).filter(
                    DbaSpaceDaily.stat_date == day,
                    DbaSpaceDaily.instance_id == inst["instance_id"],
                ).delete()
                db.add(DbaSpaceDaily(
                    stat_date=day,
                    instance_id=inst["instance_id"],
                    instance_name=inst["name"],
                    engine=inst["engine"],
                    **usage,
                ))
                db.commit()
                ok += 1
            except Exception as e:
                db.rollback()
                msg = f"{inst['instance_id']}: {type(e).__name__} {str(e)[:120]}"
                _last_status["errors"].append(msg)
                logger.warning("空间采集失败 %s", msg)

        _last_status["instances"] = ok
        _last_status["running"] = False
        _last_status["last_finished_at"] = datetime.now().isoformat()
        _last_status["last_duration_s"] = round(time.monotonic() - started, 1)
        logger.info(
            "空间采集完成 %s: %d/%d 实例, 错误 %d",
            day, ok, len(instances), len(_last_status["errors"]),
        )
        return dict(_last_status)
    finally:
        _run_lock.release()
