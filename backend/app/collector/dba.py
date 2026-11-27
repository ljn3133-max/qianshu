"""DBA 专项采集器：RDS 慢SQL / SQL洞察明细 / 空间 / 备份，全部只读。

SDK 约束（勿凭印象改）：
- RDS DescribeSlowLogs：page_size 合法区间 30~100；StartTime/EndTime 是日期粒度 'YYYY-MM-DDZ'（UTC）；
  返回行按 (日期, SQLHASH) 粒度，跨天 Top 需按 SQLHASH 客户端聚合；
  平均耗时 = MySQLTotalExecutionTimes(秒总计) / MySQLTotalExecutionCounts。
- RDS DescribeSlowLogRecords：时间是 'YYYY-MM-DDTHH:MMZ'（到分钟，无秒）。
- RDS DescribeSQLLogRecords（SQL洞察明细）：时间是 'YYYY-MM-DDTHH:MM:SSZ'（必须带秒）；
  page_size 合法区间 50~100；仅 SQL 洞察已开启的实例可用。
- RDS DescribeBackups：时间 'YYYY-MM-DDTHH:MMZ'；page_size 30~100；
  备份下载 URL 子账号无权限，不要透出。
- PG 不支持 RDS DescribeSlowLogs（InstanceEngineType.NotSupport），慢SQL走 DAS
  DescribeSlowLogRecords（时间 epoch 毫秒），无模板聚合接口 -> 明细按 SQLText 客户端聚合（有上限）。
- DAS 端点 das.{region}.aliyuncs.com。
- 经典 SQL 洞察无聚合 TopSQL API（洞察报告空、PFS 系列需洞察增强版），故只做明细查询。
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

logger = logging.getLogger("qianshu.dba")


class DBAError(Exception):
    pass


# ---------------- 时间处理 ----------------
# 阿里云 RDS/DAS 的 Z 后缀时间均为 UTC；入库/出参统一转本地时区（避免时区误读）。

def _local_to_utc(dt: datetime) -> datetime:
    """naive 本地时间 -> UTC（naive.astimezone 按本地时区解释）。"""
    return dt.astimezone(timezone.utc)


def _utc_str_to_local(s: str, fmt: str) -> str:
    """'...Z' UTC 字符串 -> 本地时间字符串；解析失败原样返回。"""
    if not s:
        return ""
    try:
        return (
            datetime.strptime(s, fmt)
            .replace(tzinfo=timezone.utc)
            .astimezone()
            .strftime("%Y-%m-%d %H:%M:%S")
        )
    except ValueError:
        return s


def rds_client(ak: str, sk: str, region_id: str):
    from alibabacloud_rds20140815.client import Client
    from alibabacloud_tea_openapi.models import Config

    cfg = Config(access_key_id=ak, access_key_secret=sk, region_id=region_id)
    cfg.endpoint = f"rds.{region_id}.aliyuncs.com"
    return Client(cfg)


def das_client(ak: str, sk: str, region_id: str):
    from alibabacloud_das20200116.client import Client
    from alibabacloud_tea_openapi.models import Config

    cfg = Config(access_key_id=ak, access_key_secret=sk, region_id=region_id)
    cfg.endpoint = f"das.{region_id}.aliyuncs.com"
    return Client(cfg)


# ---------------- 实例与洞察状态 ----------------

def list_rds_instances(db: Session) -> list[dict]:
    """从资产表取 RDS 实例（含引擎信息），不直查云 API。

    账号上下文激活时只取本账号实例——否则 A 账号凭证
    去查 B 账号实例的空间（拿错密钥空转/报错）。
    """
    from app.collector.credential import active_account_id
    from app.models.asset import Asset
    from app.services.tenant import account_index

    q = db.query(Asset).filter(Asset.product == "rds", Asset.status != "Deleted")   # 规范值大写 D（base.py 清扫写入）——小写失效过滤=Deleted 行漏进 DBA 视图
    acc = active_account_id()
    if acc is not None:
        q = q.filter(Asset.account_id == acc)
    rows = q.order_by(Asset.instance_id).all()
    acc_map = account_index(db)   # 归属标识
    out = []
    for a in rows:
        try:
            extra = json.loads(a.extra or "{}")
        except ValueError:
            extra = {}
        out.append({
            "instance_id": a.instance_id,
            "name": a.name or a.instance_id,
            "region": a.region or "",
            "status": a.status,
            "engine": extra.get("engine", ""),
            "engine_version": extra.get("engine_version", ""),
            "account_id": a.account_id,
            "account": (acc_map.get(a.account_id) or {}).get("name", ""),
        })
    return out


_insight_cache: dict = {}  # instance_id -> (expire_ts, enabled)  洞察状态 10 分钟缓存


def get_sql_insight(ak: str, sk: str, region: str, instance_id: str) -> bool:
    """SQL 洞察（SQLCollector）是否开启，带 10 分钟缓存降低 API 调用。"""
    now = time.time()
    hit = _insight_cache.get(instance_id)
    if hit and hit[0] > now:
        return hit[1]
    from alibabacloud_rds20140815 import models as rds

    try:
        resp = rds_client(ak, sk, region).describe_sqlcollector_policy(
            rds.DescribeSQLCollectorPolicyRequest(dbinstance_id=instance_id)
        )
        enabled = str((resp.body.to_map().get("SQLCollectorStatus") or "")).lower() == "enable"
    except Exception as e:
        logger.warning("查询 SQL 洞察状态失败 %s: %s", instance_id, str(e)[:120])
        enabled = False  # 查询失败按未开处理（页面仍可用其他块）
    _insight_cache[instance_id] = (now + 600, enabled)
    return enabled


# ---------------- 慢SQL Top ----------------

def fetch_slowlog_top(ak: str, sk: str, region: str, instance_id: str, engine: str, days: int = 7) -> list[dict]:
    """慢SQL Top（聚合视图）。MySQL 用 RDS 聚合接口；PG 用 DAS 明细客户端聚合。"""
    if engine.lower().startswith("mysql"):
        return _slowlog_top_mysql(ak, sk, region, instance_id, days)
    return _slowlog_top_das(ak, sk, region, instance_id, days)


def _slowlog_top_mysql(ak: str, sk: str, region: str, instance_id: str, days: int) -> list[dict]:
    from alibabacloud_rds20140815 import models as rds

    client = rds_client(ak, sk, region)
    end = datetime.now().date()          # 慢SQL日志按本地日期窗口查（天粒度）
    start = end - timedelta(days=max(days, 1) - 1)
    agg: dict = {}
    page = 1
    while True:
        resp = client.describe_slow_logs(rds.DescribeSlowLogsRequest(
            dbinstance_id=instance_id,
            start_time=f"{start.isoformat()}Z", end_time=f"{end.isoformat()}Z",
            page_size=100, page_number=page,
        ))
        body = resp.body.to_map()
        items = (body.get("Items") or {}).get("SQLSlowLog") or []
        for m in items:
            h = m.get("SQLHASH") or m.get("SQLText", "")[:200]
            g = agg.setdefault(h, {
                "sql_hash": h, "sql_text": m.get("SQLText", ""), "db": m.get("DBName", ""),
                "count": 0, "total_ms": 0.0, "max_ms": 0, "max_rows_examined": 0,
            })
            # 同一 SQL 每天一行，跨天累加
            cnt = int(m.get("MySQLTotalExecutionCounts") or 0)
            g["count"] += cnt
            g["total_ms"] += float(m.get("MySQLTotalExecutionTimes") or 0) * 1000  # 秒 -> 毫秒
            g["max_ms"] = max(g["max_ms"], int(m.get("MaxExecutionTimeMS") or 0))
            g["max_rows_examined"] = max(g["max_rows_examined"], int(m.get("ParseMaxRowCount") or 0))
        total = int(body.get("TotalRecordCount") or 0)
        if not items or page * 100 >= total:
            break
        page += 1
    rows = []
    for g in agg.values():
        g["avg_ms"] = round(g["total_ms"] / g["count"], 1) if g["count"] else 0
        rows.append(g)
    rows.sort(key=lambda x: x["total_ms"], reverse=True)
    return rows[:50]


def _slowlog_top_das(ak: str, sk: str, region: str, instance_id: str, days: int) -> list[dict]:
    """PG 慢SQL：DAS 明细按 SQLText 归一后聚合（上限 20 页防大实例拖垮）。"""
    from alibabacloud_das20200116 import models as das

    client = das_client(ak, sk, region)
    end = int(time.time() * 1000)
    start = end - days * 86400000
    agg: dict = {}
    page = 1
    while page <= 20:
        resp = client.describe_slow_log_records(das.DescribeSlowLogRecordsRequest(
            instance_id=instance_id, start_time=start, end_time=end,
            page_number=page, page_size=100,
        ))
        data = resp.body.to_map().get("Data") or {}
        logs = data.get("Logs") or []
        for m in logs:
            key = _norm_sql(m.get("SQLText", ""))
            g = agg.setdefault(key, {
                # PG 无模板哈希：sql_hash 放归一化键，明细弹窗回传它做客户端过滤
                "sql_hash": key, "sql_text": m.get("SQLText", ""), "db": m.get("DBName", ""),
                "count": 0, "total_ms": 0.0, "max_ms": 0, "max_rows_examined": 0,
            })
            qms = float(m.get("QueryTime") or 0)
            g["count"] += 1
            g["total_ms"] += qms
            g["max_ms"] = max(g["max_ms"], int(qms))
            g["max_rows_examined"] = max(g["max_rows_examined"], int(m.get("RowsExamined") or 0))
        if not logs:
            break
        page += 1
    rows = []
    for g in agg.values():
        g["avg_ms"] = round(g["total_ms"] / g["count"], 1) if g["count"] else 0
        rows.append(g)
    rows.sort(key=lambda x: x["total_ms"], reverse=True)
    return rows[:50]


def _norm_sql(sql: str) -> str:
    """SQL 归一化键：压空白、去字面量，粗粒度模板聚合（PG 无模板哈希）。"""
    s = re.sub(r"\s+", " ", (sql or "")).strip().lower()
    s = re.sub(r"'[^']*'", "'?'", s)
    s = re.sub(r"\b\d+\b", "?", s)
    return s[:200]


# ---------------- 慢SQL明细 ----------------

def fetch_slowlog_records(ak: str, sk: str, region: str, instance_id: str, engine: str,
                          start: datetime, end: datetime, page: int = 1, page_size: int = 50,
                          sql_hash: str = "") -> dict:
    """慢SQL明细（统一两引擎结构）。MySQL 走 RDS，PG 走 DAS。

    sql_hash：只看某一条慢SQL模板的记录。两引擎的过滤方式不同：
    - MySQL：模板自带 RDS SQLHASH，DescribeSlowLogRecords 支持 sqlhash 服务端精确过滤；
    - PG：模板键是客户端归一化串（_norm_sql），DAS filters 不支持按该键过滤
      （无匹配也返回全部），只能拉全部分页（上限 20 页防大实例拖垮，与 Top 聚合同口径）
      后本地过滤再分页。
    """
    if engine.lower().startswith("mysql"):
        from alibabacloud_rds20140815 import models as rds

        # sqlhash 必须是 RDS 的 32 位模板哈希；Top 行在 SQLHASH 缺失时会用
        # SQLText 前缀占位（理论上罕见），那种值传给 RDS 会报错——降级为不过滤并说明
        filterable = bool(sql_hash and re.fullmatch(r"[0-9a-fA-F]{32}", sql_hash))
        kwargs = {"sqlhash": sql_hash} if filterable else {}
        resp = rds_client(ak, sk, region).describe_slow_log_records(rds.DescribeSlowLogRecordsRequest(
            dbinstance_id=instance_id,
            start_time=_local_to_utc(start).strftime("%Y-%m-%dT%H:%MZ"),
            end_time=_local_to_utc(end).strftime("%Y-%m-%dT%H:%MZ"),
            page_size=max(30, min(page_size, 100)), page_number=page,
            **kwargs,
        ))
        body = resp.body.to_map()
        items = (body.get("Items") or {}).get("SQLSlowRecord") or []
        rows = [{
            "time": _utc_str_to_local(r.get("ExecutionStartTime", ""), "%Y-%m-%dT%H:%M:%SZ"),
            "db": r.get("DBName", ""),
            "host": r.get("HostAddress", ""),
            "query_ms": int(r.get("QueryTimeMS") or 0),
            "lock_ms": int(r.get("LockTimeMS") or 0),
            "rows_examined": int(r.get("ParseRowCounts") or 0),
            "rows_sent": int(r.get("ReturnRowCounts") or 0),
            "sql_text": r.get("SQLText", ""),
        } for r in items]
        total = int(body.get("TotalRecordCount") or 0)
        out = {"total": total, "rows": rows}
        if sql_hash and filterable:
            out["note"] = "仅显示该模板的记录（RDS 按模板哈希服务端过滤）"
        elif sql_hash:
            out["note"] = "该模板没有可用哈希，无法按模板过滤，当前为全部慢SQL明细"
        return out

    from alibabacloud_das20200116 import models as das

    if not sql_hash:
        resp = das_client(ak, sk, region).describe_slow_log_records(das.DescribeSlowLogRecordsRequest(
            instance_id=instance_id,
            start_time=int(start.timestamp() * 1000), end_time=int(end.timestamp() * 1000),
            page_number=page, page_size=page_size,
        ))
        data = resp.body.to_map().get("Data") or {}
        logs = data.get("Logs") or []
        rows = []
        for m in logs:
            rows.append({
                "time": _utc_str_to_local(m.get("QueryStartTime", ""), "%Y-%m-%dT%H:%M:%SZ"),
                "db": m.get("DBName", ""),
                "host": m.get("HostAddress", ""),
                "query_ms": int(float(m.get("QueryTime") or 0)),
                "lock_ms": int(float(m.get("LockTime") or 0) * 1000),
                "rows_examined": int(m.get("RowsExamined") or 0),
                "rows_sent": int(m.get("RowsSent") or 0),
                "sql_text": m.get("SQLText", ""),
            })
        total = int(data.get("ItemsNumbers") or 0) or len(rows)
        return {"total": total, "rows": rows}

    # PG 按模板过滤：拉最多 20 页（2000 条）后本地过滤
    matched: list = []
    scanned = 0
    for pg in range(1, 21):
        resp = das_client(ak, sk, region).describe_slow_log_records(das.DescribeSlowLogRecordsRequest(
            instance_id=instance_id,
            start_time=int(start.timestamp() * 1000), end_time=int(end.timestamp() * 1000),
            page_number=pg, page_size=100,
        ))
        logs = (resp.body.to_map().get("Data") or {}).get("Logs") or []
        if not logs:
            break
        scanned += len(logs)
        for m in logs:
            if _norm_sql(m.get("SQLText", "")) == sql_hash:
                matched.append({
                    "time": _utc_str_to_local(m.get("QueryStartTime", ""), "%Y-%m-%dT%H:%M:%SZ"),
                    "db": m.get("DBName", ""),
                    "host": m.get("HostAddress", ""),
                    "query_ms": int(float(m.get("QueryTime") or 0)),
                    "lock_ms": int(float(m.get("LockTime") or 0) * 1000),
                    "rows_examined": int(m.get("RowsExamined") or 0),
                    "rows_sent": int(m.get("RowsSent") or 0),
                    "sql_text": m.get("SQLText", ""),
                })
        if len(logs) < 100:
            break
    matched.sort(key=lambda r: r["time"], reverse=True)  # 新的在前
    lo = (page - 1) * page_size
    return {
        "total": len(matched),
        "rows": matched[lo:lo + page_size],
        "note": f"窗口内共扫描 {scanned} 条慢SQL，其中该模板 {len(matched)} 条",
    }


# ---------------- SQL 洞察明细（经典洞察，仅开启实例可用） ----------------

def fetch_insight_records(ak: str, sk: str, region: str, instance_id: str,
                          start: datetime, end: datetime, keyword: str = "",
                          page: int = 1, page_size: int = 100) -> dict:
    from alibabacloud_rds20140815 import models as rds

    resp = rds_client(ak, sk, region).describe_sqllog_records(rds.DescribeSQLLogRecordsRequest(
        dbinstance_id=instance_id,
        start_time=_local_to_utc(start).strftime("%Y-%m-%dT%H:%M:%SZ"),  # 必须带秒
        end_time=_local_to_utc(end).strftime("%Y-%m-%dT%H:%M:%SZ"),
        query_keywords=keyword or None,
        page_size=max(50, min(page_size, 100)), page_number=page,
    ))
    body = resp.body.to_map()
    items = (body.get("Items") or {}).get("SQLRecord") or []
    rows = [{
        "time": _utc_str_to_local(r.get("ExecuteTime", ""), "%Y-%m-%dT%H:%M:%SZ"),
        "db": r.get("DBName", ""),
        "user": r.get("AccountName", ""),
        "host": r.get("HostAddress", ""),
        "rows_sent": int(r.get("ReturnRowCounts") or 0),
        "sql_text": r.get("SQLText", ""),
    } for r in items]
    total = int(body.get("TotalRecordCount") or 0)
    return {"total": total, "rows": rows}


# ---------------- 空间 ----------------

def fetch_space(ak: str, sk: str, region: str, instance_id: str) -> dict:
    """当前空间使用（字节）。两引擎同构。"""
    from alibabacloud_rds20140815 import models as rds

    resp = rds_client(ak, sk, region).describe_resource_usage(
        rds.DescribeResourceUsageRequest(dbinstance_id=instance_id)
    )
    m = resp.body.to_map()
    return {
        "data_size": int(m.get("DataSize") or 0),
        "log_size": int(m.get("BackupLogSize") or 0),
        "backup_size": int(m.get("BackupSize") or 0),
        "disk_used": int(m.get("DiskUsed") or 0),
    }


# ---------------- 备份 ----------------

def fetch_backups(ak: str, sk: str, region: str, instance_id: str, days: int = 7) -> dict:
    """备份列表 + 备份策略。下载 URL 子账号无权限，不透出。"""
    from alibabacloud_rds20140815 import models as rds

    client = rds_client(ak, sk, region)
    end = datetime.now()
    start = end - timedelta(days=days)
    list_resp = client.describe_backups(rds.DescribeBackupsRequest(
        dbinstance_id=instance_id,
        start_time=_local_to_utc(start).strftime("%Y-%m-%dT%H:%MZ"),
        end_time=_local_to_utc(end).strftime("%Y-%m-%dT%H:%MZ"),
        page_size=100, page_number=1,
    ))
    items = (list_resp.body.to_map().get("Items") or {}).get("Backup") or []
    backups = []
    for m in items:
        st = m.get("BackupStartTime") or ""
        et = m.get("BackupEndTime") or ""
        dur_s = None
        try:
            if st and et:
                dur_s = round((
                    datetime.strptime(et, "%Y-%m-%dT%H:%M:%SZ") - datetime.strptime(st, "%Y-%m-%dT%H:%M:%SZ")
                ).total_seconds(), 1)
        except ValueError:
            pass
        backups.append({
            "backup_id": m.get("BackupId"),
            "type": m.get("BackupType", ""),          # FullBackup / IncrementalBackup
            "mode": m.get("BackupMode", ""),          # Automated / Manual
            "method": m.get("BackupMethod", ""),      # Snapshot / Physical / Logical
            "status": m.get("BackupStatus", ""),
            "size": int(m.get("BackupSize") or 0),
            "start_time": _utc_str_to_local(st, "%Y-%m-%dT%H:%M:%SZ"),
            "end_time": _utc_str_to_local(et, "%Y-%m-%dT%H:%M:%SZ"),
            "duration_s": dur_s,
        })
    backups.sort(key=lambda b: b["start_time"], reverse=True)

    policy = {}
    try:
        p = client.describe_backup_policy(
            rds.DescribeBackupPolicyRequest(dbinstance_id=instance_id)
        ).body.to_map()
        policy = {
            "backup_log": p.get("BackupLog", ""),
            "backup_method": p.get("BackupMethod", ""),
            "retention_days": int(p.get("BackupRetentionPeriod") or 0),
            "log_retention_hours": p.get("LogBackupRetentionPeriod", ""),
        }
    except Exception as e:  # 策略查询失败不连累列表
        logger.warning("备份策略查询失败 %s: %s", instance_id, str(e)[:120])
    return {"policy": policy, "backups": backups, "total": len(backups)}


# ---------------- Redis 大key / 热key（DAS 只读） ----------------
# 陷阱：DescribeTopBigKeys/DescribeTopHotKeys 时间窗上限 10800 秒（3 小时），超过报
# "time range too large"。r-kvstore 缓存分析（CreateCacheAnalysisTask）是创建任务类
# 操作且每日分析未配置，按低影响+成本原则不调用，只走 DAS 实时分析。

def list_redis_instances(db: Session) -> list[dict]:
    """从资产表取云 Redis 实例（product='redis'）。"""
    from app.models.asset import Asset
    from app.services.tenant import account_index

    rows = (
        db.query(Asset)
        .filter(Asset.product == "redis", Asset.status != "Deleted")   # 同上：大写 D 规范口径
        .order_by(Asset.instance_id)
        .all()
    )
    acc_map = account_index(db)   # 归属标识
    out = []
    for a in rows:
        try:
            extra = json.loads(a.extra or "{}")
        except ValueError:
            extra = {}
        out.append({
            "instance_id": a.instance_id,
            "name": a.name or a.instance_id,
            "region": a.region or "",
            "status": a.status,
            "architecture": extra.get("architecture", ""),
            "engine_version": extra.get("engine_version", ""),
            "capacity_mb": extra.get("capacity_mb", 0),
            "account_id": a.account_id,
            "account": (acc_map.get(a.account_id) or {}).get("name", ""),
        })
    return out


def fetch_big_keys(ak: str, sk: str, region: str, instance_id: str) -> list[dict]:
    """大key Top（DAS 实时分析，固定近 3 小时窗口=API 上限）。"""
    from alibabacloud_das20200116 import models as das

    resp = das_client(ak, sk, region).describe_top_big_keys(das.DescribeTopBigKeysRequest(
        instance_id=instance_id,
        start_time=int(time.time() * 1000) - 10800000,
        end_time=int(time.time() * 1000),
        console_context="qianshu",
    ))
    data = resp.body.to_map().get("Data") or {}
    items = data.get("BigKey") or []
    rows = [{
        "key": m.get("Key", ""),
        "key_type": m.get("KeyType", ""),
        "node_id": m.get("NodeId", ""),
        "db": int(m.get("Db") or 0),
        "size": int(m.get("Size") or 0),
    } for m in items]
    rows.sort(key=lambda r: r["size"], reverse=True)
    return rows


def fetch_hot_keys(ak: str, sk: str, region: str, instance_id: str) -> list[dict]:
    """热key Top（DAS 实时分析）。热key统计未开启时返回空列表（页面提示）。"""
    from alibabacloud_das20200116 import models as das

    resp = das_client(ak, sk, region).describe_top_hot_keys(das.DescribeTopHotKeysRequest(
        instance_id=instance_id,
        start_time=int(time.time() * 1000) - 10800000,
        end_time=int(time.time() * 1000),
        console_context="qianshu",
    ))
    data = resp.body.to_map().get("Data") or {}
    items = data.get("HotKey") or []
    rows = [{
        "key": m.get("Key", ""),
        "key_type": m.get("KeyType", ""),
        "node_id": m.get("NodeId", ""),
        "db": int(m.get("Db") or 0),
        "hot": m.get("Hot"),          # 热度值
        "lfu": m.get("Lfu"),           # LFU 访问频次
        "in_bytes": m.get("InBytes"),
        "out_bytes": m.get("OutBytes"),
    } for m in items]
    return rows
