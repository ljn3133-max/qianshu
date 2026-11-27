"""CDN 资产同步器：DescribeUserDomains 中心端点一次拉取（域名维度资产）。"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.collector.base import mark_missing_deleted, parse_time, upsert_asset
from app.collector.credential import resolve_aliyun_credential

logger = logging.getLogger("qianshu.sync.cdn")


def sync_cdn(db: Session) -> dict:
    """全量同步 CDN 加速域名。返回统计信息。"""
    ak, sk, source = resolve_aliyun_credential(db)
    from alibabacloud_cdn20180510 import models
    from alibabacloud_cdn20180510.client import Client
    from alibabacloud_tea_openapi.models import Config

    cfg = Config(access_key_id=ak, access_key_secret=sk, region_id="cn-hangzhou")
    cfg.endpoint = "cdn.aliyuncs.com"
    client = Client(cfg)

    page, domains = 1, []
    failed_regions: list = []
    try:
        while True:
            resp = client.describe_user_domains(
                models.DescribeUserDomainsRequest(page_size=50, page_number=page)
            ).body
            batch = resp.domains.page_data if resp.domains else []
            domains.extend(batch)
            if not batch or len(domains) >= (resp.total_count or 0):
                break
            page += 1
    except Exception as e:
        # 清扫守卫：CDN 是全局单清单（不分 region）——拉取失败/
        # 分页中断=清单不完整，记入 failed_regions 参与清扫守卫判断（防域名被误标 Deleted）。
        failed_regions.append(f"global: {str(e)[:80]}")

    seen_ids: set = set()
    created = updated = 0
    for d in domains:
        m = d.to_map()
        sources = [
            {
                "content": s.get("Content", ""),
                "type": s.get("Type", ""),
                "port": s.get("Port"),
                "priority": s.get("Priority", ""),
            }
            for s in (m.get("Sources") or {}).get("Source", [])
        ]
        extra = {
            "cname": m.get("Cname", ""),
            "cdn_type": m.get("CdnType", ""),
            "coverage": m.get("Coverage", ""),
            "ssl_protocol": m.get("SslProtocol", ""),
            "sources": sources,
            "gmt_created": m.get("GmtCreated", ""),
        }
        fields = dict(
            name=m["DomainName"],
            region="global",
            zone="",
            status=m.get("DomainStatus", ""),
            instance_type=m.get("CdnType", ""),
            charge_type="PostPaid",
            expire_time=None,
            tags="[]",
            extra=json.dumps(extra, ensure_ascii=False),
            raw_meta="",
            synced_at=datetime.now(),
        )
        if upsert_asset(db, "cdn", m["DomainName"], seen_ids, fields) == "created":
            created += 1
        else:
            updated += 1

    # 清扫守卫：清单拉取不完整=清单不可信，跳过缺失清扫；
    # 全量成功才收口清扫（保守口径：宁可少清一轮，不可误清资产）。
    missing_marked = 0
    sweep_skipped = False
    if failed_regions:
        sweep_skipped = True
        logger.warning("CDN 清单拉取不完整，跳过缺失清扫: %s", failed_regions)
    else:
        missing_marked = mark_missing_deleted(db, "cdn", seen_ids)
    db.commit()
    logger.info(
        "CDN 同步完成(凭证 %s): total=%d created=%d updated=%d deleted=%d",
        source, len(seen_ids), created, updated, missing_marked,
    )
    return {
        "total": len(seen_ids), "created": created, "updated": updated,
        "missing_marked": missing_marked, "sweep_skipped": sweep_skipped,
        "failed_regions": failed_regions,
        "credential_source": source,
    }
