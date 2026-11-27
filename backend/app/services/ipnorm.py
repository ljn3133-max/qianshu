"""IP 归一与互通：三命名空间共用一个解析函数。

IP 存量三形态（孤岛根源之一）：
  - k8s_machines.ip            ：干净 IP（也可能登记主机名）
  - link_edges.upstream_addr   ：IP 或 IP:port（502 残留端口数字的历史形态也有）
  - assets.extra JSON          ：ip_public / ip_internal / ip_private / address / private_ips

统一入口（去端口/去空白/大小写归一）——ownership.norm_key、链路 ECS 关联、
机器↔资产↔链路互通全部走这里，不再各写一份 split(':')[0]。
"""
from __future__ import annotations

import re

# IPv4:port（唯一确定形态才切端口；裸 IPv6 原样）
_IPV4_PORT_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3})(?::\d+)$")
# [IPv6]:port → IPv6
_IPV6_PORT_RE = re.compile(r"^\[([0-9a-fA-F:.]+)\](?::\d+)?$")


def norm_ip(value) -> str:
    """IP 归一：去空白 / IPv4:port 与 [IPv6]:port 去端口 / 小写（主机名形态大小写不敏感）。

    与 ownership.norm_key('ip') 同口径（该函数 ip 分支即委托本函数，单源）。
    """
    v = (value or "").strip()
    if not v:
        return ""
    m = _IPV6_PORT_RE.match(v)
    if m:
        return m.group(1).lower()
    m = _IPV4_PORT_RE.match(v)
    if m:
        return m.group(1)
    # 历史形态兼容：IPv4:非数字尾巴不切（不是端口）；其余原样小写
    head, sep, tail = v.partition(":")
    if sep and tail.isdigit() and head.count(".") == 3:
        return head
    return v.lower()


def extra_ips(extra: dict) -> list:
    """assets.extra JSON → 归一 IP 列表（三命名空间的第三种，各处提取逻辑单源）。

    键位与采集器一致：ip_public / ip_internal / ip_private / address / private_ips。
    """
    if not extra:
        return []
    raw = [extra.get("ip_public"), extra.get("ip_internal"),
           extra.get("ip_private"), extra.get("address")]
    raw += [str(x) for x in (extra.get("private_ips") or []) if x]
    out, seen = [], set()
    for x in raw:
        v = norm_ip(x)
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def ip_linkage_map(db, ips: list) -> dict:
    """批量版互通：一次扫资产/链路，给出多个 IP 的关联。

    只读查询（红线：不写库）。返回 {归一IP: {"assets": [...], "asset_count", "link_count"}}。
    """
    import json

    from app.models.asset import Asset
    from app.models.link_edge import LinkEdge

    want = {}
    for ip in ips:
        v = norm_ip(ip)
        if v:
            want.setdefault(v, {"assets": [], "asset_count": 0, "link_count": 0})
    if not want:
        return {}
    for a in db.query(Asset).filter(Asset.status != "Deleted").all():
        try:
            extra = json.loads(a.extra or "{}")
        except Exception:
            extra = {}
        for v in extra_ips(extra):
            if v in want:
                want[v]["assets"].append({"name": a.name or a.instance_id,
                                          "instance_id": a.instance_id,
                                          "product": a.product})
    # 链路边按 (chain, host, path_pattern, upstream_addr) 拆行——边数=行数（规则边粒度）
    for (up,) in db.query(LinkEdge.upstream_addr).all():
        v = norm_ip(up)
        if v in want:
            want[v]["link_count"] += 1
    for v in want.values():
        v["asset_count"] = len(v["assets"])
    return want


def ip_linkage(db, ip: str) -> dict:
    """按 IP 互通：该 IP 关联的资产与链路边数。

    机器管理↔资产↔链路三本账按归一 IP 对上。
    返回 {"ip", "assets": [{name, instance_id, product}], "asset_count", "link_count"}。
    """
    v = norm_ip(ip)
    got = ip_linkage_map(db, [v] if v else []).get(v) if v else None
    return {"ip": v, **(got or {"assets": [], "asset_count": 0, "link_count": 0})}
