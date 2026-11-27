"""本地账单只读洞察：成本事实、覆盖、排行与有证据的下一步。

本模块不触发采集、通知、异常生命周期写入或采购计算。页面与智能体必须共用
get_cost_insights；空日期没有补零语义，真实零费用只来自逐日成功采集证据。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from statistics import median

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.cloud_account import CloudAccount
from app.models.cost_daily import CostDaily
from app.models.onboarding_run import OnboardingRun
from app.services import onboarding
from app.services.baseline import V_SPIKE, day_series_check
from app.services.holiday import day_kind

_BILL_SOURCES = ("bill", "tc_bill")
_HISTORY_PROOF_DAYS = 7
_MAX_RECOMMENDATIONS = 3


def _money(value: float | None) -> float | None:
    return round(float(value), 2) if value is not None else None


def _dt_value(value) -> str:
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value)


def _dates(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def _kind(row: CostDaily) -> str:
    if row.source == "amortized":
        return "amortized"
    if row.source in _BILL_SOURCES and row.subscription_type == "PayAsYouGo":
        return "payg"
    if row.source in _BILL_SOURCES:
        return "refund" if float(row.pretax_amount or 0) < 0 else "order"
    return "other"


def _object_id(row: CostDaily) -> str:
    return "|".join((row.product_code or "", row.instance_id or "", row.billing_item or ""))


def _object(row: CostDaily, kind: str = "billing_item") -> dict:
    object_id = (
        _object_id(row) if kind == "billing_item"
        else (row.instance_id or row.product_code or "") if kind == "instance"
        else (row.product_code or "")
    )
    return {
        "kind": kind,
        "id": object_id,
        "product_code": row.product_code or "",
        "product_name": row.product_name or row.product_code or "",
        "instance_id": row.instance_id or "",
        "instance_name": row.instance_name or "",
        "billing_item": row.billing_item or "" if kind == "billing_item" else "",
        "region": row.region or "",
    }


def _daily_evidence(db: Session, account_id: int) -> dict[str, dict]:
    run = (db.query(OnboardingRun)
           .filter(OnboardingRun.account_id == account_id).first())
    if run is None:
        return {}
    account = db.get(CloudAccount, account_id)
    return {
        item["scope"]["bill_date"]: item
        for item in onboarding.capability_results(run, account)
        if item["capability"] == "cost" and item["scope"].get("bill_date")
    }


def _day_fact(day: date, rows: list[CostDaily], evidence: dict | None,
              account_id: int, vendor: str) -> dict:
    bill_rows = [x for x in rows if x.source in _BILL_SOURCES]
    amortized_rows = [x for x in rows if x.source == "amortized"]
    expected_ref = f"cost_daily:{account_id}:{day.isoformat()}"
    # 新接入链的空账单只有逐日成功记录能证明零费用；旧 Run/占位成功不补全证据。
    evidence_success = bool(
        evidence and evidence.get("execution") in ("succeeded", "skipped")
        and evidence.get("checked_at") and expected_ref in (evidence.get("evidence_refs") or [])
    )
    bill_complete = bool(bill_rows) or evidence_success
    amortized_complete = bool(amortized_rows) or (
        bill_complete and vendor != "aliyun"
    ) or (
        evidence_success and evidence.get("coverage", {}).get("truncated") is False
    )
    amounts = {"payg": 0.0, "amortized": 0.0, "order": 0.0, "refund": 0.0}
    for row in rows:
        kind = _kind(row)
        if kind in amounts:
            amounts[kind] += float(row.pretax_amount or 0)
    payg = amounts["payg"] if bill_complete else None
    amortized = amounts["amortized"] if amortized_complete else None
    order = amounts["order"] if bill_complete else None
    refund = amounts["refund"] if bill_complete else None
    # 缺一类证据时仍可展示已获取部分，但 complete=false；完全没证据则保持空值。
    partial_total = (
        (payg or 0) + (amortized or 0)
        if bill_complete or amortized_rows else None
    )
    timestamps = [_dt_value(x.created_at) for x in rows if x.created_at]
    if evidence_success:
        timestamps.append(_dt_value(evidence.get("checked_at")))
    return {
        "date": day.isoformat(),
        "payg": _money(payg),
        "amortized": _money(amortized),
        "total": _money(partial_total),
        "order": _money(order),
        "refund": _money(refund),
        "has_data": bool(rows) or evidence_success,
        "complete": bill_complete and amortized_complete,
        "bill_complete": bill_complete,
        "amortized_complete": amortized_complete,
        "collected_at": max(timestamps) if timestamps else "",
        "evidence_refs": [expected_ref] if bill_complete else [],
        "_rows": rows,
    }


def _rank(rows: list[CostDaily], *, instances: bool, account_id: int) -> list[dict]:
    groups: dict[tuple, dict] = {}
    for row in rows:
        kind = _kind(row)
        if kind not in ("payg", "amortized", "order", "refund"):
            continue
        key = ((row.product_code or "", row.instance_id or "") if instances
               else (row.product_code or "",))
        group = groups.setdefault(key, {
            "sample": row, "payg": 0.0, "amortized": 0.0,
            "order": 0.0, "refund": 0.0, "dates": set(),
            "product_name": "", "instance_name": "",
        })
        group[kind] += float(row.pretax_amount or 0)
        group["dates"].add(row.stat_date)
        if not group["product_name"] and row.product_name:
            group["product_name"] = row.product_name
        if not group["instance_name"] and row.instance_name:
            group["instance_name"] = row.instance_name
    total_cost = sum(x["payg"] + x["amortized"] for x in groups.values())
    output = []
    for group in groups.values():
        sample = group["sample"]
        amount = group["payg"] + group["amortized"]
        obj = _object(sample, "instance" if instances else "product")
        obj["product_name"] = group["product_name"] or obj["product_name"]
        obj["instance_name"] = group["instance_name"] or obj["instance_name"]
        output.append({
            "object": obj,
            "payg": _money(group["payg"]),
            "amortized": _money(group["amortized"]),
            "total": _money(amount),
            "order": _money(group["order"]),
            "refund": _money(group["refund"]),
            "share_pct": round(amount / total_cost * 100, 2) if total_cost else None,
            "time_range": {
                "from": min(group["dates"]).isoformat(),
                "to": max(group["dates"]).isoformat(),
            },
            "evidence_refs": [f"cost_daily:{account_id}:{day.isoformat()}"
                              for day in sorted(group["dates"])],
        })
    output.sort(key=lambda x: (-(x["total"] or 0), x["object"]["id"]))
    return output


def _recommendation(account_id: int, action: str, finding: dict,
                    impact_amount: float | None, sufficient: bool) -> dict:
    obj = finding["object"]
    label = " / ".join(x for x in (obj.get("product_name"), obj.get("instance_name") or obj.get("instance_id"),
                                      obj.get("billing_item")) if x)
    text = {
        "investigate_cost_growth": "排查异常增长",
        "verify_first_observed_cost": "核实首次观察费用",
        "evaluate_sustained_payg": "评估持续高额按量支出",
    }[action]
    if action == "investigate_cost_growth":
        reason = "同账号、同计费项周期基线显示异常增长；先核对业务变化和账单贡献。"
    elif action == "verify_first_observed_cost":
        reason = "这是已采集范围内首次观察到的费用；核实业务必要性，不能据此推断刚开通服务。"
    else:
        reason = "支出连续出现且金额较高，可评估更合适的计费方式；当前不给出采购数量、SKU 或节省金额。"
    if finding.get("usage") is None:
        reason += " 缺少可比较用量证据，暂不判断单价或使用量原因。"
    return {
        "id": f"recommendation:{account_id}:{action}:{finding['id']}",
        "action": action,
        "title": f"{text}：{label}",
        "reason": reason,
        "impact_amount": _money(impact_amount),
        "estimated_savings": None,
        "evidence_sufficient": sufficient,
        "risk": "read_only",
        "object": obj,
        "time_range": finding["time_range"],
        "history_range": finding.get("history_range", {"from": "", "to": ""}),
        "evidence_refs": finding["evidence_refs"],
        "next_action": {"type": action, "account_id": account_id,
                        "object": obj, "time_range": finding["time_range"],
                        "evidence_refs": finding["evidence_refs"]},
    }


def _analysis(db: Session, account: CloudAccount, start: date, end: date,
              facts: list[dict], evidence: dict[str, dict],
              history_start: date) -> tuple[list[dict], list[dict], list[dict]]:
    """按账号原始按量账单复算；不写 CostAnomaly，不沿用全局异常行。"""
    all_days = _dates(history_start, end)
    rows_by_day = {day: [] for day in all_days}
    for fact in facts:
        rows_by_day[date.fromisoformat(fact["date"])].extend(fact["_rows"])
    # facts 包含历史窗口和请求窗口；bill 完成证据同样按原日判定。
    complete_by_day = {}
    for day in all_days:
        rows = rows_by_day[day]
        cap = evidence.get(day.isoformat())
        expected_ref = f"cost_daily:{account.id}:{day.isoformat()}"
        complete_by_day[day] = any(x.source in _BILL_SOURCES for x in rows) or bool(
            cap and cap.get("execution") in ("succeeded", "skipped")
            and cap.get("checked_at") and expected_ref in (cap.get("evidence_refs") or [])
        )

    payg: dict[str, dict[date, float]] = {}
    samples: dict[str, CostDaily] = {}
    usage: dict[str, dict[date, float]] = {}
    for day in all_days:
        for row in rows_by_day[day]:
            if _kind(row) != "payg":
                continue
            key = _object_id(row)
            payg.setdefault(key, {})[day] = payg.setdefault(key, {}).get(day, 0) + float(row.pretax_amount or 0)
            samples[key] = row
            if float(row.usage or 0) != 0:
                usage.setdefault(key, {})[day] = usage.setdefault(key, {}).get(day, 0) + float(row.usage)

    anomalies, observations, candidates = [], [], []
    for key, values in payg.items():
        sample = samples[key]
        for day in _dates(start, end):
            amount = values.get(day, 0.0)
            if not complete_by_day.get(day) or amount < 0.1:
                continue
            prior_complete = [d for d in all_days
                              if day - timedelta(days=settings.BASELINE_WINDOW_DAYS) <= d < day
                              and complete_by_day.get(d)]
            series = [(d, values.get(d, 0.0)) for d in prior_complete]
            contiguous_days = []
            cursor = day - timedelta(days=1)
            while complete_by_day.get(cursor):
                contiguous_days.append(cursor)
                cursor -= timedelta(days=1)
            contiguous = len(contiguous_days)
            history_verified = contiguous >= _HISTORY_PROOF_DAYS
            appeared_before = any(abs(value) > 1e-9 for _, value in series)
            current_usage = usage.get(key, {}).get(day)
            finding = {
                "id": f"{day.isoformat()}:{key}",
                "account_id": account.id,
                "date": day.isoformat(),
                "object": _object(sample),
                "value": _money(amount),
                "usage": _money(current_usage),
                "time_range": {"from": day.isoformat(), "to": day.isoformat()},
                "source": "cost_daily",
                "evidence_refs": [f"cost_daily:{account.id}:{day.isoformat()}"],
            }
            if not appeared_before:
                history_days = sorted(contiguous_days)
                observations.append({
                    **finding, "kind": "first_observed",
                    "history_verified": history_verified,
                    "history_days": contiguous,
                    "history_range": {
                        "from": history_days[0].isoformat() if history_days else "",
                        "to": history_days[-1].isoformat() if history_days else "",
                    },
                    "evidence_refs": finding["evidence_refs"] + [
                        f"cost_daily:{account.id}:{d.isoformat()}" for d in history_days
                    ],
                    "next_action": "verify_first_observed_cost",
                })
                continue
            check = day_series_check(
                series, day, amount, lambda d: day_kind(db, d),
                unit=" 元", abs_min=settings.BASELINE_MIN_COST_YUAN,
            )
            # 冷启动 observe 不套用旧代码的阈值兜底；零基线由引擎保持 normal。
            if check["verdict"] != V_SPIKE:
                continue
            baseline_days = [d for d, _ in series]
            anomalies.append({
                **finding, "kind": "payg_spike", "baseline": check["baseline"],
                "mad": check["mad"], "ratio": check["ratio"], "delta": check["delta"],
                "level": check["level"] or "warning", "n_samples": check["n_samples"],
                "history_range": {
                    "from": min(baseline_days).isoformat(),
                    "to": max(baseline_days).isoformat(),
                },
                "evidence_refs": finding["evidence_refs"] + [
                    f"cost_daily:{account.id}:{d.isoformat()}" for d in baseline_days
                ],
                "notes": check["notes"], "next_action": "investigate_cost_growth",
            })

        requested_values = [(d, values.get(d, 0.0)) for d in _dates(start, end)
                            if complete_by_day.get(d)]
        positive = [(d, value) for d, value in requested_values if value > 0]
        daily_median = median([value for _, value in positive]) if positive else 0
        if len(positive) >= 3 and daily_median >= settings.COST_ANOMALY_MIN:
            total = sum(value for _, value in positive)
            candidates.append({
                "id": f"sustained:{key}", "account_id": account.id,
                "kind": "sustained_payg", "object": _object(sample),
                "value": _money(total), "daily_median": _money(daily_median),
                "positive_days": len(positive), "verified_days": len(requested_values),
                "usage": _money(sum(usage.get(key, {}).get(d, 0) for d, _ in positive))
                         if any(d in usage.get(key, {}) for d, _ in positive) else None,
                "time_range": {"from": positive[0][0].isoformat(), "to": positive[-1][0].isoformat()},
                "source": "cost_daily",
                "evidence_refs": [f"cost_daily:{account.id}:{d.isoformat()}" for d, _ in positive],
                "next_action": "evaluate_sustained_payg",
            })

    anomalies.sort(key=lambda x: (-(x["delta"] or 0), x["id"]))
    observations.sort(key=lambda x: (-(x["value"] or 0), x["id"]))
    candidates.sort(key=lambda x: (-(x["value"] or 0), x["id"]))
    recommendations = []
    seen_objects = set()
    candidate_keys = {x["object"]["id"] for x in candidates}
    for finding in anomalies:
        rec = _recommendation(account.id, "investigate_cost_growth", finding,
                              finding["delta"], finding["n_samples"] >= settings.BASELINE_MIN_SAMPLES)
        recommendations.append(rec)
        seen_objects.add(finding["object"]["id"])
    for finding in observations:
        if finding["object"]["id"] in seen_objects or finding["object"]["id"] in candidate_keys:
            continue
        rec = _recommendation(account.id, "verify_first_observed_cost", finding,
                              finding["value"], finding["history_verified"])
        recommendations.append(rec)
        seen_objects.add(finding["object"]["id"])
    for finding in candidates:
        if finding["object"]["id"] in seen_objects:
            continue
        rec = _recommendation(account.id, "evaluate_sustained_payg", finding,
                              finding["value"], finding["verified_days"] == (end - start).days + 1)
        recommendations.append(rec)
        seen_objects.add(finding["object"]["id"])
    recommendations.sort(key=lambda x: (
        not x["evidence_sufficient"],
        -(x["impact_amount"] if x["impact_amount"] is not None else float("-inf")),
        0 if x["risk"] == "read_only" else 1,
        x["id"],
    ))
    return anomalies, observations, recommendations[:_MAX_RECOMMENDATIONS]


def _build_account(db: Session, account: CloudAccount, rows: list[CostDaily],
                   start: date, end: date, history_start: date) -> dict:
    evidence = _daily_evidence(db, account.id)
    by_day: dict[date, list[CostDaily]] = {}
    for row in rows:
        by_day.setdefault(row.stat_date, []).append(row)
    facts = [
        _day_fact(day, by_day.get(day, []), evidence.get(day.isoformat()), account.id, account.vendor)
        for day in _dates(history_start, end)
    ]
    requested = [x for x in facts if start.isoformat() <= x["date"] <= end.isoformat()]
    missing_bill = [x["date"] for x in requested if not x["bill_complete"]]
    missing_amortized = [x["date"] for x in requested
                         if x["bill_complete"] and not x["amortized_complete"]]
    complete = not missing_bill and not missing_amortized

    def total_of(key):
        values = [x[key] for x in requested if x[key] is not None]
        return _money(sum(values)) if values else None

    has_data = any(x["has_data"] for x in requested)
    status = "no_data" if not has_data else ("available" if complete else "partial")
    reason = ("no_billing_data" if not has_data else
              "missing_billing_days" if missing_bill else
              "amortized_incomplete" if missing_amortized else "ready")
    observed = [x["date"] for x in requested if x["bill_complete"]]
    requested_rows = [row for x in requested for row in x["_rows"]]
    anomalies, observations, recommendations = _analysis(
        db, account, start, end, facts, evidence, history_start)
    return {
        "account": {"id": account.id, "name": account.name, "vendor": account.vendor,
                    "enabled": account.enabled, "billing_applicable": True},
        "range": {"start": start.isoformat(), "end": end.isoformat(),
                  "days": (end - start).days + 1},
        "status": status,
        "reason_code": reason,
        "coverage": {
            "requested_days": len(requested),
            "bill_complete_days": sum(x["bill_complete"] for x in requested),
            "amortized_complete_days": sum(x["amortized_complete"] for x in requested),
            "complete_days": sum(x["complete"] for x in requested),
            "observed_start": min(observed) if observed else "",
            "observed_end": max(observed) if observed else "",
            "missing_bill_dates": missing_bill,
            "missing_amortized_dates": missing_amortized,
            "complete": complete,
            "total_known": complete,
            "collected_at": max((x["collected_at"] for x in requested if x["collected_at"]), default=""),
        },
        "totals": {
            "payg": total_of("payg"), "amortized": total_of("amortized"),
            "total": total_of("total"), "order": total_of("order"),
            "refund": total_of("refund"), "complete": complete,
        },
        "days": [{k: v for k, v in x.items() if not k.startswith("_")} for x in requested],
        "products": _rank(requested_rows, instances=False, account_id=account.id),
        "instances": _rank([x for x in requested_rows if x.instance_id],
                           instances=True, account_id=account.id),
        "anomalies": anomalies,
        "observations": observations,
        "recommendations": recommendations,
        "analysis": {
            "anomaly_status": ("no_anomaly_in_verified_scope" if complete and not anomalies
                               else "anomalies_found" if anomalies else "not_fully_verified"),
            "recommendation_status": ("available" if recommendations else "insufficient_optimization_evidence"),
        },
    }


def get_cost_insights(db: Session, account_id: int, start: date, end: date) -> dict:
    """返回同账号、同窗口唯一成本事实；所有金额均来自本地 CostDaily。"""
    if start > end:
        raise ValueError("start 不能晚于 end")
    if (end - start).days + 1 > 366:
        raise ValueError("单次成本洞察范围不能超过 366 天")
    from app.services.cloud.provider import vendors_with

    billing_vendors = vendors_with("billing")
    all_accounts = db.query(CloudAccount).order_by(CloudAccount.id).all()
    known_ids = {x.id for x in all_accounts}
    if account_id:
        selected = next((x for x in all_accounts if x.id == account_id), None)
        if selected is None:
            raise LookupError("云账号不存在")
        accounts = [selected]
    else:
        accounts = [x for x in all_accounts if x.vendor in billing_vendors]
    history_start = start - timedelta(days=settings.BASELINE_WINDOW_DAYS)
    query = db.query(CostDaily).filter(CostDaily.stat_date >= history_start,
                                      CostDaily.stat_date <= end)
    if account_id:
        query = query.filter(CostDaily.account_id == account_id)
    rows = query.all()
    output, unattributed = [], []
    selected_ids = {x.id for x in accounts}
    for account in accounts:
        own_rows = [x for x in rows if x.account_id == account.id]
        if account.vendor not in billing_vendors:
            output.append({
                "account": {"id": account.id, "name": account.name, "vendor": account.vendor,
                            "enabled": account.enabled, "billing_applicable": False},
                "range": {"start": start.isoformat(), "end": end.isoformat(),
                          "days": (end - start).days + 1},
                "status": "not_applicable", "reason_code": "billing_not_applicable",
                "coverage": {"requested_days": (end - start).days + 1, "complete": False,
                             "total_known": False, "missing_bill_dates": [],
                             "missing_amortized_dates": []},
                "totals": {"payg": None, "amortized": None, "total": None,
                           "order": None, "refund": None, "complete": False},
                "days": [], "products": [], "instances": [], "anomalies": [],
                "observations": [], "recommendations": [],
                "analysis": {"anomaly_status": "not_applicable",
                             "recommendation_status": "insufficient_optimization_evidence"},
            })
        else:
            output.append(_build_account(db, account, own_rows, start, end, history_start))
    if not account_id:
        for raw_id in sorted({x.account_id or 0 for x in rows if (x.account_id or 0) not in known_ids}):
            placeholder = CloudAccount(id=raw_id, name="", vendor="aliyun", enabled=False)
            item = _build_account(db, placeholder,
                                  [x for x in rows if (x.account_id or 0) == raw_id],
                                  start, end, history_start)
            item["account"].update({"name": "", "vendor": "", "unattributed": True,
                                    "billing_applicable": False})
            unattributed.append(item)
    return {
        "requested": {"account_id": account_id, "start": start.isoformat(), "end": end.isoformat()},
        "accounts": output,
        "unattributed": unattributed,
        "selected_account_ids": sorted(selected_ids),
    }
