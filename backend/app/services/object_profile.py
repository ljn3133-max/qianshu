"""对象全景画像引擎。

对象分析联合 CDN、SLB、应用、主机、数据库、变更和成本信号，避免仅依赖单个产品 API
或要求运维人员逐层排查。画像对象为业务对象（域名/主机/数据库），每个对象的时间轴
关联多层信号；变化判定以层间联动被打破为依据，而非单源漂移，产出带因果链的事件。

七层信号（缺层如实留空，不硬凑）：
  cdn    SLS CDN 实时日志（流量/热点文件/状态码）+ cdn_domain_daily（T+1 口径对照）
  slb    SLS SLB 日志（请求/5xx/RT）
  app    SLS 应用日志 ERROR（按链路映射的 AppName）
  host   VM 后端 ECS CPU/内存/磁盘峰值
  db     dba_space_daily + RDS 指标（对象是 db 或映射到 db 时）
  change 统一变更源事件；cost 账单/包消耗（钱的归宿）

可回滚：OBJECT_PROFILE_V2=False 时本模块不被调度（旧 profile_engine 原样跑）；
数据只写 object_profiles / object_profile_daily 新表 + cost_anomalies(type=chain_drift)。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.collector.sls import query_logs as sls_query_logs
from app.collector.vm import query_range as vm_query_range
from app.core.config import settings
from app.models.asset import Asset
from app.models.cdn_domain_daily import CdnDomainDaily
from app.models.change_event import ChangeEvent
from app.models.cost_anomaly import CostAnomaly
from app.models.cost_daily import CostDaily
from app.models.dba_space import DbaSpaceDaily
from app.models.link_edge import LinkEdge
from app.models.object_profile import ObjectProfile, ObjectSnapshotDaily

logger = logging.getLogger("qianshu.object_profile")

GB = 1024 ** 3
# SLS 目标统一走配置，避免硬编码在换环境后查询落空；见 services/sls_targets.py。
from app.services.sls_targets import app_log, cdn_target, slb_pairs  # noqa: E402

_LAYERS = ("cdn", "slb", "app", "host", "db", "change", "cost")


# ---------------------------------------------------------------- 发现对象


def discover_objects(db: Session) -> list:
    """发现全景对象：域名（链路库+CDN 域名）/ 主机（后端/core ECS）/ 数据库（RDS）。"""
    objs: dict = {}
    hosts_of: dict = {}     # domain -> [ecs_name]
    apps_of: dict = {}      # domain -> [app_name]

    for e in db.query(LinkEdge).filter(LinkEdge.status != "ignored").all():
        key = (e.host or "").strip().lower()
        if not key:
            continue
        objs.setdefault(("domain", key), {"label": key, "tier": "core"})
        if e.ecs_name:
            hosts_of.setdefault(key, [])
            if e.ecs_name not in hosts_of[key]:
                hosts_of[key].append(e.ecs_name)
        if e.app_name:
            apps_of.setdefault(key, [])
            if e.app_name not in apps_of[key]:
                apps_of[key].append(e.app_name)

    for (d,) in db.query(CdnDomainDaily.domain).distinct().all():
        key = (d or "").strip().lower()
        if key:
            objs.setdefault(("domain", key), {"label": key, "tier": "core"})

    assets = {a.name: a for a in db.query(Asset).filter(Asset.product == "ecs").all()}
    backends = {n for lst in hosts_of.values() for n in lst}
    for name in backends:
        a = assets.get(name)
        if a is None:
            continue
        objs.setdefault(("host", a.instance_id), {
            "label": name, "tier": (a.tier or "core")})

    for a in db.query(Asset).filter(Asset.product == "rds").all():
        objs.setdefault(("db", a.instance_id), {
            "label": a.name or a.instance_id, "tier": "core"})

    out = []
    for (otype, key), meta in objs.items():
        if meta.get("tier") == "aux":
            continue   # aux 辅助机不进全景告警（D_exemption_layers）
        out.append({"object_type": otype, "object_key": key, **meta,
                    "hosts": hosts_of.get(key, []), "apps": apps_of.get(key, [])})
    return out


def ensure_profiles(db: Session) -> list:
    """注册对象（幂等）。"""
    created = []
    for o in discover_objects(db):
        p = db.query(ObjectProfile).filter(
            ObjectProfile.object_type == o["object_type"],
            ObjectProfile.object_key == o["object_key"]).first()
        if p is None:
            p = ObjectProfile(object_type=o["object_type"], object_key=o["object_key"],
                              label=o.get("label") or o["object_key"], tier=o.get("tier") or "core")
            db.add(p)
            created.append(f"{o['object_type']}:{o['object_key']}")
    db.flush()
    return created


# ---------------------------------------------------------------- 采集（各层）


def _day_bounds(d: date) -> tuple:
    s = int(datetime.combine(d, datetime.min.time()).timestamp())
    return s, s + 86400


def _sls_day_rows(ak: str, sk: str, project: str, logstore: str, query: str,
                  start_s: int, end_s: int) -> list:
    try:
        r = sls_query_logs(ak, sk, project, logstore, query, start=start_s, end=end_s, limit=100)
        return r["logs"]
    except Exception as e:
        logger.warning("全景 SLS 查询失败 %s/%s: %s", project, logstore, str(e)[:100])
        return []


def _day_key(v) -> str:
    return str(v or "")[:10]


def _collect_cdn_multi(db: Session, ak: str, sk: str, domain: str,
                       start_s: int, end_s: int) -> dict:
    """cdn 层（按天分组一次查全窗口）：流量/请求/5xx/热点文件。返回 {日期: 层数据}。"""
    rows = _sls_day_rows(ak, sk, *cdn_target(),
                         f'domain: "{domain}" | select date_trunc(\'day\', __time__) as d, '
                         'sum(response_size) as bytes, count(*) as reqs, '
                         'count_if(return_code >= 500) as e5xx group by d order by d limit 40',
                         start_s, end_s)
    out: dict = {}
    for row in rows:
        out[_day_key(row.get("d"))] = {
            "traffic_gb": round(_f(row.get("bytes")) / GB, 3),
            "requests": int(_f(row.get("reqs"))),
            "code_5xx": int(_f(row.get("e5xx"))),
        }
    tops = _sls_day_rows(ak, sk, *cdn_target(),
                         f'domain: "{domain}" | select date_trunc(\'day\', __time__) as d, '
                         'uri, sum(response_size) as bytes group by d, uri '
                         'order by d asc, bytes desc limit 600', start_s, end_s)
    per_day: dict = {}
    for x in tops:
        dk = _day_key(x.get("d"))
        per_day.setdefault(dk, []).append(
            {"uri": str(x.get("uri") or "")[:160], "gb": round(_f(x.get("bytes")) / GB, 3)})
    for dk, lst in per_day.items():
        out.setdefault(dk, {})["top_files"] = lst[:5]
    # SLS 实时日志保留期可能短于基线窗口，使用 cdn_domain_daily 的 T+1 历史补齐
    # 缺日并标注 source=api_t1，避免样本集中到特定日型而扭曲基线。
    d_lo = date.fromtimestamp(start_s)
    d_hi = date.fromtimestamp(max(start_s, end_s - 1))
    for row in (db.query(CdnDomainDaily)
                .filter(CdnDomainDaily.domain == domain,
                        CdnDomainDaily.stat_date >= d_lo,
                        CdnDomainDaily.stat_date <= d_hi).all()):
        dk = row.stat_date.isoformat()
        cell = out.setdefault(dk, {})
        if "traffic_gb" not in cell:
            cell["traffic_gb"] = round((row.traffic_bytes or 0) / GB, 3)
            cell["source"] = "api_t1"
    return out


def _collect_slb_multi(db: Session, ak: str, sk: str, domain: str,
                       start_s: int, end_s: int) -> dict:
    out: dict = {}
    for project, logstore in slb_pairs():
        rows = _sls_day_rows(ak, sk, project, logstore,
                             f'http_host: "{domain}" | select date_trunc(\'day\', __time__) as d, '
                             'count(*) as reqs, count_if(status >= 500) as e5xx, '
                             'avg(upstream_response_time) as rt group by d order by d limit 40',
                             start_s, end_s)
        for row in rows:
            dk = _day_key(row.get("d"))
            cell = out.setdefault(dk, {"requests": 0, "code_5xx": 0, "_rt": []})
            cell["requests"] += int(_f(row.get("reqs")))
            cell["code_5xx"] += int(_f(row.get("e5xx")))
            rt = _f(row.get("rt"))
            if rt > 0:
                cell["_rt"].append(rt * 1000 if rt < 100 else rt)
    for dk, cell in out.items():
        rts = cell.pop("_rt", [])
        cell["rt_avg_ms"] = round(sum(rts) / len(rts), 1) if rts else None
    return out


def _collect_app_multi(db: Session, ak: str, sk: str, apps: list,
                       start_s: int, end_s: int) -> dict:
    if not apps:
        return {}
    out: dict = {}
    for app in apps[:3]:
        rows = _sls_day_rows(ak, sk, *app_log(),
                             f'AppName: "{app}" and LogLevel: ERROR | '
                             "select date_trunc('day', __time__) as d, count(*) as cnt "
                             'group by d order by d limit 40', start_s, end_s)
        for row in rows:
            dk = _day_key(row.get("d"))
            cell = out.setdefault(dk, {"errors": 0, "top_apps": []})
            c = int(_f(row.get("cnt")))
            cell["errors"] += c
            if c:
                cell["top_apps"].append({"name": app, "errors": c})
    return out


def _collect_host(db: Session, iid: str, day: date) -> dict:
    s, e = _day_bounds(day)
    out = {}
    for field, series in (("cpu_peak", "aliyun_ecs_cpuutilization"),
                          ("mem_peak", "aliyun_ecs_memory_usedutilization"),
                          ("disk_peak", "aliyun_ecs_diskusage_utilization")):
        try:
            res = vm_query_range(f"max_over_time({series}[24h])", s, s + 300, 300)
            vals = [float(v) for r in res if (r.get("metric") or {}).get("instance_id") == iid
                    for _, v in r.get("values", [])]
            if vals:
                out[field] = round(max(vals), 1)
        except Exception as e2:
            logger.warning("全景 host 层查询失败 %s %s: %s", iid, field, str(e2)[:80])
    return out


def _collect_db(db: Session, iid: str, day: date) -> dict:
    row = db.query(DbaSpaceDaily).filter(
        DbaSpaceDaily.instance_id == iid, DbaSpaceDaily.stat_date == day).first()
    if row is None:
        return {}
    return {"disk_gb": round((row.disk_used or 0) / GB, 1),
            "data_gb": round((row.data_size or 0) / GB, 1)}


def _collect_change(db: Session, keys: list, day: date) -> dict:
    rows = (db.query(ChangeEvent)
            .filter(ChangeEvent.event_time >= datetime.combine(day, datetime.min.time()),
                    ChangeEvent.event_time < datetime.combine(day + timedelta(days=1), datetime.min.time()))
            .all())
    items = []
    for c in rows:
        blob = f"{c.summary or ''} {c.resource or ''} {c.event_name or ''}"
        if any(k and k.lower() in blob.lower() for k in keys):
            items.append(f"{c.event_time:%H:%M} {c.event_name} {c.resource}")
    return {"count": len(items), "items": items[:5]}


def _collect_cost(db: Session, otype: str, key: str, day: date, domain: str) -> dict:
    out = {}
    if otype in ("host", "db"):
        v = db.query(func.sum(CostDaily.pretax_amount)).filter(
            CostDaily.stat_date == day, CostDaily.instance_id == key).scalar()
        if v is not None:
            out["yuan"] = round(float(v), 2)
    if otype == "domain":
        row = db.query(CdnDomainDaily).filter(
            CdnDomainDaily.domain == domain, CdnDomainDaily.stat_date == day).first()
        if row is not None:
            out["pack_gb"] = round((row.traffic_bytes or 0) / GB, 3)
    return out


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------- 采集编排


def collect_window(db: Session, start_day: date, end_day: date,
                   obj_filter: dict | None = None) -> int:
    """采集 [start_day, end_day] 窗口内全部对象的逐日多层快照（upsert 幂等）。

    性能：SLS 层按天分组一次查全窗口，避免逐日查询导致回填耗时过长。
    """
    ensure_profiles(db)
    objects = discover_objects(db)
    # 空库/无对象时不需要云凭证：先返回，避免首次引导在“无源”状态因缺凭证失败。
    if not objects:
        return 0
    from app.collector.credential import resolve_sls_credential
    ak, sk, _ = resolve_sls_credential(db)
    days = [start_day + timedelta(days=i) for i in range((end_day - start_day).days + 1)]
    start_s, _ = _day_bounds(start_day)
    _, end_s = _day_bounds(end_day + timedelta(days=1))
    n = 0
    for o in objects:
        if obj_filter and (o["object_type"], o["object_key"]) != (
                obj_filter.get("object_type"), obj_filter.get("object_key")):
            continue
        otype, key = o["object_type"], o["object_key"]
        domain = key if otype == "domain" else ""
        by_day: dict = {d.isoformat(): {} for d in days}
        if otype == "domain":
            for dk, cell in _collect_cdn_multi(db, ak, sk, domain, start_s, end_s).items():
                by_day.setdefault(dk, {})["cdn"] = cell
            for dk, cell in _collect_slb_multi(db, ak, sk, domain, start_s, end_s).items():
                by_day.setdefault(dk, {})["slb"] = cell
            for dk, cell in _collect_app_multi(db, ak, sk, o.get("apps") or [], start_s, end_s).items():
                by_day.setdefault(dk, {})["app"] = cell
            for h in (o.get("hosts") or [])[:3]:
                inst = db.query(Asset).filter(Asset.name == h).first()
                if inst is None:
                    continue
                for d in days:
                    hv = _collect_host(db, inst.instance_id, d)
                    if hv:
                        cell = by_day.setdefault(d.isoformat(), {}).setdefault(
                            "host", {"backends": []})
                        if h not in cell["backends"]:
                            cell["backends"].append(h)
                        for f, v in hv.items():
                            cell[f] = max(cell.get(f, 0) or 0, v)
                        # 保留逐后端值：max 聚合会掩盖“共享节点中只有一个后端恶化”。
                        cell.setdefault("backend_metrics", {})[inst.instance_id] = dict(hv)
        elif otype == "host":
            for d in days:
                hv = _collect_host(db, key, d)
                if hv:
                    hv["backend_metrics"] = {key: {k: v for k, v in hv.items() if k != "backend_metrics"}}
                    by_day.setdefault(d.isoformat(), {})["host"] = hv
            for dk, cell in _collect_app_multi(db, ak, sk, o.get("apps") or [], start_s, end_s).items():
                by_day.setdefault(dk, {})["app"] = cell
        elif otype == "db":
            for d in days:
                dv = _collect_db(db, key, d)
                if dv:
                    by_day.setdefault(d.isoformat(), {})["db"] = dv

        for d in days:
            layers = by_day.get(d.isoformat(), {})
            # JSON 内记录快照采集时间，避免“日快照存在”被误当成“上下文仍新鲜”。
            layers["_context_collected_at"] = datetime.now().isoformat(timespec="seconds")
            layers["change"] = _collect_change(db, [key, o.get("label") or ""], d)
            layers["cost"] = _collect_cost(db, otype, key, d, domain)
            if not any(layers.get(k) for k in ("cdn", "slb", "app", "host", "db")):
                continue   # 该日各层全空不落快照（如实留空，不造假行）
            snap = db.query(ObjectSnapshotDaily).filter(
                ObjectSnapshotDaily.object_type == otype,
                ObjectSnapshotDaily.object_key == key,
                ObjectSnapshotDaily.stat_date == datetime.combine(d, datetime.min.time())).first()
            if snap is None:
                snap = ObjectSnapshotDaily(object_type=otype, object_key=key,
                                           stat_date=datetime.combine(d, datetime.min.time()))
                db.add(snap)
            snap.layers = layers
            n += 1
    db.flush()
    return n


def collect_day(db: Session, day: date, obj_filter: dict | None = None) -> int:
    """采集单日快照（= 1 天窗口）。"""
    return collect_window(db, day, day, obj_filter)


def backfill(db: Session, days: int = 7) -> int:
    """回填近 N 天快照（学习基线用）。"""
    return collect_window(db, date.today() - timedelta(days=days), date.today() - timedelta(days=1))


# ---------------------------------------------------------------- 学习与判定


_COUPLINGS = [
    ("cdn", "traffic_gb", "slb", "requests", "cdn_gb:slb_reqs"),
    ("app", "errors", "slb", "requests", "app_err:slb_reqs"),
    ("cdn", "traffic_gb", "cost", "pack_gb", "cdn_gb:pack_gb"),
]


def learn(db: Session) -> int:
    """从近 N 天快照学习各层水位与层间联动比值（中位数+MAD，可解释）。

    **同日型分组**（D_same_day_baseline）：工作日、休息日、法定节假日分别建立基线，
    避免不同作息的流量差异被误判为异常。分组键是实际作息（holiday.day_kind，含调休）。
    """
    from app.services.holiday import day_kind
    window = max(settings.PROFILE_LEARN_DAYS + 10, 21)
    since = datetime.combine(date.today() - timedelta(days=window), datetime.min.time())
    snaps = (db.query(ObjectSnapshotDaily)
             .filter(ObjectSnapshotDaily.stat_date >= since,
                     ObjectSnapshotDaily.stat_date < datetime.combine(date.today(), datetime.min.time()))
             .all())
    by_obj: dict = {}
    for s in snaps:
        by_obj.setdefault((s.object_type, s.object_key), []).append(s)

    learned = 0
    for p in db.query(ObjectProfile).all():
        rows = sorted(by_obj.get((p.object_type, p.object_key), []), key=lambda x: x.stat_date)
        if len(rows) < settings.PROFILE_MIN_SAMPLES:
            continue
        kinds: dict = {"all": rows}
        for s in rows:
            kinds.setdefault(day_kind(db, s.stat_date.date()), []).append(s)

        layers_base: dict = {}
        couplings: dict = {}
        context_base: dict = {}
        for kind, krows in kinds.items():
            lb_k, cp_k = {}, {}
            for layer in _LAYERS:
                vals = [_layer_val(s.layers.get(layer), _primary_metric(layer)) for s in krows]
                vals = [v for v in vals if v is not None]
                if len(vals) >= max(3, settings.PROFILE_MIN_SAMPLES - 2):
                    lb_k[layer] = _mad_base(vals)
            for la, ma, lb, mb, name in _COUPLINGS:
                ratios = []
                for s in krows:
                    va = _layer_val(s.layers.get(la), ma)
                    vb = _layer_val(s.layers.get(lb), mb)
                    if va is not None and vb is not None and vb > 0:
                        ratios.append(va / vb)
                if len(ratios) >= 3:
                    cp_k[name] = _mad_base(ratios)
            if lb_k:
                layers_base[kind] = lb_k
            # URL 请求量抑制所需的逐后端 CPU 与 SLB RT 基线；缺任一项后续只给 unverified。
            cpu_by_backend = {}
            for s in krows:
                metrics = ((s.layers.get("host") or {}).get("backend_metrics") or {})
                for iid, values in metrics.items():
                    if values.get("cpu_peak") is not None:
                        cpu_by_backend.setdefault(iid, []).append(float(values["cpu_peak"]))
            rt_vals = [(s.layers.get("slb") or {}).get("rt_avg_ms") for s in krows]
            rt_vals = [float(v) for v in rt_vals if v is not None]
            min_context = max(1, int(settings.PROFILE_CONTEXT_MIN_SAMPLES))
            ctx_k = {"host_cpu": {iid: _mad_base(vals) for iid, vals in cpu_by_backend.items()
                                  if len(vals) >= min_context}}
            if len(rt_vals) >= min_context:
                ctx_k["slb_rt"] = _mad_base(rt_vals)
            if ctx_k.get("host_cpu") or ctx_k.get("slb_rt"):
                context_base[kind] = ctx_k
            if cp_k:
                couplings[kind] = cp_k

        top_files = []
        for s in reversed(rows):
            tf = (s.layers.get("cdn") or {}).get("top_files") or []
            if tf:
                top_files = [x.get("uri") for x in tf]
                break
        p.baseline = {"layers_by_kind": layers_base, "couplings_by_kind": couplings,
                      "context_by_kind": context_base,
                      "top_files": top_files, "samples": len(rows)}
        p.learned = True
        if p.status == "observing":
            p.status = "normal"   # 学到基线即脱离观察期（有漂移时 detect 会改 drift）
        learned += 1
    db.flush()
    return learned


def _primary_metric(layer: str) -> str:
    return {"cdn": "traffic_gb", "slb": "requests", "app": "errors",
            "host": "mem_peak", "db": "disk_gb", "change": "count", "cost": "yuan"}.get(layer, "value")


def _layer_val(layer: dict | None, metric: str):
    if not layer:
        return None
    v = layer.get(metric)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _mad_base(vals: list) -> dict:
    vals = sorted(vals)
    med = vals[len(vals) // 2]
    mad = sorted(abs(v - med) for v in vals)[len(vals) // 2]
    return {"median": round(med, 4), "mad": round(mad, 4), "samples": len(vals)}


def detect(db: Session, day: date | None = None) -> list:
    """联动变化检测：单层异常 / 联动被打破 / 热点构成更替 -> chain_drift 事件（带因果链）。"""
    from app.services.cost_anomaly import _add
    day = day or date.today()
    found = []
    for p in db.query(ObjectProfile).filter(ObjectProfile.learned.is_(True)).all():
        snap = db.query(ObjectSnapshotDaily).filter(
            ObjectSnapshotDaily.object_type == p.object_type,
            ObjectSnapshotDaily.object_key == p.object_key,
            ObjectSnapshotDaily.stat_date == datetime.combine(day, datetime.min.time())).first()
        if snap is None:
            p.status = "unverified"
            p.last_drift_note = "上下文未核实：当日快照缺失，未执行恢复判定"
            continue
        findings = _compare(db, p, snap)
        if not findings:
            if not findings.evaluated:
                # 没有同日型适用基线=未评估，不是“没有变化”，更不能自动收口旧事件。
                if p.status != "known":
                    p.status = "unverified"
                p.last_drift_note = ("\n".join(findings.context_notes)
                                     or "上下文未核实：缺少适用的同日型基线，本次未执行漂移/恢复判定")
                continue
            if findings.raw_count:
                # 原始 SLB 变化仍在，只是稳定上下文抑制；这与真正恢复分账。
                if p.status != "known":
                    p.status = "context_stable"
                p.last_drift_note = "\n".join(findings.context_notes)[:2000]
                continue
            # 只有“适用基线存在 + 完整比较 + 原始变化为零”才允许恢复。
            if p.status == "known":
                # 变化消失后回 normal；known_at 保留为那次确认的历史。
                p.silence_until = None
                p.notify_state = ""
            if p.status in ("drift", "known"):
                p.status = "normal"
            _clear_pending(p)
            # 变化型事件未复现即收口（D_change_event_lifecycle）
            for old in (db.query(CostAnomaly)
                        .filter(CostAnomaly.type == "chain_drift",
                                CostAnomaly.target_key == f"{p.object_type}:{p.object_key}",
                                CostAnomaly.ack_status.in_(("open", "acknowledged"))).all()):
                old.ack_status = "resolved"
                old.ack_note = (old.ack_note or "") + "\n[联动已恢复平衡，自动收口]"
            continue

        # ---- 分级响应：小波动持续稳定可自适应为新常态，大变化重点关注 ----
        has_unverified = any(f.get("context_state") == "unverified" for f in findings)
        mags = {f.get("magnitude", _MAG_NOTABLE) for f in findings}
        if mags == {_MAG_WATCH}:
            # 小波动：不打扰，记新常态候选；连续 7 天 -> 自动抬基线（标记正常）
            promoted = _note_watch(db, p, snap, findings, day)
            logger.info("观测 %s: 小波动已记账%s", p.object_key, "（已自适应抬基线）" if promoted else "")
            continue

        chain = _render_chain(p, snap, findings)
        if findings.context_notes:
            chain += "\n\n上下文裁决：\n" + "\n".join("- " + n for n in findings.context_notes)
        if _MAG_MAJOR in mags:
            chain += "\n\n" + _auto_analyze(db, p, snap, findings)
        # ---- 生命周期：已知道且处于静默期内时只记录，不建事件、不发报告；
        # 静默到期后漂移仍在则重新建事件恢复提醒；aux 未勾选静默时持续通知。 ----
        from app.services.profile_engine import notify_suppressed
        now = datetime.now()
        if notify_suppressed(p, now):
            p.notify_state = "silenced"
            continue
        if p.status == "known":
            p.notify_state = "remind" if p.silence_until is not None else ""
            if p.silence_until is not None:
                chain += ("\n\n提醒：用户已知道过此变化"
                          + (f"（{p.known_at:%m-%d %H:%M} 确认）" if p.known_at else "")
                          + "，静默期已到，变化仍在故恢复提醒")
        else:
            p.status = "unverified" if has_unverified else "drift"
            p.notify_state = ""
        p.last_drift_note = chain[:2000]
        level = "critical" if any(f.get("sev") == "critical" for f in findings) else "warning"
        a = _add(db, day, "chain_drift", f"{p.object_type}:{p.object_key}", p.label or p.object_key,
                 (f"架构变化待核实: {p.label or p.object_key}" if has_unverified
                  else f"架构异常: {p.label or p.object_key}（{len(findings)} 处配置不匹配）"), chain,
                 level=level)
        if a:
            found.append(a)
            if _MAG_MAJOR in mags:
                _send_report(db, p, chain)   # 重点关注：单独出中肯报告（站内信）
    db.flush()
    return found


# ---------------------------------------------------------------- 分级响应


def _clear_pending(p: ObjectProfile) -> None:
    base = p.baseline or {}
    if base.pop("pending_normal", None) is not None:
        p.baseline = base


def _note_watch(db: Session, p: ObjectProfile, snap, findings: list, day: date) -> bool:
    """小波动记账：连续 7 天维持 -> 自动抬基线（标记正常）。安全阀：只抬 watch 级。"""
    base = dict(p.baseline or {})
    pend = dict(base.get("pending_normal") or {})
    pend.setdefault("values", [])
    pend["last_day"] = day.isoformat()
    pend["streak"] = int(pend.get("streak") or 0) + 1
    for f in findings:
        layer = f.get("layer")
        v = _layer_val((snap.layers or {}).get(layer), _primary_metric(layer))
        if v is not None:
            pend["values"].append({"day": day.isoformat(), "layer": layer, "value": v})
    base["pending_normal"] = pend
    promoted = False
    if pend["streak"] >= 7:
        # 自适应：用这 7 天观测值重算该层基线（新日常态），并留痕
        by_kind = dict(base.get("layers_by_kind") or {})
        from app.services.holiday import day_kind as _dk
        kind = _dk(db, day)
        kb = dict(by_kind.get(kind) or {})
        for layer in {v["layer"] for v in pend["values"]}:
            vals = [v["value"] for v in pend["values"] if v["layer"] == layer]
            if len(vals) >= 3:
                kb[layer] = _mad_base(vals)
        by_kind[kind] = kb
        base["layers_by_kind"] = by_kind
        base.setdefault("auto_normal", []).append({
            "day": day.isoformat(), "kind": kind,
            "layers": sorted({v["layer"] for v in pend["values"]}),
            "streak": pend["streak"],
            "note": "小波动连续 7 天维持，已自动抬基线为新常态（watch 级自适应）",
        })
        base.pop("pending_normal", None)
        promoted = True
    p.baseline = base
    return promoted


def _auto_analyze(db: Session, p: ObjectProfile, snap, findings: list) -> str:
    """重点关注级的自动根因分析（后台跑，报告里直接给结论）：流量类算热点与压缩收益。"""
    out = ["## 自动分析（重点关注级）"]
    layers_hit = {f.get("layer") for f in findings}
    domain = p.object_key if p.object_type == "domain" else ""
    if "cdn" in layers_hit and domain:
        try:
            from app.services.cdn_traffic import url_breakdown
            b = url_breakdown(db, domain, days=2, top=5)
            tops = b.get("urls") or []
            if tops:
                out.append(f"- **热点文件**：{tops[0]['uri'][:70]}（{tops[0]['total_gb']}GB，占 {tops[0]['share_pct']}%）"
                           + ("；".join(f"{t['uri'][:40]} {t['total_gb']}GB" for t in tops[1:3]) and
                              f"；次席 {tops[1]['uri'][:40]} {tops[1]['total_gb']}GB" if len(tops) > 1 else ""))
        except Exception as e:
            out.append(f"- 热点文件归因失败: {str(e)[:80]}")
        # 压缩收益估算：可压缩类型（js/css/json/html/text）字节 × 65%
        try:
            from app.collector.credential import resolve_sls_credential
            _ak, _sk, _ = resolve_sls_credential(db)
            s, e2 = _day_bounds(snap.stat_date.date())
            rows = _sls_day_rows(_ak, _sk, *cdn_target(), f'domain: "{domain}" | select content_type, '
                               'sum(response_size) as bytes group by content_type order by bytes desc limit 10',
                               s, e2)
            savable = 0.0
            for r in rows:
                ct = str(r.get("content_type") or "").lower()
                if any(k in ct for k in ("javascript", "json", "text", "html", "css", "xml")):
                    savable += _f(r.get("bytes")) * 0.65
            if savable > GB:
                out.append(f"- **文件瘦身机会**：可压缩内容约 {savable/GB:.1f}GB/天"
                           f"（按 65% 压缩收益估），**开启 gzip/brotli 后日省约 {savable/GB:.1f}GB**"
                           f"——月省流量 {30*savable/GB:.0f}GB 量级")
        except Exception as e:
            out.append(f"- 压缩收益估算失败: {str(e)[:80]}")
    out.append("- **处置建议**：若为发版分发高峰属正常（可观察 2~3 天）；若为异常刷流量，"
               "在 CDN 控制台配置访问频率/区域封禁；若为业务真涨，评估容量与流量包消耗")
    return "\n".join(out)


def _send_report(db: Session, p: ObjectProfile, chain: str) -> None:
    """重点关注级：单独出一条中肯报告进站内信（不推机器人，决策位才打扰）。"""
    from app.services.notify import notify_inapp
    notify_inapp(db,
                 f"【观测报告】{p.label or p.object_key} 出现重点关注级变化",
                 chain, level="warning", msg_type="observe",
                 audience="active", module="object_profile")
    db.flush()


_MAG_WATCH, _MAG_NOTABLE, _MAG_MAJOR = "watch", "notable", "major"


def _classify(today: float, exp: float) -> str:
    """按相对倍率和绝对增量分为小波动、观察和重点关注；小波动可自适应为新常态。"""
    if exp <= 0:
        return _MAG_NOTABLE
    ratio = today / exp
    delta = today - exp
    # 必须同时考虑绝对量：纯比值会把小域名的小幅绝对抖动放大成重点关注。
    floor = max(2.0, exp * 0.2)
    if delta < floor:
        return _MAG_WATCH
    if delta >= 150 or (ratio >= 2.5 and delta >= 10):
        return _MAG_MAJOR
    if ratio >= 1.5:
        return _MAG_NOTABLE
    return _MAG_WATCH


class ComparisonResult(list):
    """原始变化与最终 findings 分账，防止“未评估/已抑制”落入自动恢复。"""

    def __init__(self, values=(), *, evaluated=True, raw_count=None, context_notes=None):
        super().__init__(values)
        self.evaluated = evaluated
        self.raw_count = len(self) if raw_count is None else raw_count
        self.context_notes = list(context_notes or [])


def _panoramic_slb_context(base: dict, snap: ObjectSnapshotDaily) -> dict:
    """全景 URL 请求量变化：逐后端 CPU 与同域名 SLB RT 全部为必需证据。"""
    from app.services.profile_context import daily_signal, unverified, verdict

    if not settings.PROFILE_CONTEXT_ENABLED:
        return {"verdict": "not_applicable", "signals": [], "note": "上下文裁决未启用"}
    host = (snap.layers or {}).get("host") or {}
    per_backend = host.get("backend_metrics") or {}
    if not per_backend:
        return unverified("缺少逐后端 CPU 映射/数据，不能用聚合峰值代替")
    try:
        collected_at = datetime.fromisoformat(str((snap.layers or {}).get("_context_collected_at")))
    except (TypeError, ValueError):
        collected_at = None
    now = datetime.now()
    signals = []
    for iid, values in sorted(per_backend.items()):
        signals.append(daily_signal(
            f"后端 {iid} CPU", "cpu", (base.get("host_cpu") or {}).get(iid),
            (values or {}).get("cpu_peak"), collected_at=collected_at, now=now))
    signals.append(daily_signal("后端响应时间", "rt", base.get("slb_rt"),
                                ((snap.layers or {}).get("slb") or {}).get("rt_avg_ms"),
                                collected_at=collected_at, now=now))
    return verdict(signals)


def _compare(db: Session, p: ObjectProfile, snap: ObjectSnapshotDaily) -> ComparisonResult:
    from app.services.holiday import day_kind
    base = p.baseline or {}
    findings = ComparisonResult()
    incomplete_layers = []
    # 同日型基线（D_same_day_baseline）：按实际作息取当天对应组。
    kind = day_kind(db, snap.stat_date.date())
    by_kind = base.get("layers_by_kind") or {}
    layers_base = by_kind.get(kind) or {}
    couplings_base = (base.get("couplings_by_kind") or {}).get(kind) or {}
    # 宁缺勿报：当天日型没有对应基线时禁止回落到混合样本组；样本不足不猜测。
    if not layers_base:
        return ComparisonResult(evaluated=False)
    # 节后复工首日（昨天非工作日、今天工作日）：假期低谷后的流量回升是常态，
    # 流量型层需超过工作日基线的放宽门槛才判定，不能只与昨天比较。
    prev_kind = day_kind(db, snap.stat_date.date() - timedelta(days=1))
    post_holiday = (kind == "workday" and prev_kind != "workday")
    _TRAFFIC_LAYERS = ("cdn", "slb", "app")
    # 不完整日：累计型层（cdn/slb/app/change/cost）只有部分时段数据，不能直接与
    # 整日基线比较；只在累计值已达到整日基线 1.5 倍时判定，其余等待全天数据。
    # host/db 是峰值型，不按累计口径处理。
    _CUMULATIVE = ("cdn", "slb", "app", "change", "cost")
    scale = 1.0
    today_d = date.today()
    if snap.stat_date.date() == today_d:
        elapsed = (datetime.now() - datetime.combine(today_d, datetime.min.time())).total_seconds()
        scale = max(0.15, min(1.0, elapsed / 86400))
    floors = settings.OBJECT_PROFILE_MIN_DELTA or {}

    def _sev_for(dev: float, delta: float, floor: float) -> str:
        return "critical" if (delta >= floor * 5 and dev > 6) else "warning"

    for layer, b in layers_base.items():
        today = _layer_val(snap.layers.get(layer), _primary_metric(layer))
        med, mad = b.get("median", 0), b.get("mad", 0)
        if today is None or med is None:
            # 该层有适用基线但当期值缺失=这层未评估；绝不能让 raw_count=0 触发恢复。
            incomplete_layers.append(layer)
            continue
        # 不完整日不再用线性折算（早高峰型业务会被系统性高估）：
        # 只认"半日就干完全天基线 1.5 倍"的量，其余等全天数据再判
        if scale < 1 and layer in _CUMULATIVE:
            if today < med * 1.5:
                continue
            exp, mad_s = med * 1.5, mad
        else:
            exp = med * scale if layer in _CUMULATIVE else med
            mad_s = mad * scale if layer in _CUMULATIVE else mad
        thr = max(3 * mad_s, abs(exp) * 0.35, 1e-9)
        if post_holiday and layer in _TRAFFIC_LAYERS:
            thr = max(thr, abs(med) * 1.5)   # 节后复工首日：超工作日基线 1.5 倍才可疑
        delta = today - exp
        floor = float(floors.get(layer, 0) * (scale if layer in _CUMULATIVE else 1.0))
        intraday_gate = True
        if abs(delta) > thr and abs(delta) >= max(floor, 1e-9) and intraday_gate:
            dev = abs(delta) / max(mad_s, abs(exp) * 0.1, 1e-9)
            findings.append({
                "kind": "layer", "layer": layer,
                "delta": delta,
                "magnitude": _classify(today, exp),
                "note": (f"{layer} 层今日 {today:g} vs {kind}日基线期望 {exp:g}"
                         + (f"（{kind}日基线 {med:g}；半日口径按 1.5 倍基线判）" if scale < 1 else f"（±{mad:g}）")
                         + ("【节后复工首日，已放宽口径】" if post_holiday else "")),
                "sev": _sev_for(dev, abs(delta), float(floors.get(layer, 1))),
            })

    for name, b in couplings_base.items():
        la, ma, lb, mb = _decode_coupling(name)
        va = _layer_val(snap.layers.get(la), ma)
        vb = _layer_val(snap.layers.get(lb), mb)
        med, mad = b.get("median", 0), b.get("mad", 0)
        if not va or not vb or not med:
            continue
        ratio = va / vb
        thr = max(3 * mad, abs(med) * 0.6, 1e-9)
        # 比值不受半日折算影响（同窗自消），但量太小不判（分子分母都小=抖动）
        floor_a = float(floors.get(la, 0))
        if abs(ratio - med) > thr and va >= floor_a and vb >= floor_a:
            findings.append({
                "kind": "coupling", "layer": name,
                "slb_requests_driven": (lb == "slb" and mb == "requests" and ratio < med),
                "note": (f"联动失衡 {name}：今日 {ratio:.3g} vs 常态 {med:.3g}（±{mad:.3g}）"
                         f"——{la} 相对 {lb} {'偏高（它涨了而对方没跟）' if ratio > med else '偏低（对方涨了而它没跟）'}"),
                "sev": "warning",
            })

    tf = (snap.layers.get("cdn") or {}).get("top_files") or []
    base_top = (base.get("top_files") or [])
    if tf and base_top and tf[0].get("uri") and tf[0]["uri"] != base_top[0]:
        total = sum(x.get("gb") or 0 for x in tf) or 1
        share = (tf[0].get("gb") or 0) / total
        if share >= 0.3 and (tf[0].get("gb") or 0) >= float(floors.get("cdn", 5)):
            findings.append({
                "kind": "composition", "layer": "cdn",
                "note": f"热点文件更替：top1 {base_top[0][:60]} → {tf[0]['uri'][:60]}（占当日 {share * 100:.0f}%）",
                "sev": "warning",
            })
    # 只抑制“SLB 请求量上升”及明确由它造成的比值下降；CDN 热点、应用错误、
    # 5xx/其它层等独立 findings 原样保留。请求下降不适用本抑制规则。
    findings.evaluated = not incomplete_layers
    if incomplete_layers:
        findings.context_notes.append("以下已有基线的层当期值缺失，未执行完整恢复判定："
                                      + ",".join(sorted(incomplete_layers)))
    slb_spike = any(f.get("kind") == "layer" and f.get("layer") == "slb"
                    and (f.get("delta") or 0) > 0 for f in findings)
    eligible = []
    if slb_spike:
        for f in findings:
            if f.get("kind") == "layer" and f.get("layer") == "slb" and (f.get("delta") or 0) > 0:
                eligible.append(f)
            elif f.get("kind") == "coupling" and f.get("slb_requests_driven"):
                la, _, _, _ = _decode_coupling(f["layer"])
                if not any(x.get("kind") == "layer" and x.get("layer") == la for x in findings):
                    eligible.append(f)
    if eligible:
        context_base = (base.get("context_by_kind") or {}).get(kind) or {}
        context = _panoramic_slb_context(context_base, snap)
        if context["verdict"] == "context_stable":
            suppressed = {id(f) for f in eligible}
            findings[:] = [f for f in findings if id(f) not in suppressed]
            findings.context_notes.append(context["note"])
        elif context["verdict"] == "unverified":
            for f in eligible:
                f["context_state"] = "unverified"
                f["magnitude"] = _MAG_NOTABLE   # 证据不足不能掉进“小波动自动学习”
                f["sev"] = "warning"
                f["note"] += "\n" + context["note"]
            findings.context_notes.append(context["note"])
        elif context["verdict"] == "drift":
            findings.context_notes.append(context["note"])
    findings.raw_count = len(findings) + (len(eligible) if findings.context_notes
                                          and any("上下文稳定" in n for n in findings.context_notes) else 0)
    return findings


def _decode_coupling(name: str) -> tuple:
    left, right = name.split(":")
    la, ma = {"cdn_gb": ("cdn", "traffic_gb"), "app_err": ("app", "errors")}.get(
        left, (left.split("_")[0], left.split("_")[-1]))
    lb, mb = {"slb_reqs": ("slb", "requests"), "pack_gb": ("cost", "pack_gb")}.get(
        right, (right.split("_")[0], right.split("_")[-1]))
    return la, ma, lb, mb


def _render_chain(p: ObjectProfile, snap: ObjectSnapshotDaily, findings: list) -> str:
    """因果链：接入 -> 应用 -> 主机/DB -> 变更 -> 钱，一条时间轴。"""
    L = snap.layers or {}
    order = [("cdn", "接入/CDN"), ("slb", "接入/SLB"), ("app", "应用"),
             ("host", "主机"), ("db", "数据库"), ("change", "变更"), ("cost", "钱")]
    lines = [f"架构异常分析（{p.object_type}:{p.object_key}，{snap.stat_date:%Y-%m-%d}）——多层数据对照："]
    for key, label in order:
        v = L.get(key)
        if not v:
            lines.append(f"- {label}: 未映射/无数据")
            continue
        if key == "cdn":
            tf = "；热点 " + "，".join(f"{x['uri'][:40]} {x['gb']}GB" for x in (v.get("top_files") or [])[:2]) if v.get("top_files") else ""
            lines.append(f"- {label}: 流量 {v.get('traffic_gb', 0)}GB / 请求 {v.get('requests', 0):,} / 5xx {v.get('code_5xx', 0)}{tf}")
        elif key == "slb":
            # RT 无样本必须显示“无样本”，不能拼成「None ms」或按 0 处理。
            _rt = v.get("rt_avg_ms")
            _rt_txt = f"{_rt}ms" if _rt is not None else "无样本"
            lines.append(f"- {label}: 请求 {v.get('requests', 0):,} / 5xx {v.get('code_5xx', 0)} / RT {_rt_txt}")
        elif key == "app":
            lines.append(f"- {label}: ERROR {v.get('errors', 0):,}" +
                         ("（" + "，".join(f"{x['name']} {x['errors']}" for x in (v.get('top_apps') or [])[:2]) + "）"
                          if v.get("top_apps") else ""))
        elif key == "host":
            lines.append(f"- {label}: CPU峰 {v.get('cpu_peak', '-')}% / 内存峰 {v.get('mem_peak', '-')}% / 磁盘峰 {v.get('disk_peak', '-')}%"
                         + (f"（后端 {','.join(v.get('backends') or [])}）" if v.get("backends") else ""))
        elif key == "db":
            lines.append(f"- {label}: 数据 {v.get('data_gb', '-')}GB / 磁盘 {v.get('disk_gb', '-')}GB")
        elif key == "change":
            lines.append(f"- {label}: {v.get('count', 0)} 次" +
                         ("（" + "；".join(v.get("items") or []) + "）" if v.get("items") else "（无，排除变更致因）"))
        elif key == "cost":
            parts = []
            if v.get("yuan") is not None:
                parts.append(f"账单 ¥{v['yuan']}")
            if v.get("pack_gb") is not None:
                # 域名对象在账单里没有金额维度（CDN 计费走流量包），此值=包抵扣的流量
                parts.append(f"包消耗 {v['pack_gb']}GB（流量包抵扣口径，非金额）")
            lines.append(f"- {label}: {' / '.join(parts) or '无数据'}")
    lines.append("")
    lines.append("失衡点：")
    lines += [f"- [{f['sev']}] {f['note']}" for f in findings]
    lines.append("")
    lines.append("处置方向：先看变更（有变更先定责）；接入层涨而应用/后端没涨=静态/缓存侧（治理文件/防盗刷）；"
                 "应用 ERROR 或主机/DB 同步恶化=业务侧带样本找研发；只有钱涨=计费/包的问题。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 因果链视图（工具/UI）


def know_object(db: Session, object_key: str, by: str, silence_3d: bool = False) -> dict:
    """「知道了」：确认知晓对象当前的联动失衡，不改基线。
    静默规则同行为画像：core 静默 1 天（到期变化仍在恢复提醒）；aux 勾选"静默 3 天"
    才生效（默认不静默持续通知）。静默生效时收口既有 chain_drift 事件。"""
    from app.services.profile_engine import mark_known

    p = (db.query(ObjectProfile)
         .filter(ObjectProfile.object_key == object_key)
         .order_by(ObjectProfile.id.desc()).first())
    if p is None:
        raise ValueError(f"未注册的对象 {object_key}（先跑采集/发现）")
    tier = p.tier or "core"
    info = mark_known(p, tier, silence_3d)
    closed = 0
    if p.silence_until:
        closed = (db.query(CostAnomaly)
                  .filter(CostAnomaly.type == "chain_drift",
                          CostAnomaly.target_key == f"{p.object_type}:{p.object_key}",
                          CostAnomaly.ack_status.in_(("open", "acknowledged")))
                  .update({CostAnomaly.ack_status: "resolved",
                           CostAnomaly.ack_note: (f"用户已知道（by {by}），静默至 "
                                                  f"{p.silence_until:%Y-%m-%d %H:%M}，到期变化仍在则恢复提醒")},
                          synchronize_session=False))
    db.commit()
    return {"object": object_key, "object_type": p.object_type,
            "closed_events": closed, **info}


def build_chain(db: Session, object_key: str, days: int = 7) -> dict:
    """对象的全景时间轴：近 N 天各层快照 + 基线 + 未闭环变化事件。"""
    obj = (db.query(ObjectProfile)
           .filter(ObjectProfile.object_key == object_key)
           .order_by(ObjectProfile.id.desc()).first())
    if obj is None:
        return {"error": f"未注册的对象 {object_key}（先跑采集/发现）"}
    since = datetime.combine(date.today() - timedelta(days=days), datetime.min.time())
    rows = (db.query(ObjectSnapshotDaily)
            .filter(ObjectSnapshotDaily.object_type == obj.object_type,
                    ObjectSnapshotDaily.object_key == obj.object_key,
                    ObjectSnapshotDaily.stat_date >= since)
            .order_by(ObjectSnapshotDaily.stat_date).all())
    events = (db.query(CostAnomaly)
              .filter(CostAnomaly.type == "chain_drift",
                      CostAnomaly.target_key == f"{obj.object_type}:{obj.object_key}")
              .order_by(CostAnomaly.id.desc()).limit(5).all())
    return {
        "object": {"type": obj.object_type, "key": obj.object_key, "label": obj.label,
                   "status": obj.status, "learned": obj.learned, "tier": obj.tier or "core",
                   "known_at": obj.known_at.isoformat() if obj.known_at else None,
                   "silence_until": obj.silence_until.isoformat() if obj.silence_until else None,
                   "notify_state": obj.notify_state or ""},
        "baseline": obj.baseline or {},
        "timeline": [{"date": s.stat_date.strftime("%Y-%m-%d"), "layers": s.layers} for s in rows],
        "events": [{"id": e.id, "title": e.title, "level": e.level, "status": e.ack_status,
                    "last_seen": e.last_seen.isoformat() if e.last_seen else None,
                    "chain": (e.content or "")[:3000]} for e in events],
    }


def run_object_profiles(db: Session, backfill_days: int = 0) -> dict:
    """每日任务入口：发现 -> 缺口回补 -> 采集今日 -> 学习 -> 检测。

    缺口自愈：自动补采库内最新快照日到昨天之间的缺失日；库为空时使用
    OBJECT_PROFILE_BACKFILL_DAYS 回补，用于建立初始基线。"""
    ensure_profiles(db)
    row = (db.query(ObjectSnapshotDaily.stat_date)
           .order_by(ObjectSnapshotDaily.stat_date.desc()).first())
    if backfill_days <= 0 and row is None:
        backfill_days = settings.OBJECT_PROFILE_BACKFILL_DAYS
    if backfill_days > 0 and row is None:
        backfill(db, backfill_days)
    elif row is not None:
        missing_from = row.stat_date.date() + timedelta(days=1)
        yesterday = date.today() - timedelta(days=1)
        if missing_from <= yesterday:
            collect_window(db, missing_from, yesterday)
    n = collect_day(db, date.today())
    learned = learn(db)
    found = detect(db)
    logger.info("全景画像: 采集 %d 快照 / 基线 %d 对象 / 变化事件 %d 条", n, learned, len(found))
    return {"snapshots": n, "learned": learned, "events": len(found)}
