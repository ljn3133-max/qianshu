"""告警规则评估引擎。

周期（或手动）评估启用中的规则：
- source=metric：查 VictoriaMetrics（本地采集库），对产品下全部实例逐个判断，
  最近 duration 窗口内所有采样点都满足比较条件才算越限（"持续越限"语义）；
- source=sls：query（检索或含 | select 的 SQL）在 duration 窗口内取一个聚合值与阈值比较。

状态机：同一 (rule_id, target) 同时只有一条 firing 记录；越限创建并通知，
恢复带迟滞（回落到阈值下 10% 才算恢复，防贴线抖动）并默认只留痕不推送；
恢复后 60 分钟内再越限属抖动余波，只记录不推送。静默时段只压制通知，不中断评估/恢复。
通知：先落站内信，再推 rule.channel_ids 里启用的机器人渠道（失败不影响告警）。
"""
from __future__ import annotations

import bisect
import json
import logging
import threading
import time
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.collector.credential import resolve_sls_credential
from app.collector.sls import query_logs as sls_query_logs
from app.collector.vm import metric_name as vm_metric_name
from app.collector.vm import query_range as vm_query_range
from app.core.config import settings
from app.models.alert_channel import AlertChannel
from app.models.alert_record import AlertRecord
from app.models.alert_rule import AlertRule
from app.models.asset import Asset
from app.services.notify import NotifyError, send_to_channel

def _biz_impact(db, target, target_name, level):
    from app.services.biz_impact import assess
    return assess(db, target=target or "", target_name=target_name or "", level=level)


logger = logging.getLogger("qianshu.alerts")

COMPARATORS = {
    ">": lambda v, t: v > t,
    ">=": lambda v, t: v >= t,
    "<": lambda v, t: v < t,
    "<=": lambda v, t: v <= t,
}

_run_lock = threading.Lock()
_last_status: dict = {"last_run_at": None, "duration_ms": 0, "rules": 0, "fired": 0, "recovered": 0, "errors": []}


def get_status() -> dict:
    return dict(_last_status)


def _in_silence(silences_json: str, now: Optional[datetime] = None) -> Optional[str]:
    """当前是否处于静默时段。返回命中的时段串，未命中返回 None。支持跨零点（23:00-07:00）。"""
    now = now or datetime.now()
    try:
        silences = json.loads(silences_json or "[]")
    except ValueError:
        return None
    cur = now.hour * 60 + now.minute
    for item in silences:
        s = str(item)
        if "-" not in s:
            continue
        a, b = s.split("-", 1)
        try:
            ah, am = map(int, a.strip().split(":"))
            bh, bm = map(int, b.strip().split(":"))
        except ValueError:
            continue
        start, end = ah * 60 + am, bh * 60 + bm
        if start <= end:
            hit = start <= cur < end
        else:  # 跨零点
            hit = cur >= start or cur < end
        if hit:
            return s
    return None


def _compare(comparator: str, value: float, threshold: float) -> bool:
    fn = COMPARATORS.get(comparator)
    if fn is None:
        return False
    return fn(value, threshold)


# ---------------- 判据公共函数（线上评估与规则试算同源） ----------------
# 线上评估（run_evaluation）与规则试算（alert_sim，保存前历史回放）共用以下判据：
# 比较条件、持续窗口取点、多序列合并、SLS 检索语句。改动这里 = 同时改两边口径；
# 严禁在 alert_sim 另写一份判据（口径漂移红线）。以上函数对 run_evaluation 为纯抽取，
# 现有告警行为零变化。


def sustained_breach(comparator: str, threshold: float, values: list) -> bool:
    """持续越限判据（唯一判据源）：窗口内所有采样点都满足比较条件才算越限。

    空窗口不判越限（原 all(...) 语义在非空窗口上完全一致）。
    """
    return bool(values) and all(_compare(comparator, v, threshold) for v in values)


def series_window(points, end_ts: int, duration: int) -> list[float]:
    """取 end_ts 前 duration 窗口 [end_ts-duration, end_ts] 内的采样值（持续越限的取点口径）。

    points 为 [(ts, value)] 且 ts 非降序（VM range 查询输出即此序）。窗口内无点时
    回退最近一个点（ts <= end_ts），避免采集间隙漏报——与原 _eval_metric_rule 同语义。
    """
    cutoff = end_ts - duration
    # 二分定位（±inf 哨兵使比较退化为按 ts，等 ts 重复点不影响正确性）
    lo = bisect.bisect_left(points, (cutoff, float("-inf")))
    hi = bisect.bisect_right(points, (end_ts, float("inf")))
    recent = [v for _ts, v in points[lo:hi]]
    if not recent:
        recent = [points[hi - 1][1]] if hi > 0 else []
    return recent


def merge_series_breach(prev: Optional[list], breached: bool, value: float, part: str) -> list:
    """同目标多序列合并（磁盘分区/Redis 分片）：任一序列持续越限即越限。

    展示值取越限序列的最近值（保留其分区），无越限取较大值（保守展示）。
    返回 [breached, value, part]。
    """
    if prev is None:
        return [breached, value, part if breached else ""]
    merged = prev[0] or breached
    if prev[0]:
        v, detail = prev[1], prev[2]          # 保留越限序列的值与分区
    elif breached:
        v, detail = value, part               # 新越限序列
    elif value > prev[1]:
        v, detail = value, part               # 未越限取较大值（保守展示）
    else:
        v, detail = prev[1], prev[2]
    return [merged, v, detail]


def series_target(labels: dict) -> tuple:
    """序列归属（评估与试算同源）：instance_id/node → 目标；node → 分区标识
    （磁盘告警要说出是哪个路径）。返回 (target, part)，target 为空 = 跳过该序列。"""
    target = labels.get("instance_id") or labels.get("node") or ""
    part = labels.get("node") or ""
    return target, part


def sls_rule_query(query_expr: str) -> str:
    """SLS 规则检索语句（评估与试算同源）。

    - 用户 query 含 ``|``：按 SQL 分析执行原样返回（取首行首个数值列）；
    - 纯检索：包一层 ``| select count(*)`` 取精确命中总数（get_logs 的 count 只是
      本页返回条数，受 line 截断，直接用会让阈值>1 的规则永远无法触发）。
    """
    query = (query_expr or "").strip()
    if "|" in query:
        return query
    return f"{query or '*'} | select count(*) as cnt"


def _target_overrides(db: Session, rule: AlertRule) -> dict:
    """目标级水位覆盖：被"设为常态"的机器按自身水位判定。"""
    try:
        from app.models.alert_baseline import AlertTargetBaseline
        rows = db.query(AlertTargetBaseline).filter(
            AlertTargetBaseline.rule_id == rule.id).all()
        return {r.target: r for r in rows}
    except Exception:
        return {}


def _recovered(rule: AlertRule, value: float, threshold: float = None) -> bool:
    """恢复判定带迟滞：不仅"不越限"，还要回落出迟滞带。

    触发 90% 的规则，恢复须回落到 <81%（阈值下 10%），避免贴线资源每穿越一次
    阈值就成对触发/恢复推送。迟滞后同一资源只报一次、常驻 firing 直到真恢复。
    """
    th = (threshold if threshold is not None else rule.threshold) or 0.0
    margin = th * settings.ALERT_HYSTERESIS if th else 5.0
    if rule.comparator in (">", ">="):
        return value < th - margin
    return value > th + margin


# ---------------- 评估器 ----------------


def _eval_metric_rule(rule: AlertRule) -> dict:
    """返回 {target: (breached, value)}，target=instance_id。一次 VM 查询覆盖全部实例。

    同一实例可能有多条序列（Redis 集群分片 / ECS 磁盘分区，node 标签区分）：
    任一序列持续越限即算越限，value 取越限序列的最近值（无越限时取最近值最大的序列）。
    返回 {target: [breached, value, 分区名]}——磁盘告警必须说出是哪个路径
    （/dev/vda3 挂 /、C:\、E:\），避免"磁盘过高"却无法定位具体分区。
    """
    name = vm_metric_name(rule.product, rule.metric)
    end_s = int(time.time())
    lookback = max(rule.duration, 300) + 120  # 留出采集周期与步长对齐的余量
    start_s = end_s - lookback
    result = vm_query_range(name, start_s, end_s, 60)
    out: dict[str, list] = {}
    for series in result:
        labels = series.get("metric", {})
        # 分区/序列标识（磁盘要说出是哪个路径：/dev/vda3 挂 /、C:\、E:\）
        target, part = series_target(labels)
        if not target:
            continue
        points = [(int(float(ts)), float(v)) for ts, v in series.get("values", []) if float(v) == float(v)]
        if not points:
            continue
        recent = series_window(points, end_s, rule.duration)
        if not recent:
            continue  # 理论不可达（points 非空且查询窗含 end_s）；防 IndexError
        breached = sustained_breach(rule.comparator, rule.threshold, recent)
        out[target] = merge_series_breach(out.get(target), breached, recent[-1], part)
    return out


def _first_float(logs: list) -> Optional[float]:
    """从 SQL 结果里取首行首个可转 float 的值。"""
    for row in logs:
        for v in row.values():
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def _eval_sls_rule(db: Session, rule: AlertRule) -> dict:
    """返回 {target: (breached, value)}。

    检索语句口径见 sls_rule_query（含 | 走 SQL 取首行首个数值列；纯检索包一层
    count(*) 取精确命中总数）。
    """
    ak, sk, _src = resolve_sls_credential(db)
    end_s = int(time.time())
    start_s = end_s - max(rule.duration, 300)
    target = f"{rule.project}/{rule.logstore}"
    result = sls_query_logs(
        ak, sk, rule.project, rule.logstore, sls_rule_query(rule.query_expr),
        start=start_s, end=end_s, limit=1, offset=0,
    )
    value = _first_float(result["logs"])
    if value is None:
        return {}
    return {target: (_compare(rule.comparator, value, rule.threshold), value)}


# ---------------- SLB 5xx 后端归因（aux 机器策略） ----------------


def _log_error_attribution(db: Session, rule: AlertRule) -> str:
    """ERROR 日志告警归因：明确应用与错误类别，避免告警缺少可定位对象。"""
    if rule.source != "sls" or "error" not in (rule.query_expr or "").lower():
        return ""
    try:
        ak, sk, _ = resolve_sls_credential(db)
        end_s = int(time.time())
        start_s = end_s - max(rule.duration, 300)
        r = sls_query_logs(
            ak, sk, rule.project, rule.logstore,
            "* | select AppName, count(*) as cnt group by AppName order by cnt desc limit 5",
            start=start_s, end=end_s, limit=5)
        rows = r["logs"]
    except Exception as e:
        logger.warning("ERROR 归因查询失败: %s", e)
        return ""
    if not rows:
        return ""
    total = sum(float(x.get("cnt") or 0) for x in rows) or 1
    lines = [f"  - {x.get('AppName', '?')}: {float(x.get('cnt') or 0):.0f} 条"
             f"（{float(x.get('cnt') or 0) / total * 100:.0f}%）" for x in rows]
    top = rows[0]
    return ("\n- ERROR 按应用归因:\n" + "\n".join(lines)
            + f"\n- 重点：{top.get('AppName', '?')} 贡献了 {float(top.get('cnt') or 0) / total * 100:.0f}%"
              f"——去这个应用查错误日志内容定位代码问题")


def _slb_5xx_attribution(db: Session, rule: AlertRule) -> str:
    """5xx 规则触发时按后端 upstream 归因：aux 机器（如 Sentry）贡献大头时点名——
    "SLB 的 5xx 大部分是它导致的"正是 aux 机器需要被关注的第二种信号。"""
    if rule.source != "sls" or "500" not in (rule.query_expr or ""):
        return ""
    from app.models.link_edge import LinkEdge
    from app.services.tier import TIER_AUX
    try:
        ak, sk, _ = resolve_sls_credential(db)
        end_s = int(time.time())
        start_s = end_s - max(rule.duration, 300)
        r = sls_query_logs(
            ak, sk, rule.project, rule.logstore,
            f'{rule.query_expr} | select upstream_addr, count(*) as cnt '
            f'group by upstream_addr order by cnt desc limit 5',
            start=start_s, end=end_s, limit=5)
        rows = r["logs"]
    except Exception as e:
        logger.warning("5xx 归因查询失败: %s", e)
        return ""
    if not rows:
        return ""
    total = sum(float(x.get("cnt") or 0) for x in rows) or 1
    lines = []
    aux_hits = []
    up2ecs = {e.upstream_addr: (e.ecs_name or "") for e in db.query(LinkEdge).all()}
    for x in rows:
        up = str(x.get("upstream_addr", "?"))
        cnt = float(x.get("cnt") or 0)
        ecs = up2ecs.get(up, "")
        suffix = f"（{ecs}）" if ecs else ""
        lines.append(f"  - {up}{suffix}: {cnt:.0f} 条（{cnt / total * 100:.0f}%）")
        if ecs:
            from app.models.asset import Asset
            a = db.query(Asset).filter(Asset.name == ecs).first()
            if a is not None and getattr(a, "tier", "core") == TIER_AUX and cnt / total >= 0.5:
                aux_hits.append((ecs, cnt / total * 100))
    note = "\n- 5xx 后端归因:\n" + "\n".join(lines)
    for ecs, pct in aux_hits:
        note += (f"\n- 重点：5xx 的 {pct:.0f}% 来自辅助机器 {ecs}（它自身故障外溢到 SLB）——"
                 f"这正是该机器需要被关注的信号，优先查它")
    return note


# ---------------- 通知 ----------------


def _fmt_rule_def(rule: AlertRule) -> str:
    """规则的触发条件描述（落 alert_records.content，随站内信/推送/首页展示）。

    metric 规则用中文指标标签+单位（cms.ALERT_METRIC_CONFIG，含 vmware_*），
    别让人去猜 ecs.CPUUtilization 是什么；SLS 规则保持项目/日志库原文。
    """
    if rule.source == "metric":
        from app.collector.cms import ALERT_METRIC_CONFIG
        label, unit = rule.metric, ""
        for m, l, u in ALERT_METRIC_CONFIG.get(rule.product, []):
            if m == rule.metric:
                label, unit = l, u
                break
        cond = f"{label} {rule.comparator} {rule.threshold:g}"
        if unit:
            cond += unit if unit == "%" else f" {unit}"
        return f"{cond}，持续 {max(rule.duration // 60, 1)} 分钟"
    return f"SLS {rule.project}/{rule.logstore} `{rule.query_expr}` {rule.comparator} {rule.threshold:g}（{rule.duration}s）"


def _notify(db: Session, rule: AlertRule, record: AlertRecord, recovered: bool) -> list:
    """站内信（全部活跃用户）+ 渠道推送。返回成功的渠道名列表；单渠道失败不阻断。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if recovered:
        title = f"[已恢复] {rule.name} - {record.target_name or record.target}"
        content = (
            f"告警已自动恢复。\n\n"
            f"- 规则：{rule.name}\n"
            f"- 目标：{record.target_name or record.target}\n"
            f"- 条件：{_fmt_rule_def(rule)}\n"
            f"- 触发于：{record.fired_at.strftime('%m-%d %H:%M:%S')}，恢复于：{now}"
        )
        level = "info"
    else:
        title = f"[{rule.level.upper()}] {rule.name} - {record.target_name or record.target}"
        content = (
            f"规则越限触发告警。\n\n"
            f"- 规则：{rule.name}（{rule.level}）\n"
            f"- 目标：{record.target_name or record.target}\n"
            f"- 条件：{_fmt_rule_def(rule)}\n"
            f"- 当前值：{record.breach_value:g}\n"
            f"- 时间：{now}"
        )
        level = rule.level

    module = getattr(record, "module", None) or "alert"
    account_id = getattr(record, "account_id", 0) or 0
    from app.services.monitor_policy import can_push, priority_of
    from app.services.notify import notify_inapp

    # 低优先级账号的同一问题只通知一次——告警仍照常落库、
    # 仍在列表/总览可见，只是不再重复打扰；恢复通知不受此限。
    if (not recovered and priority_of(db, account_id) == "low"
            and (record.notify_count or 0) > 0):
        return []

    # 1) 站内信（唯一写出口；模块/账号关了站内信就整条不发）
    n = notify_inapp(db, title, content, level=level, msg_type="alert",
                     ref_id=record.id, audience="active", module=module,
                     account_id=account_id)

    # 2) 机器人渠道按级别分诊：企微只推 critical 及以上，
    # warning/info 进每日「今日决策清单」+ 站内信留档（碎片轰炸的根治）
    ok_channels: list[str] = []
    try:
        channel_ids = json.loads(rule.channel_ids or "[]")
    except ValueError:
        channel_ids = []
    pushable = level == "critical" if settings.NOTIFY_PUSH_MIN_LEVEL == "critical" \
        else level in ("critical", "warning")

    def _mark() -> None:
        if n or ok_channels:
            record.notify_count = (record.notify_count or 0) + 1
            record.last_notified_at = datetime.now()

    if not channel_ids or (not recovered and not pushable):
        _mark()
        return ok_channels
    if not can_push(db, module, account_id):
        _mark()
        return ok_channels
    if not recovered and level == "critical":
        from app.services.auto_diag import fire_auto_diag
        fire_auto_diag(title, content, record.target or record.target_name or rule.name)
    rows = db.query(AlertChannel).filter(AlertChannel.enabled == True).all()  # noqa: E712
    for ch in rows:
        if ch.id not in channel_ids:
            continue
        try:
            send_to_channel(ch, title, content)
            ok_channels.append(ch.name)
        except NotifyError as e:
            logger.warning("告警推送失败 渠道=%s: %s", ch.name, e)
    _mark()
    return ok_channels


# ---------------- 主入口 ----------------


def run_evaluation(db: Session, force: bool = False) -> dict:
    """评估全部启用规则一次。手动触发与后台循环共用，互斥。

    force=True（用户手动触发）绕过数据源优先级节奏；后台轮只评估到期账号
    （medium 5 分钟 / low 每天 02:00–03:05 窗口），全局 SLS 规则不受影响。
    """
    if not _run_lock.acquire(blocking=False):
        return {"skipped": "上一轮评估尚未结束"}
    started = time.time()
    fired = recovered = 0
    errors: list[str] = []
    try:
        from app.services.monitor_policy import collect_due, mark_collected

        due_cache: dict[int, bool] = {}
        evaluated_accounts: set[int] = set()

        def _due(acc_id) -> bool:
            key = int(acc_id or 0)
            if key not in due_cache:
                due_cache[key] = collect_due(db, "alert_eval", key, force=force)
            return due_cache[key]

        rules = db.query(AlertRule).filter(AlertRule.enabled == True).all()  # noqa: E712
        from app.services.tier import aux_instance_ids as _aux_ids
        aux_ids = _aux_ids(db)
        silence_hits: dict[int, str] = {}
        overrides: dict = {}   # metric 规则的 per-target 水位覆盖（SLS 规则不用）
        for rule in rules:
            silence = _in_silence(rule.silences)
            if silence:
                silence_hits[rule.id] = silence
            try:
                if rule.source == "metric":
                    targets = _eval_metric_rule(rule)
                    name_map = {}
                    acct_map = {}   # 目标 → 账号（cloud_accounts.id）
                    for a in db.query(Asset).filter(Asset.product == rule.product).all():
                        name_map.setdefault(a.instance_id, a.name or a.instance_id)
                        acct_map.setdefault(a.instance_id, a.account_id or 0)
                    # 目标级水位："95% 是这台机器的常态"时按自身水位判定，只报显著偏离
                    overrides = _target_overrides(db, rule)
                    if overrides:
                        for t, (b, v, part) in list(targets.items()):
                            ov = overrides.get(t)
                            if ov is None:
                                continue
                            high = _compare(rule.comparator, v, ov.threshold_override)
                            # 骤降=接近归零才算（该忙反闲=业务真停）。不能用"水位×0.6"——
                            # 间歇冲高型机器（CPU 平时趴着、间歇冲 95）低谷是常态，会误报
                            low = rule.comparator in (">", ">=") and v < ov.baseline_value * 0.05
                            targets[t] = (high or low, v, part)
                    # aux 辅助机器豁免指标告警，只看关机与 5xx 外溢；现存 firing 一并关闭
                    if aux_ids:
                        for t in [t for t in targets if t in aux_ids]:
                            del targets[t]
                        for rec in db.query(AlertRecord).filter(
                                AlertRecord.rule_id == rule.id,
                                AlertRecord.target.in_(list(aux_ids)),
                                AlertRecord.status == "firing").all():
                            rec.status = "recovered"
                            rec.recovered_at = datetime.now()
                            rec.content = (rec.content or "") + "\n[辅助机器(tier=aux)不再监测指标告警，自动关闭]"
                    # 规则绑定账号时只评估该账号的资产目标。
                    # 已知归属其他账号的目标剔除；对不上资产/未打标的目标保留（不猜归属不误杀）
                    if getattr(rule, "account_id", 0):
                        targets = {
                            t: v for t, v in targets.items()
                            if acct_map.get(t, 0) in (0, rule.account_id)
                        }
                    # 分级监控：未到评估窗口的账号在当前周期不评估（已 firing 记录保持原状，
                    # 到期窗口再评估恢复/重申；归因不明的目标 account=0 照旧评估）
                    targets = {t: v for t, v in targets.items()
                               if _due(acct_map.get(t, 0))}
                else:
                    raw = _eval_sls_rule(db, rule)
                    targets = {t: [b, v, ""] for t, (b, v) in raw.items()}   # SLS 无分区概念
                    name_map = {}
                    acct_map = {}
            except Exception as e:
                msg = f"{rule.name}: {type(e).__name__} {str(e)[:120]}"
                errors.append(msg)
                logger.warning("规则评估失败 %s", msg)
                continue

            for target, (breached, value, part) in targets.items():
                evaluated_accounts.add(int(acct_map.get(target, 0) or 0))
                record = (
                    db.query(AlertRecord)
                    .filter(
                        AlertRecord.rule_id == rule.id,
                        AlertRecord.target == target,
                        AlertRecord.status == "firing",
                    )
                    .first()
                )
                if breached and record is None:
                    # 再触发抑制期用于降噪：同规则同目标刚恢复不久又越限
                    # （贴线抖动的余波），只记录不推送
                    last = (
                        db.query(AlertRecord)
                        .filter(AlertRecord.rule_id == rule.id, AlertRecord.target == target)
                        .order_by(AlertRecord.id.desc()).first()
                    )
                    suppressed = bool(
                        last is not None and last.recovered_at
                        and (datetime.now() - last.recovered_at).total_seconds()
                        < settings.ALERT_RESUPPRESS_MINUTES * 60
                    )
                    from app.services.tenant import account_id as _acc_id
                    _imp = _biz_impact(db, target, name_map.get(target, target), rule.level)
                    record = AlertRecord(
                        # 账号打标优先级：目标资产账号 > 规则绑定账号 > 采集归属
                        account_id=acct_map.get(target) or getattr(rule, "account_id", 0) or _acc_id(db),
                        impact_level=_imp["impact_level"], impact_desc=_imp["impact_desc"],
                        rule_id=rule.id, rule_name=rule.name, source=rule.source,
                        target=target, target_name=name_map.get(target, target),
                        level=rule.level,
                        title=(f"{rule.name} - {name_map.get(target, target)}"
                               + (f"（{part}）" if part else "")),
                        content=(_fmt_rule_def(rule)
                                 + (_log_error_attribution(db, rule)
                                    if "error" in (rule.query_expr or "").lower()
                                    else _slb_5xx_attribution(db, rule))
                                 + (f"\n- 超限分区/序列: {part}" if part else "")),
                        status="firing",
                        breach_value=value, silenced=bool(silence) or suppressed,
                        silence_reason="window" if silence else ("resuppress" if suppressed else ""),
                    )
                    db.add(record)
                    db.flush()  # 拿 id 给站内信 ref_id
                    # #15 画像 0 延迟直挂：告警落库即刻驱动画像事件采样+短时突变判定
                    # （钩内采样/幂等/防御，异常绝不影响告警；event_tick 扫表仅为兜底，同键不双记）
                    try:
                        from app.services.profile_engine import land_hook
                        land_hook(db, "alert", record)
                    except Exception:
                        logger.warning("画像联动钩子异常", exc_info=True)
                    if silence or suppressed:
                        record.notified_channels = "[]"
                    else:
                        ok = _notify(db, rule, record, recovered=False)
                        record.notified_channels = json.dumps(ok, ensure_ascii=False)
                    fired += 1
                elif breached and record is not None:
                    record.last_eval_at = datetime.now()
                    record.breach_value = value
                    # 级别升级（warning → critical）立即解除静默并补发一次：
                    # 用户确认的是旧级别的问题，升级后必须重新通知
                    escalated = (record.level != rule.level and rule.level == "critical")
                    if record.level != rule.level:
                        record.level = rule.level
                    if escalated:
                        record.ack_status = "none"
                        record.ack_note = ""
                        record.ack_by = ""
                        record.ack_by_id = 0
                        record.ack_at = None
                        record.next_notify_at = None
                        record.notify_count = 0
                    was_silenced = record.silenced
                    # 抑制期内保持 silenced 标记（窗口结束才解除语义）
                    in_resuppress = (
                        getattr(record, "silence_reason", "") == "resuppress"
                        and record.fired_at is not None
                        and (datetime.now() - record.fired_at).total_seconds()
                        < settings.ALERT_RESUPPRESS_MINUTES * 60
                    )
                    record.silenced = bool(silence) or in_resuppress
                    if silence:
                        record.silence_reason = "window"
                    # 静默时段结束后仍在越限：补发一次触发通知。
                    # 注意：再触发抑制期（resuppress）永不补发；
                    # 否则抑制结束后仍越限的告警会被集中推送，失去降噪意义
                    can_resend = (
                        (escalated and not silence)
                        or (was_silenced and not silence
                            and record.notified_channels == "[]"
                            and record.ack_status == "none"   # 已确认的不再补发
                            and getattr(record, "silence_reason", "") != "resuppress")
                    )
                    if can_resend:
                        ok = _notify(db, rule, record, recovered=False)
                        record.notified_channels = json.dumps(ok, ensure_ascii=False)
                elif (not breached) and record is not None and _recovered(
                        rule, value,
                        (overrides[target].threshold_override if target in overrides else None)):
                    record.status = "recovered"
                    record.recovered_at = datetime.now()
                    record.last_eval_at = datetime.now()
                    # 恢复即 episode 结束：清空确认/静默状态（再次触发按新问题处理）
                    record.ack_status = "none"
                    record.ack_note = ""
                    record.ack_by = ""
                    record.ack_by_id = 0
                    record.ack_at = None
                    record.next_notify_at = None
                    # 恢复推送默认关闭，避免触发与恢复成对推送产生双倍噪音；
                    # 记录留痕，页面可查恢复时间
                    if not silence and settings.ALERT_RECOVERY_NOTIFY:
                        _notify(db, rule, record, recovered=True)
                    recovered += 1
                elif (not breached) and record is not None:
                    # 越限解除但仍在迟滞带内：保持 firing，只刷新观测值
                    record.last_eval_at = datetime.now()
                    record.breach_value = value
        for acc_id in evaluated_accounts:
            mark_collected(db, "alert_eval", acc_id)
        db.commit()
    finally:
        _run_lock.release()
    result = {
        "rules": len(rules),
        "fired": fired,
        "recovered": recovered,
        "errors": errors,
        "duration_ms": int((time.time() - started) * 1000),
    }
    _last_status.update({
        "last_run_at": datetime.now().isoformat(timespec="seconds"),
        "duration_ms": result["duration_ms"],
        "rules": result["rules"],
        "fired": fired,
        "recovered": recovered,
        "errors": errors[-10:],
    })
    return result
