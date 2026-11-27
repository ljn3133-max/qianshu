"""DBA 专项 API：RDS 慢SQL / SQL洞察明细 / 空间趋势 / 备份状态，全部只读查询 +
每日空间采集任务（手动触发写审计）。实例白名单来自资产表，不接受任意 instance_id。"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.collector.credential import CredentialError, resolve_aliyun_credential
from app.collector.dba import (
    fetch_backups,
    fetch_big_keys,
    fetch_hot_keys,
    fetch_insight_records,
    fetch_slowlog_records,
    fetch_slowlog_top,
    fetch_space,
    get_sql_insight,
    list_rds_instances,
    list_redis_instances,
)
from app.core.db import get_db
from app.core.deps import require_roles
from app.models.dba_space import DbaSpaceDaily
from app.models.user import User
from app.services import audit
from app.services import dba_job

logger = logging.getLogger("qianshu.dba_api")
router = APIRouter(prefix="/dba", tags=["dba"])

# 未配置阿里云凭证属于私有化/新装常态：页加载类接口回 200 空态和可行动说明，
# 深层查询回 503 并带接入指引，不能把 CredentialError 裸抛成 500。
_NO_CREDS_NOTE = ("未配置阿里云凭证：请到「云账号管理」添加只读账号，"
                  "或配置环境变量 ALIBABA_CLOUD_ACCESS_KEY_ID / ALIBABA_CLOUD_ACCESS_KEY_SECRET")


def _creds_or_none(db: Session):
    try:
        return resolve_aliyun_credential(db)
    except CredentialError:
        return None


def _get_instance(db: Session, instance_id: str) -> dict:
    """从资产表取实例信息；不存在则 404（防越权查任意 ID）。"""
    for inst in list_rds_instances(db):
        if inst["instance_id"] == instance_id:
            return inst
    raise HTTPException(404, f"实例不存在或未同步: {instance_id}")


@router.get("/instances")
def instances(
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """RDS 实例列表 + SQL 洞察开关状态（10 分钟缓存）。"""
    creds = _creds_or_none(db)
    if creds is None:
        return {"instances": list_rds_instances(db), "note": _NO_CREDS_NOTE, "no_credential": True}
    ak, sk, _src = creds
    out = []
    for inst in list_rds_instances(db):
        # 经典洞察明细仅 MySQL 支持：PG 不让前端把它当可选（见 insight_records 同口径说明）
        if inst["engine"].lower().startswith("mysql"):
            inst["sql_insight"] = get_sql_insight(ak, sk, inst["region"], inst["instance_id"])
        else:
            inst["sql_insight"] = False
        out.append(inst)
    return {"instances": out}


@router.get("/slowlogs")
def slowlogs(
    instance_id: str = Query(min_length=1),
    days: int = Query(7, ge=1, le=7),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """慢SQL Top（聚合视图）。MySQL: RDS 聚合接口；PG: DAS 明细按模板聚合。"""
    inst = _get_instance(db, instance_id)
    creds = _creds_or_none(db)
    if creds is None:
        raise HTTPException(503, _NO_CREDS_NOTE)
    ak, sk, _src = creds
    try:
        rows = fetch_slowlog_top(ak, sk, inst["region"], instance_id, inst["engine"], days)
    except Exception as e:
        logger.exception("慢SQL Top 查询失败 %s", instance_id)
        raise HTTPException(502, f"慢SQL查询失败: {str(e)[:200]}")
    return {"instance_id": instance_id, "engine": inst["engine"], "days": days, "rows": rows}


@router.get("/slowlog-records")
def slowlog_records(
    instance_id: str = Query(min_length=1),
    hours: int = Query(24, ge=1, le=24 * 7),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    sql_hash: str = Query("", max_length=300),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """慢SQL明细（分页，时间窗=近 N 小时）。传 sql_hash 只看该模板的记录
    （MySQL=RDS 模板哈希服务端过滤；PG=归一化模板键客户端过滤）。"""
    inst = _get_instance(db, instance_id)
    creds = _creds_or_none(db)
    if creds is None:
        raise HTTPException(503, _NO_CREDS_NOTE)
    ak, sk, _src = creds
    end = datetime.now()
    start = end - timedelta(hours=hours)
    try:
        r = fetch_slowlog_records(ak, sk, inst["region"], instance_id, inst["engine"],
                                  start, end, page, page_size, sql_hash=sql_hash)
    except Exception as e:
        logger.exception("慢SQL明细查询失败 %s", instance_id)
        raise HTTPException(502, f"慢SQL明细查询失败: {str(e)[:200]}")
    return {"instance_id": instance_id, "page": page, "page_size": page_size, **r}


@router.get("/insight/records")
def insight_records(
    instance_id: str = Query(min_length=1),
    hours: int = Query(1, ge=1, le=24),
    keyword: str = Query("", max_length=64),
    page: int = Query(1, ge=1),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """SQL 洞察明细查询（仅洞察已开启实例）。经典洞察无聚合 TopSQL API，此为替代视图。"""
    inst = _get_instance(db, instance_id)
    creds = _creds_or_none(db)
    if creds is None:
        raise HTTPException(503, _NO_CREDS_NOTE)
    # 经典 SQL 审计明细（DescribeSQLLogRecords）只有 MySQL；PostgreSQL 的
    # SQLCollectorStatus 即使启用，调用也会 403 ClusterNotSupport，
    # 因此仅 MySQL 开放该查询，避免 PG 页面进入必然失败的请求。
    if not inst["engine"].lower().startswith("mysql"):
        raise HTTPException(400, "PostgreSQL 不支持经典 SQL 洞察明细（仅 MySQL）；请用「慢SQL」或「空间趋势」页签")
    ak, sk, _src = creds
    if not get_sql_insight(ak, sk, inst["region"], instance_id):
        raise HTTPException(400, "该实例未开启 SQL 洞察（成本原因暂不开启），明细不可用")
    end = datetime.now()
    start = end - timedelta(hours=hours)
    try:
        r = fetch_insight_records(ak, sk, inst["region"], instance_id, start, end, keyword, page)
    except Exception as e:
        logger.exception("洞察明细查询失败 %s", instance_id)
        raise HTTPException(502, f"洞察明细查询失败: {str(e)[:200]}")
    return {"instance_id": instance_id, "page": page, **r}


@router.get("/space")
def space(
    instance_id: str = Query(min_length=1),
    days: int = Query(30, ge=1, le=90),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """空间：当前实时值 + 每日快照趋势（自采集启用日起累积）。"""
    inst = _get_instance(db, instance_id)
    creds = _creds_or_none(db)
    if creds is None:
        raise HTTPException(503, _NO_CREDS_NOTE)
    ak, sk, _src = creds
    try:
        current = fetch_space(ak, sk, inst["region"], instance_id)
    except Exception as e:
        logger.exception("空间查询失败 %s", instance_id)
        raise HTTPException(502, f"空间查询失败: {str(e)[:200]}")
    rows = (
        db.query(DbaSpaceDaily)
        .filter(DbaSpaceDaily.instance_id == instance_id)
        .order_by(DbaSpaceDaily.stat_date.desc())
        .limit(days)
        .all()
    )
    trend = [{
        "date": r.stat_date.isoformat(),
        "data_size": r.data_size,
        "log_size": r.log_size,
        "backup_size": r.backup_size,
        "disk_used": r.disk_used,
    } for r in reversed(rows)]
    return {"instance_id": instance_id, "current": current, "trend": trend}


@router.get("/backups")
def backups(
    instance_id: str = Query(min_length=1),
    days: int = Query(7, ge=1, le=30),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """备份：策略 + 近 N 天备份列表。"""
    inst = _get_instance(db, instance_id)
    creds = _creds_or_none(db)
    if creds is None:
        raise HTTPException(503, _NO_CREDS_NOTE)
    ak, sk, _src = creds
    try:
        r = fetch_backups(ak, sk, inst["region"], instance_id, days)
    except Exception as e:
        logger.exception("备份查询失败 %s", instance_id)
        raise HTTPException(502, f"备份查询失败: {str(e)[:200]}")
    return {"instance_id": instance_id, **r}


@router.get("/redis/instances")
def redis_instances(
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """云 Redis 实例列表（指标曲线已在监控仪表盘，此处供大key/热key 选择）。"""
    return {"instances": list_redis_instances(db)}


@router.get("/redis/keys")
def redis_keys(
    instance_id: str = Query(min_length=1),
    type: str = Query("big", pattern="^(big|hot)$"),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """Redis 大key/热key Top（DAS 实时分析，近 3 小时窗口=API 上限）。全部只读。"""
    inst = next((i for i in list_redis_instances(db) if i["instance_id"] == instance_id), None)
    if inst is None:
        raise HTTPException(404, f"Redis 实例不存在或未同步: {instance_id}")
    creds = _creds_or_none(db)
    if creds is None:
        raise HTTPException(503, _NO_CREDS_NOTE)
    ak, sk, _src = creds
    region = inst["region"]
    try:
        fn = fetch_big_keys if type == "big" else fetch_hot_keys
        rows = fn(ak, sk, region, instance_id)
    except Exception as e:
        logger.exception("Redis %skey 查询失败 %s", type, instance_id)
        raise HTTPException(502, f"Redis {type}key 查询失败: {str(e)[:200]}")
    return {"instance_id": instance_id, "type": type, "rows": rows}


@router.get("/collect/status")
def collect_status(
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """空间采集任务状态。"""
    return dba_job.get_status()


@router.post("/collect")
def trigger_collect(
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """手动触发一次空间采集（写审计）。"""
    audit.record(db, action="dba.space_collect", user_id=user.id, username=user.username,
                 resource_type="dba", result="ok")
    db.commit()
    result = dba_job.run_collection(db)
    return result
