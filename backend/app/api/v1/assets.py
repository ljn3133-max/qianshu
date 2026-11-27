"""资产查询与手动同步 API。"""
from __future__ import annotations

import json
import logging

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_  # func 用于 /stats 聚合
from sqlalchemy.orm import Session

from app.core.client_ip import get_client_ip
from app.core.db import get_db
from app.core.deps import require_roles
from app.core.i18n_msg import lang_of, t
from app.models.asset import Asset
from app.models.user import User
from app.services import audit
from app.services.cloud.provider import ALIYUN_SYNC_HANDLERS, RESOURCE_TYPES, vendor_label

logger = logging.getLogger("qianshu.assets")
router = APIRouter(prefix="/assets", tags=["assets"])

# 产品到采集器的映射统一由云厂商 Provider 层维护，此处引用同一张表
SYNC_HANDLERS = ALIYUN_SYNC_HANDLERS
SUPPORTED_PRODUCTS = tuple(SYNC_HANDLERS.keys())

# 列表页产品摘要行：不同产品从 extra 提取一行关键信息
def _summary(product: str, e: dict) -> str:
    if product in ("rds", "cdb"):
        mem = round((e.get("memory_mb") or 0) / 1024)
        return f"{e.get('engine', '')} {e.get('engine_version', '')} · {mem}G内存"
    if product == "redis":
        cap = round((e.get("capacity_mb") or 0) / 1024)
        return f"{e.get('architecture', '')} · {cap}G · {e.get('engine_version', '')}"
    if product == "crs":
        cap = round((e.get("capacity_mb") or 0) / 1024)
        return f"{e.get('node_type', '')} · {cap}G · {e.get('engine_version', '')}"
    if product in ("slb", "clb"):
        return f"{e.get('address', '')} · {e.get('address_type', '')}"
    if product == "oss":
        return f"{e.get('storage_class', '')} · {e.get('extranet_endpoint', '')}"
    if product == "cos":
        return f"{e.get('storage_class', '') or 'COS'} · 建于 {(e.get('creation_date', '') or '')[:10]}"
    if product == "cvm":
        return f"{e.get('cpu', 0)}核 {e.get('memory_gb', 0)}G · {e.get('ip_internal', '')}"
    if product == "cdn":
        return e.get("cname", "") or e.get("cdn_type", "")
    if product == "waf":
        return f"{e.get('waf_version', '')} · 接入域名 {e.get('domain_count', 0)} 个"
    if product == "ack":
        return f"{e.get('cluster_type', '')} · {e.get('node_count', 0)}节点 · {e.get('current_version', '')}"
    if product == "es":
        return f"{e.get('es_version', '')} · {e.get('node_amount', 0)}节点 {e.get('spec_info', '')}"
    if product == "domain":
        return f"注册 {e.get('registration_date', '')[:10]} · {e.get('registrant_organization', '')}"
    if product == "ssl_cert":
        return f"{e.get('cert_type', '')} · {e.get('root_brand', '')} · {e.get('algorithm', '')}"
    if product in ("vmware_vm", "vmware_host"):
        # VMware 资产：回答"几核几 G、IP、什么系统"，不再用 vCenter 地址充地域
        cpu = e.get("cpu_count") or e.get("cpu") or 0
        mem_mb = e.get("memory_mb") or 0
        mem = round(mem_mb / 1024) if mem_mb else (e.get("memory_gb") or 0)
        ip = e.get("guest_ip") or e.get("mgmt_ip") or e.get("ip_internal") or ""
        os_name = e.get("guest_os") or ""
        parts = [f"{cpu}核 {mem}G", ip, os_name]
        return " · ".join(p for p in parts if p)
    return ""


def _account_index(db: Session) -> dict[int, dict]:
    """account_id → {vendor, name}：云厂商/账号归属标注（列表/详情列）的数据源。

    走 tenant.account_index（云账号优先/数据分组兜底），与 alerts/expiry/cost 同口径；
    数据分组兜底可覆盖只有 accounts 记录、没有云账号台账行的归属。
    """
    from app.services.tenant import account_index

    return account_index(db)


def _own_apply(db: Session, item: dict, row: Asset, accounts: dict) -> dict:
    """归属回流：vendor/account_name 过解析层。

    登记字段按字段覆盖自动值（人工登记 > 采集打标）；无登记原样返回
    （红线：不造数据）。键位与拓扑一致：instance/name + extra 里的 IP。
    """
    from app.services.ipnorm import extra_ips
    from app.services.ownership import apply_account_fields, resolve_one

    try:
        extra = json.loads(row.extra or "{}")
    except Exception:
        extra = {}
    ips = extra_ips(extra)   # IP 提取与归一使用同一实现
    hit = resolve_one(db, instance=row.instance_id, name=row.name, ips=ips)
    return apply_account_fields(item, hit, acc_map=accounts, vendor_label=vendor_label)


def _cert_resolved_out(f, row: Asset) -> dict:
    """证书资产行的统一解析标注：TLS 探测为准/订单为辅 + 来源/差异标注。

    行自身的 expire_time 保留订单事实；cert_resolved 给展示层单一事实
    （tls_probe / cas_order 两来源日期都带出，不静默取一）。"""
    from app.services.cert_fact import SOURCE_LABELS

    return {
        "not_after": f.not_after.isoformat() if f.not_after else None,
        "days_left": f.days_left,
        "level": f.level,
        "source": f.source,
        "source_label": SOURCE_LABELS.get(f.source, f.source),
        "warn_days": f.warn_days,
        "source_conflict": bool(f.source_conflict or f.serial_mismatch),
        "serial_mismatch": bool(f.serial_mismatch),
        "diff_days": f.diff_days,
        "probe_not_after": (f.probe.not_after.isoformat()
                            if f.probe and f.probe.not_after else None),
        "order_not_after": (f.order.not_after.isoformat()
                            if f.order and f.order.not_after else None),
        "order_expire_time": row.expire_time.isoformat() if row.expire_time else None,
    }


def _asset_out(row: Asset, with_extra: bool = False, accounts: dict | None = None) -> dict:
    acc_id = getattr(row, "account_id", 1) or 1   # 多云过渡
    acc = (accounts or {}).get(acc_id) or {}
    out = {
        "id": row.id, "product": row.product, "instance_id": row.instance_id,
        "name": row.name, "region": row.region, "zone": row.zone, "status": row.status,
        "account_id": acc_id,
        # 云厂商/账号归属统一走 tenant.account_index，
        # 无对应行时字段留空，不猜测归属。
        "vendor": acc.get("vendor", ""),
        "vendor_label": vendor_label(acc["vendor"]) if acc.get("vendor") else "",
        "account_name": acc.get("name", ""),
        "tier": getattr(row, "tier", "core") or "core",
        "instance_type": row.instance_type, "charge_type": row.charge_type,
        "expire_time": row.expire_time.isoformat() if row.expire_time else None,
        "tags": json.loads(row.tags) if row.tags else [],
        "synced_at": row.synced_at.isoformat() if row.synced_at else None,
    }
    if with_extra and row.extra:
        out["extra"] = json.loads(row.extra)
    return out


def _query_assets(db, *, account_id: int = 0, vendor: str = "", product: str = "ecs",
                  keyword: str = "", status: str = "", region: str = ""):
    """资产查询构造器——列表与导出共用（禁止第二套筛选口径）。"""
    q = db.query(Asset).filter(Asset.product == product)
    q = _acc_scope(db, q, Asset, account_id)   # 多账号过滤
    # 云厂商筛选在服务端按账号归属执行，不能仅过滤当前页，
    # 否则翻页后的厂商维度不准确。
    if vendor:
        from app.services.tenant import account_index
        ids = [aid for aid, a in account_index(db).items() if a.get("vendor") == vendor]
        q = q.filter(Asset.account_id.in_(ids))
    if keyword:
        like = f"%{keyword}%"
        q = q.filter(or_(Asset.name.ilike(like), Asset.instance_id.ilike(like)))
    if status:
        q = q.filter(Asset.status == status)
    else:
        # 默认不含 Deleted：同步留下的历史行不应出现在在册台账里，
        # 否则与总览资产卡口径不一致、深链后数字对不上。
        # 需要回查已删除资产时，状态筛选显式选「已删除」。
        q = q.filter(Asset.status != "Deleted")
    # 地域选项必须来自全量结果而非当前页（跨地域时第 1 页可能没有某些地域）
    region_options = sorted({r for (r,) in q.with_entities(Asset.region).distinct().all() if r})
    if region:
        q = q.filter(Asset.region == region)
    return q, region_options


@router.get("")
def list_assets(
    account_id: int = 0,
    vendor: str = "",
    product: str = Query("ecs"),
    keyword: str = "",
    status: str = "",
    region: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    q, region_options = _query_assets(db, account_id=account_id, vendor=vendor,
                                      product=product, keyword=keyword,
                                      status=status, region=region)
    total = q.count()

    rows = (
        q.order_by(Asset.status, Asset.name)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    items = []
    accounts = _account_index(db)
    cert_facts = {}
    if product == "ssl_cert":
        from app.services.cert_fact import resolve_for_assets

        cert_facts = resolve_for_assets(db, rows)   # 资产证书行统一解析
    for r in rows:
        item = _own_apply(db, _asset_out(r, accounts=accounts), r, accounts)
        if r.extra:
            e = json.loads(r.extra)
            # 不同采集源的键名归一：云主机用 cpu/memory_gb/ip_internal，
            # VMware 用 cpu_count/memory_mb/guest_ip|mgmt_ip，保持详情与列表字段一致
            item["cpu"] = e.get("cpu") if e.get("cpu") is not None else e.get("cpu_count")
            mem_gb = e.get("memory_gb")
            if mem_gb is None and e.get("memory_mb"):
                mem_gb = round(e.get("memory_mb") / 1024)
            item["memory_gb"] = mem_gb
            item["ip_internal"] = (e.get("ip_internal") or e.get("guest_ip")
                                   or e.get("mgmt_ip") or "")
            item["ip_public"] = e.get("ip_public", "")
            item["os_name"] = e.get("os_name") or e.get("guest_os") or ""
            item["tools_status"] = e.get("tools_status") or ""
            item["disk_total_gb"] = e.get("disk_total_gb")
            item["disk_count"] = (len(e.get("disks", [])) if e.get("disks")
                                  else (e.get("disk_count") or 0))
            item["summary"] = _summary(product, e)
        f = cert_facts.get(r.instance_id)
        if f is not None:
            item["cert_resolved"] = _cert_resolved_out(f, r)
        items.append(item)
    return {"total": total, "items": items, "regions": region_options}


@router.get("/export")
def export_assets(
    request: Request,
    account_id: int = 0,
    vendor: str = "",
    product: str = Query("ecs"),
    keyword: str = "",
    status: str = "",
    region: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """资产台账 CSV（与列表同筛选口径；≤2 万行，真实总数见 X-Total-Rows）。"""
    from app.core.csvx import MAX_EXPORT_ROWS, csv_response
    from app.services.ipnorm import extra_ips
    from app.services.ownership import apply_account_fields, load_rules, resolve

    q, _regions = _query_assets(db, account_id=account_id, vendor=vendor,
                                product=product, keyword=keyword,
                                status=status, region=region)
    total = q.count()
    rows = q.order_by(Asset.status, Asset.name).limit(MAX_EXPORT_ROWS).all()
    accounts = _account_index(db)
    rules = load_rules(db)   # 一次加载，避免逐行查库（2 万行导出的 N+1）
    items = []
    for r in rows:
        item = _asset_out(r, accounts=accounts)
        try:
            extra = json.loads(r.extra or "{}")
        except Exception:
            extra = {}
        hit = resolve(rules, instance=r.instance_id, name=r.name,
                      ips=extra_ips(extra))
        apply_account_fields(item, hit, acc_map=accounts, vendor_label=vendor_label)
        item["business"] = (hit or {}).get("business", "") or ""
        item["ip_internal"] = (extra.get("ip_internal") or extra.get("guest_ip")
                               or extra.get("mgmt_ip") or "")
        item["ip_public"] = extra.get("ip_public", "")
        items.append(item)
    audit.record(
        db, action="assets.export", user_id=user.id, username=user.username,
        resource_type="asset",
        detail={"account_id": account_id, "vendor": vendor, "product": product,
                "keyword": keyword, "status": status, "region": region,
                "rows": len(items), "total": total},
        client_ip=get_client_ip(request),
    )
    db.commit()
    headers = [
        ("product", "产品"), ("vendor_label", "厂商"), ("account_name", "账号"),
        ("instance_id", "实例 ID"), ("name", "名称"), ("region", "地域"),
        ("status", "状态"), ("instance_type", "规格"), ("ip_internal", "内网 IP"),
        ("ip_public", "公网 IP"), ("expire_time", "到期时间"), ("business", "业务归属"),
    ]
    return csv_response(
        headers, items,
        filename=f"资产台账_{product}.csv",
        ascii_filename=f"assets_{product}.csv",
        total_rows=total,
    )


# ---------------- 资产台账 XLSX 导出 ----------------
# 产品列不导出：按产品拆 sheet，sheet 名即产品；列目录是前端勾选框的唯一事实源。

_STATUS_LABELS = {
    "running": "运行中", "active": "运行中", "normal": "正常", "ok": "正常",
    "poweredon": "运行中", "poweredoff": "已停止",
    "online": "在线", "offline": "离线", "stopped": "已停止", "suspended": "已挂起",
    "disconnected": "已断开", "not_responding": "无响应", "notresponding": "无响应",
    "deleted": "已删除", "creating": "创建中", "pending": "启动中", "rebooting": "重启中",
    "deleting": "删除中", "configuring": "配置中", "initializing": "初始化中",
    "processing": "处理中", "locked": "已锁定", "inactive": "未激活", "isolated": "已隔离",
    "recycled": "已回收", "recycling": "回收中", "expired": "已过期", "issued": "已签发",
}
_TIER_LABELS = {"core": "全量监控", "aux": "基础监控"}
_DISK_TYPE_LABELS = {"system": "系统盘", "data": "数据盘", "disk": "虚拟磁盘"}
_DISK_CATEGORY_LABELS = {
    "cloud": "普通云盘", "cloud_efficiency": "高效云盘", "cloud_ssd": "SSD 云盘",
    "cloud_essd": "ESSD 云盘", "cloud_auto": "ESSD 自动扩容", "ephemeral": "本地盘",
    "local_ssd": "本地 SSD", "cloud_premium": "ESSD PL0",
}
_DISK_STATUS_LABELS = {
    "In_use": "使用中", "Available": "待挂载", "Attaching": "挂载中",
    "Detaching": "卸载中", "Creating": "创建中", "ReIniting": "重置中",
}
_TOOLS_LABELS = {"guestToolsRunning": "Tools 运行中", "guestToolsNotRunning": "Tools 未运行"}


def _status_label(value) -> str:
    raw = str(value or "").strip()
    return _STATUS_LABELS.get(raw.lower(), raw)


def _charge_label(value) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return {
        "prepaid": "包年包月", "postpaid": "按量付费", "payondemand": "按量付费",
        "payasyougo": "按量付费", "paybyclcu": "按流量",
    }.get(raw.lower(), raw)


def _disk_desc(d: dict) -> str:
    if not isinstance(d, dict):
        return str(d or "")
    parts = []
    device = str(d.get("device") or d.get("label") or "").strip()
    if device:
        parts.append(device)
    size = d.get("size_gb")
    if size not in (None, ""):
        parts.append(f"{size}G")
    typ = _DISK_TYPE_LABELS.get(str(d.get("type") or ""), str(d.get("type") or ""))
    if typ:
        parts.append(typ)
    category = _DISK_CATEGORY_LABELS.get(str(d.get("category") or ""),
                                         str(d.get("category") or ""))
    if category:
        parts.append(category)
    disk_status = _DISK_STATUS_LABELS.get(str(d.get("status") or ""),
                                          str(d.get("status") or ""))
    if disk_status:
        parts.append(disk_status)
    expire = str(d.get("expire_time") or "")[:10]
    if expire:
        parts.append(f"到期 {expire}")
    return " · ".join(p for p in parts if p)


def _disk_summary(extra: dict, key: str = "data") -> str:
    disks = [d for d in (extra.get("disks") or []) if isinstance(d, dict)]
    if key == "system":
        chosen = [d for d in disks if str(d.get("type") or "") == "system"]
    elif key == "data":
        chosen = [d for d in disks if str(d.get("type") or "") in ("data", "")]
        # VMware 的磁盘没有 system/data 语义，全部算数据盘/虚拟磁盘
        chosen = [d for d in chosen if str(d.get("type") or "") != "system"]
    else:
        chosen = disks
    return "；".join(x for x in (_disk_desc(d) for d in chosen) if x)


def _export_column(key: str, label: str, kind: str = "text") -> tuple:
    return (key, label, kind)


def _export_common_head(product: str) -> list:
    cols = [_export_column("name", "名称")]
    if product not in ("vmware_vm", "vmware_host"):
        cols.append(_export_column("instance_id", "实例 ID"))
    cols.append(_export_column("status_label", "运行状态"))
    return cols


def _export_common_tail(*, region=True, zone=True, zone_label="可用区", spec=True,
                        charge=True, expire=True, tags=True, tier=True) -> list:
    cols = []
    if region:
        cols.append(_export_column("region", "地域"))
    if zone:
        cols.append(_export_column("zone", zone_label))
    if tier:
        cols.append(_export_column("tier_label", "监控级别"))
    if spec:
        cols.append(_export_column("instance_type", "规格"))
    if charge:
        cols.append(_export_column("charge_type_label", "计费方式"))
    if expire:
        cols.append(_export_column("expire_time", "到期时间"))
    cols += [_export_column("account_name", "账号"), _export_column("business", "业务归属")]
    if tags:
        cols.append(_export_column("tags_text", "标签"))
    cols.append(_export_column("synced_at", "最近同步"))
    return cols


_PRODUCT_EXPORT_EXTRAS = {
    "rds": [
        ("engine", "引擎"), ("engine_version", "版本"), ("instance_role", "实例角色"),
        ("memory_mb", "内存 (MB)", "number"), ("storage_type", "存储类型"),
        ("connection_string", "连接地址"), ("net_type", "网络类型"), ("lock_mode", "锁定模式"),
    ],
    "cdb": [
        ("engine", "引擎"), ("engine_version", "版本"), ("memory_mb", "内存 (MB)", "number"),
        ("storage_capacity_gb", "存储容量 (GB)", "number"), ("cpu", "CPU 核数", "number"),
        ("qps", "QPS", "number"), ("connection_string", "连接地址"), ("port", "端口", "number"),
        ("wan_domain", "公网域名"), ("wan_port", "公网端口", "number"),
        ("vpc_id", "VPC"), ("subnet_id", "子网"),
    ],
    "redis": [
        ("engine_version", "版本"), ("architecture", "架构"), ("node_type", "节点类型"),
        ("capacity_mb", "容量 (MB)", "number"), ("max_connections", "最大连接数", "number"),
        ("max_qps", "最大 QPS", "number"), ("connection_domain", "连接地址"),
        ("port", "端口", "number"), ("private_ip", "内网 IP"), ("network_type", "网络类型"),
    ],
    "crs": [
        ("engine_version", "版本"), ("node_type", "架构"), ("capacity_mb", "容量 (MB)", "number"),
        ("max_connections", "最大连接数", "number"), ("port", "端口", "number"),
        ("private_ip", "内网 IP"), ("connection_domain", "连接地址"), ("vpc_id", "VPC"),
        ("shard_num", "分片数", "number"), ("replicas_num", "副本数", "number"),
    ],
    "slb": [
        ("address", "服务地址"), ("address_type", "地址类型"), ("network_type", "网络类型"),
        ("bandwidth", "带宽 (Mbps)", "number"), ("instance_charge_type", "实例计费"),
        ("internet_charge_type", "公网计费"), ("master_zone", "主可用区"),
        ("slave_zone", "备可用区"), ("delete_protection", "删除保护"),
    ],
    "clb": [
        ("address", "VIP"), ("address_type", "地址类型"), ("network_type", "IP 版本"),
        ("forward", "转发类型"), ("domain", "域名"), ("vpc_id", "VPC"),
        ("master_zone", "主可用区"), ("create_time", "创建时间"),
    ],
    "oss": [
        ("storage_class", "存储类型"), ("extranet_endpoint", "外网端点"),
        ("intranet_endpoint", "内网端点"), ("creation_date", "创建时间"),
    ],
    "cos": [
        ("storage_class", "存储类型"), ("bucket_region", "存储地域"),
        ("creation_date", "创建时间"),
    ],
    "cdn": [
        ("cname", "CNAME"), ("cdn_type", "CDN 类型"), ("coverage", "覆盖范围"),
        ("ssl_protocol", "HTTPS"), ("gmt_created", "创建时间"),
    ],
    "waf": [
        ("waf_version", "WAF 版本"), ("edition", "实例版本"), ("pay_type", "付费类型"),
        ("domain_count", "接入域名数", "number"), ("start_time", "开通时间"),
    ],
    "ack": [
        ("cluster_type", "集群类型"), ("current_version", "当前版本"),
        ("init_version", "初始版本"), ("node_count", "节点数", "number"),
        ("network_mode", "网络模式"), ("external_loadbalancer_id", "关联 SLB"),
        ("vpc_id", "VPC"), ("deletion_protection", "删除保护"), ("timezone", "时区"),
    ],
    "es": [
        ("es_version", "ES 版本"), ("node_amount", "节点数", "number"),
        ("node_spec", "节点规格"), ("spec_info", "规格信息"),
        ("disk_gb", "磁盘容量 (GB)", "number"), ("disk_type", "磁盘类型"),
        ("port", "端口", "number"), ("payment_type", "付费类型"),
    ],
    "domain": [
        ("registration_date", "注册时间"), ("real_name_status", "实名状态"),
        ("registrant_organization", "注册人组织"),
    ],
    "ssl_cert": [
        ("cert_type", "证书类型"), ("root_brand", "根品牌"), ("algorithm", "算法"),
        ("domain_count", "域名数", "number"), ("wild_domain_count", "通配域名数", "number"),
        ("order_id", "订单号"),
    ],
}


def export_columns(product: str) -> list:
    """产品 -> 可勾选导出列 (key, 中文列名, 类型)。产品列不在此目录中。"""
    if product == "ecs":
        cols = _export_common_head(product) + [
            _export_column("os_name", "操作系统"),
            _export_column("ip_internal", "内网 IP"), _export_column("ip_public", "公网 IP"),
            _export_column("cpu", "CPU 核数", "number"),
            _export_column("memory_gb", "内存 (GB)", "number"),
            _export_column("disk_count", "磁盘数量", "number"),
            _export_column("disk_total_gb", "磁盘总容量 (GB)", "number"),
            _export_column("system_disk", "系统盘"), _export_column("data_disks", "数据盘"),
            _export_column("disk_detail", "磁盘明细"),
            _export_column("vpc_id", "VPC"), _export_column("vswitch_id", "交换机"),
            _export_column("security_groups", "安全组"),
            _export_column("internet_max_bandwidth_out", "公网带宽 (Mbps)", "number"),
        ]
        return cols + _export_common_tail(region=True, zone=True)
    if product == "cvm":
        cols = _export_common_head(product) + [
            _export_column("os_name", "操作系统"),
            _export_column("ip_internal", "内网 IP"), _export_column("ip_public", "公网 IP"),
            _export_column("cpu", "CPU 核数", "number"),
            _export_column("memory_gb", "内存 (GB)", "number"),
            _export_column("image_id", "镜像 ID"), _export_column("vpc_id", "VPC"),
            _export_column("subnet_id", "子网"),
            _export_column("system_disk_gb", "系统盘 (GB)", "number"),
        ]
        return cols + _export_common_tail(region=True, zone=True)
    if product == "vmware_vm":
        cols = _export_common_head(product) + [
            _export_column("ip_internal", "虚拟机 IP"), _export_column("os_name", "操作系统"),
            _export_column("cpu", "CPU 核数", "number"),
            _export_column("memory_gb", "内存 (GB)", "number"),
            _export_column("power_state_label", "电源状态"),
            _export_column("tools_status_label", "VMware Tools"),
            _export_column("disk_count", "磁盘数量", "number"),
            _export_column("disk_total_gb", "磁盘总容量 (GB)", "number"),
            _export_column("disk_detail", "磁盘明细"),
        ]
        return cols + _export_common_tail(region=False, zone=True, zone_label="所属主机",
                                          charge=False, expire=False)
    if product == "vmware_host":
        cols = _export_common_head(product) + [
            _export_column("mgmt_ip", "管理 IP"), _export_column("os_name", "ESXi 版本"),
            _export_column("cpu_model", "CPU 型号"), _export_column("model", "硬件型号"),
            _export_column("cpu", "CPU 核数", "number"),
            _export_column("memory_gb", "内存 (GB)", "number"),
            _export_column("datastore_count", "数据存储数", "number"),
            _export_column("datastore_total_gb", "数据存储总容量 (GB)", "number"),
            _export_column("datastore_free_gb", "数据存储剩余 (GB)", "number"),
        ]
        return cols + _export_common_tail(region=False, zone=True, zone_label="所属集群",
                                          charge=False, expire=False)
    cols = _export_common_head(product)
    for item in _PRODUCT_EXPORT_EXTRAS.get(product, []):
        cols.append(_export_column(*item))
    return cols + _export_common_tail(region=True, zone=True, spec=True,
                                      charge=True, expire=True)


_EXPORT_PRODUCT_LABELS = {
    **{k: v for k, v in RESOURCE_TYPES.items()},
    "domain": "域名", "ssl_cert": "SSL 证书",
}
_EXPORT_PRODUCTS = sorted(_EXPORT_PRODUCT_LABELS)


def _asset_flat(row: Asset, accounts: dict, rules: list) -> dict:
    """资产行 -> 导出可读扁平字段（列 key 与 export_columns 对齐）。"""
    from app.services.ipnorm import extra_ips
    from app.services.ownership import apply_account_fields, resolve

    try:
        extra = json.loads(row.extra or "{}")
    except Exception:
        extra = {}
    if not isinstance(extra, dict):
        extra = {}
    try:
        tags = json.loads(row.tags or "[]")
    except Exception:
        tags = []
    if not isinstance(tags, list):
        tags = []
    acc = accounts.get(row.account_id) or {}
    flat = {
        "name": row.name or "", "instance_id": row.instance_id or "",
        "status_label": _status_label(row.status), "status": row.status or "",
        "region": row.region or "", "zone": row.zone or "",
        "instance_type": row.instance_type or "",
        "tier_label": _TIER_LABELS.get(row.tier or "core", row.tier or ""),
        "charge_type": row.charge_type or "",
        "charge_type_label": _charge_label(row.charge_type),
        "expire_time": (row.expire_time.isoformat()[:10] if row.expire_time else ""),
        "account_name": acc.get("name", ""), "vendor": acc.get("vendor", ""),
        "tags_text": "；".join(
            f"{x.get('key', '')}: {x.get('value', '')}"
            for x in tags if isinstance(x, dict) and (x.get("key") or x.get("value"))
        ),
        "synced_at": (row.synced_at.isoformat(timespec="seconds")
                      if row.synced_at else ""),
    }
    flat.update(extra)
    hit = resolve(rules, instance=row.instance_id, name=row.name, ips=extra_ips(extra))
    apply_account_fields(flat, hit, acc_map=accounts, vendor_label=vendor_label)
    flat["business"] = (hit or {}).get("business", "") or ""
    # 归一：阿里云主机 / 腾讯 CVM / VMware 的键名不同，导出列只暴露统一键
    flat["ip_internal"] = (extra.get("ip_internal") or extra.get("guest_ip")
                           or extra.get("mgmt_ip") or "")
    flat["ip_public"] = extra.get("ip_public", "")
    flat["os_name"] = extra.get("os_name") or extra.get("guest_os") or ""
    flat["cpu"] = extra.get("cpu") if extra.get("cpu") is not None else extra.get("cpu_count")
    mem_gb = extra.get("memory_gb")
    if mem_gb is None and extra.get("memory_mb"):
        mem_gb = round((extra.get("memory_mb") or 0) / 1024)
    flat["memory_gb"] = mem_gb
    flat["vpc_id"] = extra.get("vpc_id", "")
    flat["vswitch_id"] = extra.get("vswitch_id", "")
    sgs = extra.get("security_group_ids") or []
    flat["security_groups"] = "、".join(str(x) for x in sgs if x)
    discs = extra.get("disks") or []
    if not isinstance(discs, list):
        discs = []
    flat["disk_count"] = len(discs) if discs else (extra.get("disk_count") or 0)
    flat["disk_total_gb"] = (extra.get("disk_total_gb")
                             if extra.get("disk_total_gb") is not None
                             else sum((d.get("size_gb") or 0) for d in discs
                                      if isinstance(d, dict)))
    flat["system_disk"] = _disk_summary(extra, "system")
    flat["data_disks"] = _disk_summary(extra, "data")
    flat["disk_detail"] = "；".join(x for x in (_disk_desc(d) for d in discs) if x)
    flat["power_state_label"] = _status_label(extra.get("power_state"))
    flat["tools_status_label"] = _TOOLS_LABELS.get(
        str(extra.get("tools_status") or ""), str(extra.get("tools_status") or ""))
    return flat


class AssetExportIn(BaseModel):
    products: list[str] = Field(default_factory=list)
    columns: dict[str, list[str]] = Field(default_factory=dict)
    account_id: int = 0
    vendor: str = ""
    keyword: str = ""
    region: str = ""
    status: str = ""
    status_product: str = ""


@router.get("/export/schema")
def asset_export_schema(
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """导出列目录：资产类型 + 每类可勾选列（产品列不导出，按产品分 sheet）。"""
    from app.core.csvx import MAX_EXPORT_ROWS

    return {
        "products": [
            {
                "product": product,
                "label": _EXPORT_PRODUCT_LABELS.get(product, product),
                "columns": [
                    {"key": key, "label": label, "type": kind}
                    for key, label, kind in export_columns(product)
                ],
            }
            for product in _EXPORT_PRODUCTS
        ],
        "max_rows": MAX_EXPORT_ROWS,
    }


@router.post("/export/xlsx")
def export_assets_xlsx(
    body: AssetExportIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """资产台账多产品 XLSX：每产品一个 sheet，列由用户勾选（不导出产品列）。"""
    from app.core.csvx import MAX_EXPORT_ROWS
    from app.core.xlsx import build_xlsx, xlsx_response

    products = list(dict.fromkeys(p for p in body.products if p))
    if not products:
        raise HTTPException(400, "请至少选择一个资产类型")
    if len(products) > 32:
        raise HTTPException(400, "单次最多导出 32 个资产类型")
    unknown = [p for p in products if p not in _EXPORT_PRODUCTS]
    if unknown:
        raise HTTPException(400, f"不支持的资产类型: {', '.join(unknown)}")

    from app.services.ownership import load_rules

    accounts = _account_index(db)
    rules = load_rules(db)
    remaining = MAX_EXPORT_ROWS
    sheets: list[dict] = []
    total_rows = 0
    exported_rows = 0
    per_product: list[dict] = []
    for product in products:
        selected = body.columns.get(product) or []
        catalog = {key: (label, kind) for key, label, kind in export_columns(product)}
        bad = [key for key in selected if key not in catalog]
        if bad:
            raise HTTPException(400, f"{product} 不支持的导出列: {', '.join(bad[:5])}")
        keys = [key for key in selected if key in catalog]
        if not keys:
            raise HTTPException(400, f"{product} 请至少选择一列")
        # 关键词/厂商/地域/状态来自当前页签，只对当前产品生效；
        # 同时导出其他资产类型时不把这些条件强加过去（避免跨域空表）。
        scoped = body.status_product == product
        q, _regions = _query_assets(
            db, account_id=body.account_id,
            vendor=body.vendor if scoped else "", product=product,
            keyword=body.keyword if scoped else "",
            region=body.region if scoped else "",
            status=body.status if scoped else "",
        )
        total = q.count()
        total_rows += total
        take = min(total, remaining)
        rows = (q.order_by(Asset.status, Asset.name).limit(take).all() if take else [])
        flat_rows = [_asset_flat(row, accounts, rules) for row in rows]
        exported_rows += len(flat_rows)
        remaining -= len(flat_rows)
        sheets.append({
            "name": _EXPORT_PRODUCT_LABELS.get(product, product),
            "columns": [(key, catalog[key][0], catalog[key][1]) for key in keys],
            "rows": flat_rows,
        })
        per_product.append({"product": product, "total": total,
                            "exported": len(flat_rows),
                            "columns": len(keys)})
        if remaining <= 0:
            break

    content = build_xlsx(sheets)
    audit.record(
        db, action="assets.export", user_id=user.id, username=user.username,
        resource_type="asset",
        detail={"format": "xlsx", "products": products,
                "account_id": body.account_id, "vendor": body.vendor,
                "keyword": body.keyword, "status": body.status,
                "status_product": body.status_product, "region": body.region,
                "rows": exported_rows, "total": total_rows,
                "per_product": per_product},
        client_ip=get_client_ip(request),
    )
    db.commit()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    content, headers = xlsx_response(
        content,
        filename=f"资产台账_{stamp}.xlsx",
        ascii_filename=f"assets_{stamp}.xlsx",
        total_rows=total_rows,
        exported_rows=exported_rows,
    )
    return Response(content=content, headers=headers,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def _acc_scope(db, q, model, account_id: int, request=None):
    """多账号过滤：account_id>0 过滤；0=聚合。"""
    from app.services.tenant import account_filter
    return account_filter(q, model, account_id)


@router.get("/stats", dependencies=[Depends(require_roles("admin", "operator", "viewer"))])
def asset_stats(db: Session = Depends(get_db)):
    """按产品和状态汇总在册及历史资产数量；需要登录。"""
    rows = (
        db.query(Asset.product, Asset.status, func.count(Asset.id))
        .group_by(Asset.product, Asset.status)
        .all()
    )
    stats: dict[str, dict] = {}
    for product, st, cnt in rows:
        stats.setdefault(product, {"total": 0, "by_status": {}})
        stats[product]["total"] += cnt
        stats[product]["by_status"][st or "Unknown"] = cnt
    return stats


# 资产底账：产品短标签（RESOURCE_TYPES 之外的补充）
_EXTRA_PRODUCT_LABELS = {
    "domain": "域名", "ssl_cert": "SSL 证书", "host": "物理机/Agent 机器",
}


@router.get("/cloud-overview", dependencies=[Depends(require_roles("admin", "operator", "viewer"))])
def cloud_overview(db: Session = Depends(get_db)):
    """资产底账·多云口径：按已接入云账号动态分组（接入的显示，没接入的不显示）。

    返回每个账号的资产总数/产品构成/状态构成/最近同步时间；迁移镜像行（无厂商）
    的资产和无法归属的存量资产单列 unassigned，既不丢账也不冒充某个云。
    数据源=business 资产表实时聚合（不读缓存），资产同步入库后立即反映。
    """
    from app.models.cloud_account import CloudAccount

    rows = (
        db.query(Asset.account_id, Asset.product, Asset.status, func.count(Asset.id))
        # 底账只算在册资产：Deleted 是同步留下的历史行，计入会让"总资产/非运行"
        # 两个数字都会虚高
        .filter(Asset.status != "Deleted")
        .group_by(Asset.account_id, Asset.product, Asset.status)
        .all()
    )
    by_acc: dict[int, dict] = {}
    for aid, product, st, cnt in rows:
        p = by_acc.setdefault(int(aid or 0), {}).setdefault(
            product, {"total": 0, "by_status": {}})
        p["total"] += cnt
        p["by_status"][st or "Unknown"] = cnt

    def _product_list(d: dict) -> list:
        out = [{
            "product": prod,
            "label": RESOURCE_TYPES.get(prod) or _EXTRA_PRODUCT_LABELS.get(prod, prod),
            "total": v["total"], "by_status": v["by_status"],
        } for prod, v in d.items()]
        return sorted(out, key=lambda x: -x["total"])

    cards: list[dict] = []
    claimed: set[int] = set()
    for a in db.query(CloudAccount).order_by(CloudAccount.id).all():
        try:
            extra = json.loads(a.extra_json) if a.extra_json else {}
        except ValueError:
            extra = {}
        if extra.get("migrated_from") == "accounts_p22" or not a.vendor:
            continue   # 迁移镜像行/无厂商台账行：不是"已接入的云"
        claimed.add(a.id)
        prods = by_acc.get(a.id, {})
        cards.append({
            "id": a.id, "name": a.name, "vendor": a.vendor,
            "vendor_label": vendor_label(a.vendor),
            "status": a.status, "enabled": a.enabled,
            "last_sync_at": (a.last_sync_at.isoformat(timespec="seconds")
                             if getattr(a, "last_sync_at", None) else None),
            "alert_seeded": bool(getattr(a, "alert_seeded_at", None)),
            "asset_total": sum(v["total"] for v in prods.values()),
            "products": _product_list(prods),
        })

    orphan = {aid: d for aid, d in by_acc.items() if aid not in claimed}
    unassigned_products = {}
    for d in orphan.values():
        for prod, v in d.items():
            tgt = unassigned_products.setdefault(prod, {"total": 0, "by_status": {}})
            tgt["total"] += v["total"]
            for st, cnt in v["by_status"].items():
                tgt["by_status"][st] = tgt["by_status"].get(st, 0) + cnt

    return {
        "total": sum(v["total"] for d in by_acc.values() for v in d.values()),
        "accounts": cards,
        "unassigned": {
            "asset_total": sum(v["total"] for v in unassigned_products.values()),
            "products": _product_list(unassigned_products),
        },
    }


class TierIn(BaseModel):
    instance_id: str
    tier: str  # core=业务核心 / aux=辅助机器（只看关机+5xx外溢）


@router.post("/tier")
def set_asset_tier(
    body: TierIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """设置资产关键度，监测强度与业务关键度对齐。

    aux=辅助机器（如 Sentry 错误收集机）：指标告警与行为画像全部豁免，
    只监测"关机"和"SLB 5xx 外溢"两种信号。
    """
    if body.tier not in ("core", "aux"):
        raise HTTPException(400, "tier 只能是 core 或 aux")
    row = db.query(Asset).filter(Asset.instance_id == body.instance_id).first()
    if row is None:
        raise HTTPException(404, f"资产不存在: {body.instance_id}")
    old = row.tier
    row.tier = body.tier
    audit.record(
        db, action="asset.tier", user_id=user.id, username=user.username,
        resource_type="asset", resource_id=body.instance_id,
        detail={"name": row.name, "from": old, "to": body.tier},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return {"instance_id": row.instance_id, "name": row.name, "tier": row.tier}


class TierBatchIn(BaseModel):
    tier: str
    # 二选一：显式实例清单（表格勾选）/ 按筛选条件批量（当前产品页全量）
    instance_ids: list[str] | None = None
    product: str = ""
    account_id: int = 0
    vendor: str = ""
    keyword: str = ""
    status: str = ""
    region: str = ""


@router.post("/tier/batch")
def set_asset_tier_batch(
    body: TierBatchIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """批量设置资产关键度。

    两种口径：
    - instance_ids：表格勾选的行，只改这些；
    - 无 instance_ids：按 product + 账号/厂商/关键词/状态/地域筛选批量改。必须给
      product（防止误把全产品资产一把刷），未指定状态时不动 Deleted 历史行。
    审计一条汇总记录（匹配数/变更数/筛选条件），不逐条刷屏。
    """
    if body.tier not in ("core", "aux"):
        raise HTTPException(400, "tier 只能是 core 或 aux")
    q = db.query(Asset)
    if body.instance_ids:
        ids = [str(x).strip() for x in body.instance_ids if str(x).strip()]
        if not ids:
            raise HTTPException(400, "instance_ids 为空")
        q = q.filter(Asset.instance_id.in_(ids))
        if body.product:
            q = q.filter(Asset.product == body.product)
    else:
        if not body.product:
            raise HTTPException(400, "按筛选批量设置必须指定 product")
        q = q.filter(Asset.product == body.product)
        q = _acc_scope(db, q, Asset, body.account_id)
        if body.vendor:
            from app.services.tenant import account_index
            ids = [aid for aid, a in account_index(db).items()
                   if a.get("vendor") == body.vendor]
            if not ids:
                return {"matched": 0, "updated": 0, "tier": body.tier}
            q = q.filter(Asset.account_id.in_(ids))
        if body.keyword:
            like = f"%{body.keyword}%"
            q = q.filter(or_(Asset.name.ilike(like), Asset.instance_id.ilike(like)))
        if body.status:
            q = q.filter(Asset.status == body.status)
        else:
            q = q.filter(Asset.status != "Deleted")
        if body.region:
            q = q.filter(Asset.region == body.region)

    rows = q.all()
    changed = sum(1 for r in rows if (r.tier or "core") != body.tier)
    for r in rows:
        if (r.tier or "core") != body.tier:
            r.tier = body.tier
    audit.record(
        db, action="asset.tier.batch", user_id=user.id, username=user.username,
        resource_type="asset", resource_id=body.product or "selected",
        detail={
            "tier": body.tier, "matched": len(rows), "updated": changed,
            "instance_ids": len(body.instance_ids or []),
            "account_id": body.account_id, "vendor": body.vendor,
            "keyword": body.keyword, "status": body.status, "region": body.region,
        },
        client_ip=get_client_ip(request),
    )
    db.commit()
    return {"matched": len(rows), "updated": changed, "tier": body.tier}


@router.get("/{asset_id}")
def get_asset(
    asset_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    row = db.get(Asset, asset_id)
    if row is None:
        raise HTTPException(404, "资产不存在")
    accounts = _account_index(db)
    out = _own_apply(db, _asset_out(row, with_extra=True, accounts=accounts), row, accounts)
    if row.product == "ssl_cert":
        from app.services.cert_fact import resolve_for_assets

        f = resolve_for_assets(db, [row]).get(row.instance_id)
        if f is not None:
            out["cert_resolved"] = _cert_resolved_out(f, row)
    return out


@router.post("/sync", deprecated=True)
def trigger_sync(
    request: Request,
    response: Response,
    product: str = Query("ecs"),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """旧单产品同步（已废弃，迁移窗口内保留兼容）。

    资产落库语义与 `POST /clouds/sync` 重复（同一批 SYNC_HANDLERS）；主入口是
    按账号的 /clouds/sync。本接口暂时保留：唯一剩余调用方是到期总账的域名/证书
    同步，且它不要求已启用云账号（走旧凭证回退链 env/云凭证表）——云凭证迁移
    窗口结束前不能下线。废弃标记：OpenAPI deprecated + 响应体 {deprecated, notice}
    + Deprecation/Link 响应头。
    """
    # 废弃提示（机器可读 + 人类可读，Accept-Language 出文）
    response.headers["Deprecation"] = "true"
    response.headers["Link"] = '</clouds/sync>; rel="successor-version"'
    if product not in SUPPORTED_PRODUCTS:
        raise HTTPException(400, f"暂不支持 {product} 同步（当前支持: {SUPPORTED_PRODUCTS}）")
    try:
        result = SYNC_HANDLERS[product](db)
    except Exception as e:
        logger.exception("手动同步失败")
        audit.record(
            db, action="asset.sync", user_id=user.id, username=user.username,
            resource_type="asset", resource_id=product, result="failed",
            detail={"error": str(e)[:200]},
            client_ip=get_client_ip(request),
        )
        db.commit()
        raise HTTPException(502, f"同步失败: {str(e)[:200]}")
    audit.record(
        db, action="asset.sync", user_id=user.id, username=user.username,
        resource_type="asset", resource_id=product, detail=result,
        client_ip=get_client_ip(request),
    )
    db.commit()
    # 废弃提示随响应下发（不改变既有字段，调用方按 total/created/updated 取值不受影响）
    return {"deprecated": True,
            "notice": t("assets.sync_deprecated", lang_of(request)),
            **result}
