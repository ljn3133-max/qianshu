"""RDS 资产同步器：DescribeDBInstances 分页拉取，upsert 进 assets 表。"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.collector.base import list_regions, mark_missing_deleted, parse_time, upsert_asset
from app.collector.credential import resolve_aliyun_credential

logger = logging.getLogger("qianshu.sync.rds")


def _client(ak: str, sk: str, region_id: str):
    from alibabacloud_rds20140815.client import Client
    from alibabacloud_tea_openapi.models import Config

    cfg = Config(access_key_id=ak, access_key_secret=sk, region_id=region_id)
    cfg.endpoint = f"rds.{region_id}.aliyuncs.com"
    return Client(cfg)


def _fetch_instances(client, region_id: str) -> list:
    from alibabacloud_rds20140815 import models as rds

    page, instances = 1, []
    while True:
        resp = client.describe_dbinstances(
            rds.DescribeDBInstancesRequest(region_id=region_id, page_size=100, page_number=page)
        )
        body = resp.body
        batch = body.items.dbinstance if body.items else []
        instances.extend(batch)
        if not batch or len(instances) >= (body.total_record_count or 0):
            break
        page += 1
    return instances


def sync_rds(db: Session) -> dict:
    """全量同步 RDS。返回 {total, created, updated, missing_marked, failed_regions}。"""
    ak, sk, source = resolve_aliyun_credential(db)

    seen_ids: set = set()
    created = updated = 0
    failed_regions = []
    for region_id in list_regions(ak, sk):
        try:
            instances = _fetch_instances(_client(ak, sk, region_id), region_id)
        except Exception as e:
            failed_regions.append(f"{region_id}: {str(e)[:80]}")
            continue

        for inst in instances:
            m = inst.to_map()
            extra = {
                "engine": m.get("Engine", ""),
                "engine_version": m.get("EngineVersion", ""),
                "instance_role": m.get("DBInstanceType", ""),        # Primary/ReadOnly
                "memory_mb": m.get("DBInstanceMemory", 0),
                "storage_capacity_gb": m.get("DBInstanceStorage", 0),   # 总容量（写满预测用）
                "storage_type": m.get("DBInstanceStorageType", ""),
                "connection_string": m.get("ConnectionString", ""),
                "net_type": m.get("InstanceNetworkType", ""),
                "lock_mode": m.get("LockMode", ""),
                "vpc_id": m.get("VpcId", ""),
                "vswitch_id": m.get("VSwitchId", ""),
            }
            # 列表接口不回 DBInstanceStorage（实测恒 0）——用 Attribute 接口补总容量，
            # 写满预测（dba_space_trend.days_to_full）依赖它
            if not extra.get("storage_capacity_gb"):
                try:
                    import alibabacloud_rds20140815.models as _rds_models
                    abody = _client(ak, sk, region_id).describe_dbinstance_attribute(
                        _rds_models.DescribeDBInstanceAttributeRequest(
                            dbinstance_id=m.get("DBInstanceId", ""))).body
                    for attr in (abody.items.dbinstance_attribute or []):
                        am = attr.to_map()
                        if am.get("DBInstanceStorage"):
                            extra["storage_capacity_gb"] = am["DBInstanceStorage"]
                            break
                except Exception as e:  # 容量取不到不影响同步主流程
                    logger.warning("RDS 容量补采失败 %s: %s", m.get("DBInstanceId"), str(e)[:80])
            fields = dict(
                name=m.get("DBInstanceDescription") or m.get("DBInstanceId", ""),
                region=region_id,
                zone=m.get("ZoneId", ""),
                status=m.get("DBInstanceStatus", ""),
                instance_type=m.get("DBInstanceClass", ""),
                charge_type=m.get("PayType", ""),
                expire_time=parse_time(m.get("ExpireTime")),
                tags="[]",
                extra=json.dumps(extra, ensure_ascii=False),
                raw_meta="",
                synced_at=datetime.now(),
            )
            if upsert_asset(db, "rds", m["DBInstanceId"], seen_ids, fields) == "created":
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
        logger.warning("RDS 存在失败 region，跳过缺失清扫: %s", failed_regions)
    else:
        missing_marked = mark_missing_deleted(db, "rds", seen_ids)
    db.commit()
    logger.info(
        "RDS 同步完成(凭证 %s): total=%d created=%d updated=%d deleted=%d failed_regions=%d",
        source, len(seen_ids), created, updated, missing_marked, len(failed_regions),
    )
    return {
        "total": len(seen_ids), "created": created, "updated": updated,
        "missing_marked": missing_marked, "sweep_skipped": sweep_skipped,
        "failed_regions": failed_regions,
        "credential_source": source,
    }
