"""成本 API：采集状态/手动触发/明细/汇总、异动检测与资源包监控。"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.core.client_ip import get_client_ip
from app.core.config import settings
from app.core.db import get_db
from app.core.deps import require_roles
from app.models.cost_anomaly import CostAnomaly
from app.models.cost_daily import CostDaily
from app.models.resource_pack_snapshot import ResourcePackSnapshot
from app.models.user import User
from app.schemas.cost import (
    AnomalyAckRequest,
    CollectRequest,
    CostInsightsResponse,
    DetectRequest,
)
from app.services import audit
from app.services.cost_anomaly import (
    ANOMALY_TYPE_DOMAINS,
    _pack_rate,
    _parse_time,
    detect_cost_for_date,
    snapshot_and_detect_packs,
)
from app.services.pack_impact import (
    compute_pools, impact_analysis, pack_chinese, pool_key, related_pack_exhaustions,
)
from app.services.biz_map import resolve_biz
from app.services.cost_job import get_status, get_tencent_status, run_collection, run_tencent_collection
from app.services.host_watch import close_builtin_for_anomalies

logger = logging.getLogger("qianshu.cost")
router = APIRouter(prefix="/cost", tags=["cost"])


def _parse_date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise HTTPException(400, f"日期格式不正确: {s}（应为 YYYY-MM-DD）")


def _own_rows(db, items: list, acc_map: dict | None = None, rules=None) -> list:
    """归属回流：在 account_fields 自动归属之后补登记解析层。

    行的实体键（instance_id / instance_name 或 name）过 ownership 解析，
    登记字段按字段覆盖自动值；无键行（产品/场景聚合行）与无登记行原样
    （红线：不造数据）。instance/name 为 "-" 的显示占位不当键。
    rules 可由批量调用方预加载，避免逐行 load_rules（导出 2 万行的 N+1）。
    """
    from app.services.ownership import apply_account_fields, load_rules, resolve

    if rules is None:
        rules = load_rules(db)
    for it in items:
        iid = it.get("instance_id") or ""
        nm = it.get("instance_name") or it.get("name") or ""
        if iid == "-":
            iid = ""
        if nm == "-":
            nm = ""
        if not iid and not nm:
            continue
        hit = resolve(rules, instance=iid or None, name=nm or None)
        if hit:
            apply_account_fields(it, hit, acc_map=acc_map)
    return items


@router.get("/collect/status")
def collect_status(
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    status = get_status()
    # 进程重启后内存状态清空，用库内最新采集日补"最近采集"
    if not status.get("last_dates"):
        latest = db.query(func.max(CostDaily.stat_date)).scalar()
        if latest:
            status["last_dates"] = [latest.isoformat()]
    status["tencent"] = get_tencent_status()   # 腾讯账单通道（tc_bill）
    return status


@router.post("/collect")
def trigger_collect(
    body: CollectRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """手动采集：默认 T-1 一天；days 回填最近 N 天；date/start+end 指定日期。
    阿里（bill/amortized）+ 腾讯（tc_bill）两通道各采一轮，覆盖各自账单场景。"""
    start = _parse_date(body.date) if body.date else (
        _parse_date(body.start) if body.start else None
    )
    end = _parse_date(body.end) if body.end else None
    try:
        result = run_collection(db, days=body.days or 1, start=start, end=end)
        result["tencent"] = run_tencent_collection(
            db, days=body.days or 1, start=start, end=end, force=True)
    except Exception as e:
        logger.exception("手动成本采集失败")
        raise HTTPException(502, f"采集失败: {str(e)[:200]}")
    audit.record(
        db, action="cost.collect", user_id=user.id, username=user.username,
        resource_type="cost", resource_id="bss",
        detail={"dates": result.get("last_dates"), "rows": result.get("rows"),
                "tc_rows": (result.get("tencent") or {}).get("rows")},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return result


def _date_range(start: str, end: str) -> tuple[date, date]:
    d_end = _parse_date(end) if end else date.today() - timedelta(days=1)
    d_start = _parse_date(start) if start else d_end - timedelta(days=6)
    if d_start > d_end:
        raise HTTPException(400, "start 不能晚于 end")
    return d_start, d_end


def _query_daily(db, *, start: str, end: str, product: str, instance_id: str,
                 account_id: int):
    """成本明细查询构造器——列表与导出共用（禁止第二套筛选口径）。"""
    d_start, d_end = _date_range(start, end)
    from app.services.tenant import account_filter

    q = account_filter(db.query(CostDaily).filter(
        CostDaily.stat_date >= d_start, CostDaily.stat_date <= d_end
    ), CostDaily, account_id)
    if product:
        q = q.filter(CostDaily.product_code == product)
    if instance_id:
        q = q.filter(CostDaily.instance_id == instance_id)
    return q


def _daily_item(r: CostDaily, acc_map: dict) -> dict:
    from app.services.tenant import account_fields

    return {
        "stat_date": r.stat_date.isoformat(),
        "source": r.source,
        "product_code": r.product_code,
        "product_name": r.product_name,
        "instance_id": r.instance_id,
        "instance_name": r.instance_name,
        "billing_item": r.billing_item,
        "subscription_type": r.subscription_type,
        "region": r.region,
        "pretax_amount": r.pretax_amount,
        "pretax_gross_amount": r.pretax_gross_amount,
        "payment_amount": r.payment_amount,
        "usage": r.usage,
        **account_fields(acc_map, r.account_id, with_vendor=True),
    }


@router.get("/daily")
def daily_rows(
    start: str = Query("", description="YYYY-MM-DD，默认最近7天"),
    end: str = Query("", description="YYYY-MM-DD"),
    product: str = Query(""),
    instance_id: str = Query(""),
    account_id: int = Query(0, ge=0, description="云账号 id，0=全部（跟随顶部账号切换）"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """成本明细（按日×产品×实例×计费项）。行带归属标识。"""
    q = _query_daily(db, start=start, end=end, product=product,
                     instance_id=instance_id, account_id=account_id)
    total = q.count()
    rows = (
        q.order_by(CostDaily.stat_date.desc(), CostDaily.pretax_amount.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    # 归属标识：account_id -> {vendor, name}，与 alerts/expiry 同口径
    from app.services.tenant import account_index

    acc_map = account_index(db)
    return {
        "total": total,
        "items": _own_rows(db, [_daily_item(r, acc_map) for r in rows], acc_map),
        "note": "口径：pretax_amount=账单金额（判断花费以此为准）；payment_amount=挂账现金字段（常 0），仅存证不作依据。"
                "T+1 账单，近 7 天可能补账修订",
    }


@router.get("/daily/export")
def export_daily(
    request: Request,
    start: str = Query("", description="YYYY-MM-DD，默认最近7天"),
    end: str = Query("", description="YYYY-MM-DD"),
    product: str = Query(""),
    instance_id: str = Query(""),
    account_id: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """成本明细 CSV（与 /daily 同筛选口径；≤2 万行，真实总数见 X-Total-Rows）。"""
    from app.core.csvx import MAX_EXPORT_ROWS, csv_response
    from app.services.cloud.provider import vendor_label
    from app.services.tenant import account_index

    q = _query_daily(db, start=start, end=end, product=product,
                     instance_id=instance_id, account_id=account_id)
    total = q.count()
    rows = (q.order_by(CostDaily.stat_date.desc(), CostDaily.pretax_amount.desc())
            .limit(MAX_EXPORT_ROWS).all())
    acc_map = account_index(db)
    from app.services.ownership import load_rules

    rules = load_rules(db)   # 一次加载，避免逐行查库（2 万行导出的 N+1）
    items = []
    for r in rows:
        it = _daily_item(r, acc_map)
        it["vendor_label"] = vendor_label(it.get("vendor") or "") or (it.get("vendor") or "")
        items.append(it)
    # 与列表同口径：自动归属之后过人工登记回流（否则导出账号名与页面不一致）
    _own_rows(db, items, acc_map, rules=rules)
    d_start, d_end = _date_range(start, end)
    audit.record(
        db, action="cost.daily.export", user_id=user.id, username=user.username,
        resource_type="cost_daily",
        detail={"start": d_start.isoformat(), "end": d_end.isoformat(),
                "product": product, "instance_id": instance_id,
                "account_id": account_id, "rows": len(items), "total": total},
        client_ip=get_client_ip(request),
    )
    db.commit()
    headers = [
        ("stat_date", "日期"), ("account_name", "账号"), ("vendor_label", "厂商"),
        ("product_code", "产品代码"), ("product_name", "产品名"),
        ("instance_id", "实例 ID"), ("instance_name", "实例名"),
        ("billing_item", "计费项"), ("pretax_amount", "金额（元）"),
        ("usage", "用量"), ("source", "来源"),
    ]
    return csv_response(
        headers, items,
        filename=f"成本明细_{d_start.isoformat()}_{d_end.isoformat()}.csv",
        ascii_filename=f"cost-daily_{d_start.isoformat()}_{d_end.isoformat()}.csv",
        total_rows=total,
    )


@router.get("/instances")
def instance_rows(
    start: str = Query("", description="YYYY-MM-DD，默认最近7天"),
    end: str = Query("", description="YYYY-MM-DD"),
    product: str = Query("", description="产品代码过滤，如 sls"),
    account_id: int = Query(0, ge=0, description="云账号 id，0=全部（跟随顶部账号切换）"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """实例维度日成本：区间内按实例聚合，日成本口径（payg+amortized）。

    行带归属标识；实例×账号聚合（实例 ID 跨账号可能撞号）。
    """
    d_end = _parse_date(end) if end else date.today() - timedelta(days=1)
    d_start = _parse_date(start) if start else d_end - timedelta(days=6)
    if d_start > d_end:
        raise HTTPException(400, "start 不能晚于 end")
    q = db.query(
        CostDaily.instance_id,
        func.max(CostDaily.product_code),
        func.max(CostDaily.product_name),
        func.max(CostDaily.instance_name),
        func.count(CostDaily.id),
        func.sum(CostDaily.pretax_amount).filter(
            or_(CostDaily.source == "amortized", CostDaily.subscription_type == "PayAsYouGo")
        ),
        func.sum(CostDaily.pretax_amount).filter(
            and_(CostDaily.source.in_(("bill", "tc_bill")), CostDaily.subscription_type != "PayAsYouGo")
        ),
        CostDaily.account_id,
    ).filter(
        CostDaily.stat_date >= d_start, CostDaily.stat_date <= d_end,
        CostDaily.instance_id != "",
    )
    from app.services.tenant import account_filter

    q = account_filter(q, CostDaily, account_id)
    if product:
        q = q.filter(CostDaily.product_code == product)
    q = q.group_by(CostDaily.instance_id, CostDaily.account_id)
    # 归属标识：account_id -> {vendor, name}，与 alerts/expiry 同口径
    from app.services.tenant import account_fields, account_index

    acc_map = account_index(db)
    # 日成本降序在 Python 侧排（SQL 侧 sum(filter) 表达式复用麻烦）
    rows = [
        {
            "instance_id": r[0],
            "product_code": r[1] or "",
            "product_name": r[2] or r[1] or "",
            "instance_name": r[3] or "",
            "billing_rows": r[4],
            "amount": round(float(r[5] or 0), 2),
            "order_amount": round(float(r[6] or 0), 2),
            **account_fields(acc_map, r[7], with_vendor=True),
        }
        for r in q.all()
    ]
    rows = _own_rows(db, rows, acc_map)   # account_fields 之后补解析层
    rows.sort(key=lambda x: -x["amount"])
    total = len(rows)
    start_i = (page - 1) * page_size
    return {"total": total, "items": rows[start_i:start_i + page_size],
            "note": "口径：amount=日成本（按量+摊销，账单金额口径，判断花费以此为准）。T+1 账单，近 7 天可能补账修订"}


@router.get("/summary")
def summary(
    start: str = Query("", description="YYYY-MM-DD，默认最近30天"),
    end: str = Query("", description="YYYY-MM-DD"),
    account_id: int = Query(0, ge=0, description="云账号 id，0=全部（跟随顶部账号切换）"),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """区间汇总，供成本看板复用。

    口径拆分：payg=按量消费(bill/PayAsYouGo)；amortized=包年包月摊销；
    order=当日订单/退款事件(bill/Subscription，一次性不计入日成本)；
    total(日成本)=payg+amortized。

    billing_applicable：当前范围内是否存在"启用且支持账单采集"的
    云账号（aliyun/tencent）。没有时前端显示"账单面不适用"而不是 ¥0.00/
    "请先采集"——vCenter/自建库等非计费数据源本来就没有账单，伪装成零花费
    会让人误以为账单采集坏了。
    """
    d_end = _parse_date(end) if end else date.today() - timedelta(days=1)
    d_start = _parse_date(start) if start else d_end - timedelta(days=29)
    if d_start > d_end:
        raise HTTPException(400, "start 不能晚于 end")
    from app.models.cloud_account import CloudAccount
    from app.services.cloud.provider import vendors_with
    from app.services.tenant import account_filter

    billing_q = db.query(func.count(CloudAccount.id)).filter(
        CloudAccount.enabled.is_(True),
        CloudAccount.vendor.in_(vendors_with("billing")),
    )
    if account_id:
        billing_q = billing_q.filter(CloudAccount.id == account_id)
    billing_accounts = int(billing_q.scalar() or 0)
    base = account_filter(db.query(CostDaily).filter(
        CostDaily.stat_date >= d_start, CostDaily.stat_date <= d_end
    ), CostDaily, account_id)
    by_day_map: dict = {}
    by_prod_map: dict = {}
    for r in base.with_entities(
        CostDaily.stat_date, CostDaily.source, CostDaily.subscription_type,
        CostDaily.product_code, CostDaily.product_name, CostDaily.pretax_amount,
    ).all():
        d, src, sub, pc, pn, amt = r
        if src == "amortized":
            kind = "amortized"
        elif sub == "PayAsYouGo":
            kind = "payg"
        else:
            kind = "order"
        day_row = by_day_map.setdefault(d, {"payg": 0.0, "amortized": 0.0, "order": 0.0})
        day_row[kind] += float(amt or 0)
        prod_row = by_prod_map.setdefault(
            pc, {"product_code": pc, "product_name": pn or pc, "payg": 0.0, "amortized": 0.0, "order": 0.0}
        )
        prod_row[kind] += float(amt or 0)
        if not prod_row["product_name"] or prod_row["product_name"] == pc:
            prod_row["product_name"] = pn or pc or ""
    days = []
    d = d_start
    while d <= d_end:
        # has_data=当天是否有账单行。没有行的日期不再当成"花了 0 元"：
        # 历史数据从某天才开始采集时，补零会在趋势图上画出一段"费用归零"的假曲线
        # 尚未开始采集的日期也必须与真实零费用区分。
        # 数值仍回 0 保持老调用方兼容，前端按 has_data 断线/不计入均值。
        has_data = d in by_day_map
        v = by_day_map.get(d, {"payg": 0.0, "amortized": 0.0, "order": 0.0})
        days.append({
            "date": d.isoformat(),
            "payg": round(v["payg"], 2),
            "amortized": round(v["amortized"], 2),
            "order": round(v["order"], 2),
            "total": round(v["payg"] + v["amortized"], 2),
            "has_data": has_data,
        })
        d += timedelta(days=1)
    products = sorted(by_prod_map.values(), key=lambda x: -(x["payg"] + x["amortized"]))
    for p in products:
        p["payg"] = round(p["payg"], 2)
        p["amortized"] = round(p["amortized"], 2)
        p["order"] = round(p["order"], 2)
        p["total"] = round(p["payg"] + p["amortized"], 2)
    return {
        "start": d_start.isoformat(),
        "end": d_end.isoformat(),
        "total": round(sum(x["total"] for x in days), 2),
        "days": days,
        "products": products,
        "billing_applicable": billing_accounts > 0,
        "billing_accounts": billing_accounts,
        "note": "口径：日成本=按量+摊销（账单金额口径，判断花费以此为准）。T+1 账单，近 7 天可能补账修订；"
                "has_data=false 的日期没有账单行，不是零花费",
    }


@router.get("/insights", response_model=CostInsightsResponse)
def cost_insights(
    start: str = Query("", description="YYYY-MM-DD，默认最近30天"),
    end: str = Query("", description="YYYY-MM-DD，默认截至昨日"),
    account_id: int = Query(0, ge=0, description="云账号 id，0=全部"),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """只读本地账单，返回账号、覆盖、排行、异常和有证据的下一步。"""
    from app.services.cost_insights import get_cost_insights

    d_end = _parse_date(end) if end else date.today() - timedelta(days=1)
    d_start = _parse_date(start) if start else d_end - timedelta(days=29)
    try:
        return get_cost_insights(db, account_id, d_start, d_end)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except LookupError as exc:
        raise HTTPException(404, str(exc))


@router.get("/anomalies")
def anomaly_list(
    start: str = Query("", description="YYYY-MM-DD，默认最近30天"),
    end: str = Query("", description="YYYY-MM-DD"),
    type: str = Query("", description="异动类型过滤（单值精确匹配）：payg_spike/payg_new/pack_spike/pack_exhausting/pack_exhausted/pack_expiry/traffic_spike/growth_accel/host_down/aux_spillover/profile_drift/chain_drift"),
    domain: str = Query("", description="类型域过滤：cost=成本类 monitor=监控类 cdn=CDN流量类 profile=画像类，空=全部（成本页传 cost，监控页传 monitor）"),
    level: str = Query(""),
    status: str = Query("", description="生命周期过滤：open/acknowledged/ignored/resolved，空=全部"),
    anomaly_id: int = Query(0, description="按记录 id 精确定位单条（画像/异动互跳深链用），0=不过滤"),
    account_id: int = Query(0, ge=0, description="云账号 id，0=全部；0 值（全局/无法归因）记录仍展示"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """异动记录列表，带生命周期与类型域过滤。

    时间过滤作用于 last_seen（最近复现日）；同一事件不重复入库，
    detect_date 是首次发现日。
    type/domain 可叠加（交集）：type 单值精确匹配，domain 按
    services.cost_anomaly.ANOMALY_TYPE_DOMAINS 取类型集合。
    每条带 profile_links：对象维度对齐的关联画像（漂移状态 + 构成变化摘要
    + ±24h 漂移事件 + 跳转标识），异动详情可点回画像。
    """
    d_end = _parse_date(end) if end else date.today()
    d_start = _parse_date(start) if start else d_end - timedelta(days=29)
    if d_start > d_end:
        raise HTTPException(400, "start 不能晚于 end")
    # anomaly_id 精确定位时不受默认 30 天窗口限制（深链可能指向更早的历史记录）
    q = db.query(CostAnomaly)
    if anomaly_id:
        q = q.filter(CostAnomaly.id == anomaly_id)
    else:
        q = q.filter(CostAnomaly.last_seen >= d_start, CostAnomaly.last_seen <= d_end)
    if account_id:
        # account_id=0 是全局/无法归因项（如画像漂移），选具体账号时仍保留可见，
        # 只收窄账号级记录，不把"无法归因"伪装成"不属于你"
        q = q.filter(or_(CostAnomaly.account_id == account_id,
                         CostAnomaly.account_id == 0))
    if domain:
        types = ANOMALY_TYPE_DOMAINS.get(domain)
        if types is None:
            raise HTTPException(400, f"未知类型域: {domain}（可选 cost/monitor/cdn/profile）")
        q = q.filter(CostAnomaly.type.in_(types))
    if type:
        q = q.filter(CostAnomaly.type == type)
    if level:
        q = q.filter(CostAnomaly.level == level)
    if status:
        q = q.filter(CostAnomaly.ack_status == status)
    total = q.count()
    rows = (
        q.order_by(CostAnomaly.last_seen.desc(), CostAnomaly.value.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    out_items = []
    # 归属标识：异动表无账号列，按对象实例反查资产归属；
    # 池键/全局类（__total__ 等）无实例可查则留空。
    from app.models.asset import Asset
    from app.services.tenant import account_index
    # 异动关联画像按对象维度和 ±24h 漂移窗对齐
    from app.models.behavior_profile import BehaviorProfile
    from app.services.profile_engine import profile_links_for
    all_profiles = db.query(BehaviorProfile).all()

    acc_map = account_index(db)

    def _key_segs(key: str) -> list:
        # target_key 形态：实例ID / 产品|实例|计费项 / process:实例ID / 池键 / __total__
        # ——|与:两种分隔符的段都作为候选实例 ID 反查资产
        out = [key or ""]
        for part in (key or "").replace(":", "|").split("|"):
            out.append(part)
        return [s.strip() for s in out if (s or "").strip() not in ("", "__total__", "__all__")]

    cand_ids: set = set()
    for r in rows:
        cand_ids.update(_key_segs(r.target_key))
    inst_acc: dict = {}
    if cand_ids:
        for a in (db.query(Asset.instance_id, Asset.account_id)
                  .filter(Asset.instance_id.in_(cand_ids)).all()):
            inst_acc[a.instance_id] = a.account_id
    for r in rows:
        account_name = ""
        for seg in _key_segs(r.target_key):
            if seg in inst_acc:
                account_name = (acc_map.get(inst_acc[seg]) or {}).get("name", "")
                break
        item = {
            "id": r.id,
            "detect_date": r.detect_date.isoformat(),
            "last_seen": r.last_seen.isoformat() if r.last_seen else r.detect_date.isoformat(),
            "ack_status": r.ack_status or "open",
            "ack_by": r.ack_by or "",
            "ack_note": r.ack_note or "",
            "account_id": r.account_id or 0,
            "next_notify_at": r.next_notify_at.isoformat() if r.next_notify_at else "",
            "notify_count": r.notify_count or 0,
            "last_notified_at": (r.last_notified_at.isoformat()
                                 if r.last_notified_at else ""),
            "type": r.type,
            "level": r.level,
            "target_key": r.target_key,
            "target_name": r.target_name,
            "title": r.title,
            "content": r.content,
            "value": r.value,
            "baseline": r.baseline,
            "ratio": r.ratio,
            "account": account_name,
            "related_packs": [],
            "biz": resolve_biz(db, r.target_key, r.target_name, f"{r.title} {r.content[:200]}"),
            "profile_links": profile_links_for(
                db, r.target_key, r.target_name, r.title,
                at=r.created_at, profiles=all_profiles),
        }
        # payg 突增/新增 -> 动态关联近 3 天内耗尽的覆盖包（历史记录同样生效，不改库）
        if r.type in ("payg_spike", "payg_new"):
            product = r.target_key.split("|")[0] if "|" in r.target_key else ""
            if product:
                item["related_packs"] = related_pack_exhaustions(db, product, r.last_seen or r.detect_date)
            elif r.target_key == "__total__":
                item["related_packs"] = related_pack_exhaustions(db, "__all__", r.last_seen or r.detect_date)
        out_items.append(item)
    return {"total": total, "items": out_items}


@router.post("/anomalies/ack")
def ack_anomalies(
    body: AnomalyAckRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """异动生命周期操作：确认（acknowledged）/忽略（ignored）/重开（open）。

    确认与忽略后不再进巡检与通知；事件仍在持续时每天只静默刷新 last_seen。
    确认/忽略联动关闭同源内置告警记录（host_down/aux_spillover 双写的另一本账），
    避免异动已处理而告警中心仍挂 firing；重开不回写告警。
    """
    rows = db.query(CostAnomaly).filter(CostAnomaly.id.in_(body.ids)).all()
    found = {r.id for r in rows}
    missing = [i for i in body.ids if i not in found]
    if missing:
        raise HTTPException(404, f"异动记录不存在: {missing[:5]}")
    now = datetime.now()
    for a in rows:
        if body.status == "open":
            a.ack_status, a.ack_by, a.ack_at = "open", "", None
            a.ack_by_id = 0   # 重开时同步清空操作人 id
            a.next_notify_at = None
        else:
            a.ack_status, a.ack_by, a.ack_at = body.status, user.username, now
            a.ack_by_id = user.id
            # 分级提醒按数据源优先级计算重申时间，
            # high 默认 None（直到解决）/ medium 7 天 / low 30 天 / repeat_days=0 永不
            from app.services.alert_ack import compute_next_notify
            a.next_notify_at = compute_next_notify(db, a.account_id, now)
        if body.note is not None:
            a.ack_note = body.note
    # 联动：确认/忽略 -> 同源内置告警一并收口（同一事务，成败一致）
    linked_closed = 0
    if body.status in ("acknowledged", "ignored"):
        label = "确认" if body.status == "acknowledged" else "忽略"
        linked_closed = close_builtin_for_anomalies(
            db, rows, f"异动{label}联动关闭")
    audit.record(
        db, action="cost.anomaly.ack", user_id=user.id, username=user.username,
        resource_type="cost", resource_id="anomalies",
        detail={"ids": body.ids, "status": body.status, "note": body.note,
                "linked_alerts_closed": linked_closed},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return {"updated": len(rows), "linked_alerts_closed": linked_closed}


@router.get("/packs")
def pack_list(
    account_id: int = Query(0, ge=0, description="云账号 id，0=全部（跟随顶部账号切换）"),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """资源包余量/消耗速率/耗尽预测，带抵扣能力池视角。

    单包 days_left 是包内视角；池视角（pool_*）才是告警口径——单包快耗尽
    而池内还有大量额度属正常轮转（state=rotating），不算风险。
    已耗尽的包不逐个列出（exhausted_count 只给汇总数，异动记录负责发现）。
    """
    # 账号过滤时 latest 也按账号取（不同账号采集日期可能不同步）
    latest_q = db.query(func.max(ResourcePackSnapshot.snapshot_date))
    if account_id:
        latest_q = latest_q.filter(ResourcePackSnapshot.account_id == account_id)
    latest = latest_q.scalar()
    if latest is None:
        return {"snapshot_date": None, "items": [], "exhausted_count": 0}
    snap_q = db.query(ResourcePackSnapshot).filter(
        ResourcePackSnapshot.snapshot_date == latest
    )
    if account_id:
        snap_q = snap_q.filter(ResourcePackSnapshot.account_id == account_id)
    snaps = snap_q.all()
    rates = {}
    from_hist_map = {}
    for s in snaps:
        rate, fh = _pack_rate(s, db)
        rates[s.instance_id] = rate
        from_hist_map[s.instance_id] = fh
    pools = compute_pools(snaps, rates)
    today = date.today()
    # 归属标识：account_id -> {vendor, name}，与 alerts/expiry 同口径
    from app.services.tenant import account_fields, account_index

    acc_map = account_index(db)
    items = []
    for s in snaps:
        rate = rates[s.instance_id]
        from_hist = from_hist_map[s.instance_id]
        zh = pack_chinese(s.commodity_code, s.applicable_products)
        pool = pools[pool_key(s.commodity_code, s.applicable_products)]
        days_left = (s.remaining_pct / rate) if rate > 0 and s.remaining_pct > 0 else None
        pool_low = pool["days_left"] is not None and pool["days_left"] <= settings.PACK_EXHAUST_DAYS
        exp = _parse_time(s.expiry_time)
        days_to_exp = (exp.date() - today).days if exp else None
        if s.remaining_pct <= 0.01:
            state = "exhausted"      # 已耗尽（用量在转按量；是否告警看池）
        elif days_left is not None and days_left <= settings.PACK_EXHAUST_DAYS:
            state = "exhausting" if pool_low else "rotating"  # 池内还有额度=正常轮转
        elif days_to_exp is not None and 0 <= days_to_exp <= 30 and s.remaining_pct >= 20:
            state = "expiring"       # 即将到期且有富余（浪费风险）
        elif s.status and s.status != "Available":
            state = s.status.lower()
        else:
            state = "ok"
        items.append({
            "snapshot_date": s.snapshot_date.isoformat(),
            "instance_id": s.instance_id,
            "commodity_code": s.commodity_code,
            "type_label": zh["type_label"],
            "scope_label": zh["scope_label"],
            "products_label": zh["products_label"],
            "package_type": s.package_type,
            "applicable_products": s.applicable_products,
            "region": s.region,
            "deduct_type": s.deduct_type,
            "total_amount": s.total_amount,
            "total_unit": s.total_unit,
            "remaining_amount": s.remaining_amount,
            "remaining_unit": s.remaining_unit,
            "remaining_pct": round(s.remaining_pct, 2),
            "rate_pct_per_day": round(rate, 3),
            "rate_from_history": from_hist,
            "days_left": round(days_left, 1) if days_left is not None else None,
            "days_to_expiry": days_to_exp,
            "effective_time": s.effective_time,
            "expiry_time": s.expiry_time,
            "status": s.status,
            "state": state,
            "pool_key": pool["key"],
            "pool_packs": len(pool["packs"]),
            "pool_remaining_pct": pool["remaining_pct"],
            "pool_days_left": round(pool["days_left"], 1) if pool["days_left"] is not None else None,
            **account_fields(acc_map, s.account_id, with_vendor=True),
        })
    items = _own_rows(db, items, acc_map)   # account_fields 之后补解析层
    # 紧迫度排序：即将耗尽/即将到期/池内轮转 优先级递减，再按剩余比例
    state_order = {"exhausting": 0, "expiring": 1, "rotating": 2, "ok": 3}
    items.sort(key=lambda x: (state_order.get(x["state"], 4), -(x["days_left"] or 9999)))
    # 已耗尽的包不逐个展示：余量 0 的包没有观测意义，
    # "发现已用完"由池级异动（pack_exhausted）和本接口的 exhausted_count 汇总负责。
    # 费用影响量化在 /cost/packs/impact，仍用全部死包，不受此过滤影响。
    exhausted_count = sum(1 for x in items if x["state"] == "exhausted")
    alive = [x for x in items if x["state"] != "exhausted"]
    return {"snapshot_date": latest.isoformat(), "items": alive, "exhausted_count": exhausted_count}


@router.get("/packs/impact")
def packs_impact(
    account_id: int = Query(0, ge=0, description="云账号 id，0=全部（跟随顶部账号切换）"),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """已耗尽/即将耗尽包的按量费用影响量化。

    耗尽前后各 7 天 covered products 按量日均对比：回答"包没了会多花多少钱"。
    耗尽日早于账单数据范围的包标记 available=false（无基线可对比）。
    """
    latest_q = db.query(func.max(ResourcePackSnapshot.snapshot_date))
    if account_id:
        latest_q = latest_q.filter(ResourcePackSnapshot.account_id == account_id)
    latest = latest_q.scalar()
    if latest is None:
        return {"items": []}
    snap_q = db.query(ResourcePackSnapshot).filter(
        ResourcePackSnapshot.snapshot_date == latest
    )
    if account_id:
        snap_q = snap_q.filter(ResourcePackSnapshot.account_id == account_id)
    snaps = snap_q.all()
    items = []
    for s in snaps:
        if s.remaining_pct > 0.01:
            continue  # 只分析已耗尽的包
        zh = pack_chinese(s.commodity_code, s.applicable_products)
        items.append({
            "type_label": zh["type_label"],
            "products_label": zh["products_label"],
            **impact_analysis(db, s.instance_id, s.applicable_products),
        })
    items.sort(key=lambda x: -(x.get("extra_daily") or 0))
    return {"items": items,
            "note": "口径：extra_daily=账单金额口径的日均多花（pretax_amount，判断花费以此为准）；"
                    "T+1 账单非终态，近 7 天可能补账修订"}


@router.get("/drilldown")
def payg_drilldown(
    days: int = Query(7, ge=1, le=30),
    product: str = Query(""),
    account_id: int = Query(0, ge=0, description="云账号 id，0=全部（跟随顶部账号切换）"),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """昨日按量付费下钻：按产品、实例、用量和资源包消耗归因。
    口径：按量消费（bill/PayAsYouGo），T+1 账单。"""
    from app.models.cdn_domain_daily import CdnDomainDaily
    from app.services.pack_impact import compute_pools, pack_chinese, pool_key
    from app.services.tenant import account_filter

    today = date.today()
    y_day = today - timedelta(days=1)

    def payg_q(d):
        return account_filter(db.query(CostDaily).filter(
            CostDaily.stat_date == d, CostDaily.source.in_(("bill", "tc_bill")),
            CostDaily.subscription_type == "PayAsYouGo"), CostDaily, account_id)

    y_rows = payg_q(y_day).all()
    if not y_rows:
        return {"date": y_day.isoformat(), "available": False,
                "note": f"{y_day.isoformat()} 的按量账单未采集（T+1，每日 10:00 自动）"}
    # 金额口径：主数=pretax_amount（账单金额，判断花费的唯一口径）；
    # payment_amount 是挂账现金字段（常为 0），不作花费依据、不得推断抵扣
    total = round(sum(r.pretax_amount or 0 for r in y_rows), 2)
    prev_rows = payg_q(y_day - timedelta(days=1)).all()
    prev_total = round(sum(r.pretax_amount or 0 for r in prev_rows), 2)

    # 1) 产品拆解（金额+环比，账单金额口径；产品×账号拆行）
    # 归属标识：account_id -> {vendor, name}，与 alerts/expiry 同口径
    from app.services.tenant import account_fields, account_index

    acc_map = account_index(db)
    by_prod: dict = {}
    for r in y_rows:
        k = (r.product_name or r.product_code, r.account_id or 0)
        by_prod[k] = by_prod.get(k, 0.0) + (r.pretax_amount or 0)
    prev_by_prod: dict = {}
    for r in prev_rows:
        k = (r.product_name or r.product_code, r.account_id or 0)
        prev_by_prod[k] = prev_by_prod.get(k, 0.0) + (r.pretax_amount or 0)
    products = []
    for k, v in sorted(by_prod.items(), key=lambda kv: -kv[1]):
        prev = prev_by_prod.get(k)
        products.append({
            "product": k[0], "amount": round(v, 2),
            "pct": round(v / total * 100, 1) if total else 0,
            "delta_pct": round((v - prev) / prev * 100, 1) if prev else None,
            **account_fields(acc_map, k[1], with_vendor=True),
        })
    products = _own_rows(db, products, acc_map)   # 聚合行无实体键时自动跳过（不猜）

    # 2) 实例用量明细（金额 + 用量值 + 计费项），可按产品过滤
    q = payg_q(y_day)
    if product:
        q = q.filter(CostDaily.product_name == product)
    filtered_rows = q.all()
    inst: dict = {}
    for r in filtered_rows:
        k = (r.instance_id or "-", r.instance_name or r.instance_id or "-", r.account_id or 0)
        e = inst.setdefault(k, {"amount": 0.0, "items": set(),
                                "product": r.product_name or r.product_code})
        e["amount"] += r.pretax_amount or 0
        if r.billing_item:
            e["items"].add(r.billing_item)
    # 注：BSS 账单 DAILY 明细不带用量数字（实测字段只有金额/产品/服务时长）——
    # "用量多少"只在有真实数据面处展示（如 CDN 域名流量 GB），不硬凑
    instances = sorted(
        ({"instance_id": k[0], "name": k[1], "amount": round(v["amount"], 2),
          "product": v["product"], "billing_items": sorted(v["items"])[:4],
          "pct": round(v["amount"] / total * 100, 1) if total else 0,
          **account_fields(acc_map, k[2], with_vendor=True)}
         for k, v in inst.items()), key=lambda x: -x["amount"])[:30]
    instances = _own_rows(db, instances, acc_map)   # account_fields 之后补解析层

    # 2.5) 计费场景汇总：
    # 按 billing_item（=计费场景，采集器从 InstanceConfig 解析）归并，场景×账号拆行。
    # 金额口径：amount=pretax_amount（账单金额，判断花费的唯一口径）
    scen: dict = {}
    scen_total = sum(r.pretax_amount or 0 for r in filtered_rows)
    for r in filtered_rows:
        s = (r.billing_item or "（未标注场景）", r.account_id or 0)
        e = scen.setdefault(s, {"amount": 0.0, "instances": set(), "product": r.product_name})
        e["amount"] += r.pretax_amount or 0
        if r.instance_id:
            e["instances"].add(r.instance_id)
    scenarios = sorted(
        ({"scenario": k[0], "product": v["product"],
          "amount": round(v["amount"], 2),
          "pct": round(v["amount"] / scen_total * 100, 1) if scen_total else 0,
          "instances": sorted(v["instances"])[:5],
          **account_fields(acc_map, k[1], with_vendor=True)}
         for k, v in scen.items()), key=lambda x: -x["amount"])[:20]
    scenarios = _own_rows(db, scenarios, acc_map)   # 聚合行无实体键时自动跳过（不猜）
    scenario_note = ("统计说明：按量账单的计费场景（BSS DAILY 不给 BillingItem，场景取自实例配置描述）；"
                     "金额=账单金额（pretax_amount，判断花费以此为准）；"
                     "payment_amount 是挂账现金字段（常 0），仅存证不作依据")

    # 3) 资源包消耗归因：谁在烧（近 days 天）
    since = today - timedelta(days=days)
    packs_out = []
    latest_snap = db.query(func.max(ResourcePackSnapshot.snapshot_date)).scalar()
    if latest_snap:
        snaps = db.query(ResourcePackSnapshot).filter(
            ResourcePackSnapshot.snapshot_date == latest_snap).all()
        rates = {x.instance_id: _pack_rate(x, db)[0] for x in snaps}
        pools = compute_pools(snaps, rates)
        for key, p in pools.items():
            member = next(x for x in snaps if pool_key(x.commodity_code, x.applicable_products) == key)
            zh = pack_chinese(member.commodity_code, member.applicable_products)
            consumers = []
            if "cdn" in (member.commodity_code or "").lower() or "dcdn" in (zh["type_label"]):
                # CDN 池：域名归因（数据面在 cdn_domain_daily）
                tot_bytes = db.query(func.sum(CdnDomainDaily.traffic_bytes)).filter(
                    CdnDomainDaily.stat_date >= since).scalar() or 0
                rows = (db.query(CdnDomainDaily.domain, func.sum(CdnDomainDaily.traffic_bytes))
                        .filter(CdnDomainDaily.stat_date >= since)
                        .group_by(CdnDomainDaily.domain)
                        .order_by(func.sum(CdnDomainDaily.traffic_bytes).desc()).limit(5).all())
                for d_name, b in rows:
                    gb = (b or 0) / 1024 ** 3
                    consumers.append({"name": d_name, "value": round(gb, 1), "unit": "GB",
                                      "pct": round(b / tot_bytes * 100, 1) if tot_bytes else 0})
                consumer_note = f"近 {days} 天域名流量归因"
            else:
                # 其他池的 DAILY 账单 usage 为空，不能用于抵扣量归因；
                # 使用包抵完后昨日仍扣费的计费场景，避免无意义查询。
                codes = {c.strip() for c in (member.applicable_products or "").split(",") if c.strip()}
                scen_map: dict = {}
                for r in y_rows:
                    if codes and (r.product_code or "") not in codes:
                        continue
                    s = r.billing_item or r.product_name or "-"
                    scen_map[s] = scen_map.get(s, 0.0) + (r.pretax_amount or 0)
                tot_s = sum(scen_map.values()) or 1
                for s, amt in sorted(scen_map.items(), key=lambda kv: -kv[1])[:5]:
                    consumers.append({"name": s, "value": round(amt, 2), "unit": "元",
                                      "pct": round(amt / tot_s * 100, 1)})
                consumer_note = ("昨日账单场景的金额构成（账单金额口径）；"
                                 "被包抵掉的抵扣量无账单行，故不展示'谁在烧抵扣量'")
            packs_out.append({
                "type_label": zh["type_label"], "pool_pct": round(p["remaining_pct"], 1),
                "days_left": round(p["days_left"], 1) if p["days_left"] is not None else None,
                "consumers": consumers, "note": consumer_note,
            })
    return {
        "date": y_day.isoformat(),
        "scenarios": scenarios,
        "scenario_note": scenario_note, "available": True,
        # 金额口径：total/amount=pretax_amount（账单金额，判断花费的唯一口径）
        "total": total,
        "prev_total": prev_total,
        "delta_pct": round((total - prev_total) / prev_total * 100, 1) if prev_total else None,
        "products": products, "instances": instances, "packs": packs_out,
        "note": "金额口径：total/amount=账单金额（pretax_amount，判断花费以此为准）；"
                "payment_amount=挂账现金字段（常 0），仅存证不作依据，不得推断资源包抵扣。"
                "T+1 账单，近 7 天可能补账修订",
    }


@router.get("/purchase-advice")
def purchase_advice_api(
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """采购建议：该买/该治/浪费——给结论不给数据点。"""
    from app.services.purchase_advice import purchase_advice

    return purchase_advice(db)


@router.get("/cdn-domains")
def cdn_domain_traffic(
    days: int = Query(14, ge=1, le=90),
    top: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """CDN/DCDN 按域名流量 Top：流量包到底被哪些域名烧掉。

    与诊断工具 cdn_domain_traffic 同一聚合口径（services/cdn_traffic.py）。
    """
    from app.services.cdn_traffic import domain_traffic_summary

    return domain_traffic_summary(db, days=days, top=top)


@router.get("/cdn-urls")
def cdn_url_breakdown_api(
    domain: str = Query(..., max_length=255, description="CDN 域名，如 cdn.example.com"),
    days: int = Query(7, ge=1, le=30),
    top: int = Query(10, ge=1, le=30),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """URL/文件级流量下钻（CDN 突增归因）：峰值日较前日哪些 URL 在涨。"""
    from app.services.cdn_traffic import url_breakdown

    return url_breakdown(db, domain=domain.strip().lower(), days=days, top=top)


@router.post("/detect")
def trigger_detect(
    body: DetectRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """手动重跑异动检测（默认 T-1 按量；packs=true 时同时刷新资源包快照）。不推送通知。"""
    day = _parse_date(body.date) if body.date else date.today() - timedelta(days=1)
    try:
        new = detect_cost_for_date(db, day, notify=False)
        pack_result = snapshot_and_detect_packs(db, notify=False) if body.packs else None
    except Exception as e:
        logger.exception("手动异动检测失败")
        raise HTTPException(502, f"检测失败: {str(e)[:200]}")
    audit.record(
        db, action="cost.detect", user_id=user.id, username=user.username,
        resource_type="cost", resource_id=day.isoformat(),
        detail={"date": day.isoformat(), "packs": body.packs, "found": len(new)},
        client_ip=get_client_ip(request),
    )
    db.commit()
    return {
        "date": day.isoformat(),
        "new_anomalies": len(new),
        "anomalies": [
            {"type": a.type, "level": a.level, "title": a.title, "value": a.value,
             "baseline": a.baseline, "ratio": a.ratio}
            for a in new
        ],
        "packs": pack_result,
    }
