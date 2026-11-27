"""数据源注册表与探测。

分层接入（接入成本 O(源类型) 不是 O(客户数)）：
  指标：Prometheus / VictoriaMetrics（API 兼容，同一查询客户端）/ OTel→OTLP 落 VM（部署层）
  日志：Loki（查询适配）/ SLS（已有）；SkyWalking/Zipkin 走告警/事件集成（不进全量 trace）
  云：  CloudAdapter 接口 + packs/<cloud>/ 知识包（按订单实现）
K8s 监控走 Prometheus 路径（kube-state-metrics/cAdvisor → Prometheus → 注册进来）。

探测语义：**探活必须取到版本/系统信息才算通（不是测端口）**。
每类源一个真实探测器（版本号/集群名/服务目录），detail 直接给"版本 x.y.z"式信息，
前端「测试连通」显示"连通 · 版本 x.y.z"。只有连通性、取不到系统信息 = 如实报不通/未暴露。
探活与查询适配器状态解耦（矩阵见 docs/active/data-sources.md）：探活全类型真做；
查询适配器 prometheus/vm=全量、zabbix=最新监控项值（item.get/history.get）、
日志侧 sls/loki/cls/es=统一查询层（services/log_query.py）、
其余排期——不假实现。

安全：探测全部只读 GET / 只读 JSON-RPC；TLS 校验默认关（内网自签证书常态，凭证仅
发往用户自己配置的地址），数据源可配 verify_ssl 开启校验。
"""
from __future__ import annotations

import json
import logging
import re
import time

from app.core.config import settings

logger = logging.getLogger("qianshu.datasource")

_VER_RE = re.compile(r"\bv?(\d+\.\d+(?:\.\d+){0,2})\b")


def metric_sources() -> list:
    try:
        rows = json.loads(settings.METRIC_SOURCES or "[]")
        return [r for r in rows if r.get("url") or r.get("type") == "vm"]
    except Exception:
        return [{"name": "builtin-vm", "type": "vm", "url": ""}]


def log_sources() -> list:
    try:
        return json.loads(settings.LOG_SOURCES or "[]")
    except Exception:
        return []


# 类型中文名（探测报告/前端展示共用）
SOURCE_TYPE_LABELS = {
    "prometheus": "Prometheus", "vm": "VictoriaMetrics", "zabbix": "Zabbix",
    "influxdb": "InfluxDB", "graphite": "Graphite", "es": "Elasticsearch",
    "opentsdb": "OpenTSDB", "http": "自定义 HTTP API",
    "loki": "Loki", "sls": "SLS", "cls": "腾讯云 CLS",
    "graylog": "Graylog", "filebeat": "Filebeat",
    "skywalking": "SkyWalking", "zipkin": "Zipkin",
}

# 已实现查询适配器的类型（探活不受此限——探活全类型真做）。
# zabbix=最新监控项值查询（services/zabbix.py）；
# 日志侧 sls/loki/cls/es 全部进统一查询层 services/log_query.py：
# sls=collector/sls、loki=collector/loki、cls=collector/cls
# SearchLog，es=collector/es_query DSL/Lucene；其余类型尚无查询适配器。
METRIC_QUERY_TYPES = ("prometheus", "vm", "zabbix")
LOG_QUERY_TYPES = ("sls", "loki", "cls", "es")


# ---------------- 探测结果封装 ----------------

def _result(src: dict, t0: float, ok: bool | None, detail: str, **extra) -> dict:
    out = {"name": src.get("name"), "type": src.get("type"),
           "url": src.get("url") or "",
           "ok": ok, "latency_ms": round((time.time() - t0) * 1000, 1),
           "detail": detail}
    out.update(extra)
    return out


def _pending_probe(src: dict, kind: str) -> dict:
    """无探测器的类型：如实回报"查询待启用"，不做假连通。"""
    typ = src.get("type") or "?"
    label = SOURCE_TYPE_LABELS.get(typ, typ)
    return {"name": src.get("name"), "type": typ, "url": src.get("url") or "",
            "ok": None, "pending": True,
            # 查询能力未实现时如实说明，不能声称已支持
            "detail": f"{label}{kind}：本期支持登记与连通测试；查询能力随版本更新自动启用"}


def _get(url: str, headers: dict | None = None, timeout: int = 8, verify: bool = False):
    import httpx

    return httpx.get(url, timeout=timeout, headers=headers or {},
                     verify=verify, follow_redirects=True)


# ---------------- 各类型真实探测器（取版本/系统信息才算通） ----------------

def _vm_self_version(url: str, headers: dict, verify: bool = False) -> str:
    """VictoriaMetrics 自指标 vm_app_version（流式抓 /metrics，命中即停）。

    VM 的 /api/v1/status/buildinfo 只回 Prom 兼容版本号（如 2.24.0），不是 VM 真版本；
    vm_app_version 不进查询 API（自指标不入库），只能从 /metrics 拿。
    """
    import httpx

    try:
        with httpx.stream("GET", f"{url}/metrics", timeout=8, headers=headers,
                          verify=verify) as r:
            if r.status_code != 200:
                return ""
            seen = 0
            for line in r.iter_lines():
                seen += len(line) + 1
                if line.startswith("vm_app_version"):
                    m = re.search(r'short_version="([^"]+)"', line) \
                        or re.search(r'version="([^"]+)"', line)
                    return m.group(1) if m else ""
                if seen > 512 * 1024:   # 自指标段落之外还没命中就放弃
                    return ""
    except Exception:
        return ""


def _probe_promvm(src: dict) -> dict:
    """Prometheus / VictoriaMetrics：取版本号才算通。

    链：① /api/v1/status/buildinfo 完整字段=真 Prometheus（goVersion/revision）
        ② /metrics 的 vm_app_version=VM 真版本  ③ /vmui 可达=VM 产品标识
        ④ buildinfo 极简回退（Prom 兼容层）
    云托管 Prometheus 兼容端点（腾讯云可观测/TEM、AWS AMP、火山 TLS 等）走同一条链——
    腾讯域名（*.prometheus.tencentcloud.com）识别后如实标注形态。
    """
    url = (src.get("url") or settings.VM_URL).rstrip("/")
    headers = src.get("headers") or {}
    t0 = time.time()
    verify = bool(src.get("verify_ssl"))   # 内网默认不校验证书，可显式开启
    tencent = "prometheus.tencentcloud.com" in url
    minimal_ver = ""

    def _tag(detail: str) -> str:
        return detail + ("（腾讯云可观测 Prometheus 兼容端点）" if tencent else "")

    try:
        r = _get(f"{url}/api/v1/status/buildinfo", headers, verify=verify)
        if r.status_code == 200:
            d = (r.json().get("data") or {})
            if d.get("goVersion") or d.get("revision"):
                return _result(src, t0, True,
                               _tag(f"版本 {d.get('version', '?')}（Prometheus）"), url=url)
            minimal_ver = str(d.get("version") or "")
    except Exception as e:
        logger.debug("buildinfo 探测 %s: %s", url, e)
    ver = _vm_self_version(url, headers, verify=verify)
    if ver:
        return _result(src, t0, True, _tag(f"版本 {ver}（VictoriaMetrics）"), url=url)
    try:
        r = _get(f"{url}/vmui/", headers, verify=verify)
        if r.status_code == 200 and "vmui" in r.text.lower():
            return _result(src, t0, True,
                           _tag("VictoriaMetrics vmui 可达（版本接口未暴露）"), url=url)
    except Exception as e:
        logger.debug("vmui 探测 %s: %s", url, e)
    if minimal_ver:
        return _result(src, t0, True,
                       _tag(f"版本 {minimal_ver}（Prometheus API 兼容）"), url=url)
    try:
        r = _get(f"{url}/-/healthy", headers, verify=verify)
        if r.status_code == 200:
            return _result(src, t0, True, _tag("健康检查通过（版本接口未暴露）"), url=url)
    except Exception:
        pass
    return _result(src, t0, False,
                   "未取到版本信息（buildinfo/vm_app_version/vmui 均无有效响应）", url=url)


def _probe_zabbix(src: dict) -> dict:
    """Zabbix：JSON-RPC apiinfo.version 免认证取版本（真探活）。

    配了 API Token/账号时顺带验证查询通道（item.get+history.get 最新值）。
    """
    from app.services.zabbix import ZabbixError, api_version, probe_query_channel

    t0 = time.time()
    verify = bool(src.get("verify_ssl"))   # 内网默认不校验证书，可显式开启
    try:
        ver = api_version(src.get("url") or "", verify=verify)
    except ZabbixError as e:
        return _result(src, t0, False, f"apiinfo.version 失败: {str(e)[:120]}")
    if not ver:
        return _result(src, t0, False, "apiinfo.version 返回空（未取到版本）")
    detail = f"版本 {ver}"
    has_cred = bool((src.get("token") or "").strip()
                    or (src.get("headers") or {}).get("Authorization")
                    or ((src.get("user") or "").strip() and (src.get("password") or "")))
    if has_cred:
        detail += f" · {probe_query_channel(src)}"
    else:
        detail += " · 查询适配器已实现（item/history 最新值查询）——配置 API Token 或账号后自动验证"
    return _result(src, t0, True, detail)


def _probe_influx(src: dict) -> dict:
    """InfluxDB：/api/v2/ping（v2）/ /ping（v1）取 X-Influxdb-Version 头。"""
    url = (src.get("url") or "").rstrip("/")
    headers = src.get("headers") or {}
    t0 = time.time()
    verify = bool(src.get("verify_ssl"))   # 内网默认不校验证书，可显式开启
    last = "无响应"
    for path in ("/api/v2/ping", "/ping"):
        try:
            r = _get(f"{url}{path}", headers, verify=verify)
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:80]}"
            continue
        if r.status_code in (200, 204):
            ver = (r.headers.get("X-Influxdb-Version")
                   or r.headers.get("x-influxdb-version") or "")
            build = (r.headers.get("X-Influxdb-Build")
                     or r.headers.get("x-influxdb-build") or "")
            if ver:
                return _result(src, t0, True,
                               f"版本 {ver}" + (f" {build}" if build else ""), url=url)
            last = f"{path} 通（版本头未暴露）"
        else:
            last = f"{path} HTTP {r.status_code}"
    try:
        r = _get(f"{url}/health", headers, verify=verify)
        if r.status_code == 200:
            d = r.json()
            if not isinstance(d, dict):
                return _result(src, t0, None, "InfluxDB /health 可达，但响应无法识别", url=url)
            ver = str(d.get("version") or "")
            name = str(d.get("name") or "")
            if "influxdb" in name.lower():
                return _result(src, t0, True,
                               f"InfluxDB /health 通（{name}，版本 {ver or '未暴露'}）", url=url)
            return _result(src, t0, None,
                           "InfluxDB /health 可达，但响应缺少 InfluxDB 产品标识；无法验证数据源",
                           url=url)
    except Exception:
        pass
    return _result(src, t0, False, f"未取到版本信息（{last}）", url=url)


def _probe_es(src: dict) -> dict:
    """Elasticsearch：GET / 取 version.number + cluster_name。"""
    url = (src.get("url") or "").rstrip("/")
    t0 = time.time()
    verify = bool(src.get("verify_ssl"))   # 内网默认不校验证书，可显式开启
    try:
        r = _get(f"{url}/", src.get("headers"), verify=verify)
        if r.status_code != 200:
            return _result(src, t0, False, f"HTTP {r.status_code}", url=url)
        d = r.json()
        ver = (d.get("version") or {}).get("number", "")
        cluster, name = d.get("cluster_name", ""), d.get("name", "")
        if ver or cluster:
            detail = f"版本 {ver or '未知'}"
            if cluster:
                detail += f" · 集群 {cluster}"
            return _result(src, t0, True, detail, url=url)
        return _result(src, t0, False, "响应无版本/集群信息", url=url)
    except Exception as e:
        return _result(src, t0, False,
                       f"{type(e).__name__}: {str(e)[:80]}", url=url)


def _probe_opentsdb(src: dict) -> dict:
    """OpenTSDB：/api/version 取版本。"""
    url = (src.get("url") or "").rstrip("/")
    headers = src.get("headers") or {}
    t0 = time.time()
    verify = bool(src.get("verify_ssl"))   # 内网默认不校验证书，可显式开启
    last = "/api/version 无有效响应"
    for path in ("/api/version", "/api/config"):
        try:
            r = _get(f"{url}{path}", headers, verify=verify)
            if r.status_code == 200:
                d = r.json()
                ver = d.get("version") or (d.get("opentsdb") or {}).get("version") or ""
                if ver:
                    return _result(src, t0, True, f"版本 {ver}", url=url)
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:80]}"
            continue
    return _result(src, t0, False, f"未取到版本信息（{last}）", url=url)


def _probe_graphite(src: dict) -> dict:
    """Graphite：/metrics/version 取版本；取不到退 /metrics/find 验查询 API 可达。"""
    url = (src.get("url") or "").rstrip("/")
    headers = src.get("headers") or {}
    t0 = time.time()
    verify = bool(src.get("verify_ssl"))   # 内网默认不校验证书，可显式开启
    last = "无响应"
    for path in ("/metrics/version", "/version"):
        try:
            r = _get(f"{url}{path}", headers, verify=verify)
            if r.status_code == 200 and r.text.strip():
                m = _VER_RE.search(r.text)
                if m:
                    return _result(src, t0, True,
                                   f"版本 {m.group(1)}（{path}）", url=url)
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:80]}"
            continue
    try:
        r = _get(f"{url}/metrics/find?query=*&format=completer", headers, verify=verify)
        if r.status_code == 200:
            return _result(src, t0, True,
                           "Graphite 查询 API 可达（版本端点未暴露）", url=url)
    except Exception as e:
        last = f"{type(e).__name__}: {str(e)[:80]}"
    return _result(src, t0, False, f"未取到版本信息（{last}）", url=url)


def _probe_loki(src: dict) -> dict:
    """Loki：/loki/api/v1/status/buildinfo 取版本；老版本退 /ready。"""
    url = (src.get("url") or "").rstrip("/")
    headers = src.get("headers") or {}
    t0 = time.time()
    verify = bool(src.get("verify_ssl"))   # 内网默认不校验证书，可显式开启
    try:
        r = _get(f"{url}/loki/api/v1/status/buildinfo", headers, verify=verify)
        if r.status_code == 200:
            d = r.json()
            ver = d.get("version") or ""
            if ver:
                return _result(src, t0, True, f"版本 {ver}", url=url)
    except Exception as e:
        logger.debug("loki buildinfo %s: %s", url, e)
    try:
        r = _get(f"{url}/ready", headers, verify=verify)
        if r.status_code == 200:
            return _result(src, t0, True,
                           "Loki ready（版本接口未暴露）", url=url)
        return _result(src, t0, False, f"/ready HTTP {r.status_code}", url=url)
    except Exception as e:
        return _result(src, t0, False,
                       f"{type(e).__name__}: {str(e)[:80]}", url=url)


def _probe_graylog(src: dict) -> dict:
    """Graylog：GET /api 取版本字段（Graylog 签名=cluster_id/node_id/tagline）。

    Graylog 401/403 只能证明 HTTP 可达，标为未验证，不能冒充已连通。
    """
    url = (src.get("url") or "").rstrip("/")
    headers = src.get("headers") or {}
    t0 = time.time()
    verify = bool(src.get("verify_ssl"))   # 内网默认不校验证书，可显式开启
    last = "/api 无有效响应"
    for path in ("/api", "/api/", "/"):
        try:
            r = _get(f"{url}{path}", headers, verify=verify)
            if r.status_code == 200:
                try:
                    d = r.json()
                except Exception:
                    continue
                ver = d.get("version") or d.get("Version") or ""
                if ver:
                    return _result(src, t0, True, f"版本 {ver}", url=url)
                if d.get("cluster_id") or d.get("node_id") or d.get("tagline") is not None:
                    return _result(src, t0, True,
                                   "Graylog API 可达（版本需登录后可见）", url=url)
            if r.status_code in (401, 403):
                return _result(src, t0, None,
                               "Graylog API 可达，但鉴权未通过；无法验证数据源", url=url)
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:80]}"
            continue
    return _result(src, t0, False, f"未取到版本信息（{last}）", url=url)


def _probe_filebeat(src: dict) -> dict:
    """Filebeat：beats HTTP 监控端点找版本字段（/ 根信息端点带 version，先找版本再认可达）。"""
    url = (src.get("url") or "").rstrip("/")
    headers = src.get("headers") or {}
    t0 = time.time()
    verify = bool(src.get("verify_ssl"))   # 内网默认不校验证书，可显式开启
    last = "监控端点无响应"
    reachable = ""
    for path in ("/", "/state", "/stats"):
        try:
            r = _get(f"{url}{path}", headers, verify=verify)
            if r.status_code != 200:
                continue
            try:
                d = r.json()
            except Exception:
                continue
            # Filebeat 根信息带 beat=filebeat；host.os.version 等嵌套版本不是产品证据。
            ver = str(d.get("version") or d.get("beat_version") or "")
            identity = str(d.get("beat") or d.get("name") or "")
            if "filebeat" in identity.lower():
                return _result(src, t0, True,
                               f"Filebeat 监控端点可达（版本 {ver or '未暴露'}）", url=url)
            if isinstance(d, dict) and not reachable:
                reachable = f"Filebeat 监控地址 {path} 返回 JSON，但缺少 Filebeat 产品标识"
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:80]}"
            continue
    if reachable:
        return _result(src, t0, None, reachable + "；无法验证数据源", url=url)
    return _result(src, t0, False, f"未取到版本信息（{last}）", url=url)


def _probe_http_custom(src: dict) -> dict:
    """自定义 HTTP API：GET 配置地址，响应体 JSON 里嗅探版本字段。"""
    url = (src.get("url") or "").rstrip("/")
    t0 = time.time()
    verify = bool(src.get("verify_ssl"))   # 内网默认不校验证书，可显式开启
    try:
        r = _get(url, src.get("headers"), verify=verify)
    except Exception as e:
        return _result(src, t0, False,
                       f"{type(e).__name__}: {str(e)[:80]}", url=url)
    if not 200 <= r.status_code < 300:
        return _result(src, t0, False, f"HTTP {r.status_code}（请求未成功）", url=url)
    ver = ""
    try:
        d = r.json()
        if isinstance(d, dict):
            ver = str(d.get("version") or d.get("ver") or d.get("build") or "")
            if not ver:
                for v in d.values():
                    if isinstance(v, dict) and v.get("version"):
                        ver = str(v["version"])
                        break
    except Exception:
        m = _VER_RE.search(r.text[:2000])
        ver = m.group(1) if m else ""
    if not ver:
        return _result(src, t0, None,
                       f"HTTP {r.status_code} 可达，但未识别版本/系统信息；无法验证数据源", url=url)
    return _result(src, t0, True, f"HTTP {r.status_code} · 版本 {ver}", url=url)


def _probe_sls(src: dict, db=None) -> dict:
    """SLS：走阿里云凭证 ListProject（与凭证连通性测试同口径，真数据）。"""
    t0 = time.time()
    if db is None:
        return _result(src, t0, None, "SLS 探测需凭证上下文（本次调用未带 db）")
    try:
        from app.collector.credential import resolve_sls_credential

        ak, sk, source = resolve_sls_credential(db)
        if not ak:
            return _result(src, t0, False,
                           "未配置阿里云凭证（SLS 查询需 AK/SK）")
        import logging as _logging

        level = _logging.getLogger().getEffectiveLevel()
        _logging.getLogger().setLevel(_logging.CRITICAL)   # 探测期间静默 SLS SDK 失败刷屏，finally 恢复原级别
        try:
            from aliyun.log import LogClient
            from app.collector.sls import normalize_endpoint

            client = LogClient(normalize_endpoint(src.get("url") or ""), ak, sk)
            count = len(client.list_project().projects or [])
        finally:
            _logging.getLogger().setLevel(level)
        return _result(src, t0, True,
                       f"SLS 可达 · 可见 project {count} 个（凭证 {source}）")
    except Exception as e:
        return _result(src, t0, False,
                       f"{type(e).__name__}: {str(e)[:100]}")


def _probe_cls(src: dict, db=None) -> dict:
    """腾讯云 CLS：走腾讯云凭证 DescribeTopics（只读探活，与 SLS ListProject 同口径）。

    凭证不在本表单配置（与 SLS 同款）：统一走云账号管理的 tencent 只读 AK。
    """
    t0 = time.time()
    if db is None:
        return _result(src, t0, None, "CLS 探测需凭证上下文（本次调用未带 db）")
    try:
        from app.collector.cls import probe as cls_probe
        from app.collector.credential import resolve_tencent_credential

        ak, sk, source = resolve_tencent_credential(db)
        if not ak:
            return _result(src, t0, False,
                           "未配置腾讯云凭证（CLS 查询需 SecretId/SecretKey）")
        r = cls_probe(ak, sk, src.get("url") or "")
        if not r.get("ok"):
            return _result(src, t0, False, f"CLS 探测失败: {r.get('detail')}")
        return _result(src, t0, True, f"{r.get('detail')}（凭证 {source}）")
    except Exception as e:
        return _result(src, t0, False,
                       f"{type(e).__name__}: {str(e)[:100]}")


# ---------------- 对外入口 ----------------

_METRIC_PROBES = {
    "prometheus": _probe_promvm,
    "vm": _probe_promvm,
    "zabbix": _probe_zabbix,
    "influxdb": _probe_influx,
    "es": _probe_es,
    "opentsdb": _probe_opentsdb,
    "graphite": _probe_graphite,
    "http": _probe_http_custom,
}

_LOG_PROBES = {
    "loki": _probe_loki,
    "es": _probe_es,
    "graylog": _probe_graylog,
    "filebeat": _probe_filebeat,
    "http": _probe_http_custom,
}


def probe_metric_source(src: dict, db=None) -> dict:
    """指标源探测：按类型走真实探测器（取版本/系统信息才算通）。

    src 支持 headers（{"Authorization": "Bearer xxx"}）——腾讯云可观测/火山 TLS/AWS AMP
    等云托管 Prometheus 兼容入口的鉴权走这里。未取到版本/系统信息时按不通/未暴露如实报，
    不能把端口可达当作探测成功。
    """
    probe = _METRIC_PROBES.get(src.get("type") or "")
    if probe is None:
        return _pending_probe(src, "指标")
    try:
        return probe(src)
    except Exception as e:   # 单源探测失败不阻断体检
        return _result(src, time.time(), False,
                       f"探测异常 {type(e).__name__}: {str(e)[:80]}")


def probe_log_source(src: dict, db=None) -> dict:
    """日志源探测：Loki/SLS/CLS/ES/Graylog/Filebeat/自定义 各取版本；APM 走服务目录探测。"""
    typ = src.get("type") or ""
    if typ in ("skywalking", "zipkin"):
        return probe_apm_source(src)
    if typ == "sls":
        return _probe_sls(src, db)
    if typ == "cls":
        return _probe_cls(src, db)
    probe = _LOG_PROBES.get(typ)
    if probe is None:
        return _pending_probe(src, "日志")
    try:
        return probe(src)
    except Exception as e:
        return _result(src, time.time(), False,
                       f"探测异常 {type(e).__name__}: {str(e)[:80]}")


def apm_sources() -> list:
    """APM 源（skywalking/zipkin）：LOG_SOURCES 里 type 匹配。"""
    return [s for s in log_sources() if s.get("type") in ("skywalking", "zipkin")]


def probe_all(db=None) -> dict:
    """全数据源体检（底座体检的接入面扩展，含 APM 服务目录）。"""
    metrics = [probe_metric_source(s, db) for s in metric_sources()]
    logs = [probe_log_source(s, db) for s in log_sources()]
    apm = [probe_apm_source(s) for s in apm_sources()]
    return {
        "metrics": metrics,
        "logs": logs,
        "apm": apm,
        "summary": {
            "metric_ok": sum(1 for m in metrics if m["ok"]),
            "metric_total": len(metrics),
            "log_ok": sum(1 for l in logs if l["ok"]),
            "log_total": len(logs),
            "apm_ok": sum(1 for a in apm if a["ok"]),
            "apm_total": len(apm),
        },
    }


def probe_apm_source(src: dict) -> dict:
    """APM 源探测+服务目录（skywalking/zipkin）：服务清单进资产（product=apm_service）。

    产品边界不变：全量 trace 不存；**服务目录+告警=数据接入**（服务拓扑/延迟指标来自
    APM 自身，千枢收编服务清单进资产与拓扑）。
    """
    import httpx

    url = (src.get("url") or "").rstrip("/")
    t0 = time.time()
    verify = bool(src.get("verify_ssl"))   # 内网默认不校验证书，可显式开启
    try:
        if src.get("type") == "skywalking":
            # SkyWalking GraphQL：服务列表
            q = {"query": "query{getAllServices(duration:{start:\"%s\",end:\"%s\",step:MINUTE}){name}}" % (
                __import__("datetime").datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
                __import__("datetime").datetime.utcnow().strftime("%Y-%m-%d %H:%M"))}
            r = httpx.post(f"{url}/graphql", json=q, timeout=10,
                           headers=src.get("headers") or {}, verify=verify)
            names = ((r.json().get("data") or {}).get("getAllServices") or [])
        else:
            # Zipkin: /api/v2/services
            r = httpx.get(f"{url}/api/v2/services", timeout=10,
                          headers=src.get("headers") or {}, verify=verify)
            names = r.json() if r.status_code == 200 else []
        latency = round((time.time() - t0) * 1000, 1)
        ok = r.status_code == 200
        return {"name": src.get("name"), "type": src.get("type"), "url": url,
                "ok": ok, "latency_ms": latency,
                "detail": f"{len(names)} 个服务" if ok else f"HTTP {r.status_code}",
                "services": list(names)[:50]}
    except Exception as e:
        return {"name": src.get("name"), "type": src.get("type"), "url": url,
                "ok": False, "latency_ms": round((time.time() - t0) * 1000, 1),
                "detail": f"{type(e).__name__}: {str(e)[:80]}", "services": []}


def sync_apm_services(db, src: dict) -> int:
    """APM 服务清单 → assets（product=apm_service），进拓扑 middleware 层。"""
    from app.collector.base import upsert_asset

    info = probe_apm_source(src)
    seen = set()
    n = 0
    for name in info.get("services") or []:
        iid = f"{src.get('type', 'apm')}:{name}"
        seen.add(iid)
        upsert_asset(db, "apm_service", iid, set(), {
            "name": name, "region": src.get("type", ""),
            "status": "Running",
            "extra": __import__("json").dumps({"apm": src.get("name"), "url": src.get("url")}),
        })
        n += 1
    db.commit()
    return n
