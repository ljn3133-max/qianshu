"""域名与 SSL 证书资产同步器。

无对应 v2 SDK（未安装 alibabacloud_domain / alibabacloud_cas），改用
aliyunsdkcore 通用 RPC（都是只读列表 API）：
- 域名：QueryDomainList（domain.aliyuncs.com，2018-01-29）
  本账号查询结果可能为空（域名在别的账号购买）——0 条不算失败。
- 证书：ListUserCertificateOrder（cas.aliyuncs.com，2020-04-07）
  注意版本 2020-04-07 没有 DescribeUserCertificateList（那是老版 2018-05-13
  的 action，此端点已不接受），证书起止时间是毫秒时间戳。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.collector.base import mark_missing_deleted, parse_time, upsert_asset
from app.collector.credential import resolve_aliyun_credential

logger = logging.getLogger("qianshu.sync.domain")


def _rpc(client, endpoint: str, version: str, action: str, params: dict) -> dict:
    from aliyunsdkcore.request import CommonRequest

    req = CommonRequest()
    req.set_domain(endpoint)
    req.set_version(version)
    req.set_action_name(action)
    for k, v in params.items():
        req.add_query_param(k, str(v))
    return json.loads(client.do_action_with_exception(req))


def _client(ak: str, sk: str):
    from aliyunsdkcore.client import AcsClient

    return AcsClient(ak, sk, "cn-hangzhou")


def sync_domains(db: Session) -> dict:
    """同步账号下注册的域名（到期时间进 assets.expire_time）。"""
    ak, sk, source = resolve_aliyun_credential(db)
    client = _client(ak, sk)

    page, domains = 1, []
    failed_regions: list = []
    try:
        while True:
            r = _rpc(client, "domain.aliyuncs.com", "2018-01-29", "QueryDomainList",
                     {"PageNum": page, "PageSize": 50})
            batch = ((r.get("Data") or {}).get("Domain")) or []
            domains.extend(batch)
            total = int(r.get("TotalItemNum") or 0)
            if not batch or len(domains) >= total:
                break
            page += 1
    except Exception as e:
        # 清扫守卫：域名清单是全局单清单（不分 region）——拉取
        # 失败/分页中断=清单不完整，记入 failed_regions 参与清扫守卫判断（防域名被误标
        # Deleted）。注意：查询结果为空（域名在别的账号购买）是正常态，不算失败。
        failed_regions.append(f"global: {str(e)[:80]}")

    seen_ids: set = set()
    created = updated = 0
    for d in domains:
        fields = dict(
            name=d.get("DomainName", ""),
            region="global",
            zone="",
            status=d.get("DomainStatus") or d.get("Status") or "",
            instance_type="",
            charge_type="PrePaid",
            expire_time=parse_time(d.get("ExpirationDate")),
            tags="[]",
            extra=json.dumps({
                "registration_date": d.get("RegistrationDate", ""),
                "premium": d.get("Premium"),
                "real_name_status": d.get("RealNameStatus", ""),
                "registrant_organization": d.get("RegistrantOrganization", ""),
            }, ensure_ascii=False),
            raw_meta="",
            synced_at=datetime.now(),
        )
        if upsert_asset(db, "domain", d.get("InstanceId") or d.get("DomainName", ""), seen_ids, fields) == "created":
            created += 1
        else:
            updated += 1

    # 清扫守卫：清单拉取不完整=清单不可信，跳过缺失清扫；
    # 全量成功才收口清扫（保守口径：宁可少清一轮，不可误清资产）。
    missing_marked = 0
    sweep_skipped = False
    if failed_regions:
        sweep_skipped = True
        logger.warning("域名清单拉取不完整，跳过缺失清扫: %s", failed_regions)
    else:
        missing_marked = mark_missing_deleted(db, "domain", seen_ids)
    db.commit()
    logger.info("域名同步完成(凭证 %s): total=%d created=%d updated=%d deleted=%d",
                source, len(seen_ids), created, updated, missing_marked)
    return {
        "total": len(seen_ids), "created": created, "updated": updated,
        "missing_marked": missing_marked, "sweep_skipped": sweep_skipped,
        "failed_regions": failed_regions,
        "credential_source": source,
    }


def sync_certs(db: Session) -> dict:
    """同步 SSL 证书订单（含免费证书；到期时间进 assets.expire_time）。"""
    ak, sk, source = resolve_aliyun_credential(db)
    client = _client(ak, sk)

    page, certs = 1, []
    failed_regions: list = []
    try:
        while True:
            r = _rpc(client, "cas.aliyuncs.com", "2020-04-07", "ListUserCertificateOrder",
                     {"CurrentPage": page, "ShowSize": 50})
            batch = r.get("CertificateOrderList") or []
            certs.extend(batch)
            total = int(r.get("TotalCount") or 0)
            if not batch or len(certs) >= total:
                break
            page += 1
    except Exception as e:
        # 清扫守卫：证书清单是全局单清单（不分 region）——拉取
        # 失败/分页中断=清单不完整，记入 failed_regions 参与清扫守卫判断（防证书被误标
        # Deleted）。注意：证书清单为空（本账号无证书订单）是正常态，不算失败。
        failed_regions.append(f"global: {str(e)[:80]}")

    seen_ids: set = set()
    created = updated = 0
    for c in certs:
        fields = dict(
            name=c.get("Domain", "") or f"cert-{c.get('OrderId', '')}",
            region="global",
            zone="",
            status=c.get("Status", ""),            # ISSUED / EXPIRED ...
            instance_type=c.get("ProductName", ""),
            charge_type="PrePaid",
            expire_time=parse_time(c.get("CertEndTime")),   # 毫秒时间戳
            tags="[]",
            extra=json.dumps({
                "cert_type": c.get("CertType", ""),
                "root_brand": c.get("RootBrand", ""),
                "algorithm": c.get("Algorithm", ""),
                "domain_count": c.get("DomainCount"),
                "wild_domain_count": c.get("WildDomainCount"),
                "cert_start_time": c.get("CertStartTime"),
                "order_id": c.get("OrderId"),
            }, ensure_ascii=False),
            raw_meta="",
            synced_at=datetime.now(),
        )
        if upsert_asset(db, "ssl_cert", str(c.get("InstanceId") or c.get("OrderId", "")), seen_ids, fields) == "created":
            created += 1
        else:
            updated += 1

    # 清扫守卫：清单拉取不完整=清单不可信，跳过缺失清扫；
    # 全量成功才收口清扫（保守口径：宁可少清一轮，不可误清资产）。
    missing_marked = 0
    sweep_skipped = False
    if failed_regions:
        sweep_skipped = True
        logger.warning("证书清单拉取不完整，跳过缺失清扫: %s", failed_regions)
    else:
        missing_marked = mark_missing_deleted(db, "ssl_cert", seen_ids)
    db.commit()
    logger.info("证书同步完成(凭证 %s): total=%d created=%d updated=%d deleted=%d",
                source, len(seen_ids), created, updated, missing_marked)
    return {
        "total": len(seen_ids), "created": created, "updated": updated,
        "missing_marked": missing_marked, "sweep_skipped": sweep_skipped,
        "failed_regions": failed_regions,
        "credential_source": source,
    }
