"""腾讯云 CLS 日志服务只读查询适配。

只读数据源（字段映射依据 tencentcloud.cls.v20201016 SDK 模型 introspection）：
- DescribeTopics：日志主题清单（TopicId/TopicName/LogsetId/Index/Period/StorageType）；
- SearchLog：检索/分析（From/To 毫秒、QueryString 检索条件[|SQL]、TopicId 单主题、
  Context 翻页），返回 Results（LogInfo 原始日志）或 AnalysisResults+ColNames（SQL 统计）。

签名：TC3-HMAC-SHA256（腾讯云 API 3.0 通用签名，cls.tencentcloudapi.com / 2020-10-16）——
**纯标准库实现**（hashlib/hmac/base64，不依赖 tencentcloud SDK；SDK 仅作 mock 测试的
签名对照基准与字段模型来源）。seam：_call 可被 mock 测试替换。

统一返回形状（与 collector/sls.py::query_logs / collector/loki.py::query_loki 同构，
日志类工具可平移）：{logs: [dict], is_sql: bool, count: int, has_more: bool}
  - 原始日志行 = LogJson（JSON 串）解析出的 KV 平铺；LogJson 缺失退 RawLog（{"_line"}）；
    补 __time__（毫秒）与 _topic_name/_host/_source 元数据（仅检索模式补 __time__，
    与 SLS 口径一致——SQL 结果的 time 是分析时刻无意义）；
  - SQL 分析行 = ColNames × AnalysisResults（LogItem KV）平铺。

字段映射以公开 API 文档和 SDK 模型为准；接入真实账号时仍需核对返回结构。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime

logger = logging.getLogger("qianshu.cls")

SERVICE = "cls"
API_VERSION = "2020-10-16"
DEFAULT_ENDPOINT = "cls.tencentcloudapi.com"
_MAX_PAGES = 50
_PAGE_SIZE = 100   # DescribeTopics Limit 上限


class CLSQueryError(RuntimeError):
    """code 属性保存业务错误码（与 SLSQueryError/TCBSSError 同构）。"""

    def __init__(self, msg: str, code: str = ""):
        super().__init__(msg)
        self.code = code


# ---------------- TC3-HMAC-SHA256 签名（纯标准库） ----------------
# 构造与 tencentcloud-sdk-python 逐字同构（abstract_client._get_tc3_signature）：
# POST JSON 体，canonical headers 只签 content-type;host，payload=请求体原文。

def tc3_signature(secret_key: str, date: str, service: str, string_to_sign: str) -> str:
    """TC3 派生签名密钥链（TC3+key -> date -> service -> tc3_request）后对串签名。"""
    def _hmac(key: bytes, msg: str) -> "hmac.HMAC":
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256)

    k_date = _hmac(("TC3" + secret_key).encode("utf-8"), date)
    k_service = _hmac(k_date.digest(), service)
    k_signing = _hmac(k_service.digest(), "tc3_request")
    return _hmac(k_signing.digest(), string_to_sign).hexdigest()


def tc3_auth_headers(action: str, params: dict, secret_id: str, secret_key: str,
                     host: str = DEFAULT_ENDPOINT, region: str = "",
                     timestamp: int | None = None) -> dict:
    """一次 CLS API 调用的全部请求头（含 Authorization）。timestamp 可注入供测试定值。"""
    ts = int(timestamp if timestamp is not None else time.time())
    date = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
    payload = json.dumps(params)
    payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    content_type = "application/json"
    canonical_headers = f"content-type:{content_type}\nhost:{host}\n"
    signed_headers = "content-type;host"
    canonical_request = f"POST\n/\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    credential_scope = f"{date}/{SERVICE}/tc3_request"
    string_to_sign = (f"TC3-HMAC-SHA256\n{ts}\n{credential_scope}\n"
                      f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}")
    signature = tc3_signature(secret_key, date, SERVICE, string_to_sign)
    auth = (f"TC3-HMAC-SHA256 Credential={secret_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}")
    headers = {
        "Authorization": auth,
        "Content-Type": content_type,
        "Host": host,
        "X-TC-Action": action,
        "X-TC-Timestamp": str(ts),
        "X-TC-Version": API_VERSION,
    }
    if region:
        headers["X-TC-Region"] = region
    return headers


# ---------------- 端点/区域解析 ----------------

def endpoint_and_region(url: str) -> tuple[str, str]:
    """数据源 url -> (host, region)。

    标准形态 https://cls.<region>.tencentcloudapi.com（region 从域名取）；
    全局端点 cls.tencentcloudapi.com 退默认 ap-guangzhou（CLS 主题按区域隔离，
    region 走 X-TC-Region 头）。url 允许裸域名。
    """
    import re

    raw = (url or "").strip()
    raw = re.sub(r"^https?://", "", raw).split("/")[0].strip()
    host = raw or DEFAULT_ENDPOINT
    m = re.fullmatch(r"cls\.([a-z0-9-]+)\.tencentcloudapi\.com", host)
    region = m.group(1) if m else "ap-guangzhou"
    return host, region


# ---------------- 调用层（seam：mock 测试替换 _call） ----------------

def _call(ak: str, sk: str, action: str, params: dict,
          host: str = DEFAULT_ENDPOINT, region: str = "") -> dict:
    """POST 一次 CLS 公共 API，返回 Response 体（只读动作）。"""
    import httpx

    headers = tc3_auth_headers(action, params, ak, sk, host=host, region=region)
    try:
        r = httpx.post(f"https://{host}/", json=params, headers=headers, timeout=30)
    except Exception as e:
        raise CLSQueryError(f"CLS 请求失败: {type(e).__name__}: {str(e)[:120]}") from e
    try:
        body = r.json()
    except Exception as e:
        raise CLSQueryError(f"CLS 响应非 JSON（HTTP {r.status_code}）") from e
    resp = body.get("Response") or {}
    err = resp.get("Error")
    if err:
        raise CLSQueryError(f"CLS {err.get('Code')}: {str(err.get('Message'))[:160]}",
                            code=str(err.get("Code") or ""))
    return resp


# ---------------- 日志主题清单（DescribeTopics） ----------------

def list_topics(ak: str, sk: str, url: str = "", name_filter: str = "") -> list:
    """可见日志主题清单（只读，分页拉全）。返回 [{topic_id, topic_name, logset_id, ...}]。

    name_filter 为空=全部；否则按 topicName 模糊过滤（DescribeTopics Filters）。
    """
    host, region = endpoint_and_region(url)
    out: list = []
    offset = 0
    for _ in range(_MAX_PAGES):
        params: dict = {"Offset": offset, "Limit": _PAGE_SIZE}
        if name_filter:
            params["Filters"] = [{"Key": "topicName", "Values": [name_filter]}]
        resp = _call(ak, sk, "DescribeTopics", params, host=host, region=region)
        for t in (resp.get("Topics") or []):
            out.append({
                "topic_id": t.get("TopicId") or "",
                "topic_name": t.get("TopicName") or "",
                "logset_id": t.get("LogsetId") or "",
                "index": bool(t.get("Index")),
                "status": bool(t.get("Status")),
                "storage_type": t.get("StorageType") or "",
                "period": t.get("Period"),
                "create_time": t.get("CreateTime") or "",
            })
        offset += _PAGE_SIZE
        total = int(resp.get("TotalCount") or 0)
        if not resp.get("Topics") or offset >= total:
            break
    return out


# ---------------- 检索（SearchLog） -> 统一形状 ----------------

def _log_row(item: dict) -> dict:
    """LogInfo -> 统一行（LogJson KV 平铺；缺则 RawLog 兜底；补时间/元数据）。"""
    row: dict = {}
    raw_json = item.get("LogJson") or ""
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                row.update({str(k): v for k, v in parsed.items()})
        except ValueError:
            row["_line"] = raw_json
    if not row and (item.get("RawLog") or ""):
        row["_line"] = item["RawLog"]
    ts = item.get("Time")
    if ts is not None:
        row["__time__"] = ts   # 毫秒，与 SLS __time__ 同语义
    for key, val in (("_topic_name", item.get("TopicName")),
                     ("_host", item.get("HostName")),
                     ("_source", item.get("Source")),
                     ("_file", item.get("FileName"))):
        if val:
            row.setdefault(key, val)
    return row


def _analysis_rows(col_names: list, analysis_results: list) -> list:
    """SQL 统计结果 -> 行字典（ColNames × AnalysisResults[LogItem]）。"""
    rows: list = []
    names = [str(c) for c in (col_names or [])]
    for items in (analysis_results or []):
        row: dict = {}
        if isinstance(items, list):
            for i, kv in enumerate(items):
                key = names[i] if i < len(names) else f"col{i}"
                row[key] = kv.get("Value") if isinstance(kv, dict) else kv
        rows.append(row)
    return rows


def query_cls(ak: str, sk: str, topic_id: str, query: str,
              start: int, end: int, limit: int = 100,
              url: str = "", context: str = "") -> dict:
    """检索/分析一个日志主题（只读）。

    query 含 `|` 视为 SQL 分析（与 SLS 口径一致）；start/end 秒 -> From/To 毫秒
    （左闭右开）。返回统一形状 {logs, is_sql, count, has_more}。
    """
    if not topic_id:
        raise CLSQueryError("CLS 日志主题未指定（TopicId）")
    host, region = endpoint_and_region(url)
    query = (query or "").strip() or "*"
    is_sql = "|" in query
    params = {
        "TopicId": topic_id,
        "From": int(start) * 1000,
        "To": int(end) * 1000,
        "QueryString": query,
        "QuerySyntax": 1,
        "Sort": "desc",
        "Limit": int(limit),
    }
    if context:
        params["Context"] = context
    try:
        resp = _call(ak, sk, "SearchLog", params, host=host, region=region)
    except CLSQueryError:
        raise
    except Exception as e:   # 统一转业务异常，错误信息透出给前端
        raise CLSQueryError(str(e)[:300]) from e

    analysis = bool(resp.get("Analysis")) or is_sql
    if analysis:
        logs = _analysis_rows(resp.get("ColNames") or [], resp.get("AnalysisResults") or [])
    else:
        logs = [_log_row(x) for x in (resp.get("Results") or [])]
    count = len(logs)
    list_over = bool(resp.get("ListOver"))
    return {
        "logs": logs[:limit],
        "is_sql": analysis,
        "count": count,
        # SQL 模式行数由语句自身 limit 决定（无翻页概念，同 SLS 口径）；
        # 检索模式 ListOver=False 即还有后续（Context 翻页）
        "has_more": (not analysis) and ((not list_over) or count >= limit),
    }


def probe(ak: str, sk: str, url: str = "") -> dict:
    """探活（只读）：DescribeTopics Limit=1 取可见主题数——凭证有效即返回。

    返回 {ok, topics, detail}；失败 {ok: False, detail}（不抛——探活单源失败不阻断体检）。
    """
    try:
        host, region = endpoint_and_region(url)
        resp = _call(ak, sk, "DescribeTopics",
                     {"Offset": 0, "Limit": 1}, host=host, region=region)
        total = int(resp.get("TotalCount") or 0)
        return {"ok": True, "topics": total,
                "detail": f"CLS 可达 · 可见日志主题 {total} 个（{region}）"}
    except Exception as e:
        return {"ok": False, "topics": 0,
                "detail": f"{type(e).__name__}: {str(e)[:120]}"}
