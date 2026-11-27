"""诊断工具注册表：故障排查的只读取证工具。

安全红线（与 ai_tools 一致）：全部只读，LLM 只产出工具名+参数，
代码侧执行白名单参数化查询，绝不执行 LLM 生成的 SQL/PromQL 原文。

每个工具带 scenarios 标签，由 diag_router 按问题意图选择场景，Agent 只拿到该场景的
工具，避免把成本或 CDN 问题误走成 SLB 链路排查。

链路故障（link_fault）七工具，对应「xxx.com 打不开/接口慢」式故障的逐层下钻：
- find_domain        关键词 -> 已知链路域名（把模糊输入对准到 link_edges）
- slb_overview       域名 -> SLB 层状态码分布/5xx率/RT/按后端分组
- slb_5xx_detail     域名 -> 5xx 明细（URI/状态码计数）
- path_latency_detail 域名/路径 -> 各 URI 的平均RT/p95/5xx（定位哪个接口慢）
- backend_health     域名 -> 后端 ECS（状态/CPU/内存/磁盘峰值/流量/5xx/RT）
- app_errors         应用 -> ERROR 日志计数 + 样本
- db_health          下游 RDS/Redis 关键指标（CPU/内存/磁盘/连接/平均RT）

成本分析（cost_analysis）工具——数据来自本地库（包快照/日账单/异动/到期账）：
- pack_pools         全部抵扣能力池（剩余/日消耗/几天耗尽）
- pool_usage_trend   某类资源包的每日消耗趋势（快照余量逐日差分）
- product_cost_trend 某产品日成本趋势（按量/包月拆分）
- instance_cost_top  某产品费用 Top 实例
- cost_anomaly_list  近期待处理成本异动
- expiry_list        即将到期的域名/证书/资产/资源包

数据库专项（dba）工具：
- rds_list           RDS 实例清单（本地资产，秒回）
- slowlog_top        慢SQL Top（云 API，实例×天数）
- dba_space_trend    实例空间近 14 天趋势（本地快照）

主机资源（host_res）按时间对齐取证：
- host_metrics       内存/CPU/磁盘水位时间线 -> 定位峰值时刻
- process_top        进程级 top（CMS process.memory/cpu）-> 该时刻谁在吃资源
- biz_errors         应用 ERROR + SLB 5xx 小时分布 -> 该时段业务是否受损
- process_logs       进程名归一匹配日志 AppName -> 该服务事发时段在跑什么；
                     进程名与日志应用名可能使用不同前缀、扩展名或尾部序号
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.collector.sls import query_logs as sls_query_logs
from app.collector.vm import query_range as vm_query_range
from app.models.asset import Asset
from app.models.cost_anomaly import CostAnomaly
from app.models.cost_daily import CostDaily
from app.models.link_edge import LinkEdge
from app.models.resource_pack_snapshot import ResourcePackSnapshot
from app.services.pack_impact import _SIZE_UNITS, norm_amount, pack_chinese, pool_key
from app.services.tenant import account_fields, account_filter, account_id_param, account_index

logger = logging.getLogger("qianshu.diag_tools")

_WINDOW_SECONDS = {"1h": 3600, "6h": 6 * 3600, "24h": 86400}

# SLS 目标统一走配置，改 SLS_SLB_TARGETS / SLS_APP_LOG 即可适配新环境；
# 不得恢复代码内硬编码，详见 services/sls_targets.py。
from app.services.sls_targets import app_log, slb_targets  # noqa: E402

_MAX_ROWS = 20  # 返回给 LLM 的最大行数


class ToolError(ValueError):
    """工具参数/执行错误，message 面向 LLM 或用户展示。"""


def _clean(v) -> str:
    """SLS 检索值的引号清洗：去掉引号防止注入检索语法。"""
    return str(v or "").strip().replace('"', "").replace("'", "")


def _num(row: dict, key: str, default=0.0) -> float:
    try:
        return float(row.get(key))
    except (TypeError, ValueError):
        return default


def _edge_freshness(edges) -> tuple:
    """链路边数据时间：返回 (as_of 文本, 是否超过 24h 未刷新)。

    链路库只在刷新链路草稿时更新；超过 24h 的结果必须标记陈旧，不能把历史窗口
    数字当成当前实时结论。
    """
    times = [e.generated_at for e in edges if getattr(e, "generated_at", None)]
    if not times:
        return "", False
    as_of = max(times)
    stale = (datetime.now() - as_of).total_seconds() > 86400
    return as_of.strftime("%Y-%m-%d %H:%M"), stale


def _acc(params: dict) -> int:
    """account_id=0 或缺省表示全部账号聚合；非法值统一转成工具参数错误。"""
    try:
        return account_id_param(params)
    except ValueError as e:
        raise ToolError(str(e))


def _window(params: dict, default: str = "24h") -> int:
    w = str(params.get("window") or default)
    if w not in _WINDOW_SECONDS:
        raise ToolError(f"不支持的时间窗口 {w}，可选: {', '.join(_WINDOW_SECONDS)}")
    return _WINDOW_SECONDS[w]


# ---------------- 工具 1：域名定位 ----------------


# ---------------- 工具 0：变更时间线（排障第一问） ----------------

def recent_changes(db: Session, params: dict) -> dict:
    """统一变更时间线：按账号配置的变更源保鲜，失败不能解读成“没有变更”。"""
    hours = int(params.get("hours") or 48)
    hours = max(1, min(hours, 24 * 30))
    account_id = _acc(params)
    keyword = _clean(params.get("keyword") or "")
    from datetime import datetime, timedelta
    from app.models.change_event import ChangeEvent
    from sqlalchemy import or_
    # 数据保鲜按“启用账号 × 其配置拉取源”判断；A 账号或 webhook 的新事件
    # 不能掩盖 B 账号/另一拉取源从未同步。只刷新过期目标，其他账号不重复调云。
    from app.models.cloud_account import CloudAccount
    from app.services.change_source import legacy_account_source_key, source_key_for_account, sync_changes

    def _stale(aid: int, source: str = "") -> bool:
        query = db.query(func.max(ChangeEvent.created_at))
        if aid:
            query = query.filter(ChangeEvent.account_id == aid)
        if source:
            query = query.filter(ChangeEvent.source_type == source)
        newest = query.scalar()
        return newest is None or (datetime.now() - newest).total_seconds() > 3600

    stale_targets: list[int] = []
    if account_id:
        target_account = db.get(CloudAccount, account_id)
        if target_account is None or not target_account.enabled:
            stale_targets.append(account_id)
        else:
            try:
                configured_source = source_key_for_account(target_account)
                if _stale(account_id, configured_source):
                    stale_targets.append(account_id)
            except Exception:
                stale_targets.append(account_id)
    else:
        enabled_accounts = (db.query(CloudAccount)
                            .filter(CloudAccount.enabled.is_(True))
                            .order_by(CloudAccount.id).all())
        if enabled_accounts:
            for account in enabled_accounts:
                try:
                    configured_source = source_key_for_account(account)
                except Exception:
                    stale_targets.append(account.id)  # 由编排返回明确配置错误
                    continue
                if _stale(account.id, configured_source):
                    stale_targets.append(account.id)
        elif (db.query(CloudAccount.id).first() is None
              and _stale(0, legacy_account_source_key(db))):
            stale_targets.append(0)  # 仅无台账旧部署使用统一旧凭证兜底

    sync_runs: list[dict] = []
    for target in stale_targets:
        try:
            run = sync_changes(db, days=2, account_id=target)
        except Exception as e:
            logger.warning("变更同步失败（沿用库内数据）: %s", str(e)[:120])
            run = {"ok": False, "errors": [{"account_id": target, "error": str(e)[:200]}],
                   "results": [], "fetched": 0, "created": 0, "truncated": False}
        sync_runs.append(run)
    sync_result = None
    if sync_runs:
        sync_result = {
            "ok": all(r.get("ok", False) for r in sync_runs),
            "fetched": sum(r.get("fetched", 0) for r in sync_runs),
            "created": sum(r.get("created", 0) for r in sync_runs),
            "truncated": any(r.get("truncated", False) for r in sync_runs),
            "results": [item for r in sync_runs for item in r.get("results", [])],
            "errors": [item for r in sync_runs for item in r.get("errors", [])],
        }
        if sync_result["errors"]:
            logger.warning("部分变更源同步失败（沿用库内数据）: %s", sync_result["errors"])
    since = datetime.now() - timedelta(hours=hours)
    q = db.query(ChangeEvent).filter(ChangeEvent.event_time >= since)
    if account_id:
        q = q.filter(ChangeEvent.account_id == account_id)
    if keyword:
        like = f"%{keyword}%"
        q = q.filter(or_(ChangeEvent.event_name.like(like), ChangeEvent.resource.like(like),
                         ChangeEvent.summary.like(like), ChangeEvent.user.like(like)))
    rows = q.order_by(ChangeEvent.event_time.desc()).limit(_MAX_ROWS).all()
    items = [{
        "time": r.event_time.strftime("%m-%d %H:%M"), "service": r.service,
        "event": r.event_name, "user": r.user, "resource": r.resource,
        "result": r.error_code or "成功", "summary": r.summary,
        "account_id": r.account_id, "source": r.source_type,
    } for r in rows]
    sync_failed = bool(sync_result and sync_result.get("errors"))
    sync_note = "；本轮变更源同步失败，不能据此排除变更致因" if sync_failed else ""
    if not items:
        note = (f"近 {hours} 小时无已入库变更记录；应用发版等线下变更需人工补充。"
                f"{sync_note or '；仅表示当前已接入来源在此窗口没有记录'}")
        out = {"items": [], "note": note}
    else:
        out = {"items": items,
               "note": f"近 {hours} 小时变更 {len(items)} 条（成功/失败均含）{sync_note}"}
    if sync_result is not None:
        out["sync"] = {"ok": sync_result.get("ok", False),
                       "errors": sync_result.get("errors", []),
                       "truncated": bool(sync_result.get("truncated"))}
    return out


def find_domain(db: Session, params: dict) -> dict:
    """把模糊关键词对准到链路库里的已知域名。"""
    keyword = _clean(params.get("keyword") or params.get("host"))
    if not keyword:
        raise ToolError("keyword 不能为空")
    rows = db.query(LinkEdge).filter(LinkEdge.host.ilike(f"%{keyword}%")).all()
    as_of, stale = _edge_freshness(rows)
    agg: dict = {}
    for e in rows:
        a = agg.setdefault(e.host, {
            "host": e.host, "chains": set(), "backends": set(),
            "req_count": 0, "cnt_5xx": 0, "rt_avg_ms": None, "ecs_cpu": None,
            "app_name": e.app_name or "",
        })
        a["chains"].add("外层SLB" if e.chain == "outer" else "内层SLB")
        if e.ecs_name:
            a["backends"].add(e.ecs_name)
        a["req_count"] += e.req_count
        a["cnt_5xx"] += e.cnt_5xx
        # 零请求的边没有 RT 样本，不参与"最高 RT"（否则会报 0ms 假数据）；
        # 有请求但 RT 是 0.0 占位的边同样不算样本（链路草稿对云端补边写 0.0）
        if e.req_count and (e.rt_avg_ms or 0) > 0:
            a["rt_avg_ms"] = max(a["rt_avg_ms"] or 0, e.rt_avg_ms)
        if e.ecs_cpu is not None:
            a["ecs_cpu"] = max(a["ecs_cpu"] or 0, e.ecs_cpu)
    items = sorted(agg.values(), key=lambda x: -x["req_count"])[:_MAX_ROWS]
    for a in items:
        a["chains"] = sorted(a["chains"])
        a["backends"] = sorted(a["backends"])
        rt_txt = f"平均RT最高 {a['rt_avg_ms']}ms" if a["rt_avg_ms"] is not None \
            else "RT 无样本（窗口内无请求）"
        a["note"] = (f"近24h请求 {a['req_count']:,}，5xx {a['cnt_5xx']} 条，{rt_txt}"
                     + (f"，后端CPU峰值 {a['ecs_cpu']}%" if a["ecs_cpu"] is not None else ""))
    if not items:
        return {"items": [], "hint": f"链路库中没有匹配「{keyword}」的域名。可能是："
                "①域名不在 SLB 链路上（DNS 直连）；②拼写不同。可换关键词重试。"}
    note = "数据来自链路拓扑库（采集窗口=刷新链路草稿时）"
    out = {"items": items, "note": note}
    if as_of:
        out["data_as_of"] = as_of
        if stale:
            out["stale"] = True
            out["note"] = (note + f"；⚠ 链路数据截至 {as_of}，已超过 24h 未刷新，"
                           "这些数字不是实时值，需先到链路页刷新再下结论")
    return out


# ---------------- 工具 2：SLB 层总览 ----------------

def slb_overview(db: Session, params: dict) -> dict:
    """域名在 SLB 层的健康：状态码分布、5xx率、RT、按后端分组。"""
    host = _clean(params.get("host"))
    if not host:
        raise ToolError("host 不能为空")
    from app.collector.credential import resolve_sls_credential
    ak, sk, _ = resolve_sls_credential(db)
    end_s = int(time.time())
    start_s = end_s - _window(params)

    results: list = []
    for project, logstore, chain in slb_targets():
        try:
            r = sls_query_logs(ak, sk, project, logstore,
                               f'http_host: "{host}" | select count(*) as cnt, '
                               'count_if(status >= 500) as c5xx, '
                               'count_if(status >= 400 and status < 500) as c4xx, '
                               'count_if(status >= 200 and status < 300) as c2xx, '
                               'avg(upstream_response_time) as rt',
                               start=start_s, end=end_s, limit=1)
        except Exception as e:
            logger.warning("slb_overview 查询失败 %s: %s", logstore, e)
            continue
        for row in r["logs"]:
            cnt = int(_num(row, "cnt"))
            if cnt <= 0:
                continue
            c5xx = int(_num(row, "c5xx"))
            item = {
                "chain": "外层SLB(用户流量)" if chain == "outer" else "内层SLB(服务间调用)",
                "logstore": logstore,
                "requests": cnt,
                "2xx": int(_num(row, "c2xx")),
                "4xx": int(_num(row, "c4xx")),
                "5xx": c5xx,
                "rate_5xx_pct": round(c5xx / cnt * 100, 4),
                "avg_rt_ms": round(_num(row, "rt") * 1000, 1),
            }
            # 按后端分组明细（同一个库内二次查询）
            try:
                r2 = sls_query_logs(ak, sk, project, logstore,
                                    f'http_host: "{host}" | select upstream_addr, count(*) as cnt, '
                                    'count_if(status >= 500) as c5xx, '
                                    'avg(upstream_response_time) as rt '
                                    'group by upstream_addr order by cnt desc limit 10',
                                    start=start_s, end=end_s, limit=10)
                import re as _re
                _up_re = _re.compile(r"^[a-zA-Z0-9_.\-]+:\d+$")
                item["by_upstream"] = [
                    {"upstream": str(row2.get("upstream_addr", "")),
                     "requests": int(_num(row2, "cnt")),
                     "5xx": int(_num(row2, "c5xx")),
                     "avg_rt_ms": round(_num(row2, "rt") * 1000, 1)}
                    for row2 in r2["logs"]
                    if _up_re.match(str(row2.get("upstream_addr", "")))
                ][:_MAX_ROWS]
            except Exception as e:
                logger.warning("slb_overview 后端分组失败 %s: %s", logstore, e)
            results.append(item)
    if not results:
        return {"host": host, "items": [],
                "hint": f"窗口内 {host} 无任何 SLB 访问日志。若用户说『打不开』，"
                        "这本身就是关键证据：流量根本没到 SLB（DNS 解析/网络/WAF 拦截层面的问题）。"}
    return {"host": host, "items": results}


# ---------------- 工具 3：5xx 明细 ----------------

def slb_5xx_detail(db: Session, params: dict) -> dict:
    """域名 5xx 的 URI/状态码分布（定位具体出错接口）。"""
    host = _clean(params.get("host"))
    if not host:
        raise ToolError("host 不能为空")
    from app.collector.credential import resolve_sls_credential
    ak, sk, _ = resolve_sls_credential(db)
    end_s = int(time.time())
    start_s = end_s - _window(params)

    items: list = []
    for project, logstore, chain in slb_targets():
        try:
            r = sls_query_logs(ak, sk, project, logstore,
                               f'http_host: "{host}" and status >= 500 | '
                               'select request_uri, status, upstream_addr, '
                               'count(*) as cnt group by request_uri, status, upstream_addr '
                               'order by cnt desc limit 20',
                               start=start_s, end=end_s, limit=20)
        except Exception as e:
            logger.warning("slb_5xx_detail 查询失败 %s: %s", logstore, e)
            continue
        for row in r["logs"]:
            items.append({
                "chain": "外层" if chain == "outer" else "内层",
                "uri": str(row.get("request_uri", ""))[:120],
                "status": str(row.get("status", "")),
                "upstream": str(row.get("upstream_addr", "")),
                "count": int(_num(row, "cnt")),
            })
    items.sort(key=lambda x: -x["count"])
    if not items:
        return {"host": host, "items": [], "note": f"窗口内 {host} 无 5xx 记录"}
    return {"host": host, "items": items[:_MAX_ROWS],
            "note": f"Top {min(len(items), _MAX_ROWS)} 个 5xx URI（按次数）"}


# ---------------- 工具 3.5：接口路径级延迟 ----------------

def _latency_rows(db: Session, host: str, window_s: int,
                  with_p95: bool, by_path: bool) -> tuple:
    """查 SLB 日志里该域名的接口级请求数/平均RT/5xx（可选 p95）。

    by_path=True 时按「去掉查询串的路径」归并：同一接口带不同 timestamp/参数的请求
    若按原始 URI 分组会碎成多行，单次离群值会被放大成「最慢接口」；归并后才能
    得到接口级请求数、平均 RT 与 p95 的正确口径。
    p95 用 approx_percentile；regexp_extract/approx_percentile 不被支持时由调用方降级。
    返回 (rows, errors)：errors 非空=有日志库查询失败（区别于「查询成功但窗口无数据」）。
    """
    from app.collector.credential import resolve_sls_credential
    ak, sk, _ = resolve_sls_credential(db)
    end_s = int(time.time())
    start_s = end_s - window_s
    p95_expr = ("approx_percentile(upstream_response_time, 0.95) as p95, "
                if with_p95 else "")
    grp = ("regexp_extract(request_uri, '^([^?]*)', 1)"
           if by_path else "request_uri")
    rows: list = []
    errors: list = []
    for project, logstore, chain in slb_targets():
        q = (f'http_host: "{host}" | select {grp} as uri, count(*) as cnt, '
             f'avg(upstream_response_time) as rt, {p95_expr}'
             'count_if(status >= 500) as c5xx '
             f'group by {grp} order by rt desc limit 100')
        try:
            r = sls_query_logs(ak, sk, project, logstore, q,
                               start=start_s, end=end_s, limit=100)
        except Exception as e:
            logger.warning("path_latency_detail 查询失败 %s: %s", logstore, e)
            errors.append(str(e)[:200])
            continue
        for row in r["logs"]:
            p95 = None
            if with_p95 and row.get("p95") not in (None, "", "-"):
                try:
                    p95 = round(float(row["p95"]) * 1000, 1)
                except (TypeError, ValueError):
                    p95 = None
            rows.append({
                "chain": "外层" if chain == "outer" else "内层",
                "uri": str(row.get("uri") or row.get("request_uri") or "")[:160],
                "requests": int(_num(row, "cnt")),
                "avg_rt_ms": round(_num(row, "rt") * 1000, 1),
                "p95_rt_ms": p95,
                "5xx": int(_num(row, "c5xx")),
                "source": "SLB日志",
            })
    return rows, errors


def path_latency_detail(db: Session, params: dict) -> dict:
    """域名下按 URI 的响应时间（avg/p95）+ 5xx：定位到底是哪个接口慢。

    200 状态的慢请求同样按 URI 聚合，不能只靠 5xx 明细或整域名平均定位慢接口。
    """
    host = _clean(params.get("host"))
    if not host:
        raise ToolError("host 不能为空（先 find_domain 拿到完整域名）")
    path = _clean(params.get("path") or params.get("uri") or "")
    window_s = _window(params, default="24h")
    try:
        top = max(1, min(int(params.get("top") or 10), _MAX_ROWS))
    except (TypeError, ValueError):
        top = 10

    items: list = []
    note = ""
    # 能力降级链：路径归并+p95 → 路径归并 → 原始 URI+p95 → 原始 URI。
    # 查询成功但窗口内确实无日志时立即停止重试（不再用换写法硬碰）。
    for by_path, with_p95 in ((True, True), (True, False), (False, True), (False, False)):
        try:
            items, errors = _latency_rows(db, host, window_s,
                                          with_p95=with_p95, by_path=by_path)
        except Exception as e:
            logger.warning("path_latency_detail 失败（改查链路库）: %s", str(e)[:160])
            items, errors = [], [str(e)]
        if items:
            if not by_path:
                note += "当前 SLS 不支持按路径归并，以下为原始 URI（含查询串）粒度；"
            if not with_p95:
                note += "当前 SLS 不支持 p95 分位，仅返回平均RT；"
            break
        if not errors:
            break

    if path:
        needle = path.lower()
        items = [it for it in items if needle in (it.get("uri") or "").lower()]
    items.sort(key=lambda x: (-(x.get("p95_rt_ms") or x.get("avg_rt_ms") or 0),
                              -x.get("requests", 0)))
    items = items[:top]

    if not items:
        # 回退链路库路径级聚合（LinkEdge.path_pattern + rt_avg_ms，24h 口径）：
        # SLS 窗口内无日志/目标未配置时仍能给出历史慢路径线索。
        edges = db.query(LinkEdge).filter(LinkEdge.host.ilike(f"%{host}%")).all()
        fallback: list = []
        for e in edges:
            uri = e.path_pattern or "(全路径)"
            hay = f"{uri} {e.path_summary or ''}".lower()
            if path and path.lower() not in hay:
                continue
            fallback.append({
                "chain": "外层" if e.chain == "outer" else "内层",
                "uri": uri[:160], "requests": int(e.req_count or 0),
                "avg_rt_ms": round(float(e.rt_avg_ms or 0), 1), "p95_rt_ms": None,
                "5xx": int(e.cnt_5xx or 0), "source": "链路库(24h聚合)",
            })
        fallback.sort(key=lambda x: -x["avg_rt_ms"])
        if fallback:
            as_of, stale = _edge_freshness(edges)
            src = f"链路库聚合（截至 {as_of}）" if as_of else "链路库聚合"
            msg = (note + f"SLB 日志窗口内无匹配，以下为{src}（路径前缀粒度，p95 不可得）"
                   + ("；⚠ 该数据已超过 24h 未刷新，不是实时值" if stale else ""))
            out = {"host": host, "path": path, "items": fallback[:top], "note": msg}
            if as_of:
                out["data_as_of"] = as_of
            if stale:
                out["stale"] = True
            return out
        hint = (f"窗口内 {host}" + (f" 路径含「{path}」" if path else "")
                + " 没有 SLB 访问日志。可能是：①DNS 直连/未走 SLB；"
                "②流量走 CDN 未回源；③路径前缀不同（可换更短的前缀重试）。")
        return {"host": host, "path": path, "items": [], "hint": hint}

    slow = [it for it in items if (it.get("p95_rt_ms") or it.get("avg_rt_ms") or 0) >= 1000]
    note += (f"按 p95{'（缺失时用平均RT）' if any(i.get('p95_rt_ms') is None for i in items) else ''}"
             f" 取慢 Top {len(items)}；p95/平均RT ≥1000ms 视为疑似慢接口"
             f"（本次 {len(slow)} 个）")
    return {"host": host, "path": path, "items": items, "note": note}


# ---------------- 工具 4：后端健康 ----------------

def backend_health(db: Session, params: dict) -> dict:
    """域名的后端主机：资产状态、可用资源指标、每台的流量/5xx/RT。"""
    host = _clean(params.get("host"))
    if not host:
        raise ToolError("host 不能为空")
    edges = db.query(LinkEdge).filter(LinkEdge.host.ilike(f"%{host}%")).all()
    if not edges:
        return {"host": host, "items": [],
                "hint": "链路库中没有该域名的后端信息，请先用 find_domain 确认域名"}
    as_of, stale = _edge_freshness(edges)

    host_products = ("ecs", "cvm", "vmware_vm", "host")
    assets = {a.name: a for a in db.query(Asset).filter(Asset.product.in_(host_products)).all()}
    # 旧 VM 指标名只适用于阿里 ECS；其他厂商保留资产与流量证据，指标为空而非误查/误报。
    peaks: dict = {}  # name -> {"cpu": x, "mem": y, "disk": z}
    iid_name = {a.instance_id: (a.name or a.instance_id)
                for a in assets.values() if a.product == "ecs"}
    for field, prom in (
        ("cpu", "max_over_time(aliyun_ecs_cpuutilization[24h])"),
        ("mem", "max_over_time(aliyun_ecs_memory_usedutilization[24h])"),
        ("disk", "max_over_time(aliyun_ecs_diskusage_utilization[24h])"),
    ):
        try:
            now_s = int(time.time())
            result = vm_query_range(prom, now_s - 3600, now_s, 1800)
            for r in result:
                try:
                    val = float(r["values"][-1][1])
                except (KeyError, IndexError, ValueError, TypeError):
                    continue
                name = iid_name.get((r.get("metric") or {}).get("instance_id", ""))
                if not name:
                    continue
                # 磁盘按分区多序列（node=挂载点）：同一台取最满的分区，并记住是哪个盘
                cur = peaks.setdefault(name, {})
                if val > cur.get(field, 0.0):
                    cur[field] = round(val, 1)
                    if field == "disk":
                        cur["disk_part"] = (r.get("metric") or {}).get("node", "")
        except Exception as e:
            logger.warning("backend_health %s 查询失败: %s", field, e)

    items: list = []
    for e in edges:
        a = assets.get(e.ecs_name)
        p = peaks.get(e.ecs_name, {})
        items.append({
            "host": e.host, "chain": "外层" if e.chain == "outer" else "内层",
            "upstream_addr": e.upstream_addr, "ecs_name": e.ecs_name or "(未关联资产)",
            "asset_product": a.product if a else "",
            "ecs_status": (a.status if a else "未知"),
            "ecs_cpu_24h_peak": p.get("cpu"),
            "ecs_mem_24h_peak": p.get("mem"),
            "ecs_disk_24h_peak": p.get("disk"),   # 磁盘=各分区中最满的
            "ecs_disk_partition": p.get("disk_part") or "",   # 是哪个盘（/、C:\…）
            "requests_24h": e.req_count, "5xx_24h": e.cnt_5xx,
            "avg_rt_ms": e.rt_avg_ms,
        })
    out = {"host": host, "items": items[:_MAX_ROWS]}
    if as_of:
        out["data_as_of"] = as_of
        if stale:
            out["stale"] = True
            out["note"] = (f"⚠ 链路数据截至 {as_of}（超过 24h 未刷新）：requests_24h/5xx_24h/"
                           "avg_rt_ms 不是实时值；ECS 资源峰值为当前实时查询。"
                           "下结论前先到链路页刷新草稿")
    return out


# ---------------- 工具 5：应用错误日志 ----------------

def app_errors(db: Session, params: dict) -> dict:
    """应用的 ERROR 日志：计数 + 最新样本（内容摘要）。"""
    app_name = _clean(params.get("app_name"))
    if not app_name:
        raise ToolError("app_name 不能为空（来自链路库的 app_name 字段，或域名本身）")
    from app.collector.credential import resolve_sls_credential
    ak, sk, _ = resolve_sls_credential(db)
    end_s = int(time.time())
    start_s = end_s - _window(params, default="1h")

    project, logstore = app_log()
    r = sls_query_logs(ak, sk, project, logstore,
                       f'AppName: "{app_name}" and LogLevel: ERROR | '
                       'select count(*) as cnt',
                       start=start_s, end=end_s, limit=1)
    cnt = int(_num(r["logs"][0], "cnt")) if r["logs"] else 0
    if cnt <= 0:
        return {"app_name": app_name, "error_count": 0,
                "note": f"窗口内无 ERROR 日志（应用侧无错误或应用名不匹配）"}

    # 最新样本（检索模式直接取原始日志行）
    samples: list = []
    try:
        r2 = sls_query_logs(ak, sk, project, logstore,
                            f'AppName: "{app_name}" and LogLevel: ERROR',
                            start=start_s, end=end_s, limit=5)
        for row in r2["logs"]:
            content = str(row.get("Content", ""))[:200]
            ts = row.get("__time__")
            try:
                ts_str = time.strftime("%m-%d %H:%M:%S", time.localtime(int(ts))) if ts else ""
            except (TypeError, ValueError):
                ts_str = str(ts or "")
            samples.append({"time": ts_str, "content": content})
    except Exception as e:
        logger.warning("app_errors 样本查询失败: %s", e)
    return {"app_name": app_name, "error_count": cnt,
            "samples_latest": samples,
            "note": "样本为最新 5 条（内容截断 200 字符）"}


# ---------------- 工具 7/8/9：主机资源三件套 ----------------
# 资源类问题不能只报峰值或绕链路找域名，取证顺序：host_metrics 定位峰值时刻
# -> process_top 查该时刻谁在吃资源 -> biz_errors 查该时段业务是否受损（5xx/ERROR）。

_HOST_METRIC_SERIES = (
    ("mem", "aliyun_ecs_memory_usedutilization", "内存使用率%"),
    ("cpu", "aliyun_ecs_cpuutilization", "CPU使用率%"),
    ("disk", "aliyun_ecs_diskusage_utilization", "磁盘使用率%"),
)


def _resolve_instance(db: Session, params: dict) -> tuple:
    """instance/name 参数 -> (instance_id, 展示名)。资产库查不到也允许直查。"""
    key = str(params.get("instance") or params.get("name")
              or params.get("instance_id") or "").strip()
    if not key:
        raise ToolError("instance 不能为空（ECS 实例ID或名称，如 web-server-01）")
    a = (db.query(Asset).filter(Asset.instance_id == key).first()
         or db.query(Asset).filter(Asset.name == key).first()
         or db.query(Asset).filter(Asset.name.ilike(f"%{key}%")).first())
    if a is None:
        return key, key
    return a.instance_id, (a.name or a.instance_id)


def _parse_time(s) -> int | None:
    """时刻参数解析：'2026-10-07 19:05' / ISO / '10-07 19:05' -> unix 秒。"""
    if not s:
        return None
    text = str(s).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m-%d %H:%M:%S", "%m-%d %H:%M"):
        try:
            dt = datetime.strptime(text[:len(fmt) + 2], fmt)
            if fmt.startswith("%m"):
                dt = dt.replace(year=date.today().year)
            return int(dt.timestamp())
        except ValueError:
            continue
    try:
        return int(float(s))
    except (TypeError, ValueError):
        raise ToolError(f"无法解析时刻 {s}，请用 '2026-10-07 19:05' 格式")


def host_metrics(db: Session, params: dict) -> dict:
    """主机资源水位时间线：内存/CPU/磁盘的峰值时刻、峰值、当前值——资源类问题第一取证。"""
    iid, disp = _resolve_instance(db, params)
    window = str(params.get("window") or "24h")
    win_s = _window(params, default="24h")
    end_s = int(time.time())
    start_s = end_s - win_s
    step = max(60, win_s // 180)
    out = {"instance_id": iid, "instance_name": disp, "window": window, "metrics": {}}
    for field, series, label in _HOST_METRIC_SERIES:
        try:
            result = vm_query_range(f'{series}{{instance_id="{iid}"}}', start_s, end_s, step)
        except Exception as e:
            logger.warning("host_metrics %s 查询失败: %s", field, e)
            out["metrics"][field] = {"label": label, "error": str(e)[:120]}
            continue
        # 磁盘多分区（node=挂载点）：同一时刻取最满的分区并记住是哪个盘
        by_ts: dict = {}
        for sr in result:
            part = (sr.get("metric") or {}).get("node", "") or ""
            for ts, v in sr.get("values", []):
                try:
                    t, val = int(float(ts)), float(v)
                except (TypeError, ValueError):
                    continue
                if t not in by_ts or val > by_ts[t][0]:
                    by_ts[t] = (val, part)
        if not by_ts:
            out["metrics"][field] = {"label": label, "note": "窗口内无数据"}
            continue
        pts = sorted(by_ts.items())
        peak_t, (peak_v, peak_part) = max(pts, key=lambda x: x[1][0])
        cur_t, (cur_v, cur_part) = pts[-1]
        stride = max(1, len(pts) // 16)
        curve = [{"t": time.strftime("%m-%d %H:%M", time.localtime(t)), "v": round(v, 1)}
                 for t, (v, _) in pts[::stride]]
        item = {
            "label": label,
            "start": round(pts[0][1][0], 1),
            "peak": round(peak_v, 1),
            "peak_time": time.strftime("%Y-%m-%d %H:%M", time.localtime(peak_t)),
            "current": round(cur_v, 1),
            "current_time": time.strftime("%m-%d %H:%M", time.localtime(cur_t)),
            "curve": curve,
        }
        if peak_part:
            item["peak_partition"] = peak_part
            item["current_partition"] = cur_part
        out["metrics"][field] = item
    out["hint"] = ("资源类问题：以 mem/cpu/disk 的 peak_time 为事发时刻，"
                   "用 process_top 查该时刻前后谁在吃资源，用 biz_errors 查该时段业务是否报错")
    return out


def process_top(db: Session, params: dict) -> dict:
    """进程级 top（CMS process.memory / process.cpu）：谁在吃内存/CPU、何时突变。

    与主机水位配合：峰值时刻前后该进程是否显著高于其日常水位，就是"哪个进程造成的"。
    """
    iid, disp = _resolve_instance(db, params)
    metric = str(params.get("metric") or "memory").lower()
    cms_metric = "process.memory" if metric.startswith("mem") else "process.cpu"
    window = str(params.get("window") or "24h")
    win_s = _window(params, default="24h")
    around_ts = _parse_time(params.get("around"))
    end_s = int(time.time())
    start_s = end_s - win_s
    if around_ts:
        # 聚焦时刻：窗口至少覆盖 around 前后 1 小时
        start_s = min(start_s, around_ts - 3600)
        end_s = max(end_s, around_ts + 3600)

    from app.collector.cms import _client
    from app.collector.credential import resolve_sls_credential
    from alibabacloud_cms20190101 import models as cms

    ak, sk, _ = resolve_sls_credential(db)
    inst = db.query(Asset).filter(Asset.instance_id == iid).first()
    region = (inst.region if inst else None) or "cn-hangzhou"
    try:
        client = _client(ak, sk, region)
        req = cms.DescribeMetricListRequest(
            namespace="acs_ecs_dashboard", metric_name=cms_metric,
            dimensions=json.dumps([{"instanceId": iid}]), period="300",
            start_time=str(start_s * 1000), end_time=str(end_s * 1000))
        dps = json.loads(client.describe_metric_list(req).body.datapoints or "[]")
    except Exception as e:
        logger.warning("process_top 查询失败 %s %s: %s", iid, cms_metric, str(e)[:120])
        raise ToolError(f"进程数据查询失败: {str(e)[:120]}")

    by_name: dict = {}
    for d in dps:
        name = d.get("name") or "?"
        try:
            t, v = int(d["timestamp"]) // 1000, float(d.get("Value") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        rec = by_name.setdefault(name, {"pid": str(d.get("pid") or ""),
                                        "user": str(d.get("user") or ""), "pts": []})
        rec["pts"].append((t, v))
    if not by_name:
        return {"instance_id": iid, "instance_name": disp, "metric": cms_metric,
                "items": [], "note": f"窗口内无 {cms_metric} 数据（该实例未装云监控插件或无进程数据）"}

    items: list = []
    for name, rec in by_name.items():
        pts = sorted(rec["pts"])
        vals = [v for _, v in pts]
        peak_t, peak_v = max(pts, key=lambda x: x[1])
        avg = sum(vals) / len(vals)
        med = sorted(vals)[len(vals) // 2]
        items.append({
            "name": name, "pid": rec["pid"], "user": rec["user"],
            "peak": round(peak_v, 2),
            "peak_time": time.strftime("%Y-%m-%d %H:%M", time.localtime(peak_t)),
            "avg": round(avg, 2), "normal": round(med, 2),   # 日常水位（窗口中位）
            "latest": round(pts[-1][1], 2),
            # 突变标记：峰值显著高于自身日常水位（内存泄漏/任务突刺的典型形状）
            "jumped": peak_v >= max(3 * med, med + 1.0),
        })
    items.sort(key=lambda x: -x["peak"])

    focused = []
    if around_ts:
        for name, rec in by_name.items():
            win_vals = [v for t, v in rec["pts"] if abs(t - around_ts) <= 30 * 60]
            if not win_vals:
                continue
            focused.append({"name": name, "peak_around": round(max(win_vals), 2),
                            "avg_around": round(sum(win_vals) / len(win_vals), 2)})
        focused.sort(key=lambda x: -x["peak_around"])
    out = {
        "instance_id": iid, "instance_name": disp, "metric": cms_metric,
        "window": window,
        "unit_hint": "Value=CMS 原值（内存与整机内存%同量级、CPU 为占用%）；用于排序与'峰值 vs 日常水位'对比，不作绝对容量换算",
        "items": items[:_MAX_ROWS],
        "jumped_top": [i["name"] for i in items if i["jumped"]][:5],
    }
    if focused:
        out["around"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(around_ts))
        out["focused_top"] = focused[:8]
    out["hint"] = ("重点看 jumped（峰值远超日常水位）的进程——它就是造成主机资源冲高的嫌疑对象；"
                   "结合 biz_errors 判断是否伤到业务")
    return out


def biz_errors(db: Session, params: dict) -> dict:
    """业务报错核查：窗口内应用 ERROR 与 SLB 5xx 的小时分布——资源异常时段业务是否受损。"""
    window = str(params.get("window") or "24h")
    win_s = _window(params, default="24h")
    around_ts = _parse_time(params.get("around"))
    end_s = int(time.time())
    start_s = end_s - win_s
    if around_ts:
        start_s = min(start_s, around_ts - 3600)
        end_s = max(end_s, around_ts + 3600)
    app_name = _clean(params.get("app_name"))
    host = _clean(params.get("host"))

    from app.collector.credential import resolve_sls_credential
    ak, sk, _ = resolve_sls_credential(db)

    hours: dict = {}

    def _bucket(h) -> str:
        return str(h or "")[:16].replace("T", " ")

    # 1) 应用 ERROR 按小时
    app_cond = f'AppName: "{app_name}" and LogLevel: ERROR' if app_name else "LogLevel: ERROR"
    try:
        project, logstore = app_log()
        r = sls_query_logs(ak, sk, project, logstore,
                           f'{app_cond} | select date_trunc(\'hour\', __time__) as h, '
                           'count(*) as cnt group by h order by h limit 48',
                           start=start_s, end=end_s, limit=48)
        for row in r["logs"]:
            hours.setdefault(_bucket(row.get("h")), {})["app_errors"] = int(_num(row, "cnt"))
    except Exception as e:
        logger.warning("biz_errors 应用 ERROR 查询失败: %s", e)

    # 2) SLB 5xx 按小时（外层+内层合计）+ 请求量求 5xx 率
    total_5xx, total_req = 0, 0
    for project, logstore, chain in slb_targets():
        cond = f'http_host: "{host}" and status >= 500' if host else "status >= 500"
        try:
            r = sls_query_logs(ak, sk, project, logstore,
                               f'{cond} | select date_trunc(\'hour\', __time__) as h, '
                               'count(*) as cnt group by h order by h limit 48',
                               start=start_s, end=end_s, limit=48)
            for row in r["logs"]:
                cell = hours.setdefault(_bucket(row.get("h")), {})
                cell["slb_5xx"] = cell.get("slb_5xx", 0) + int(_num(row, "cnt"))
                total_5xx += int(_num(row, "cnt"))
        except Exception as e:
            logger.warning("biz_errors 5xx 查询失败 %s: %s", logstore, e)
        try:
            cond_all = f'http_host: "{host}"' if host else "*"
            r2 = sls_query_logs(ak, sk, project, logstore, f'{cond_all} | select count(*) as cnt',
                                start=start_s, end=end_s, limit=1)
            total_req += int(_num(r2["logs"][0], "cnt")) if r2["logs"] else 0
        except Exception as e:
            logger.warning("biz_errors 请求量查询失败 %s: %s", logstore, e)

    rows = [{"h": h, **hours[h]} for h in sorted(hours)]
    app_total = sum(x.get("app_errors", 0) for x in rows)
    out = {
        "window": window,
        "app_name": app_name or "（全部应用）",
        "host": host or "（全部域名）",
        "hours": rows[:_MAX_ROWS],
        "totals": {
            "app_errors": app_total,
            "slb_5xx": total_5xx,
            "slb_requests": total_req,
            "slb_5xx_rate": round(total_5xx / total_req * 100, 3) if total_req else None,
        },
        "note": ("小时分布看与资源峰值时刻是否重叠：报错跟着涨=业务已受损；"
                 "报错平稳=资源冲高尚未伤到业务（仍需处理资源）"),
    }
    if around_ts:
        out["around"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(around_ts))
    return out


def _norm_proc(name: str) -> str:
    """进程名/应用名归一：去 .exe、已知厂商前缀和尾部实例序号。

    同一服务在进程名与日志 AppName 中可能使用不同前缀，归一后才能按服务名关联。
    """
    n = re.sub(r"\.exe$", "", str(name or "").strip(), flags=re.I)
    n = re.sub(r"^(beisen|senyun)\.", "", n, flags=re.I)
    n = re.sub(r"\d+$", "", n)
    return n.lower()


def process_logs(db: Session, params: dict) -> dict:
    """进程 -> 应用日志对应：这个进程在事发时段到底在干什么。

    服务名归一匹配 AppName，再看该时段日志量形状/级别分布/内容样本——
    把 process_top 定位到的"谁吃资源"挖到"它在跑什么任务"。
    诚实边界：trace 日志无实例维度，对应关系=服务名归一+时间窗咬合，非实例硬绑定。
    """
    proc = str(params.get("process") or params.get("name") or "").strip()
    if not proc:
        raise ToolError("process 不能为空（进程名，如 nginx / java / xxx.WinService.exe）")
    window = str(params.get("window") or "24h")
    win_s = _window(params, default="24h")
    around_ts = _parse_time(params.get("around"))
    end_s = int(time.time())
    start_s = end_s - win_s
    if around_ts:
        start_s = min(start_s, around_ts - 3600)
        end_s = max(end_s, around_ts + 3600)

    from app.collector.credential import resolve_sls_credential
    ak, sk, _ = resolve_sls_credential(db)
    project, logstore = app_log()

    # 1) 窗口内 AppName 清单 -> 归一匹配进程名
    matched: dict = {}   # app_name -> cnt
    try:
        r = sls_query_logs(ak, sk, project, logstore,
                           "* | select AppName, count(*) as cnt "
                           "group by AppName order by cnt desc limit 100",
                           start=start_s, end=end_s, limit=100)
        key = _norm_proc(proc)
        for row in r["logs"]:
            app = str(row.get("AppName") or "")
            akey = _norm_proc(app)
            if key and (key == akey or key in akey or akey in key):
                matched[app] = int(_num(row, "cnt"))
    except Exception as e:
        logger.warning("process_logs AppName 匹配失败: %s", e)
    if not matched:
        return {"process": proc, "matched_apps": {}, "note":
                f"窗口内日志中未找到与 {proc} 对应的 AppName（服务未打日志、命名差异过大或日志无该应用）"}

    top_app = max(matched, key=matched.get)
    cond = f'AppName: "{top_app}"'

    # 2) 逐小时量（看形状：突刺/骤降/反弹）+ 级别分布
    hours: list = []
    levels: dict = {}
    try:
        r = sls_query_logs(ak, sk, project, logstore,
                           f'{cond} | select date_trunc(\'hour\', __time__) as h, '
                           'count(*) as cnt group by h order by h limit 48',
                           start=start_s, end=end_s, limit=48)
        hours = [{"h": _bucket_h(row.get("h")), "logs": int(_num(row, "cnt"))}
                 for row in r["logs"]]
        r2 = sls_query_logs(ak, sk, project, logstore,
                            f'{cond} | select LogLevel, count(*) as cnt '
                            'group by LogLevel order by cnt desc limit 10',
                            start=start_s, end=end_s, limit=10)
        levels = {str(row.get("LogLevel") or "?"): int(_num(row, "cnt")) for row in r2["logs"]}
    except Exception as e:
        logger.warning("process_logs 量级查询失败: %s", e)

    # 3) 事发时刻附近的内容样本（ERROR 优先，Debug 看在跑什么）
    samples: list = []
    try:
        s0 = (around_ts - 20 * 60) if around_ts else max(start_s, end_s - 1800)
        s1 = (around_ts + 20 * 60) if around_ts else end_s
        for lvl in ("Error",):
            r = sls_query_logs(ak, sk, project, logstore,
                               f'{cond} and LogLevel: {lvl}',
                               start=s0, end=s1, limit=4)
            for row in r["logs"]:
                samples.append({"level": lvl, "content": str(row.get("Content", ""))[:220]})
        r = sls_query_logs(ak, sk, project, logstore, cond, start=s0, end=s1, limit=6)
        for row in r["logs"]:
            samples.append({"level": str(row.get("LogLevel") or ""),
                            "content": str(row.get("Content", ""))[:220]})
    except Exception as e:
        logger.warning("process_logs 样本查询失败: %s", e)

    return {
        "process": proc,
        "matched_apps": matched,
        "app": top_app,
        "window": window,
        "hours": hours[:_MAX_ROWS],
        "levels": levels,
        "samples_around": samples[:8],
        "around": time.strftime("%Y-%m-%d %H:%M", time.localtime(around_ts)) if around_ts else "",
        "note": ("对应关系=服务名归一匹配 + 时间窗咬合（trace 日志无实例维度，"
                 "非实例硬绑定）；日志量骤降/反弹与资源曲线同窗=该服务确实在事发时段干重活"),
        "hint": "样本里的任务名/方法名（如 GetTaskInfos 数量 N、循环任务）就是它当时在跑的东西",
    }


def _bucket_h(h) -> str:
    return str(h or "")[:16].replace("T", " ")


def cdn_url_breakdown(db: Session, params: dict) -> dict:
    """CDN 流量突增归因：URL/文件级下钻（谁在烧、峰值日谁涨）。"""
    domain = _clean(params.get("domain") or params.get("host") or params.get("object"))
    if not domain:
        raise ToolError("domain 不能为空（CDN 域名，如 cdn.example.com）")
    days = _clamp_days(params.get("days"), 7, 1, 30)
    top = max(1, min(30, int(params.get("top") or 10)))
    from app.services.cdn_traffic import url_breakdown
    return url_breakdown(db, domain=domain.lower(), days=days, top=top)


def bill_breakdown(db: Session, params: dict) -> dict:
    """账单拆解：目标（实例/产品/计费场景）近 N 天账单行 +
    逐日趋势 + 计费场景构成——"这笔钱怎么来的"第一取证。

    统计方式：BSS 账单（source=bill），账单金额；计费场景标签取自实例配置描述。
    account_id 可选过滤（缺省=全部账号聚合），账单行带归属标识。
    """
    target = _clean(params.get("instance") or params.get("product") or params.get("target"))
    if not target:
        raise ToolError("target 不能为空（实例ID/名称/产品名，如 daspost-cn-xxx 或 对象存储）")
    days = _clamp_days(params.get("days"), 30, 1, 90)
    acc = _acc(params)
    from app.models.cost_daily import CostDaily
    since = date.today() - timedelta(days=days)
    q = account_filter(db.query(CostDaily), CostDaily, acc).filter(
        CostDaily.stat_date >= since, CostDaily.source.in_(("bill", "tc_bill")))   # 账号过滤（0=聚合）
    t = target.lower()
    def _hit(v: str) -> bool:
        v = (v or "").lower()
        return bool(v) and (t in v or (len(v) >= 2 and v in t))   # 双向包含：整句提问也能命中产品名
    rows = [r for r in q.all()
            if _hit(r.instance_id) or _hit(r.instance_name) or _hit(r.product_name)
            or _hit(r.product_code) or _hit(r.billing_item)]
    if not rows:
        return {"target": target, "rows": [], "note": f"近 {days} 天无「{target}」的账单行"}
    daily: dict = {}
    scen: dict = {}
    products = {}
    for r in rows:
        d = r.stat_date.isoformat()
        daily[d] = round(daily.get(d, 0.0) + (r.pretax_amount or 0), 2)
        s = r.billing_item or "（未标注场景）"
        scen[s] = round(scen.get(s, 0.0) + (r.pretax_amount or 0), 2)
        products[r.product_name or r.product_code] = products.get(r.product_name or r.product_code, 0) + (r.pretax_amount or 0)
    total = round(sum(daily.values()), 2)
    vals = list(daily.values())
    peak_d = max(daily, key=daily.get)
    acc_map = account_index(db)   # 账单行补充账号归属标识
    items = [{
        "date": r.stat_date.isoformat(), "product": r.product_name or r.product_code,
        "instance": r.instance_id, "scenario": r.billing_item or "（未标注）",
        "amount": round(r.pretax_amount or 0, 2),
        **account_fields(acc_map, r.account_id),
    } for r in sorted(rows, key=lambda x: (x.stat_date, -(x.pretax_amount or 0)))[:_MAX_ROWS]]
    return {
        "target": target, "days": days, "total": total, "row_count": len(rows),
        "daily": daily, "scenarios": dict(sorted(scen.items(), key=lambda kv: -kv[1])),
        "products": {k: round(v, 2) for k, v in products.items()},
        "avg_per_day": round(total / max(1, len(daily)), 2),
        "peak_day": {"date": peak_d, "amount": daily[peak_d]},
        "min_day": {"date": min(daily, key=daily.get), "amount": min(vals)},
        "rows": items,
        "note": "统计说明：amount=账单金额（pretax_amount，判断花费以此为准）；"
                "payment_amount=挂账现金字段（常 0），仅存证不作依据；计费场景=实例配置描述。"
                "看规律：工作日/假期差异=按量计费的典型形状；持续同值=开通即计费。T+1 账单非终态，近 7 天可能补账修订",
    }


def bill_config(db: Session, params: dict) -> dict:
    """计费相关配置实况：读 SQL 审计/日志的开通与存储配置——
    回答"这个功能是不是开着、免费额度多少、哪块在收钱"。只读。"""
    inst = _clean(params.get("instance") or params.get("target"))
    if not inst:
        raise ToolError("instance 不能为空（RDS 实例ID）")
    # 计费实例号形如 daspost-cn-xxx;pgm-bp-yyy——取 RDS 段（配置接口认 RDS 实例ID）
    if ";" in inst:
        parts = [x.strip() for x in inst.split(";") if x.strip()]
        inst = next((x for x in parts if not x.lower().startswith("das")), parts[-1])
    from app.collector.dba import das_client
    from app.collector.credential import resolve_sls_credential
    import alibabacloud_das20200116.models as das_models
    ak, sk, _ = resolve_sls_credential(db)
    try:
        c = das_client(ak, sk, "cn-hangzhou")
        d = (c.describe_sql_log_config(das_models.DescribeSqlLogConfigRequest(instance_id=inst))
             .body.to_map().get("Data") or {})
    except Exception as e:
        msg = str(e)[:120]
        if "InstanceNotFound" in msg or "404" in msg:
            return {"instance": inst, "applicable": False,
                    "note": f"「{inst}」不是 RDS 实例（或无计费配置可查）——"
                            "账单构成请用 bill_breakdown；资源包类看 pack_pools"}
        raise ToolError(f"配置读取失败: {msg}")
    if not d:
        return {"instance": inst, "note": "无配置返回"}
    hot = d.get("HotEnable")
    cold = d.get("ColdEnable")
    def _ts(ms):
        try:
            return datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return ""
    return {
        "instance": inst,
        "sql_log_enabled": d.get("SqlLogEnable"),
        "data_ingest_enabled": d.get("RequestEnable"),        # 数据引入（收费，按量）
        "cold_storage": {"enabled": cold, "retention_days": d.get("ColdRetention"),
                         "since": _ts(d.get("ColdStartTime")),
                         "fee_note": "≤30 天免冷存储费，超出收冷存储费"},
        "hot_index": {"enabled": hot, "retention_days": d.get("HotRetention"),
                      "since": _ts(d.get("HotStartTime")),
                      "fee_note": "日志索引（实时搜索）收费；赠送 1 天热存储，超 1 天另收热存储费"},
        "note": "免费的是存储额度；数据引入费与日志索引费按量收（跟业务 SQL 量走，人不点也扣）",
    }


def object_chain(db: Session, params: dict) -> dict:
    """对象全景因果链：一个对象近 N 天的多层同窗读数 +
    联动变化事件——跨层分析第一入口，替代"SLB→日志→应用→DB 逐个控制台翻"。"""
    key = str(params.get("object") or params.get("object_key") or params.get("host")
              or params.get("domain") or params.get("instance") or "").strip()
    if not key:
        raise ToolError("object 不能为空（域名/实例ID/名称，如 cdn.example.com）")
    days = _clamp_days(params.get("days"), 7, 1, 30)
    from app.services.object_profile import build_chain
    return build_chain(db, key, days)


# ---------------- 工具 6：下游 DB/Redis ----------------

_DB_METRICS = [
    ("rds", "aliyun_rds_cpuusage", "CPU使用率%", "warn", 80),
    ("rds", "aliyun_rds_memoryusage", "内存使用率%", "warn", 80),
    ("rds", "aliyun_rds_diskusage", "磁盘使用率%", "crit", 90),
    ("rds", "aliyun_rds_connectionusage", "连接数使用率%", "warn", 80),
    ("redis", "aliyun_redis_shardingcpuusage", "CPU使用率%", "warn", 80),
    ("redis", "aliyun_redis_shardingmemoryusage", "内存使用率%", "warn", 80),
    ("redis", "aliyun_redis_shardingconnectionusage", "连接数使用率%", "warn", 80),
    # 平均 RT 由 CMS 采集（collector/cms.py ShardingAvgRt）；诊断必须读取它，
    # 才能区分资源水位与 Redis 自身响应劣化。50ms 为集群 Redis 的明显劣化线
    # （正常多为亚毫秒到几毫秒）。
    ("redis", "aliyun_redis_shardingavgrt", "平均响应时间ms", "warn", 50),
]


def db_health(db: Session, params: dict) -> dict:
    """下游 RDS/Redis 指标与证据覆盖度；无样本/查询失败绝不解释为正常。

    status 描述证据可用性：ok=全部指标有样本，partial=仅部分指标可用，
    no_data=查询成功但零样本，error=全部查询失败。是否超阈值另看 items。
    """
    _window(params, default="24h")
    window = str(params.get("window") or "24h")
    names: dict = {}
    for product in ("rds", "redis"):
        for a in db.query(Asset).filter(Asset.product == product).all():
            names[(product, a.instance_id)] = a.name or a.instance_id
    items: list = []
    summary: list = []
    failures: list = []
    empty_metrics: list = []
    queries_succeeded = 0
    sample_series = 0
    now_s = int(time.time())
    for product, series, label, _lv, threshold in _DB_METRICS:
        try:
            result = vm_query_range(f"max_over_time({series}[{window}])",
                                    now_s - 3600, now_s, 1800)
        except Exception as e:
            logger.warning("db_health 查询失败 %s: %s", series, e)
            failures.append({"product": product.upper(), "metric": label,
                             "series": series, "error": str(e)[:200]})
            continue
        queries_succeeded += 1
        top_val, top_name = None, ""
        metric_samples = 0
        for r in result or []:
            try:
                val = float(r["values"][-1][1])
            except (KeyError, IndexError, ValueError, TypeError):
                continue
            metric_samples += 1
            iid = (r.get("metric") or {}).get("instance_id", "")
            name = names.get((product, iid), iid)
            if top_val is None or val > top_val:
                top_val, top_name = val, name
            if val >= threshold:
                items.append({
                    "product": product.upper(), "name": name,
                    "metric": label, "peak": round(val, 1),
                    "threshold": threshold,
                })
        sample_series += metric_samples
        if top_val is not None:
            summary.append({"product": product.upper(), "metric": label,
                            "peak": round(top_val, 2), "instance": top_name,
                            "threshold": threshold})
        else:
            empty_metrics.append({"product": product.upper(), "metric": label,
                                  "series": series})
    items.sort(key=lambda x: -x["peak"])
    total = len(_DB_METRICS)
    with_samples = total - len(empty_metrics) - len(failures)
    coverage = {
        "metrics_total": total, "queries_succeeded": queries_succeeded,
        "queries_failed": len(failures), "metrics_with_samples": with_samples,
        "sample_series": sample_series,
    }
    if not queries_succeeded:
        status = "error"
        note = f"近{window} RDS/Redis 指标全部查询失败，无法判断下游是否正常"
    elif not sample_series:
        status = "partial" if failures else "no_data"
        note = f"近{window} RDS/Redis 查询窗口内零指标样本，无法判断下游是否正常（无数据≠无瓶颈）"
    elif failures or empty_metrics:
        status = "partial"
        note = (f"近{window}仅 {with_samples}/{total} 项指标有样本"
                f"（{len(failures)} 项查询失败、{len(empty_metrics)} 项零样本）；"
                "以下为部分证据，不能据此宣称 RDS/Redis 全部正常")
    else:
        status = "ok"
        note = (f"近{window}超阈值指标（这些下游瓶颈可能拖慢全部业务）" if items else
                f"近{window} RDS/Redis {total} 项指标均有样本且峰值均在阈值内"
                "（summary 为各指标最高实例读数；无瓶颈迹象）")
    return {
        "status": status, "items": items[:_MAX_ROWS], "summary": summary,
        "coverage": coverage, "failed_metrics": failures,
        "no_data_metrics": empty_metrics, "note": note,
    }


# ---------------- 成本场景工具（cost_analysis） ----------------

def _fmt_norm(v: float, is_size: bool) -> str:
    """归一化量转人话（容量类转 GB/TB，计数类原值）。"""
    if is_size:
        gb = v / 1024 ** 3
        if gb >= 1024:
            return f"{gb / 1024:.2f}TB"
        if gb >= 1:
            return f"{gb:.2f}GB"
        return f"{v / 1024 ** 2:.1f}MB"
    return f"{v:,.0f}"


def _latest_snaps(db: Session) -> list:
    latest = db.query(func.max(ResourcePackSnapshot.snapshot_date)).scalar()
    if latest is None:
        return []
    return db.query(ResourcePackSnapshot).filter(
        ResourcePackSnapshot.snapshot_date == latest).all()


def pack_pools(db: Session, params: dict) -> dict:
    """全部抵扣能力池：剩余/日消耗/几天耗尽（告警统计方式就是池，不是单包）。

    account_id 可选过滤（缺省=全部账号聚合），池带归属标识。
    """
    acc = _acc(params)
    snaps = _latest_snaps(db)
    if acc:
        snaps = [s for s in snaps if (s.account_id or 0) == acc]   # 账号过滤
    if not snaps:
        return {"items": [], "note": "尚无资源包快照数据"}
    # 池聚合：剩余/总量按归一化单位；日消耗用"昨日余量-今日余量"逐日差分更直观，
    # 此处先给池构成，消耗趋势用 pool_usage_trend 查
    pools: dict = {}
    for s in snaps:
        k = pool_key(s.commodity_code, s.applicable_products)
        p = pools.setdefault(k, {
            "commodity_code": s.commodity_code, "products": s.applicable_products or "",
            "packs": 0, "rem": 0.0, "total": 0.0,
            "is_size": (s.total_unit or "").strip().upper() in _SIZE_UNITS,
            "_accs": set(),
        })
        p["packs"] += 1
        p["rem"] += norm_amount(s.remaining_amount or 0.0, s.remaining_unit)
        p["total"] += norm_amount(s.total_amount or 0.0, s.total_unit)
        p["_accs"].add(s.account_id or 0)
    items = []
    acc_map = account_index(db)   # 资源包池补充账号归属标识
    for p in sorted(pools.values(), key=lambda x: -(x["rem"] / x["total"] if x["total"] else 0)):
        zh = pack_chinese(p["commodity_code"], p["products"])
        pool_accs = p["_accs"]
        items.append({
            "type_label": zh["type_label"], "products_label": zh["products_label"],
            "commodity_code": p["commodity_code"],
            "pack_count": p["packs"],
            "remaining": _fmt_norm(p["rem"], p["is_size"]),
            "remaining_pct": round(p["rem"] / p["total"] * 100, 2) if p["total"] else 0,
            "note": "日消耗与耗尽预测请用 pool_usage_trend 查该池",
            # 池跨账号聚合时归属留空（分账号看传 account_id 过滤）
            **account_fields(acc_map, pool_accs.pop() if len(pool_accs) == 1 else 0),
        })
    return {"items": items, "note": "数据为最新快照日；消耗趋势/耗尽预测用 pool_usage_trend"}


def pool_usage_trend(db: Session, params: dict) -> dict:
    """某类资源包的每日消耗趋势（快照余量逐日差分，归一化跨单位）。

    回答"每天消耗多少、趋势是涨是跌、几天耗尽、耗尽后按量多花多少钱"。
    """
    keyword = _clean(params.get("commodity") or params.get("keyword"))
    days = _clamp_days(params.get("days"), default=14, lo=3, hi=60)
    acc = _acc(params)
    snaps = _latest_snaps(db)
    if acc:
        snaps = [s for s in snaps if (s.account_id or 0) == acc]   # 账号过滤
    if not snaps:
        return {"items": [], "note": "尚无资源包快照数据"}

    # 关键词 -> 池键（匹配 commodity_code 或适用产品）
    matched = set()
    for s in snaps:
        if not keyword or (keyword.lower() in (s.commodity_code or "").lower()
                           or keyword.lower() in (s.applicable_products or "").lower()):
            matched.add(pool_key(s.commodity_code, s.applicable_products))
    if not matched:
        return {"items": [], "hint": f"没有匹配「{keyword}」的资源包，可先 pack_pools 看全部池"}

    start = snaps[0].snapshot_date - timedelta(days=days)
    rows = account_filter(db.query(ResourcePackSnapshot), ResourcePackSnapshot, acc).filter(
        ResourcePackSnapshot.snapshot_date >= start).order_by(
        ResourcePackSnapshot.snapshot_date).all()

    # (date, pool_key) -> 归一化剩余
    daily_rem: dict = {}
    meta: dict = {}
    for s in rows:
        k = pool_key(s.commodity_code, s.applicable_products)
        if k not in matched:
            continue
        d = daily_rem.setdefault(s.snapshot_date.isoformat(), {})
        d[k] = d.get(k, 0.0) + norm_amount(s.remaining_amount or 0.0, s.remaining_unit)
        m = meta.setdefault(k, {
            "commodity_code": s.commodity_code, "products": s.applicable_products or "",
            "is_size": (s.remaining_unit or "").strip().upper() in _SIZE_UNITS,
            "_accs": set(),
        })
        m["_accs"].add(s.account_id or 0)

    items = []
    acc_map = account_index(db)   # 趋势结果补充账号归属标识
    dates = sorted(daily_rem)
    for k in sorted(matched):
        m = meta.get(k)
        if m is None:
            continue
        series = [(d, daily_rem[d].get(k)) for d in dates if daily_rem[d].get(k) is not None]
        usage = []  # (date, used)
        for i in range(1, len(series)):
            used = series[i - 1][1] - series[i][1]
            if used >= 0:
                usage.append((series[i][0], used))
        is_size = m["is_size"]
        zh = pack_chinese(m["commodity_code"], m["products"])
        pool_accs = m["_accs"]
        item = {
            "type_label": zh["type_label"], "products_label": zh["products_label"],
            "commodity_code": m["commodity_code"],
            "current_remaining": _fmt_norm(series[-1][1], is_size) if series else "-",
            "daily_usage_recent": [
                {"date": d, "used": _fmt_norm(u, is_size)} for d, u in usage[-10:]
            ],
            # 池跨账号聚合时归属留空（分账号看传 account_id 过滤）
            **account_fields(acc_map, pool_accs.pop() if len(pool_accs) == 1 else 0),
        }
        if len(usage) >= 2:
            avg = sum(u for _, u in usage) / len(usage)
            last = usage[-1][1]
            item["avg_daily_usage"] = _fmt_norm(avg, is_size)
            item["last_daily_usage"] = _fmt_norm(last, is_size)
            trend = "上升" if last > avg * 1.3 else ("下降" if last < avg * 0.7 else "持平")
            item["usage_trend"] = trend
            if series[-1][1] <= 0:
                # 当前余量已为 0：是"已耗尽"不是"即将耗尽"（0 天后耗尽有二义）
                item["exhausted"] = True
                item["note"] = (f"整池已于 {series[-1][0]} 耗尽（当前剩余 0），"
                                "抵扣范围产品用量已全部转按量计费——看按量账单确认费用影响")
            elif avg > 0:
                days_left = series[-1][1] / avg
                item["est_days_to_exhaust"] = round(days_left, 1)
                item["note"] = (f"按近{len(usage)}日平均消耗，约 {days_left:.0f} 天后整池耗尽；"
                                "耗尽后抵扣范围产品用量全部转按量计费（账单会突增）")
        items.append(item)
    return {"items": items,
            "note": "每日消耗=相邻快照日余量差分（归一化）；资源包抵扣的用量不体现在按量账单，"
                    "池耗尽后按量账单才会突增，可用 product_cost_trend 对照"}


def _clamp_days(v, default: int, lo: int, hi: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def product_cost_trend(db: Session, params: dict) -> dict:
    """某产品的日成本趋势（按量/包月拆分，T+1 账单统计方式）。

    account_id 可选过滤（缺省=全部账号聚合），趋势带归属标识。
    """
    keyword = _clean(params.get("product") or params.get("keyword"))
    days = _clamp_days(params.get("days"), default=14, lo=3, hi=90)
    acc = _acc(params)
    start = date.today() - timedelta(days=days)
    q = account_filter(db.query(CostDaily), CostDaily, acc).filter(   # 账号过滤（0=聚合）
        CostDaily.stat_date >= start)
    if keyword:
        like = f"%{keyword}%"
        q = q.filter((CostDaily.product_code.ilike(like))
                     | (CostDaily.product_name.ilike(like))
                     | (CostDaily.billing_item.ilike(like)))
    rows = q.all()
    if not rows:
        return {"items": [], "hint": f"没有匹配「{keyword}」的账单数据（注意账单 T+1，今天只有到昨天的）"}
    by_date: dict = {}
    products = set()
    row_accs = set()
    for r in rows:
        d = by_date.setdefault(r.stat_date.isoformat(), {"payg": 0.0, "subs": 0.0})
        if r.subscription_type == "PayAsYouGo":
            d["payg"] += r.pretax_amount or 0
        else:
            d["subs"] += r.pretax_amount or 0
        products.add(f"{r.product_name}({r.product_code})")
        row_accs.add(r.account_id or 0)
    dates = sorted(by_date)
    payg_series = [round(by_date[d]["payg"], 2) for d in dates]
    acc_map = account_index(db)   # 产品成本趋势补充账号归属标识
    item = {
        "products": sorted(products)[:5],
        "dates": dates,
        "payg_daily": payg_series,
        "prepaid_amortized_daily": [round(by_date[d]["subs"], 2) for d in dates],
        "payg_avg": round(sum(payg_series) / len(payg_series), 2) if payg_series else 0,
        "payg_last": payg_series[-1] if payg_series else 0,
        # 趋势跨账号聚合时归属留空（分账号看传 account_id 过滤）
        **account_fields(acc_map, row_accs.pop() if len(row_accs) == 1 else 0),
    }
    if len(payg_series) >= 4:
        first_half = sum(payg_series[: len(payg_series) // 2])
        second_half = sum(payg_series[len(payg_series) // 2:])
        if first_half > 0:
            ratio = second_half / first_half
            item["trend_note"] = (f"后半段按量费用是前半段的 {ratio:.2f} 倍"
                                  + ("（明显上涨，疑似资源包耗尽转按量——可查 pack_pools 池余量）" if ratio >= 1.5 else ""))
    return {"items": [item], "note": "账单统计方式：payg_daily=按量账单金额（pretax_amount，判断花费以此为准）；"
            "prepaid_amortized_daily=包月日摊销。金额单位：元（CNY）。"
            "payment_amount=挂账现金字段（常 0），仅存证不作依据；T+1 账单非终态，近 7 天可能补账修订"}


def instance_cost_top(db: Session, params: dict) -> dict:
    """某产品近 N 天费用 Top 实例（定位钱花在哪台/哪个域名上）。

    account_id 可选过滤（缺省=全部账号聚合），行带归属标识。
    """
    keyword = _clean(params.get("product") or params.get("keyword"))
    days = _clamp_days(params.get("days"), default=14, lo=3, hi=90)
    acc = _acc(params)
    start = date.today() - timedelta(days=days)
    q = db.query(
        CostDaily.instance_id, CostDaily.instance_name, CostDaily.product_name,
        func.sum(CostDaily.pretax_amount),
        func.count(func.distinct(CostDaily.stat_date)),
        CostDaily.account_id,
    ).filter(CostDaily.stat_date >= start).group_by(
        CostDaily.instance_id, CostDaily.instance_name, CostDaily.product_name,
        CostDaily.account_id)   # 实例×账号（ID 跨账号可能撞号）
    q = account_filter(q, CostDaily, acc)   # 账号过滤（0=聚合）
    if keyword:
        like = f"%{keyword}%"
        q = q.filter((CostDaily.product_code.ilike(like))
                     | (CostDaily.product_name.ilike(like))
                     | (CostDaily.billing_item.ilike(like)))
    rows = q.all()
    acc_map = account_index(db)   # 实例成本结果补充账号归属标识
    items = [
        {"instance": (r[1] or r[0] or "(无实例维度)"), "product": r[2] or "",
         "total": round(r[3] or 0, 2),
         # 日均按**有数据天数**折算（整窗口折算会把只用几天的实例稀释得看不出单价）
         "avg_daily": round((r[3] or 0) / (r[4] or 1), 2), "days_with_data": r[4] or 0,
         **account_fields(acc_map, r[5])}
        for r in sorted(rows, key=lambda x: -(x[3] or 0))[:15] if (r[3] or 0) > 0
    ]
    return {"items": items, "note": f"近 {days} 天合计 Top15，金额单位：元（CNY）（instance_id 空=订单/平台类费用）。"
            "total=账单金额（pretax_amount，判断花费以此为准）；avg_daily 按有数据天数折算；T+1 账单非终态，近 7 天可能补账修订"}


def cost_anomaly_list(db: Session, params: dict) -> dict:
    """近期待处理成本异动（池将耗尽/费用突增等，含确认状态）。"""
    cutoff = date.today() - timedelta(days=14)
    rows = (db.query(CostAnomaly)
            .filter(CostAnomaly.last_seen >= cutoff)
            .order_by(CostAnomaly.level, CostAnomaly.last_seen.desc())
            .limit(20).all())
    items = [{
        "type": r.type, "level": r.level, "status": r.ack_status,
        "title": r.title, "value": r.value, "last_seen": r.last_seen.isoformat(),
    } for r in rows]
    return {"items": items, "note": "type: pack_exhausting=池将耗尽 pack_exhausted=包刚耗尽 "
            "pack_expiry=将到期有富余 payg_spike=按量突增 payg_new=新增按量费用项"}


def expiry_list(db: Session, params: dict) -> dict:
    """即将到期清单（域名/证书/包年资产/资源包，30/7 天分级）。"""
    from app.services.expiry import expiry_items

    data = expiry_items(db)   # 已忽略项默认不进报告（用户可关提醒）
    within = [i for i in data["items"] if i["days_left"] <= 90]
    return {"summary": data["summary"], "items": within,
            "note": "分级：expired=已过期 critical=7天内 warning=30天内；按剩余天数升序"}


def cdn_domain_traffic(db: Session, params: dict) -> dict:
    """CDN/DCDN 按域名每日流量——资源包到底被哪个域名烧掉（每日 09:35 采集）。"""
    days = _clamp_days(params.get("days"), default=14, lo=1, hi=30)
    try:
        top_n = int(params.get("top") or 5)
    except (TypeError, ValueError):
        top_n = 5
    from app.services.cdn_traffic import domain_traffic_summary
    out = domain_traffic_summary(db, days=days, top=max(1, min(top_n, 20)))
    out["note"] = (out.get("note", "") + "；daily_gb 键为日期。")
    return out


# ---------------- 数据库专项工具（dba） ----------------

def rds_list(db: Session, params: dict) -> dict:
    """RDS 实例清单（本地资产表，含引擎/规格；先看有哪些实例再下钻）。

    account_id 可选过滤（缺省=全部账号聚合），行带归属标识。
    """
    acc = _acc(params)
    q = account_filter(db.query(Asset), Asset, acc).filter(   # 账号过滤（0=聚合）
        Asset.product == "rds", Asset.status != "Deleted")
    rows = q.all()
    acc_map = account_index(db)   # RDS 清单补充账号归属标识
    items = [{
        "instance_id": a.instance_id, "name": a.name or "",
        "status": a.status, "region": a.region or "",
        "expire_time": a.expire_time.strftime("%Y-%m-%d") if a.expire_time else "",
        **account_fields(acc_map, a.account_id),
    } for a in rows]
    return {"items": items, "note": "慢SQL用 slowlog_top 查，空间趋势用 dba_space_trend 查"}


def slowlog_top(db: Session, params: dict) -> dict:
    """某 RDS 实例的慢SQL Top（云 API，MySQL 聚合/PG DAS 明细）。"""
    ident = _clean(params.get("instance_id") or params.get("instance") or params.get("name"))
    days = _clamp_days(params.get("days"), default=7, lo=1, hi=7)
    if not ident:
        raise ToolError("instance_id 不能为空（可先 rds_list 查清单）")
    rows = db.query(Asset).filter(Asset.product == "rds", Asset.status != "Deleted").all()
    inst = next((a for a in rows if ident.lower() in (a.instance_id or "").lower()
                 or ident.lower() in (a.name or "").lower()), None)
    if inst is None:
        raise ToolError(f"找不到 RDS 实例「{ident}」，可先 rds_list 查清单")
    from app.collector.credential import resolve_sls_credential
    from app.collector.dba import fetch_slowlog_top
    ak, sk, _ = resolve_sls_credential(db)
    engine = ""
    try:
        import json as _json
        engine = (_json.loads(inst.extra or "{}").get("engine") or "").lower()
    except ValueError:
        engine = ""
    out = fetch_slowlog_top(ak, sk, inst.region or "cn-hangzhou", inst.instance_id, engine or "mysql", days)
    return {"instance": f"{inst.name}({inst.instance_id})", "engine": engine, "days": days,
            "items": out[:10] if isinstance(out, list) else out,
            "note": f"近 {days} 天慢SQL Top10；明细/洞察去 DBA 专项页看"}


def dba_space_trend(db: Session, params: dict) -> dict:
    """某 RDS 实例空间近 14 天趋势（本地快照，数据/日志/备份/磁盘）。

    统计方式：残差只是各分项与总量的统计口径差，不得解释为临时文件等实际占用。
    - data_size    数据文件：稳定增长主体，写满预测看它
    - log_size     日志：轮转摆动，不是增长
    - backup_size  备份集大小：不占实例盘，不能计入磁盘增长
    - disk_used    实例盘用量：含轮转摆动，单日尖峰≠增长
    - 残差         统计方式噪声（各分项与总量统计统计方式不重叠），禁止解释为业务实体
    """
    ident = _clean(params.get("instance_id") or params.get("instance") or params.get("name"))
    if not ident:
        raise ToolError("instance_id 不能为空（可先 rds_list 查清单）")
    from app.models.dba_space import DbaSpaceDaily
    start = date.today() - timedelta(days=14)
    rows = (db.query(DbaSpaceDaily)
            .filter(DbaSpaceDaily.stat_date >= start)
            .order_by(DbaSpaceDaily.stat_date).all())
    picked = [r for r in rows if ident.lower() in (r.instance_id or "").lower()
              or ident.lower() in (r.instance_name or "").lower()]
    if not picked:
        return {"items": [], "hint": f"没有「{ident}」的空间快照（每日 09:40 采集，次日生效）"}
    gb = 1024 ** 3
    items = [{
        "date": r.stat_date.isoformat(), "instance": r.instance_name or r.instance_id,
        "data_gb": round(r.data_size / gb, 1), "log_gb": round(r.log_size / gb, 1),
        "backup_gb": round(r.backup_size / gb, 1), "disk_used_gb": round(r.disk_used / gb, 1),
    } for r in picked]

    # 稳健净增长：data 分量日增量的中位数（单日尖峰不作数）
    data_deltas = [items[i]["data_gb"] - items[i - 1]["data_gb"] for i in range(1, len(items))]
    used_deltas = [items[i]["disk_used_gb"] - items[i - 1]["disk_used_gb"] for i in range(1, len(items))]
    log_deltas = [items[i]["log_gb"] - items[i - 1]["log_gb"] for i in range(1, len(items))]
    net_growth = 0.0
    if data_deltas:
        net_growth = sorted(data_deltas)[len(data_deltas) // 2]
    used_swing = max(used_deltas, key=abs) if used_deltas else 0.0
    log_swing = max(log_deltas, key=abs) if log_deltas else 0.0

    # 波动归因：used 单日摆动大头来自谁（log 轮转/备份临时空间）
    swing_note = ""
    if used_deltas and abs(used_swing) > max(2.0, abs(net_growth) * 3):
        swing_note = (f"used 日摆动最大 {used_swing:+.1f}GB（同期 log 摆动 {log_swing:+.1f}GB）"
                      f"——这是**备份/日志轮转的波动，不是增长**，不要按增长处置。")
    capacity_gb = 0.0
    try:
        import json as _json
        inst = picked[-1]
        from app.models.asset import Asset
        a = (db.query(Asset).filter(Asset.instance_id == inst.instance_id).first()
             or db.query(Asset).filter(Asset.name == inst.instance_name).first())
        extra = _json.loads((a.extra if a else None) or "{}")
        capacity_gb = float(extra.get("storage_capacity_gb") or 0)
    except Exception:
        capacity_gb = 0.0
    days_full = (round((capacity_gb - items[-1]["disk_used_gb"]) / net_growth, 0)
                 if capacity_gb > 0 and net_growth > 0.05 else None)

    attribution = (
        f"统计说明：data=数据文件（稳定增长主体）；log=日志（轮转摆动）；"
        f"backup=备份集大小（不占实例盘）；used=实例盘用量（含摆动）。"
        f"净增长以 data 日增量中位数为准：{net_growth:+.2f}GB/天。{swing_note}"
        + (f"当前 used {items[-1]['disk_used_gb']:.1f}GB / 容量 {capacity_gb:.0f}GB，"
           f"按净增速约 {days_full:.0f} 天写满。" if days_full is not None else
           "容量未采（asset.extra 无 storage_capacity_gb），写满天数待补采总容量后计算。")
        + " data 真在异常增长才需要下钻表级（space_suspects）；"
          "波动问题查慢SQL 答非所问，不要查。"
    )
    return {"items": items, "disk_growth_gb_14d": round(items[-1]["disk_used_gb"] - items[0]["disk_used_gb"], 1),
            "net_growth_gb_per_day": round(net_growth, 2),
            "used_swing_gb_max": round(used_swing, 1),
            "days_to_full": days_full,
            "growth_attribution": attribution,
            "note": "空间快照每日 09:40 采集；判断'会不会写满'看净增长（data），不看单日尖峰"}




# ---------------- 工具：空间嫌疑定位 ----------------

def space_suspects(db: Session, params: dict) -> dict:
    """空间上涨的表级嫌疑定位：从 SQL 证据找膨胀源与临时文件源。

    数据面：慢SQL/SQL洞察明细（含 rows_examined/写语句/SQL文本）——
    大扫描+ORDER BY/DISTINCT/unnest = 临时文件元凶；密集 UPDATE/DELETE 大扫描
    = 死元组膨胀源；SQL 文本里的表名就是"哪张表"。云 API 无表空间接口，
    这是从行为侧定位表级嫌疑的正解。
    """
    ident = _clean(params.get("instance_id") or params.get("instance") or params.get("name"))
    days = int(params.get("days") or 3)
    days = max(1, min(days, 7))
    if not ident:
        raise ToolError("instance_id 不能为空（可先 rds_list 查清单）")
    from datetime import datetime as _dt
    from app.collector.credential import resolve_sls_credential
    from app.collector.dba import fetch_slowlog_records
    ak, sk, _ = resolve_sls_credential(db)
    engine = "postgresql" if any(x in ident.lower() for x in ("pg", "postgres")) else "mysql"
    end = _dt.now()
    start = end - timedelta(days=days)
    try:
        r = fetch_slowlog_records(ak, sk, "cn-hangzhou", ident, engine, start, end, 1, 100)
    except Exception as e:
        return {"items": [], "note": f"SQL 明细查询失败: {str(e)[:120]}"}
    rows = r.get("rows", [])
    if not rows:
        return {"items": [], "note": f"近 {days} 天无慢SQL/明细记录（也可能是查询都很快——空间问题仍需看 dba_space_trend 归因）"}

    import re as _re
    def _table_of(sql: str) -> str:
        m = _re.search(r"(?:from|update|into|join)\s+[\"']?([\w\.]+)", sql or "", _re.I)
        return m.group(1) if m else "?"

    suspects: dict = {}
    for x in rows:
        sql = x.get("sql_text") or ""
        low = sql.lower()
        table = _table_of(sql)
        is_write = bool(_re.match(r"\s*(update|delete|insert|alter|create)", low))
        has_sort = any(k in low for k in ("distinct", "order by", "group by", "unnest", "join", "union"))
        scanned = x.get("rows_examined") or 0
        s = suspects.setdefault(table, {"table": table, "max_scanned": 0, "slow_count": 0,
                                        "write_count": 0, "tempfile_risk": False,
                                        "sample_sql": "", "latest": ""})
        s["slow_count"] += 1
        s["max_scanned"] = max(s["max_scanned"], scanned)
        s["latest"] = max(s["latest"], x.get("time") or "")
        if is_write:
            s["write_count"] += 1
        if has_sort and scanned >= 500_000:
            s["tempfile_risk"] = True   # 大扫描+聚合/排序 -> 超 work_mem 落临时文件
        if not s["sample_sql"]:
            s["sample_sql"] = " ".join(sql.split())[:160]

    out = sorted(suspects.values(), key=lambda s: -(s["max_scanned"]))
    for s in out:
        tags = []
        if s["tempfile_risk"]:
            tags.append("临时文件源（大扫描+聚合/排序）")
        if s["write_count"]:
            tags.append(f"膨胀源（{s['write_count']} 条写语句）")
        s["verdict"] = "、".join(tags) or "普通慢查询"
    return {
        "items": out[:10],
        "note": ("表级嫌疑定位（云 API 无表空间接口，从 SQL 行为侧推断）："
                 "「临时文件源」=大扫描+distinct/order/join/unnest，超 work_mem 落盘；"
                 "「膨胀源」=大扫描写语句堆积死元组。处置见 verdict 对应动作"),
    }

# ---------------- 注册表 ----------------

TOOLS = {
    "space_suspects": {
        "fn": space_suspects,
        "desc": "空间上涨的表级嫌疑：从 SQL 证据找膨胀源（写语句大扫描）与临时文件源（大扫描+聚合/排序），定位到表",
        "params": {"instance_id": "RDS 实例ID（可先 rds_list）", "days": "回看天数，默认 3"},
        "scenarios": ("dba",),
    },
    "recent_changes": {
        "fn": recent_changes,
        "desc": "云上变更时间线：谁在什么时候改了什么（重启/改配置/扩容等）。排障第一问——先查这个再查指标",
        "params": {"hours": "回看小时数，默认 48", "keyword": "按资源/操作/人过滤，可选",
                   "account_id": "云账号ID，缺省/0=全部账号"},
        "scenarios": ("link_fault", "cost_analysis", "dba", "host_res"),
    },
    "find_domain": {
        "desc": "按关键词查找已知链路域名（把『xxx.com打不开』对准到具体域名），返回后端/流量/健康概要",
        "params": {"keyword": "域名或关键词，如 example.com 或 www.example.com"},
        "scenarios": ("link_fault",),
        "fn": find_domain,
    },
    "slb_overview": {
        "desc": "查域名在 SLB 层的健康：状态码分布、5xx率、平均RT、按后端分组。窗口默认 24h",
        "params": {"host": "完整域名（先 find_domain 拿到）", "window": "1h/6h/24h，默认 24h"},
        "scenarios": ("link_fault",),
        "fn": slb_overview,
    },
    "slb_5xx_detail": {
        "desc": "查域名 5xx 明细：具体哪些 URI、什么状态码、哪个后端返回的",
        "params": {"host": "完整域名", "window": "1h/6h/24h，默认 24h"},
        "scenarios": ("link_fault",),
        "fn": slb_5xx_detail,
    },
    "path_latency_detail": {
        "desc": "查域名下各接口路径的响应时间（平均/p95）与 5xx：定位到底哪个 URI 慢。用户点名 /api/xxx 慢时先查这个",
        "params": {"host": "完整域名（先 find_domain 拿到）",
                   "path": "路径或前缀，如 /api/order（可选，缺省=全部 URI）",
                   "window": "1h/6h/24h，默认 24h", "top": "返回条数，默认 10"},
        "scenarios": ("link_fault",),
        "fn": path_latency_detail,
    },
    "backend_health": {
        "desc": "查域名的后端 ECS：运行状态、CPU/内存/磁盘 24h 峰值、每台的流量/5xx/RT",
        "params": {"host": "完整域名"},
        "scenarios": ("link_fault",),
        "fn": backend_health,
    },
    "app_errors": {
        "desc": "查应用 ERROR 日志：数量 + 最新样本内容（应用名 = 链路库 app_name 或域名）",
        "params": {"app_name": "应用名，如 www.example.com", "window": "1h/6h/24h，默认 1h"},
        "scenarios": ("link_fault", "host_res"),
        "fn": app_errors,
    },
    "host_metrics": {
        "desc": "查主机内存/CPU/磁盘水位时间线：峰值时刻、峰值、当前值（资源类问题先查这个定位事发时刻）",
        "params": {"instance": "ECS 实例ID或名称", "window": "1h/6h/24h，默认 24h"},
        "scenarios": ("host_res", "link_fault"),
        "fn": host_metrics,
    },
    "process_top": {
        "desc": "查进程级 top（内存或 CPU）：各进程峰值/日常水位/突变时刻——谁在吃资源",
        "params": {"instance": "ECS 实例ID或名称", "metric": "memory/cpu，默认 memory",
                   "window": "1h/6h/24h，默认 24h", "around": "聚焦时刻如 2026-10-07 19:05（可选）"},
        "scenarios": ("host_res",),
        "fn": process_top,
    },
    "biz_errors": {
        "desc": "查业务报错：应用 ERROR 与 SLB 5xx 的小时分布——资源异常时段业务是否受损",
        "params": {"window": "1h/6h/24h，默认 24h", "around": "聚焦时刻（可选）",
                   "app_name": "应用名过滤（可选）", "host": "域名过滤（可选）"},
        "scenarios": ("host_res", "link_fault"),
        "fn": biz_errors,
    },
    "process_logs": {
        "desc": "查进程对应的应用日志：服务名归一匹配 AppName，看事发时段它在跑什么（日志量形状/级别/内容样本）",
        "params": {"process": "进程名，如 nginx / java / xxx.WinService.exe",
                   "around": "事发时刻如 2026-10-07 19:04（可选）", "window": "1h/6h/24h，默认 24h"},
        "scenarios": ("host_res",),
        "fn": process_logs,
    },
    "cdn_url_breakdown": {
        "desc": "CDN 域名的 URL/文件级流量下钻：哪些文件在烧流量、峰值日较前日谁涨了（流量突增归因）",
        "params": {"domain": "CDN 域名，如 cdn.example.com", "days": "默认 7，最多 30", "top": "默认 10"},
        "scenarios": ("cost_analysis", "link_fault", "host_res"),
        "fn": cdn_url_breakdown,
    },
    "bill_breakdown": {
        "desc": "账单拆解：目标（实例/产品/场景）近 N 天账单行、逐日趋势、计费场景构成——'这笔钱怎么来的'。可按 account_id 过滤",
        "params": {"target": "实例ID/名称/产品名，如 daspost-cn-xxx 或 对象存储", "days": "默认 30，最多 90",
                   "account_id": "云账号ID过滤（可选，缺省=全部账号聚合）"},
        "scenarios": ("cost_analysis", "dba"),
        "fn": bill_breakdown,
    },
    "bill_config": {
        "desc": "计费相关配置实况：SQL 审计/日志的开通状态、冷热存储时长与收费统计方式——'是不是开着、哪块在收钱'",
        "params": {"instance": "RDS 实例ID或名称，如 rds-example"},
        "scenarios": ("cost_analysis", "dba"),
        "fn": bill_config,
    },
    "object_chain": {
        "desc": "对象全景因果链：域名/实例近 N 天的多层同窗读数（CDN/SLB/应用/主机/DB/变更/钱）+ 联动变化事件——跨层分析第一入口",
        "params": {"object": "域名或实例（如 cdn.example.com / i-xxx）", "days": "默认 7，最多 30"},
        "scenarios": ("link_fault", "host_res", "cost_analysis", "dba"),
        "fn": object_chain,
    },
    "db_health": {
        "desc": "查下游 RDS/Redis 关键指标（CPU/内存/磁盘/连接/平均RT），含各指标最高实例读数，看是否有下游瓶颈",
        "params": {"window": "1h/6h/24h，默认 24h"},
        "scenarios": ("link_fault", "dba"),
        "fn": db_health,
    },
    "pack_pools": {
        "desc": "查全部资源包抵扣能力池：类型/包数/剩余/剩余比例（告警统计方式是池不是单包）。可按 account_id 过滤",
        "params": {"account_id": "云账号ID过滤（可选，缺省=全部账号聚合）"},
        "scenarios": ("cost_analysis",),
        "fn": pack_pools,
    },
    "pool_usage_trend": {
        "desc": "查某类资源包的每日消耗趋势、平均/最近日消耗、几天耗尽（如 CDN 流量包、日志服务包）。可按 account_id 过滤",
        "params": {"commodity": "包类型关键词，如 cdn / dcdn / sls / oss / csk", "days": "趋势天数，默认 14",
                   "account_id": "云账号ID过滤（可选，缺省=全部账号聚合）"},
        "scenarios": ("cost_analysis",),
        "fn": pool_usage_trend,
    },
    "product_cost_trend": {
        "desc": "查某产品的日成本趋势（按量/包月拆分，T+1 账单统计方式），看费用是否突增。可按 account_id 过滤",
        "params": {"product": "产品关键词，如 cdn / oss / ecs / rds", "days": "趋势天数，默认 14",
                   "account_id": "云账号ID过滤（可选，缺省=全部账号聚合）"},
        "scenarios": ("cost_analysis",),
        "fn": product_cost_trend,
    },
    "instance_cost_top": {
        "desc": "查某产品近 N 天费用 Top 实例（钱具体花在哪台机器/哪个域名上）。可按 account_id 过滤",
        "params": {"product": "产品关键词", "days": "默认 14",
                   "account_id": "云账号ID过滤（可选，缺省=全部账号聚合）"},
        "scenarios": ("cost_analysis",),
        "fn": instance_cost_top,
    },
    "cost_anomaly_list": {
        "desc": "查近 14 天成本异动记录（池将耗尽/包刚耗尽/按量突增/新增费用项，含确认状态）",
        "params": {},
        "scenarios": ("cost_analysis",),
        "fn": cost_anomaly_list,
    },
    "expiry_list": {
        "desc": "查即将到期的域名/SSL证书/包年资产/资源包清单（90 天内，按剩余天数升序）",
        "params": {},
        "scenarios": ("cost_analysis",),
        "fn": expiry_list,
    },
    "cdn_domain_traffic": {
        "desc": "查 CDN/DCDN 按域名的每日流量——CDN 流量包到底被哪个域名烧掉、各占多少",
        "params": {"days": "趋势天数，默认 14", "top": "返回域名数，默认 5"},
        "scenarios": ("cost_analysis",),
        "fn": cdn_domain_traffic,
    },
    "rds_list": {
        "desc": "查 RDS 实例清单（引擎/状态/到期），下钻慢SQL或空间前先看这个。可按 account_id 过滤",
        "params": {"account_id": "云账号ID过滤（可选，缺省=全部账号聚合）"},
        "scenarios": ("dba",),
        "fn": rds_list,
    },
    "slowlog_top": {
        "desc": "查某 RDS 实例的慢SQL Top（执行次数/耗时/SQL 摘要），定位慢查询",
        "params": {"instance_id": "实例ID或名称（可先 rds_list）", "days": "1~7，默认 7"},
        "scenarios": ("dba",),
        "fn": slowlog_top,
    },
    "dba_space_trend": {
        "desc": "查某 RDS 实例空间近 14 天趋势（数据/日志/备份/磁盘，含 14 天增量）",
        "params": {"instance_id": "实例ID或名称（可先 rds_list）"},
        "scenarios": ("dba",),
        "fn": dba_space_trend,
    },
}


def tools_for_scenario(scenario: str) -> dict:
    """某场景可用的工具子集（未知名场景返回全部——兜底行为）。"""
    if scenario not in ("link_fault", "cost_analysis", "dba", "host_res"):
        return TOOLS
    return {n: s for n, s in TOOLS.items() if scenario in s.get("scenarios", ())}


def execute_tool(db: Session, tool: str, params: dict) -> dict:
    """执行一个白名单诊断工具。"""
    spec = TOOLS.get(tool)
    if spec is None:
        raise ToolError(f"未知工具 {tool}，可选: {', '.join(TOOLS)}")
    if not isinstance(params, dict):
        raise ToolError("params 必须是对象")
    return spec["fn"](db, params)
