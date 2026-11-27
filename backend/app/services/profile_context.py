"""画像构成变化的统一上下文裁决。

只有“原始构成/请求量确实变化”后才调用本模块。上下文稳定可以抑制对应的
无影响漂移；上下文缺失、过期、无法映射或查询失败一律是 unverified，绝不
等同于正常。显著上升必须同时超过相对阈值和绝对阈值，避免低基数放大。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from statistics import mean

from app.core.config import settings

logger = logging.getLogger("qianshu.profile_context")

STABLE = "context_stable"
DRIFT = "drift"
UNVERIFIED = "unverified"
NOT_APPLICABLE = "not_applicable"


def _positive_int(name: str) -> int:
    return max(1, int(getattr(settings, name)))


def _nonnegative_float(name: str) -> float:
    return max(0.0, float(getattr(settings, name)))


def unverified(reason: str) -> dict:
    """构造统一的“证据不足”结果，供映射/采集失败路径使用。"""
    return verdict([], [reason])


def verdict(signals: list, errors: list | None = None) -> dict:
    """汇总各必需信号的裁决。

    任一必需信号未知，整体即 unverified（即使另一信号已上升）；全部有效且
    任一上升为 drift；全部有效且全部未显著上升才是 context_stable。
    """
    problems = [str(x) for x in (errors or []) if x]
    for s in signals:
        if s.get("state") not in ("stable", "rise"):
            problems.append(f"{s.get('name', '上下文')}：{s.get('reason', '证据不足')}")
    if problems:
        return {"verdict": UNVERIFIED, "signals": signals,
                "note": "上下文未核实：" + "；".join(problems)}
    if any(s.get("state") == "rise" for s in signals):
        return {"verdict": DRIFT, "signals": signals,
                "note": "上下文显著上升：" + "；".join(
                    s["note"] for s in signals if s.get("state") == "rise")}
    if not signals:
        return {"verdict": UNVERIFIED, "signals": [], "note": "上下文未核实：没有可评估信号"}
    return {"verdict": STABLE, "signals": signals,
            "note": "上下文稳定（仅抑制本次无影响构成/请求量变化）：" + "；".join(
                s["note"] for s in signals)}


def compare_rise(name: str, baseline: float, current: float, *, relative: float,
                 absolute: float, unit: str) -> dict:
    """双门槛：delta >= 绝对阈值 AND delta/baseline >= 相对阈值才算上升。"""
    delta = current - baseline
    ratio = delta / max(abs(baseline), 1e-9) if delta > 0 else 0.0
    rise = delta >= absolute and ratio >= relative and delta > 0
    return {
        "name": name, "state": "rise" if rise else "stable",
        "baseline": round(baseline, 3), "current": round(current, 3),
        "delta": round(delta, 3), "ratio": round(ratio, 4),
        "note": (f"{name} {baseline:g}→{current:g}{unit}，增量 {delta:g}{unit}"
                 f"（显著上升需同时 ≥{absolute:g}{unit} 且 ≥{relative * 100:g}%）"),
    }


def _thresholds(kind: str) -> tuple[float, float, str]:
    if kind == "cpu":
        return (_nonnegative_float("PROFILE_CONTEXT_CPU_RELATIVE"),
                _nonnegative_float("PROFILE_CONTEXT_CPU_ABS_PP"), "%")
    if kind == "memory":
        return (_nonnegative_float("PROFILE_CONTEXT_MEM_RELATIVE"),
                _nonnegative_float("PROFILE_CONTEXT_MEM_ABS_PP"), "%")
    return (_nonnegative_float("PROFILE_CONTEXT_RT_RELATIVE"),
            _nonnegative_float("PROFILE_CONTEXT_RT_ABS_MS"), "ms")


def series_signal(name: str, kind: str, points: list, *, baseline_start: datetime,
                  current_start: datetime, now: datetime) -> dict:
    """评估 [(epoch_seconds, value), ...]，当前窗和历史窗均须有足够新鲜样本。"""
    min_samples = _positive_int("PROFILE_CONTEXT_MIN_SAMPLES")
    stale_s = _positive_int("PROFILE_CONTEXT_STALE_MINUTES") * 60
    clean = []
    for ts, value in points or []:
        try:
            clean.append((float(ts), float(value)))
        except (TypeError, ValueError):
            continue
    clean.sort()
    cur = [(ts, v) for ts, v in clean if current_start.timestamp() <= ts <= now.timestamp()]
    base = [(ts, v) for ts, v in clean if baseline_start.timestamp() <= ts < current_start.timestamp()]
    if len(cur) < min_samples or len(base) < min_samples:
        return {"name": name, "state": "unverified",
                "reason": f"样本不足（当前 {len(cur)}/历史 {len(base)}，各需 {min_samples}）"}
    if now.timestamp() - cur[-1][0] > stale_s:
        return {"name": name, "state": "unverified",
                "reason": f"最新样本过期（{datetime.fromtimestamp(cur[-1][0]):%Y-%m-%d %H:%M:%S}）"}
    relative, absolute, unit = _thresholds(kind)
    result = compare_rise(name, mean(v for _, v in base), mean(v for _, v in cur),
                          relative=relative, absolute=absolute, unit=unit)
    result["samples"] = {"baseline": len(base), "current": len(cur)}
    return result


def daily_signal(name: str, kind: str, base: dict | None, current, *,
                 collected_at: datetime | None, now: datetime) -> dict:
    """全景日快照适配器；与实时序列共用同一双门槛和最终 verdict。"""
    if current is None:
        return {"name": name, "state": "unverified", "reason": "当前值缺失"}
    if collected_at is None:
        return {"name": name, "state": "unverified", "reason": "采集时间缺失，无法证明新鲜"}
    if now - collected_at > timedelta(minutes=_positive_int("PROFILE_CONTEXT_STALE_MINUTES")):
        return {"name": name, "state": "unverified", "reason": "日快照上下文已过期"}
    min_samples = _positive_int("PROFILE_CONTEXT_MIN_SAMPLES")
    if not base or int(base.get("samples") or 0) < min_samples or base.get("median") is None:
        return {"name": name, "state": "unverified",
                "reason": f"历史基线样本不足（需 {min_samples}）"}
    try:
        cur = float(current)
    except (TypeError, ValueError):
        return {"name": name, "state": "unverified", "reason": "当前值不是有效数值"}
    relative, absolute, unit = _thresholds(kind)
    result = compare_rise(name, float(base["median"]), cur,
                          relative=relative, absolute=absolute, unit=unit)
    result["samples"] = {"baseline": int(base["samples"]), "current": 1}
    return result


def _vm_points(instance_id: str, metric: str, start: datetime, end: datetime) -> list:
    """读取本机 VictoriaMetrics 中同实例总体指标；查询异常向上转为 unverified。"""
    from app.collector.vm import query_range

    step = max(60, _positive_int("PROFILE_CONTEXT_CURRENT_MINUTES") * 60 // 12)
    rows = query_range(metric, int(start.timestamp()), int(end.timestamp()), step)
    return [(ts, value) for row in rows
            if (row.get("metric") or {}).get("instance_id") == instance_id
            for ts, value in (row.get("values") or [])]


def process_context(db, instance_id: str, now: datetime) -> dict:
    """进程 top 变化：同一 ECS/K8s 节点整机 CPU 和内存均为必需证据。"""
    if not settings.PROFILE_CONTEXT_ENABLED:
        return {"verdict": NOT_APPLICABLE, "signals": [], "note": "上下文裁决未启用"}
    current_start = now - timedelta(minutes=_positive_int("PROFILE_CONTEXT_CURRENT_MINUTES"))
    baseline_start = current_start - timedelta(minutes=_positive_int("PROFILE_CONTEXT_BASELINE_MINUTES"))
    signals, errors = [], []
    for name, kind, metric in (
            ("整机 CPU", "cpu", "aliyun_ecs_cpuutilization"),
            ("整机内存", "memory", "aliyun_ecs_memory_usedutilization")):
        try:
            points = _vm_points(instance_id, metric, baseline_start, now)
            signals.append(series_signal(name, kind, points, baseline_start=baseline_start,
                                         current_start=current_start, now=now))
        except Exception as exc:
            logger.warning("画像上下文查询失败 %s %s: %s", instance_id, metric, str(exc)[:120])
            errors.append(f"{name}查询失败")
    return verdict(signals, errors)


def _matching_edges(db, uri: str) -> list:
    """按已发现转发路径匹配 URI；每域名取最长路径，不猜测无拓扑的对应关系。"""
    from app.models.link_edge import LinkEdge

    path = (uri or "").split("?", 1)[0]
    rows = (db.query(LinkEdge)
            .filter(LinkEdge.status != "ignored", LinkEdge.chain == "outer").all())
    matched = [e for e in rows if e.host and e.ecs_name
               and (not e.path_pattern or path.startswith(e.path_pattern))]
    best_by_host = {}
    for edge in matched:
        specificity = len(edge.path_pattern or "")
        old = best_by_host.get(edge.host)
        if old is None or specificity > old[0]:
            best_by_host[edge.host] = (specificity, [])
        if specificity == best_by_host[edge.host][0]:
            best_by_host[edge.host][1].append(edge)
    return [e for _, edges in best_by_host.values() for e in edges]


def slb_uri_context(db, uri: str, now: datetime) -> dict:
    """传统 URL top 变化：唯一拓扑对应的所有后端 CPU + 对应域名 RT。

    无路径映射、一个后端缺资产、任一信号缺失都返回 unverified；不会借用其它
    域名或无关主机的指标来制造“稳定”。SLS 使用既有小时桶语法，当前窗默认
    3 小时，历史窗默认此前 6 小时。
    """
    if not settings.PROFILE_CONTEXT_ENABLED:
        return {"verdict": NOT_APPLICABLE, "signals": [], "note": "上下文裁决未启用"}
    edges = _matching_edges(db, uri)
    if not edges:
        return unverified(f"URL {uri or '?'} 无可用域名→后端拓扑映射")
    hosts = sorted({e.host for e in edges})
    if len(hosts) != 1:
        # 传统 slb_uri 快照只有 URI、没有 http_host；同路径可属于多个域名时无法
        # 证明哪些域名是“对应后端”，不得借用全部域名的稳定数据抑制。
        return unverified(f"URI {uri or '?'} 所属域名不唯一（{','.join(hosts)}），无法确定对应后端")
    backend_names = sorted({e.ecs_name for e in edges if e.ecs_name})
    if not backend_names:
        return unverified("URL 对应后端未映射 ECS")
    from app.models.asset import Asset
    assets = (db.query(Asset).filter(Asset.product == "ecs", Asset.name.in_(backend_names)).all())
    ids = {a.name: a.instance_id for a in assets if a.instance_id}
    missing = sorted(set(backend_names) - set(ids))
    if missing:
        return unverified("后端 ECS 资产缺失：" + ",".join(missing))

    current_start = now - timedelta(minutes=_positive_int("PROFILE_CONTEXT_CURRENT_MINUTES"))
    baseline_start = current_start - timedelta(minutes=_positive_int("PROFILE_CONTEXT_BASELINE_MINUTES"))
    signals, errors = [], []
    for name in backend_names:
        try:
            points = _vm_points(ids[name], "aliyun_ecs_cpuutilization", baseline_start, now)
            signals.append(series_signal(f"后端 {name} CPU", "cpu", points,
                                         baseline_start=baseline_start,
                                         current_start=current_start, now=now))
        except Exception as exc:
            logger.warning("URL 上下文 CPU 查询失败 %s: %s", name, str(exc)[:120])
            errors.append(f"后端 {name} CPU 查询失败")

    try:
        from app.collector.credential import resolve_sls_credential
        from app.collector.sls import query_logs
        from app.services.sls_targets import slb_pairs
        ak, sk, _ = resolve_sls_credential(db)
        for host in hosts:
            safe_host = host.replace("\\", "\\\\").replace('"', '\\"')
            query = (f'http_host: "{safe_host}" | select date_trunc(\'hour\', __time__) as h, '
                     'avg(upstream_response_time) as rt, count(*) as n group by h order by h limit 100')
            # 每个域名单独评估，不能用跨域名均值掩盖其中一个后端 RT 恶化。
            buckets = {}
            for project, logstore in slb_pairs():
                result = query_logs(ak, sk, project, logstore, query,
                                    start=int(baseline_start.timestamp()), end=int(now.timestamp()), limit=100)
                for row in result.get("logs") or []:
                    if row.get("rt") is None:
                        continue
                    stamp = datetime.fromisoformat(str(row.get("h")).replace("Z", "+00:00")).replace(tzinfo=None)
                    # 小时桶结束时间作为样本时刻；当前未完小时最多取 now。
                    ts = min(stamp.timestamp() + 3600, now.timestamp())
                    rt = float(row.get("rt"))
                    if rt <= 0:
                        continue   # 与全景采集一致：无有效 RT 不是“0ms 很快”
                    rt_ms = rt * 1000 if rt < 100 else rt
                    n = max(0.0, float(row.get("n") or 0))
                    old = buckets.setdefault(ts, [0.0, 0.0])
                    old[0] += rt_ms * n
                    old[1] += n
            points = [(ts, total / n) for ts, (total, n) in buckets.items() if n > 0]
            signals.append(series_signal(f"域名 {host} 后端响应时间", "rt", points,
                                         baseline_start=baseline_start,
                                         current_start=current_start, now=now))
    except Exception as exc:
        logger.warning("URL 上下文 RT 查询失败: %s", str(exc)[:120])
        errors.append("后端响应时间查询失败")
    return verdict(signals, errors)


def for_behavior_profile(db, profile, now: datetime, latest: dict) -> dict:
    """传统日/快层共用入口；其它画像类型不做不相干的资源抑制。"""
    if not settings.PROFILE_CONTEXT_ENABLED:
        return {"verdict": NOT_APPLICABLE, "signals": [], "note": "上下文裁决未启用"}
    if profile.kind == "process":
        try:
            return process_context(db, profile.subject, now)
        except Exception as exc:
            logger.warning("进程上下文裁决失败 %s: %s", profile.subject, str(exc)[:120])
            return unverified("进程上下文采集失败")
    if profile.kind == "slb_uri":
        members = latest.get("members") or []
        try:
            return slb_uri_context(db, members[0].get("name", "") if members else "", now)
        except Exception as exc:
            logger.warning("URL 上下文裁决失败: %s", str(exc)[:120])
            return unverified("URL 对应后端上下文采集失败")
    return {"verdict": NOT_APPLICABLE, "signals": [], "note": "该画像类型无适用上下文"}
