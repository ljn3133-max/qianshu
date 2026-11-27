"""ES(Elasticsearch) 日志查询适配。

只读查询（与 collector/sls.py / loki.py / cls.py 同构，统一形状可平移）：
- query_es：POST {url}/{index}/_search —— query 以 ``{`` 开头按 query DSL 解析
  （取其 "query" 键，缺省 match_all），否则按 Lucene query_string；空查询退 match_all；
  时间范围统一注入 ``@timestamp`` range filter（epoch_millis，左闭右闭）+
  sort @timestamp desc + track_total_hits（has_more 精确）；
- list_indices：GET /_cat/indices 索引清单（隐藏索引剔除）。

统一返回形状：{logs: [dict], is_sql: False, count: int, has_more: bool}
  - 行 = _source 平铺 + __time__（@timestamp 解析为秒，与 SLS/CLS 口径一致）
    + _index/_id 元数据；无 _source 退空字典补元数据。

鉴权（只发往用户自己配置的地址）：headers 透传；user/password -> Basic；token -> Bearer。
seam：_call 可被 mock 测试替换（无真实 ES 依赖即可验字段映射）。
"""
from __future__ import annotations

import base64
import json
import logging
from datetime import datetime

logger = logging.getLogger("qianshu.es_query")


class ESQueryError(RuntimeError):
    """ES 查询失败（地址/索引/语法/连通等）。"""


def auth_headers(src: dict) -> dict:
    """数据源配置 -> 请求头（headers 优先；user/password=Basic；token=Bearer）。"""
    h = dict(src.get("headers") or {})
    user = (src.get("user") or "").strip()
    password = src.get("password") or ""
    token = (src.get("token") or "").strip()
    if user and password and "Authorization" not in h:
        raw = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        h["Authorization"] = f"Basic {raw}"
    elif token and "Authorization" not in h:
        h["Authorization"] = f"Bearer {token}"
    return h


# ---------------- 调用层（seam：mock 测试替换 _call） ----------------

def _call(url: str, index: str, body: dict,
          headers: dict | None = None, verify_ssl: bool = False) -> dict:
    """POST {url}/{index}/_search，返回响应 JSON（只读）。"""
    import httpx

    base = (url or "").rstrip("/")
    if not base:
        raise ESQueryError("ES 数据源未配置（LOG_SOURCES 里 type=es）")
    try:
        r = httpx.post(f"{base}/{index}/_search", json=body,
                       headers=headers or {}, timeout=30, verify=verify_ssl)
    except Exception as e:
        raise ESQueryError(f"ES 请求失败: {type(e).__name__}: {str(e)[:120]}") from e
    try:
        data = r.json()
    except Exception as e:
        raise ESQueryError(f"ES 响应非 JSON（HTTP {r.status_code}）") from e
    if r.status_code >= 400:
        err = data.get("error") if isinstance(data, dict) else None
        reason = ""
        if isinstance(err, dict):
            reason = str(err.get("reason") or err.get("type") or "")[:160]
        elif err:
            reason = str(err)[:160]
        raise ESQueryError(f"ES HTTP {r.status_code}: {reason or r.text[:160]}")
    return data if isinstance(data, dict) else {}


# ---------------- 查询体构造（纯函数，mock 测试直接验） ----------------

def build_body(query: str, start: int, end: int,
               limit: int = 20, offset: int = 0) -> dict:
    """检索请求体：DSL/Lucene 双模 + @timestamp 时间范围注入。"""
    q: dict
    stripped = (query or "").strip()
    if not stripped:
        q = {"match_all": {}}
    elif stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except ValueError as e:
            raise ESQueryError(f"ES query DSL 解析失败: {str(e)[:120]}") from e
        if not isinstance(parsed, dict):
            raise ESQueryError("ES query DSL 必须是 JSON 对象")
        q = parsed.get("query") if isinstance(parsed.get("query"), dict) else parsed
    else:
        q = {"query_string": {"query": stripped}}
    return {
        "query": {
            "bool": {
                "must": [q],
                # 时间过滤按 @timestamp 注入（无该字段的索引结果为空——语法帮助已注明）
                "filter": [{"range": {"@timestamp": {
                    "gte": int(start) * 1000, "lte": int(end) * 1000}}}],
            }
        },
        "size": int(limit),
        "from": int(offset),
        "sort": [{"@timestamp": {"order": "desc", "unmapped_type": "date"}}],
        "track_total_hits": True,
    }


# ---------------- 字段映射 ----------------

def _parse_ts(v) -> int | None:
    """@timestamp（ISO 串/epoch 秒/毫秒）-> epoch 秒；解析不了返回 None。"""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        n = float(v)
        return int(n / 1000) if n > 1e11 else int(n)
    s = str(v).strip()
    try:
        n = float(s)
        return int(n / 1000) if n > 1e11 else int(n)
    except ValueError:
        pass
    try:
        # py3.9 fromisoformat 不认 'Z' 后缀，统一换 +00:00
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except ValueError:
        return None


def _log_row(hit: dict) -> dict:
    """ES hit -> 统一行（_source 平铺 + __time__ + _index/_id 元数据）。"""
    row: dict = {}
    src = hit.get("_source") or {}
    if isinstance(src, dict):
        row.update({str(k): v for k, v in src.items()})
    elif src:
        row["_line"] = str(src)
    ts = _parse_ts(src.get("@timestamp") if isinstance(src, dict) else None)
    if ts is None:
        sort_v = (hit.get("sort") or [None])[0]
        ts = _parse_ts(sort_v)
    if ts is not None:
        row["__time__"] = ts
    if hit.get("_index"):
        row["_index"] = hit["_index"]
    if hit.get("_id") is not None:
        row["_id"] = hit["_id"]
    return row


# ---------------- 查询 / 清单 ----------------

def query_es(url: str, index: str, query: str, start: int, end: int,
             limit: int = 20, offset: int = 0,
             headers: dict | None = None, verify_ssl: bool = False) -> dict:
    """检索一个索引（只读）。index 支持通配（如 logs-*）。返回统一形状。"""
    if not index:
        raise ESQueryError("ES 索引未指定（target=索引名，支持通配如 logs-*）")
    body = build_body(query, start, end, limit=limit, offset=offset)
    resp = _call(url, index, body, headers=headers, verify_ssl=verify_ssl)
    hits = resp.get("hits") or {}
    total = hits.get("total")
    total_n = int(total.get("value", 0)) if isinstance(total, dict) else int(total or 0)
    logs = [_log_row(h) for h in (hits.get("hits") or []) if isinstance(h, dict)]
    count = len(logs)
    return {"logs": logs, "is_sql": False, "count": count,
            "has_more": (int(offset) + count) < total_n}


def list_indices(url: str, headers: dict | None = None,
                 verify_ssl: bool = False) -> list:
    """索引清单（/_cat/indices?format=json，隐藏索引剔除）。返回 [{index, docs, size}]。"""
    import httpx

    base = (url or "").rstrip("/")
    if not base:
        raise ESQueryError("ES 数据源未配置（LOG_SOURCES 里 type=es）")
    try:
        r = httpx.get(f"{base}/_cat/indices",
                      params={"format": "json", "h": "index,docs.count,store.size",
                              "bytes": "b"},
                      headers=headers or {}, timeout=15, verify=verify_ssl)
    except Exception as e:
        raise ESQueryError(f"ES 请求失败: {type(e).__name__}: {str(e)[:120]}") from e
    if r.status_code >= 400:
        raise ESQueryError(f"ES HTTP {r.status_code}: {r.text[:160]}")
    try:
        rows = r.json()
    except Exception as e:
        raise ESQueryError(f"ES 响应非 JSON（HTTP {r.status_code}）") from e
    out = []
    for x in rows if isinstance(rows, list) else []:
        name = str(x.get("index") or "")
        if not name or name.startswith("."):
            continue
        out.append({"index": name, "docs": int(x.get("docs.count") or 0),
                    "size": str(x.get("store.size") or "")})
    out.sort(key=lambda d: d["index"])
    return out[:500]
