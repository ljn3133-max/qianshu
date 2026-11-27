"""SLS 日志查询封装（只读）：project/logstore 列表、日志检索与 SQL 分析。

注意：
- 带 ``| select`` 的分析语句必须走 execute_logstore_sql（GetLogs 会返回空结果），
  其 SDK 签名是 ``(project, logstore, from_time, to_time, sql, power_sql)``，不接收 GetLogsRequest。
- SDK 0.9.50 里 project 是 dict（projectName 字段），不是对象属性。
- SLS 检索接口 line 上限 100，翻页用 offset。
"""
from __future__ import annotations

import json
import logging
import re

from app.core.config import settings

logger = logging.getLogger("qianshu.sls")


class SLSQueryError(RuntimeError):
    """SLS 查询失败（凭证/参数/语法等）。"""


def normalize_endpoint(endpoint: str = "") -> str:
    """接受控制台常见 URL 或 host，统一为 SLS SDK 所需 endpoint。"""
    from urllib.parse import urlsplit

    raw = (endpoint or settings.SLS_ENDPOINT).strip()
    if "://" in raw:
        return urlsplit(raw).netloc
    return raw.split("/", 1)[0]


def _client(ak: str, sk: str, endpoint: str = ""):
    from aliyun.log import LogClient

    return LogClient(normalize_endpoint(endpoint), ak, sk)


def _project_name(p) -> str:
    if isinstance(p, dict):
        return p.get("projectName") or p.get("project_name") or ""
    return getattr(p, "project_name", "") or getattr(p, "projectName", "")


def list_projects(ak: str, sk: str, endpoint: str = "") -> list[str]:
    """全量 project 名（分页拉取）。"""
    client = _client(ak, sk, endpoint)
    names: list[str] = []
    offset, size = 0, 100
    while True:
        resp = client.list_project(offset, size)
        batch = resp.get_projects() or []
        names.extend(_project_name(p) for p in batch if _project_name(p))
        if len(batch) < size:
            break
        offset += size
    return sorted(names)


def list_projects_detailed(ak: str, sk: str, endpoint: str = "") -> dict:
    """全量 Project 及覆盖证据；某页失败时保留已获取范围，不能伪装成空目录。"""
    client = _client(ak, sk, endpoint)
    names: list[str] = []
    offset, size, pages = 0, 100, 0
    try:
        while True:
            resp = client.list_project(offset, size)
            batch = resp.get_projects() or []
            names.extend(_project_name(p) for p in batch if _project_name(p))
            pages += 1
            if len(batch) < size:
                return {"projects": sorted(names), "complete": True,
                        "error": "", "pages": pages, "next_offset": None}
            offset += size
    except Exception as e:
        return {"projects": sorted(names), "complete": False,
                "error": str(e)[:300], "pages": pages, "next_offset": offset}


def list_logstores(ak: str, sk: str, project: str, endpoint: str = "") -> list[str]:
    client = _client(ak, sk, endpoint)
    from aliyun.log import ListLogstoresRequest

    resp = client.list_logstores(ListLogstoresRequest(project))
    return sorted(resp.get_logstores() or [])


_CANONICAL_APP_FIELDS = {"LogLevel": "level_field", "AppName": "app_field", "Content": "message_field"}
_SAFE_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")


def _application_mapping(project: str, logstore: str) -> dict | None:
    """只对新向导写入的第三段映射生效；旧两段配置保持历史字段行为。"""
    try:
        configured = json.loads(settings.SLS_APP_LOG or "[]")
    except (TypeError, ValueError):
        return None
    if (not isinstance(configured, list) or len(configured) < 3
            or configured[:2] != [project, logstore]):
        return None
    confirmed = configured[2] if isinstance(configured[2], dict) else {}
    mapping = {}
    for canonical, key in _CANONICAL_APP_FIELDS.items():
        value = confirmed.get(key) or ""
        if value and (not isinstance(value, str) or not _SAFE_FIELD.fullmatch(value)):
            value = ""
        mapping[canonical] = value
    return mapping


def _transform_unquoted_fields(text: str, transform) -> str:
    """只处理引号外 token，不改用户检索值或 SQL 字符串。"""
    out = []
    quote = ""
    token = []
    index = 0

    def flush():
        if token:
            out.append(transform("".join(token)))
            token.clear()

    while index < len(text):
        char = text[index]
        if quote:
            out.append(char)
            if char == quote:
                # SQL 中单引号/检索中双引号用两个相同字符转义。
                if index + 1 < len(text) and text[index + 1] == quote:
                    out.append(text[index + 1])
                    index += 1
                else:
                    quote = ""
        elif char in ("'", '"'):
            flush()
            quote = char
            out.append(char)
        elif char.isalnum() or char in "_.-":
            token.append(char)
        else:
            flush()
            out.append(char)
        index += 1
    flush()
    return "".join(out)


def _replace_fields(text: str, mapping: dict, *, select_aliases: bool = False) -> str:
    def replace(value: str) -> str:
        replacement = mapping.get(value, value)
        if select_aliases and value in mapping and mapping[value]:
            replacement += f" as {value}"
        return replacement

    return _transform_unquoted_fields(text, replace)


def _referenced_app_fields(query: str) -> set[str]:
    referenced = set()

    def collect(value: str) -> str:
        if value in _CANONICAL_APP_FIELDS:
            referenced.add(value)
        return value

    _transform_unquoted_fields(query, collect)
    return referenced


def _rewrite_application_query(project: str, logstore: str, query: str) -> tuple[str, dict | None]:
    mapping = _application_mapping(project, logstore)
    if mapping is None:
        return query, None
    referenced = _referenced_app_fields(query)
    missing = sorted(field for field in referenced if not mapping[field])
    if missing:
        raise SLSQueryError(
            f"应用日志未确认 {', '.join(missing)} 对应字段，已停止级别/应用语义分析；原始检索仍可使用")
    if not referenced:
        return query, mapping
    if "|" not in query:
        return _replace_fields(query, mapping), mapping
    search, sql = query.split("|", 1)
    search = _replace_fields(search, mapping)
    # SLS 消费方读取 LogLevel/AppName 别名，SQL 输出形状保持不变。
    select = re.search(r"\bselect\b(.*?)(?=\bfrom\b|\bgroup\s+by\b|\border\s+by\b|\blimit\b|$)",
                       sql, flags=re.IGNORECASE | re.DOTALL)
    if select:
        body = _replace_fields(select.group(1), mapping, select_aliases=True)
        # group/order by 也使用真实字段；消费方仍读 select 输出的旧别名。
        sql = (sql[:select.start(1)] + body
               + _replace_fields(sql[select.end(1):], mapping))
    else:
        sql = _replace_fields(sql, mapping)
    return search + "|" + sql, mapping


def query_logs(
    ak: str,
    sk: str,
    project: str,
    logstore: str,
    query: str,
    start: int,
    end: int,
    limit: int = 20,
    offset: int = 0,
    reverse: bool = True,
    endpoint: str = "",
    apply_field_mapping: bool = True,
) -> dict:
    """执行一次检索/分析。

    query 含 ``|`` 视为 SQL 分析（execute_logstore_sql，行数由语句自身 limit 决定，
    不支持 offset 翻页），否则普通检索（get_logs，line/offset 翻页）。
    返回 {logs, is_sql, count, has_more}；has_more 为启发式（本页满页即认为可能有下一页）。

    统一守卫：目标未配置（产品默认态）抛 SLSQueryError，消费方 try/except 接住后
    走"未配置"降级——一处守卫，全站日志类能力说人话不崩。
    """
    if not project or not logstore:
        raise SLSQueryError(
            "SLS 目标未配置（请走安装向导，或配置 SLS_SLB_TARGETS / "
            "SLS_APP_LOG / SLS_CDN_TARGET）")
    client = _client(ak, sk, endpoint)
    query = (query or "").strip() or "*"
    query, field_mapping = (_rewrite_application_query(project, logstore, query)
                            if apply_field_mapping else (query, None))
    is_sql = "|" in query
    try:
        if is_sql:
            # SDK 的 execute_logstore_sql 是独立签名，不接收 GetLogsRequest
            resp = client.execute_logstore_sql(project, logstore, int(start), int(end), query, False)
        else:
            from aliyun.log import GetLogsRequest

            req = GetLogsRequest(
                project, logstore, int(start), int(end), topic="",
                query=query, reverse=reverse, line=limit, offset=offset,
            )
            resp = client.get_logs(req)
    except Exception as e:  # 统一转成业务异常，错误信息透出给前端
        raise SLSQueryError(str(e)[:300]) from e

    logs: list[dict] = []
    for item in resp.get_logs() or []:
        try:
            contents = dict(item.get_contents() or {})
        except Exception:
            continue
        try:
            ts = item.get_time()
            # 仅检索模式补 __time__（SQL 结果的 time 是分析时刻，无意义，不加）
            if ts and not is_sql:
                contents.setdefault("__time__", ts)
        except Exception:
            pass
        if field_mapping:
            for canonical, actual in field_mapping.items():
                if actual and actual in contents:
                    contents.setdefault(canonical, contents[actual])
        logs.append(contents)

    count = len(logs)
    try:
        raw_count = resp.get_count()
        if isinstance(raw_count, int):
            count = raw_count
    except Exception:
        pass
    return {
        "logs": logs,
        "is_sql": is_sql,
        "count": count,
        # SQL 模式行数由语句自身 limit 决定，没有翻页概念
        "has_more": (not is_sql) and count >= limit,
    }
