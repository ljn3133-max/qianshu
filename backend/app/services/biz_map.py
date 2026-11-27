"""资源 -> 业务线 反向解析，让告警、异动和巡检结果直接显示业务归属。

业务映射（businesses + biz_identifiers）建的是 业务 -> 标识 方向；
告警/异动/巡检侧需要反着问：这条记录涉及哪些业务线？

匹配规则（大小写不敏感；host/ip 先去 :端口 再比）：
- host：与 target/target_name 等值，或作为子串出现在合并文本里（域名特征足够独特）
- ip：去端口等值命中，或按 IP 边界（前后非数字/点）出现在
  目标/文本里（URL/带端口目标能命中；不做子串——防 1.1.1.1 误伤 11.1.1.10）
- 实例/应用名（ecs_name/rds_instance/db_name/app_name/instance）：与 target/target_name
  等值命中；长度 >= 8 时也允许子串命中（短名如 "web" 子串匹配会误伤一片）

业务线数据由用户在 链路拓扑-业务映射 页自建，不预置业务归属。
未建时解析返回空列表——前端不显示业务标签，功能不报错不阻塞。
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
from typing import Optional

from sqlalchemy.orm import Session

from app.models.business import BizIdentifier, Business

logger = logging.getLogger("qianshu.biz_map")

_MIN_SUBSTR_LEN = 8   # 短标识不允许子串匹配，防误伤


def _norm_host(v: str) -> str:
    return (v or "").strip().lower().split(":")[0]


def resolve_biz(db: Session, target: str = "", target_name: str = "",
                text: str = "") -> list:
    """把一条告警/异动/巡检记录翻译成业务线名列表（["OKR", "绩效"]）。

    target=实例ID/资源键，target_name=实例名/项目名，text=标题或正文。
    同一标识可挂多业务，故返回列表。
    """
    try:
        rows = (
            db.query(BizIdentifier, Business.name)
            .join(Business, BizIdentifier.business_id == Business.id)
            .filter(Business.status == "confirmed")
            .all()
        )
    except Exception:
        return []  # 表不存在等极端情况（老库未跑过链路确认迁移）
    if not rows:
        return []

    hay = {h.strip().lower() for h in (target, target_name) if h and h.strip()}
    text_l = (text or "").lower()
    names: set = set()
    for ident, biz_name in rows:
        v = (ident.ident_value or "").strip()
        if not v or not biz_name:
            continue
        is_host = ident.ident_type == "host"
        is_ip = ident.ident_type == "ip"
        vl = _norm_host(v) if (is_host or is_ip) else v.lower()
        hit = any(vl == (_norm_host(h) if (is_host or is_ip) else h) for h in hay)
        if not hit:
            if is_host:
                hit = vl in text_l
            elif is_ip:
                # IP 按边界匹配（防 1.1.1.1 误伤 11.1.1.10），URL/带端口目标按 IP token 命中
                pat = re.compile(r"(?<![\d.])" + re.escape(vl) + r"(?![\d.])")
                hit = any(pat.search(h) for h in hay) or bool(pat.search(text_l))
            elif len(vl) >= _MIN_SUBSTR_LEN:
                hit = vl in text_l or any(vl in h for h in hay)
        if hit:
            names.add(biz_name)
    return sorted(names)


def resolve_biz_label(db: Session, target: str = "", target_name: str = "",
                      text: str = "") -> str:
    """便捷形式：返回 "OKR" / "OKR+绩效" / ""（空=无映射，前端直接不显示）。"""
    names = resolve_biz(db, target, target_name, text)
    return "+".join(names) if names else ""


_GENERIC_HOST_LABELS = {
    "www", "api", "app", "web", "portal", "gateway", "gw", "admin",
    "test", "dev", "staging", "prod", "cdn", "static", "assets",
}
_BUSINESS_TAG_KEYS = {"business", "biz", "business_line", "业务", "业务线"}
_UNKNOWN_NAMES = {"", "unknown", "none", "null", "unassigned", "未知", "未归属"}
_AUTO_DESCRIPTION = "自动草稿：来自已确认日志链路或云资源业务标签；确认前不影响告警、巡检和成本归属"


def _host_business_label(host: str) -> str:
    """域名首段只产生待确认名称；通用入口和 IP 不猜业务。"""
    value = _norm_host(host).rstrip(".")
    if not value or "*" in value or "." not in value:
        return ""
    try:
        ipaddress.ip_address(value)
        return ""
    except ValueError:
        pass
    label = value.split(".", 1)[0]
    if label in _GENERIC_HOST_LABELS or label.isdigit():
        return ""
    return label


def _tag_pairs(raw: str) -> list:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    items = value.items() if isinstance(value, dict) else value if isinstance(value, list) else []
    out = []
    for item in items:
        if isinstance(item, dict):
            key, val = item.get("key", item.get("Key", "")), item.get("value", item.get("Value", ""))
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            key, val = item
        else:
            continue
        key, val = str(key or "").strip(), str(val or "").strip()
        if key and val:
            out.append((key, val))
    return out


def _new_group(name: str) -> dict:
    return {
        "name": name, "hosts": set(), "ecs": set(), "apps": set(),
        "rds": set(), "instances": set(), "assets": set(), "sources": set(),
        "request_count": 0,
    }


def plan_biz_draft(db: Session) -> dict:
    """只读生成归属建议，不写库；所有输出都带证据或明确的未决原因。

    - 链路只消费已人工确认、真实有流量的日志边，忽略云配置补边和草稿边；
    - 云资源只认精确 business/biz/business_line/业务/业务线 标签，不按资源名猜；
    - 同一后端、应用或实例指向多个候选时列入冲突，不自动多挂；
    - www/api 等通用域名首段保持未决，交给人填写业务语义。
    """
    from app.models.asset import Asset
    from app.models.link_edge import LinkEdge

    groups: dict = {}
    unresolved: dict = {}
    conflicts: list = []
    conflict_keys: set = set()

    def add_conflict(item: dict) -> None:
        key = (item["kind"], item["ident_type"], item["value"].casefold(),
               tuple(n.casefold() for n in item["candidates"]))
        if key not in conflict_keys:
            conflict_keys.add(key)
            conflicts.append(item)
    edges = (db.query(LinkEdge)
             .filter(LinkEdge.status == "confirmed", LinkEdge.rule_source == "log",
                     LinkEdge.req_count > 0).all())
    for edge in edges:
        host = _norm_host(edge.host)
        label = _host_business_label(host)
        if not label:
            if host:
                unresolved[host] = "generic_or_invalid_host_label"
            continue
        key = label.casefold()
        group = groups.setdefault(key, _new_group(label))
        group["hosts"].add(host)
        group["request_count"] += max(int(edge.req_count or 0), 0)
        group["sources"].add("confirmed_log_edges")
        if edge.ecs_name:
            group["ecs"].add(edge.ecs_name.strip())
        if edge.app_name:
            group["apps"].add(edge.app_name.strip())

    tagged_assets = 0
    for asset in db.query(Asset).filter(Asset.status != "Deleted").all():
        tagged = {}
        for key, value in _tag_pairs(asset.tags):
            if key.casefold() in _BUSINESS_TAG_KEYS and value.casefold() not in _UNKNOWN_NAMES:
                tagged[value.casefold()] = value[:128]
        if not tagged:
            continue
        if len(tagged) > 1:
            add_conflict({
                "kind": "asset_tag", "ident_type": "instance", "value": asset.instance_id,
                "candidates": sorted(tagged.values(), key=str.casefold),
                "reason": "conflicting_business_tags",
            })
            continue
        name = next(iter(tagged.values()))
        group = groups.setdefault(name.casefold(), _new_group(name))
        tagged_assets += 1
        group["instances"].add(asset.instance_id)
        group["assets"].add(f"{asset.product}:{asset.instance_id}")
        group["sources"].add("asset_business_tags")
        if asset.product == "ecs" and asset.name:
            group["ecs"].add(asset.name.strip())
        elif asset.product == "rds" and asset.name:
            group["rds"].add(asset.name.strip())

    owners: dict = {}
    for key, group in groups.items():
        for kind, field in (("host", "hosts"), ("ecs_name", "ecs"), ("app_name", "apps"),
                            ("rds_instance", "rds"), ("instance", "instances")):
            for value in group[field]:
                if value:
                    owners.setdefault((kind, value.casefold()), set()).add(key)

    candidates = []
    for key, group in sorted(groups.items()):
        identifiers = []
        for kind, field in (("host", "hosts"), ("ecs_name", "ecs"), ("app_name", "apps"),
                            ("rds_instance", "rds"), ("instance", "instances")):
            for value in sorted(group[field], key=str.casefold):
                names = owners.get((kind, value.casefold()), set())
                if len(names) > 1:
                    add_conflict({
                        "kind": "shared_identifier", "ident_type": kind, "value": value,
                        "candidates": sorted(groups[n]["name"] for n in names),
                        "reason": "identifier_belongs_to_multiple_candidates",
                    })
                    continue
                identifiers.append({"ident_type": kind, "ident_value": value})
        if not identifiers:
            continue
        candidates.append({
            "name": group["name"], "code": group["name"][:64],
            "status": "draft", "sources": sorted(group["sources"]),
            "request_count": group["request_count"], "asset_count": len(group["assets"]),
            "identifiers": identifiers,
        })
    conflicts.sort(key=lambda x: (x["ident_type"], x["value"], x["kind"]))
    return {
        "candidates": candidates, "conflicts": conflicts,
        "unresolved_hosts": [{"host": host, "reason": reason}
                             for host, reason in sorted(unresolved.items())],
        "evidence": {"confirmed_log_edges": len(edges), "tagged_assets": tagged_assets},
    }


def generate_biz_draft(db: Session, created_by: str = "", created_by_id: int = 0) -> dict:
    """持久化不参与归属解析的草稿；只有用户逐条确认后才成为事实。"""
    plan = plan_biz_draft(db)
    existing = {b.name.casefold() for b in db.query(Business).all()}
    created, skipped, ident_n = [], [], 0
    created_candidates = []
    for candidate in plan["candidates"]:
        name = candidate["name"]
        if name.casefold() in existing:
            skipped.append(name)
            continue
        biz = Business(name=name, code=candidate["code"],
                       description=_AUTO_DESCRIPTION, status="draft")
        db.add(biz)
        db.flush()
        for ident in candidate["identifiers"]:
            source = "、".join(candidate["sources"])
            db.add(BizIdentifier(
                business_id=biz.id, ident_type=ident["ident_type"],
                ident_value=ident["ident_value"],
                note=f"自动草稿证据：{source}；请核对后确认业务",
                created_by=created_by or "auto", created_by_id=created_by_id or 0,
            ))
            ident_n += 1
        created.append(name)
        created_candidates.append(candidate)
    return {
        "created": len(created), "businesses": created,
        "identifiers": ident_n, "skipped_existing": skipped,
        "candidates": created_candidates, "conflicts": plan["conflicts"],
        "unresolved_hosts": plan["unresolved_hosts"], "evidence": plan["evidence"],
        "hint": "草稿不参与归属；请核对证据并逐条确认，未知与冲突保持未归属",
    }
