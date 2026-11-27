"""OSS 资产同步器：ListBuckets 全局一次拉取（bucket 自带 region）。"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.collector.base import mark_missing_deleted, upsert_asset
from app.collector.credential import resolve_aliyun_credential

logger = logging.getLogger("qianshu.sync.oss")


def sync_oss(db: Session) -> dict:
    """全量同步 OSS bucket。返回统计信息。"""
    ak, sk, source = resolve_aliyun_credential(db)
    import oss2

    auth = oss2.Auth(ak, sk)
    service = oss2.Service(auth, "https://oss-cn-hangzhou.aliyuncs.com")
    failed_regions: list = []
    try:
        buckets = list(oss2.BucketIterator(service))
    except Exception as e:
        # 清扫守卫：OSS 是全局单清单（不分 region）——拉取失败
        # =清单不可信，记入 failed_regions 参与清扫守卫判断（防全桶被误标 Deleted）。
        failed_regions.append(f"global: {str(e)[:80]}")
        buckets = []

    seen_ids: set = set()
    created = updated = 0
    for b in buckets:
        created_at = (
            datetime.fromtimestamp(b.creation_date) if isinstance(b.creation_date, (int, float)) else None
        )
        extra = {
            "storage_class": b.storage_class or "",
            "extranet_endpoint": b.extranet_endpoint or "",
            "intranet_endpoint": b.intranet_endpoint or "",
            "creation_date": created_at.isoformat() if created_at else "",
        }
        fields = dict(
            name=b.name,
            region=b.region or (b.location or "").replace("oss-", ""),
            zone="",
            status="Active",
            instance_type=b.storage_class or "",
            charge_type="PostPaid",
            expire_time=None,
            tags="[]",
            extra=json.dumps(extra, ensure_ascii=False),
            raw_meta="",
            synced_at=datetime.now(),
        )
        if upsert_asset(db, "oss", b.name, seen_ids, fields) == "created":
            created += 1
        else:
            updated += 1

    # 清扫守卫：清单拉取失败=清单不可信，跳过缺失清扫；
    # 全量成功才收口清扫（保守口径：宁可少清一轮，不可误清资产）。
    missing_marked = 0
    sweep_skipped = False
    if failed_regions:
        sweep_skipped = True
        logger.warning("OSS 清单拉取失败，跳过缺失清扫: %s", failed_regions)
    else:
        missing_marked = mark_missing_deleted(db, "oss", seen_ids)
    db.commit()
    logger.info(
        "OSS 同步完成(凭证 %s): total=%d created=%d updated=%d deleted=%d",
        source, len(seen_ids), created, updated, missing_marked,
    )
    return {
        "total": len(seen_ids), "created": created, "updated": updated,
        "missing_marked": missing_marked, "sweep_skipped": sweep_skipped,
        "failed_regions": failed_regions,
        "credential_source": source,
    }
