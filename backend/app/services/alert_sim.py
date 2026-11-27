"""告警规则试算：保存前用历史数据回放规则——
"会触发几次 / 什么时间 / 样本值多少"，帮用户调阈值不盲拍。

口径（红线）：判据与线上评估同源——比较条件、持续窗口取点、多序列合并、恢复迟滞、
再触发抑制、静默时段全部复用 alert_eval 的公共函数（sustained_breach / series_window /
merge_series_breach / series_target / sls_rule_query / _compare / _first_float /
_recovered / _in_silence），本模块不另写判据（防口径漂移）。

只读历史数据零写库：只查 VictoriaMetrics / SLS，不建告警记录、不发通知、不写任何表。

回放口径说明：
- metric：全序列逐评估点回放（步长=采集粒度，与线上评估周期一致），触发=进入越限
  状态的次数（含再触发抑制期的触发，只是标注 suppressed 不代表不计数——与线上
  "记录会建、通知被压"一致）；恢复带迟滞（阈值下 ALERT_HYSTERESIS 才收口）。
- sls：SLS 历史检索按抽样窗口回放（每窗一次检索，窗口数封顶 SLS_MAX_EVALS），
  触发次数为下界，samples_summary.note 里明示抽样粒度。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy.orm import Session

from app.collector.credential import resolve_sls_credential
from app.collector.sls import query_logs as sls_query_logs
from app.collector.vm import metric_name as vm_metric_name
from app.collector.vm import query_range as vm_query_range
from app.core.config import settings
from app.models.asset import Asset
from app.services.alert_eval import (
    _compare,
    _first_float,
    _in_silence,
    _recovered,
    merge_series_breach,
    series_target,
    series_window,
    sls_rule_query,
    sustained_breach,
)

logger = logging.getLogger("qianshu.alert_sim")

MAX_DAYS = 30
SLS_MAX_EVALS = 24   # SLS 回放抽样窗口上限（每窗一次检索，控制耗时）


def _rule_ns(draft: dict) -> SimpleNamespace:
    """草稿字段 → 与 AlertRule 同名属性的轻量对象（不建 ORM 实例，零写库）。
    _recovered / _fmt_rule_def 等公共判据按属性名鸭子类型取值。"""
    return SimpleNamespace(
        name=draft.get("name") or "(试算)",
        source=draft.get("source") or "metric",
        level=draft.get("level") or "warning",
        product=draft.get("product"),
        metric=draft.get("metric"),
        project=draft.get("project"),
        logstore=draft.get("logstore"),
        query_expr=draft.get("query_expr"),
        comparator=draft.get("comparator") or ">",
        threshold=float(draft.get("threshold")),
        duration=int(draft.get("duration") or 300),
        silences=json.dumps([s.strip() for s in (draft.get("silences") or []) if s.strip()]),
        account_id=int(draft.get("account_id") or 0),   # 试算沿用线上账号过滤口径
    )


def _fire_event(rule, target: str, target_name: str, ts: int, value: float,
                window_silence: bool, resuppressed: bool) -> dict:
    reason = "window" if window_silence else ("resuppress" if resuppressed else "")
    return {
        "ts": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
        "ts_epoch": ts,
        "value": round(float(value), 4),
        "threshold": rule.threshold,
        "target": target,
        "target_name": target_name or target,
        "silenced": bool(window_silence) or resuppressed,
        "silence_reason": reason,
    }


def simulate(db: Session, draft: dict, days: int = 7) -> dict:
    """规则试算主入口：近 days 天历史回放。只读，不写库。"""
    days = max(1, min(int(days or 7), MAX_DAYS))
    rule = _rule_ns(draft)
    if rule.source == "metric":
        return _simulate_metric(db, rule, days)
    return _simulate_sls(db, rule, days)


# ---------------- metric：本地采集库回放 ----------------


def _simulate_metric(db: Session, rule, days: int) -> dict:
    name = vm_metric_name(rule.product, rule.metric)
    end_s = int(time.time())
    start_s = end_s - days * 86400
    step = 60 if days <= 7 else 300   # 7 天内按线上评估周期（60s）逐点回放
    result = vm_query_range(name, start_s, end_s, step)

    series_list = []          # (target, part, points 升序)
    n_points = 0
    v_min = v_max = v_sum = None
    breach_points = 0
    for series in result:
        labels = series.get("metric", {})
        target, part = series_target(labels)
        if not target:
            continue
        points = sorted(
            (int(float(ts)), float(v))
            for ts, v in series.get("values", []) if float(v) == float(v)
        )
        if not points:
            continue
        series_list.append((target, part, points))
        for _ts, v in points:
            n_points += 1
            v_sum = (v if v_sum is None else v_sum + v)
            v_min = v if v_min is None else min(v_min, v)
            v_max = v if v_max is None else max(v_max, v)
            if _compare(rule.comparator, v, rule.threshold):
                breach_points += 1

    # 账号维度与线上评估同口径：规则绑定账号时只回放该账号目标，
    # 已知归属其他账号的剔除；对不上资产/未打标的保留（不猜归属不误杀）
    if getattr(rule, "account_id", 0) and db is not None:
        acct_of = {a.instance_id: (a.account_id or 0) for a in db.query(Asset).all()}
        series_list = [(t, p, pts) for (t, p, pts) in series_list
                       if acct_of.get(t, 0) in (0, rule.account_id)]

    base_summary = {
        "days": days, "source": "metric",
        "metric": f"{rule.product}.{rule.metric}",
        "window_seconds": rule.duration, "eval_stride_seconds": step,
        "condition": f"{rule.metric} {rule.comparator} {rule.threshold:g} 持续 {rule.duration}s",
    }
    if not series_list:
        return {
            "would_fire": [], "count": 0,
            "samples_summary": {**base_summary, "evals": 0, "series": 0, "targets": 0,
                                "data_points": 0,
                                "note": "近 %d 天无该指标历史数据，无法回放（先确认采集正常）" % days},
        }

    targets = {t for t, _p, _pts in series_list}
    name_map = {
        a.instance_id: (a.name or a.instance_id)
        for a in db.query(Asset).filter(Asset.product == rule.product).all()
    } if db is not None else {}

    # 评估时间线：首个完整窗口起，按线上评估周期（step）推进；窗口比历史跨度还长时只评一次
    eval_ts = list(range(start_s + rule.duration, end_s + 1, step)) or [end_s]

    firing = {t: False for t in targets}
    last_recovered_at: dict = {t: None for t in targets}
    events: list = []
    for t in eval_ts:
        merged: dict = {}
        for target, part, points in series_list:
            win = series_window(points, t, rule.duration)
            if not win:
                continue
            breached = sustained_breach(rule.comparator, rule.threshold, win)
            merged[target] = merge_series_breach(merged.get(target), breached, win[-1], part)
        for target, (breached, value, _part) in merged.items():
            if breached and not firing[target]:
                firing[target] = True
                window_silence = bool(_in_silence(rule.silences, now=datetime.fromtimestamp(t)))
                # 再触发抑制期：刚恢复不久又越限——记录仍会建（计入次数），通知被压
                resuppressed = bool(
                    last_recovered_at[target] is not None
                    and (t - last_recovered_at[target]) < settings.ALERT_RESUPPRESS_MINUTES * 60
                )
                events.append(_fire_event(rule, target, name_map.get(target, target), t,
                                          value, window_silence, resuppressed))
            elif (not breached) and firing[target] and _recovered(rule, value):
                firing[target] = False
                last_recovered_at[target] = t

    events.sort(key=lambda e: e["ts_epoch"])
    fired_targets = sorted({e["target"] for e in events})
    samples_summary = {
        **base_summary,
        "evals": len(eval_ts), "series": len(series_list), "targets": len(targets),
        "data_points": n_points,
        "value_min": round(v_min, 4), "value_max": round(v_max, 4),
        "value_avg": round(v_sum / n_points, 4) if n_points else None,
        "point_breach_ratio": round(breach_points / n_points, 4) if n_points else None,
        "targets_fired": fired_targets,
        "note": ("判据与线上评估同源（持续越限 + 恢复迟滞 + 再触发抑制回放）；"
                 "触发=进入越限状态的次数，含静默/抑制时段（线上同样会留记录、只是不推送）；"
                 "只读回放零写库"),
    }
    return {"would_fire": events, "count": len(events), "samples_summary": samples_summary}


# ---------------- sls：历史检索抽样回放 ----------------


def _simulate_sls(db: Session, rule, days: int) -> dict:
    ak, sk, _src = resolve_sls_credential(db)
    end_s = int(time.time())
    start_s = end_s - days * 86400
    window = max(rule.duration, 300)          # 与线上 _eval_sls_rule 同窗
    span = end_s - start_s
    n_evals = min(SLS_MAX_EVALS, max(1, span // window))
    stride = max(window, span // n_evals)     # 抽样步长 >= 窗口，不重叠
    query = sls_rule_query(rule.query_expr)   # 检索语句与线上同源
    target = f"{rule.project}/{rule.logstore}"

    eval_ts = [start_s + window + i * stride for i in range(n_evals)]
    eval_ts = [t for t in eval_ts if t <= end_s] or [end_s]

    firing = False
    last_recovered_at = None
    events: list = []
    values: list = []
    errors: list = []
    for t in eval_ts:
        try:
            result = sls_query_logs(
                ak, sk, rule.project, rule.logstore, query,
                start=t - window, end=t, limit=1, offset=0,
            )
        except Exception as e:
            errors.append(f"{datetime.fromtimestamp(t).strftime('%m-%d %H:%M')}: {str(e)[:80]}")
            continue
        value = _first_float(result["logs"])
        if value is None:
            continue
        values.append(float(value))
        breached = _compare(rule.comparator, value, rule.threshold)   # 与线上同源判据
        if breached and not firing:
            firing = True
            window_silence = bool(_in_silence(rule.silences, now=datetime.fromtimestamp(t)))
            resuppressed = bool(
                last_recovered_at is not None
                and (t - last_recovered_at) < settings.ALERT_RESUPPRESS_MINUTES * 60
            )
            events.append(_fire_event(rule, target, target, t, value, window_silence, resuppressed))
        elif (not breached) and firing and _recovered(rule, value):
            firing = False
            last_recovered_at = t

    events.sort(key=lambda e: e["ts_epoch"])
    samples_summary = {
        "days": days, "source": "sls",
        "metric": f"{rule.project}/{rule.logstore}",
        "window_seconds": window, "eval_stride_seconds": stride,
        "condition": f"{rule.query_expr or '*'} {rule.comparator} {rule.threshold:g}",
        "evals": len(eval_ts), "sampled_windows": len(values),
        "value_min": round(min(values), 4) if values else None,
        "value_max": round(max(values), 4) if values else None,
        "value_avg": round(sum(values) / len(values), 4) if values else None,
        "note": ("SLS 历史回放为抽样窗口（每个窗口一次检索，共 %d 窗、粒度 %ds），"
                 "触发次数为下界；判据与线上评估同源；只读回放零写库" % (len(eval_ts), stride)),
        "errors": errors[:5],
    }
    return {"would_fire": events, "count": len(events), "samples_summary": samples_summary}
