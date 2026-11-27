"""采购建议。

每次进入成本页自动计算资源包购买量、浪费与主要消耗来源，给出可直接决策的结论。

结论口径：
- 池级判定（池才是告警口径）：≤7 天立刻买 / ≤30 天该买，按日耗给购入量
- 已耗尽包的转按量代价（按账单日均多花）
- 流费风险：快到期还有大余量的包（到期作废）
- 大头提示：流量被单域名吃掉时，先治理再囤包
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.cdn_domain_daily import CdnDomainDaily
from app.models.resource_pack_snapshot import ResourcePackSnapshot
from app.services.cost_anomaly import _pack_rate
from app.services.pack_impact import compute_pools, impact_analysis, norm_amount, pack_chinese, pool_active, pool_key

logger = logging.getLogger("qianshu.purchase_advice")

GB = 1024 ** 3


def purchase_advice(db: Session) -> dict:
    latest = db.query(func.max(ResourcePackSnapshot.snapshot_date)).scalar()
    if latest is None:
        return {"items": [], "note": "暂无资源包快照（每日自动采集）"}
    snaps = db.query(ResourcePackSnapshot).filter(
        ResourcePackSnapshot.snapshot_date == latest).all()
    rates = {s.instance_id: _pack_rate(s, db)[0] for s in snaps}
    pools = compute_pools(snaps, rates)

    # 池分组与 29 天前对比（消耗量/建议购入量）
    base_day = latest - timedelta(days=29)
    base_snap_date = db.query(func.max(ResourcePackSnapshot.snapshot_date)).filter(
        ResourcePackSnapshot.snapshot_date <= base_day).scalar()
    base = {}
    if base_snap_date:
        base = {s.instance_id: s for s in db.query(ResourcePackSnapshot).filter(
            ResourcePackSnapshot.snapshot_date == base_snap_date).all()}

    items: list = []
    groups: dict = {}
    for s in snaps:
        groups.setdefault(pool_key(s.commodity_code, s.applicable_products), []).append(s)
    pool_daily: dict = {}   # pk -> (日耗, is_size) 供"已耗尽"条目给购入量

    for pk, members in groups.items():
        p = pools[pk]
        zh = pack_chinese(members[0].commodity_code, members[0].applicable_products)
        label = zh["type_label"]
        alive = [s for s in members if (s.remaining_pct or 0) > 0.01]
        dead = len(members) - len(alive)
        dl = p["days_left"]
        unit = (members[0].remaining_unit or "").lower()
        is_size = unit in ("gb", "tb", "byte", "bytes", "mb")

        # 29 天消耗 -> 日均 -> 建议购入量（按全部成员算——本月内烧完的死包也是消耗）
        month_used = 0.0
        for s in members:
            b = base.get(s.instance_id)
            if b is None:
                continue
            if is_size:
                month_used += (norm_amount(b.remaining_amount, b.remaining_unit)
                               - norm_amount(s.remaining_amount, s.remaining_unit)) / GB
            else:
                month_used += (b.remaining_pct or 0) - (s.remaining_pct or 0)
        month_used = max(month_used, 0.0)   # 新买入的包会让差值为负
        daily = month_used / 29 if month_used > 0 else 0.0
        pool_daily[pk] = (daily, is_size)

        if p["remaining_pct"] <= 0.5 and month_used > 0:
            # 池已归零但业务还在消耗（正在转按量）——最急的"立刻买"
            items.append({
                "level": "urgent", "kind": "buy",
                "title": f"{label}：已归零，立刻买",
                "detail": (f"池剩余 {p['remaining_pct']:.1f}%，用量已转按量计费"
                           + (f"；按本月消耗 {month_used:,.0f}GB，建议购入 {daily * 45:,.0f}GB（45 天量）"
                              if is_size and daily > 0 else "")),
                "money": "",
            })
        elif dl is not None and dl <= 7:
            items.append({
                "level": "urgent", "kind": "buy",
                "title": f"{label}：立刻买（约 {dl:.0f} 天耗尽）",
                "detail": (f"池剩余 {p['remaining_pct']:.1f}%，"
                           + (f"建议购入 {daily * 45:,.0f}GB（45 天量）" if is_size and daily > 0
                              else "按 30 天量补购")),
                "money": "",
            })
        elif dl is not None and dl <= 30:
            items.append({
                "level": "warning", "kind": "buy",
                "title": f"{label}：该买了（约 {dl:.0f} 天耗尽）",
                "detail": (f"池剩余 {p['remaining_pct']:.1f}%，本月耗 {month_used:,.0f}"
                           + ("GB" if is_size else "%")
                           + (f"，建议购入 {daily * 45:,.0f}GB（45 天量）" if is_size and daily > 0 else "")),
                "money": "",
            })

    # 已耗尽包的转按量代价（实测）必须逐包点名，只有汇总金额无法支持续购决策。
    # 池级守卫：死包在活池里属于正常轮转，不是转按量；只有池见底才报。
    dead: list = []
    for s in snaps:
        if (s.remaining_pct or 0) > 0.01:
            continue
        if pool_active(db, s.instance_id, s.applicable_products):
            continue   # 池内还有新包在抵扣：历史死包=轮转，不报
        imp = impact_analysis(db, s.instance_id, s.applicable_products)
        # extra_daily 使用账单金额口径计算日均多花
        if imp.get("available") and imp.get("extra_daily", 0) > 0:
            dead.append((float(imp.get("extra_daily") or 0), s))
    dead.sort(key=lambda x: -x[0])
    for extra_daily, s in dead:
        zh = pack_chinese(s.commodity_code, s.applicable_products)
        scope = zh.get("products_label") or s.applicable_products or zh.get("scope_label") or "抵扣范围"
        pk = pool_key(s.commodity_code, s.applicable_products)
        daily, is_size = pool_daily.get(pk, (0.0, False))
        buy_hint = (f"，建议购入 {daily * 45:,.0f}GB（45 天量）" if is_size and daily > 0
                    else "，规格按近 30 天用量选（控制台同类型包下单页有推荐档位）")
        money_txt = f"日均多花 ¥{extra_daily:,.2f}（月约 ¥{extra_daily * 30:,.0f}）"
        money = f"¥{extra_daily:,.2f}/天"
        items.append({
            "level": "urgent", "kind": "cost",
            # 标题点名到包类型与抵扣范围；金额只放 money 字段一处（此前标题+money 双写
            # 导致前端渲染成「¥121.75¥121.75/天」）
            "title": f"已耗尽正在转按量：{zh['type_label']}（{scope}）",
            "detail": (f"包实例 {s.instance_id}：{zh['type_label']}（抵扣范围 {scope}）额度已用尽，"
                       f"这部分用量转按量计费——{money_txt}。"
                       f"续购同类型包即可回到抵扣{buy_hint}"),
            "money": money,
        })
    if len(dead) > 1:
        total_extra = sum(x[0] for x in dead)
        money_txt = f"日均多花 ¥{total_extra:,.2f}"
        items.append({
            "level": "urgent", "kind": "cost",
            "title": f"合计：{len(dead)} 个包耗尽转按量，{money_txt}",
            "detail": f"月约 ¥{total_extra * 30:,.0f}——上面逐包列了各自的钱与建议购入量，按急迫度逐个续购",
            "money": "",
        })

    # 流费风险：快到期还有大余量
    for s in snaps:
        if (s.remaining_pct or 0) <= 15 or not s.expiry_time:
            continue
        try:
            exp = datetime.fromisoformat(s.expiry_time).date()
        except ValueError:
            continue
        days = (exp - date.today()).days
        if 0 <= days <= 45:
            zh = pack_chinese(s.commodity_code, s.applicable_products)
            items.append({
                "level": "warning", "kind": "waste",
                "title": f"浪费风险：{zh['type_label']} 还剩 {s.remaining_pct:.0f}% 但 {days} 天后到期",
                "detail": f"{s.instance_id} 到期作废——优先消耗它，或核对抵扣范围/顺序",
                "money": "",
            })

    # 大头提示：CDN 流量被单域名吃掉 -> 先治理再囤包
    total_bytes = db.query(func.sum(CdnDomainDaily.traffic_bytes)).filter(
        CdnDomainDaily.stat_date >= date.today() - timedelta(days=14)).scalar() or 0
    if total_bytes > 0:
        top = (db.query(CdnDomainDaily.domain, func.sum(CdnDomainDaily.traffic_bytes))
               .filter(CdnDomainDaily.stat_date >= date.today() - timedelta(days=14))
               .group_by(CdnDomainDaily.domain)
               .order_by(func.sum(CdnDomainDaily.traffic_bytes).desc()).first())
        if top and top[1] / total_bytes >= 0.9:
            items.append({
                "level": "info", "kind": "govern",
                "title": f"囤包前先看流量大头：{top[0]} 占近 14 天 CDN 流量 {top[1] / total_bytes * 100:.1f}%",
                "detail": "一个域名吃掉几乎全部流量——先确认是合理业务还是异常（被盗刷/未压缩），治理比囤包省钱",
                "money": "",
            })

    order = {"urgent": 0, "warning": 1, "info": 2}
    items.sort(key=lambda x: order.get(x["level"], 3))
    return {"items": items, "snapshot_date": latest.isoformat(),
            "note": "结论按池口径计算，每日随资源包快照刷新"}
