"""CDN/DCDN 按域名流量聚合。

数据来自 cdn_domain_daily 表（collector/cdn_traffic.py 每日 09:35 采昨日）。
诊断工具 cdn_domain_traffic 与 GET /cost/cdn-domains（Cost.vue 域名消耗页签）
共用这一个聚合口径。
"""
from __future__ import annotations

import time
from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.models.cdn_domain_daily import CdnDomainDaily


def domain_traffic_summary(db: Session, days: int = 14, top: int = 10) -> dict:
    """近 days 天按域名聚合：总量/占比/日均/最近一日 + 每日序列。

    返回 {window_days, grand_total_gb, avg_daily_gb, domains: [...], note}。
    days/top 由调用方 clamp；表空时 domains 为空（note 说明采集节奏）。
    """
    cutoff = date.today() - timedelta(days=days)
    rows = (db.query(CdnDomainDaily)
            .filter(CdnDomainDaily.stat_date >= cutoff)
            .order_by(CdnDomainDaily.stat_date).all())
    if not rows:
        return {"window_days": 0, "grand_total_gb": 0, "avg_daily_gb": 0,
                "domains": [],
                "note": "暂无采集数据（每日 09:35 采昨日；刚部署需等一轮）"}
    agg: dict = {}
    day_totals: dict = {}
    for r in rows:
        g = agg.setdefault(r.domain, {"product": r.product, "total": 0, "daily": {}})
        g["total"] += r.traffic_bytes or 0
        g["daily"][r.stat_date.isoformat()] = (r.traffic_bytes or 0) / 1024 ** 3
        k = r.stat_date.isoformat()
        day_totals[k] = day_totals.get(k, 0) + (r.traffic_bytes or 0)
    grand = sum(g["total"] for g in agg.values())
    n_days = max(1, len(day_totals))
    ranked = sorted(agg.items(), key=lambda kv: -kv[1]["total"])[:top]
    domains = [{
        "domain": d, "product": g["product"],
        "total_gb": round(g["total"] / 1024 ** 3, 2),
        "share_pct": round(g["total"] / grand * 100, 1) if grand else 0,
        "avg_daily_gb": round(g["total"] / n_days / 1024 ** 3, 2),
        "last_daily_gb": round(g["daily"][max(g["daily"])], 2),
        "daily_gb": g["daily"],
    } for d, g in ranked]
    return {
        "window_days": n_days,
        "grand_total_gb": round(grand / 1024 ** 3, 2),
        "avg_daily_gb": round(grand / n_days / 1024 ** 3, 2),
        "domains": domains,
        "note": "口径：CDN/DCDN 下行流量（流量包抵扣对象），北京自然日；"
                "与资源包池消耗同量级（抵扣精度/监控粒度边界略有差异）",
    }


def url_breakdown(db: Session, domain: str, days: int = 7, top: int = 10) -> dict:
    """URL/文件级流量下钻，用于定位 CDN 突增的具体来源。

    数据=SLS CDN 实时日志（domain/uri/response_size/return_code），比 T+1 的
    cdn_domain_daily 多出文件维度；SLS 保留期约 7 天，更早的如实缺日。
    返回每日总量 + Top URL（占比/请求/逐日序列/最近日较前日增减=突增归因）。
    """
    import logging
    from app.collector.credential import resolve_sls_credential
    from app.collector.sls import query_logs as sls_query_logs
    from app.services.sls_targets import cdn_target

    logger = logging.getLogger("qianshu.cdn_traffic")
    GB = 1024 ** 3
    ak, sk, _ = resolve_sls_credential(db)
    end_s = int(time.time())
    start_s = end_s - days * 86400

    def _q(sql: str, limit: int = 200) -> list:
        try:
            return sls_query_logs(ak, sk, *cdn_target(), sql,
                                  start=start_s, end=end_s, limit=limit)["logs"]
        except Exception as e:
            logger.warning("CDN URL 下钻查询失败: %s", str(e)[:100])
            return []

    def _f(v) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    def _dk(v) -> str:
        return str(v or "")[:10]

    daily_total = {}
    for row in _q(f'domain: "{domain}" | select date_trunc(\'day\', __time__) as d, '
                  'sum(response_size) as bytes group by d order by d limit 40', 40):
        daily_total[_dk(row.get("d"))] = round(_f(row.get("bytes")) / GB, 3)

    top_rows = _q(f'domain: "{domain}" | select uri, sum(response_size) as bytes, '
                  'count(*) as reqs, count_if(return_code >= 500) as e5 '
                  f'group by uri order by bytes desc limit {top}', top)
    if not top_rows:
        return {"domain": domain, "days": days, "urls": [], "daily_total": daily_total,
                "note": f"窗口内无 {domain} 的 CDN 日志（SLS 保留约 7 天，或域名不走 CDN）"}

    top_uris = [str(r.get("uri") or "") for r in top_rows]
    # 逐日序列按 Top URL 精确过滤（不限制行数截断——之前 group by 全量 limit 会漏掉
    # 小日子的行，导致逐日全 0、delta 恒 0）
    per_uri_day: dict = {}
    if top_uris:
        cond = " or ".join(f'uri: "{u[:120]}"' for u in top_uris)
        for row in _q(f'domain: "{domain}" and ({cond}) | '
                      "select date_trunc('day', __time__) as d, uri, "
                      'sum(response_size) as bytes group by d, uri order by d asc limit 400', 400):
            u = str(row.get("uri") or "")
            per_uri_day.setdefault(u, {})[_dk(row.get("d"))] = round(_f(row.get("bytes")) / GB, 3)

    total_window = sum(_f(r.get("bytes")) for r in top_rows) or 1
    days_sorted = sorted(daily_total)
    # 突增归因：对比**峰值日 vs 前一日**（今天半日没可比性；找窗口内最高那天才是突增点）
    peak = max(daily_total, key=daily_total.get) if daily_total else None
    prev_of_peak = days_sorted[days_sorted.index(peak) - 1] if peak and days_sorted.index(peak) > 0 else None
    urls = []
    for r in top_rows:
        u = str(r.get("uri") or "")[:200]
        daily = per_uri_day.get(u, {})
        gb = _f(r.get("bytes")) / GB
        peak_v = daily.get(peak or "", 0.0)
        prev_v = daily.get(prev_of_peak or "", 0.0)
        urls.append({
            "uri": u,
            "total_gb": round(gb, 3),
            "share_pct": round(_f(r.get("bytes")) / total_window * 100, 1),
            "requests": int(_f(r.get("reqs"))),
            "e5xx": int(_f(r.get("e5"))),
            "peak_day_gb": peak_v,
            "delta_gb": round(peak_v - prev_v, 3) if peak else None,
            "daily_gb": daily,
        })
    urls.sort(key=lambda x: -x["total_gb"])
    return {
        "domain": domain, "days": days, "urls": urls, "daily_total": daily_total,
        "peak_day": peak, "prev_day": prev_of_peak,
        "note": "口径=SLS CDN 实时日志（response_size），北京自然日；SLS 保留约 7 天，"
                "更早日子缺日属正常。突增归因看「峰值日较前日」列——涨的就是元凶",
    }
