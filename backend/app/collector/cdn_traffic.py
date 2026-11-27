"""CDN/DCDN 按域名每日流量采集器。

CDN 流量包（dcdnpaybag）抵扣 cdn/cdn_S77/dcdn 下行流量，池口径只有总量，
不能定位到域名；按量账单 T+1 且实例维度只有账号 ID。
本采集器用 CDN 域名维度的监控 API 补上这层观测（全部只读）：

- 域名清单：DescribeUserDomains（cdn.aliyuncs.com 2018-05-10）
            + DescribeDcdnUserDomains（dcdn.aliyuncs.com 2018-01-15）
- 每日流量：cdn    DescribeDomainFlowData（cdn.aliyuncs.com **2014-11-11**，
            该 action 在 2018-05-10 是 404，必须用老版本；小时点求和）
            dcdn   DescribeDcdnDomainTrafficData（2018-01-15）
- 口径：北京自然日（前一日 16:00Z ~ 当日 15:59:59Z），与资源包快照/账单的
  自然日对齐；域名监控合计与池差分可能因抵扣精度和监控粒度边界而有差异。

注意：本模块只管"流量观测"，CDN 域名资产同步在 collector/cdn.py（sync_cdn）。
幂等：唯一键 (stat_date, domain)，重拉覆盖。域名当日无数据也落 0 行，
"有行"代表该域名当天存在。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app.collector.credential import resolve_aliyun_credential
from app.collector.domain import _client, _rpc
from app.models.cdn_domain_daily import CdnDomainDaily

logger = logging.getLogger("qianshu.sync.cdn_traffic")


def _list_domains(client) -> list:
    """返回 [(domain, product)]，cdn + dcdn 全量（翻页）。"""
    out: list = []
    page = 1
    while True:
        r = _rpc(client, "cdn.aliyuncs.com", "2018-05-10", "DescribeUserDomains",
                 {"PageNumber": page, "PageSize": 100})
        batch = (r.get("Domains") or {}).get("PageData") or []
        out += [(d["DomainName"], "cdn") for d in batch if d.get("DomainName")]
        if not batch or len(out) >= int(r.get("TotalCount") or 0):
            break
        page += 1
    page, dcdn_count = 1, 0
    while True:
        r = _rpc(client, "dcdn.aliyuncs.com", "2018-01-15", "DescribeDcdnUserDomains",
                 {"PageNumber": page, "PageSize": 100})
        batch = (r.get("Domains") or {}).get("PageData") or []
        dcdn_count += len(batch)
        out += [(d["DomainName"], "dcdn") for d in batch if d.get("DomainName")]
        if not batch or dcdn_count >= int(r.get("TotalCount") or 0):
            break
        page += 1
    return out


def _day_traffic(client, domain: str, product: str, day: date) -> int:
    """一个域名一个北京自然日的流量（字节）。域名在该日不存在则抛异常由调用方跳过。"""
    # 北京日 00:00/23:59:59 = UTC 前一日 16:00 / 当日 15:59:59
    st = f"{(day - timedelta(days=1)).isoformat()}T16:00:00Z"
    et = f"{day.isoformat()}T15:59:59Z"
    if product == "dcdn":
        r = _rpc(client, "dcdn.aliyuncs.com", "2018-01-15",
                 "DescribeDcdnDomainTrafficData",
                 {"DomainName": domain, "StartTime": st, "EndTime": et})
        mods = (r.get("TrafficDataPerInterval") or {}).get("DataModule") or []
        return int(sum(float(m.get("Traffic") or 0) for m in mods))
    r = _rpc(client, "cdn.aliyuncs.com", "2014-11-11", "DescribeDomainFlowData",
             {"DomainName": domain, "StartTime": st, "EndTime": et,
              "Interval": "3600"})
    mods = (r.get("FlowDataPerInterval") or {}).get("DataModule") or []
    return int(sum(float(m.get("Value") or 0) for m in mods))


def sync_cdn_traffic(db: Session, day: date, domains: list | None = None) -> dict:
    """采集 day（北京自然日）全部 CDN/DCDN 域名流量，upsert 进 cdn_domain_daily。

    domains 可传入 [(domain, product)] 复用清单（回补多日时避免重复拉清单）。
    返回 {day, domains, total_bytes, failed}。单个域名当日数据缺失（如域名
    晚于该日才创建）只跳过不中断。
    """
    ak, sk, _src = resolve_aliyun_credential(db)
    client = _client(ak, sk)
    if domains is None:
        domains = _list_domains(client)

    now = datetime.now()
    total, failed = 0, []
    for domain, product in domains:
        try:
            nbytes = _day_traffic(client, domain, product, day)
        except Exception as e:  # 域名当日不存在/单域名 API 抖动
            logger.info("CDN 域名 %s %s 无数据: %s", domain, day, str(e)[:100])
            failed.append(domain)
            continue
        total += nbytes
        row = db.query(CdnDomainDaily).filter(
            CdnDomainDaily.stat_date == day, CdnDomainDaily.domain == domain).first()
        if row is None:
            from app.services.tenant import account_id as _acc   # 多账号打标

            db.add(CdnDomainDaily(stat_date=day, account_id=_acc(db), domain=domain,
                                  product=product, traffic_bytes=nbytes, collected_at=now))
        else:
            row.product = product
            row.traffic_bytes = nbytes
            row.collected_at = now
    db.commit()
    logger.info("CDN 域名流量 %s: %d 域名合计 %.2fGB, 跳过 %d",
                day, len(domains) - len(failed), total / 1024 ** 3, len(failed))
    return {"day": day.isoformat(), "domains": len(domains),
            "total_bytes": total, "failed": failed}


def sync_cdn_traffic_backfill(db: Session, days: int) -> list:
    """回补最近 days 个自然日（含昨天，今天数据不全不采）。返回每日摘要列表。"""
    ak, sk, _src = resolve_aliyun_credential(db)
    domains = _list_domains(_client(ak, sk))
    out = []
    for i in range(days, 0, -1):
        day = date.today() - timedelta(days=i)
        out.append(sync_cdn_traffic(db, day, domains))
    return out
