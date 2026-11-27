"""ES(阿里云 Elasticsearch) 资产同步器：ListInstance 按 region 遍历。

ES 只在部分 region 提供，先用 DNS 预检跳过不支持的 region（避免每次同步
都等一堆连接超时）；"region 未开通"类报错不算失败。DNS 通不代表 ES 可用：
部分 region 的通配解析会返回地址但连接被对端关闭，这类连接级错误按
“地域不可达”处理——重试一次后跳过该地域，不把整个产品判失败。
连地域清单（ECS DescribeRegions）本身断连也按“本轮跳过”处理，不清扫存量资产。
"""
from __future__ import annotations

import json
import logging
import socket
import time
from datetime import datetime

from sqlalchemy.orm import Session

from app.collector.base import (
    is_connectivity_error,
    list_regions,
    mark_missing_deleted,
    parse_time,
    upsert_asset,
)
from app.collector.credential import resolve_aliyun_credential


def _dns_ok(host: str) -> bool:
    try:
        socket.gethostbyname(host)
        return True
    except OSError:
        return False

logger = logging.getLogger("qianshu.sync.es")


def _client(ak: str, sk: str, region_id: str):
    from alibabacloud_elasticsearch20170613.client import Client
    from alibabacloud_tea_openapi.models import Config

    cfg = Config(access_key_id=ak, access_key_secret=sk, region_id=region_id)
    cfg.endpoint = f"elasticsearch.{region_id}.aliyuncs.com"
    return Client(cfg)


# 兼容旧引用：连接级判断统一在 base，各采集器共用一套口径。
_is_connectivity_error = is_connectivity_error


def sync_es(db: Session) -> dict:
    """全量同步 Elasticsearch 实例。返回统计信息。"""
    ak, sk, source = resolve_aliyun_credential(db)
    from alibabacloud_elasticsearch20170613 import models

    seen_ids: set = set()
    created = updated = 0
    failed_regions: list[str] = []
    unavailable_regions: list[str] = []
    reachable_regions = 0
    try:
        regions = list_regions(ak, sk)
    except Exception as e:
        # 连地域清单都拿不到（RetryError/断连）：本轮跳过但不清扫、不判产品失败，
        # 避免一次瞬时抖动把已有 ES 资产标 Deleted 或卡住整个接入流程。
        if not is_connectivity_error(e):
            raise
        logger.warning("ES 地域清单获取失败（连接级），本轮跳过且未清理: %s",
                       str(e)[:120])
        return {
            "total": 0, "created": 0, "updated": 0,
            "missing_marked": 0, "sweep_skipped": True,
            # 没拿到任何地域=本轮没有可信的 ES 结果，标 skipped 而非 ready；
            # 不能算作“至少一个产品可用”，也不能触发缺失清扫。
            "skipped": True,
            "failed_regions": [],
            "unavailable_regions": ["region-list"],
            "note": "地域清单暂时不可达，本轮跳过 ES 同步且未清理存量资产",
            "credential_source": source,
        }
    for region_id in regions:
        if not _dns_ok(f"elasticsearch.{region_id}.aliyuncs.com"):
            continue  # ES 未在该 region 提供
        instances = None
        last_error: Exception | None = None
        for attempt in (1, 2):
            try:
                body = _client(ak, sk, region_id).list_instance(
                    models.ListInstanceRequest(size=50, page=1)
                ).body
                instances = body.result or []
                reachable_regions += 1
                break
            except Exception as e:
                # RegionNotSupport / 未开通属预期，不算失败
                if "RegionNotSupport" in str(e) or "not activated" in str(e):
                    break
                last_error = e
                # 连接被对端关闭多数是地域未提供 ES 或瞬时抖动：重试一次；
                # 仍失败则按“地域不可达”记录，不把整个 ES 产品判失败。
                if attempt == 1 and _is_connectivity_error(e):
                    time.sleep(0.5)
                    continue
                break
        if instances is None:
            if last_error is None:
                continue  # RegionNotSupport / 未开通
            detail = f"{region_id}: {str(last_error)[:80]}"
            if _is_connectivity_error(last_error):
                unavailable_regions.append(detail)
            else:
                failed_regions.append(detail)
            continue

        for inst in instances:
            m = inst.to_map()
            node_spec = m.get("nodeSpec") or {}
            extra = {
                "es_version": m.get("esVersion", ""),
                "node_amount": m.get("nodeAmount", 0),
                "node_spec": node_spec.get("spec", ""),
                "spec_info": node_spec.get("specInfo", ""),
                "disk_gb": node_spec.get("disk", 0),
                "disk_type": node_spec.get("diskType", ""),
                "port": m.get("port", 0),
                "payment_type": m.get("paymentType", ""),
            }
            fields = dict(
                name=m.get("description") or m["instanceId"],
                region=region_id,
                zone=m.get("zoneId", "") or "",
                status=m.get("status", ""),
                instance_type=node_spec.get("spec", ""),
                charge_type=m.get("paymentType", ""),
                expire_time=parse_time(m.get("endTime")),
                tags="[]",
                extra=json.dumps(extra, ensure_ascii=False),
                raw_meta="",
                synced_at=datetime.now(),
            )
            if upsert_asset(db, "es", m["instanceId"], seen_ids, fields) == "created":
                created += 1
            else:
                updated += 1

    if unavailable_regions and reachable_regions == 0:
        # 所有可达请求都断连：这是整体连通性问题，不能把空清单当成“无 ES”，
        # 否则会把产品伪装成成功并清掉存量资产。
        failed_regions.extend(unavailable_regions)
        unavailable_regions = []

    # 清扫守卫：任一 region 请求失败=该 region 清单不可信，
    # 跳过缺失清扫防把整片实例误标 Deleted；全 region 成功才收口清扫（保守口径：
    # 宁可少清一轮，不可误清资产）。DNS 预检跳过/RegionNotSupport 属"该 region 无 ES"
    # 已确认为空，不算失败；连接不可达的地域同样跳过清扫，避免误清。
    missing_marked = 0
    sweep_skipped = False
    if failed_regions or unavailable_regions:
        sweep_skipped = True
        if failed_regions:
            logger.warning("ES 存在失败 region，跳过缺失清扫: %s", failed_regions)
        if unavailable_regions:
            logger.warning("ES 地域暂不可达，跳过该地域：%s", unavailable_regions)
    else:
        missing_marked = mark_missing_deleted(db, "es", seen_ids)
    db.commit()
    logger.info(
        "ES 同步完成(凭证 %s): total=%d created=%d updated=%d deleted=%d "
        "failed_regions=%d unavailable_regions=%d",
        source, len(seen_ids), created, updated, missing_marked,
        len(failed_regions), len(unavailable_regions),
    )
    note = ""
    if unavailable_regions:
        note = (f"{len(unavailable_regions)} 个地域暂时不可达，已跳过且未清理"
                f"该地域资产")
    return {
        "total": len(seen_ids), "created": created, "updated": updated,
        "missing_marked": missing_marked, "sweep_skipped": sweep_skipped,
        "failed_regions": failed_regions,
        "unavailable_regions": unavailable_regions,
        "note": note,
        "credential_source": source,
    }
