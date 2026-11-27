"""资源包 -> 按量费用 影响关联。

流量包 applicable_products 声明了抵扣范围（slb/ecs/eip...）。本模块把该范围用于
产品码可读化，并关联"包耗尽"与"按量费用上涨"，避免两类事件各自报警却无法互相解释。

本模块提供三层能力：
1) 包/产品码中文可读化（pack_chinese / products_label）；
2) 已耗尽包影响量化：耗尽日前后 covered products 按量日均对比（impact_analysis）；
3) payg 突增与 ±3 天内耗尽的覆盖包互相指认（related_pack_exhaustions）。

注意：多包先后耗尽时，后一个包的 before 窗口天然包含前一个包的影响，
增量对比依然正确；耗尽日早于账单数据范围时 impact 返回 unavailable。
"""
from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.cost_anomaly import CostAnomaly
from app.models.cost_daily import CostDaily
from app.models.resource_pack_snapshot import ResourcePackSnapshot

logger = logging.getLogger("qianshu.pack_impact")

# impact_analysis 进程内缓存（账单 T+1，5 分钟内结果不变）
_IMPACT_CACHE_TTL = 300.0
_impact_cache: dict = {}

# 资源包类型 -> (中文名, 抵扣范围描述)
PACK_LABELS: dict = {
    "flowbag": ("通用流量包", "公网流量（抵扣后按量流量费不再收）"),
    "dcdnpaybag": ("CDN/DCDN 流量包", "CDN 与全站加速下行流量"),
    "ossbag": ("OSS 资源包", "OSS 存储/流量/请求"),
    "sls_plan_bag": ("日志服务资源包", "SLS 读写/存储 CU"),
    "waf_secu_dp_cn": ("WAF 容量包", "WAF 实例 CU"),
    "csk_ackpro_dp_cn": ("ACK Pro 资源包", "容器服务 ACK Pro CU"),
    "slb_clbcubag_dp_cn": ("SLB 容量包", "SLB 容量单元 CU"),
}

# 产品码 -> 中文名（适用产品串 + 异动关联共用；未收录的回落原码）
PRODUCT_LABELS: dict = {
    "slb": "负载均衡", "slb_cainiao": "负载均衡",
    "ecs": "云服务器", "vm": "云服务器", "ecs_cainiao": "云服务器", "vm_cainiao": "云服务器",
    "eip": "弹性公网IP", "eip_cainiao": "弹性公网IP",
    "ipv6bandwidth": "IPv6 带宽",
    "bandwidth_package": "带宽包", "bandwidth_package_cainiao": "带宽包",
    "cbwp": "云数据传输", "cbwp_cainiao": "云数据传输",
    "cdt_DataTransfer_public_cn": "云数据传输", "cdt_internet_public_cn": "云数据传输",
    "cdn": "CDN", "cdn_S77": "CDN", "dcdn": "全站加速",
    "oss": "对象存储", "oss_cainiao": "对象存储", "oss_jushita": "对象存储", "oss_mayi": "对象存储",
    "sls": "日志服务", "sls_cainiao": "日志服务", "sls_dataingest_public_cn": "日志服务",
    "sls_jushita": "日志服务", "sls_mayi": "日志服务",
    "waf_v2_public_cn": "WAF 防火墙",
    "csk_propayasgo_public_cn": "容器服务 ACK Pro",
}


def pack_chinese(commodity_code: str, applicable_products: str) -> dict:
    """包类型中文 + 适用产品中文标签（Top3 + 等 N 项）。"""
    name, scope = PACK_LABELS.get(commodity_code or "", (commodity_code or "资源包", ""))
    prods = [p for p in (applicable_products or "").split(",") if p]
    zh = [PRODUCT_LABELS.get(p, p) for p in prods]
    # 去重保持顺序（loadbalancer 等中文别名重复）
    seen, uniq = set(), []
    for z in zh:
        if z not in seen:
            seen.add(z)
            uniq.append(z)
    label = "、".join(uniq[:3]) + (f" 等{len(uniq)}项" if len(uniq) > 3 else "")
    return {"type_label": name, "scope_label": scope, "products_label": label or "-"}


def covered_products(applicable_products: str) -> set:
    return {p for p in (applicable_products or "").split(",") if p}


def pool_key(commodity_code: str, applicable_products: str) -> str:
    """抵扣能力池键：同类型且同抵扣范围的包共享额度，告警单元为池而非单包。"""
    return f"{commodity_code or 'unknown'}|{applicable_products or '-'}"


_SIZE_UNITS = {"BYTE": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3,
               "TB": 1024 ** 4, "PB": 1024 ** 5}


def norm_amount(v: float, unit: str) -> float:
    """归一化到基准单位（容量类转 Byte，计数类原值），保证跨单位可比较。"""
    u = (unit or "").strip().upper()
    return v * _SIZE_UNITS.get(u, 1)


def compute_pools(snaps: list, rates: dict) -> dict:
    """把包快照按池分组，算池级剩余/消耗/耗尽预测。

    snaps: ResourcePackSnapshot 列表（同一快照日）；
    rates: {instance_id: 日消耗率 %/天}（_pack_rate 的结果）。
    返回 {pool_key: {key, commodity_code, products, packs: [instance_id],
                     remaining_pct, daily_use, days_left, unit}}。
    days_left = 池总剩余量 / 池总日消耗量；池无消耗时为 None（不会耗尽）。

    量与速率全部用 norm_amount 归一化后再聚合；同一池可能同时存在 TB、GB 等单位，
    原始值直接相加会得到错误余量与消耗率。
    展示用 daily_use 按池内首个包的单位回折。
    """
    pools: dict = {}
    for s in snaps:
        k = pool_key(s.commodity_code, s.applicable_products)
        p = pools.setdefault(k, {
            "key": k, "commodity_code": s.commodity_code,
            "products": s.applicable_products or "", "packs": [],
            "rem": 0.0, "total": 0.0, "daily_use": 0.0, "unit": s.total_unit or "",
        })
        p["packs"].append(s.instance_id)
        p["rem"] += norm_amount(s.remaining_amount or 0.0, s.remaining_unit)
        p["total"] += norm_amount(s.total_amount or 0.0, s.total_unit)
        rate = rates.get(s.instance_id, 0.0)
        # 前瞻日消耗只统计还有余量的包——已耗尽的包不可能再消耗，
        # 把它们的历史速率算进去会虚增池日消耗（实测 CDN 池：8 个死包
        # 的兜底速率合计 315GB/天，把真实 ~14 天耗尽估成 4.2 天，误报 critical）
        if rate > 0 and s.total_amount and (s.remaining_amount or 0) > 0.01:
            # 日消耗率 % × 该包归一化总量 = 日消耗绝对量（消耗只发生在有余量的包上）
            p["daily_use"] += rate / 100.0 * norm_amount(s.total_amount, s.total_unit)
    out = {}
    for k, p in pools.items():
        rem_pct = (p["rem"] / p["total"] * 100.0) if p["total"] > 0 else 0.0
        days_left = (p["rem"] / p["daily_use"]) if p["daily_use"] > 0 and p["rem"] > 0 else None
        # 回折到池内首个包的单位，仅用于展示
        factor = _SIZE_UNITS.get((p["unit"] or "").strip().upper(), 1)
        out[k] = {
            "key": k, "commodity_code": p["commodity_code"], "products": p["products"],
            "packs": p["packs"], "remaining_pct": round(rem_pct, 2),
            "daily_use": round(p["daily_use"] / factor, 4), "days_left": days_left,
            "unit": p["unit"],
        }
    return out


def find_exhaust_date(db: Session, instance_id: str) -> Optional[date]:
    """从快照历史找耗尽日：余量>0.5% 的最后一个快照日的次日（首个 0 快照日）。"""
    rows = (
        db.query(ResourcePackSnapshot.snapshot_date, ResourcePackSnapshot.remaining_pct)
        .filter(ResourcePackSnapshot.instance_id == instance_id)
        .order_by(ResourcePackSnapshot.snapshot_date)
        .all()
    )
    for i in range(1, len(rows)):
        if rows[i - 1][1] > 0.5 and rows[i][1] <= 0.01:
            return rows[i][0]
    return None


def _daily_avg(db: Session, products: set, d_from: date, d_to: date) -> tuple:
    """[d_from, d_to] 闭区间内 covered products 的按量日均与按实例聚合。

    金额口径：pretax_amount（账单金额）是判断花费的唯一口径；
    payment_amount 是挂账现金字段（常 0），不作花费依据。
    返回 (日均金额, by_inst)，by_inst 每项 amount=账单金额合计。
    """
    if not products or d_from > d_to:
        return 0.0, {}
    rows = (
        db.query(
            CostDaily.stat_date,
            CostDaily.instance_id,
            CostDaily.instance_name,
            func.sum(CostDaily.pretax_amount),
        )
        .filter(
            CostDaily.source == "bill",
            CostDaily.subscription_type == "PayAsYouGo",
            CostDaily.product_code.in_(products),
            CostDaily.stat_date >= d_from,
            CostDaily.stat_date <= d_to,
        )
        .group_by(CostDaily.stat_date, CostDaily.instance_id, CostDaily.instance_name)
        .all()
    )
    if not rows:
        return 0.0, {}
    n_days = len({r[0] for r in rows})
    by_inst: dict = {}
    for _, iid, iname, pretax in rows:
        g = by_inst.setdefault(iid, {"instance_id": iid, "name": iname or iid, "amount": 0.0})
        g["amount"] += pretax or 0.0
    total = sum(g["amount"] for g in by_inst.values())
    if n_days:
        return total / n_days, by_inst
    return 0.0, by_inst


def pool_active(db: Session, instance_id: str, applicable_products: str) -> bool:
    """该包所在抵扣池是否仍有额度。

    单包耗尽而池内还有余量 = 正常轮转，**不是"正在转按量"**。
    只有池本身见底（remaining_pct <= 0.5）才谈转按量代价。
    无快照数据时保守返回 True（宁可少报不误报）。
    """
    from sqlalchemy import func as _func
    latest = db.query(_func.max(ResourcePackSnapshot.snapshot_date)).scalar()
    if latest is None:
        return True
    snaps = db.query(ResourcePackSnapshot).filter(
        ResourcePackSnapshot.snapshot_date == latest).all()
    own = next((s for s in snaps if s.instance_id == instance_id), None)
    if own is None:
        return True
    pools = compute_pools(snaps, {})   # remaining_pct 不依赖速率，空 rates 足够
    p = pools.get(pool_key(own.commodity_code, own.applicable_products))
    return bool(p and p["remaining_pct"] > 0.5)


def impact_analysis(db: Session, instance_id: str, applicable_products: str) -> dict:
    """已耗尽包的影响量化（带 5 分钟进程内缓存）。

    首页会对全部死包逐个调本函数，缓存避免逐包重复计算形成 N+1；
    账单数据是 T+1，结果 5 分钟内不会变。
    账单每日 10:00 更新，TTL 5 分钟远小于更新周期，无失效一致性问题。
    """
    key = (instance_id, applicable_products)
    now = time.monotonic()
    hit = _impact_cache.get(key)
    if hit and now - hit[0] < _IMPACT_CACHE_TTL:
        return hit[1]
    out = _impact_analysis_uncached(db, instance_id, applicable_products)
    _impact_cache[key] = (now, out)
    return out


def _impact_analysis_uncached(db: Session, instance_id: str, applicable_products: str) -> dict:
    """已耗尽包的影响量化：耗尽前后各 7 天按量日均对比 + 受影响实例 Top。"""
    exhaust_date = find_exhaust_date(db, instance_id)
    out = {
        "instance_id": instance_id,
        "exhaust_date": exhaust_date.isoformat() if exhaust_date else None,
        "available": False,
    }
    if exhaust_date is None:
        return out
    # 账单数据是否覆盖得了 before 窗口（耗尽太早的包没有历史可对比）
    min_cost = db.query(func.min(CostDaily.stat_date)).scalar()
    if min_cost is None or min_cost > exhaust_date - timedelta(days=1):
        return out

    prods = covered_products(applicable_products)
    before, before_inst = _daily_avg(
        db, prods, exhaust_date - timedelta(days=7), exhaust_date - timedelta(days=1))
    max_cost = db.query(func.max(CostDaily.stat_date)).scalar()
    after_end = min(exhaust_date + timedelta(days=6), max_cost)
    after, after_inst = _daily_avg(db, prods, exhaust_date, after_end)

    inst_rows = []
    for iid, g in after_inst.items():
        b = before_inst.get(iid, {}).get("amount", 0.0)
        # after 窗口可能不满 7 天，按天数折算成日均再对比
        n_after = max(1, (after_end - exhaust_date).days + 1)
        extra = g["amount"] / n_after - b / 7.0
        if extra > 0.1:
            inst_rows.append({
                "instance_id": iid, "name": g["name"],
                "before_daily": round(b / 7.0, 2), "after_daily": round(g["amount"] / n_after, 2),
                "extra_daily": round(extra, 2),
            })
    inst_rows.sort(key=lambda x: -x["extra_daily"])

    extra_daily = after - before
    out.update({
        "available": True,
        "before_daily": round(before, 2),
        "after_daily": round(after, 2),
        "extra_daily": round(extra_daily, 2),
        "extra_monthly": round(extra_daily * 30, 0),
        "top_instances": inst_rows[:5],
        "note": "口径：amount/extra_daily=账单金额（pretax_amount，判断花费以此为准）；"
                "payment_amount=挂账现金字段（常 0），仅存证不作依据。"
                "T+1 账单非终态，近 7 天可能补账修订",
    })
    return out


def related_pack_exhaustions(db: Session, product_code: str, day: date,
                             window_days: int = 3) -> list:
    """day 前 window_days 内耗尽的、抵扣范围覆盖 product_code 的资源包。

    payg 突增的因果指认：返回 [{instance_id, type_label, detect_date}]。
    只向前看（耗尽日必须早于等于突增日，发生在突增之后的包不可能是原因）；
    pack_exhausted 的 detect_date 是余量归零前一日，故窗口取 [day-3, day]。
    product_code 传 "__all__" 表示不限产品（全局突增时用）。
    历史异动记录也走这里动态关联（不改写库内记录）。
    """
    rows = (
        db.query(CostAnomaly)
        .filter(
            CostAnomaly.type == "pack_exhausted",
            CostAnomaly.detect_date >= day - timedelta(days=window_days),
            CostAnomaly.detect_date <= day,
        )
        .all()
    )
    out = []
    for a in rows:
        snap = (
            db.query(ResourcePackSnapshot)
            .filter(ResourcePackSnapshot.instance_id == a.target_key)
            .order_by(ResourcePackSnapshot.snapshot_date.desc())
            .first()
        )
        if snap is None:
            continue
        if product_code != "__all__" and product_code not in covered_products(snap.applicable_products):
            continue
        zh = pack_chinese(snap.commodity_code, snap.applicable_products)
        out.append({
            "instance_id": a.target_key,
            "type_label": zh["type_label"],
            "detect_date": a.detect_date.isoformat(),
        })
    return out
