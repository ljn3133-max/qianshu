"""链路拓扑草稿生成：SLB 日志聚合出
「域名 -> 转发规则/路径 -> 后端」规则边，带证据量 + 健康指标 + 云转发规则标注。

真实拓扑是"域名下按 URL/路径/转发规则分流到不同后端"——旧粒度（host × upstream）
把定制路由转发糊成一堆无标签边。2026-11-04 起边粒度带路径维度：
- 边键 = (chain, host, path_pattern, upstream_addr)；同域名多后端按规则拆成多条规则边
- path_pattern = request_uri 首段前缀（/api、/），二级路径留作 path_summary 证据

数据来源（全部只读）：
- 外层/内层 SLB 访问日志（SLS_SLB_TARGETS）：按 http_host × upstream_addr × 路径前缀
    聚合近 24h：请求数 / 4xx / 5xx / 平均RT（count_if + avg 一次聚合）
- 二级路径证据: regexp_extract(request_uri, '^(/[^/]*/[^/]*)', 1) 聚合，边内 Top5
- 应用匹配: SLS_APP_LOG 的 AppName（总日志数 + ERROR 数）
- ECS 关联: 资产表 extra.ip_internal / ip_public 建索引，upstream 去 port 后查名；
    后端 ECS 的 24h CPU 峰值来自 VictoriaMetrics
- 云转发规则（多云 Provider 抽象，只读）：阿里 SLB DescribeRules/DescribeVServerGroups、
    腾讯 CLB DescribeListeners/DescribeTargets——给规则边贴规则名；有规则但 24h
    无流量的补成 rule_source='cloud' 的规则边（配置视角不缺角）

野流量剔除：*.example.com 泛解析落到 SLB、nginx 无 vhost 走默认 server 的 bot 探测，
按 host 统计 2xx/3xx 成功率 <0.8 整域剔除（真实域名 >90%）。

重新生成策略：删除全部 draft 后重建；confirmed/ignored 的边保留状态、刷新证据字段。
旧粒度（path_pattern=''）已确认行按 (chain,host,upstream) 就地升级/继承状态——
粒度拆分不丢用户判断。不做任何写云操作。
"""
from __future__ import annotations

import json
import re
import logging
import threading
import time
from datetime import datetime

from sqlalchemy.orm import Session

from app.collector.sls import query_logs as sls_query_logs
from app.collector.vm import query_range as vm_query_range
from app.models.asset import Asset
from app.models.link_edge import LinkEdge
from app.services.ipnorm import extra_ips, norm_ip

logger = logging.getLogger("qianshu.link_draft")

_lock = threading.Lock()
_last_status: dict = {"running": False, "last_run_at": None, "last_result": None}

# SLS 目标为 (project, logstore, chain 标签)；2026-09-24 产品化配置化，2026-10-26 起
# 解析统一收口到 services/sls_targets.py（优先 SLS_SLB_TARGETS/SLS_APP_LOG，
# 兼容回退本文件的旧键 SLS_LINK_TARGETS / SLS_LINK_APP_LOG）
def _slb_targets() -> list:
    from app.services.sls_targets import slb_targets
    return slb_targets()


def _app_log_target() -> tuple:
    from app.services.sls_targets import app_log
    return app_log()

_TOP_PATHS = 5
_EDGE_LIMIT = 500      # host × upstream × 路径前缀 规则边聚合行数上限

# 后端地址必须是 ip:port / 域名:port。502 类异常日志的 upstream_addr 会残留
# 端口数字（实测 '55259'，status=502 连不上后端）——那种不是机器，不进拓扑
_UPSTREAM_RE = re.compile(r"^[a-zA-Z0-9_.\-]+:\d+$")


def _valid_upstream(u: str) -> bool:
    return bool(_UPSTREAM_RE.match(u))
_PREFIX_LIMIT = 500    # host × upstream × 前缀 聚合行数上限
_APP_LIMIT = 200       # AppName 聚合行数上限
_MIN_HOST_OK_RATIO = 0.8  # 2xx/3xx 成功率低于此值的 host 视为野流量剔除（真实域名 >90%）


def get_status() -> dict:
    return dict(_last_status)


def _num(row: dict, key: str, default=None):
    """显式取聚合列并转 float，不能"取第一个能转 float 的值"：
    字段顺序 http_host,status,cnt 时 status='200' 会被误当计数。"""
    try:
        return float(row.get(key))
    except (TypeError, ValueError):
        return default


def _ecs_ip_map(db: Session) -> dict:
    """{归一 IP -> ECS 资产名}，键走 ipnorm 单源归一，与查询同口径。"""
    ip_map: dict = {}
    for a in db.query(Asset).filter(Asset.product == "ecs").all():
        try:
            extra = json.loads(a.extra or "{}")
        except ValueError:
            continue
        for ip in extra_ips(extra):
            ip_map[ip] = a.name or a.instance_id
    return ip_map


def _ecs_cpu_map(db: Session) -> dict:
    """{ECS 资产名 -> 近24h CPU 峰值%}（VictoriaMetrics）。"""
    try:
        now_s = int(time.time())
        result = vm_query_range(
            "max_over_time(aliyun_ecs_cpuutilization[24h])",
            now_s - 3600, now_s, 1800)
    except Exception as e:
        logger.warning("ECS CPU 查询失败: %s", e)
        return {}
    iid_name = {a.instance_id: (a.name or a.instance_id)
                for a in db.query(Asset).filter(Asset.product == "ecs").all()}
    out: dict = {}
    for r in result:
        try:
            val = float(r["values"][-1][1])
        except (KeyError, IndexError, ValueError, TypeError):
            continue
        iid = (r.get("metric") or {}).get("instance_id", "")
        name = iid_name.get(iid)
        if name:
            out[name] = round(val, 1)
    return out


def _app_stats(ak: str, sk: str, start_s: int, end_s: int) -> tuple:
    """应用日志画像：({AppName -> 总日志数}, {AppName -> ERROR 数})。

    SLS_APP_LOG 未配置时优雅降级为空画像（collector 守卫会抛 SLSQueryError，
    这里不接住就会把整个草稿生成打挂——应用匹配只是标注，不该绑架主流程）。
    """
    project, logstore = _app_log_target()
    if not project or not logstore:
        return {}, {}
    total: dict = {}
    err: dict = {}
    r = sls_query_logs(ak, sk, project, logstore,
                       "* | select AppName, count(*) as cnt "
                       f"group by AppName order by cnt desc limit {_APP_LIMIT}",
                       start=start_s, end=end_s, limit=_APP_LIMIT)
    for row in r["logs"]:
        name = str(row.get("AppName", ""))
        cnt = _num(row, "cnt")
        if name and cnt is not None:
            total[name] = int(cnt)
    try:
        r2 = sls_query_logs(ak, sk, project, logstore,
                            "LogLevel: ERROR | select AppName, count(*) as cnt "
                            f"group by AppName order by cnt desc limit {_APP_LIMIT}",
                            start=start_s, end=end_s, limit=_APP_LIMIT)
        for row in r2["logs"]:
            name = str(row.get("AppName", ""))
            cnt = _num(row, "cnt")
            if name and cnt is not None:
                err[name] = int(cnt)
    except Exception as e:
        logger.warning("应用 ERROR 聚合失败: %s", e)
    return total, err


def _host_ok_ratio(ak: str, sk: str, project: str, logstore: str,
                   start_s: int, end_s: int) -> dict:
    """{host: 2xx/3xx 成功率}。用于剔除泛解析野流量的 host。

    *.example.com 泛解析落到 SLB 后 nginx 找不到 vhost 走默认 server：
    静态资源全 404、只有碰巧命中的 API 返回 200（bot 探测）。
    真实业务域名 2xx/3xx 占比 >90%，野域名最高 ~58%，界限清晰。
    """
    r = sls_query_logs(ak, sk, project, logstore,
                       "* | select http_host, status, count(*) as cnt "
                       "group by http_host, status order by cnt desc limit 1000",
                       start=start_s, end=end_s, limit=1000)
    stat: dict = {}
    for row in r["logs"]:
        host = str(row.get("http_host", "")).strip()
        status = str(row.get("status", ""))
        cnt = _num(row, "cnt")
        if not host or cnt is None:
            continue
        s = stat.setdefault(host, {"ok": 0, "total": 0})
        s["total"] += int(cnt)
        if status[:1] in ("2", "3"):
            s["ok"] += int(cnt)
    return {h: (v["ok"] / v["total"] if v["total"] else 0.0) for h, v in stat.items()}


def _match_app(host: str, app_counts: dict) -> str:
    """AppName 与 host 匹配：先精确（host 可能带 :port，先去掉），再退化为去掉 www. 前缀。"""
    bare_host = host.rsplit(":", 1)[0] if ":" in host else host
    if bare_host in app_counts:
        return bare_host
    no_www = bare_host[4:] if bare_host.startswith("www.") else ""
    if no_www and no_www in app_counts:
        return no_www
    return ""


def _norm_host(host: str) -> str:
    """域名归一（规则匹配用）：去 :port、小写。"""
    h = (host or "").strip().lower()
    return h.rsplit(":", 1)[0] if ":" in h else h


def _path_prefix(url: str) -> str:
    """云规则 URL 归一为前缀：'/api/*' -> '/api'，'/' 或 '' -> '/'。"""
    u = (url or "").split("?")[0].strip()
    u = u.rstrip("*").rstrip("/")
    return u if u.startswith("/") else "/"


def _rule_covers(rule_prefix: str, path_pattern: str) -> bool:
    """规则前缀是否覆盖该路径边（整段包含关系；'/' 默认规则兜底全命中）。"""
    if rule_prefix == "/":
        return True
    return (path_pattern == rule_prefix
            or path_pattern.startswith(rule_prefix + "/")
            or rule_prefix.startswith(path_pattern + "/"))


def _pick_rule(rules_by_host: dict, host: str, path_pattern: str) -> dict:
    """给一条路径边挑最具体的命中规则：长前缀优先，默认规则（'/'）最后兜底。"""
    cands = [r for r in rules_by_host.get(_norm_host(host), [])
             if _rule_covers(r.get("path_prefix") or _path_prefix(r.get("url", "")), path_pattern)]
    if not cands:
        return {}
    cands.sort(key=lambda r: ((r.get("path_prefix") or "/") == "/",
                              -len(r.get("path_prefix") or "/"),
                              r.get("rule_id") or ""))
    return cands[0]


def _sub_paths(subs: list, pp: str, limit: int = _TOP_PATHS) -> list:
    """把二级路径证据归到所属规则边：'/api/users' 属 '/api'；'/foo' 属 '/'。"""
    out = []
    for p2, c in subs:
        if p2 == pp:
            continue
        belongs = ("/" not in p2[1:]) if pp == "/" else p2.startswith(pp + "/")
        if belongs:
            out.append((p2, c))
    return sorted(out, key=lambda x: -x[1])[:limit]


def _fetch_cloud_rules(db: Session) -> list:
    """多云转发规则（Provider 抽象，云 API 只读）：SLB/CLB 的 URL 转发规则 + 后端组。

    返回 [{rule_key, rule_id, rule_name, domain, url, path_prefix, backends,
    address_type, lb_id, lb_name, vendor, region}]。
    无云账号/无凭证/SDK 失败一律降级为空——草稿主数据是 SLS 日志，规则只是
    标注与补边，不能反客为主。禁止厂商专属路径：一律走 provider.fetch_forward_rules。
    """
    from app.core.security import decrypt_secret
    from app.models.cloud_account import CloudAccount
    from app.services.cloud.provider import get_provider

    rules: list = []
    for acc in db.query(CloudAccount).filter(CloudAccount.enabled.is_(True)).all():
        if not (acc.ak_cipher and acc.secret_cipher):
            continue
        try:
            provider = get_provider(acc.vendor)
            ak = decrypt_secret(acc.ak_cipher)
            sk = decrypt_secret(acc.secret_cipher)
            for r in provider.fetch_forward_rules(acc, ak, sk):
                r.setdefault("path_prefix", _path_prefix(r.get("url", "")))
                r.setdefault("rule_key",
                             f'{r.get("vendor", "")}:{r.get("lb_id", "")}:{r.get("rule_id", "")}')
                rules.append(r)
        except Exception as e:
            logger.warning("云转发规则拉取失败（%s/%s，跳过不阻断草稿）: %s",
                           acc.vendor, acc.name, str(e)[:120])
    return rules


def _generate(db: Session) -> dict:
    from app.collector.credential import resolve_sls_credential

    ak, sk, _src = resolve_sls_credential(db)
    end_s = int(time.time())
    start_s = end_s - 86400

    ip_map = _ecs_ip_map(db)
    ecs_cpu = _ecs_cpu_map(db)
    app_total, app_err = _app_stats(ak, sk, start_s, end_s)

    # 规则边键 = (chain, host, path_pattern, upstream_addr)；跨 logstore 同键合并计数
    edge_map: dict = {}
    skipped_no_upstream = 0
    filtered_hosts: dict = {}  # {host: 请求数}，被剔除的野域名（证据，透明可查）
    for project, logstore, chain in _slb_targets():
        # 0) host 成功率画像：剔除泛解析野流量（bot 探测，2xx/3xx 占比极低）
        try:
            ok_ratio = _host_ok_ratio(ak, sk, project, logstore, start_s, end_s)
        except Exception as e:
            logger.warning("host 状态码画像查询失败 %s: %s", logstore, e)
            ok_ratio = {}

        # 1) host × upstream × 路径前缀主聚合，按规则边粒度统计：
        #    每条规则边各带各的请求数 / 4xx / 5xx / 平均RT，定制路由转发不再被糊掉
        r = sls_query_logs(ak, sk, project, logstore,
                           "* | select http_host, upstream_addr, "
                           "regexp_extract(request_uri, '^(/[^/]*)', 1) as pp, "
                           "count(*) as cnt, "
                           "count_if(status >= 500) as c5xx, "
                           "count_if(status >= 400 and status < 500) as c4xx, "
                           "avg(upstream_response_time) as rt "
                           "group by http_host, upstream_addr, pp order by cnt desc "
                           f"limit {_EDGE_LIMIT}",
                           start=start_s, end=end_s, limit=_EDGE_LIMIT)
        iter_keys: set = set()
        for row in r["logs"]:
            host = str(row.get("http_host", "")).strip()
            upstream = str(row.get("upstream_addr", "")).strip()
            pp = str(row.get("pp", "") or "").strip() or "/"
            cnt = _num(row, "cnt")
            if not host or cnt is None:
                continue
            # upstream 为 '-' 表示 SLB 直接应答（健康检查/拒绝），不是链路边；
            # 含 ',' 是一次请求命中多个后端被合并记录，无法定位单台，跳过；
            # 不是 ip:port/域名:port 形态的是 502 类日志残留（如裸端口 '55259'）——
            # 不是机器，画进拓扑会形成假节点
            if not upstream or upstream == "-" or "," in upstream or not _valid_upstream(upstream):
                skipped_no_upstream += 1
                continue
            # 野域名剔除：2xx/3xx 成功率低于阈值的 host 整体不入草稿
            if ok_ratio and ok_ratio.get(host, 1.0) < _MIN_HOST_OK_RATIO:
                filtered_hosts[host] = filtered_hosts.get(host, 0) + int(cnt)
                continue
            key = (chain, host, pp, upstream)
            iter_keys.add(key)
            st = edge_map.get(key)
            if st is None:
                st = edge_map[key] = {
                    "chain": chain, "host": host, "path_pattern": pp,
                    "upstream_addr": upstream, "cnt": 0, "c5xx": 0, "c4xx": 0,
                    "rt_ms": 0.0, "rt_w": 0, "paths": [],
                }
            st["cnt"] += int(cnt)
            st["c5xx"] += int(_num(row, "c5xx", 0))
            st["c4xx"] += int(_num(row, "c4xx", 0))
            # SLS 里 upstream_response_time 单位是秒，展示统一毫秒
            # （同键跨 logstore 合并时按流量加权近似）
            # 无 RT 样本的行不参与加权；按 0.0 计权重会把整条边的平均 RT 拉低，形成假快。
            rt_raw = _num(row, "rt")
            if rt_raw is not None:
                rt_ms = round(rt_raw * 1000, 1)
                w = int(cnt)
                st["rt_ms"] = (round((st["rt_ms"] * st["rt_w"] + rt_ms * w)
                                     / (st["rt_w"] + w), 1)
                               if st["rt_w"] else rt_ms)
                st["rt_w"] += w

        # 2) 二级路径证据（规则边内的 Top 子路径，如 /api 边下的 /api/users）
        sub_map: dict = {}
        try:
            r2 = sls_query_logs(ak, sk, project, logstore,
                                "* | select http_host, upstream_addr, "
                                "regexp_extract(request_uri, '^(/[^/]*/[^/]*)', 1) as pp2, "
                                "count(*) as cnt group by http_host, upstream_addr, pp2 "
                                f"order by cnt desc limit {_PREFIX_LIMIT}",
                                start=start_s, end=end_s, limit=_PREFIX_LIMIT)
            for row in r2["logs"]:
                host = str(row.get("http_host", "")).strip()
                upstream = str(row.get("upstream_addr", "")).strip()
                p2 = str(row.get("pp2", "") or "").strip() or "/"
                cnt = _num(row, "cnt")
                if host and upstream and _valid_upstream(upstream) and cnt is not None:
                    sub_map.setdefault((host, upstream), []).append((p2, int(cnt)))
        except Exception as e:
            logger.warning("二级路径聚合失败 %s: %s", logstore, e)

        for key in iter_keys:
            _c, host, pp, upstream = key
            edge_map[key]["paths"] = _sub_paths(sub_map.get((host, upstream), []), pp)

    # 3) 多云转发规则：规则边贴规则名 + 补「有规则无流量」边（配置视角不缺角，
    #    走 Provider 抽象只读拉取，失败降级为空不阻断）
    rules = _fetch_cloud_rules(db)
    rules_by_host: dict = {}
    for r in rules:
        if r.get("domain"):
            rules_by_host.setdefault(_norm_host(r["domain"]), []).append(r)
    matched_rule_keys: set = set()
    rule_edges = 0
    for st in edge_map.values():
        rule = _pick_rule(rules_by_host, st["host"], st["path_pattern"])
        if rule:
            st["rule_name"] = (rule.get("rule_name")
                               or f'{rule.get("domain", "")}{rule.get("url", "")}')
            matched_rule_keys.add(rule.get("rule_key"))
            rule_edges += 1
        else:
            st["rule_name"] = ""
    chain_of = {"internet": "outer", "intranet": "inner"}
    cloud_only = 0
    for r in rules:
        if r.get("rule_key") in matched_rule_keys:
            continue
        domain = (r.get("domain") or "").strip()
        if not domain:
            continue   # 默认转发规则没有域名，不造边
        chain_r = chain_of.get(r.get("address_type", ""), "outer")
        pp = r.get("path_prefix") or _path_prefix(r.get("url", ""))
        name = r.get("rule_name") or f'{domain}{r.get("url", "")}'
        for be in (r.get("backends") or []):
            be = str(be).strip()
            if not _valid_upstream(be):
                continue
            key = (chain_r, domain, pp, be)
            if key in edge_map:
                continue   # 该规则边已有流量行，不重复造
            edge_map[key] = {
                "chain": chain_r, "host": domain, "path_pattern": pp,
                "upstream_addr": be, "cnt": 0, "c5xx": 0, "c4xx": 0,
                "rt_ms": 0.0, "paths": [], "rule_name": name,
                "rule_source": "cloud",
            }
            cloud_only += 1
    edges = list(edge_map.values())

    # 4) 落库：删 draft 重建；confirmed/ignored 刷新证据、保留状态；
    #    旧粒度行（path_pattern=''）按 (chain,host,upstream) 就地升级/继承状态——
    #    2026-11-04 规则粒度拆分仍须保留旧边上的用户判断
    db.query(LinkEdge).filter(LinkEdge.status == "draft").delete(
        synchronize_session=False)
    kept = 0
    existing = {}
    legacy = {}
    for e in db.query(LinkEdge).filter(LinkEdge.status != "draft").all():
        if e.path_pattern or "":
            existing[(e.chain, e.host, e.path_pattern, e.upstream_addr)] = e
        else:
            legacy[(e.chain, e.host, e.upstream_addr)] = e
    legacy_used: set = set()
    now = datetime.now()
    for e in edges:
        ip = norm_ip(e["upstream_addr"])
        ecs_name = ip_map.get(ip, "")
        app = _match_app(e["host"], app_total)
        path_summary = json.dumps(
            [{"path": p, "cnt": c} for p, c in e.get("paths", [])], ensure_ascii=False)
        fields = dict(
            ecs_name=ecs_name, app_name=app,
            req_count=e["cnt"], cnt_4xx=e["c4xx"], cnt_5xx=e["c5xx"],
            rt_avg_ms=e["rt_ms"],
            ecs_cpu=ecs_cpu.get(ecs_name),
            app_error_cnt=app_err.get(app, 0),
            path_summary=path_summary, generated_at=now,
            rule_name=e.get("rule_name", ""),
            rule_source=e.get("rule_source", "log"),
        )
        ident = (e["chain"], e["host"], e["path_pattern"], e["upstream_addr"])
        old = existing.get(ident)
        if old is not None:
            for k, v in fields.items():
                setattr(old, k, v)
            kept += 1
            continue
        leg_key = (e["chain"], e["host"], e["upstream_addr"])
        leg = legacy.get(leg_key)
        if leg is not None and leg_key not in legacy_used:
            # 旧全路径行就地升级为本规则边（保 id/状态/备注/确认人）
            legacy_used.add(leg_key)
            leg.path_pattern = e["path_pattern"]
            for k, v in fields.items():
                setattr(leg, k, v)
            existing[ident] = leg
            kept += 1
            continue
        kwargs = {}
        if leg is not None:
            # 2026-11-09 起同一旧边拆出的后续规则边也继承确认状态及身份 id
            kwargs = dict(status=leg.status, note=leg.note,
                          confirmed_by=leg.confirmed_by, confirmed_at=leg.confirmed_at,
                          confirmed_by_id=getattr(leg, "confirmed_by_id", 0))
            kept += 1
        db.add(LinkEdge(chain=e["chain"], host=e["host"],
                        path_pattern=e["path_pattern"],
                        upstream_addr=e["upstream_addr"],
                        **kwargs, **fields))
    db.commit()

    matched_ecs = sum(1 for e in edges if norm_ip(e["upstream_addr"]) in ip_map)
    matched_app = sum(1 for e in edges if _match_app(e["host"], app_total))
    # 被剔除的野域名按流量排序，取前 10 个名字留证据（透明可查）
    dropped = sorted(filtered_hosts.items(), key=lambda x: -x[1])
    result = {
        "edge_count": len(edges),
        "matched_ecs": matched_ecs,
        "matched_app": matched_app,
        "kept_confirmed": kept,
        "skipped_no_upstream": skipped_no_upstream,
        "filtered_host_count": len(filtered_hosts),
        "filtered_hosts_top": [h for h, _ in dropped[:10]],
        "app_names": len(app_total),
        "rule_edges": rule_edges,
        "cloud_rules_total": len(rules),
        "cloud_rules_matched": len(matched_rule_keys),
        "cloud_only_edges": cloud_only,
    }
    msg = f"草稿已生成：{len(edges)} 条边（剔除野域名 {len(filtered_hosts)} 个）"
    if rules:
        msg += f"；云转发规则标注 {rule_edges} 条，仅配置无流量补边 {cloud_only} 条"
    result["message"] = msg
    logger.info("链路草稿生成完成: %s", result)
    if dropped:
        logger.info("已剔除野域名 host（2xx/3xx 占比 < %.0f%%）: %s",
                    _MIN_HOST_OK_RATIO * 100,
                    ", ".join(f"{h}={c}" for h, c in dropped[:20]))
    return result


def generate_link_draft(db: Session) -> dict:
    """生成/刷新链路草稿。返回统计；SLS 未配置返回 {skipped, message}；忙碌 {busy, message}。"""
    # 千枢产品态优雅降级：SLS 目标未配置时干净跳过（不抛异常不刷 ERROR 堆栈）。
    # 只有应用日志没有 SLB 访问日志也不算可生成——链路证据就是 SLB 日志，硬跑会
    # 把既有草稿删空。skipped 说清去哪配置（安装向导 / SLS_* 环境变量）。
    from app.services.sls_targets import slb_targets as _st
    if not _st():
        _last_status["last_result"] = {
            "skipped": "SLS 目标未配置",
            "message": ("未生成草稿：SLS 目标未配置。请在「安装向导 → 数据接入」配置 "
                        "SLB 访问日志目标（SLS_SLB_TARGETS），或在部署环境变量里设置 "
                        "SLS_SLB_TARGETS / SLS_APP_LOG 后重试"),
        }
        logger.info("链路草稿跳过：SLS 目标未配置")
        return _last_status["last_result"]
    if not _lock.acquire(blocking=False):
        return {"busy": True, "message": "草稿生成中，请稍候再点「生成 / 刷新草稿」"}
    _last_status.update({"running": True, "last_run_at": datetime.now().isoformat()})
    try:
        result = _generate(db)
        _last_status["last_result"] = result
        return result
    finally:
        _last_status["running"] = False
        _lock.release()
