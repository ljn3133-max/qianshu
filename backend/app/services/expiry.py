"""到期总账：把散在 assets（ECS/RDS/域名/证书…）和
resource_pack_snapshots（资源包）里的到期时间合并成一张账，统一 30/7/1 天分级。

纯只读聚合，不给前端多张表自己拼。分级：
- expired   已过期（days < 0）
- critical  7 天内到期（紧急档固定）
- warning   warn_days 天内到期（证书行=拨测任务 cert_warn_days，默认 30；其他资产固定 30）
- ok        更远

证书行走 cert 层统一解析（app.services.cert_fact）：
同一证书实测（tls_probe）为准、订单（cas_order）为辅，两来源到期日差 >7 天带
source_conflict 标注（详情并列展示，不静默取一）。
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.models.asset import Asset
from app.models.resource_pack_snapshot import ResourcePackSnapshot
from app.services.cert_fact import (DEFAULT_WARN_DAYS, SOURCE_LABELS, cert_level,
                                    resolve_for_assets)
from app.services.cloud.provider import RESOURCE_TYPES
from app.services.ownership import apply_account_fields, resolve_one
from app.services.pack_impact import pack_chinese

logger = logging.getLogger("qianshu.expiry")

_EPOCH = datetime(1970, 1, 1)  # expire_time 空值兜底，排序时沉底

# 证书已过期超过该天数不再进到期账：只看最近 30 天内的过期证，
# 给重签/排查留出窗口；更早的历史证
# 既不占列表也不计入顶部"已过期"数字。
CERT_STALE_DAYS = 30

_PRODUCT_LABELS = {
    **RESOURCE_TYPES,
    "ecs": "ECS 云服务器", "rds": "RDS 数据库", "redis": "Redis",
    "es": "Elasticsearch", "slb": "SLB", "domain": "域名", "ssl_cert": "SSL 证书",
}

_HINTS = {
    "ssl_cert": "证书到期前 30 天可免费重签（DigiCert 免费版一年一换），别拖到当天",
    "domain": "域名到期有 30 天赎回期但费用高，提前续费",
    "ecs": "包年机器到期会停机（数据保留有限期），提前续费或确认释放计划",
    "rds": "包年实例到期自动转按量（更贵）或释放，提前续费",
    "pack": "资源包到期余量作废，看余量决定续购或调整",
}


def _parse_iso(s: str):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _level(days: int, warn_days: int = DEFAULT_WARN_DAYS) -> str:
    """到期分级统一走 cert_fact.cert_level：warn_days 消费拨测任务 cert_warn_days。"""
    return cert_level(days, warn_days)


def latest_cert_by_domain(certs: list) -> list:
    """证书账的展示口径：同域名多张证只保留最新一张。

    被替换下来的历史证不再具有到期观测意义，按域名（name，忽略大小写）取
    expire_time 最新的一张，避免旧证持续进入提醒。
    """
    best: dict = {}
    for c in certs:
        k = (c.name or "").lower()
        cur = best.get(k)
        if cur is None or (c.expire_time or _EPOCH) > (cur.expire_time or _EPOCH):
            best[k] = c
    return list(best.values())


def _ignore_set(db: Session) -> set:
    """当前生效的忽略项：until_at 为空=永久；有值且未到期=限时忽略。

    支持「忽略 30 天」，到期后自动恢复提醒，
    不用用户再手动来取消。
    """
    from sqlalchemy import or_

    from app.models.expiry_ignore import ExpiryIgnore

    now = datetime.now()
    rows = (db.query(ExpiryIgnore)
            .filter(or_(ExpiryIgnore.until_at.is_(None),
                        ExpiryIgnore.until_at > now))
            .all())
    return {r.target_key for r in rows}


def _disabled_account_ids(db: Session) -> set:
    """停用（enabled=false）的云账号 id 集合。停用账号不再采集，其资产/资源包
    是停采前的旧快照，继续进到期账会造成"永远在提醒一个已经不管的账号"。"""
    from app.models.cloud_account import CloudAccount

    return {i for (i,) in db.query(CloudAccount.id)
            .filter(CloudAccount.enabled.is_(False)).all()}


def expiry_items(db: Session, include_ignored: bool = False,
                 account_id: int = 0, include_disabled: bool = False) -> dict:
    """到期账。默认排除已忽略项，避免关闭提醒后仍重复展示；
    include_ignored=True 时返回全部并带 ignored 标记（前端"已忽略"筛选视图）。

    account_id>0：只看该云账号（顶部账号切换联动）；0=全部。
    include_disabled=False（默认）：停用账号的自动同步资产不进账，只回条数，
    人工登记（status=Manual，如外部域名）不受影响。
    """
    _ignored = _ignore_set(db)
    data = _expiry_items_raw(db, account_id=account_id,
                             include_disabled=include_disabled)
    all_items = data.get("items", [])
    for i in all_items:
        i["ignored"] = f"{i['category']}:{i['instance_id'] or i['name']}" in _ignored

    # 汇总拆成 visible/ignored 两组，前端按开关取用；否则顶部汇总会包含列表已排除的忽略项。
    def _sum(seq):
        return {
            "expired": sum(1 for i in seq if i["level"] == "expired"),
            "critical": sum(1 for i in seq if i["level"] == "critical"),
            "warning": sum(1 for i in seq if i["level"] == "warning"),
            "total": len(seq),
        }

    data["summary"] = _sum(all_items)
    data["visible_summary"] = _sum([i for i in all_items if not i["ignored"]])
    data["ignored_summary"] = _sum([i for i in all_items if i["ignored"]])
    if not include_ignored:
        data["items"] = [i for i in all_items if not i["ignored"]]
    data["ignored_count"] = len(_ignored)
    return data


def _expiry_items_raw(db: Session, account_id: int = 0,
                      include_disabled: bool = False) -> dict:
    """全量到期账：资产 + 资源包，按剩余天数升序。"""
    today = date.today()
    items: list = []
    # 不进账但需要回条数的两类：过期太久的历史证书、停用账号的旧资产
    stale_omitted = {"ssl_cert": 0, "disabled_account": 0}
    # 归属标识：account_id -> {vendor, name}（无则空）
    from app.services.tenant import account_index

    acc_map = account_index(db)
    disabled_ids = set() if include_disabled else _disabled_account_ids(db)

    asset_q = db.query(Asset).filter(
        Asset.expire_time.isnot(None), Asset.status != "Deleted")
    if account_id:
        asset_q = asset_q.filter(Asset.account_id == account_id)
    assets = asset_q.all()
    # 证书：同域名只看最新一张（历史旧证没有观测意义，见 latest_cert_by_domain），
    # 展示日期/分级/来源走统一解析（实测为准、订单为辅）
    certs = latest_cert_by_domain([a for a in assets if a.product == "ssl_cert"])
    others = [a for a in assets if a.product != "ssl_cert"]
    resolved = resolve_for_assets(db, certs)
    for a in others + certs:
        try:
            days = (a.expire_time.date() - today).days
        except AttributeError:
            continue
        # 停用账号的自动同步资产不进账（人工登记行 Manual 不受影响，见 docstring）
        if a.status != "Manual" and (a.account_id or 0) in disabled_ids:
            stale_omitted["disabled_account"] += 1
            continue
        acc = acc_map.get(getattr(a, "account_id", 0) or 0) or {}
        item = {
            "category": a.product,
            "category_label": _PRODUCT_LABELS.get(a.product, a.product),
            "name": a.name or a.instance_id,
            "instance_id": a.instance_id,
            "region": a.region or "",
            "vendor": acc.get("vendor", ""),
            "account_name": acc.get("name", ""),
            "expire_time": a.expire_time.isoformat(),
            "days_left": days,
            "level": _level(days),
            "status": a.status,
            "hint": _HINTS.get(a.product, "提前安排续费"),
        }
        # 证书行统一解析：实测为准（tls_probe）/订单为辅（cas_order），
        # 两来源日期都留痕（order_expire_time/probe_not_after），不静默取一
        f = resolved.get(a.instance_id) if a.product == "ssl_cert" else None
        if f is not None:
            item["order_expire_time"] = a.expire_time.isoformat()
            if f.not_after:
                item["expire_time"] = f.not_after.isoformat()
                days = (f.not_after.date() - today).days
                item["days_left"] = days
            item["level"] = cert_level(days, f.warn_days)
            item["cert_source"] = f.source
            item["cert_source_label"] = SOURCE_LABELS.get(f.source, f.source)
            item["cert_warn_days"] = f.warn_days
            item["source_conflict"] = bool(f.source_conflict or f.serial_mismatch)
            item["serial_mismatch"] = bool(f.serial_mismatch)
            item["diff_days"] = f.diff_days
            item["probe_not_after"] = (f.probe.not_after.isoformat()
                                       if f.probe and f.probe.not_after else None)
            item["order_not_after"] = (f.order.not_after.isoformat()
                                       if f.order and f.order.not_after else None)
        # 归属回流：vendor/account_name 过解析层（登记覆盖自动值；
        # 无登记原样，不造数据）。键位：instance/name + extra IP；域名/证书补 domain 键
        try:
            extra = json.loads(a.extra or "{}")
        except Exception:
            extra = {}
        from app.services.ipnorm import extra_ips

        ips = extra_ips(extra)   # IP 提取与归一统一走 extra_ips
        hit = resolve_one(db, instance=a.instance_id, name=a.name, ips=ips,
                          domain=a.name if a.product in ("domain", "ssl_cert") else None)
        apply_account_fields(item, hit, acc_map=acc_map)
        # 证书口径：过期超过 30 天的历史证不进账（日期已按实测/订单统一解析后的结果）
        if a.product == "ssl_cert" and item["days_left"] < -CERT_STALE_DAYS:
            stale_omitted["ssl_cert"] += 1
            continue
        items.append(item)

    # 资源包：每 instance_id 取最新快照的 expiry_time（已耗尽的包不进账——
    # 余量为 0 的包到期无可作废额度，续购决策也不看它，纯属噪音）
    latest: dict = {}
    snap_q = db.query(ResourcePackSnapshot)
    if account_id:
        snap_q = snap_q.filter(ResourcePackSnapshot.account_id == account_id)
    for s in snap_q.order_by(ResourcePackSnapshot.instance_id, ResourcePackSnapshot.snapshot_date).all():
        latest[s.instance_id] = s  # 日期升序遍历，留下的就是最新
    for s in latest.values():
        if (s.remaining_pct or 0) <= 0.01:
            continue
        if (s.account_id or 0) in disabled_ids:
            stale_omitted["disabled_account"] += 1
            continue
        et = _parse_iso(s.expiry_time)
        if et is None:
            continue
        days = (et.date() - today).days
        zh = pack_chinese(s.commodity_code, s.applicable_products)
        acc = acc_map.get(getattr(s, "account_id", 0) or 0) or {}
        item = {
            "category": "pack",
            "category_label": f"资源包·{zh.get('type_label', s.commodity_code)}",
            "name": s.instance_id,
            "instance_id": s.instance_id,
            "region": s.region or "",
            "vendor": acc.get("vendor", ""),
            "account_name": acc.get("name", ""),
            "expire_time": et.isoformat(),
            "days_left": days,
            "level": _level(days),
            "status": s.status,
            "hint": _HINTS["pack"],
        }
        # 归属回流：资源包按包 ID（instance 键）解析；无登记原样
        hit = resolve_one(db, instance=s.instance_id)
        apply_account_fields(item, hit, acc_map=acc_map)
        items.append(item)

    items.sort(key=lambda x: x["days_left"])
    summary = {
        "expired": sum(1 for i in items if i["level"] == "expired"),
        "critical": sum(1 for i in items if i["level"] == "critical"),
        "warning": sum(1 for i in items if i["level"] == "warning"),
        "total": len(items),
    }
    return {"date": today.isoformat(), "summary": summary, "items": items,
            "stale_omitted": stale_omitted}
