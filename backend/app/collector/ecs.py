"""ECS 资产同步器：遍历所有 region，拉取实例 + 磁盘，upsert 进 assets 表。"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.collector.credential import resolve_aliyun_credential
from app.models.asset import Asset

logger = logging.getLogger("qianshu.sync.ecs")


def _build_client(ak: str, sk: str, region_id: str):
    """每个 region 一个 client：跨 region 调用会被部分区域拒绝(NotSupportedEndpoint)。"""
    from alibabacloud_ecs20140526.client import Client
    from alibabacloud_tea_openapi.models import Config

    return Client(Config(access_key_id=ak, access_key_secret=sk, region_id=region_id))


def _parse_time(v) -> Optional[datetime]:
    """兼容时间戳(ms, int)与 ISO 字符串两种形式。"""
    if not v or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        try:
            return datetime.fromtimestamp(v / 1000)
        except (ValueError, OSError, OverflowError):
            return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _disk_brief(d: Any) -> dict:
    expired = _parse_time(getattr(d, "expired_time", None))
    return {
        "disk_id": d.disk_id,
        "name": d.disk_name or "",
        "size_gb": d.size or 0,
        "category": d.category or "",        # cloud/cloud_essd/cloud_efficiency...
        "type": d.type or "",                 # system/data
        "status": d.status or "",
        "device": d.device or "",             # 挂载设备名 /dev/xvdb
        "instance_id": d.instance_id or "",
        "expire_time": expired.isoformat() if expired else None,
        "charge_type": getattr(d, "disk_charge_type", ""),
    }


def _instance_extra(inst: Any, disk_map: dict) -> dict:
    """实例关键信息整理进 extra。磁盘明细来自 DescribeDisks。"""
    disks = disk_map.get(inst.instance_id, [])
    vpc = inst.vpc_attributes
    private_ips = (vpc.private_ip_address.ip_address if vpc and vpc.private_ip_address else None) or []
    public_ips = (inst.public_ip_address.ip_address if inst.public_ip_address else None) or []
    eip = inst.eip_address.ip_address if inst.eip_address else ""
    return {
        "cpu": inst.cpu,
        "memory_gb": round((inst.memory or 0) / 1024, 1),
        "ip_internal": private_ips[0] if private_ips else (inst.inner_ip_address or ""),
        "ip_public": public_ips[0] if public_ips else (eip or ""),
        "os_name": inst.osname or "",
        "image_id": inst.image_id or "",
        "vpc_id": (vpc.vpc_id if vpc else "") or "",
        "vswitch_id": (vpc.v_switch_id if vpc else "") or "",
        "security_group_ids": inst.security_group_ids.security_group_id if inst.security_group_ids else [],
        "internet_max_bandwidth_out": getattr(inst, "internet_max_bandwidth_out", ""),
        "disks": disks,
        "disk_total_gb": sum(d["size_gb"] for d in disks),
    }


def _fetch_instances(client, region_id: str) -> list:
    from alibabacloud_ecs20140526 import models as ecs

    page, instances = 1, []
    while True:
        resp = client.describe_instances(
            ecs.DescribeInstancesRequest(region_id=region_id, page_size=100, page_number=page)
        )
        batch = resp.body.instances.instance or []
        instances.extend(batch)
        if not batch or len(instances) >= resp.body.total_count:
            break
        page += 1
    return instances


def _fetch_disk_map(client, region_id: str) -> dict:
    from alibabacloud_ecs20140526 import models as ecs

    disk_map: dict[str, list[dict]] = {}
    page, fetched = 1, 0
    while True:
        resp = client.describe_disks(
            ecs.DescribeDisksRequest(region_id=region_id, page_size=100, page_number=page)
        )
        batch = resp.body.disks.disk or []
        for d in batch:
            disk_map.setdefault(d.instance_id or "", []).append(_disk_brief(d))
        fetched += len(batch)
        if not batch or fetched >= resp.body.total_count:
            break
        page += 1
    return disk_map


def sync_ecs(db: Session) -> dict:
    """全量同步 ECS。返回 {total, created, updated, missing_marked, failed_regions}。"""
    ak, sk, source = resolve_aliyun_credential(db)
    from alibabacloud_ecs20140526 import models as ecs

    region_ids = [
        r.region_id
        for r in _build_client(ak, sk, "cn-hangzhou")
        .describe_regions(ecs.DescribeRegionsRequest())
        .body.regions.region
    ]

    seen_ids: set = set()
    created = updated = 0
    failed_regions: list[str] = []
    for region_id in region_ids:
        client = _build_client(ak, sk, region_id)
        try:
            instances = _fetch_instances(client, region_id)
            if not instances:
                continue
            disk_map = _fetch_disk_map(client, region_id)
        except Exception as e:
            # 单 region 失败不影响其他 region
            failed_regions.append(f"{region_id}: {str(e)[:80]}")
            continue

        for inst in instances:
            seen_ids.add(inst.instance_id)
            tags = (
                [{"key": t.tag_key, "value": t.tag_value} for t in (inst.tags.tag or [])]
                if inst.tags
                else []
            )
            row = (
                db.query(Asset)
                .filter(Asset.product == "ecs", Asset.instance_id == inst.instance_id)
                .first()
            )
            fields = dict(
                name=inst.instance_name or "",
                region=region_id,
                zone=inst.zone_id or "",
                status=inst.status or "",
                instance_type=inst.instance_type or "",
                charge_type=inst.instance_charge_type or "",
                expire_time=_parse_time(getattr(inst, "expired_time", None)),
                tags=json.dumps(tags, ensure_ascii=False),
                extra=json.dumps(_instance_extra(inst, disk_map), ensure_ascii=False),
                raw_meta="",  # 原始快照体积大，不默认保存；按需裁剪后再存
                synced_at=datetime.now(),
            )
            if row is None:
                from app.services.tenant import account_id as _acc

                fields.setdefault("account_id", _acc(db))   # 多账号打标（对齐 upsert_asset）
                row = Asset(product="ecs", instance_id=inst.instance_id, **fields)
                db.add(row)
                created += 1
            else:
                # 归属自愈（对齐 base.upsert_asset）：账号上下文采集到的
                # 存量行更新时纠正 account_id，否则旧数据分组 id 的 ECS 永远"未归属"。
                from app.collector.credential import active_account_id

                _acc = active_account_id()
                if _acc is not None:
                    row.account_id = _acc
                for k, v in fields.items():
                    setattr(row, k, v)
                updated += 1

    # 当次同步未见到且未标记过的 -> Deleted（保留历史，不物理删）。
    # 统一调用 mark_missing_deleted，复用账号级清扫范围，避免多账号同步时误删其他账号实例。
    from app.collector.base import mark_missing_deleted

    # 清扫守卫：任一 region 请求失败=该 region 清单不可信，
    # 跳过缺失清扫防把整片实例误标 Deleted；全 region 成功才收口清扫（保守口径：
    # 宁可少清一轮，不可误清资产）。
    missing_marked = 0
    sweep_skipped = False
    if failed_regions:
        sweep_skipped = True
        logger.warning("ECS 存在失败 region，跳过缺失清扫: %s", failed_regions)
    else:
        missing_marked = mark_missing_deleted(db, "ecs", seen_ids)

    db.commit()
    logger.info(
        "ECS 同步完成(凭证 %s): total=%d created=%d updated=%d deleted=%d failed_regions=%d",
        source, len(seen_ids), created, updated, missing_marked, len(failed_regions),
    )
    return {
        "total": len(seen_ids), "created": created, "updated": updated,
        "missing_marked": missing_marked, "sweep_skipped": sweep_skipped,
        "failed_regions": failed_regions,
        "credential_source": source,
    }
