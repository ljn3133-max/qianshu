"""自然语言查询工具注册表。安全红线：全部只读，LLM 只产出参数、
代码侧用参数化白名单查询，绝不执行 LLM 生成的 SQL/PromQL 原文。

四个工具：
- query_assets      资产查询（Asset 表，产品/region/关键字/状态过滤）
- query_metric      监控查询（VictoriaMetrics，语义指标名 -> 白名单 PromQL 模板）
- query_cost        成本查询（cost_daily，日成本口径）
- query_alert_history 告警历史（alert_records）
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.collector.vm import query_range as vm_query_range
from app.models.alert_record import AlertRecord
from app.models.asset import Asset
from app.models.cost_daily import CostDaily
from app.services.tenant import account_fields, account_filter, account_id_param, account_index

logger = logging.getLogger("qianshu.ai_tools")

_MAX_ROWS = 50          # 工具返回给 LLM 的最大行数
_ASSET_PRODUCTS = ["ecs", "rds", "redis", "slb", "oss", "cdn", "waf", "ack", "es"]

# 语义指标 -> (产品, CMS 指标名, 单位, 中文含义)。LLM 说 cpu/rds 才能落到具体序列。
_METRIC_MAP = {
    ("cpu", "ecs"): ("CPUUtilization", "%", "CPU使用率"),
    ("cpu", "rds"): ("CpuUsage", "%", "CPU使用率"),
    ("cpu", "redis"): ("ShardingCpuUsage", "%", "CPU使用率"),
    ("memory", "ecs"): ("memory_usedutilization", "%", "内存使用率"),
    ("memory", "rds"): ("MemoryUsage", "%", "内存使用率"),
    ("memory", "redis"): ("ShardingMemoryUsage", "%", "内存使用率"),
    ("disk", "ecs"): ("diskusage_utilization", "%", "磁盘使用率（各分区最满）"),
    ("disk", "rds"): ("DiskUsage", "%", "磁盘使用率"),
    ("connection", "rds"): ("ConnectionUsage", "%", "连接数使用率"),
    ("connection", "redis"): ("ShardingConnectionUsage", "%", "连接数使用率"),
    ("iops", "rds"): ("IOPSUsage", "%", "IOPS使用率"),
    ("qps", "slb"): ("InstanceQps", "count/s", "QPS"),
    ("rt", "slb"): ("InstanceRt", "ms", "请求RT"),
}
_WINDOW_SECONDS = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400}


class ToolError(ValueError):
    """工具参数/执行错误，message 面向 LLM 或用户展示。"""


def _clamp_limit(v, default=20) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, _MAX_ROWS))


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _acc(params: dict) -> int:
    """account_id 参数：0/缺省=全部账号聚合。"""
    try:
        return account_id_param(params)
    except ValueError as e:
        raise ToolError(str(e))


# ---------------- 工具实现 ----------------

def query_assets(db: Session, params: dict) -> dict:
    """资产查询（白名单字段，参数化）。account_id 可选过滤，缺省全部账号聚合。"""
    product = str(params.get("product") or "").strip().lower()
    if product and product not in _ASSET_PRODUCTS:
        raise ToolError(f"不支持的产品 {product}，可选: {', '.join(_ASSET_PRODUCTS)}")
    region = str(params.get("region") or "").strip()
    keyword = str(params.get("keyword") or "").strip()
    status = str(params.get("status") or "").strip()
    limit = _clamp_limit(params.get("limit"), 20)
    acc = _acc(params)

    q = account_filter(db.query(Asset), Asset, acc)   # 账号过滤（0=聚合）
    if product:
        q = q.filter(Asset.product == product)
    if region:
        q = q.filter(Asset.region == region)
    if status:
        q = q.filter(Asset.status.ilike(f"%{status}%"))
    if keyword:  # 名称或实例ID 子串匹配
        q = q.filter(or_(Asset.name.ilike(f"%{keyword}%"), Asset.instance_id.ilike(f"%{keyword}%")))
    rows = q.order_by(Asset.product, Asset.name).limit(limit).all()
    acc_map = account_index(db)   # 归属标识
    items = [
        {
            "product": a.product, "instance_id": a.instance_id, "name": a.name,
            "region": a.region, "status": a.status, "instance_type": a.instance_type,
            "charge_type": a.charge_type,
            "expire_time": a.expire_time.strftime("%Y-%m-%d") if a.expire_time else "",
            **account_fields(acc_map, a.account_id),
        }
        for a in rows
    ]
    total = q.count()
    return {"total": total, "items": items,
            "columns": ["product", "instance_id", "name", "region", "status", "instance_type",
                        "charge_type", "expire_time", "account_id", "account_name"]}


def query_metric(db: Session, params: dict) -> dict:
    """监控指标查询：语义指标名+产品 -> 白名单 VM 序列，窗口聚合后按阈值过滤。

    params: metric(cpu/memory/disk/connection/iops/qps/rt), product(ecs/rds/redis/slb),
    op(>/>=/</<=), threshold, window(1h/6h/24h/7d), aggregation(avg/max/latest), keyword
    """
    metric = str(params.get("metric") or "").strip().lower()
    product = str(params.get("product") or "").strip().lower()
    # 产品缺省时按语义指标名猜一个（cpu 优先 ECS）
    if not product:
        for (m, p) in _METRIC_MAP:
            if m == metric:
                product = p
                break
    key = (metric, product)
    if key not in _METRIC_MAP:
        supported = ", ".join(f"{m}@{p}" for m, p in _METRIC_MAP)
        raise ToolError(f"不支持的指标/产品组合 {metric}@{product}；支持: {supported}")
    cms_metric, unit, label = _METRIC_MAP[key]

    window = str(params.get("window") or "24h")
    win_s = _WINDOW_SECONDS.get(window)
    if win_s is None:
        raise ToolError(f"不支持的时间窗口 {window}，可选: {', '.join(_WINDOW_SECONDS)}")
    agg = str(params.get("aggregation") or "max").lower()
    if agg not in ("avg", "max", "latest"):
        agg = "max"
    op = str(params.get("op") or "").strip() or ">"
    if op not in (">", ">=", "<", "<="):
        raise ToolError(f"不支持的比较符 {op}")
    threshold = _f(params.get("threshold"), None) if params.get("threshold") is not None else None
    keyword = str(params.get("keyword") or "").strip()
    limit = _clamp_limit(params.get("limit"), 20)
    acc = _acc(params)

    series_name = f"aliyun_{product}_{cms_metric.lower()}"
    series = series_name
    if keyword:
        safe = keyword.replace('\\', '\\\\').replace('"', '\\"')
        series = f'{series_name}{{instance_id=~".*{safe}.*"}}'
    now_s = int(time.time())
    step = max(60, win_s // 240)  # 控制返回点数 <= ~240
    if agg == "latest":
        promql = series
    elif agg == "avg":
        promql = f"avg_over_time({series}[{window}])"
    else:
        promql = f"max_over_time({series}[{window}])"
    result = vm_query_range(promql, now_s - win_s, now_s, step)

    # 实例名与账号归属映射
    acc_map = account_index(db)
    amap = {a.instance_id: a for a in db.query(Asset).filter(Asset.product == product).all()}
    name_map = {iid: (a.name or iid) for iid, a in amap.items()}

    ops = {">": lambda a, b: a > b, ">=": lambda a, b: a >= b, "<": lambda a, b: a < b, "<=": lambda a, b: a <= b}
    sort_key = f"{metric}_{agg}_{window}"
    items = []
    for r in result:
        try:
            val = float(r["values"][-1][1])
        except (KeyError, IndexError, ValueError, TypeError):
            continue
        if threshold is not None and not ops[op](val, threshold):
            continue
        labels = r.get("metric", {})
        instance_id = labels.get("instance_id", "")
        a = amap.get(instance_id)
        if acc and (a is None or (a.account_id or 0) != acc):
            continue   # 账号过滤：归属不明的序列不计入（无法确认属于该账号）
        items.append({
            "instance_id": instance_id,
            "name": name_map.get(instance_id, ""),
            "region": labels.get("region", ""),
            sort_key: round(val, 2),
            "unit": unit,
            **account_fields(acc_map, a.account_id if a else 0),
        })
    items.sort(key=lambda x: -x[sort_key])
    limited = items[:min(limit, _MAX_ROWS)]
    return {"total": len(items), "returned": len(limited), "items": limited,
            "columns": ["instance_id", "name", "region", sort_key, "unit", "account_id", "account_name"],
            "metric_desc": f"{product} {label}（{agg} over {window}）"}


def query_cost(db: Session, params: dict) -> dict:
    """成本查询（日成本口径 = 按量 + 摊销，订单事件不计入）。

    account_id 可选过滤（缺省=全部账号聚合）；group_by 支持 product/instance/account。
    """
    # days 专用 clamp（1-90）：不能走 _clamp_limit（那会先被 _MAX_ROWS=50 截断，
    # "近 60/90 天"被静默砍半）；total 用截断前计数（截断后的 len(items) 不是总数）
    try:
        days = int(params.get("days"))
    except (TypeError, ValueError):
        days = 7
    days = max(1, min(days, 90))
    product = str(params.get("product") or "").strip().lower()
    group_by = str(params.get("group_by") or "product").strip().lower()
    if group_by not in ("product", "instance", "account"):
        group_by = "product"
    top = _clamp_limit(params.get("top"), 10)
    acc = _acc(params)

    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    q = account_filter(db.query(CostDaily), CostDaily, acc)   # 账号过滤（0=聚合）
    q = q.filter(
        CostDaily.stat_date >= start, CostDaily.stat_date <= end,
        or_(CostDaily.source == "amortized", CostDaily.subscription_type == "PayAsYouGo"),
    )
    if product:
        q = q.filter(CostDaily.product_code == product)
    rows = q.all()
    acc_map = account_index(db)   # 归属标识

    if group_by == "instance":
        agg: dict = {}
        for r in rows:
            # 实例×账号 聚合（实例 ID 跨账号可能撞号，带账号才归属无歧义）
            k = (r.instance_id or "(无实例)", r.account_id or 0)
            a = agg.setdefault(k, {"instance_id": k[0], "product": r.product_code,
                                   "name": r.instance_name or "", "amount": 0.0,
                                   **account_fields(acc_map, k[1])})
            a["amount"] += float(r.pretax_amount or 0)     # 账单金额（判断花费以此为准）
            if not a["name"] and r.instance_name:
                a["name"] = r.instance_name
        items = sorted(agg.values(), key=lambda x: -x["amount"])[:top]
        columns = ["instance_id", "product", "name", "amount", "account_id", "account_name"]
    elif group_by == "account":
        agg = {}
        for r in rows:
            k = r.account_id or 0
            a = agg.setdefault(k, {"amount": 0.0, **account_fields(acc_map, k)})
            a["amount"] += float(r.pretax_amount or 0)
        items = sorted(agg.values(), key=lambda x: -x["amount"])[:top]
        columns = ["account_id", "account_name", "amount"]
    else:
        agg = {}
        for r in rows:
            k = r.product_code or "(未知)"
            a = agg.setdefault(k, {"product": k, "name": r.product_name or k, "amount": 0.0,
                                   "_accs": set()})
            a["amount"] += float(r.pretax_amount or 0)
            a["_accs"].add(r.account_id or 0)
            if a["name"] == k and r.product_name:
                a["name"] = r.product_name
        for a in agg.values():
            accs = a.pop("_accs")
            # 产品聚合缺省跨账号合计；单一账号时补归属，跨账号留空（分账号用
            # group_by=account 或 account_id 过滤）
            a.update(account_fields(acc_map, accs.pop() if len(accs) == 1 else 0))
        items = sorted(agg.values(), key=lambda x: -x["amount"])[:top]
        columns = ["product", "name", "amount", "account_id", "account_name"]
    total = len(agg)   # 截断前计数（items 已被 top 截断，len(items) 不是总数）
    for i in items:
        i["amount"] = round(i["amount"], 2)
    return {"start": start.isoformat(), "end": end.isoformat(), "days": days,
            "total": total, "returned": len(items), "items": items, "columns": columns,
            "note": (f"近{days}天日成本合计（按量+摊销），Top{len(items)}。"
                     "金额口径：amount=账单金额（判断花费以此为准）；"
                     "payment_amount=挂账现金字段（常 0），仅存证不作依据。"
                     "归属：items 带 account_id/account_name；产品聚合跨账号时留空，"
                     "分账号看用 group_by=account 或 account_id 过滤。")}


def query_alert_history(db: Session, params: dict) -> dict:
    """告警历史查询。account_id 可选过滤，缺省全部账号聚合。"""
    days = _clamp_limit(params.get("days"), 7)
    days = max(1, min(days, 90))
    level = str(params.get("level") or "").strip().lower()
    status = str(params.get("status") or "").strip().lower()
    limit = _clamp_limit(params.get("limit"), 20)
    acc = _acc(params)

    start = datetime.now() - timedelta(days=days)
    q = account_filter(db.query(AlertRecord), AlertRecord, acc)   # 账号过滤（0=聚合）
    q = q.filter(AlertRecord.fired_at >= start)
    if level:
        q = q.filter(AlertRecord.level == level)
    if status:
        q = q.filter(AlertRecord.status == status)
    rows = q.order_by(AlertRecord.fired_at.desc()).limit(limit).all()
    acc_map = account_index(db)   # 归属标识
    items = [
        {
            "fired_at": r.fired_at.strftime("%m-%d %H:%M"), "rule_name": r.rule_name,
            "level": r.level, "status": r.status,
            "target": r.target_name or r.target, "breach_value": r.breach_value,
            "title": r.title[:80],
            **account_fields(acc_map, r.account_id),
        }
        for r in rows
    ]
    return {"total": q.count(), "items": items,
            "columns": ["fired_at", "rule_name", "level", "status", "target", "breach_value",
                        "title", "account_id", "account_name"]}


# ---------------- 注册表 ----------------

TOOLS = {
    "query_assets": {
        "desc": "查询云资产清单（ECS/RDS/Redis/SLB/OSS/CDN/WAF/ACK/ES）：数量、规格、状态、地域、到期时间。可按 account_id 过滤",
        "params": {
            "product": "产品代码，可选 ecs/rds/redis/slb/oss/cdn/waf/ack/es",
            "region": "地域，如 cn-hangzhou",
            "keyword": "名称或实例ID关键字",
            "status": "状态过滤，如 Running/Stopped",
            "account_id": "云账号ID过滤（可选，缺省=全部账号聚合）",
            "limit": "返回行数，默认20",
        },
        "fn": query_assets,
    },
    "query_metric": {
        "desc": "查询监控指标：CPU/内存/磁盘/连接数使用率、QPS、RT。支持阈值过滤，如『CPU超80的机器』。可按 account_id 过滤",
        "params": {
            "metric": "指标语义名：cpu/memory/disk/connection/iops/qps/rt",
            "product": "产品：ecs/rds/redis/slb（cpu 缺省 ecs）",
            "op": "比较符 >/>=/</<=",
            "threshold": "阈值数字（如 80）",
            "window": "时间窗口 1h/6h/24h/7d，默认 24h",
            "aggregation": "窗口聚合方式 avg/max/latest，默认 max",
            "keyword": "实例名称/ID关键字过滤",
            "account_id": "云账号ID过滤（可选，缺省=全部账号聚合）",
            "limit": "返回行数，默认20",
        },
        "fn": query_metric,
    },
    "query_cost": {
        "desc": "查询成本：近N天按产品/实例/账号的日成本合计（按量+摊销）。可按 account_id 过滤，分账号看用 group_by=account",
        "params": {
            "days": "天数，默认7",
            "product": "产品代码过滤",
            "group_by": "聚合维度 product/instance/account，默认 product",
            "account_id": "云账号ID过滤（可选，缺省=全部账号聚合）",
            "top": "返回Top N，默认10",
        },
        "fn": query_cost,
    },
    "query_alert_history": {
        "desc": "查询告警历史记录：最近触发的告警规则、级别、状态。可按 account_id 过滤",
        "params": {
            "days": "天数，默认7",
            "level": "级别 critical/warning",
            "status": "状态 firing/recovered",
            "account_id": "云账号ID过滤（可选，缺省=全部账号聚合）",
            "limit": "返回行数，默认20",
        },
        "fn": query_alert_history,
    },
}


def execute_tool(db: Session, tool: str, params: dict) -> dict:
    """执行一个白名单工具。返回结果 dict（可直接 JSON 给 LLM）。"""
    spec = TOOLS.get(tool)
    if spec is None:
        raise ToolError(f"未知工具 {tool}，可选: {', '.join(TOOLS)}")
    if not isinstance(params, dict):
        raise ToolError("params 必须是对象")
    return spec["fn"](db, params)
