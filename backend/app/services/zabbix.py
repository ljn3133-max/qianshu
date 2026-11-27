"""Zabbix JSON-RPC 适配器。

产品口径（对齐 services/datasource.py 探测语义）：
  探活=取到真实信息：apiinfo.version 免认证取版本号，取到版本才算通；
  查询适配器=最新监控项值查询：认证（user.login 或 API Token）+ item.get + history.get，
  返回形状对齐 ai_tools.query_metric（{total, returned, items, columns}）。

安全：全部只读接口（apiinfo.version / user.login / user.logout / item.get / history.get），
不碰配置写接口；凭证只进不出（入参传入，不落日志）。

API 兼容：Zabbix 4.0~7.x。user.login 参数名 5.4 起 user→username，两边都试；
API Token（5.2+，6.4 完善）走 Authorization: Bearer 头。
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger("qianshu.zabbix")

_JSON_HEADERS = {"Content-Type": "application/json-rpc",
                 "User-Agent": "qianshu-datasource-probe"}


class ZabbixError(RuntimeError):
    """Zabbix API 调用失败（含 JSON-RPC error 字段）。"""


def api_endpoint(url: str) -> str:
    """URL 归一：用户可填根地址或完整 api_jsonrpc.php 地址。"""
    u = (url or "").strip().rstrip("/")
    if not u:
        raise ZabbixError("Zabbix 地址为空")
    if u.endswith(".php"):
        return u
    return u + "/api_jsonrpc.php"


def rpc(url: str, method: str, params: dict | None = None,
        auth: str = "", token: str = "", timeout: int = 10, verify: bool = False):
    """单次 JSON-RPC 调用。token 走 Authorization: Bearer（API Token）；auth 为 user.login 会话。

    verify：TLS 校验开关，默认关（内网自签常态），数据源可开。"""
    import httpx

    body = {"jsonrpc": "2.0", "method": method,
            "params": params or {}, "id": 1}
    if auth:
        body["auth"] = auth
    headers = dict(_JSON_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = httpx.post(api_endpoint(url), json=body, headers=headers,
                       timeout=timeout, verify=verify)
    except Exception as e:
        raise ZabbixError(f"{type(e).__name__}: {str(e)[:120]}") from e
    if r.status_code != 200:
        raise ZabbixError(f"HTTP {r.status_code}: {r.text[:120]}")
    try:
        data = r.json()
    except Exception as e:
        raise ZabbixError(f"响应非 JSON: {r.text[:120]}") from e
    if "error" in data:
        err = data["error"]
        msg = err.get("data") or err.get("message") or str(err)
        raise ZabbixError(f"[{err.get('code', '?')}] {str(msg)[:160]}")
    return data.get("result")


def api_version(url: str, verify: bool = False) -> str:
    """apiinfo.version（免认证）——探活核心：取到版本才算通。"""
    result = rpc(url, "apiinfo.version", verify=verify)
    return str(result or "").strip()


def login(url: str, user: str, password: str, verify: bool = False) -> str:
    """user.login 取会话 token（5.4+ 参数 username，老版本 user——先新后旧）。"""
    try:
        return str(rpc(url, "user.login",
                       {"username": user, "password": password}, verify=verify) or "")
    except ZabbixError as e:
        if "unexpected parameter" not in str(e).lower() and "unexpected" not in str(e).lower():
            raise
        return str(rpc(url, "user.login",
                       {"user": user, "password": password}, verify=verify) or "")


def logout(url: str, auth: str, verify: bool = False) -> None:
    try:
        rpc(url, "user.logout", [], auth=auth, verify=verify)
    except Exception:  # 会话清理失败不影响结果
        pass


def _auth_kwargs(src: dict) -> dict:
    """从数据源配置取认证：优先 API Token（headers.Bearer 或 token 字段），其次 user/password。"""
    token = str(src.get("token") or "").strip()
    headers = src.get("headers") or {}
    if not token:
        authz = str(headers.get("Authorization") or headers.get("authorization") or "")
        if authz.lower().startswith("bearer "):
            token = authz[7:].strip()
    return {"token": token,
            "user": str(src.get("user") or "").strip(),
            "password": str(src.get("password") or "")}


def query_latest_items(src: dict, keyword: str = "", limit: int = 20) -> dict:
    """最新监控项值查询（查询适配器）：item.get + history.get 各取最新一点。

    src: 数据源配置 {url, token|user+password, headers}。
    返回 {total, returned, items, columns, auth_mode}；item 形如
    {host, item, key, value, units, clock}——与 query_metric 同形状（表格可直渲染）。
    """
    url = src.get("url") or ""
    cred = _auth_kwargs(src)
    limit = max(1, min(int(limit or 20), 50))
    verify = bool(src.get("verify_ssl"))   # 内网默认不校验证书，可显式开启
    auth = ""
    auth_mode = "none"
    try:
        if cred["token"]:
            auth_mode = "api_token"
            ak = {"verify": verify}
        elif cred["user"] and cred["password"]:
            auth_mode = "user.login"
            auth = login(url, cred["user"], cred["password"], verify=verify)
            ak = {"auth": auth, "verify": verify}
        else:
            raise ZabbixError("未配置认证（API Token 或 用户名/密码）——查询适配器需凭证，探活不需要")

        params = {
            "output": ["itemid", "name", "key_", "units", "value_type"],
            "sortfield": "name",
            "limit": limit,
        }
        if keyword:
            params["search"] = {"name": keyword, "key_": keyword}
            params["search_by_any"] = True
        # selectHosts 兼容：7.0 可能拒参数，降级重试
        try:
            items = rpc(url, "item.get", {**params, "selectHosts": ["host"]}, **ak) or []
        except ZabbixError as e:
            if "selecthosts" not in str(e).lower():
                raise
            items = rpc(url, "item.get", params, **ak) or []

        out = []
        for it in items:
            row = {
                "host": (it.get("hosts") or [{}])[0].get("host", ""),
                "item": it.get("name", ""),
                "key": it.get("key_", ""),
                "units": it.get("units", ""),
                "value": "",
                "clock": "",
            }
            try:
                hist = rpc(url, "history.get", {
                    "output": ["clock", "value"],
                    "itemids": [it.get("itemid")],
                    "sortfield": "clock", "sortorder": "DESC",
                    "limit": 1,
                    "history": it.get("value_type", 0),
                }, **ak) or []
                if hist:
                    row["value"] = hist[0].get("value", "")
                    row["clock"] = hist[0].get("clock", "")
            except ZabbixError as e:  # 单条历史失败不阻断整体
                row["value"] = f"(取值失败: {str(e)[:60]})"
            out.append(row)
        return {"total": len(out), "returned": len(out), "items": out,
                "columns": ["host", "item", "key", "value", "units", "clock"],
                "auth_mode": auth_mode}
    finally:
        if auth:
            logout(url, auth, verify=verify)


def probe_query_channel(src: dict) -> str:
    """探活附带的查询通道验证：拿 1 条监控项最新值，成功返回人话摘要，失败返回原因。"""
    try:
        r = query_latest_items(src, limit=1)
        n = r.get("total", 0)
        if n:
            it = r["items"][0]
            return (f"查询通道正常（{r['auth_mode']}，监控项样例："
                    f"{it['host']}/{it['item']}={it['value']}{it.get('units') or ''}）")
        return f"查询通道正常（{r['auth_mode']}，实例暂无监控项）"
    except Exception as e:
        return f"查询通道未通过：{str(e)[:100]}"
