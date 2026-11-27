"""域名登记：回填管道 + 证据聚合。

统一归集域名→证书→SLB/CDN→业务的关联证据。

回填管道 = 从既有数据源自动归集（勿造数据），upsert 进 domain_registry：
  ① link_edges.host          链路规则边（SLB 日志/云转发规则，host 可能带 :port）
  ② cdn_domain_daily.domain  CDN/DCDN 按域名流量（account_id=采集账号事实）
  ③ object_profiles          对象画像（object_type=domain）
  ④ synth_checks.target      拨测目标（URL / host:port / 域名）+ synth_results.detail 证书
  ⑤ diagnosis_sessions.host  诊断引用的目标域名
  ⑥ biz_identifiers(host)    业务线登记（业务字段来源之一）
  ⑦ assets(domain)           collector/domain.py 云同步 + 到期人工登记（注册）
  ⑧ assets(ssl_cert)         证书订单：精确域名作证据（通配符只做覆盖匹配不算域名）
字段语义（与归属登记同构，见 services/ownership.py「按字段覆盖」）：
  - 自动字段=事实：sources / cert_* / updated_at，回填每次刷新
  - 登记字段=人工/归属登记：business / vendor / account_name / note / status，
    manual_fields 标记人工固定项，回填永不覆盖；没固定时取派生值
    （归属登记 > 业务线登记 > 采集账号事实，登记优先）
  - 不提供删除：域名下线 status=disabled（停用）或仅 note

域名归一（norm_domain）：去 scheme/路径/端口/userinfo、小写、去尾点；
非域名形态（IPv4/IPv6/无点伪 host 如 default）一律不进登记表（不造数据）。
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models.asset import Asset
from app.models.business import BizIdentifier, Business
from app.models.cdn_domain_daily import CdnDomainDaily
from app.models.diagnosis import DiagnosisSession
from app.models.domain_registry import DomainRegistry
from app.models.link_edge import LinkEdge
from app.models.object_profile import ObjectProfile
from app.models.synth import SynthCheck, SynthResult
from app.services.cert_fact import cert_covers, cert_level
from app.services.ownership import hints as ownership_hints
from app.services.ownership import load_rules, norm_key, resolve
from app.services.tenant import account_index

logger = logging.getLogger("qianshu.domain_registry")

# 来源标记（domain_registry.sources）→ 展示名
SOURCE_LABELS = {
    "link": "链路/SLB",
    "cdn": "CDN 流量",
    "profile": "对象画像",
    "synth": "拨测",
    "diagnosis": "诊断",
    "biz": "业务线登记",
    "registrar": "域名注册",
    "cert": "证书匹配",
}

_EPOCH = datetime(1970, 1, 1)
_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


# ---------------- 域名归一 ----------------

def norm_domain(raw) -> str:
    """域名归一：去 scheme/路径/端口/userinfo、小写、去尾点。

    非域名形态返回 ''（IPv4/IPv6 字面量、无点伪 host 如 SLB 的 default）——
    这些是 IP/伪主机，不进域名登记表（不造数据）。
    """
    v = (raw or "").strip()
    if not v:
        return ""
    if "://" in v:
        v = v.split("://", 1)[1]
    v = v.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if "@" in v:
        v = v.rsplit("@", 1)[1]
    if v.startswith("["):          # IPv6 字面量，不是域名
        return ""
    head, sep, tail = v.partition(":")
    if sep and (tail.isdigit() or tail == ""):
        v = head
    v = norm_key("domain", v)      # 消费归属层归一（小写、去尾点）
    if not v or "." not in v:
        return ""
    if _IPV4_RE.match(v) or not _DOMAIN_RE.match(v):
        return ""
    return v


# cert_covers / cert_level 收口 cert 层单一事实（app.services.cert_fact）：
# 同证书归一、实测为准/订单为辅、warn_days 分级全在该模块；此处保留名字兼容调用方。


# ---------------- 六源归集 ----------------

def biz_domain_map(db: Session) -> dict:
    """业务线登记（biz_identifiers host）→ {归一域名: [业务名, ...]}（只读）。"""
    out: dict = {}
    try:
        rows = (db.query(BizIdentifier, Business)
                .join(Business, BizIdentifier.business_id == Business.id)
                .filter(BizIdentifier.ident_type == "host",
                        Business.status == "confirmed").all())
    except Exception:   # 表缺失/未迁移：静默降级，不炸回填
        logger.debug("业务线登记加载失败（忽略）", exc_info=True)
        return out
    for ident, biz in rows:
        d = norm_domain(ident.ident_value)
        if not d:
            continue
        names = out.setdefault(d, [])
        if biz.name not in names:
            names.append(biz.name)
    return out


def collect(db: Session) -> dict:
    """六源(+注册)证据归集：{归一域名: {sources:set, raw:set, ...事实}}。"""
    idx: dict = {}

    def touch(domain: str, tag: str, raw: str = "") -> dict:
        e = idx.setdefault(domain, {"sources": set(), "raw": set()})
        e["sources"].add(tag)
        if raw:
            e["raw"].add(raw)
        return e

    # ① 链路（SLB 日志/云规则 host，可能带 :port / 大小写混杂）
    for (host,) in db.query(LinkEdge.host).distinct():
        d = norm_domain(host)
        if d:
            touch(d, "link", host)

    # ② CDN 按域名流量（account_id=采集账号事实）
    for dom, prod, acc in (
            db.query(CdnDomainDaily.domain, func.max(CdnDomainDaily.product),
                     func.max(CdnDomainDaily.account_id))
            .group_by(CdnDomainDaily.domain).all()):
        d = norm_domain(dom)
        if d:
            e = touch(d, "cdn", dom)
            e["cdn_account"] = acc or 0
            e["cdn_product"] = prod or ""

    # ③ 对象画像（域名类）
    for (key,) in (db.query(ObjectProfile.object_key)
                   .filter(ObjectProfile.object_type == "domain").distinct()):
        d = norm_domain(key)
        if d:
            touch(d, "profile", key)

    # ④ 拨测目标（URL / host:port / 域名）
    for (target,) in db.query(SynthCheck.target).distinct():
        d = norm_domain(target)
        if d:
            touch(d, "synth", target)

    # ⑤ 诊断引用
    for (host,) in (db.query(DiagnosisSession.host)
                    .filter(DiagnosisSession.host != "").distinct()):
        d = norm_domain(host)
        if d:
            touch(d, "diagnosis", host)

    # ⑥ 业务线登记
    for d, names in biz_domain_map(db).items():
        touch(d, "biz")
        idx[d]["biz"] = names

    # ⑦ 注册/人工登记（collector/domain.py 云同步 + 到期人工登记 Manual）
    for a in db.query(Asset).filter(Asset.product == "domain", Asset.status != "Deleted"):
        d = norm_domain(a.instance_id or a.name)
        if d:
            e = touch(d, "registrar", a.instance_id)
            if a.status != "Manual":
                # Manual 人工登记行的 account_id=1 是列默认值不是归属证据（不猜）；
                # 云同步行的 account_id 才是采集账号事实
                e["registrar_account"] = a.account_id or 0

    # ⑧ 证书订单的精确域名（非通配符）也是域名证据：到期项点域名可进详情。
    #    通配符名（*.demo.com）不是具体域名，norm_domain 自动剔除
    for a in db.query(Asset).filter(Asset.product == "ssl_cert", Asset.status != "Deleted"):
        d = norm_domain(a.name)
        if d:
            touch(d, "cert", a.name)
    return idx


def cert_index(db: Session, domains: set) -> dict:
    """证书证据：{域名: {"facts": 优胜事实, "evidence": [证据...], "fact": CertFact}}。

    cert 层统一使用 app.services.cert_fact：同证书归一（序列号/域名+签发者），
    实测为准（tls_probe）、订单为辅（cas_order），两来源
    到期日差 >7 天标注 source_conflict（不静默取一）；序列号只有证据真实带出才填。
    """
    from app.services import cert_fact

    out: dict = {}
    for d, f in cert_fact.resolve_for_domain(db, set(domains)).items():
        out[d] = {"facts": f.facts_dict(), "evidence": [e.as_dict() for e in f.evidence],
                  "fact": f}
    return out


# ---------------- 派生字段（登记字段的自动值） ----------------

def _resolve_domain(rules: list, domain: str, raws) -> Optional[dict]:
    """归属登记解析（消费 services/ownership.py）：归一键优先，
    历史带端口登记（ownership 归一不去端口）按原形态兜底。"""
    hit = resolve(rules, domain=domain)
    if hit:
        return hit
    for raw in sorted(raws or ()):
        hit = resolve(rules, domain=raw)
        if hit:
            return hit
    return None


def derived_fields(rules: list, acc_map: dict, domain: str, ev: dict) -> dict:
    """登记字段的派生值（人工没固定时回填这些）：
    归属登记 > 业务线登记 > 采集账号事实（登记优先，与拓扑展示口径一致）。"""
    out = {"business": "", "vendor": "", "account_name": ""}
    hit = _resolve_domain(rules, domain, ev.get("raw"))
    if hit:
        out["business"] = hit.get("business") or ""
        acc = acc_map.get(hit.get("account_id") or 0) if hit.get("account_id") else None
        if acc:
            out["vendor"] = acc.get("vendor") or ""
            out["account_name"] = acc.get("name") or ""
        else:
            out["vendor"] = hit.get("vendor") or ""
            out["account_name"] = hit.get("account_name") or ""
    if not out["business"]:
        out["business"] = "、".join(ev.get("biz") or [])
    if not out["account_name"]:
        acc = (acc_map.get(ev.get("cdn_account") or 0)
               or acc_map.get(ev.get("registrar_account") or 0) or {})
        out["vendor"] = out["vendor"] or (acc.get("vendor") or "")
        out["account_name"] = out["account_name"] or (acc.get("name") or "")
    return out


def _manual_set(row: DomainRegistry) -> set:
    return {f for f in (row.manual_fields or "").split(",") if f}


# ---------------- 回填管道（幂等 upsert） ----------------

def backfill(db: Session, only_domain: str = None) -> dict:
    """六源归集 → domain_registry 幂等 upsert。

    自动字段=事实每次刷新；登记字段只在没被 manual_fields 固定时回填派生值
    （人工字段不被采集覆盖）。无任何证据的域名不建行（不造数据）。
    """
    idx = collect(db)
    if only_domain:
        idx = {d: e for d, e in idx.items() if d == only_domain}
    certs = cert_index(db, set(idx))
    rules = load_rules(db)
    acc_map = account_index(db)
    created = updated = 0
    now = datetime.now()
    for d in sorted(idx):
        ev = idx[d]
        facts = (certs.get(d) or {}).get("facts") or {}
        row = db.get(DomainRegistry, d)
        if row is None:
            row = DomainRegistry(domain=d)
            db.add(row)
            created += 1
        else:
            updated += 1
        srcs = set(ev["sources"])
        if certs.get(d):
            srcs.add("cert")
        row.sources = ",".join(sorted(srcs))
        row.cert_serial = facts.get("serial") or ""
        row.cert_not_after = facts.get("not_after")
        row.cert_source = facts.get("source") or ""
        manual = _manual_set(row)
        derived = derived_fields(rules, acc_map, d, ev)
        for f in ("business", "vendor", "account_name"):
            if f not in manual:
                setattr(row, f, derived.get(f) or "")
        row.updated_at = now
    db.commit()
    logger.info("域名登记回填: domains=%d created=%d updated=%d", len(idx), created, updated)
    return {"domains": len(idx), "created": created, "updated": updated}


# ---------------- 清单 / 详情 ----------------

def _slb_agg(db: Session) -> dict:
    """按归一域名聚合链路（关联 SLB）：链/规则边数/24h 请求。"""
    out: dict = {}
    rows = (db.query(LinkEdge.host, LinkEdge.chain, func.count(LinkEdge.id),
                     func.sum(LinkEdge.req_count))
            .group_by(LinkEdge.host, LinkEdge.chain).all())
    for host, chain, cnt, reqs in rows:
        d = norm_domain(host)
        if not d:
            continue
        e = out.setdefault(d, {"chains": [], "edge_count": 0, "req_24h": 0})
        if chain and chain not in e["chains"]:
            e["chains"].append(chain)
        e["edge_count"] += int(cnt or 0)
        e["req_24h"] += int(reqs or 0)
    for e in out.values():
        e["chains"].sort()
    return out


def _cdn_agg(db: Session) -> dict:
    """按归一域名聚合 CDN 近 30 天流量。"""
    out: dict = {}
    rows = (db.query(CdnDomainDaily.domain, func.max(CdnDomainDaily.product),
                     func.max(CdnDomainDaily.stat_date), func.sum(CdnDomainDaily.traffic_bytes))
            .filter(CdnDomainDaily.stat_date >= date.today() - timedelta(days=30))
            .group_by(CdnDomainDaily.domain).all())
    for dom, prod, last, traffic in rows:
        d = norm_domain(dom)
        if not d:
            continue
        out[d] = {
            "product": prod or "",
            "traffic_30d_gb": round((traffic or 0) / 1024 ** 3, 2),
            "last_date": last.isoformat() if last else None,
        }
    return out


def _iso(dt) -> Optional[str]:
    return dt.isoformat(timespec="seconds") if dt else None


def registry_list(db: Session, *, q: str = "", status: str = "", level: str = "",
                  source: str = "") -> dict:
    """登记清单：证书到期（升序，无证书沉底）+ 关联 SLB/CDN/业务/归属。

    证书列走 cert 层统一解析：到期日/分级/来源标注与到期总账同一事实，
    warn_days 消费拨测任务 cert_warn_days；两来源不一致带出标注（不静默取一）。"""
    rows = db.query(DomainRegistry).all()
    slb, cdn = _slb_agg(db), _cdn_agg(db)
    certs = cert_index(db, {r.domain for r in rows})
    today = date.today()
    items: list = []
    for r in rows:
        cf = (certs.get(r.domain) or {}).get("fact")
        not_after = cf.not_after if (cf and cf.not_after) else r.cert_not_after
        days = (not_after.date() - today).days if not_after else None
        srcs = [s for s in (r.sources or "").split(",") if s]
        it = {
            "domain": r.domain, "status": r.status, "business": r.business,
            "vendor": r.vendor, "account_name": r.account_name, "note": r.note,
            "sources": srcs,
            "source_labels": [SOURCE_LABELS.get(s, s) for s in srcs],
            "manual_fields": [f for f in (r.manual_fields or "").split(",") if f],
            "cert_serial": r.cert_serial, "cert_source": r.cert_source,
            "cert_not_after": _iso(not_after),
            "cert_days_left": days,
            "cert_level": cert_level(days, cf.warn_days if cf else 30),
            "cert_warn_days": cf.warn_days if cf else None,
            "cert_source_conflict": bool(cf.source_conflict) if cf else False,
            "cert_serial_mismatch": bool(cf.serial_mismatch) if cf else False,
            "cert_diff_days": cf.diff_days if cf else None,
            "cert_probe_not_after": _iso(cf.probe.not_after) if (cf and cf.probe) else None,
            "cert_order_not_after": _iso(cf.order.not_after) if (cf and cf.order) else None,
            "slb": slb.get(r.domain), "cdn": cdn.get(r.domain),
            "created_at": _iso(r.created_at), "updated_at": _iso(r.updated_at),
        }
        if q:
            blob = f"{r.domain} {r.business} {r.vendor} {r.account_name} {r.note}".lower()
            if q.lower() not in blob:
                continue
        if status and r.status != status:
            continue
        if level and it["cert_level"] != level:
            continue
        if source and source not in srcs:
            continue
        items.append(it)
    # 排序：证书到期升序（无证书沉底），同级按域名
    items.sort(key=lambda x: (x["cert_days_left"] if x["cert_days_left"] is not None else 10 ** 6,
                              x["domain"]))
    return {"total": len(items), "items": items, "source_labels": SOURCE_LABELS}


def detail(db: Session, domain: str) -> Optional[dict]:
    """域名详情：聚合六处证据（链路/CDN/画像/拨测/诊断/业务线 + 证书 + 注册资产）。

    进详情先做单域名自愈回填（无证据=不建行，返回 None→404）。
    """
    d = norm_domain(domain)
    if not d:
        raise ValueError("非法域名")
    backfill(db, only_domain=d)
    row = db.get(DomainRegistry, d)
    if row is None:
        return None
    today = date.today()
    days = (row.cert_not_after.date() - today).days if row.cert_not_after else None
    cert = cert_index(db, {d}).get(d) or {}
    fact = cert.get("fact")
    if fact is not None:
        cert_block = fact.as_dict()
    else:
        cert_block = {"serial": "", "source": "", "not_after": _iso(row.cert_not_after),
                      "days_left": days, "level": cert_level(days), "evidence": [],
                      "warn_days": 30, "source_conflict": False, "serial_mismatch": False,
                      "diff_days": None, "probe_not_after": None, "order_not_after": None,
                      "below_warn": days is not None and days <= 30}

    # ① 链路/SLB 规则边（host 带端口/大小写的历史形态也归进来）
    edges = (db.query(LinkEdge)
             .filter(or_(func.lower(LinkEdge.host) == d,
                         func.lower(LinkEdge.host).like(f"{d}:%")))
             .order_by(LinkEdge.req_count.desc()).limit(50).all())
    slb_rows = [{
        "chain": e.chain, "host": e.host, "path_pattern": e.path_pattern,
        "rule_name": e.rule_name, "rule_source": e.rule_source,
        "upstream_addr": e.upstream_addr, "ecs_name": e.ecs_name, "app_name": e.app_name,
        "req_count": e.req_count, "cnt_4xx": e.cnt_4xx, "cnt_5xx": e.cnt_5xx,
        "rt_avg_ms": e.rt_avg_ms, "status": e.status,
    } for e in edges]
    slb = {"summary": _slb_agg(db).get(d) or {"chains": [], "edge_count": 0, "req_24h": 0},
           "edges": slb_rows}

    # ② CDN 流量
    cdn_rows = (db.query(CdnDomainDaily)
                .filter(func.lower(CdnDomainDaily.domain) == d)
                .order_by(CdnDomainDaily.stat_date.desc()).limit(30).all())
    cdn = {"summary": _cdn_agg(db).get(d),
           "daily": [{"stat_date": c.stat_date.isoformat(), "product": c.product,
                      "traffic_gb": round((c.traffic_bytes or 0) / 1024 ** 3, 3),
                      "account_id": c.account_id} for c in cdn_rows]}

    # ③ 拨测（含证书实测文本）
    checks = [c for c in db.query(SynthCheck).all() if norm_domain(c.target) == d]
    synth_checks = []
    for c in checks:
        results = (db.query(SynthResult).filter(SynthResult.check_id == c.id)
                   .order_by(SynthResult.checked_at.desc()).limit(3).all())
        synth_checks.append({
            "id": c.id, "name": c.name, "check_type": c.check_type, "target": c.target,
            "enabled": c.enabled, "last_ok": c.last_ok, "last_latency_ms": c.last_latency_ms,
            "consecutive_fail": c.consecutive_fail,
            "results": [{"checked_at": _iso(r.checked_at), "ok": r.ok,
                         "status_code": r.status_code, "latency_ms": r.latency_ms,
                         "detail": r.detail} for r in results],
        })

    # ④ 对象画像（域名类）
    profiles = []
    for p in (db.query(ObjectProfile).filter(ObjectProfile.object_type == "domain").all()):
        if norm_domain(p.object_key) != d:
            continue
        profiles.append({
            "object_key": p.object_key, "label": p.label, "tier": p.tier,
            "status": p.status, "learned": p.learned,
            "last_drift_note": p.last_drift_note, "known_at": _iso(p.known_at),
            "silence_until": _iso(p.silence_until), "notify_state": p.notify_state,
        })

    # ⑤ 诊断引用
    sessions = []
    for s in (db.query(DiagnosisSession).filter(DiagnosisSession.host != "")
              .order_by(DiagnosisSession.created_at.desc()).limit(500).all()):
        if norm_domain(s.host) != d:
            continue
        sessions.append({
            "id": s.id, "created_at": _iso(s.created_at), "question": s.question[:120],
            "scenario": s.scenario, "trigger": s.trigger, "status": s.status,
            "level": s.level,
        })
        if len(sessions) >= 10:
            break

    # ⑥ 业务线登记 + 归属登记 + 识别线索（只读建议）
    biz_rows = []
    for ident, biz in (db.query(BizIdentifier, Business)
                       .join(Business, BizIdentifier.business_id == Business.id)
                       .filter(BizIdentifier.ident_type == "host",
                               Business.status == "confirmed").all()):
        if norm_domain(ident.ident_value) != d:
            continue
        biz_rows.append({"business": biz.name, "ident_value": ident.ident_value,
                         "note": ident.note, "created_by": ident.created_by,
                         "created_at": _iso(ident.created_at)})
    rules = load_rules(db)
    ev = collect(db).get(d) or {"raw": set(), "biz": []}
    hit = _resolve_domain(rules, d, ev.get("raw"))

    # 注册/证书资产（collector/domain.py 云同步 + 到期人工登记）
    dom_assets = [{"instance_id": a.instance_id, "name": a.name, "status": a.status,
                   "expire_time": _iso(a.expire_time), "account_id": a.account_id}
                  for a in db.query(Asset).filter(
                      Asset.product == "domain", Asset.status != "Deleted").all()
                  if norm_domain(a.instance_id or a.name) == d]
    # 资产证书行走统一解析：每行标注解析后的到期日/来源，与订单日期并列不静默
    cert_rows = [a for a in db.query(Asset).filter(
        Asset.product == "ssl_cert", Asset.status != "Deleted").all()
        if cert_covers(a.name, d)]
    from app.services.cert_fact import resolve_for_assets

    resolved_assets = resolve_for_assets(db, cert_rows)
    cert_assets = []
    for a in cert_rows:
        f = resolved_assets.get(a.instance_id)
        cert_assets.append({
            "instance_id": a.instance_id, "cert_name": a.name, "status": a.status,
            "expire_time": _iso(a.expire_time), "extra": a.extra,
            "resolved_source": f.source if f else "",
            "resolved_not_after": _iso(f.not_after) if f else None,
            "source_conflict": bool(f.source_conflict) if f else False,
            "serial_mismatch": bool(f.serial_mismatch) if f else False,
        })

    return {
        "domain": d,
        "registry": {
            "domain": row.domain, "status": row.status, "business": row.business,
            "vendor": row.vendor, "account_name": row.account_name, "note": row.note,
            "sources": [s for s in (row.sources or "").split(",") if s],
            "manual_fields": [f for f in (row.manual_fields or "").split(",") if f],
            "cert_serial": row.cert_serial, "cert_not_after": _iso(row.cert_not_after),
            "cert_source": row.cert_source,
            "created_at": _iso(row.created_at), "updated_at": _iso(row.updated_at),
        },
        "cert": cert_block,
        "slb": slb, "cdn": cdn, "synth": {"checks": synth_checks},
        "profile": {"objects": profiles},
        "diagnosis": {"sessions": sessions},
        "business": {
            "identifiers": biz_rows,
            "ownership": hit,
            "hints": ownership_hints(db, "domain", d),
            "derived": derived_fields(rules, account_index(db), d, ev),
        },
        "assets": {"domain": dom_assets, "certs": cert_assets},
        "source_labels": SOURCE_LABELS,
    }
