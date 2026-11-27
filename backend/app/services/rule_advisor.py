"""按资产覆盖与历史指标分析告警规则缺口，并给出可解释的阈值建议。

不依赖人工预置的资产清单，分析过程遵循以下口径：

  1. 缺口识别：按账号×产品盘点实际资产，对照已启用规则（含 0=全部账号的规则），
     找出"有资产、有指标通道、但没有规则"的组合；停用账号、域名/证书（到期预警）
     与 CDN（内置漂移检测）不参与，口径与数据完整度顾问一致；
  2. 阈值建议：读 VictoriaMetrics 近 N 天真实样本，按目标归属过滤到本账号实例，
     算 P95/峰值。百分比类指标默认 90%，现状 P95 已逼近/超过时按 P95+2 上浮并标注
     "现状已高"风险；计数/速率类保持通用阈值，只有实测更高时才上浮；
  3. 可解释：每条候选带样本数、P95、峰值、建议理由；无数据的候选明确标注
     "采用通用阈值"，不假装分析过；
  4. 只读分析：analyze() 零写库；用户在前端点选后 create_selected() 才落库，
     且服务端重新分析、只创建仍缺失的规则（不信任前端传来的阈值）。

红线：规则创建不发通知（channel_ids=[]，站内记录）；阈值全部来自确定性统计，
不让 LLM 产出阈值/SQL/PromQL。
"""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime

from sqlalchemy.orm import Session

from app.collector.cms import ALERT_METRIC_CONFIG
from app.collector.vm import metric_name as vm_metric_name
from app.collector.vm import query_range as vm_query_range
from app.models.alert_rule import AlertRule
from app.models.asset import Asset
from app.models.cloud_account import CloudAccount
from app.services.alert_eval import series_target
from app.services.cloud.provider import RESOURCE_TYPES
from app.services.copilot.workflows.onboard import DEFAULT_METRIC_RULES

logger = logging.getLogger("qianshu.rule_advisor")

PRODUCT_LABELS = {
    **RESOURCE_TYPES,
    "ecs": "ECS", "rds": "RDS", "redis": "Redis", "slb": "SLB",
    "vmware_vm": "VMware虚拟机", "vmware_host": "VMware主机",
}
# 统计查询上限：异常大的账号矩阵下宁可少算几个候选，也不让页面等几分钟
MAX_STATS_QUERIES = 30
STEP_S = 600          # 10 分钟粒度足够算 P95，响应体积可控
LOOKBACK_DAYS_MAX = 30

# 分类口径与数据完整度顾问保持一致：
# 域名/证书由到期预警覆盖、CDN 有内置漂移检测，都不能算"无通道缺口"；
# 停用账号的资产不参与监测，也不应生成新规则。
_EXPIRY_PRODUCTS = ("domain", "ssl_cert")
_BUILTIN_RULE_PRODUCTS = {"cdn": "CDN 流量/域名构成漂移内置检测"}


def _product_label(product: str) -> str:
    return PRODUCT_LABELS.get(product, product.upper())


def _metric_label(product: str, metric: str) -> str:
    for m, label, *_ in ALERT_METRIC_CONFIG.get(product, []):
        if m == metric:
            return label
    return metric


def _unit_of(product: str, metric: str) -> str:
    for m, _label, unit in ALERT_METRIC_CONFIG.get(product, []):
        if m == metric:
            return unit
    return ""


def _p95(values: list) -> float:
    values.sort()
    return values[int(0.95 * (len(values) - 1))]


def _stats_for(product: str, metric: str, instance_ids: set, days: int) -> dict | None:
    """近 N 天真实样本统计（只读 VM）。返回 None=无样本。"""
    name = vm_metric_name(product, metric)
    end_s = int(time.time())
    start_s = end_s - days * 86400
    try:
        result = vm_query_range(name, start_s, end_s, STEP_S)
    except Exception as e:
        logger.warning("规则分析查询失败 %s: %s", name, str(e)[:150])
        return None
    values: list = []
    targets = 0
    for series in result:
        target, _part = series_target(series.get("metric", {}))
        if instance_ids and target not in instance_ids:
            continue
        pts = []
        for _ts, v in series.get("values", []):
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if math.isnan(f) or math.isinf(f):
                continue
            pts.append(f)
        if pts:
            targets += 1
            values.extend(pts)
    if not values:
        return None
    return {
        "targets": targets,
        "points": len(values),
        "p95": round(_p95(values), 2),
        "max": round(max(values), 2),
    }


def _propose_threshold(product: str, metric: str, default: float, stats: dict | None) -> tuple:
    """返回 (threshold, risk, reason_suffix)。百分比指标按现状上浮，计数类保守。"""
    unit = _unit_of(product, metric)
    if not stats:
        return default, "", "近 {days} 天无采集样本，采用通用阈值"
    p95 = float(stats["p95"])
    max_v = float(stats["max"])
    if unit == "%":
        if p95 >= default:
            thr = min(99.5, round(p95 + 2, 1))
            return thr, "high_baseline", (
                f"近 {{days}} 天 {stats['targets']} 条指标序列 {stats['points']} 个样本，"
                f"P95={p95:g}%（已接近/超过通用阈值 {default:g}%），阈值上浮到 {thr:g}%，"
                "建规则后仍可能频繁触发，建议先扩容/降载")
        if p95 >= default - 5:
            # P95 尚在阈值内、但距阈值不足 5 个点：峰值很可能短时越限。
            # 不因此上浮阈值（会把真实风险藏起来），只如实标注，让用户先知道。
            return default, "near_baseline", (
                f"近 {{days}} 天 {stats['targets']} 条指标序列 {stats['points']} 个样本，"
                f"P95={p95:g}%、峰值={max_v:g}%，距通用阈值 {default:g}% 仅 {default - p95:.1f} 个点，"
                "峰值可能短时触发；阈值保持通用值，建议关注并考虑扩容")
        return default, "", (
            f"近 {{days}} 天 {stats['targets']} 条指标序列 {stats['points']} 个样本，"
            f"P95={p95:g}%、峰值={max_v:g}%，通用阈值 {default:g}% 有足够余量")
    # 计数/速率/延迟类：默认阈值已人工校准过告警量，实测更高时按 2 倍留余量
    if p95 > default:
        thr = round(max(default, p95 * 2), 2)
        return thr, "", (
            f"近 {{days}} 天 {stats['targets']} 条指标序列 {stats['points']} 个样本，"
            f"P95={p95:g}、峰值={stats['max']:g}，按峰值余量上浮到 {thr:g}")
    return default, "", (
        f"近 {{days}} 天 {stats['targets']} 条指标序列 {stats['points']} 个样本，"
        f"P95={p95:g}、峰值={stats['max']:g}，采用通用阈值 {default:g}")


def analyze(db: Session, account_id: int = 0, days: int = 7) -> dict:
    """只读：盘点覆盖缺口 + 按真实样本给阈值建议。account_id=0 全账号。"""
    days = max(1, min(int(days or 7), LOOKBACK_DAYS_MAX))
    q = db.query(Asset).filter(Asset.status != "Deleted")
    if account_id:
        q = q.filter(Asset.account_id == account_id)
    else:
        # 全账号分析：停用账号已停止采集/监测，其资产不参与缺口与自动建规则
        # （显式指定账号时尊重用户选择，不在此收窄）。
        acc_rows = db.query(CloudAccount.id, CloudAccount.enabled).all()
        if acc_rows:
            q = q.filter(Asset.account_id.in_(
                {aid for aid, enabled in acc_rows if enabled} | {0}))
    assets = q.all()

    acc_names = {a.id: a.name for a in db.query(CloudAccount).all()}
    groups: dict = {}
    unsupported: dict = {}      # product -> asset count（无阈值规则通道）
    expiry: dict = {}           # 域名/证书：到期预警覆盖
    builtin: dict = {}          # CDN：内置漂移检测覆盖
    for a in assets:
        prod = a.product or "unknown"
        if prod in _EXPIRY_PRODUCTS:
            expiry[prod] = expiry.get(prod, 0) + 1
            continue
        if prod in _BUILTIN_RULE_PRODUCTS:
            builtin[prod] = builtin.get(prod, 0) + 1
            continue
        if prod not in ALERT_METRIC_CONFIG:
            unsupported[prod] = unsupported.get(prod, 0) + 1
            continue
        groups.setdefault((a.account_id or 0, prod), []).append(a)

    rules = db.query(AlertRule).filter(AlertRule.source == "metric").all()
    # 覆盖判定：0=全部账号的规则覆盖所有账号；>0 只覆盖自身
    covered_all: set = set()
    covered_acc: set = set()
    for r in rules:
        if not r.product or not r.metric:
            continue
        if (r.account_id or 0) == 0:
            covered_all.add((r.product, r.metric))
        else:
            covered_acc.add((r.account_id, r.product, r.metric))
    existing_names = {r.name for r in rules}

    # 指标类资产覆盖：产品有任一适用的启用规则即算覆盖
    enabled_cov: set = set()
    for r in rules:
        if r.enabled and r.product:
            if (r.account_id or 0) == 0:
                enabled_cov.add(("*", r.product))
            else:
                enabled_cov.add((r.account_id, r.product))

    candidates: list = []
    n_queries = 0
    covered_assets = 0
    metric_assets = 0
    for (acc_id, product), group in sorted(groups.items()):
        metric_assets += len(group)
        if (acc_id, product) in enabled_cov or ("*", product) in enabled_cov:
            covered_assets += len(group)
        instance_ids = {a.instance_id for a in group if a.instance_id}
        for metric, mlabel, cmp_, default_thr, level, dur in DEFAULT_METRIC_RULES.get(product, []):
            if (product, metric) in covered_all or (acc_id, product, metric) in covered_acc:
                continue
            name = f"自动规则·{acc_names.get(acc_id, acc_id)}·{_product_label(product)}·{mlabel}"
            if name in existing_names:
                continue
            stats = None
            if n_queries < MAX_STATS_QUERIES:
                n_queries += 1
                stats = _stats_for(product, metric, instance_ids, days)
            threshold, risk, reason = _propose_threshold(product, metric, float(default_thr), stats)
            reason = reason.format(days=days)
            candidates.append({
                "key": f"{acc_id}:{product}:{metric}",
                "account_id": acc_id,
                "account_name": acc_names.get(acc_id, str(acc_id)),
                "product": product,
                "product_label": _product_label(product),
                "metric": metric,
                "metric_label": mlabel,
                "unit": _unit_of(product, metric),
                "comparator": cmp_,
                "threshold": threshold,
                "duration": dur,
                "level": level,
                "name": name,
                "reason": reason,
                "risk": risk,
                "data_backed": bool(stats),
                "stats": stats,
                "asset_count": len(group),
            })

    # 孤儿规则：规则指向的产品在本范围内没有资产（改名/下云后残留）
    live_products = {p for (_a, p) in groups}
    orphan_rules = [
        {"id": r.id, "name": r.name, "product": r.product}
        for r in rules
        if r.product and r.product not in live_products and (not account_id or (r.account_id or 0) in (0, account_id))
    ][:20]

    findings: list = []
    if unsupported:
        detail = "、".join(f"{_product_label(p)} {n} 台" for p, n in sorted(unsupported.items()))
        findings.append(
            f"{sum(unsupported.values())} 台资产不在自动阈值规则范围（{detail}）："
            "这些产品线当前没有阈值规则通道，不生成候选，也不计入告警规则覆盖率评分")
    if expiry:
        detail = "、".join(f"{_product_label(p)} {n} 台" for p, n in sorted(expiry.items()))
        findings.append(f"{detail}由到期预警覆盖，不建阈值规则")
    if builtin:
        detail = "、".join(f"{_product_label(p)} {n} 台，"
                          f"{_BUILTIN_RULE_PRODUCTS[p]}" for p, n in sorted(builtin.items()))
        findings.append(f"{detail}，不建阈值规则")
    if orphan_rules:
        findings.append(
            f"发现 {len(orphan_rules)} 条规则指向当前已无资产的产品（如已下云/改名），"
            "建议在规则列表核对后禁用或删除")
    if metric_assets and not candidates and not findings:
        findings.append("指标类资产已被现有规则覆盖，暂无需新建规则")

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "days": days,
        "account_id": account_id,
        "summary": {
            "assets": len(assets),
            "metric_assets": metric_assets,
            "covered_assets": covered_assets,
            "unsupported_assets": sum(unsupported.values()),
            "expiry_assets": sum(expiry.values()),
            "builtin_assets": sum(builtin.values()),
            "existing_rules": len(rules),
            "candidates": len(candidates),
            "data_backed": sum(1 for c in candidates if c["data_backed"]),
            "orphan_rules": len(orphan_rules),
            "stats_queries": n_queries,
        },
        "findings": findings,
        "candidates": candidates,
    }


def create_selected(db: Session, keys: list, username: str = "") -> dict:
    """创建用户点选的候选规则（服务端重新分析，只建仍缺失的，阈值不信任前端）。"""
    wanted = {str(k) for k in (keys or []) if k}
    if not wanted:
        return {"created": [], "skipped": [], "note": "未选择任何规则"}
    report = analyze(db, account_id=0)
    by_key = {c["key"]: c for c in report["candidates"]}
    created, skipped = [], []
    taken = {r.name for r in db.query(AlertRule).all()}
    for key in sorted(wanted):
        c = by_key.get(key)
        if c is None:
            skipped.append({"key": key, "reason": "已存在或被更高优先级规则覆盖"})
            continue
        if c["name"] in taken:
            skipped.append({"key": key, "reason": "同名规则已存在"})
            continue
        db.add(AlertRule(
            name=c["name"], source="metric", level=c["level"],
            product=c["product"], metric=c["metric"],
            comparator=c["comparator"], threshold=float(c["threshold"]),
            duration=int(c["duration"]), channel_ids="[]", silences="[]",
            account_id=int(c["account_id"] or 0), origin="auto", enabled=True,
        ))
        taken.add(c["name"])
        created.append({"key": key, "name": c["name"], "threshold": c["threshold"],
                        "reason": c["reason"]})
    db.commit()
    return {"created": created, "skipped": skipped}
