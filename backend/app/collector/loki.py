"""Loki 日志查询适配：
与 sls.query_logs 同形状，日志类工具可平移。

Loki API: GET {url}/loki/api/v1/query_range?query=...&start=...&end=...
返回 {logs: [...], is_sql: False, count, has_more}（统一形状）。
行 = stream 标签 + _line（原始行）+ _time（纳秒原文）+ __time__（秒，统一时间字段
——SLS/CLS 同名同义，前端 fmtTime 直接可用）。
"""
from __future__ import annotations

import logging
import time

import httpx

logger = logging.getLogger("qianshu.loki")


class LokiQueryError(RuntimeError):
    pass


def query_loki(url: str, logql: str, start: int, end: int, limit: int = 100,
               headers: dict | None = None, verify_ssl: bool = False) -> dict:
    """LogQL 查询（只读）。url=Loki 根地址；headers=HTTP 鉴权（如 X-Scope-OrgID）。"""
    if not url:
        raise LokiQueryError("Loki 数据源未配置（LOG_SOURCES 里 type=loki）")
    try:
        r = httpx.get(f"{url.rstrip('/')}/loki/api/v1/query_range",
                      params={"query": logql, "start": start, "end": end,
                              "limit": limit},
                      timeout=30, headers=headers or {}, verify=verify_ssl)
        r.raise_for_status()
        data = r.json().get("data") or {}
        logs = []
        for result in data.get("result") or []:
            stream = result.get("stream") or {}
            for ts, line in result.get("values") or []:
                row = {**stream, "_time": ts, "_line": line}
                try:
                    # Loki values 时间戳是纳秒（串或数字）-> 秒，统一 __time__ 口径
                    row["__time__"] = int(int(ts) // 1_000_000_000)
                except (TypeError, ValueError):
                    pass
                logs.append(row)
        return {"logs": logs[:limit], "is_sql": False,
                "count": len(logs), "has_more": len(logs) >= limit}
    except LokiQueryError:
        raise
    except Exception as e:
        raise LokiQueryError(f"Loki 查询失败: {str(e)[:150]}") from e
