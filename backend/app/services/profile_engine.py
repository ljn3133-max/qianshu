"""行为画像引擎：构成快照 → 学习正常基线 → 检测漂移与关联事件。

与阈值告警的区别：
- 阈值看数值（CPU>95）；本引擎看构成（top1、top3 成员及占比变化）；
- 基线可经人工确认，偏离基线时给出具体成员变化，而不只报告数值越限。

构成来源（复用既有数据面，进程画像使用带 name/pid/user 的 CMS process.cpu）：
- process:<instance>   实例进程 CPU top 构成
- cdn_traffic          CDN 流量按域名构成（本地 cdn_domain_daily）
- bill_product         账单按产品构成（本地 cost_daily）
- slb_uri              SLB 流量按 URI 构成（SLS）
- log_app              日志按应用构成（SLS）

学习：滚动窗口逐日样本 -> 成员出现频次>=60% 进基线，占比取中位数；
     样本>=5 且 top1 稳定（众数>=60%）才 learned，否则 observing 只记录。
漂移（learned 后才报）：top1 变了 / 陌生成员进 top3 / 基线成员占比突变或骤降。
人工确认 accept：把当前构成为新常态，吸收进画像。

状态与采样：
- 「已知道」只确认当前变化，不改基线；核心机器静默 1 天，辅助机器需显式选择 3 天静默，
  到期后漂移仍存在则恢复提醒（mark_known / notify_suppressed / run_profiles）。
- 慢层在 PROFILE_SAMPLE_HOURS 指定时刻采集当日累计快照，由 _aggregate_day 聚合成日构成；
  快层在告警、异动、变更落库时补记分钟级事件样本，用 short_mutation_check 检测短时突变。
- related_events_timeline 与 drift_change_windows 提供对象事件时间线和漂移/变更窗口关联。
- 告警、异动、变更的落库钩子立即采样与判定；event_tick 兜底覆盖未挂钩路径，并以同一
  (source, ref_id) 幂等键避免双记。profile_drift 不回灌事件样本，防止自反馈。
- aux 机器不建立或检测进程构成画像，也不写事件构成样本；原始事件时间线不受影响。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter
from datetime import date, datetime, time as dtime, timedelta
from statistics import median
from typing import Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.behavior_profile import BehaviorProfile
from app.models.cost_anomaly import CostAnomaly
from app.services.cost_anomaly import _add

logger = logging.getLogger("qianshu.profile")

TOP_N = 5          # 快照保留 top 成员数
_EVENT_SAMPLE_KEEP = 100   # 事件样本滚动窗口
_DAY_KEEP = 30             # 日构成样本滚动窗口
_EVENT_LOOKBACK_DAYS = 7   # 补录只认近 7 天的事件

# 采样写入互斥（run_profiles / event_tick / on_anomaly_landed 多入口）：事件样本是
# JSON 读改写，多线程并发会互相覆盖样本。RLock：主动钩子在 _add 内被调，检测器内部
# 亦可能持锁重入（profile_drift 自反馈闸会先短路，RLock 是纵深防御）
_profile_write_lock = threading.RLock()
_EVENT_TICK_MIN_INTERVAL_S = 120   # 事件补录节流兜底值（配置 PROFILE_EVENT_SYNC_INTERVAL_S 优先）
_last_event_tick = 0.0


def _is_system(name: str) -> bool:
    """系统例行进程/内核线程：快照可见但不参与基线/漂移判定。

    归一化：Windows 进程名可能带 .exe（AliSecCheck.exe）；Linux 内核线程
    呈 [kworker/...] 形式（kswapd0/ksoftirqd 等），一律视为非业务构成。
    """
    raw = (name or "").lower()
    if raw.startswith("["):          # 内核线程
        return True
    key = raw[:-4] if raw.endswith(".exe") else raw
    try:
        sys_names = {x.lower().removesuffix(".exe") for x in json.loads(settings.PROFILE_SYSTEM_PROCESSES)}
    except Exception:
        sys_names = set()
    return key in sys_names


# ---------------- 构成采集器（每类：某窗口的成员构成） ----------------

def _share(members: list) -> dict:
    """[{name,value}] -> {members:[{name,share_pct,value}], total}，按占比降序取 TOP_N。"""
    total = sum(m["value"] for m in members)
    if total <= 0:
        return {"members": [], "total": 0}
    members = sorted(members, key=lambda m: -m["value"])[:TOP_N]
    return {
        "members": [
            {"name": str(m["name"])[:64], "share_pct": round(m["value"] / total * 100, 2),
             "value": round(m["value"], 3)}
            for m in members
        ],
        "total": round(total, 3),
    }


def _collect_cdn_day(db: Session, day: date) -> dict:
    # 本地日表（T+1）：只有日粒度，窗口参数对它无意义（如实按日取）
    from app.models.cdn_domain_daily import CdnDomainDaily
    rows = db.query(CdnDomainDaily).filter(CdnDomainDaily.stat_date == day).all()
    return _share([{"name": r.domain, "value": (r.traffic_bytes or 0) / 1024 ** 3} for r in rows])


def _collect_bill_day(db: Session, day: date) -> dict:
    from sqlalchemy import func
    from app.models.cost_daily import CostDaily
    rows = (db.query(CostDaily.product_name, func.sum(CostDaily.pretax_amount))
            .filter(CostDaily.stat_date == day).group_by(CostDaily.product_name).all())
    return _share([{"name": n or "?", "value": float(v or 0)} for n, v in rows])


_ALLOWED_NAME_FIELDS = {"request_uri", "AppName"}   # 进查询串的字段名只许白名单


def _collect_sls_day(db: Session, day: date, project: str, logstore: str, name_field: str,
                     start_ts: Optional[int] = None, end_ts: Optional[int] = None) -> dict:
    if name_field not in _ALLOWED_NAME_FIELDS:
        logger.warning("画像 SLS 字段名不在白名单: %s", name_field)
        return {"members": [], "total": 0}
    from app.collector.credential import resolve_sls_credential
    from app.collector.sls import query_logs
    ak, sk, _ = resolve_sls_credential(db)
    start = start_ts if start_ts is not None else int(datetime.combine(day, dtime.min).timestamp())
    end = end_ts if end_ts is not None else start + 86400
    q = f"* | select {name_field} as name, count(*) as value group by {name_field} order by value desc limit {TOP_N}"
    try:
        r = query_logs(ak, sk, project, logstore, q, start=start, end=end, limit=TOP_N)
    except Exception as e:
        logger.warning("画像 SLS 查询失败 %s/%s %s: %s", project, logstore, day, str(e)[:100])
        return {"members": [], "total": 0}
    return _share([{"name": row.get("name", "?"), "value": float(row.get("value") or 0)}
                   for row in r["logs"]])


def _collect_process_day(db: Session, day: date, instance_id: str,
                         start_ts: Optional[int] = None, end_ts: Optional[int] = None) -> dict:
    """进程 CPU top 构成（CMS process.cpu：字段 name/pid/user/Value）。"""
    import json as _json
    from app.collector.cms import _client
    from app.collector.credential import resolve_aliyun_credential
    from app.models.asset import Asset
    from alibabacloud_cms20190101 import models as cms

    ak, sk, _ = resolve_aliyun_credential(db)
    inst = db.query(Asset).filter(Asset.instance_id == instance_id).first()
    region = (inst.region if inst else None) or "cn-hangzhou"
    start_s = start_ts if start_ts is not None else int(datetime.combine(day, dtime.min).timestamp())
    end_s = end_ts if end_ts is not None else start_s + 86400
    start_ms, end_ms = start_s * 1000, end_s * 1000
    try:
        c = _client(ak, sk, region)
        req = cms.DescribeMetricListRequest(
            namespace="acs_ecs_dashboard", metric_name="process.cpu",
            dimensions=_json.dumps([{"instanceId": instance_id}]), period="300",
            start_time=str(start_ms), end_time=str(end_ms))
        dps = json.loads(c.describe_metric_list(req).body.datapoints or "[]")
    except Exception as e:
        logger.warning("画像进程查询失败 %s %s: %s", instance_id, day, str(e)[:100])
        return {"members": [], "total": 0}
    by_name: dict = {}
    for dp in dps:
        name = dp.get("name") or "?"
        v = dp.get("Average")
        if v is None:
            continue
        by_name.setdefault(name, []).append(float(v))
    return _share([{"name": k, "value": sum(vs) / len(vs)} for k, vs in by_name.items()])


def collect_windowed(db: Session, p: BehaviorProfile, start: datetime, end: datetime) -> dict:
    """[start, end) 窗口的构成快照。慢层当日快照与快层事件样本共用此入口。

    - 实时源（process/slb_uri/log_app）：按窗口实查（CMS/SLS）；
    - T+1 本地日表（cdn_traffic/bill_product）：只有日粒度，按 start 所在日取数
      （窗口语义对它不存在，如实降级——不造假的日内构成）。
    """
    day = start.date()
    s_ts, e_ts = int(start.timestamp()), int(end.timestamp())
    if p.kind == "process":
        return _collect_process_day(db, day, p.subject, s_ts, e_ts)
    if p.kind == "cdn_traffic":
        return _collect_cdn_day(db, day)
    if p.kind == "bill_product":
        return _collect_bill_day(db, day)
    if p.kind == "slb_uri":
        from app.services.sls_targets import outer_slb
        project, logstore = outer_slb()
        return _collect_sls_day(db, day, project, logstore, "request_uri", s_ts, e_ts)
    if p.kind == "log_app":
        from app.services.sls_targets import app_log
        project, logstore = app_log()
        return _collect_sls_day(db, day, project, logstore, "AppName", s_ts, e_ts)
    return {"members": [], "total": 0}


def collect_day(db: Session, p: BehaviorProfile, day: date) -> dict:
    return collect_windowed(db, p, datetime.combine(day, dtime.min),
                            datetime.combine(day + timedelta(days=1), dtime.min))


# ---------------- 慢层：当日多快照 -> 日构成聚合 ----------------

def _aggregate_day(snapshots: list) -> dict:
    """当日多快照聚合成日构成：

    - 每份快照 = 一个采样时刻的**当日累计口径**构成观测（00:00 → 采样时刻）；
    - 成员占比 = 该成员在**其出现的快照**中的中位数——中位数抗单次采集抖动
      （限速重试/局部空窗），出现率 <50% 快照的成员直接剔除（偶发成员不进日构成，
      与 learn_baseline 的"出现频次>=60% 才算正常成员"哲学一致）；
    - total = 各快照 total 的均值（保留总量级）；
    - 按聚合占比降序取 TOP_N；快照 0 份返回空（宁缺勿报），1 份直接采用。

    学习与检测读的都是本函数聚合后的"日构成"，样本间口径统一可比。
    """
    if not snapshots:
        return {"members": [], "total": 0}
    n = len(snapshots)
    if n == 1:
        return {"members": list(snapshots[0].get("members") or []),
                "total": snapshots[0].get("total", 0)}
    appear: dict = {}
    for s in snapshots:
        for m in (s.get("members") or []):
            appear.setdefault(str(m.get("name")), []).append(float(m.get("share_pct") or 0))
    members = []
    for name, vals in appear.items():
        if len(vals) / n < 0.5:
            continue   # 出现率不足一半：偶发成员，不进日构成
        members.append({"name": name[:64], "share_pct": round(median(vals), 2),
                        "value": round(median(vals), 3)})
    members.sort(key=lambda m: -m["share_pct"])
    totals = [float(s.get("total") or 0) for s in snapshots]
    return {"members": members[:TOP_N], "total": round(sum(totals) / len(totals), 3)}


def _day_covered(entry: dict, now: datetime) -> bool:
    """当日聚合构成参与日构成漂移检测的门槛（宁缺勿报，防夜间半日样本误报）：

    当日快照 >=3 份，或最新快照时刻已过 12:00（覆盖主要活动时段）才判；
    否则只采样学习不判漂移——凌晨/清晨的"当日累计"构成天然偏夜型，
    直接比全日基线会系统性误报；短时构成突变（事件层）不受此限。
    """
    snaps = entry.get("snapshots") or []
    if len(snaps) >= 3:
        return True
    try:
        last_ts = datetime.fromisoformat(str(snaps[-1]["ts"]))
        return last_ts.hour >= 12
    except (IndexError, KeyError, TypeError, ValueError):
        return now.hour >= 12


# ----------------「已知道」状态 + 分级静默 ----------------

def silence_for_tier(tier: str, silence_3d: bool) -> Optional[timedelta]:
    """分级静默规则：
    - 核心业务机器（assets.tier='core'）：知道了即静默 1 天，到期漂移仍在则恢复提醒；
    - 辅助机器（tier='aux'）：勾选"静默 3 天"才生效，默认不静默、持续通知。
    """
    if tier == "aux":
        return timedelta(days=3) if silence_3d else None
    return timedelta(days=1)


def tier_of_profile(db: Session, p: BehaviorProfile) -> str:
    """画像对应机器的关键度：process 按 assets.tier；平台级构成画像（cdn/账单/SLB/日志）
    不是机器，按核心口径（静默 1 天）。"""
    if p.kind == "process" and p.subject:
        from app.services.tier import tier_of
        return tier_of(db, p.subject)
    return "core"


def mark_known(p, tier: str, silence_3d: bool, now: Optional[datetime] = None) -> dict:
    """写入 known 状态与静默期（不动事件——事件收口由调用方按静默是否生效决定）。"""
    now = now or datetime.now()
    delta = silence_for_tier(tier, silence_3d)
    p.status = "known"
    p.known_at = now
    p.silence_until = (now + delta) if delta else None
    p.notify_state = "silenced" if delta else ""
    return {"status": "known", "tier": tier,
            "silence_until": p.silence_until.isoformat() if p.silence_until else None,
            "notify_state": p.notify_state, "silenced": bool(delta)}


def notify_suppressed(p, now: Optional[datetime] = None) -> bool:
    """通知抑制判定：已知道且静默期内不生成或刷新通知事件，到期自动恢复。"""
    now = now or datetime.now()
    return p.status == "known" and p.silence_until is not None and now < p.silence_until


def close_drift_events(db: Session, profile_key: str, note: str) -> int:
    """收口某画像的漂移事件（日构成 + 短时突变两条通道）。返回收口条数。"""
    n = (db.query(CostAnomaly)
         .filter(CostAnomaly.type == "profile_drift",
                 CostAnomaly.target_key.in_((profile_key, f"{profile_key}#fast")),
                 CostAnomaly.ack_status.in_(("open", "acknowledged")))
         .update({CostAnomaly.ack_status: "resolved", CostAnomaly.ack_note: note},
                 synchronize_session=False))
    return n


def know_profile(db: Session, profile_key: str, by: str, silence_3d: bool = False) -> dict:
    """「知道了」只确认当前变化，不改基线，与 accept 记为正常的语义不同。

    core 静默 1 天，aux 勾选后才静默 3 天。静默生效时收口既有漂移事件；到期后漂移仍在，
    下一轮检测重新建事件以恢复提醒。
    """
    p = db.query(BehaviorProfile).filter(BehaviorProfile.profile_key == profile_key).first()
    if p is None:
        raise ValueError(f"画像不存在: {profile_key}")
    tier = tier_of_profile(db, p)
    info = mark_known(p, tier, silence_3d)
    closed = 0
    if p.silence_until:
        closed = close_drift_events(
            db, profile_key,
            f"用户已知道（by {by}），静默至 {p.silence_until:%Y-%m-%d %H:%M}，到期漂移仍在则恢复提醒")
    p.drift_note = p.drift_note   # 保留变化说明（已知道≠变化消失）
    db.commit()
    return {"profile_key": profile_key, "closed_events": closed, **info}


# ---------------- 快层：事件样本补录 ----------------

def _is_aux_profile(db: Session, p: BehaviorProfile) -> bool:
    """aux 辅助机器不启用 process 构成检测，也不补事件构成样本。

    与 run_profiles 跳过 aux、ensure_profiles 不给 aux 建画像保持一致；
    事件时间线仍读取原始事件表，不受采样停用影响。
    """
    if p.kind != "process" or not p.subject:
        return False
    from app.services.tier import aux_instance_ids
    try:
        return p.subject in aux_instance_ids(db)
    except Exception:
        return False


def record_event_sample(db: Session, p: BehaviorProfile, ts: datetime,
                        source: str, ref_id: str, title: str) -> bool:
    """给画像记一笔快层事件样本，sample_kind='event'，时间戳取事件真实时间。

    构成 = 事件时刻前 1 小时窗口的构成快照（与日样本互补）；采集失败/无构成也记
    标记（members=[]），事件时间线不丢。幂等：同 (source, ref_id) 不重复记。

    aux 机器构成检测停用（run_profiles 跳过、不建画像），事件构成采样同口径跳过：
    样本唯一消费方是短时突变判定，检测停用后不应继续积累无人消费的数据。
    本函数是事件样本唯一写入口（钩子/兜底扫表/直调三路共用），在此单点收口。
    """
    if _is_aux_profile(db, p):
        return False
    try:
        samples = json.loads(p.event_samples or "[]")
    except ValueError:
        samples = []
    if any(s.get("ref_id") == ref_id for s in samples):
        return False
    snap = {"members": [], "total": 0}
    try:
        snap = collect_windowed(db, p, ts - timedelta(hours=1), ts)
    except Exception as e:
        logger.warning("事件样本构成采集失败 %s %s: %s", p.profile_key, ref_id, str(e)[:100])
    samples.append({
        "ts": ts.strftime("%Y-%m-%d %H:%M:%S"),
        "sample_kind": "event",
        "source": source, "ref_id": ref_id, "title": (title or "")[:200],
        "members": snap.get("members") or [], "total": snap.get("total", 0),
    })
    p.event_samples = json.dumps(samples[-_EVENT_SAMPLE_KEEP:], ensure_ascii=False)
    return True


def _match_profiles(profiles: list, text: str) -> list:
    """事件文本 -> 对应画像：subject/profile_key 为标识，忽略大小写子串命中。

    subject='*'（平台级构成：cdn/账单）无单一对象，不挂事件样本（如实跳过）。
    """
    low = (text or "").lower()
    out = []
    for p in profiles:
        if not p.subject or p.subject == "*":
            continue
        if p.subject.lower() in low or p.profile_key.lower() in low:
            out.append(p)
    return out


def sync_event_samples(db: Session, max_recorded: int = 24) -> dict:
    """快层补录挂接点：扫三类事件的新落库记录 -> 匹配画像 -> 记事件样本。

    事件发生点 = 告警评估（alert_eval 落库）/ 异动检测（cost_anomaly 落库）/
    变更落库（change_events）。三处均已接落库即钩子（on_alert_landed /
    on_anomaly_landed / on_change_landed 秒级入线）；本扫表是兜底，覆盖未挂钩
    落库路径（events_webhook、host_watch/synth 内置双写等）与任何绕过钩子的写入，
    延迟=采集循环周期。每轮扫描 alert_records / cost_anomalies / change_events 近
    7 天的新记录，按 target/resource 匹配画像对象补记事件样本（时间戳用事件真实
    时间，分钟级）。幂等：(source, ref_id) 去重（与钩子同键，不双记）。
    """
    from app.models.alert_record import AlertRecord
    from app.models.change_event import ChangeEvent

    since = datetime.now() - timedelta(days=_EVENT_LOOKBACK_DAYS)
    profiles = db.query(BehaviorProfile).all()
    scanned = recorded = 0
    touched: set = set()

    def _emit(p: BehaviorProfile, ts, source: str, ref_id: str, title: str) -> None:
        nonlocal recorded
        if ts is None or recorded >= max_recorded or ts < since:
            return
        if record_event_sample(db, p, ts, source, ref_id, title):
            recorded += 1
            touched.add(p.profile_key)

    rows = (db.query(AlertRecord).order_by(AlertRecord.id.desc()).limit(100).all())
    for r in rows:
        scanned += 1
        text = f"{r.target} {r.target_name} {r.title}"
        for p in _match_profiles(profiles, text):
            _emit(p, r.fired_at, "alert", f"alert:{r.id}", f"[{r.level}] {r.rule_name} {r.target_name or r.target}")

    rows = (db.query(CostAnomaly).order_by(CostAnomaly.id.desc()).limit(100).all())
    for a in rows:
        scanned += 1
        if a.type == "profile_drift":
            continue   # 自反馈闸：画像引擎自己的漂移通知不是外部事件，
            # 不回灌事件样本，避免“漂移通知→样本→短时突变→通知”自激；
            # 外部异动（payg_spike 等）与 chain_drift 照常补录。
        text = f"{a.target_key} {a.target_name} {a.title}"
        for p in _match_profiles(profiles, text):
            _emit(p, a.created_at, "anomaly", f"anomaly:{a.id}", f"[{a.level}] {a.title}")

    rows = (db.query(ChangeEvent).order_by(ChangeEvent.id.desc()).limit(100).all())
    for c in rows:
        scanned += 1
        text = f"{c.resource} {c.summary} {c.event_name} {c.service}"
        for p in _match_profiles(profiles, text):
            _emit(p, c.event_time, "change", f"change:{c.id}",
                  f"{c.event_name} {c.resource}（{c.user or '未知操作者'}）")

    if recorded:
        db.flush()
    return {"scanned": scanned, "recorded": recorded, "touched": sorted(touched)}


def detect_short_mutation(db: Session, p: BehaviorProfile, now: Optional[datetime] = None) -> bool:
    """快层检测：事件样本短时构成突变（1 小时内 top1 换人即报，规则见
    baseline.short_mutation_check）。命中建 profile_drift 事件（target_key 带 #fast
    后缀，与日构成漂移分账、同域生命周期）。

    aux 机器构成检测停用：存量事件样本也不入判定。写入口已拒绝新样本，
    此处拦截存量或手工样本，确保两条路径都不进入漂移判定。"""
    if _is_aux_profile(db, p):
        return False
    from app.services.baseline import short_mutation_check
    now = now or datetime.now()
    try:
        samples = json.loads(p.event_samples or "[]")
    except ValueError:
        samples = []
    if not samples:
        return False
    try:
        last_ts = datetime.fromisoformat(str(samples[-1].get("ts")))
    except (TypeError, ValueError):
        return False
    # 时效门槛：只判 1 小时内仍在发生的突变（陈旧样本不反复重报）
    if last_ts < now - timedelta(hours=1):
        return False
    chk = short_mutation_check(samples[-30:], is_system=_is_system)
    if not chk.get("hit"):
        return False
    if notify_suppressed(p, now):
        return False   # 已知道且静默期内不发通知
    from app.services.profile_context import NOT_APPLICABLE, STABLE, UNVERIFIED, for_behavior_profile
    context = for_behavior_profile(db, p, now, samples[-1])
    if context["verdict"] == STABLE:
        # 原始 top1 变化仍留在画像说明里；稳定上下文只抑制无影响告警，不写成“整体正常”。
        p.drift_note = ("\n".join(chk["notes"]) + "\n" + context["note"])[:4000]
        if p.status not in ("drift", "unverified"):
            p.status = "context_stable"
        for old in db.query(CostAnomaly).filter(
                CostAnomaly.type == "profile_drift",
                CostAnomaly.target_key == f"{p.profile_key}#fast",
                CostAnomaly.ack_status.in_(("open", "acknowledged"))).all():
            old.ack_status = "resolved"
            old.ack_note = (old.ack_note or "") + "\n[构成仍变化，但整机/后端上下文稳定，本次不告警]"
        return False
    unverified = context["verdict"] == UNVERIFIED
    if unverified and p.status != "drift":
        p.status = "unverified"
    today = now.date()
    context_note = ("\n- 上下文：" + context["note"]) if context["verdict"] != NOT_APPLICABLE else ""
    _add(
        db, today, "profile_drift", f"{p.profile_key}#fast", f"{p.label}（短时构成）",
        (f"短时构成变化待核实: {p.label}" if unverified else f"短时构成突变: {p.label}"),
        (f"「{p.label}」的构成在短时间内发生 top1 更替（事件样本分钟级对照，日构成基线看不到这一层）\n\n"
         f"- 变化：\n" + "\n".join(f"  - {n}" for n in chk["notes"]) + context_note +
         f"\n- 处理：先看事发时刻的变更（发版/重启）；无变更则排查是谁接管了 top1"),
        value=0, baseline=0, ratio=0, level=(chk.get("level") or "warning"),
    )
    # 检出返回值不与"是否插入新行"绑定：_add 是生命周期 upsert，
    # 同日复现刷新既有事件时返回 None。与日构成漂移同口径，命中即算检出；
    # 无论新建还是刷新，事件都已落账。
    return True


def event_tick(db: Session, min_interval_s: Optional[float] = None,
               force: bool = False) -> dict:
    """快层事件补录的兜底扫表，挂在 metrics_job 采集循环。

    扫 alert_records / cost_anomalies（排除 profile_drift 自反馈）/ change_events
    三类事件的新落库 -> 匹配画像补记事件样本（sample_kind='event'，时间戳=事件真实
    时刻）-> 对补到样本的画像**即时**跑短时构成突变判定。

    主动联动分工：异动（on_anomaly_landed，挂 _add）、告警（on_alert_landed，
    挂 alert_eval）、变更（on_change_landed，挂统一变更源）均已落库即钩子秒级入线；
    本函数覆盖未挂钩落库路径（events_webhook、host_watch/synth 内置双写等）与任何
    绕过钩子的写入，延迟=采集循环周期。

    节流：min_interval_s（默认取配置 PROFILE_EVENT_SYNC_INTERVAL_S，兜底 120s）内
    重复调用直接返回（多账号逐轮触发只补一轮）；force=True 绕过节流（手动触发/测试）。
    PROFILE_EVENT_SYNC_ENABLED=False 时整体停用（可回滚）。与 run_profiles 共用写锁。
    """
    global _last_event_tick
    if not settings.PROFILE_EVENT_SYNC_ENABLED:
        return {"skipped": "disabled", "scanned": 0, "recorded": 0, "mutations": 0}
    if min_interval_s is None:
        min_interval_s = getattr(settings, "PROFILE_EVENT_SYNC_INTERVAL_S", 0) or _EVENT_TICK_MIN_INTERVAL_S
    now = time.monotonic()
    if not force and now - _last_event_tick < min_interval_s:
        return {"skipped": "throttled", "scanned": 0, "recorded": 0, "mutations": 0}
    _last_event_tick = now
    with _profile_write_lock:
        r = sync_event_samples(db)
        mutations = 0
        for key in r.get("touched") or []:
            p = db.query(BehaviorProfile).filter(BehaviorProfile.profile_key == key).first()
            if p is not None and detect_short_mutation(db, p):
                mutations += 1
        db.commit()
    return {**r, "mutations": mutations}


# ---------------- 主动联动：异动检出 -> 画像即刻入线 + 双向关联 ----------------

STATUS_LABELS = {"observing": "观察中", "normal": "正常", "known": "已知道", "drift": "漂移"}


def _land_event_sample(db: Session, row, source: str, ts: datetime,
                       title: str, match_text: str) -> dict:
    """告警、异动、变更三类落库钩子的共用核心：采样 + 判定 + 幂等 + 自反馈闸。

    - 采样：匹配对象画像补记事件样本（时间戳=事件真实时刻，分钟级）；
    - 判定：对补到样本的画像**即时**跑短时构成突变（不等 event_tick 扫表）；
    - 幂等：(source, ref_id=f"{source}:{id}") 去重——钩子与兜底扫表同键，不双记；
    - 自反馈闸（不许自激）：profile_drift（含 #fast）是画像引擎自己的漂移通知，
      不回灌事件样本——否则 漂移通知->样本->短时突变->通知 自激。此闸与
      sync_event_samples 的同名闸同语义（tests/test_profile_c4_active_mock.py 回归）；
      告警/变更源无此类型，闸随共用核心统一收口；
    - 防御：外层入口（land_hook / cost_anomaly._profile_active_hook）兜 try，
      本函数任何异常不影响事件落库本身。
    """
    if not settings.PROFILE_EVENT_SYNC_ENABLED:
        return {"skipped": "disabled", "recorded": 0, "mutations": 0}
    if getattr(row, "type", None) == "profile_drift":
        return {"skipped": "self_feedback", "recorded": 0, "mutations": 0}
    if getattr(row, "id", None) is None:
        db.flush()   # 取 id 做幂等键（与扫表补录同键 {source}:{id}，不双记）
    ref_id = f"{source}:{row.id}"
    recorded = mutations = 0
    with _profile_write_lock:
        profiles = db.query(BehaviorProfile).all()
        for p in _match_profiles(profiles, match_text):
            if record_event_sample(db, p, ts, source, ref_id, title):
                recorded += 1
                if detect_short_mutation(db, p):
                    mutations += 1
        db.flush()
    return {"recorded": recorded, "mutations": mutations, "ref": ref_id}


def on_anomaly_landed(db: Session, a: CostAnomaly) -> dict:
    """异动检出**主动钩子**（active_sync，挂 cost_anomaly._add 单点收口）：

    异动落库（cost_anomaly / host_watch / baseline_job 周期基线 / object_profile 等
    全部检出器都走 _add）即刻：匹配对象画像 -> 补记事件样本（时间戳=检出时刻）->
    短时构成突变判定——秒级，不等 event_tick 扫表（扫表只是兜底）。

    为什么挂 _add 单点而不是逐检出器挂钩/统一轮询：_add 是全部异动检出器唯一的
    落库口，一处挂接覆盖现有与未来的检出器（修一类不修一个），且在落库事务内
    同生共死——异动与画像事件样本要么都有要么都无，无半态。

    自反馈闸、幂等与开关统一在共用核心 _land_event_sample，回归见
    tests/test_profile_c4_active_mock.py。防御：钩子任何异常不影响异动
    检测本身（调用方 _profile_active_hook 兜 try）。
    """
    return _land_event_sample(db, a, "anomaly", a.created_at or datetime.now(),
                              f"[{a.level}] {a.title}",
                              f"{a.target_key} {a.target_name} {a.title}")


def on_alert_landed(db: Session, a) -> dict:
    """告警落库主动钩子，挂在 alert_eval 新告警落库点：

    新告警落库即刻驱动画像事件采样 + 短时构成突变判定（秒级，不等 event_tick）；
    与兜底扫表同键 alert:{id}（幂等不双记）、同匹配/同标题口径（_match_profiles，
    与 sync_event_samples 的 alert 分支逐字段一致）。静默/抑制的告警也入线——
    事件时间线要真实，通知抑制不等于事件不发生。
    """
    return _land_event_sample(db, a, "alert", a.fired_at or datetime.now(),
                              f"[{a.level}] {a.rule_name} {a.target_name or a.target}",
                              f"{a.target} {a.target_name} {a.title}")


def on_change_landed(db: Session, c) -> dict:
    """变更落库主动钩子，挂在统一变更源入库点：

    云上变更落库即刻入线（ref=change:{id}，与兜底扫表同键幂等不双记）；
    events_webhook 等未挂钩落库路径（红线不动）仍由 event_tick 兜底补录。
    """
    return _land_event_sample(db, c, "change", c.event_time or datetime.now(),
                              f"{c.event_name} {c.resource}（{c.user or '未知操作者'}）",
                              f"{c.resource} {c.summary} {c.event_name} {c.service}")


def land_hook(db: Session, source: str, row) -> None:
    """落库即钩子的防御入口，alert/change 落库点一行挂载：
    land_hook(db, "alert", record) / land_hook(db, "change", ev)。

    与 cost_anomaly._profile_active_hook 同款防御：钩子（采样/判定）任何异常绝不
    影响事件落库本身；recorded/mutations 有量才记日志（静默路径零噪音）；import
    失败由挂载点的 try 兜住。异动源不走本入口，仍挂 cost_anomaly._add 内的
    _profile_active_hook。
    """
    try:
        fn = {"alert": on_alert_landed, "change": on_change_landed}.get(source)
        if fn is None:
            logger.warning("画像主动联动钩子未知来源: %s（不处理）", source)
            return
        r = fn(db, row)
        if r.get("recorded") or r.get("mutations"):
            logger.info("画像主动联动(%s): %s", source, r)
    except Exception:
        logger.warning("画像主动联动钩子异常(%s)", source, exc_info=True)


def profile_links_for(db: Session, target_key: str, target_name: str = "", title: str = "",
                      at: Optional[datetime] = None, window_hours: int = 24,
                      profiles: Optional[list] = None) -> list:
    """异动 -> 关联画像：异动详情展示"对象漂移状态 + 构成变化摘要"。

    关联键 = 对象/资源维度对齐（instance/host/app：target_key/target_name/title 与画像
    subject/profile_key 子串命中，与事件样本匹配 _match_profiles 同口径）；
    时间窗 = 异动时刻 ±window_hours（默认 24h）内的画像漂移事件（含 #fast）为强关联
    证据；窗口内无漂移事件也给对象**当前**漂移状态与构成变化摘要（状态关联）。

    返回 [{profile_key, label, kind, subject, status, status_label, summary,
          top1_now, top1_base, learned, drift_events:[{id,title,ts,level,ack_status}],
          jump:{view:'profiles', profile_key}}]——jump 即前端跳转数据结构
    （/profiles?profile=<key> 直开画像详情）。
    """
    at = at or datetime.now()
    if profiles is None:
        profiles = db.query(BehaviorProfile).all()
    t0, t1 = at - timedelta(hours=window_hours), at + timedelta(hours=window_hours)
    out = []
    for p in _match_profiles(profiles, f"{target_key} {target_name} {title}"):
        base = json.loads(p.baseline or "{}")
        latest = json.loads(p.latest or "{}")
        lm = latest.get("members") or []
        top1_now = str(lm[0].get("name", "")) if lm else ""
        top1_base = str(base.get("top1") or "")
        if p.status == "drift" and p.drift_note:
            summary = p.drift_note.split("\n")[0]
        elif top1_base and top1_now and top1_base != top1_now:
            summary = f"top1：{top1_base} → {top1_now}"
        else:
            summary = f"top1：{top1_now or '—'}（{'与正常画像一致' if p.learned else '学习中'}）"
        evs = (db.query(CostAnomaly)
               .filter(CostAnomaly.type == "profile_drift",
                       CostAnomaly.target_key.in_((p.profile_key, f"{p.profile_key}#fast")),
                       CostAnomaly.created_at >= t0, CostAnomaly.created_at <= t1)
               .order_by(CostAnomaly.id.desc()).limit(5).all())
        out.append({
            "profile_key": p.profile_key, "label": p.label, "kind": p.kind,
            "subject": p.subject, "status": p.status,
            "status_label": STATUS_LABELS.get(p.status, p.status),
            "summary": summary,
            "top1_now": top1_now, "top1_base": top1_base,
            "learned": bool(p.learned),
            "drift_events": [{"id": e.id, "title": e.title,
                              "ts": e.created_at.isoformat() if e.created_at else "",
                              "level": e.level, "ack_status": e.ack_status} for e in evs],
            "jump": {"view": "profiles", "profile_key": p.profile_key},
        })
    return out


# ---------------- 画像创建 ----------------

# subject 从配置生成；SLS 未配置时跳过对应画像（千枢默认态：不建垃圾画像，
# 配好 SLS_* 后下一次 ensure_profiles 自动补上）
def _default_profiles() -> list:
    from app.services.sls_targets import app_log, outer_slb
    out = [
        ("cdn_traffic", "*", "CDN 流量构成（按域名）"),
        ("bill_product", "*", "账单构成（按产品）"),
    ]
    _slb_p, _slb_l = outer_slb()
    if _slb_p:
        out.append(("slb_uri", f"{_slb_p}/{_slb_l}", "外层 SLB 流量构成（按 URI）"))
    _app_p, _app_l = app_log()
    if _app_l:
        out.append(("log_app", _app_l, "应用日志构成（按应用）"))
    return out


_DEFAULT_PROFILES = _default_profiles()


def ensure_profiles(db: Session, discover_process: bool = True) -> list:
    """确保默认画像存在；process 画像对有 process.cpu 数据的 ECS 自动发现（每台一次探测）。"""
    created = []
    for kind, subject, label in _DEFAULT_PROFILES:
        key = f"{kind}:{subject}"
        if db.query(BehaviorProfile).filter(BehaviorProfile.profile_key == key).first() is None:
            db.add(BehaviorProfile(profile_key=key, kind=kind, subject=subject, label=label))
            created.append(key)
    if discover_process and db.query(BehaviorProfile).filter(
            BehaviorProfile.kind == "process").count() == 0:
        from app.models.asset import Asset
        from app.collector.cms import _client
        from app.collector.credential import resolve_aliyun_credential
        import json as _json
        from alibabacloud_cms20190101 import models as cms
        ak, sk, _ = resolve_aliyun_credential(db)
        now_ms = int(time.time() * 1000)
        from app.services.tier import aux_instance_ids as _aux
        aux = _aux(db)
        for a in db.query(Asset).filter(Asset.product == "ecs", Asset.status != "Deleted").all():
            if a.instance_id in aux:
                continue   # aux 辅助机器不做进程画像（用户策略：爆了都无所谓）
            try:
                c = _client(ak, sk, a.region or "cn-hangzhou")
                req = cms.DescribeMetricListRequest(
                    namespace="acs_ecs_dashboard", metric_name="process.cpu",
                    dimensions=_json.dumps([{"instanceId": a.instance_id}]), period="60",
                    start_time=str(now_ms - 3600 * 1000), end_time=str(now_ms))
                dps = json.loads(c.describe_metric_list(req).body.datapoints or "[]")
            except Exception:
                dps = []
            if dps:
                db.add(BehaviorProfile(
                    profile_key=f"process:{a.instance_id}", kind="process", subject=a.instance_id,
                    label=f"进程构成 · {a.name or a.instance_id}"))
                created.append(f"process:{a.instance_id}")
            time.sleep(0.15)  # CMS 限速礼貌
    db.commit()
    return created


# ---------------- 学习与漂移 ----------------

def learn_baseline(samples: list) -> dict:
    """滚动样本 -> 正常构成基线。成员出现频次 >=60% 才算'正常成员'。"""
    if not samples:
        return {}
    n = len(samples)
    freq: Counter = Counter()
    shares: dict = {}
    top1s: Counter = Counter()
    for s in samples:
        biz = [m for m in s.get("members", []) if not _is_system(m["name"])]
        for m in biz:
            freq[m["name"]] += 1
            shares.setdefault(m["name"], []).append(m["share_pct"])
        if biz:
            top1s[biz[0]["name"]] += 1
    members = []
    for name, f in freq.most_common():
        if f / n < 0.6:
            continue
        vals = shares[name]
        members.append({
            "name": name, "freq": round(f / n, 2),
            "share_avg": round(median(vals), 2),
            "share_min": round(min(vals), 2), "share_max": round(max(vals), 2),
        })
    top1, top1_freq = (top1s.most_common(1)[0] if top1s else ("", 0))
    return {
        "members": members,
        "top1": top1,
        "top1_freq": round(top1_freq / n, 2) if n else 0,
        "n": n,
        "learned_at": date.today().isoformat(),
    }


def _is_gentle(notes: list) -> bool:
    """温和漂移：只有占比突变/骤降，无 top1 更替、无陌生成员——可自动学成新常态。"""
    return bool(notes) and all(
        ("占比" in n or "骤降" in n) and "陌生成员" not in n and "top1" not in n for n in notes)


def detect_drift(baseline: dict, latest: dict) -> tuple:
    """构成漂移检测。返回 (drifted, level, [差异说明...])。"""
    if not baseline or not baseline.get("members"):
        return False, "", []
    b_names = {m["name"]: m for m in baseline["members"]}
    lm = [m for m in (latest.get("members") or []) if not _is_system(m["name"])]
    notes: list = []
    level = "warning"
    top1_now = lm[0]["name"] if lm else ""
    if baseline.get("top1") and top1_now and top1_now != baseline["top1"]:
        b = b_names.get(baseline["top1"], {})
        notes.append(f"top1 变化：{baseline['top1']}（正常时 {b.get('share_avg', '?')}%）→ 现在是 {top1_now}"
                     f"（{lm[0]['share_pct']}%）")
    for m in lm[:3]:
        if m["name"] not in b_names and m["share_pct"] >= settings.PROFILE_NEW_MEMBER_MIN_SHARE:
            notes.append(f"陌生成员进入 top3：{m['name']} 占 {m['share_pct']}%（正常画像无此成员）")
            if m["share_pct"] >= 50:
                level = "critical"
    for name, b in b_names.items():
        cur = next((x for x in lm if x["name"] == name), None)
        cur_share = cur["share_pct"] if cur else 0.0
        avg = b.get("share_avg", 0)
        if avg >= 10 and cur_share < avg * 0.3:
            notes.append(f"原成员骤降：{name} 从正常 {avg}% 降到 {cur_share}%")
        elif avg >= 10 and cur_share > avg * 2 and (cur_share - avg) >= 10:
            notes.append(f"成员占比突变：{name} 从正常 {avg}% 升到 {cur_share}%")
    return bool(notes), level, notes


def run_profiles(db: Session) -> dict:
    """双层采样入口：每天 PROFILE_SAMPLE_HOURS 时刻各跑一轮，手动「立即采样」同入口。

    1) 快层：扫表补录事件样本（告警/异动/变更 -> sample_kind='event'）；
    2) 慢层：当日快照（当日累计口径）-> 多快照聚合成日构成（_aggregate_day）；
       历史缺口补样（实时源历史日定型不重采；T+1 本地表每轮对齐，本地 SQL 便宜）；
    3) 学习 + 日构成漂移检测（已知道+静默期内只记不发，到期恢复提醒）；
    4) 快层检测：事件样本短时构成突变（1 小时内 top1 换人，baseline.short_mutation_check）。
    """
    with _profile_write_lock:   # 与 event_tick 互斥（事件样本 JSON 读改写防交错）
        return _run_profiles_impl(db)


def _run_profiles_impl(db: Session) -> dict:
    ensure_profiles(db)
    today = date.today()
    now = datetime.now()
    stats = {"profiles": 0, "drifted": 0, "event_samples": 0, "mutations": 0, "snapshots": 0}
    ev = sync_event_samples(db)
    stats["event_samples"] = ev.get("recorded", 0)
    from app.services.tier import aux_instance_ids as _aux
    aux = _aux(db)
    for p in db.query(BehaviorProfile).all():
        if p.kind == "process" and p.subject in aux:
            continue   # aux 辅助机器的画像停用（只看关机与 5xx 外溢）
        samples = json.loads(p.samples or "[]")
        by_date = {s.get("date"): s for s in samples if s.get("date")}

        # ---- 慢层 1：历史缺口补样（只补缺失日） ----
        for i in range(settings.PROFILE_LEARN_DAYS, 0, -1):
            d = today - timedelta(days=i)
            key = d.isoformat()
            need = key not in by_date or p.kind in ("cdn_traffic", "bill_product")
            if not need:
                continue   # 实时源（CMS/SLS）历史日数据定型，不重复打 API
            snap = collect_day(db, p, d)
            if snap["members"]:
                entry = {"date": key, "sample_kind": "daily", **snap,
                         "snapshots": [{"ts": f"{key} 23:59:59", **snap}]}
                samples = [s for s in samples if s.get("date") != key] + [entry]
                by_date[key] = entry
            time.sleep(0.12)   # CMS/SLS 限速礼貌

        # ---- 慢层 2：当日快照（当日累计口径） -> 聚合日构成 ----
        snap = collect_windowed(db, p, datetime.combine(today, dtime.min), now)
        if snap["members"]:
            stats["snapshots"] += 1
            entry = by_date.get(today.isoformat())
            if entry is None:
                entry = {"date": today.isoformat(), "sample_kind": "daily",
                         "members": [], "total": 0, "snapshots": []}
                samples.append(entry)
                by_date[today.isoformat()] = entry
            if "snapshots" not in entry:
                entry["snapshots"] = [{"ts": f"{today.isoformat()} 23:59:59", **snap}]
            else:
                entry["snapshots"].append({"ts": now.strftime("%Y-%m-%d %H:%M:%S"), **snap})
            agg = _aggregate_day(entry["snapshots"])
            entry["members"], entry["total"] = agg["members"], agg["total"]

        samples = sorted(samples, key=lambda s: s.get("date", ""))[-_DAY_KEEP:]
        if not samples:
            continue
        stats["profiles"] += 1
        p.samples = json.dumps(samples, ensure_ascii=False)
        latest_entry = by_date.get(today.isoformat()) or samples[-1]
        p.latest = json.dumps(latest_entry, ensure_ascii=False)

        baseline = json.loads(p.baseline or "{}")
        if not p.learned or len(samples) >= settings.PROFILE_MIN_SAMPLES:
            new_base = learn_baseline(samples)
            if new_base:
                # 已 learned 的画像不因单日波动重学（重学走 accept 人工确认）
                if not p.learned:
                    p.baseline = json.dumps(new_base, ensure_ascii=False)
                    if (len(samples) >= settings.PROFILE_MIN_SAMPLES
                            and new_base.get("top1_freq", 0) >= 0.6):
                        p.learned = True
                        logger.info("画像 %s 形成正常基线: top1=%s（%d 个样本）",
                                    p.profile_key, new_base.get("top1"), new_base.get("n"))
                baseline = json.loads(p.baseline or "{}")

        if p.learned and baseline:
            # 当日覆盖门槛（宁缺勿报）：聚合构成没盖够时段不判日构成漂移
            today_entry = by_date.get(today.isoformat())
            can_judge_today = today_entry is None or _day_covered(today_entry, now)
            judged = latest_entry if can_judge_today or latest_entry is not today_entry else None
            if judged is not None:
                drifted, level, notes = detect_drift(baseline, judged)
            else:
                drifted, level, notes = False, "", []
            from app.services.profile_context import NOT_APPLICABLE, STABLE, UNVERIFIED, for_behavior_profile
            context = {"verdict": NOT_APPLICABLE, "note": ""}
            if drifted and p.kind in ("process", "slb_uri"):
                if judged.get("date") == today.isoformat():
                    context = for_behavior_profile(db, p, now, judged)
                else:
                    from app.services.profile_context import unverified
                    context = unverified("构成快照不是当日数据，不能用当前资源上下文替代事发窗口证据")
            if drifted and context["verdict"] == STABLE:
                # 保留原始变化和稳定证据；只收口对应日漂移，不影响其它独立异常。
                p.status = "context_stable"
                p.notify_state = ""
                p.drift_note = ("\n".join(notes) + "\n" + context["note"])[:4000]
                stats["context_stable"] = stats.get("context_stable", 0) + 1
                for old_ev in db.query(CostAnomaly).filter(
                        CostAnomaly.type == "profile_drift",
                        CostAnomaly.target_key == p.profile_key,
                        CostAnomaly.ack_status.in_(("open", "acknowledged"))).all():
                    old_ev.ack_status = "resolved"
                    old_ev.ack_note = (old_ev.ack_note or "") + "\n[构成仍变化，但整机/后端上下文稳定，本次不告警]"
            elif drifted and _is_gentle(notes) and context["verdict"] != UNVERIFIED:
                # 温和漂移持续稳定后自动学成新常态。安全阀：只自动学习占比类温和漂移；
                # top1 更替/陌生成员属于架构级变化，必须人工复核，防止把异常学成正常。
                streak = int(baseline.get("gentle_streak") or 0) + 1
                if streak >= 7:
                    new_base = learn_baseline(samples)
                    new_base["auto_normal_at"] = today.isoformat()
                    new_base["auto_normal_note"] = (
                        f"占比类温和漂移连续 {streak} 天维持，自动学成新常态")
                    p.baseline = json.dumps(new_base, ensure_ascii=False)
                    if p.status != "known":   # 已知道状态不被自动学习覆盖
                        p.status = "normal"
                    p.drift_note = ""
                    for old_ev in db.query(CostAnomaly).filter(
                            CostAnomaly.type == "profile_drift",
                            CostAnomaly.target_key == p.profile_key,
                            CostAnomaly.ack_status.in_(("open", "acknowledged"))).all():
                        old_ev.ack_status = "resolved"
                        old_ev.ack_note = (old_ev.ack_note or "") + "\n[温和漂移持续 7 天，自动学成新常态]"
                else:
                    baseline["gentle_streak"] = streak
                    p.baseline = json.dumps(baseline, ensure_ascii=False)
                    if p.status != "known":
                        p.status = "drift"
                    p.drift_note = "\n".join(notes) + f"\n（温和漂移观察中 {streak}/7 天，持续则自动学成新常态）"
                continue
            elif drifted:
                if baseline.pop("gentle_streak", None) is not None:
                    p.baseline = json.dumps(baseline, ensure_ascii=False)
                unverified = context["verdict"] == UNVERIFIED
                if unverified:
                    level = "warning"
                    stats["unverified"] = stats.get("unverified", 0) + 1
                p.drift_note = ("\n".join(notes)
                                + (("\n" + context["note"]) if unverified else ""))[:4000]
                finding_title = (f"行为画像变化待核实: {p.label}" if unverified
                                 else f"行为画像漂移: {p.label}")
                notes_for_event = notes + ([context["note"]] if unverified else [])
                # ---- 已知道生命周期：静默期内只记不发；到期漂移仍在则恢复提醒 ----
                if p.status == "known":
                    if p.silence_until is not None and now < p.silence_until:
                        p.notify_state = "silenced"   # 静默期：通知抑制（不生成/不刷新事件）
                    else:
                        p.notify_state = "remind" if p.silence_until is not None else ""   # 到期恢复/aux 持续通知
                        a = _add(
                            db, today, "profile_drift", p.profile_key, p.label,
                            finding_title,
                            (f"「{p.label}」的构成与正常画像不符（这比'数值超限'更值得关注：构成变了=里面发生了变化）\n\n"
                             f"- 变化：\n" + "\n".join(f"  - {n}" for n in notes_for_event) +
                             f"\n- 正常画像：{baseline.get('top1', '?')} 领衔（{len(baseline.get('members', []))} 个常态成员）"
                             + (f"\n- 提醒：用户已知道过此变化（{p.known_at:%m-%d %H:%M} 确认），静默期已到，漂移仍在故恢复提醒"
                                if p.known_at else "")
                             + f"\n- 处理：确认是正常业务（新版本/新业务）就点'接受当前构成为正常'；仍需观察点'知道了'再静默"),
                            value=judged["members"][0]["share_pct"] if judged.get("members") else 0,
                            baseline=baseline.get("members", [{}])[0].get("share_avg", 0) if baseline.get("members") else 0,
                            ratio=0, level=level,
                        )
                        stats["drifted"] += 1
                else:
                    p.status = "unverified" if unverified else "drift"
                    p.notify_state = ""
                    a = _add(
                        db, today, "profile_drift", p.profile_key, p.label,
                        finding_title,
                        (f"「{p.label}」的构成与正常画像不符（这比'数值超限'更值得关注：构成变了=里面发生了变化）\n\n"
                         f"- 变化：\n" + "\n".join(f"  - {n}" for n in notes_for_event) +
                         f"\n- 正常画像：{baseline.get('top1', '?')} 领衔（{len(baseline.get('members', []))} 个常态成员）\n"
                         f"- 处理：确认是正常业务（新版本/新业务）就点'接受当前构成为正常'；否则排查是谁进来了"),
                        value=judged["members"][0]["share_pct"] if judged.get("members") else 0,
                        baseline=baseline.get("members", [{}])[0].get("share_avg", 0) if baseline.get("members") else 0,
                        ratio=0, level=level,
                    )
                    stats["drifted"] += 1   # 按检出计数（刷新既有事件时 _add 返回 None 也不漏）
            else:
                if p.status == "known":
                    # 变化消失 -> 回 normal（「知道」的是那次变化；known_at 保留为历史）
                    p.silence_until = None
                    p.notify_state = ""
                p.status = "normal"
                p.drift_note = ""
        else:
            if p.status != "known":
                p.status = "observing"

        # ---- 快层检测：事件样本短时构成突变（1 小时内 top1 换人） ----
        if detect_short_mutation(db, p, now):
            stats["mutations"] += 1
    db.commit()
    return stats


# ---------------- 关联事件/变更时间线 ----------------

def related_events_timeline(db: Session, keys: list, days: int = 7) -> list:
    """对象近 N 天的告警/异动/变更时间线，时间/系统/操作/人显式成条。

    命中规则：keys 里的标识（画像 key/subject，忽略大小写）出现在事件的
    target/target_name/target_key/title/resource/summary 中。返回按时间倒序。

    每条带跳转标识：id（告警/异动/变更行 id）、anomaly_type（异动类型，供
    /anomalies?anomaly=<id> 定位）；linked=True 表示该条落在本对象画像漂移事件
    （含 #fast）±24h 时间窗内，供画像详情时间线标注与跳转。
    """
    from app.models.alert_record import AlertRecord
    from app.models.change_event import ChangeEvent

    keys = [str(k).strip().lower() for k in (keys or []) if k and str(k).strip() and str(k).strip() != "*"]
    if not keys:
        return []
    since = datetime.now() - timedelta(days=days)

    def _hit(text: str) -> bool:
        low = (text or "").lower()
        return any(k in low for k in keys)

    # 本对象画像漂移事件时刻是 ±24h 关联窗的锚点。
    key_set = set(keys)
    drift_ts: list = []
    for e in (db.query(CostAnomaly)
              .filter(CostAnomaly.type == "profile_drift", CostAnomaly.created_at >= since)
              .order_by(CostAnomaly.id.desc()).limit(200).all()):
        if (e.target_key or "").lower().removesuffix("#fast") in key_set and e.created_at:
            drift_ts.append(e.created_at)

    def _linked(ts_iso: str) -> bool:
        try:
            t = datetime.fromisoformat(ts_iso)
        except (TypeError, ValueError):
            return False
        return any(abs((t - d).total_seconds()) <= 24 * 3600 for d in drift_ts)

    out: list = []
    for r in (db.query(AlertRecord).filter(AlertRecord.fired_at >= since)
              .order_by(AlertRecord.id.desc()).limit(200).all()):
        if _hit(f"{r.target} {r.target_name} {r.title}"):
            out.append({"ts": r.fired_at.isoformat(), "kind": "alert",
                        "title": f"[{r.level}] {r.rule_name} · {r.target_name or r.target}",
                        "detail": (r.content or "")[:200], "level": r.level,
                        "status": r.status, "user": "", "service": "",
                        "id": r.id, "anomaly_type": "",
                        "linked": _linked(r.fired_at.isoformat())})
    for a in (db.query(CostAnomaly).filter(CostAnomaly.created_at >= since)
              .order_by(CostAnomaly.id.desc()).limit(200).all()):
        if _hit(f"{a.target_key} {a.target_name} {a.title}"):
            out.append({"ts": a.created_at.isoformat(), "kind": "anomaly",
                        "title": f"[{a.level}] {a.title}",
                        "detail": (a.content or "")[:200], "level": a.level,
                        "status": a.ack_status, "user": a.ack_by or "", "service": "",
                        "id": a.id, "anomaly_type": a.type,
                        "linked": _linked(a.created_at.isoformat())})
    for c in (db.query(ChangeEvent).filter(ChangeEvent.event_time >= since)
              .order_by(ChangeEvent.id.desc()).limit(200).all()):
        if _hit(f"{c.resource} {c.summary} {c.event_name}"):
            out.append({"ts": c.event_time.isoformat(), "kind": "change",
                        "title": f"{c.event_name} {c.resource}",
                        "detail": (c.summary or "")[:200], "level": "",
                        "status": "error" if c.error_code else "ok",
                        "user": c.user or "", "service": c.service or "",
                        "id": c.id, "anomaly_type": "",
                        "linked": _linked(c.event_time.isoformat())})
    out.sort(key=lambda x: x["ts"], reverse=True)
    return out[:50]


def drift_change_windows(db: Session, days: int = 7, window_hours: int = 24,
                         limit: int = 6) -> list:
    """画像漂移 × 变更时间窗关联：每条画像漂移事件（含 #fast 短时突变）
    列出其时间窗内的云上变更（时间/系统/操作/人），供诊断报告【关联变更】
    证据块成文——"漂移前后改了什么"直接成对照，窗口内无变更也是证据（排除变更致因）。

    时间窗 = [检出时刻 - window_hours, 检出时刻 + 1h]：漂移由采样察觉（慢层每天
    2~6 次），致因变更通常在察觉之前，24h 回看覆盖一个采样周期；+1h 容纳同时段处置动作。
    """
    from app.models.change_event import ChangeEvent

    since = datetime.now() - timedelta(days=days)
    events = (db.query(CostAnomaly)
              .filter(CostAnomaly.type == "profile_drift", CostAnomaly.created_at >= since)
              .order_by(CostAnomaly.created_at.desc()).limit(limit).all())
    out = []
    for e in events:
        t0 = e.created_at - timedelta(hours=window_hours)
        t1 = e.created_at + timedelta(hours=1)
        rows = (db.query(ChangeEvent)
                .filter(ChangeEvent.event_time >= t0, ChangeEvent.event_time <= t1)
                .order_by(ChangeEvent.event_time.desc()).limit(10).all())
        notes = [ln.strip()[2:].strip() for ln in (e.content or "").splitlines()
                 if ln.startswith("  - ")][:2]
        out.append({
            "ts": e.created_at.isoformat(), "key": e.target_key, "title": e.title,
            "level": e.level, "status": e.ack_status, "notes": notes,
            "changes": [{"ts": c.event_time.isoformat(), "service": c.service or "",
                         "event_name": c.event_name, "user": c.user or "",
                         "resource": c.resource or ""} for c in rows],
        })
    return out


# ---------------- 慢层多时刻调度辅助 ----------------

def sample_hours() -> list:
    """慢层采样时刻点：PROFILE_SAMPLE_HOURS（逗号分隔小时 0~23，默认 2,8,14,20）。"""
    try:
        hs = sorted({int(x) for x in str(settings.PROFILE_SAMPLE_HOURS).replace("，", ",").split(",")
                     if x.strip() and 0 <= int(x) <= 23})
    except (TypeError, ValueError):
        hs = []
    return hs or [2, 8, 14, 20]


def seconds_until_next_sample(now: Optional[datetime] = None) -> float:
    """距离下一个采样时刻的秒数（供 app/main.py 的采样循环调度）。"""
    now = now or datetime.now()
    for h in sample_hours():
        t = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if t > now:
            return (t - now).total_seconds()
    t = (now + timedelta(days=1)).replace(hour=sample_hours()[0], minute=0, second=0, microsecond=0)
    return (t - now).total_seconds()


def accept_profile(db: Session, profile_key: str, by: str, by_id: int = 0) -> dict:
    """人工确认：把当前看到的构成记为新常态，并关闭未处理漂移事件。

    这里记录当前构成，不重学历史众数；只有明确接受当前状态才改变基线。
    """
    p = db.query(BehaviorProfile).filter(BehaviorProfile.profile_key == profile_key).first()
    if p is None:
        raise ValueError(f"画像不存在: {profile_key}")
    samples = json.loads(p.samples or "[]")
    if not samples or not samples[-1].get("members"):
        raise ValueError("暂无样本，无法建立基线")
    latest = samples[-1]
    base = {
        "members": [{"name": m["name"], "share_avg": m["share_pct"],
                     "share_min": m["share_pct"], "share_max": m["share_pct"], "freq": 1.0}
                    for m in latest["members"]],
        "top1": latest["members"][0]["name"],
        "top1_freq": 1.0,
        "n": len(samples),
        "learned_at": date.today().isoformat(),
        "accepted_from": latest.get("date", ""),
    }
    p.baseline = json.dumps(base, ensure_ascii=False)
    p.learned = True
    p.status = "normal"
    p.drift_note = ""
    p.confirmed_by = by
    p.confirmed_by_id = by_id or 0   # 同时保留稳定的用户 ID 引用
    p.confirmed_at = datetime.now()
    # 记为正常表示变化已吸收：静默态一并收口，事件按正常语义关闭。
    p.silence_until = None
    p.notify_state = ""
    n = close_drift_events(db, profile_key, f"已接受当前构成为新常态（by {by}）")
    db.commit()
    return {"profile_key": profile_key, "top1": base.get("top1"), "closed_events": n}
