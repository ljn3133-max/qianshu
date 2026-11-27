"""SLB(CLB) 资产同步器：DescribeLoadBalancers 分页拉取。ALB/NLB 在后续里程碑扩展。"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.collector.base import list_regions, mark_missing_deleted, parse_time, upsert_asset
from app.collector.credential import resolve_aliyun_credential

logger = logging.getLogger("qianshu.sync.slb")


def _client(ak: str, sk: str, region_id: str):
    from alibabacloud_slb20140515.client import Client
    from alibabacloud_tea_openapi.models import Config

    cfg = Config(access_key_id=ak, access_key_secret=sk, region_id=region_id)
    cfg.endpoint = f"slb.{region_id}.aliyuncs.com"
    return Client(cfg)


def _fetch_lbs(client, region_id: str) -> list:
    from alibabacloud_slb20140515 import models as slb

    page, lbs = 1, []
    while True:
        resp = client.describe_load_balancers(
            slb.DescribeLoadBalancersRequest(region_id=region_id, page_size=100, page_number=page)
        )
        body = resp.body
        batch = body.load_balancers.load_balancer if body.load_balancers else []
        lbs.extend(batch)
        if not batch or len(lbs) >= (body.total_count or 0):
            break
        page += 1
    return lbs



def _fetch_backends(client, lb_id: str) -> list:
    """SLB 后端挂载（拓扑 SLB→ECS 边的数据源）。单实例失败不影响整体同步。

    CLB 后端常**全挂在虚拟服务器组（VServerGroup）**，默认服务器组可能为空；
    只采默认组会漏掉真实后端。两处都采，带组名。护栏 50 条防超大监听组。
    """
    from alibabacloud_slb20140515 import models as slb

    def _one(srv, group: str) -> dict:
        return {"server_id": srv.server_id or "", "ip": srv.server_ip or "",
                "port": srv.port or 0, "weight": srv.weight or 0,
                "type": srv.type or "", "group": group}

    out: list = []
    # 1) 默认服务器组
    try:
        body = client.describe_load_balancer_attribute(
            slb.DescribeLoadBalancerAttributeRequest(load_balancer_id=lb_id)).body
        servers = (body.backend_servers.backend_server if body.backend_servers else []) or []
        out.extend(_one(s, "default") for s in servers)
    except Exception as e:
        logger.warning("SLB 默认组采集失败 %s: %s", lb_id, str(e)[:80])
    # 2) 虚拟服务器组（真实后端大本营）
    try:
        vs = client.describe_vserver_groups(
            slb.DescribeVServerGroupsRequest(load_balancer_id=lb_id)).body
        groups = (vs.vserver_groups.vserver_group if vs.vserver_groups else []) or []
        for g in groups[:20]:
            try:
                ga = client.describe_vserver_group_attribute(
                    slb.DescribeVServerGroupAttributeRequest(
                        vserver_group_id=g.vserver_group_id)).body
                gs = (ga.backend_servers.backend_server if ga.backend_servers else []) or []
                out.extend(_one(s, g.vserver_group_name or g.vserver_group_id) for s in gs)
            except Exception as e:
                logger.info("SLB 虚拟组 %s 采集跳过: %s", g.vserver_group_id, str(e)[:60])
    except Exception as e:
        logger.warning("SLB 虚拟服务器组列表失败 %s: %s", lb_id, str(e)[:80])
    return out[:50]


def sync_slb(db: Session) -> dict:
    """全量同步 SLB。返回 {total, created, updated, missing_marked, failed_regions}。"""
    ak, sk, source = resolve_aliyun_credential(db)

    seen_ids: set = set()
    created = updated = 0
    failed_regions = []
    for region_id in list_regions(ak, sk):
        try:
            client = _client(ak, sk, region_id)
            lbs = _fetch_lbs(client, region_id)
        except Exception as e:
            failed_regions.append(f"{region_id}: {str(e)[:80]}")
            continue

        for lb in lbs:
            m = lb.to_map()
            tags = [
                {"key": t.get("TagKey", ""), "value": t.get("TagValue", "")}
                for t in (m.get("Tags") or {}).get("Tag", [])
            ]
            extra = {
                "address": m.get("Address", ""),
                "address_type": m.get("AddressType", ""),            # internet/intranet
                "network_type": m.get("NetworkType", ""),            # vpc/classic
                "bandwidth": m.get("Bandwidth", 0),
                "spec": m.get("LoadBalancerSpec", ""),
                "instance_charge_type": m.get("InstanceChargeType", ""),
                "internet_charge_type": m.get("InternetChargeTypeAlias", ""),
                "master_zone": m.get("MasterZoneId", ""),
                "slave_zone": m.get("SlaveZoneId", ""),
                "delete_protection": m.get("DeleteProtection", ""),
                "vpc_id": m.get("VpcId", ""),
                "vswitch_id": m.get("VSwitchId", ""),
                # 拓扑边：SLB→后端挂载。护栏 50 个，防超大监听组撑爆 extra
                "backends": _fetch_backends(client, m["LoadBalancerId"])[:50],
            }
            fields = dict(
                name=m.get("LoadBalancerName") or m.get("LoadBalancerId", ""),
                region=region_id,
                zone=m.get("MasterZoneId", ""),
                status=m.get("LoadBalancerStatus", ""),
                instance_type=m.get("LoadBalancerSpec", ""),
                charge_type=m.get("PayType", ""),
                expire_time=parse_time(m.get("EndTime")) if m.get("EndTime") else None,
                tags=json.dumps(tags, ensure_ascii=False),
                extra=json.dumps(extra, ensure_ascii=False),
                raw_meta="",
                synced_at=datetime.now(),
            )
            if upsert_asset(db, "slb", m["LoadBalancerId"], seen_ids, fields) == "created":
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
        logger.warning("SLB 存在失败 region，跳过缺失清扫: %s", failed_regions)
    else:
        missing_marked = mark_missing_deleted(db, "slb", seen_ids)
    db.commit()
    logger.info(
        "SLB 同步完成(凭证 %s): total=%d created=%d updated=%d deleted=%d failed_regions=%d",
        source, len(seen_ids), created, updated, missing_marked, len(failed_regions),
    )
    return {
        "total": len(seen_ids), "created": created, "updated": updated,
        "missing_marked": missing_marked, "sweep_skipped": sweep_skipped,
        "failed_regions": failed_regions,
        "credential_source": source,
    }
