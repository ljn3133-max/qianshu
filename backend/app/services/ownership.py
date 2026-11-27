"""归属解析层：实体键 → 厂商/账号/业务。

定位：全平台归属的**单一解析入口**——登记在 ownership_rules（人工），自动来源
（采集打标/云账号台账/业务线登记）由调用方先填，本层按字段覆盖：
登记了的字段覆盖自动值，没登记的字段保留自动值；两者都没有 → 调用方如实显示
"未归属"（红线：不造数据、绝不猜）。

解析规则（产品语义，确定性）：
  1. exact 优先于 pattern（人工精确登记视为更正）；
  2. 同类按实体键优先级 instance > ip > domain > name（登记实例 ID 意味着点名该实体）；
  3. pattern 内更长（更具体）的键优先；仍并列取 id 大（更新）者；
  4. 命中多条**按优先级逐字段合并**（各条登记了的字段都生效，同一字段取优先级最高者）——
     例：IP 登记了厂商/账号、名称登记了业务，两条都生效不互斥。

归一（norm_key）：ip 去端口（IPv4:port → IPv4）/ domain 小写去尾点 /
instance 与 name 小写去空白。
"""
from __future__ import annotations

import fnmatch
import logging
import re
import zlib

logger = logging.getLogger("qianshu.ownership")

KEY_TYPES = ("instance", "ip", "domain", "name")
KEY_TYPE_LABELS = {"instance": "实例 ID", "ip": "IP", "domain": "域名", "name": "名称"}


# ---------------- 键归一 ----------------

def norm_key(key_type: str, value) -> str:
    """实体键归一（历史形态兼容：IP:port / 大小写 / 域名尾点）。

    ip 分支统一委托 services/ipnorm.norm_ip，机器/链路/资产三命名空间共用同一去端口口径。
    """
    v = (value or "").strip()
    if not v:
        return ""
    if key_type == "ip":
        from app.services.ipnorm import norm_ip

        return norm_ip(v)
    if key_type == "domain":
        return v.lower().rstrip(".")
    return v.lower()   # instance / name 大小写不敏感


# ---------------- 登记加载与解析 ----------------

def load_rules(db) -> list:
    from app.models.ownership import OwnershipRule

    return db.query(OwnershipRule).order_by(OwnershipRule.id).all()


def _hit(rule, matched_type: str, matched_value: str, how: str) -> dict:
    return {
        "rule_id": rule.id,
        "key_type": matched_type,
        "key_value": matched_value,
        "match_type": rule.match_type,
        "how": how,
        "vendor": rule.vendor or "",
        "account_name": rule.account_name or "",
        "account_id": rule.account_id or 0,
        "business": rule.business or "",
        "note": rule.note or "",
    }


def resolve_hits(rules: list, *, instance=None, ips=None, domain=None, name=None) -> list:
    """全部命中（优先级序）：exact 阶段（键优先级）在前，pattern 阶段（键优先级、长键先）在后。

    ips 可传单值或列表；返回 _hit 结构列表（可能多条——字段合并见 _merge_hits）。
    """
    keys: dict = {"instance": instance, "ip": ips, "domain": domain, "name": name}
    normed: dict = {}
    for kt in KEY_TYPES:
        vals = keys.get(kt)
        if vals is None:
            continue
        if isinstance(vals, (str, bytes)):
            vals = [vals]
        vs = []
        for raw in vals:
            v = norm_key(kt, raw)
            if v:
                vs.append(v)
        if vs:
            normed[kt] = vs

    exact: dict = {}
    patterns: dict = {kt: [] for kt in KEY_TYPES}
    for r in rules:
        if (r.match_type or "exact") == "pattern":
            patterns.setdefault(r.key_type, []).append(r)
        else:
            exact[(r.key_type, norm_key(r.key_type, r.key_value))] = r

    hits: list = []
    # 阶段 1：exact（键优先级 instance > ip > domain > name）
    for kt in KEY_TYPES:
        for v in normed.get(kt, []):
            r = exact.get((kt, v))
            if r is not None:
                hits.append(_hit(r, kt, v, "exact"))
    # 阶段 2：pattern（键优先级同上；同类更长的键更具体优先）
    for kt in KEY_TYPES:
        for v in normed.get(kt, []):
            cands = [r for r in patterns.get(kt, [])
                     if fnmatch.fnmatchcase(v, norm_key(kt, r.key_value))]
            cands.sort(key=lambda r: (-len(r.key_value or ""), -r.id))
            hits.extend(_hit(r, kt, v, "pattern") for r in cands)
    return hits


def _merge_hits(hits: list) -> dict:
    """多条命中按优先级逐字段合并：各条登记了的字段都生效，同字段取优先级最高者。

    厂商/账号/绑定账号视为一组（首条带账号信息的命中整组生效）——绑定云账号台账时
    账号名以台账为准（apply_to_meta 解析）。
    """
    top = hits[0]
    out = {
        "rule_id": top["rule_id"], "key_type": top["key_type"], "key_value": top["key_value"],
        "match_type": top["match_type"], "how": top["how"], "note": top.get("note", ""),
        "vendor": "", "account_name": "", "account_id": 0, "business": "",
        "matches": [{k: h[k] for k in ("rule_id", "key_type", "key_value", "match_type", "how")}
                    for h in hits],
    }
    acct_done = False
    for h in hits:
        if h.get("vendor") and not out["vendor"]:
            out["vendor"] = h["vendor"]
        if not acct_done and (h.get("account_name") or h.get("account_id")):
            out["account_name"] = h.get("account_name") or ""
            out["account_id"] = h.get("account_id") or 0
            acct_done = True
        if h.get("business") and not out["business"]:
            out["business"] = h["business"]
    return out


def resolve(rules: list, *, instance=None, ips=None, domain=None, name=None) -> dict | None:
    """按实体键解析归属登记（合并视图）。多键同时给出时按 instance > ip > domain > name。

    ips 可传单值或列表；返回合并后的归属（含 matches 命中明细）或 None
    （无登记=调用方保持自动值/未归属）。
    """
    hits = resolve_hits(rules, instance=instance, ips=ips, domain=domain, name=name)
    return _merge_hits(hits) if hits else None


def resolve_one(db, **keys) -> dict | None:
    """便捷入口：load_rules + resolve（小调用量场景；批量请自建 rules 复用）。"""
    return resolve(load_rules(db), **keys)


# ---------------- 回流：meta 按字段覆盖 ----------------

def pseudo_account_id(vendor: str, account_name: str) -> int:
    """自由文本归属（未绑定云账号台账）的稳定虚拟账号 ID（负数，不与台账 id 碰撞）。

    同 (vendor, account_name) 跨次构建得到同一 ID——图例/泳道分组稳定。
    """
    key = f"{(vendor or '').lower()}|{(account_name or '').strip()}"
    return -(zlib.crc32(key.encode("utf-8")) % 2147483647) or -1


def apply_to_meta(meta: dict, hit: dict, vendor_label=None, acc_map: dict | None = None) -> dict:
    """把解析命中按字段覆盖进展示 meta（vendor/account/business）。

    - 绑定了云账号台账（account_id 有对应行）→ 与台账同名同色（可纠正打标）；
    - 只有文本账号 → 稳定虚拟账号 ID 进图例，不污染台账；
    - hit 里为空的字段保留调用方已有的自动值（按字段覆盖，不整包替换）。
    """
    if not hit:
        return meta
    acc = (acc_map or {}).get(hit.get("account_id") or 0)
    if acc is not None:
        meta["account_id"] = acc.id
        meta["vendor"] = acc.vendor
        meta["vendor_label"] = vendor_label(acc.vendor) if vendor_label else acc.vendor
        meta["account"] = acc.name
    else:
        if hit.get("vendor"):
            meta["vendor"] = hit["vendor"]
            meta["vendor_label"] = vendor_label(hit["vendor"]) if vendor_label else hit["vendor"]
        if hit.get("account_name"):
            meta["account"] = hit["account_name"]
            meta["account_id"] = pseudo_account_id(meta.get("vendor", ""), hit["account_name"])
    if hit.get("business"):
        meta["business"] = hit["business"]
    meta["ownership"] = "manual"
    meta["ownership_key"] = f"{hit['key_type']}:{hit['key_value']}"
    return meta


# ---------------- 回流：展示行按字段覆盖 ----------------

def hit_account(hit: dict | None, acc_map: dict | None = None) -> dict:
    """命中 → 账号展示字段 {account_name, vendor}。

    绑定云账号台账（hit.account_id 命中 acc_map）→ 台账为准（纠正打标）；
    自由文本归属 → 登记值；无命中/命中未登记账号字段 → 空串
    （调用方保留自动值——按字段覆盖，绝不造数据）。
    """
    if not hit:
        return {"account_name": "", "vendor": ""}
    acc = (acc_map or {}).get(hit.get("account_id") or 0)
    if acc is not None:
        return {"account_name": acc.get("name", ""), "vendor": acc.get("vendor", "")}
    return {"account_name": hit.get("account_name", ""), "vendor": hit.get("vendor", "")}


def apply_account_fields(fields: dict, hit: dict | None, *, acc_map: dict | None = None,
                         vendor_label=None) -> dict:
    """归属命中按字段覆盖进展示行（vendor/account_name[/vendor_label]）。

    与 apply_to_meta 同语义（人工登记 > 自动推导，按字段覆盖；无登记原样）。
    account_id 保持调用方的采集打标不动（过滤/审计口径），只覆盖展示字段。
    """
    if not hit:
        return fields
    got = hit_account(hit, acc_map)
    if got["account_name"]:
        fields["account_name"] = got["account_name"]
    if got["vendor"]:
        fields["vendor"] = got["vendor"]
        if "vendor_label" in fields and vendor_label:
            fields["vendor_label"] = vendor_label(got["vendor"])
    return fields


def target_keys(target: str = "", name: str = "") -> dict:
    """软引用 target → resolve 键参数（§3 键归一：兼容带端口/带路径的历史形态）。

    instance=target 恒给（原样归一）；host 段为 IPv4(:port) → ip 键、
    域名形态（含点）→ domain 键。project/logstore 等无点标识只走 instance/name（不猜）。
    """
    keys: dict = {}
    t = (target or "").strip()
    if t:
        keys["instance"] = t
    if name and name.strip():
        keys["name"] = name.strip()
    if not t:
        return keys
    host = t.split("://", 1)[-1].split("/", 1)[0].split("?", 1)[0].split("@")[-1]
    if not host:
        return keys
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?", host):
        keys["ips"] = [host]   # norm_key 解析时去端口
    elif "." in host and " " not in host:
        keys["domain"] = host
    return keys


def account_name_for(db, acc_map: dict | None = None, *, target: str = "",
                     target_name: str = "") -> str:
    """软引用实体的账号名（告警等历史记录补空缺）：绑定台账取台账名/文本取登记名，
    无登记返回 ''（调用方保持原值，不造数据）。"""
    hit = resolve_one(db, **target_keys(target, target_name))
    return hit_account(hit, acc_map)["account_name"]


# ---------------- 业务线互补（既有 biz_identifiers 只读回填） ----------------

def business_hint_map(db) -> dict:
    """既有业务线登记（biz_identifiers）→ 归一键值到业务名列表（只读互补，登记优先）。

    ident_type 归一到本层键位：host→domain、ecs_name/rds_instance→name、
    app_name/db_name→name（库名/应用名按名称键匹配）、ip→ip、instance→instance。
    业务挂接经本层回流到各展示面，同名多业务全部保留。
    """
    out: dict = {}
    try:
        from app.models.business import BizIdentifier

        type_map = {"host": "domain", "ecs_name": "name", "rds_instance": "name",
                    "app_name": "name", "db_name": "name",
                    "ip": "ip", "instance": "instance"}
        for row in db.query(BizIdentifier).all():
            kt = type_map.get(row.ident_type)
            if not kt:
                continue
            v = norm_key(kt, row.ident_value)
            if not v:
                continue
            try:
                bname = row.business.name if row.business else ""
            except Exception:
                bname = ""
            if bname and getattr(row.business, "status", "confirmed") == "confirmed":
                out.setdefault((kt, v), [])
                if bname not in out[(kt, v)]:
                    out[(kt, v)].append(bname)
    except Exception:   # 表缺失/未迁移：业务互补静默降级
        logger.debug("业务线登记加载失败（忽略）", exc_info=True)
    return out


def business_from_hints(biz_map: dict, *, instance=None, ips=None, domain=None,
                        name=None) -> str:
    """无归属登记时的业务兜底（业务线登记互补）：'、' 连接，无命中返回 ''。"""
    cands: list = []
    for kt, vals in (("instance", instance), ("ip", ips), ("domain", domain), ("name", name)):
        if vals is None:
            continue
        if isinstance(vals, (str, bytes)):
            vals = [vals]
        for raw in vals:
            v = norm_key(kt, raw)
            for b in biz_map.get((kt, v), []):
                if b not in cands:
                    cands.append(b)
    return "、".join(cands)


# ---------------- 未归属清单（配置引导） ----------------

def _suggest_keys(node: dict) -> list:
    """节点 → 建议登记键（优先 IP/域名/实例 ID，名称兜底）。"""
    meta = node.get("meta") or {}
    nid = str(node.get("id") or "")
    out: list = []
    if meta.get("instance_id"):
        out.append(("instance", meta["instance_id"]))
    if meta.get("ip"):
        out.append(("ip", norm_key("ip", meta["ip"])))
    if nid.startswith("domain:"):
        out.append(("domain", nid[len("domain:"):]))
    elif nid.startswith("eip:"):
        out.append(("ip", nid[len("eip:"):]))
    elif nid.startswith("host:"):
        out.append(("ip", norm_key("ip", nid[len("host:"):])))
    if meta.get("name"):
        out.append(("name", meta["name"]))
    if not out and node.get("label"):
        out.append(("name", node["label"]))
    return out


def unattributed(db, graph: dict | None = None) -> list:
    """当前仍显示"未归属"的实体清单（厂商/账号为空），带建议登记键。

    数据源=拓扑图（资产/EIP/域名/未纳管主机全形态）；已配置归属的实体不再出现
    （配置即从清单消失——这就是回流验证）。
    """
    if graph is None:
        from app.services.topology import build_graph   # 局部导入避免环

        graph = build_graph(db)
    out: list = []
    for n in graph.get("nodes", []):
        meta = n.get("meta") or {}
        if meta.get("account_id") or meta.get("account"):
            continue   # 已归属（台账/登记/绑定）
        keys = _suggest_keys(n)
        if not keys:
            continue
        out.append({
            "node_id": n.get("id", ""),
            "label": n.get("label", ""),
            "layer": n.get("layer", ""),
            "type": n.get("type", ""),
            "key_type": keys[0][0],
            "key_value": keys[0][1],
            "more_keys": [{"key_type": k, "key_value": v} for k, v in keys[1:]],
            "business": meta.get("business", ""),
        })
    return out


# ---------------- 识别线索（人工确认建议，不落库） ----------------

def hints(db, key_type: str, key_value: str) -> list:
    """该实体键的关联线索：登记对话框展示"它实际上是谁"，人工点确认才采纳。

    只读查询（资产/机器/链路），不写任何数据——红线：线索是建议，不是归属。
    """
    v = norm_key(key_type, key_value)
    if not v or key_type not in KEY_TYPES:
        return []
    out: list = []
    try:
        from app.models.asset import Asset

        assets = db.query(Asset).filter(Asset.status != "Deleted").all()
    except Exception:
        assets = []
    import json as _json

    def _acc_of(a):
        try:
            from app.models.cloud_account import CloudAccount

            acc = db.get(CloudAccount, a.account_id or 0) if (a.account_id or 0) else None
            if acc is not None:
                return acc.vendor, acc.name
        except Exception:
            pass
        return "", ""

    for a in assets:
        try:
            extra = _json.loads(a.extra or "{}")
        except Exception:
            extra = {}
        matched = False
        if key_type == "instance":
            matched = norm_key("instance", a.instance_id) == v
        elif key_type == "name":
            matched = norm_key("name", a.name or "") == v
        elif key_type == "ip":
            from app.services.ipnorm import extra_ips

            matched = v in extra_ips(extra)   # IP 提取与归一统一走 extra_ips
        elif key_type == "domain":
            pass   # 域名线索走链路/CDN，见下
        if matched:
            av, an = _acc_of(a)
            out.append({"source": "asset",
                        "ref": f"{a.name or a.instance_id}（{a.product}:{a.instance_id}）",
                        "account": f"{av} · {an}".strip(" ·") if (av or an) else "",
                        "suggest": {"vendor": av, "account_name": an}})
            if len(out) >= 5:
                return out

    if key_type == "domain":
        try:
            from app.models.link_edge import LinkEdge

            rows = (db.query(LinkEdge)
                    .filter(LinkEdge.host == v, LinkEdge.status != "ignored")
                    .limit(5).all())
            for e in rows:
                out.append({"source": "link_edge",
                            "ref": f"链路：{e.host} → {e.ecs_name or e.upstream_addr or '?'}",
                            "account": "", "suggest": {}})
        except Exception:
            pass
        try:
            from app.models.cdn_domain_daily import CdnDomainDaily

            n = db.query(CdnDomainDaily).filter(CdnDomainDaily.domain == key_value).count()
            if n:
                out.append({"source": "cdn", "ref": f"CDN 域名（{n} 天流量记录）",
                            "account": "", "suggest": {}})
        except Exception:
            pass

    if key_type == "ip":
        try:
            from app.models.machine import K8sMachine

            # 双侧归一对比：机器 ip 列也可能带端口/大小写历史形态
            m = next((x for x in db.query(K8sMachine).all()
                      if norm_key("ip", x.ip) == v), None)
            if m is not None:
                out.append({"source": "machine",
                            "ref": f"机器管理：{m.host_name or ''}（{m.ip}）",
                            "account": "", "suggest": {}})
        except Exception:
            pass
    return out
