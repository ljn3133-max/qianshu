"""ACK(容器服务) 资产同步器：DescribeClustersV1 是全局 API（不分 region），单次拉取。"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.collector.base import mark_missing_deleted, upsert_asset
from app.collector.credential import resolve_aliyun_credential

logger = logging.getLogger("qianshu.sync.ack")


def sync_ack(db: Session) -> dict:
    """全量同步 ACK 集群。返回统计信息。"""
    ak, sk, source = resolve_aliyun_credential(db)
    from alibabacloud_cs20151215 import models
    from alibabacloud_cs20151215.client import Client
    from alibabacloud_tea_openapi.models import Config

    cfg = Config(access_key_id=ak, access_key_secret=sk, region_id="cn-hangzhou")
    cfg.endpoint = "cs.cn-hangzhou.aliyuncs.com"
    client = Client(cfg)

    seen_ids: set = set()
    created = updated = 0
    failed_regions: list = []
    page, clusters = 1, []
    try:
        while True:
            body = client.describe_clusters_v1(
                models.DescribeClustersV1Request(page_number=page, page_size=50)
            ).body
            batch = body.clusters or []
            clusters.extend(batch)
            page_info = body.page_info.to_map() if body.page_info else {}
            if not batch or len(clusters) >= (page_info.get("total_count") or 0):
                break
            page += 1
    except Exception as e:
        # 清扫守卫：ACK 是全局单清单（不分 region）——拉取
        # 失败/分页中断=清单不完整，记入 failed_regions 参与清扫守卫判断。
        failed_regions.append(f"global: {str(e)[:80]}")

    for cl in clusters:
        m = cl.to_map()
        extra = {
            "cluster_type": m.get("cluster_type", ""),
            "current_version": m.get("current_version", ""),
            "init_version": m.get("init_version", ""),
            "node_count": m.get("size", 0),
            "network_mode": m.get("network_mode", ""),
            "external_loadbalancer_id": m.get("external_loadbalancer_id", ""),
            "vpc_id": m.get("vpc_id", ""),
            "vswitch_ids": m.get("vswitch_ids", []),
            "deletion_protection": m.get("deletion_protection"),
            "profile": m.get("profile", ""),
            "timezone": m.get("timezone", ""),
        }
        fields = dict(
            name=m.get("name") or m["cluster_id"],
            region=m.get("region_id", ""),
            zone=m.get("zone_id", ""),
            status=m.get("state", ""),
            instance_type=m.get("cluster_spec", ""),
            charge_type="PayAsYouGo",
            expire_time=None,
            tags="[]",
            extra=json.dumps(extra, ensure_ascii=False),
            raw_meta="",
            synced_at=datetime.now(),
        )
        if upsert_asset(db, "ack", m["cluster_id"], seen_ids, fields) == "created":
            created += 1
        else:
            updated += 1

    # 清扫守卫：清单拉取不完整=清单不可信，跳过缺失清扫；
    # 全量成功才收口清扫（保守口径：宁可少清一轮，不可误清资产）。
    missing_marked = 0
    sweep_skipped = False
    if failed_regions:
        sweep_skipped = True
        logger.warning("ACK 清单拉取不完整，跳过缺失清扫: %s", failed_regions)
    else:
        missing_marked = mark_missing_deleted(db, "ack", seen_ids)
    db.commit()
    logger.info(
        "ACK 同步完成(凭证 %s): total=%d created=%d updated=%d deleted=%d",
        source, len(seen_ids), created, updated, missing_marked,
    )
    return {
        "total": len(seen_ids), "created": created, "updated": updated,
        "missing_marked": missing_marked, "sweep_skipped": sweep_skipped,
        "failed_regions": failed_regions,
        "credential_source": source,
    }
