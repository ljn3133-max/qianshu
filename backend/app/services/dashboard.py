"""首页"今日答案页"只读聚合：回答"今天有没有事、先处理哪件"。

数据全部来自既有表：firing 告警、近 7 日仍复现的待处理成本异动、最新巡检计数、
已耗尽资源包的转按量日均影响。最多 8 条待办，每条包含影响、处理方向和直达链接；
数据未就绪必须与"没有异常"区分。
"""
from __future__ import annotations

import logging
from sqlalchemy import or_
from datetime import date, datetime, timedelta

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.alert_record import AlertRecord
from app.models.asset import Asset
from app.models.cost_anomaly import CostAnomaly
from app.models.cost_daily import CostDaily
from app.models.inspection import InspectionItem, InspectionRun
from app.models.resource_pack_snapshot import ResourcePackSnapshot
from app.services.biz_map import resolve_biz
from app.services.expiry import _PRODUCT_LABELS, _level
from app.services.pack_impact import impact_analysis, pool_active

logger = logging.getLogger("qianshu.dashboard")

_MAX_TODOS = 8
_LVL = {"critical": 0, "warning": 1, "info": 2}

# 每类成本的"怎么办"（三行内，直白）
_HINTS = {
    "pack_exhausting": "整池额度快耗尽，届时抵扣范围产品全部转按量计费——提前续购",
    "pack_exhausted": "包已耗尽，用量正在转按量多花钱——续购或确认用量计划",
    "pack_expiry": "包快到期但还有余量，到期作废——确认用量计划或调整续购",
    "pack_spike": "资源包消耗突然变快——排查用量来源（新业务？异常流量？）",
    "payg_spike": "按量费用突增——看明细里'疑似因资源包耗尽'标注，或查实例用量",
    "payg_new": "出现新的按量费用项——确认是新业务还是配置变更引入",
    "traffic_spike": "流量/日志量超同类日基线——看域名消耗定位是谁涨的，或一键诊断",
    "growth_accel": "磁盘/空间增速突然加快——看是否日志/备份暴涨，评估几天写满",
    "profile_drift": "行为构成变了（top 成员/占比）——确认是新业务就接受为正常，否则排查谁进来了",
    "chain_drift": "架构异常：多层数据不匹配——先看最近变更，再看哪层没跟上",
    "host_down": "服务器关机/非运行状态——确认是计划内停机就点确认，否则立即开机排查（告警中心也可查）",
    "aux_spillover": "辅助机器把 5xx 外溢到了 SLB——查这台机器本身（它挂了会连累全站）",
}


def _money(v: float) -> str:
    return f"¥{v:,.0f}" if v >= 100 else f"¥{v:,.2f}"


def _pack_products(db: Session, instance_id: str) -> str:
    s = (
        db.query(ResourcePackSnapshot)
        .filter(ResourcePackSnapshot.instance_id == instance_id)
        .order_by(ResourcePackSnapshot.snapshot_date.desc())
        .first()
    )
    return s.applicable_products if s else ""


def _anomaly_link(a: CostAnomaly) -> str:
    """异动待办的直达链接：落「异动」独立页（带类型域参数）。

    总览待办统一进入 /anomalies?domain=<cost|monitor|cdn|profile> 查看该域全量异动，
    再由行级深链进入实际处理页面。
    """
    from app.services.cost_anomaly import ANOMALY_TYPE_DOMAINS

    # cdn 是 monitor 子集，traffic_spike 归 cdn（与 ANOMALY_TYPE_DOMAINS 展示口径一致）
    if a.type in ANOMALY_TYPE_DOMAINS["cdn"]:
        domain = "cdn"
    elif a.type in ANOMALY_TYPE_DOMAINS["profile"]:
        domain = "profile"
    elif a.type in ANOMALY_TYPE_DOMAINS["monitor"]:
        domain = "monitor"
    else:
        domain = "cost"
    return f"/anomalies?domain={domain}"


def summary(db: Session) -> dict:
    today = date.today()
    todos: list = []
    # 总览先按模块/账号提醒去向与确认状态过滤，再截断到 8 条，避免已停用账号占待办位。
    from app.services.monitor_policy import can_overview, module_of_anomaly

    # 1) firing 告警
    firing = (
        db.query(AlertRecord)
        .filter(AlertRecord.status == "firing", AlertRecord.ack_status == "none")
        .order_by(
            case((AlertRecord.level == "critical", 0), (AlertRecord.level == "warning", 1), else_=2),
            AlertRecord.fired_at.desc(),
        )
        .all()
    )
    # 提醒次数（notify_count）只控制「主不主动打扰」，不控制待办可见性：
    # 低优先级告警只主动通知一次，但用户没处理前仍应能在首页看到；
    # 不想看总览时由账号开关 notify_overview 显式关闭。
    firing = [r for r in firing
              if can_overview(db, getattr(r, "module", None) or "alert",
                              getattr(r, "account_id", 0))]
    for r in firing[:_MAX_TODOS]:
        detail = r.target_name or r.target or ""
        if r.breach_value is not None:
            # 首页直接展示实例、当前值与触发条件，不必进入详情才能判断告警对象。
            detail = f"当前值 {r.breach_value:g}（条件：{r.content}） · 实例 {r.target}"
        age = (datetime.now() - r.fired_at).days if r.fired_at else 0
        todos.append({
            "sev": r.level or "warning",
            "cat": "告警",
            "biz": resolve_biz(db, r.target, r.target_name, f"{r.title} {(r.content or '')[:200]}"),
            "title": r.title or f"{r.rule_name} 越限",
            "detail": detail,
            "money": "",
            "hint": ("这台持续贴线已 " + str(age) + " 天，疑似常态高位——告警记录里点「设为常态」后不再重复报，只报显著偏离"
                     if age >= 3 else "查看告警详情，可用'一键诊断'自动排查"),
            "link": "/alerts?tab=records",   # 直达告警记录而非规则配置页
            "age_days": age,
            "plan": False,
        })

    # 2) 待处理成本异动（open 且近 7 日复现）
    cost_rows = (
        db.query(CostAnomaly)
        .filter(CostAnomaly.ack_status == "open", CostAnomaly.last_seen >= today - timedelta(days=7))
        .all()
    )
    # 画像漂移属于待确认事项，不占首页处置待办；仍保留在今日决策清单与画像页。
    cost_rows = [a for a in cost_rows if a.type != "profile_drift"]
    cost_rows = [a for a in cost_rows
                 if can_overview(db, module_of_anomaly(a),
                                 getattr(a, "account_id", 0))]
    for a in cost_rows:
        money = ""
        hint = _HINTS.get(a.type, "查看成本异动详情")
        if a.type == "pack_exhausted":
            # 池级守卫：死包在活池里属于正常轮转，不报钱、不催买。
            if pool_active(db, a.target_key, _pack_products(db, a.target_key)):
                hint = "历史包耗尽属正常轮转（池内仍有额度在抵扣），无需处理"
            else:
                imp = impact_analysis(db, a.target_key, _pack_products(db, a.target_key))
                # extra_daily 使用账单金额口径的日均多花，不混用刊例价或摊销金额。
                if imp.get("available") and imp.get("extra_daily", 0) > 0:
                    money = f"{_money(imp['extra_daily'])}/天（月约 {_money(imp['extra_monthly'])}）"
        elif a.type in ("payg_spike", "payg_new") and (a.value or 0) > 0:
            money = f"{_money(a.value)}/天"
        age = (today - a.detect_date).days if a.detect_date else 0
        todos.append({
            "sev": a.level or "warning",
            "cat": "全景" if a.type == "chain_drift" else "成本",
            "id": a.id,          # 异动详情与收口按钮的行级标识
            "biz": resolve_biz(db, a.target_key, a.target_name, f"{a.title} {(a.content or '')[:200]}"),
            "title": a.title,
            "detail": "",
            "money": money,
            "hint": hint,
            # 深链必须落到对应页面与页签：包类进 packs，异动进 anomalies，
            # 流量突增携带域名参数直达 URL 归因，不能只落到成本概览。
            "link": _anomaly_link(a),
            "age_days": age,
            # 计划类（还有十几天的耗尽/到期）是"安排"不是"今天动手"，不抢红位
            "plan": a.type in ("pack_exhausting", "pack_expiry"),
        })

    # 3) 到期总账（资产侧；资源包到期由 pack_expiry 异动负责，不重复）
    #    已过期 30 天内的还值得处理（换证书/确认释放），更早的烂账只在到期页看。
    #    同域名只看最新一张证；历史旧证和已过期证不进入待办。
    #    预警从 30 天 warning、7 天 critical 开始，只有未过期的证才需要处置。
    expiry_rows = (
        db.query(Asset)
        .filter(Asset.expire_time.isnot(None), Asset.status != "Deleted")
        .all()
    )
    from app.services.expiry import latest_cert_by_domain

    certs = latest_cert_by_domain([a for a in expiry_rows if a.product == "ssl_cert"])
    expiry_rows = [a for a in expiry_rows if a.product != "ssl_cert"] + certs
    # 证书日期与分级由 cert 层统一解析，与到期总账使用同一事实来源，拨测证据优先。
    from app.services.cert_fact import resolve_for_assets

    cert_facts = resolve_for_assets(db, certs) if certs else {}
    # 已忽略的到期项不再进入决策清单，避免持续干扰其他待办。
    from app.services.expiry import _ignore_set
    ignored = _ignore_set(db)
    expiry_stat = {"expired": 0, "critical": 0, "warning": 0}
    expiry_todos: list = []
    for a in expiry_rows:
        if f"{a.product}:{a.instance_id or a.name}" in ignored:
            continue
        if not can_overview(db, "expiry", getattr(a, "account_id", 0)):
            continue
        days = (a.expire_time.date() - today).days
        warn_days = 30
        if a.product == "ssl_cert":
            f = cert_facts.get(a.instance_id)
            if f is not None and f.not_after:
                days = (f.not_after.date() - today).days
                warn_days = f.warn_days
            if days < 0:
                continue  # 已过期证书不汇报（账页可见）
        if not (-30 <= days <= 30):
            continue
        lv = _level(days, warn_days)
        if lv in expiry_stat:
            expiry_stat[lv] += 1
        expiry_todos.append((days, a, lv))
    expiry_todos.sort(key=lambda t: t[0])
    # 相同级别与到期天数聚合为一条，避免同类待办逐行刷屏
    if expiry_todos:
        _groups: dict = {}
        for days, a, lv in expiry_todos:
            _groups.setdefault((lv, days), []).append(a.name or a.instance_id)
        for (lv, days), names in sorted(_groups.items(), key=lambda x: x[0][1])[:2]:
            txt = f"{len(names)} 台资源 {abs(days)} 天后到期" if days >= 0 else f"{len(names)} 台资源已过期 {abs(days)} 天"
            todos.append({
                "cat": "到期",
                "level": lv,
                "sev": "warning" if lv == "warning" else "critical",
                "title": txt + (f"（最急：{names[0]}）" if len(names) > 1 else f"（{names[0]}）"),
                "detail": "、".join(names[:6]) + ("…" if len(names) > 6 else ""),
                "link": "/expiry",   # 前端「去看」只读 link，勿改名为 href
            })
    for days, a, lv in []:  # 占位保留结构
        if lv not in ("expired", "critical", "warning"):
            continue
        label = _PRODUCT_LABELS.get(a.product, a.product)
        when = "已过期" if days < 0 else f"{days} 天后到期"
        todos.append({
            "sev": "critical" if (days < 0 or days <= 7) else "warning",
            "cat": "到期",
            "biz": resolve_biz(db, a.instance_id, a.name, f"{a.name} {a.instance_id}"),
            "title": f"{label} {a.name or a.instance_id} {when}",
            "detail": "",
            "money": "",
            "hint": f"到期时间 {a.expire_time.strftime('%Y-%m-%d')}——提前续费或确认释放计划",
            "link": f"/expiry?days={max(days, 0)}",
            "age_days": 0,
            "plan": True,   # 到期是计划事项，按剩余天数已排好序
        })

    # 4) 最新巡检的 critical 条目（避免与成本/告警重复刷屏，只取 critical）
    latest_run = db.query(InspectionRun).order_by(InspectionRun.id.desc()).first()
    insp_stat = None
    from app.services.monitor_policy import module_enabled
    if latest_run is not None and module_enabled(db, "inspection"):
        cnt = dict(
            db.query(InspectionItem.level, func.count(InspectionItem.id))
            .filter(InspectionItem.run_id == latest_run.id)
            .group_by(InspectionItem.level).all()
        )
        insp_stat = {
            "run_id": latest_run.id,
            "critical": cnt.get("critical", 0),
            "warning": cnt.get("warning", 0),
            "finished_at": latest_run.finished_at.isoformat() if latest_run.finished_at else None,
        }

    # 待办排序：级别 -> 新事项优先（龄期小在前）-> 计划类靠后 -> 有钱优先。
    # 老积压不许挤掉今天的新事项——超上限时折叠进"积压"计数（总账在今日决策清单）
    todos.sort(key=lambda t: (
        _LVL.get(t["sev"], 3),
        1 if t.get("plan") else 0,
        t.get("age_days", 0),
        0 if t.get("money") else 1,
    ))
    overflow = todos[_MAX_TODOS:]
    todos = todos[:_MAX_TODOS]

    # 已耗尽包的累计转按量影响（一句话背景数据；池级守卫——活池里的死包是轮转不计钱）
    latest_snap = db.query(func.max(ResourcePackSnapshot.snapshot_date)).scalar()
    extra_total = 0.0
    if latest_snap is not None:
        for s in db.query(ResourcePackSnapshot).filter(
            ResourcePackSnapshot.snapshot_date == latest_snap,
            ResourcePackSnapshot.remaining_pct <= 0.01,
        ).all():
            if pool_active(db, s.instance_id, s.applicable_products):
                continue
            imp = impact_analysis(db, s.instance_id, s.applicable_products)
            if imp.get("available") and imp.get("extra_daily", 0) > 0:
                extra_total += imp["extra_daily"]

    # 昨日按量付费总额与环比使用 T+1 账单的 pretax_amount，不混用摊销金额。
    def _payg(d):
        # bill/tc_bill 分别是阿里/腾讯按量账单明细，统一使用按量金额口径。
        return float(db.query(func.sum(CostDaily.pretax_amount)).filter(
            CostDaily.stat_date == d, CostDaily.source.in_(("bill", "tc_bill")),
            CostDaily.subscription_type == "PayAsYouGo").scalar() or 0)
    y_day = today - timedelta(days=1)
    payg_yesterday = round(_payg(y_day), 2) if db.query(CostDaily.id).filter(CostDaily.stat_date == y_day).first() else None
    payg_amortized = round(float(db.query(func.sum(CostDaily.pretax_amount)).filter(
        CostDaily.stat_date == y_day, CostDaily.source == "amortized",
    ).scalar() or 0), 2)   # 摊销单列（不混进按量）
    prev_v = _payg(y_day - timedelta(days=1))
    payg_change_pct = (round((payg_yesterday - prev_v) / prev_v * 100, 1)
                       if payg_yesterday is not None and prev_v > 0 else None)
    # 最急的资源包（池口径几天耗尽）——不进成本页也能看到该买什么
    urgent_pack = None
    try:
        from app.services.cost_anomaly import _pack_rate
        from app.services.pack_impact import compute_pools, pack_chinese, pool_key
        latest_snap = db.query(func.max(ResourcePackSnapshot.snapshot_date)).scalar()
        if latest_snap:
            snaps = db.query(ResourcePackSnapshot).filter(
                ResourcePackSnapshot.snapshot_date == latest_snap).all()
            rates = {x.instance_id: _pack_rate(x, db)[0] for x in snaps}
            best = None
            for key, p in compute_pools(snaps, rates).items():
                if p["days_left"] is None:
                    continue
                member = next(x for x in snaps if pool_key(x.commodity_code, x.applicable_products) == key)
                label = pack_chinese(member.commodity_code, member.applicable_products)["type_label"]
                if best is None or p["days_left"] < best[0]:
                    best = (p["days_left"], label)
            if best:
                urgent_pack = {"label": best[1], "days_left": round(best[0], 1)}
    except Exception:
        urgent_pack = None

    blocking = [t for t in todos if t["sev"] in ("critical", "warning")]
    # 异动入口卡：近 30 天异动总数 + 还开着的——点进 /anomalies
    anomalies_30d = db.query(CostAnomaly).filter(
        CostAnomaly.last_seen >= today - timedelta(days=29)).count()
    anomalies_open = db.query(CostAnomaly).filter(
        CostAnomaly.ack_status == "open", CostAnomaly.last_seen >= today - timedelta(days=29)).count()
    # 数据就绪度：上游未采集不能与"真的没事"同形，未就绪必须明确展示
    not_ready: list = []
    if db.query(CostDaily.id).first() is None:
        not_ready.append(f"成本数据未采集（每日 {settings.COST_COLLECT_HOUR}:00 自动，T+1 账单）")
    # 成本停更守卫：只判全空会漏掉“有历史但已断供”；账单过旧时必须暴露原因，
    # 不能让「昨日按量」显示 '-' 而就绪度仍全绿。
    _cost_last = db.query(func.max(CostDaily.stat_date)).scalar()
    _cost_stale_days = (today - _cost_last).days if _cost_last else None
    if _cost_last is not None and _cost_last < today - timedelta(days=2):
        not_ready.append(f"成本数据停更 {_cost_stale_days} 天（最后 {_cost_last.isoformat()}）"
                         "——到「云账号管理」检查账号启用与凭证，T+1 账单")
    if db.query(InspectionRun.id).first() is None:
        not_ready.append("巡检未运行（每日 08:10 自动，或巡检页手动触发）")
    try:
        from app.collector.metrics_job import get_status as _mstat
        if not _mstat().get("last_finished_at"):
            not_ready.append("指标采集未运行（每 5 分钟自动）")
    except Exception:
        pass
    # 告警评估停更：空数据≠0，评估循环停了时 firing=0 会被误读成"零告警"，
    # 与"真的没事"同形。评估周期 60s，这里按 15 分钟宽限判停更。
    try:
        from app.services.alert_eval import get_status as _astat
        _a_last = _astat().get("last_run_at")
        if not settings.ALERT_EVAL_ENABLED:
            not_ready.append("告警评估已关闭（ALERT_EVAL_ENABLED=false）——只有已存告警，不会新增")
        elif not _a_last:
            not_ready.append("告警评估尚未运行（启动后约 1 分钟开始，每 60 秒一轮）")
        else:
            _a_min = (datetime.now() - datetime.fromisoformat(_a_last)).total_seconds() / 60
            if _a_min > 15:
                not_ready.append(f"告警评估停更 {int(_a_min)} 分钟（每 60 秒自动）——"
                                 "当前告警数量不代表真实状态")
    except Exception:
        pass
    # 校准周报待办：跨域待办统一聚合到一处
    try:
        from app.models.calibration import CalibrationItem

        n_cal = (db.query(CalibrationItem)
                 .filter(CalibrationItem.status == "pending").count())
        if n_cal:
            todos.append({
                "cat": "新发现", "level": "info", "sev": "info",
                "title": f"本周新发现 {n_cal} 项待确认",
                "detail": "新资源/新域名的业务归属与重要级别——逐条确认即可（1 分钟）",
                "link": "/oncall",   # 前端「去看」读 link（与到期聚合同修）
            })
    except Exception:
        pass

    return {
        "date": today.isoformat(),
        "ok": len(blocking) == 0,
        "ready": len(not_ready) == 0,
        "not_ready_reasons": not_ready,
        "todo_count": len(blocking),
        # 超出 8 条上限被折叠的积压（不丢：今日决策清单有全量总账）
        "overflow": {
            "count": len(overflow),
            "oldest_days": max((t.get("age_days", 0) for t in overflow), default=0),
        },
        "todos": todos,
        # 总览数字位只保留关键事项：
        # 钱（昨日按量付费+环比）/警（触发中）/事（待决策）/急（最急的包）
        "stats": {
            "alerts_firing": len(firing),
            "cost_open": len(cost_rows),
            "anomalies_30d": anomalies_30d,
            "anomalies_open": anomalies_open,
            "pack_extra_daily": round(extra_total, 2),
            "inspection": insp_stat,
            "expiry": expiry_stat,
            "payg_yesterday": payg_yesterday,
            "payg_amortized": payg_amortized,
            "payg_change_pct": payg_change_pct,
            # 昨日按量显示 '-' 时，用最后账单日期与停更天数解释原因。
            "cost_last_date": _cost_last.isoformat() if _cost_last else None,
            "cost_stale_days": _cost_stale_days,
            "urgent_pack": urgent_pack,
        },
    }
