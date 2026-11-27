"""证书单一事实：同证书多来源统一解析，cert 层唯一取数口。

同一张证书有两套采集——TLS 拨测实测（synth_results.detail「证书 N 天后到期」）
与 CAS 证书订单（assets ssl_cert，collector/domain.py），到期日天然可不同。
本模块统一消费口径，避免到期总账与域名登记各自取源而展示两个日期：

- 同证书归一（norm_serial / cert_identity / same_cert）：序列号优先（去分隔符大写）；
  无序列号用 域名/证书名+签发者（通配符按一级标签覆盖；签发者两边都有且不同才算不同，
  缺省不猜）
- 优胜：实测为准（tls_probe，域名实际出示的证书），订单为辅（cas_order，实测缺才用）；
  两来源都标注 source: tls_probe / cas_order
- 差异可见（不静默取一）：两来源到期日差 > DIFF_THRESHOLD_DAYS(7) 天 →
  source_conflict + diff_days（详情并列展示）；两边序列号都有且不同 → serial_mismatch
  （实测出示与订单不是同一张证书，订单行保留订单日期不冒领实测日期）
- 到期分级（cert_level）：expired / critical(≤7 天，紧急档固定) / warning(≤warn_days) /
  ok / none。warn_days=胜出实测所在拨测任务的 synth_checks.cert_warn_days
  （死配置接线：预警档阈值随任务配置，默认 30=原 30/7 口径不变），订单兜底时=默认 30

消费方：services/expiry.py（到期总账）、services/domain_registry.py（域名登记/详情）、
api/v1/assets.py（资产证书行）、services/dashboard.py（决策清单到期段）。
红线：synth 采集本体（services/synth.py）不改——本模块只做读侧解析。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.models.asset import Asset
from app.models.synth import SynthCheck, SynthResult

logger = logging.getLogger("qianshu.cert_fact")

SOURCE_TLS = "tls_probe"      # TLS 拨测实测（域名实际出示的证书）
SOURCE_CAS = "cas_order"      # CAS 证书订单（assets ssl_cert）
SOURCE_LABELS = {
    SOURCE_TLS: "TLS 拨测实测",
    SOURCE_CAS: "CAS 证书订单",
}

DIFF_THRESHOLD_DAYS = 7       # 两来源到期日差超过此值=来源不一致（并列展示）
DEFAULT_WARN_DAYS = 30        # cert_warn_days 缺省（=原 30/7 分级的 warning 档）
CRITICAL_DAYS = 7             # 紧急档固定阈值（不受任务配置影响）

_EPOCH = datetime(1970, 1, 1)
_CERT_DAYS_RE = re.compile(r"证书\s*(-?\d+)\s*天后到期")
_CERT_SERIAL_RE = re.compile(r"(?:序列号|serial)\s*[:：]?\s*([0-9A-Fa-f:-]{4,})", re.I)


# ---------------- 归一（同证书判定） ----------------

def norm_serial(raw) -> str:
    """序列号归一：去分隔符（: - 空白）转大写；空/非法返回 ''。"""
    v = re.sub(r"[:\-\s]", "", str(raw or "")).upper()
    return v if re.fullmatch(r"[0-9A-Z]{4,}", v or "") else ""


def cert_identity(serial: str = "", name: str = "", issuer: str = "") -> str:
    """同证书归一键：序列号优先；无序列号用 域名/证书名+签发者（小写、去尾点）。"""
    s = norm_serial(serial)
    if s:
        return f"serial:{s}"
    n = (name or "").strip().lower().rstrip(".")
    i = (issuer or "").strip().lower()
    return f"name:{n}|issuer:{i}"


def cert_covers(cert_name: str, domain: str) -> bool:
    """证书域名覆盖：精确匹配或通配符（*.demo.com 覆盖 www.demo.com，
    不覆盖 demo.com 本身 / a.b.demo.com 多级——通配符只管一级标签）。"""
    c = (cert_name or "").strip().lower().rstrip(".")
    d = (domain or "").strip().lower().rstrip(".")
    if not c or not d:
        return False
    if c == d:
        return True
    if c.startswith("*."):
        suffix = c[1:]                      # ".demo.com"
        return d.endswith(suffix) and d.count(".") == c.count(".")
    return False


def same_cert(a: "CertEvidence", b: "CertEvidence") -> bool:
    """同证书判定：序列号都有→必须相等；有序列号缺失→域名/证书名覆盖 + 签发者不冲突。"""
    sa, sb = norm_serial(a.serial), norm_serial(b.serial)
    if sa and sb:
        return sa == sb
    ia, ib = (a.issuer or "").strip().lower(), (b.issuer or "").strip().lower()
    if ia and ib and ia != ib:
        return False
    return (cert_covers(a.cert_name, b.domain) or cert_covers(b.cert_name, a.domain)
            or (a.cert_name or "").strip().lower() == (b.cert_name or "").strip().lower())


def cert_level(days: Optional[int], warn_days: int = DEFAULT_WARN_DAYS) -> str:
    """到期分级：expired / critical(≤7 固定) / warning(≤warn_days) / ok / none。

    warn_days 消费 synth_checks.cert_warn_days（胜出实测所在任务的配置，默认 30）；
    warn_days < 7 时预警档被紧急档吸收（≤7 一律 critical）。"""
    if days is None:
        return "none"
    if days < 0:
        return "expired"
    if days <= CRITICAL_DAYS:
        return "critical"
    try:
        warn = int(warn_days)
    except (TypeError, ValueError):
        warn = DEFAULT_WARN_DAYS
    if warn <= 0:
        warn = DEFAULT_WARN_DAYS
    if days <= warn:
        return "warning"
    return "ok"


def clamp_warn_days(v) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return DEFAULT_WARN_DAYS
    return max(1, min(365, n)) if n > 0 else DEFAULT_WARN_DAYS


def parse_probe_cert(detail: str) -> tuple:
    """拨测 detail 文本解证书：("证书 N 天后到期" → days；"序列号 X" → serial)。

    读侧解析（synth 采集本体不改）；文本形态以 services/synth.py 实际输出为准。"""
    m = _CERT_DAYS_RE.search(detail or "")
    if not m:
        return None, ""
    ms = _CERT_SERIAL_RE.search(detail or "")
    return int(m.group(1)), (ms.group(1) if ms else "")


# ---------------- 证据 ----------------

@dataclass
class CertEvidence:
    source: str                      # tls_probe / cas_order
    ref: str                         # 拨测为 synth#<check_id> <YYYY-MM-DD>，订单为 instance_id
    cert_name: str                   # 订单证书名（域名/通配符）；实测=拨测域名
    domain: str                      # 归一域名（订单通配符名无具体域名= ''）
    serial: str = ""
    issuer: str = ""
    not_after: Optional[datetime] = None
    warn_days: Optional[int] = None  # 实测：所在拨测任务的 cert_warn_days
    checked_at: Optional[datetime] = None
    status: str = ""
    note: str = ""

    @property
    def identity(self) -> str:
        return cert_identity(self.serial, self.cert_name, self.issuer)

    def as_dict(self) -> dict:
        """API 序列化（沿用域名详情 evidence 行既有键位）。"""
        return {
            "source": self.source, "ref": self.ref, "cert_name": self.cert_name,
            "serial": self.serial, "issuer": self.issuer,
            "not_after": self.not_after.isoformat(timespec="seconds") if self.not_after else None,
            "status": self.status, "note": self.note,
            "warn_days": self.warn_days,
        }


def order_evidence(a: Asset) -> CertEvidence:
    """assets ssl_cert 行 → 订单证据（serial/root_brand 只取 extra 里真实带出的）。"""
    try:
        extra = json.loads(a.extra or "{}")
    except ValueError:
        extra = {}
    name = a.name or ""
    return CertEvidence(
        source=SOURCE_CAS, ref=a.instance_id or "", cert_name=name,
        domain=name if "." in name and not name.startswith("*.") else "",
        serial=str(extra.get("serial") or extra.get("cert_serial") or ""),
        issuer=str(extra.get("root_brand") or ""),
        not_after=a.expire_time, status=a.status or "",
        note=f"证书订单（{a.status or '未知'}）{extra.get('root_brand', '')}".strip(),
    )


def probe_evidence(db: Session) -> list:
    """拨测实测证据（按 checked_at 降序，同域名第一条=最新实测）。"""
    from app.services.domain_registry import norm_domain   # 读侧复用域名归一

    checks = {c.id: (norm_domain(c.target), clamp_warn_days(c.cert_warn_days))
              for c in db.query(SynthCheck).all()}
    rows = (db.query(SynthResult)
            .filter(SynthResult.detail.like("%天后到期%"))
            .order_by(SynthResult.checked_at.desc()).limit(500).all())
    out: list = []
    for r in rows:
        d, warn = checks.get(r.check_id, ("", DEFAULT_WARN_DAYS))
        if not d:
            continue
        days, serial = parse_probe_cert(r.detail or "")
        if days is None:
            continue
        not_after = datetime.combine(r.checked_at.date() + timedelta(days=days),
                                     datetime.min.time())
        out.append(CertEvidence(
            source=SOURCE_TLS, ref=f"synth#{r.check_id} {r.checked_at.date().isoformat()}",
            cert_name=d, domain=d, serial=serial,
            not_after=not_after, warn_days=warn, checked_at=r.checked_at,
            note=f"拨测实测：证书 {days} 天后到期",
        ))
    return out


def _order_rows(db: Session) -> list:
    return [order_evidence(a) for a in
            db.query(Asset).filter(Asset.product == "ssl_cert", Asset.status != "Deleted").all()]


def _best_order(orders: list, *, serial: str = "", domain: str = "") -> Optional[CertEvidence]:
    """实测配对的订单：序列号命中优先，否则域名覆盖里到期最新的一张。"""
    s = norm_serial(serial)
    if s:
        hit = [o for o in orders if norm_serial(o.serial) == s]
        if hit:
            return max(hit, key=lambda o: o.not_after or _EPOCH)
    if domain:
        hit = [o for o in orders if cert_covers(o.cert_name, domain)]
        if hit:
            return max(hit, key=lambda o: o.not_after or _EPOCH)
    return None


# ---------------- 解析结果 ----------------

@dataclass
class CertFact:
    """同证书统一解析结果：实测为准、订单为辅，两来源都留痕。"""
    source: str = ""
    not_after: Optional[datetime] = None
    serial: str = ""
    warn_days: int = DEFAULT_WARN_DAYS
    probe: Optional[CertEvidence] = None
    order: Optional[CertEvidence] = None
    evidence: list = field(default_factory=list)
    diff_days: Optional[int] = None
    source_conflict: bool = False      # 两来源到期日差 > 7 天
    serial_mismatch: bool = False      # 两边序列号都有且不同（不是同一张证书）

    @property
    def days_left(self) -> Optional[int]:
        return (self.not_after.date() - date.today()).days if self.not_after else None

    @property
    def level(self) -> str:
        return cert_level(self.days_left, self.warn_days)

    def facts_dict(self) -> dict:
        """domain_registry.cert_index 既有 facts 键位（serial/not_after/source）+ 新增标注。"""
        return {
            "serial": self.serial, "not_after": self.not_after, "source": self.source,
            "warn_days": self.warn_days,
            "source_conflict": self.source_conflict, "serial_mismatch": self.serial_mismatch,
            "diff_days": self.diff_days,
        }

    def as_dict(self) -> dict:
        """域名详情 cert 块（并列展示两来源日期，不静默取一）。"""
        d = self.facts_dict()
        d.update({
            "not_after": self.not_after.isoformat(timespec="seconds") if self.not_after else None,
            "days_left": self.days_left, "level": self.level,
            "source_label": SOURCE_LABELS.get(self.source, self.source),
            "probe_not_after": self.probe.not_after.isoformat(timespec="seconds")
            if self.probe and self.probe.not_after else None,
            "order_not_after": self.order.not_after.isoformat(timespec="seconds")
            if self.order and self.order.not_after else None,
            "below_warn": self.days_left is not None and self.days_left <= self.warn_days,
            "evidence": [e.as_dict() for e in self.evidence],
        })
        return d


def _diff(probe: Optional[CertEvidence], order: Optional[CertEvidence]):
    if not probe or not order or not probe.not_after or not order.not_after:
        return None, False
    diff = abs((probe.not_after.date() - order.not_after.date()).days)
    return diff, diff > DIFF_THRESHOLD_DAYS


def _fact(probe: Optional[CertEvidence], order: Optional[CertEvidence],
          *, prefer: str) -> CertFact:
    """两来源归并（实测为准、订单为辅，两来源都留痕）。

    prefer='probe'（域名事实）：实测出示的就是域名在用的证——即便与订单序列号不同，
      日期也取实测（serial_mismatch 只标注不改口径）；
    prefer='same_cert'（订单行事实）：只有同证书实测才替换订单日期，
      序列号不同=另一张证，订单行保留订单日期不冒领。"""
    serial_mismatch = bool(probe and order and norm_serial(probe.serial)
                           and norm_serial(order.serial)
                           and norm_serial(probe.serial) != norm_serial(order.serial))
    diff_days, conflict = _diff(probe, order)
    if probe and (prefer == "probe" or not serial_mismatch):
        src, not_after = SOURCE_TLS, probe.not_after
        serial = probe.serial or ""     # 序列号只填实测真实带出的（不从订单猜）
    elif order:
        src, not_after = SOURCE_CAS, order.not_after
        serial = order.serial or ""
    else:
        src, not_after = SOURCE_TLS, probe.not_after
        serial = probe.serial or ""
    warn = clamp_warn_days(probe.warn_days) if (probe and src == SOURCE_TLS) else DEFAULT_WARN_DAYS
    ev = [e for e in (probe, order) if e]
    return CertFact(source=src, not_after=not_after, serial=serial, warn_days=warn,
                    probe=probe, order=order, evidence=ev,
                    diff_days=diff_days, source_conflict=conflict,
                    serial_mismatch=serial_mismatch)


# ---------------- 消费入口 ----------------

def resolve_for_domain(db: Session, domains: set) -> dict:
    """域名维度解析：{归一域名: CertFact}（域名登记回填/清单/详情用）。

    实测（该域名最新一条）为准；订单取同序列号或覆盖该域名里到期最新的一张。"""
    probes = probe_evidence(db)
    orders = _order_rows(db)
    latest_probe: dict = {}
    for p in probes:                      # 已按 checked_at 降序，首见=最新
        latest_probe.setdefault(p.domain, p)
    out: dict = {}
    for d in domains:
        probe = latest_probe.get(d)
        order = _best_order(orders, serial=probe.serial if probe else "", domain=d)
        if probe is None and order is None:
            continue
        out[d] = _fact(probe, order, prefer="probe")
    return out


def resolve_for_assets(db: Session, assets: list) -> dict:
    """订单资产维度解析：{instance_id: CertFact}（到期总账/资产证书行用）。

    实测只配对「同证书」的订单（序列号命中，或无序列号时覆盖域名中到期最新的一张
    =同域名多张旧证只认现行那张）；配不上的订单保留订单日期（cas_order）不冒领。"""
    probes = probe_evidence(db)
    orders = [order_evidence(a) for a in assets]
    paired: dict = {}
    for p in probes:                      # 1 实测 → 1 订单（该域名/该证书的现行订单）
        o = _best_order(orders, serial=p.serial, domain=p.domain)
        if o is not None:
            paired.setdefault(o.ref, []).append(p)
    out: dict = {}
    for a in assets:
        o = order_evidence(a)
        ps = paired.get(o.ref) or []
        probe = ps[0] if ps else None      # 已降序：最新实测
        out[a.instance_id] = _fact(probe, o, prefer="same_cert")
    return out
