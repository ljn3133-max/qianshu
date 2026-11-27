"""Redis(r-kvstore) 资产同步器。注意：region 级端点不可解析，统一用中心端点。"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.collector.base import list_regions, mark_missing_deleted, parse_time, upsert_asset
from app.collector.credential import resolve_aliyun_credential

logger = logging.getLogger("qianshu.sync.redis")


def _client(ak: str, sk: str):
    from alibabacloud_r_kvstore20150101.client import Client
    from alibabacloud_tea_openapi.models import Config

    cfg = Config(access_key_id=ak, access_key_secret=sk, region_id="cn-hangzhou")
    cfg.endpoint = "r-kvstore.aliyuncs.com"  # 中心端点，region 用参数区分
    return Client(cfg)


def _fetch_instances(client, region_id: str) -> list:
    from alibabacloud_r_kvstore20150101 import models as kv

    page, instances = 1, []
    while True:
        resp = client.describe_instances(
            kv.DescribeInstancesRequest(region_id=region_id, page_size=50, page_number=page)
        )
        body = resp.body
        batch = body.instances.kvstore_instance if body.instances else []
        instances.extend(batch)
        if not batch or len(instances) >= (body.total_count or 0):
            break
        page += 1
    return instances


def sync_redis(db: Session) -> dict:
    """全量同步 Redis。返回 {total, created, updated, missing_marked, failed_regions}。"""
    ak, sk, source = resolve_aliyun_credential(db)
    client = _client(ak, sk)

    seen_ids: set = set()
    created = updated = 0
    failed_regions = []
    for region_id in list_regions(ak, sk):
        try:
            instances = _fetch_instances(client, region_id)
        except Exception as e:
            failed_regions.append(f"{region_id}: {str(e)[:80]}")
            continue

        for inst in instances:
            m = inst.to_map()
            extra = {
                "engine_version": m.get("EngineVersion", ""),
                "architecture": m.get("ArchitectureType", ""),     # cluster/standard
                "edition": m.get("EditionType", ""),                # Community/Enterprise
                "node_type": m.get("NodeType", ""),                 # double/single
                "capacity_mb": m.get("Capacity", 0),
                "max_connections": m.get("Connections", 0),
                "bandwidth_mb": m.get("Bandwidth", 0),
                "max_qps": m.get("QPS", 0),
                "connection_domain": m.get("ConnectionDomain", ""),
                "port": m.get("Port", 0),
                "private_ip": m.get("PrivateIp", ""),
                "network_type": m.get("NetworkType", ""),
                "vpc_id": m.get("VpcId", ""),
                "vswitch_id": m.get("VSwitchId", ""),
            }
            fields = dict(
                name=m.get("InstanceName") or m.get("InstanceId", ""),
                region=region_id,
                zone=m.get("ZoneId", ""),
                status=m.get("InstanceStatus", ""),
                instance_type=m.get("InstanceClass", ""),
                charge_type=m.get("ChargeType", ""),
                expire_time=parse_time(m.get("EndTime")),
                tags="[]",
                extra=json.dumps(extra, ensure_ascii=False),
                raw_meta="",
                synced_at=datetime.now(),
            )
            if upsert_asset(db, "redis", m["InstanceId"], seen_ids, fields) == "created":
                created += 1
            else:
                updated += 1

    # 清扫守卫：任一 region 请求失败=该 region 清单不可信，
    # 跳过缺失清扫防把整片实例误标 Deleted；全 region 成功才收口清扫（保守口径：
    # 宁可少清一轮，不可误清资产）。
    missing_marked = 0
    sweep_skipped = False
    if failed_regions:
        sweep_skipped = True
        logger.warning("Redis 存在失败 region，跳过缺失清扫: %s", failed_regions)
    else:
        missing_marked = mark_missing_deleted(db, "redis", seen_ids)
    db.commit()
    logger.info(
        "Redis 同步完成(凭证 %s): total=%d created=%d updated=%d deleted=%d failed_regions=%d",
        source, len(seen_ids), created, updated, missing_marked, len(failed_regions),
    )
    return {
        "total": len(seen_ids), "created": created, "updated": updated,
        "missing_marked": missing_marked, "sweep_skipped": sweep_skipped,
        "failed_regions": failed_regions,
        "credential_source": source,
    }
