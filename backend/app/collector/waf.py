"""WAF 资产同步器：DescribeInstance 拿实例 + DescribeDomains 拿接入域名数。

注意：WAF 按量实例无固定到期时间（EndTime 是占位极大值），expire 置空。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.collector.base import mark_missing_deleted, parse_time, upsert_asset
from app.collector.credential import resolve_aliyun_credential

logger = logging.getLogger("qianshu.sync.waf")

# WAF 3.0 OpenAPI 的两个服务区域（中国大陆 / 海外）
WAF_REGIONS = ("cn-hangzhou", "ap-southeast-1")
# Status 数字码 -> 统一状态
WAF_STATUS = {0: "Stopped", 1: "Normal", 2: "Locked"}


def sync_waf(db: Session) -> dict:
    """全量同步 WAF 实例。返回统计信息。"""
    ak, sk, source = resolve_aliyun_credential(db)
    from alibabacloud_waf_openapi20211001 import models
    from alibabacloud_waf_openapi20211001.client import Client
    from alibabacloud_tea_openapi.models import Config

    seen_ids: set = set()
    created = updated = 0
    failed_regions = []
    for region in WAF_REGIONS:
        cfg = Config(access_key_id=ak, access_key_secret=sk, region_id=region)
        cfg.endpoint = f"wafopenapi.{region}.aliyuncs.com"
        client = Client(cfg)
        try:
            inst = client.describe_instance(
                models.DescribeInstanceRequest()
            ).body.to_map()
        except Exception as e:
            # 无实例的区域会报错，属预期
            failed_regions.append(f"{region}: {str(e)[:80]}")
            continue

        instance_id = inst.get("InstanceId", "")
        if not instance_id:
            continue

        # 接入域名数
        domain_count = 0
        try:
            domains = client.describe_domains(
                models.DescribeDomainsRequest(
                    instance_id=instance_id, page_size=100, page_number=1
                )
            ).body.to_map()
            domain_count = domains.get("TotalCount", 0) or 0
        except Exception as e:
            logger.warning("WAF 域名列表查询失败: %s", str(e)[:120])

        pay_type = inst.get("PayType", "")
        extra = {
            "edition": inst.get("Edition", ""),
            "pay_type": pay_type,
            "domain_count": domain_count,
            "waf_version": "2.0" if instance_id.startswith("waf_v2") else "3.0",
            "start_time": inst.get("StartTime"),
            "in_debt": inst.get("InDebt"),
        }
        fields = dict(
            name=instance_id,
            region=inst.get("RegionId", region),
            zone="",
            status=WAF_STATUS.get(inst.get("Status"), str(inst.get("Status", ""))),
            instance_type=inst.get("Edition", ""),
            charge_type="POSTPAY" if pay_type == "POSTPAY" else pay_type,
            expire_time=parse_time(inst.get("EndTime")) if pay_type != "POSTPAY" else None,
            tags="[]",
            extra=json.dumps(extra, ensure_ascii=False),
            raw_meta="",
            synced_at=datetime.now(),
        )
        if upsert_asset(db, "waf", instance_id, seen_ids, fields) == "created":
            created += 1
        else:
            updated += 1

    # 清扫守卫：任一 region 请求失败=该 region 清单不可信，跳过
    # 缺失清扫防把整片实例误标 Deleted；全 region 成功才收口清扫（保守口径：宁可少清
    # 一轮，不可误清资产）。注意 WAF 无实例区域会报 InvalidInstanceId/NotFound——那是
    # "确认为空"不是失败，不进 real_failures（清扫照常），只有真实失败才拦清扫。
    real_failures = [f for f in failed_regions
                     if "InvalidInstanceId" not in f and "NotFound" not in f]
    missing_marked = 0
    sweep_skipped = False
    if real_failures:
        sweep_skipped = True
        logger.warning("WAF 存在失败 region，跳过缺失清扫: %s", real_failures)
    else:
        missing_marked = mark_missing_deleted(db, "waf", seen_ids)
    db.commit()
    logger.info(
        "WAF 同步完成(凭证 %s): total=%d created=%d updated=%d deleted=%d",
        source, len(seen_ids), created, updated, missing_marked,
    )
    return {
        "total": len(seen_ids), "created": created, "updated": updated,
        "missing_marked": missing_marked, "sweep_skipped": sweep_skipped,
        "failed_regions": real_failures,
        "credential_source": source,
    }
