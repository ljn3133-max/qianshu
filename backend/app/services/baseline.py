"""周期基线异常检测引擎。

识别不符合周期规律的突变，而非只按固定阈值告警。不能只对比星期几：
调休上班的周日与普通周日直接比较会误报。

三个机制：
1. **同日型基线**：今天按节假日日历判作息（工作日/休息日/法定假日），只跟
   同日型的历史日子比；日型样本够多再细分到同星期几。样本不足 2 个 -> 只观察。
2. **双门槛**：超基线中位数 ×倍数 且 超绝对增量 才报（休息日 10→20 元翻倍不报）。
3. **数据自校验**：日历说休息但实测呈工作日节奏（调休没维护/业务不跟官方日历）
   的样本自动改判归类；今天日历与实测节奏矛盾且按另一日型复核属正常 -> 降 info
   并说明，不硬报 critical。

增长型指标（磁盘/空间）单独建模：水平单调涨没意义，比"每天涨多少"（增速）。

统计口径：中位数 + MAD（均值/标准差会被节日样本拉爆）；全部输出带数字的
可解释文案（"今天 300，工作日基线 95 ±12，近 8 周 6 个同类样本"）。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from statistics import median
from typing import Callable, Optional

from app.core.config import settings
from app.services.holiday import KIND_HOLIDAY, KIND_REST, KIND_WORKDAY, kind_label

logger = logging.getLogger("qianshu.baseline")

# 判定结论
V_SPIKE = "spike"       # 突增
V_DROP = "drop"         # 该忙反低（仅工作日）
V_NORMAL = "normal"
V_OBSERVE = "observe"   # 样本不足/冷启动，只记录不告警


def _mad(vals: list) -> float:
    if not vals:
        return 0.0
    m = median(vals)
    return median([abs(v - m) for v in vals])


def _fmt(v: float) -> str:
    return f"{v:,.2f}".rstrip("0").rstrip(".") if v < 1000 else f"{v:,.0f}"


def day_series_check(
    series: list,                # [(date, value)]，不含今天
    today: date,
    today_value: float,
    kind_fn: Callable[[date], str],
    *,
    unit: str = "",
    abs_min: float = 0.0,        # 双门槛的绝对增量下限（与 value 同单位）
    ratio_warn: Optional[float] = None,
    ratio_crit: Optional[float] = None,
    drop_ratio: Optional[float] = None,
) -> dict:
    """水平型指标（成本/流量/日志量）的周期基线判定。

    返回 {verdict, level, baseline, mad, ratio, n_samples, day_kind, notes: [..]}。
    """
    ratio_warn = ratio_warn if ratio_warn is not None else settings.BASELINE_RATIO_WARN
    ratio_crit = ratio_crit if ratio_crit is not None else settings.BASELINE_RATIO_CRIT
    drop_ratio = drop_ratio if drop_ratio is not None else settings.BASELINE_DROP_RATIO
    window_start = today - timedelta(days=settings.BASELINE_WINDOW_DAYS)

    today_kind = kind_fn(today)
    notes: list = [f"按作息判定：{kind_label(today_kind)}"]

    samples = [(d, v) for d, v in series if window_start <= d < today]
    if not samples:
        return {"verdict": V_OBSERVE, "level": "", "baseline": 0, "mad": 0,
                "ratio": 0, "n_samples": 0, "day_kind": today_kind,
                "value": round(today_value, 2), "delta": 0,
                "notes": notes + ["窗口内无历史样本（冷启动），只记录不告警"]}

    # ---- 数据自校验：日历日型与实测节奏不符的样本改判归类 ----
    by_cal: dict = {KIND_WORKDAY: [], KIND_REST: []}
    for d, v in samples:
        k = kind_fn(d)
        by_cal[KIND_WORKDAY if k == KIND_WORKDAY else KIND_REST].append(v)
    w_med = median(by_cal[KIND_WORKDAY]) if by_cal[KIND_WORKDAY] else 0.0
    r_med = median(by_cal[KIND_REST]) if by_cal[KIND_REST] else 0.0

    retagged: list = []
    if w_med > r_med:  # 常规业务：工作日高。反向业务（周末高峰）不改判
        def eff_kind(d: date, v: float) -> str:
            k = kind_fn(d)
            base = KIND_WORKDAY if k == KIND_WORKDAY else KIND_REST
            if base == KIND_REST and v >= 0.7 * w_med and v > 1.2 * r_med:
                retagged.append(f"{d.isoformat()}（{kind_label(k)}）实测呈工作日节奏，按工作日归类")
                return KIND_WORKDAY
            if base == KIND_WORKDAY and v <= 0.7 * r_med and v < 0.5 * w_med:
                retagged.append(f"{d.isoformat()}（{kind_label(k)}）实测呈休息日节奏，按休息日归类")
                return KIND_REST
            return base
    else:
        def eff_kind(d: date, v: float) -> str:
            k = kind_fn(d)
            return KIND_WORKDAY if k == KIND_WORKDAY else KIND_REST

    def group_of(d: date, v: float) -> str:
        k = eff_kind(d, v)
        # 法定假日样本够多才单列，否则并入休息日大盘
        if kind_fn(d) == KIND_HOLIDAY:
            n_holi = sum(1 for dd, vv in samples if kind_fn(dd) == KIND_HOLIDAY)
            return KIND_HOLIDAY if n_holi >= 4 else k
        return k

    want = today_kind if today_kind != KIND_HOLIDAY else (
        KIND_HOLIDAY if sum(1 for dd, _ in samples if kind_fn(dd) == KIND_HOLIDAY) >= 4 else KIND_REST)
    candidates = [(d, v) for d, v in samples if group_of(d, v) == want]
    same_wd = [(d, v) for d, v in candidates if d.weekday() == today.weekday()]
    if len(same_wd) >= 4:
        candidates = same_wd
        notes.append(f"样本取「{kind_label(want)}且星期{'一二三四五六日'[today.weekday()]}」{len(candidates)} 个")

    if retagged:
        notes.append("数据自校验：" + "；".join(retagged[:3]) + ("等" if len(retagged) > 3 else ""))

    if len(candidates) < settings.BASELINE_MIN_SAMPLES:
        all_vals = [v for _, v in samples]
        base_all = median(all_vals)
        return {"verdict": V_OBSERVE, "level": "", "baseline": round(base_all, 2),
                "mad": round(_mad(all_vals), 2), "ratio": round(today_value / base_all, 2) if base_all else 0,
                "n_samples": len(candidates), "day_kind": today_kind,
                "value": round(today_value, 2), "delta": round(today_value - base_all, 2),
                "notes": notes + [f"同类日样本仅 {len(candidates)} 个（<{settings.BASELINE_MIN_SAMPLES}），冷启动只观察不告警"]}

    vals = [v for _, v in candidates]
    base = median(vals)
    mad = _mad(vals)
    ratio = today_value / base if base > 0 else 0.0
    delta = today_value - base
    notes.append(
        f"今天 {_fmt(today_value)}{unit}，{kind_label(want)}基线中位数 {_fmt(base)}{unit}"
        f"（MAD ±{_fmt(mad)}，近 {settings.BASELINE_WINDOW_DAYS // 7} 周同类日 {len(vals)} 个样本）")

    # ---- 今天日历日型 vs 实测节奏矛盾 ----
    conflict = ""
    if w_med > r_med:
        if today_kind != KIND_WORKDAY and today_value >= 0.7 * w_med:
            conflict = "休息日/假日但实测呈工作日节奏（像调休上班或业务不跟官方日历）"
        elif today_kind == KIND_WORKDAY and today_value <= 0.7 * r_med:
            conflict = "工作日但实测呈休息日节奏（像临时放假/业务停摆）"

    def verdict_against(b: float) -> tuple:
        if b <= 0:
            return V_NORMAL, "", 0.0
        r = today_value / b
        dlt = today_value - b
        if r >= ratio_crit and dlt >= abs_min:
            return V_SPIKE, "critical", r
        if r >= ratio_warn and dlt >= abs_min:
            return V_SPIKE, "warning", r
        if today_kind == KIND_WORKDAY and r <= drop_ratio and -dlt >= abs_min:
            return V_DROP, "warning", r
        return V_NORMAL, "", r

    verdict, level, ratio = verdict_against(base)

    # 矛盾且按另一日型复核属正常 -> 降 info（不硬报）；复核仍异常 -> 保留并注明
    if conflict and verdict in (V_SPIKE, V_DROP):
        other = KIND_REST if want == KIND_WORKDAY else KIND_WORKDAY
        other_vals = [v for d, v in samples if group_of(d, v) == other]
        if len(other_vals) >= 2:
            o_base = median(other_vals)
            v2, _, _ = verdict_against(o_base)
            if v2 == V_NORMAL:
                notes.append(f"注意：{conflict}；按{kind_label(other)}基线（{_fmt(o_base)}{unit}）复核属正常，"
                             f"本次降为提示不告警")
                verdict, level = V_SPIKE if verdict == V_SPIKE else V_DROP, "info"
            else:
                notes.append(f"注意：{conflict}；按{kind_label(other)}基线复核仍属异常，维持判定")
        else:
            notes.append(f"注意：{conflict}（另一日型样本不足，无法复核）")

    return {"verdict": verdict, "level": level, "baseline": round(base, 2),
            "mad": round(mad, 2), "ratio": round(ratio, 2), "n_samples": len(vals),
            "day_kind": today_kind, "notes": notes,
            "value": round(today_value, 2), "delta": round(delta, 2)}


def short_mutation_check(
    event_samples: list,        # [{ts, members:[{name, share_pct}], total}]，时间升序
    *,
    window_minutes: int = 60,
    is_system: Optional[Callable[[str], bool]] = None,
) -> dict:
    """短时构成突变：1 小时内 top1 换人即报。

    与日构成漂移的分工（检测分层）：
    - 日构成漂移走既有路径（learn_baseline + detect_drift，逐日构成 vs 正常画像）；
    - 事件样本是**分钟级**样本（告警/异动/变更发生时记一笔），看的是短窗口——
      日构成基线看不到"几分钟内 top1 换人"这类瞬时构成突变，本函数补这个盲区。

    判定规则（口径注释，逐条可复算）：
    1) 只看事件样本；窗口内**有效构成样本 <2 份**不判（宁缺勿报）；
    2) 有效构成 = 过滤系统成员（内核线程/安全组件，is_system 口径与日构成判定一致）
       后仍有成员的样本；
    3) 窗口 = [最新样本时刻 - window_minutes, 最新样本时刻]；取窗口内**首/末**
       两份有效样本的 top1（占比最高的非系统成员），两者不同 -> 短时构成突变；
    4) top1 相同不判（占比涨落属日构成/占比漂移的活，短时只抓"换人"）；
    5) 返回带时间与前后 top1 的可解释文案。

    返回 {hit, level, notes, from_top1, to_top1, ts_from, ts_to, n_in_window}。
    """
    def _top1(sample: dict) -> str:
        members = sample.get("members") or []
        for m in sorted(members, key=lambda x: -(x.get("share_pct") or 0)):
            name = str(m.get("name") or "")
            if is_system is not None and is_system(name):
                continue
            if name:
                return name
        return ""

    valid = []
    for s in event_samples or []:
        try:
            ts = datetime.fromisoformat(str(s.get("ts")))
        except (TypeError, ValueError):
            continue
        if _top1(s):
            valid.append((ts, _top1(s), s))
    if len(valid) < 2:
        return {"hit": False, "level": "", "notes": [], "n_in_window": len(valid)}
    t_last = valid[-1][0]
    in_win = [(t, n) for t, n, _ in valid if t >= t_last - timedelta(minutes=window_minutes)]
    if len(in_win) < 2:
        return {"hit": False, "level": "", "notes": [], "n_in_window": len(in_win)}
    t0, n0 = in_win[0]
    t1, n1 = in_win[-1]
    if n0 == n1:
        return {"hit": False, "level": "", "notes": [], "n_in_window": len(in_win)}
    mins = max(1, int((t1 - t0).total_seconds() // 60))
    return {
        "hit": True,
        "level": "warning",   # 即报但不标 critical（critical 会触发事件自动重开，短时突变先给 warning 由人看）
        "notes": [
            f"短时构成突变：{t0:%m-%d %H:%M}→{t1:%m-%d %H:%M}（{mins} 分钟内）top1 换人："
            f"{n0} → {n1}（窗口 {window_minutes} 分钟内首末事件样本对照）",
            "短时换人=分钟级构成突变（发版/进程接管/刷量起点），比日构成漂移更急，优先看事发时刻的变更",
        ],
        "from_top1": n0, "to_top1": n1,
        "ts_from": t0.isoformat(), "ts_to": t1.isoformat(),
        "n_in_window": len(in_win),
    }


def growth_check(
    series: list,        # [(date, value)]，含或不含今天均可（按日期识别）
    unit: str = "%",
    min_delta: Optional[float] = None,
) -> dict:
    """增长型指标（磁盘/空间使用量）：比"每天涨多少"，不比水平。

    日增量 g_t = v_t - v_{t-1}；基线 = 近 N 天日增量中位数；
    异常 = 今日增量 > 基线×GROWTH_RATIO 且 >= 绝对下限。
    """
    min_delta = min_delta if min_delta is not None else settings.BASELINE_GROWTH_MIN_DELTA
    series = sorted(series)
    if len(series) < 3:
        return {"verdict": V_OBSERVE, "level": "", "notes": ["历史点不足 3 天，增速无法建模"],
                "growth": 0, "baseline_growth": 0, "n_samples": 0}
    deltas: list = []   # [(date, g)]
    for i in range(1, len(series)):
        deltas.append((series[i][0], series[i][1] - series[i - 1][1]))
    today, today_g = deltas[-1]
    window = [g for d, g in deltas[:-1] if d >= today - timedelta(days=settings.BASELINE_GROWTH_WINDOW)]
    if len(window) < 2:
        return {"verdict": V_OBSERVE, "level": "", "notes": [f"近 {settings.BASELINE_GROWTH_WINDOW} 天日增量样本不足"],
                "growth": round(today_g, 2), "baseline_growth": 0, "n_samples": len(window)}
    base_g = median(window)
    ratio = today_g / base_g if base_g > 0 else 0.0
    notes = [
        f"今日增长 {_fmt(today_g)}{unit}/天，近 {settings.BASELINE_GROWTH_WINDOW} 天日常增速 "
        f"{_fmt(base_g)}{unit}/天（{len(window)} 个样本）",
    ]
    verdict, level = V_NORMAL, ""
    if today_g >= min_delta and (today_g >= settings.BASELINE_GROWTH_RATIO * max(base_g, 0.01)
                                 or (base_g <= 0.01 and today_g >= min_delta)):
        verdict, level = V_SPIKE, ("critical" if today_g >= 2 * min_delta else "warning")
        notes.append(f"增速突变：{today_g / base_g:.1f} 倍于日常" if base_g > 0.01
                     else f"增速突变：日常近零增长，今日 +{_fmt(today_g)}{unit}")
        # 预计多久到顶（按最近速度）
        last_v = series[-1][1]
        speed = max(today_g, base_g)
        if speed > 0:
            notes.append(f"按此速度将继续快速上涨（近 24h 峰值 {_fmt(last_v)}{unit}，当前实测见上）")
    return {"verdict": verdict, "level": level, "growth": round(today_g, 2),
            "baseline_growth": round(base_g, 2), "ratio": round(ratio, 2),
            "n_samples": len(window), "notes": notes, "value": round(series[-1][1], 2)}
