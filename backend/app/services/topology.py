"""拓扑图与架构图构建器。

节点 = 资产 / 域名 / 网络分组；边 = 四类来源（全只读推导，不新增采集面）：
  flow  link_edges（域名→后端 ECS，24h 流量权重）——实证链路，最有含金量的边
  slb   SLB backend 挂载（asset.extra.backends）
  eip   公网 IP/域名 → ECS（asset.extra.ip_public）
  net   资产 → VSwitch → VPC（extra.vpc_id/vswitch_id，作分组不作节点）

分层（汇报版布局与明细分色共用）：
  ingress(域名/EIP) → gateway(CDN/SLB/WAF) → app(ECS/ACK) → middleware(Redis/ES) → data(RDS/OSS)

组件类型归一：产品码先经 component_kinds.COMPONENT_KINDS 归一到厂商中立组件类型
（host/db/lb/object_storage/cache/cdn/mq/search/k8s/waf/apm），不同云厂商的同语义产品
共用类型；PRODUCT_LAYER/product_role 保留产品码级兼容映射，优先级见 classify_asset。

脱敏（redact=True，对外导出用）：实例 ID/内网 IP/规模数字全部抹掉，只留形态与关系。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from sqlalchemy.orm import Session

from app.services.component_kinds import (
    KIND_LABELS,
    KIND_ROLES,
    component_kind,
)
from app.services.ipnorm import extra_ips, norm_ip
from app.services.ownership import (
    apply_to_meta as _apply_own,
    business_from_hints as _biz_from_hints,
    business_hint_map as _biz_hint_map,
    load_rules as _load_own_rules,
    resolve as _own_resolve,
)

logger = logging.getLogger("qianshu.topology")

LAYER_ORDER = ("ingress", "gateway", "app", "middleware", "data")
LAYER_LABELS = {"ingress": "接入", "gateway": "网关", "app": "应用",
                "middleware": "中间件", "data": "数据"}

# 组件类型 -> 图层（通用规则，多云产品码经 COMPONENT_KINDS 归一后共用）
KIND_LAYER = {
    "cdn": "gateway", "lb": "gateway", "waf": "gateway",
    "host": "app", "k8s": "app",
    "cache": "middleware", "search": "middleware", "mq": "middleware", "apm": "middleware",
    "db": "data", "object_storage": "data",
}

# 产品码 -> 图层（兼容层：未登记组件类型的产品码走此表，行为与历史一致）
PRODUCT_LAYER = {
    "cdn": "gateway", "slb": "gateway", "waf": "gateway",
    "ecs": "app", "ack": "app",
    "redis": "middleware", "es": "middleware",
    "rds": "data", "oss": "data",
}


def _layer_of(product: str) -> str | None:
    """资产图层：组件类型优先（vendor 中立），产品码表兜底（兼容）。"""
    kind = component_kind(product)
    return KIND_LAYER.get(kind) or PRODUCT_LAYER.get(product)


# ---------------- 数据源分版 ----------------
# 桶语义固定（深链/测试可写死），页签由后端每次返回的 sources 索引动态渲染：
#   all=汇总（云容器分段，不混合）、private=私有云（vCenter/ESXi/KVM/Agent）、
#   <provider>=阿里云/腾讯云等、unassigned=未归属（台账无账号且无归属登记）。
SOURCE_ALL = "all"
SOURCE_PRIVATE = "private"
SOURCE_UNASSIGNED = "unassigned"
PRIVATE_VENDORS = frozenset({"vcenter", "esxi", "kvm", "onprem"})
SOURCE_ORDER = ("all", "aliyun", "private", "tencent", "unassigned")
SOURCE_LABELS = {"all": "汇总", "private": "私有云", "unassigned": "未归属"}


def _provider_names() -> frozenset:
    try:
        from app.services.cloud.provider import PROVIDERS

        return frozenset(PROVIDERS)
    except Exception:  # pragma: no cover - 导入链异常时按未归属处理
        return frozenset()


def valid_sources() -> frozenset:
    """合法 source 键：固定桶 + 已登记公有云厂商（非法值 API 直接 400，不画空图）。

    私有云厂商（esxi/vcenter/kvm/onprem）不单独成桶——它们统一落 private，
    否则 source=esxi 会得到一张永远为空的图。
    """
    return (frozenset({SOURCE_ALL, SOURCE_PRIVATE, SOURCE_UNASSIGNED})
            | (_provider_names() - PRIVATE_VENDORS))


def source_key_of(vendor: str) -> str:
    """厂商标识 -> 数据源桶。未知/未登记厂商一律 unassigned（红线：不发明厂商）。"""
    v = (vendor or "").strip().lower()
    if not v:
        return SOURCE_UNASSIGNED
    if v in PRIVATE_VENDORS:
        return SOURCE_PRIVATE
    if v in _provider_names():
        return v
    return SOURCE_UNASSIGNED


def source_label(key: str) -> str:
    if key in SOURCE_LABELS:
        return SOURCE_LABELS[key]
    try:
        from app.services.cloud.provider import vendor_label

        return vendor_label(key) or key
    except Exception:  # pragma: no cover
        return key


def _meta_source(meta: dict) -> str:
    return (meta or {}).get("source") or SOURCE_UNASSIGNED


def _region_of(meta: dict) -> str:
    """地域层级回退链：region → zone/cluster（VMware）→ 账号数据中心；全空不造地域。"""
    meta = meta or {}
    return (meta.get("region") or meta.get("zone") or meta.get("cluster")
            or meta.get("datacenter") or "")


def _layer_role(layer: str) -> str:
    """图层 -> 架构角色（外部占位框归位用；graph 只有 5 层，arch 有 7 角色）。"""
    return {"ingress": "ingress", "gateway": "gateway", "app": "app",
            "middleware": "middleware", "data": "db"}.get(layer, "app")


def _account_datacenter(acc) -> str:
    if acc is None:
        return ""
    try:
        return str(json.loads(acc.extra_json or "{}").get("datacenter") or "")
    except Exception:
        return ""


_IP_RE = re.compile(r"\d{1,3}(\.\d{1,3}){3}")
_ID_RE = re.compile(r"\b(i-[0-9a-z]+|lb-[0-9a-z]+|pgm-[0-9a-z]+|r-[0-9a-z]+|m-[0-9a-z]+)\b", re.I)


def _mask(text: str, redact: bool) -> str:
    """脱敏：IP→••，实例 ID→''（调用方已保证 label 用业务名）。"""
    if not redact or not text:
        return text or ""
    text = _IP_RE.sub("••••", str(text))
    text = _ID_RE.sub("", text)
    return text.strip()


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _build_full_graph(db: Session, redact: bool = False, assets: list | None = None,
                      acc_map: dict | None = None) -> dict:
    """全量图（所有数据源）：节点 meta 统一带 source/source_label/zone/cluster/datacenter。

    build_graph / build_arch 共用本函数，一次构建多次切片——消掉原「build_arch 先调
    build_graph 再自己全表扫一遍」的双循环。
    节点: {id, label, type, layer, group, meta}；边: {source, target, type, weight, label}。
    """
    from app.models.asset import Asset
    from app.models.link_edge import LinkEdge

    # 资产行 account_id 关联云账号台账，节点携带厂商/账号标签供混合云分组与图例使用。
    from app.models.cloud_account import CloudAccount
    try:
        from app.services.cloud.provider import vendor_label as _vendor_label
    except Exception:  # pragma: no cover - 导入链异常时退回原始 vendor 值
        _vendor_label = lambda v: v  # noqa: E731
    if acc_map is None:
        acc_map = {c.id: c for c in db.query(CloudAccount).all()}
    if assets is None:
        assets = db.query(Asset).filter(Asset.status != "Deleted").all()

    # 归属登记：EIP/域名/未纳管主机/台账外资产支持人工归属，
    # 与业务线登记（biz_identifiers）只读互补——配置在哪登记见系统设置-归属配置
    own_rules = _load_own_rules(db)
    biz_hints = _biz_hint_map(db)

    nodes: dict = {}   # id -> node
    edges: list = []
    edge_seen: set = set()

    def _add_edge(src: str, dst: str, etype: str, weight: float = 0.0, label: str = ""):
        if not src or not dst or src == dst:
            return
        key = (src, dst, etype)
        if key in edge_seen:
            return
        edge_seen.add(key)
        # 脱敏：规模数字与线条粗细都会泄露体量，一律抹平（只留关系）
        if redact:
            weight, label = 1.0, ""
        edges.append({"source": src, "target": dst, "type": etype,
                      "weight": round(weight, 1), "label": label})

    def _add_node(nid: str, label: str, ntype: str, layer: str, group: str = "", meta: dict | None = None):
        if not nid or nid in nodes:
            return nodes.get(nid)
        node = {"id": nid, "label": label or nid, "type": ntype,
                "layer": layer, "group": group or "", "meta": meta or {}}
        nodes[nid] = node
        return node

    # ---------- 资产节点 ----------
    by_name: dict = {}      # ecs 业务名 -> 节点 id（link_edges 用名字对齐）
    by_ip: dict = {}        # ip -> 节点 id（SLB backend 用 IP 对齐）
    for a in assets:
        layer = _layer_of(a.product)
        if layer is None:
            continue
        kind = component_kind(a.product)     # 厂商中立组件类型（空=未登记产品码）
        try:
            extra = json.loads(a.extra or "{}")
        except Exception:
            extra = {}
        nid = f"{a.product}:{a.instance_id}"
        label = _mask(a.name or a.instance_id, redact)
        acc = acc_map.get(a.account_id or 0)   # 台账无此账号=未归属（不造标签）
        meta = {
            "product": a.product,
            "kind": kind,
            "instance_id": "" if redact else a.instance_id,
            "name": "" if redact else a.name,     # 脱敏导出不回显资产名（账号标签同理）
            "region": a.region or "",
            "zone": a.zone or "",
            # VMware：集群在 extra.cluster（采集端写入 zone 同值）；私有云层级用区域→集群→数据中心回退
            "cluster": str(extra.get("cluster") or a.zone or ""),
            "datacenter": _account_datacenter(acc),
            "status": a.status or "",
            "tier": a.tier or "core",
            "account_id": acc.id if acc else 0,
            "vendor": acc.vendor if acc else "",
            "vendor_label": _vendor_label(acc.vendor) if acc else "",
            "account": "" if redact else (acc.name if acc else ""),
        }
        if kind in ("host", "lb", "db", "cache"):
            meta["ip"] = "" if redact else (extra.get("ip_public") or extra.get("address") or "")
            meta["vpc_id"] = extra.get("vpc_id", "")
        if kind == "lb":
            meta["backends"] = len(extra.get("backends") or [])
        # 归属登记按字段覆盖（台账外账号/纠正打标/补业务）；无登记保留自动值
        # IP 提取与归一统一使用 ipnorm.extra_ips。
        own_ips = extra_ips(extra)
        hit = _own_resolve(own_rules, instance=a.instance_id, name=a.name, ips=own_ips)
        if hit:
            _apply_own(meta, hit, vendor_label=_vendor_label, acc_map=acc_map)
        if not meta.get("business"):
            meta["business"] = _biz_from_hints(
                biz_hints, instance=a.instance_id, name=a.name, ips=own_ips)
        _add_node(nid, label, a.product, layer,
                  group=extra.get("vpc_id", "") or a.region or "", meta=meta)
        if kind == "host":
            by_name[(a.name or "").strip().lower()] = nid
            # SLB backend 的 ServerIp 是私网：ip_internal 是 ecs collector 的私网字段
            for ip in own_ips:
                by_ip[ip] = nid

    # ---------- 域名节点 + flow 边（link_edges，实证链路） ----------
    flow_hosts: dict = {}
    flow_edges: dict = {}   # (src,dst) -> req 累加：链路边按路径拆行后同 (域名,后端) 多行，权重求和
    for e in db.query(LinkEdge).filter(LinkEdge.status != "ignored").all():
        host = (e.host or "").strip().lower()
        if not host:
            continue
        hid = f"domain:{host}"
        acc = flow_hosts.setdefault(host, {"req": 0, "e5xx": 0})
        acc["req"] += e.req_count or 0
        acc["e5xx"] += e.cnt_5xx or 0
        if hid not in nodes:
            _add_node(hid, _mask(host, redact), "domain", "ingress",
                      group=host.split(".")[-2] if host.count(".") >= 1 else host,
                      meta={"type": "domain", "req_24h": 0})
        target = by_name.get((e.ecs_name or "").strip().lower())
        if target:
            flow_edges[(hid, target)] = flow_edges.get((hid, target), 0) + (e.req_count or 0)
        elif e.upstream_addr:
            # 后端不在资产库（未纳管机器）：落一个幽灵节点，如实呈现
            gid = f"host:{e.upstream_addr}"
            _add_node(gid, _mask(e.upstream_addr, redact), "host", "app",
                      meta={"unmanaged": True})
            flow_edges[(hid, gid)] = flow_edges.get((hid, gid), 0) + (e.req_count or 0)
    for (src, dst), req in flow_edges.items():
        _add_edge(src, dst, "flow", req, f"{req} req/24h")
    for host, acc in flow_hosts.items():
        hid = f"domain:{host}"
        if hid in nodes:
            nodes[hid]["meta"]["req_24h"] = acc["req"] if not redact else 0
            nodes[hid]["meta"]["err_24h"] = acc["e5xx"] if not redact else 0

    # ---------- LB 后端边（SLB/CLB 等负载均衡，按组件类型识别） ----------
    for a in assets:
        if component_kind(a.product) != "lb":
            continue
        try:
            backends = json.loads(a.extra or "{}").get("backends") or []
        except Exception:
            backends = []
        src = f"{a.product}:{a.instance_id}"
        for b in backends:
            tgt = by_ip.get(norm_ip(b.get("ip", "")))
            if tgt:
                _add_edge(src, tgt, "slb", _f(b.get("weight")),
                          f"{b.get('port') or ''}/w{b.get('weight') or ''}")

    # ---------- EIP/公网 → 云主机边（域名解析不到时的直连形态） ----------
    for a in assets:
        if component_kind(a.product) != "host":
            continue
        try:
            extra = json.loads(a.extra or "{}")
        except Exception:
            extra = {}
        pub = extra.get("ip_public") or ""
        if pub and f"domain:" not in pub:
            eid = f"eip:{pub}"
            _add_node(eid, _mask(pub, redact), "eip", "ingress",
                      meta={"type": "eip", "ip": "" if redact else pub})
            _add_edge(eid, f"{a.product}:{a.instance_id}", "eip")

    # ---------- CDN 域名 → 源站（有域名节点则挂边） ----------
    try:
        from app.models.cdn_domain_daily import CdnDomainDaily
        cdn_domains = [d for (d,) in db.query(CdnDomainDaily.domain).distinct().all() if d]
    except Exception:
        cdn_domains = []
    for d in cdn_domains:
        key = d.strip().lower()
        # 节点键必须与建图一致的 "domain:{host}"，裸 key 匹配不到节点
        nid = f"domain:{key}"
        if nid in nodes:
            nodes[nid]["type"] = "cdn_domain"
            nodes[nid]["layer"] = "gateway"
            nodes[nid]["meta"]["is_cdn"] = True

    # ---------- 归属登记回填到拓扑节点 ----------
    # EIP/域名/未纳管主机无采集账号：按归属登记（ownership_rules）回填厂商/账号/业务；
    # 无登记保持"未归属"（红线：不造数据、绝不猜）。业务可由业务线登记互补。
    for n in nodes.values():
        meta = n["meta"]
        if n["type"] in ("domain", "cdn_domain"):
            okeys = {"domain": n["id"][len("domain:"):]}
        elif n["type"] == "eip":
            okeys = {"ips": [n["id"][len("eip:"):]]}
        elif meta.get("unmanaged"):
            okeys = {"ips": [n["id"][len("host:"):]]}
        else:
            continue
        hit = _own_resolve(own_rules, **okeys)
        if hit:
            _apply_own(meta, hit, vendor_label=_vendor_label, acc_map=acc_map)
        if not meta.get("business"):
            meta["business"] = _biz_from_hints(biz_hints, **okeys)

    # ---------- 数据源分桶 ----------
    # 资产=账号台账 vendor；域名/EIP/未纳管主机=归属登记 vendor；两者都没有=未归属。
    # 未知 vendor（自由文本/历史脏值）不发明厂商，一律 unassigned。
    for n in nodes.values():
        meta = n.setdefault("meta", {})
        if not meta.get("source"):
            meta["source"] = source_key_of(meta.get("vendor", ""))
        meta["source_label"] = source_label(meta["source"])

    node_list = list(nodes.values())
    if redact:
        # id 序号化：实例 ID 不进对外导出数据（边同步映射）
        remap = {n["id"]: f"n{i + 1}" for i, n in enumerate(node_list)}
        for n in node_list:
            n["id"] = remap[n["id"]]
        for e in edges:
            e["source"] = remap.get(e["source"], e["source"])
            e["target"] = remap.get(e["target"], e["target"])
        nodes_after = {n["id"]: n for n in node_list}
    else:
        nodes_after = nodes
    return {"nodes": nodes_after, "edges": edges, "acc_map": acc_map, "assets": assets}


def _view_graph(full: dict, source: str) -> tuple[dict, list, list]:
    """按数据源切片：返回 (nodes, edges, external_nodes)。

    - 汇总：原样返回；跨源边标记 cross_source（前端画虚线）。
    - 单源：跨源对端不删除（删了会被当成"没有依赖"），折叠为外部占位节点
      （同 source+role 一个，防爆炸，不展开他云成员名）。
    - 未归属的域名/EIP 是公共入口：与视图内节点有边就保留，且不算跨源。
    """
    nodes, edges = full["nodes"], full["edges"]
    if source == SOURCE_ALL:
        out_edges = []
        for e in edges:
            oe = dict(e)
            ss = _meta_source((nodes.get(e["source"]) or {}).get("meta"))
            ts = _meta_source((nodes.get(e["target"]) or {}).get("meta"))
            if ss != ts and SOURCE_UNASSIGNED not in (ss, ts):
                oe["cross_source"] = True
                oe["cross_source_label"] = f"{source_label(ss)} → {source_label(ts)}"
            out_edges.append(oe)
        return dict(nodes), out_edges, []

    keep = {nid for nid, n in nodes.items() if _meta_source(n.get("meta")) == source}
    if not keep:
        return {}, [], []
    changed = True
    while changed:   # 公共入口两跳内挂靠（域名→EIP→主机等）
        changed = False
        for e in edges:
            for a, b in ((e["source"], e["target"]), (e["target"], e["source"])):
                if a in keep and b not in keep:
                    nb = nodes.get(b)
                    if (nb is not None
                            and _meta_source(nb.get("meta")) == SOURCE_UNASSIGNED
                            and nb.get("type") in ("domain", "cdn_domain", "eip")):
                        keep.add(b)
                        changed = True

    ext: dict = {}
    out_edges = []
    for e in edges:
        s, t = e["source"], e["target"]
        in_s, in_t = s in keep, t in keep
        if not in_s and not in_t:
            continue
        if in_s and in_t:
            oe = dict(e)
            ss = _meta_source((nodes.get(s) or {}).get("meta"))
            ts = _meta_source((nodes.get(t) or {}).get("meta"))
            if ss != ts and SOURCE_UNASSIGNED not in (ss, ts):
                oe["cross_source"] = True
                oe["cross_source_label"] = f"{source_label(ss)} → {source_label(ts)}"
            out_edges.append(oe)
            continue
        out_id = t if in_s else s
        nb = nodes.get(out_id) or {}
        nsrc = _meta_source(nb.get("meta"))
        if nsrc == SOURCE_UNASSIGNED and nb.get("type") not in ("domain", "cdn_domain", "eip"):
            continue   # 无归属且非入口的对端：没有可信来源，宁缺不造
        role = _layer_role(nb.get("layer"))
        ext_id = f"ext:{nsrc}:{role}"
        if ext_id not in ext:
            ext[ext_id] = {
                "id": ext_id, "label": source_label(nsrc), "type": "external",
                "layer": nb.get("layer") or "app", "group": "",
                "meta": {"external": True, "source": nsrc,
                         "source_label": source_label(nsrc), "role": role},
            }
        oe = dict(e)
        if in_s:
            oe["target"] = ext_id
            oe["cross_source_label"] = f"{source_label(source)} → {source_label(nsrc)}"
        else:
            oe["source"] = ext_id
            oe["cross_source_label"] = f"{source_label(nsrc)} → {source_label(source)}"
        oe["cross_source"] = True
        out_edges.append(oe)

    view = {nid: n for nid, n in nodes.items() if nid in keep}
    view.update(ext)
    return view, out_edges, list(ext.values())


def build_graph(db: Session, redact: bool = False, source: str = SOURCE_ALL,
                _assets: list | None = None, _acc_map: dict | None = None) -> dict:
    """构建拓扑图（可按数据源分版）。返回 {nodes, edges, layers, stats, sources, source}。

    source != all 时只返回该数据源视图；sources 索引始终是全量的（页签一次渲染）。
    """
    full = _build_full_graph(db, redact=redact, assets=_assets, acc_map=_acc_map)
    nodes, edges, _ext = _view_graph(full, source)
    node_list = list(nodes.values())
    stats = {
        "nodes": len(node_list),
        "edges": len(edges),
        "layers": {ln: sum(1 for n in node_list if n["layer"] == ln) for ln in LAYER_ORDER},
        "domains": sum(1 for n in node_list if n["type"] in ("domain", "cdn_domain")),
        "unmanaged": sum(1 for n in node_list if n["meta"].get("unmanaged")),
    }
    return {
        "nodes": node_list,
        "edges": edges,
        "layers": [{"key": k, "label": LAYER_LABELS[k]} for k in LAYER_ORDER],
        "stats": stats,
        "sources": _sources_index(full["nodes"]),
        "source": source,
        "redacted": redact,
    }

# ================= 数据源索引（页签一次渲染，与当前视图无关） =================

def _clustered_components() -> set:
    return {c.lower() for c in (load_component_rules().get("clustered_components") or [])}


def _node_group_id(n: dict, clustered: set, with_source: bool = True) -> str:
    """节点 -> 聚合键。with_source=True 为新口径（source+account 进键，杜绝混框）；
    False 为旧口径，只用于统计「消除了多少混框」。"""
    meta = n.get("meta") or {}
    typ = n.get("type")
    if typ in ("domain", "cdn_domain"):
        role, comp, ckey = "ingress", "域名", "domains"
    elif typ == "eip":
        role, comp, ckey = "ingress", "公网入口", "eip"
    elif meta.get("product"):
        role, comp, ckey = classify_asset(meta["product"], meta.get("name") or "",
                                          meta.get("source") or SOURCE_UNASSIGNED)
    else:
        return ""
    ck = ckey if (comp or "").lower() in clustered else "all"
    if not with_source:
        return f"g:{role}:{comp}:{ck}"
    src = meta.get("source") or SOURCE_UNASSIGNED
    aid = meta.get("account_id") or 0
    return f"g:{src}:{aid}:{role}:{comp}:{ck}"


def _sources_index(nodes: dict) -> list[dict]:
    """全量数据源索引：[{key,label,count,accounts,regions,groups}]。

    count=该源节点数（资产+域名/EIP/未纳管），groups=按新聚合键的组数。
    固定桶顺序按用户口径（汇总/阿里云/私有云/腾讯云/未归属），其它厂商按资产出现追加。
    """
    clustered = _clustered_components()
    buckets: dict[str, dict] = {}
    gids: dict[str, set] = {}
    for n in nodes.values():
        meta = n.get("meta") or {}
        src = meta.get("source") or SOURCE_UNASSIGNED
        b = buckets.setdefault(src, {"key": src, "label": source_label(src),
                                     "count": 0, "accounts": [], "regions": []})
        b["count"] += 1
        acc = meta.get("account") or ""
        if acc and acc not in b["accounts"]:
            b["accounts"].append(acc)
        reg = _region_of(meta)
        if reg and reg not in b["regions"]:
            b["regions"].append(reg)
        gid = _node_group_id(n, clustered)
        if gid:
            gids.setdefault(src, set()).add(gid)
    for src, b in buckets.items():
        b["groups"] = len(gids.get(src, ()))
        b["accounts"] = sorted(b["accounts"])
        b["regions"] = sorted(b["regions"])

    total = sum(b["count"] for b in buckets.values())
    all_gids: set = set()
    for s in gids.values():
        all_gids |= s
    out = [{"key": SOURCE_ALL, "label": source_label(SOURCE_ALL), "count": total,
            "accounts": sorted({a for b in buckets.values() for a in b["accounts"]}),
            "regions": sorted({r for b in buckets.values() for r in b["regions"]}),
            "groups": len(all_gids)}]
    keys = [k for k in SOURCE_ORDER if k != SOURCE_ALL and k in buckets]
    keys += sorted(k for k in buckets if k not in SOURCE_ORDER)
    out += [buckets[k] for k in keys]
    return out


def _mixed_groups_eliminated(nodes: dict) -> int:
    """旧聚合键下会混合多个 source/账号的组数——给用户量化「混云框已消除」。"""
    clustered = _clustered_components()
    old: dict = {}
    for n in nodes.values():
        gid = _node_group_id(n, clustered, with_source=False)
        if not gid:
            continue
        meta = n.get("meta") or {}
        old.setdefault(gid, set()).add(
            (meta.get("source") or SOURCE_UNASSIGNED, meta.get("account_id") or 0))
    return sum(1 for v in old.values() if len(v) > 1)


# ================= 架构图语义层 =================
# 按架构层级分组、按组件集群聚合，不把资产清单直接当成架构图。
# 识别来源为资产名模式与产品类型；环境相关命名规则在 config/components.json 配置。

ROLE_ORDER = ("ingress", "gateway", "app", "bigdata", "middleware", "db", "third_party")
ROLE_LABELS = {
    "ingress": "接入层", "gateway": "网关层", "app": "应用层", "bigdata": "大数据/搜索",
    "middleware": "中间件", "db": "数据库", "third_party": "第三方/外部应用",
}

# 组件识别规则：config/components.json 优先（换环境加规则不改代码），代码默认兜底。
# 组件类型（kind_role）是 vendor 中立默认——腾讯云 cvm/cdb/clb/cos、
# 阿里云 ecs/rds/slb/oss、自建 vmware/kvm 同语义同类型；product_role 只留产品码级
# 特例（按产品码显式覆盖 kind_role，保持既有产品码映射兼容）。
_BUILTIN_RULES = {
    "product_role": {
        "host": ("app", "主机（Agent）"),
    },
    "kind_role": {k: (KIND_ROLES[k], KIND_LABELS[k])
                  for k in KIND_LABELS if k in KIND_ROLES},
    "name_rules": [
        {"pattern": r"emr", "role": "bigdata", "component": "EMR 大数据集群"},
        {"pattern": r"kafka", "role": "bigdata", "component": "Kafka"},
        {"pattern": r"^ds-\d+", "role": "bigdata", "component": "数据计算集群"},
        {"pattern": r"milvus", "role": "bigdata", "component": "Milvus 向量库"},
        {"pattern": r"superset", "role": "bigdata", "component": "Superset BI"},
        {"pattern": r"cassandra", "role": "db", "component": "Cassandra"},
        {"pattern": r"apollo[-_]?data|database|pgsql|mysql|paas-write", "role": "db", "component": ""},
        {"pattern": r"apollo", "role": "third_party", "component": "Apollo 配置中心"},
        {"pattern": r"sentry", "role": "third_party", "component": "Sentry 错误追踪"},
        {"pattern": r"chat-?gpt|openclaw", "role": "third_party", "component": "ChatGPT/OpenAI"},
        {"pattern": r"skywalking", "role": "middleware", "component": "SkyWalking APM"},
        {"pattern": r"nginx", "role": "gateway", "component": "Nginx"},
        {"pattern": r"worker-k8s|kubernetes|k8s", "role": "app", "component": "K8s 工作节点"},
        {"pattern": r"jumpserver|堡垒机", "role": "gateway", "component": "Jumpserver 堡垒机"},
        {"pattern": r"pritunl|vpn", "role": "gateway", "component": "Pritunl VPN"},
    ],
    "clustered_components": ["Cassandra", "Kafka", "EMR 大数据集群", "缓存",
                             "数据库", "数据计算集群", "搜索引擎", "K8s 工作节点"],
}

_rules_cache = {"loaded": None}


def load_component_rules(force: bool = False) -> dict:
    """加载组件识别规则：config/components.json（相对本文件或 CWD）优先，内置兜底。"""
    if _rules_cache["loaded"] is not None and not force:
        return _rules_cache["loaded"]
    import json as _json
    import os as _os

    candidates = [
        _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))),
                      "config", "components.json"),
        _os.path.join(_os.getcwd(), "config", "components.json"),
    ]
    for path in candidates:
        if _os.path.exists(path):
            try:
                rules = _json.load(open(path))
                rules.setdefault("product_role", _BUILTIN_RULES["product_role"])
                rules.setdefault("kind_role", _BUILTIN_RULES["kind_role"])
                rules.setdefault("name_rules", _BUILTIN_RULES["name_rules"])
                rules.setdefault("clustered_components", _BUILTIN_RULES["clustered_components"])
                _rules_cache["loaded"] = rules
                logger.info("组件识别规则已加载: %s（name_rules %d 条）",
                            path, len(rules["name_rules"]))
                return rules
            except Exception as e:
                logger.warning("components.json 解析失败，用内置规则: %s", str(e)[:80])
    _rules_cache["loaded"] = _BUILTIN_RULES
    return _BUILTIN_RULES


_cluster_re = re.compile(r"[-_]?\d+([-_]\d+)*$")
_NODE_ROLE_RE = re.compile(r"[-_](core|master|task|worker|node|slave)$", re.I)


def _cluster_key(name: str) -> str:
    """集群键：去掉尾部节点编号与节点角色。Cassandra1/2→cassandra；
    EMR_c-xxx_core-1-1 / EMR_c-xxx_master-1-1→emr_c-xxx_core→emr_c-xxx（同集群）。"""
    k = (name or "").strip()
    for _ in range(3):
        k2 = _cluster_re.sub("", k)
        if k2 == k or not k2:
            break
        k = k2
    k = _NODE_ROLE_RE.sub("", k)
    return k.lower()


def classify_asset(product: str, name: str, source: str = SOURCE_UNASSIGNED) -> tuple:
    """→ (role, component_name, cluster_key)。架构语义，非云产品语义。

    识别优先级（组件类型 vendor 中立）：
      product_role（产品码级显式覆盖，保持既有映射兼容）
      > kind_role（组件类型默认：云主机/数据库/负载均衡/…，多云同语义同类型）
      > name_rules（主机上自建组件的命名规则）
      > 应用层兜底（主机按名字前缀聚类）。
    托管件（db/lb/cache/…）产品码带语义，先归类（**绝不进 name_rules**）；host 类组件
    装什么就是什么，走名字规则。规则来自 config/components.json（换环境加规则不改代码）。

    name_rules 支持可选 `scope`（private/public/any，缺省 any=保持既有输出）。
    客户可在 components.json 里给软件命名规则限定 ``"scope": "private"``，避免公有云主机名
    里的 kafka/mysql 关键字改变架构角色；代码不强制回写客户配置。
    """
    is_private = source == SOURCE_PRIVATE
    rules = load_component_rules()
    key = _cluster_key(name)
    prod_role = rules.get("product_role") or {}
    if product in prod_role:
        role, comp = prod_role[product]
        return (role, comp, key)
    kind = component_kind(product)
    kind_role = rules.get("kind_role") or {}
    if kind and kind != "host" and kind in kind_role:
        role, comp = kind_role[kind]
        return (role, comp, key)
    n = (name or "").lower()
    for r in rules.get("name_rules") or []:
        scope = (r.get("scope") or "any").strip().lower()
        if scope == "private" and not is_private:
            continue
        if scope == "public" and is_private:
            continue
        try:
            if re.search(r["pattern"], n):
                return (r["role"], r.get("component") or key, key)
        except re.error:
            continue
    return ("app", key or (name or "未命名"), key)


def build_arch(db: Session, redact: bool = False, source: str = SOURCE_ALL) -> dict:
    """架构图数据：roles（分组）→ groups（组件/集群框）→ members + role_links（框间边）。

    与 build_graph 的关系：同一份全量图（_build_full_graph）的两种抽象；
    source=all 返回汇总（云容器由前端按 group.source 分段），其它值返回单源视图。
    聚合键含 source+account：任何视图下组件框都不跨云、不跨账号混合。
    """
    from app.models.asset import Asset

    # 一次全表查询：全量图（含 sources 索引/混框统计）→ 按视图切片，避免两遍扫描
    assets = db.query(Asset).filter(Asset.status != "Deleted").all()
    full = _build_full_graph(db, redact=redact, assets=assets)
    nodes, node_edges, _ext = _view_graph(full, source)
    clustered = _clustered_components()

    groups: dict = {}   # group_id -> group
    node_group: dict = {}

    def _group(gid: str, role: str, comp: str, cluster: str, label: str,
               meta: dict, external: bool = False) -> dict:
        grp = groups.get(gid)
        if grp is None:
            src = meta.get("source") or SOURCE_UNASSIGNED
            grp = groups[gid] = {
                "id": gid, "role": role, "component": comp, "cluster": cluster,
                "label": label, "members": [], "count": 0,
                "source": src, "source_label": source_label(src),
                "account_id": meta.get("account_id") or 0,
                "account": meta.get("account") or "",
                "vendor": meta.get("vendor") or "",
                "vendor_label": meta.get("vendor_label") or "",
                "external": external,
            }
        return grp

    for n in nodes.values():
        meta = n.get("meta") or {}
        if meta.get("external"):
            src = meta.get("source") or SOURCE_UNASSIGNED
            role = meta.get("role") or _layer_role(n.get("layer"))
            gid = f"g:ext:{src}:{role}"
            grp = _group(gid, role, meta.get("source_label") or source_label(src), "",
                         meta.get("source_label") or source_label(src), meta, external=True)
            grp["count"] += 1
            node_group[n["id"]] = gid
            continue
        typ = n.get("type")
        if typ in ("domain", "cdn_domain"):
            role, comp, ckey, label = "ingress", "域名", "domains", "业务域名"
        elif typ == "eip":
            role, comp, ckey, label = "ingress", "公网入口", "eip", "公网入口"
        elif meta.get("product"):
            role, comp, ckey = classify_asset(
                meta["product"], meta.get("name") or "",
                meta.get("source") or SOURCE_UNASSIGNED)
            label = comp
        else:
            continue
        src = meta.get("source") or SOURCE_UNASSIGNED
        aid = meta.get("account_id") or 0
        ck = ckey if (comp or "").lower() in clustered else "all"
        gid = f"g:{src}:{aid}:{role}:{comp}:{ck}"
        grp = _group(gid, role, comp, ckey if ck != "all" else "", label, meta)
        grp["members"].append({"id": n["id"], "label": n["label"],
                               "meta": meta})
        grp["count"] += 1
        node_group[n["id"]] = gid

    # 框间边：节点边聚合（同组对合并权重；跨源边标 cross_source，前端画虚线）
    agg: dict = {}
    for e in node_edges:
        sg, tg = node_group.get(e["source"]), node_group.get(e["target"])
        if not sg or not tg or sg == tg:
            continue
        key = (sg, tg)
        cell = agg.setdefault(key, {"weight": 0.0, "types": set(),
                                    "cross": False, "labels": set()})
        cell["weight"] += e.get("weight") or 1.0
        cell["types"].add(e["type"])
        if e.get("cross_source"):
            cell["cross"] = True
            if e.get("cross_source_label"):
                cell["labels"].add(e["cross_source_label"])
    role_links = [{
        "source": sg, "target": tg,
        "weight": round(v["weight"], 1), "types": sorted(v["types"]),
        "cross_source": v["cross"],
        "cross_source_label": "；".join(sorted(v["labels"])) if v["labels"] else "",
    } for (sg, tg), v in agg.items()]

    # 组级数据源/账号/地域维度：聚合键已拆分，框内不再混合账号
    for g in groups.values():
        regions: dict = {}
        for m in g["members"]:
            reg = _region_of(m.get("meta") or {})
            if reg:
                regions[reg] = regions.get(reg, 0) + 1
        g["regions"] = [{"region": r, "count": c} for r, c in sorted(regions.items())]
        g["container"] = (g["regions"][0]["region"] if len(g["regions"]) == 1
                          else ("多地域（%d）" % len(g["regions"]) if g["regions"] else ""))
        if g["external"]:
            g["account_label"] = g["source_label"]
            g["accounts"] = []
        elif g["account_id"]:
            g["accounts"] = [{"id": g["account_id"], "vendor": g["vendor"],
                              "vendor_label": g["vendor_label"], "name": g["account"]}]
            g["account_label"] = (f"{g['vendor_label'] or g['source_label']} · {g['account']}"
                                  if g["account"] else g["source_label"])
        else:
            g["accounts"] = []
            g["account_label"] = g["source_label"]

    # 厂商/账号图例（只收真实账号；未归属/公共入口不发明账号）
    legend: dict = {}
    for g in groups.values():
        if g["account_id"] and not g["external"]:
            legend.setdefault(g["account_id"], {
                "id": g["account_id"], "vendor": g["vendor"],
                "vendor_label": g["vendor_label"], "name": g["account"]})

    out_groups = [g for g in groups.values() if g["count"]]
    src_order = {k: i for i, k in enumerate(SOURCE_ORDER)}
    out_groups.sort(key=lambda g: (
        ROLE_ORDER.index(g["role"]) if g["role"] in ROLE_ORDER else 99,
        src_order.get(g["source"], 50), g["account_label"], g["label"]))

    sources = _sources_index(full["nodes"])
    return {
        "roles": [{"key": r, "label": ROLE_LABELS[r]} for r in ROLE_ORDER],
        "groups": out_groups,
        "links": role_links,
        "accounts": sorted(legend.values(), key=lambda x: (x["vendor"], x["name"])),
        "sources": sources,
        "source": source,
        "stats": {
            "groups": len(out_groups),
            "links": len(role_links),
            "members": sum(g["count"] for g in out_groups),
            "sources": {s["key"]: s["count"] for s in sources},
            "mixed_groups_eliminated": _mixed_groups_eliminated(full["nodes"]),
            "external_groups": sum(1 for g in out_groups if g["external"]),
        },
        "redacted": redact,
    }


# ================= 拓扑扫描：手动 + 定时 + 变化留痕 =================

SIGNATURE_VERSION = 2   # 聚合键含 source+account；旧基线升级时只重建、不刷屏


def _group_rows(arch: dict) -> dict:
    """{group_id: {label, role, count}}——diff 的对比键。"""
    return {g["id"]: {"label": g["label"], "role": g["role"], "count": g["count"]}
            for g in arch.get("groups", [])}


def _group_signature(arch: dict) -> dict:
    """带版本的签名（旧形态是裸 dict，读取时兼容）。"""
    return {"version": SIGNATURE_VERSION, "groups": _group_rows(arch)}


def _diff(prev: dict, cur: dict) -> dict:
    """组件级变化：新增/消失/扩容/缩容（变化是产品灵魂，不是装饰）。"""
    added = [cur[k]["label"] for k in cur if k not in prev]
    removed = [prev[k]["label"] for k in prev if k not in cur]
    grown = [{"label": cur[k]["label"], "from": prev[k]["count"], "to": cur[k]["count"]}
             for k in cur if k in prev and cur[k]["count"] > prev[k]["count"]]
    shrunk = [{"label": cur[k]["label"], "from": prev[k]["count"], "to": cur[k]["count"]}
              for k in cur if k in prev and cur[k]["count"] < prev[k]["count"]]
    return {"added": added, "removed": removed, "grown": grown, "shrunk": shrunk}


def _new_session():
    from app.core.db import SessionLocal
    return SessionLocal()


# 扫描状态机（异步可轮询）：POST /scan 立即返回，前端轮询 scan_status
_scan_state = {"running": False, "started_at": None, "stage": "", "last_result": None}


def scan_status() -> dict:
    return dict(_scan_state)


def run_scan_async() -> dict:
    """后台线程执行全量扫描（云 API 同步 1-2 分钟，不能让 HTTP 请求等）。"""
    import threading

    if _scan_state["running"]:
        return {"started": False, "message": "扫描进行中"}
    _scan_state.update({"running": True, "started_at": datetime.now().isoformat(timespec="seconds"),
                        "stage": "通过统一 Provider 同步全部已启用账号资产"})

    def _worker():
        try:
            db = _new_session()
            try:
                result = run_scan(db, trigger="manual", full=True)
            finally:
                db.close()
            _scan_state.update({"running": False, "stage": "", "last_result": result})
        except Exception as e:
            _scan_state.update({"running": False, "stage": "",
                                "last_result": {"error": str(e)[:200]}})

    threading.Thread(target=_worker, daemon=True).start()
    return {"started": True, "message": "扫描已开始（资产同步约 1-2 分钟）"}


def run_scan(db: Session, trigger: str = "manual", full: bool = True) -> dict:
    """执行一次拓扑扫描。

    full=True（手动/首次）：先全量同步资产+链路（只读云 API），再重建图并留痕；
    full=False（定时轻扫描）：只重建图+diff（秒级，捕捉 DB 内已同步数据的变化）。
    """
    import json as _json
    import time as _time

    from app.models.topology_scan import TopologyScan

    t0 = _time.time()
    synced: dict = {}
    if full:
        # 唯一采集入口：按全部已启用账号走 Provider，不能绕过厂商抽象只刷四种阿里产品。
        # run_scan 本身在后台线程执行，串行同步不会阻塞 HTTP；失败逐项留痕，不中断建图。
        from app.services.cloud.sync_job import sync_accounts

        try:
            out = sync_accounts(db, force=True)
            synced["provider_sync"] = {"summary": out["summary"], "results": out["results"]}
        except Exception as e:
            synced["provider_sync"] = {"error": str(e)[:200]}
        try:
            from app.services.link_draft import generate_link_draft
            sess = _new_session()
            try:
                synced["link_draft"] = generate_link_draft(sess)
            finally:
                sess.close()
        except Exception as e:
            synced["link_draft"] = {"error": str(e)[:100]}

    arch = build_arch(db)
    cur = _group_rows(arch)
    last = (db.query(TopologyScan).order_by(TopologyScan.id.desc()).first())
    prev_raw = {}
    if last and last.diff_json is not None:
        try:
            prev_raw = _json.loads(last.synced_json or "{}").get("_signature") or {}
        except Exception:
            prev_raw = {}
    if isinstance(prev_raw, dict) and isinstance(prev_raw.get("groups"), dict):
        prev, version_ok = prev_raw["groups"], prev_raw.get("version") == SIGNATURE_VERSION
    else:
        prev, version_ok = (prev_raw if isinstance(prev_raw, dict) else {}), False

    if prev and version_ok:
        diff = _diff(prev, cur)
    elif prev:
        # 聚合键包含 source+account；旧基线不可比，只重建，不批量产生 added/removed 事件
        diff = {"added": [], "removed": [], "grown": [], "shrunk": [],
                "note": "识别规则与分组键升级，已重建基线"}
    else:
        diff = {"added": [], "removed": [], "grown": [], "shrunk": [],
                "note": "首次扫描，无对比基线"}
    duration_ms = int((_time.time() - t0) * 1000)

    synced_payload = dict(synced)
    synced_payload["_signature"] = _group_signature(arch)     # 下次 diff 的基线
    scan = TopologyScan(
        scanned_at=datetime.now(), trigger=trigger,
        groups=arch["stats"]["groups"], links=arch["stats"]["links"],
        members=arch["stats"]["members"], duration_ms=duration_ms,
        diff_json=_json.dumps(diff, ensure_ascii=False),
        synced_json=_json.dumps(synced_payload, ensure_ascii=False),
        arch_json=_json.dumps(arch, ensure_ascii=False),
    )
    db.add(scan)
    db.commit()
    return {"scan_id": scan.id, "scanned_at": scan.scanned_at.isoformat(timespec="seconds"),
            "trigger": trigger, "duration_ms": duration_ms, "stats": arch["stats"],
            "diff": diff, "synced": synced}


def last_scan_info(db: Session) -> dict:
    """最近一次扫描摘要（arch 接口带出，前端显示数据新鲜度）。"""
    import json as _json

    from app.models.topology_scan import TopologyScan

    last = db.query(TopologyScan).order_by(TopologyScan.id.desc()).first()
    if last is None:
        return {"scanned_at": None, "trigger": None, "diff": None}
    try:
        diff = _json.loads(last.diff_json or "{}")
    except Exception:
        diff = None
    return {"scanned_at": last.scanned_at.isoformat(timespec="seconds"),
            "trigger": last.trigger, "duration_ms": last.duration_ms,
            "groups": last.groups, "links": last.links, "members": last.members,
            "has_snapshot": bool(last.arch_json), "diff": diff}


def scan_snapshot(db: Session, scan_id: int = 0) -> dict | None:
    """读取指定扫描的完整架构；scan_id=0 表示最近一次。

    老记录没有 arch_json 时仍返回扫描元信息，但 arch=None，调用方必须展示证据缺失，
    不得回退 build_arch 用当前数据伪装成历史快照。
    """
    import json as _json

    from app.models.topology_scan import TopologyScan

    q = db.query(TopologyScan)
    row = q.filter(TopologyScan.id == scan_id).first() if scan_id else \
        q.order_by(TopologyScan.id.desc()).first()
    if row is None:
        return None
    arch = None
    if row.arch_json:
        try:
            loaded = _json.loads(row.arch_json)
            if isinstance(loaded, dict) and isinstance(loaded.get("groups"), list) \
                    and isinstance(loaded.get("links"), list):
                arch = loaded
        except (TypeError, ValueError):
            arch = None
    return {
        "scan_id": row.id,
        "scanned_at": row.scanned_at.isoformat(timespec="seconds"),
        "trigger": row.trigger,
        "arch": arch,
        "note": "" if arch is not None else "该扫描记录没有完整架构快照，请重新扫描",
    }


# ================= 链路完整性：哪些断了、怎么补 =================

def link_integrity(db: Session) -> dict:
    """拓扑断点检测：每条断点给"缺了会怎样 + 补齐方案"。

    检查项（数据都在拓扑/资产/指标里，纯推导不加采集面）：
      domain_no_backend  域名无后端（链路断在最外层）
      backend_no_metric  后端机器无指标（资源诊断缺位）
      isolated_component 组件无任何依赖边（孤岛，架构图上"不知道它跟谁有关"）
      layer_gap          整层缺位（如无数据库层——环境里真的没有还是没纳管？）
    """
    arch = build_arch(db)
    g0 = build_graph(db)
    issues = []

    linked = {e["source"] for e in g0["edges"]} | {e["target"] for e in g0["edges"]}
    domains = [n for n in g0["nodes"] if n["type"] in ("domain", "cdn_domain")]
    no_backend = [n["label"] for n in domains if n["id"] not in linked]
    if no_backend:
        issues.append({
            "type": "domain_no_backend", "count": len(no_backend), "samples": no_backend[:5],
            "impact": "这些域名在链路上是断头——出问题时无法归因到后端机器",
            "fix": "生成链路草稿（SLB 访问日志解析）或手工补链路映射；确认域名是否已下线",
        })

    # 后端无指标：云主机在链路里但 metrics 空（用 asset extra 的 cpu 字段粗判）
    backend_no_metric = []
    for n in g0["nodes"]:
        if component_kind(n["type"]) == "host" and n["id"] in linked:
            meta = n.get("meta") or {}
            if meta.get("unmanaged"):
                continue
    # （指标面由底座体检覆盖，这里不重复报——见 capability_registry）

    # 组件孤岛：架构图上有框但无任何依赖边
    linked_g = {l["source"] for l in arch["links"]} | {l["target"] for l in arch["links"]}
    isolated = [g["label"] for g in arch["groups"] if g["id"] not in linked_g]
    if isolated:
        issues.append({
            "type": "isolated_component", "count": len(isolated), "samples": isolated[:5],
            "impact": "组件在架构图上是孤岛——与谁依赖、影响谁都说不清",
            "fix": "补 SLB 挂载/链路观测（域名流量解析）让关系显形；或确认是否独立运维的边缘系统",
        })

    # 整层缺位
    present = {g["role"] for g in arch["groups"]}
    for role in ("gateway", "app", "db"):
        if role not in present:
            issues.append({
                "type": "layer_gap", "count": 1, "samples": [role],
                "impact": f"{role} 层没有任何组件——环境真的没有，还是没纳管/没识别？",
                "fix": "确认该层组件是否在资产同步范围内；新组件命名可加进 config/components.json 识别规则",
            })

    return {
        "ok": not issues,
        "issues": issues,
        "stats": {"groups": arch["stats"]["groups"], "links": arch["stats"]["links"],
                  "domains": len(domains), "domains_linked": len(domains) - len(no_backend)},
    }
