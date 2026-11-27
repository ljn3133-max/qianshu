"""周期基线检测任务。

- traffic_spike：CDN/DCDN 按域名日流量 vs 同日型基线（cdn_domain_daily）
- growth_accel：磁盘/空间"日增速"突变（ECS 用 VM diskusage_utilization 每日峰值，
  RDS 用 dba_space_daily.disk_used）——水平单调涨没意义，比的是每天涨多少

成本的 payg_spike 已在 cost_anomaly.detect_cost_for_date 内换用同一引擎。
产出复用 cost_anomalies 生命周期（_add upsert / 3 日未复现自动关闭 / ack 分级）。
"""
from __future__ import annotations

import logging
import time
from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.collector.vm import query_range as vm_query_range
from app.core.config import settings
from app.models.cdn_domain_daily import CdnDomainDaily
from app.models.dba_space import DbaSpaceDaily
from app.services.baseline import V_SPIKE, day_series_check, growth_check
from app.models.cost_anomaly import CostAnomaly
from app.services.cost_anomaly import _add
from app.services.holiday import day_kind

logger = logging.getLogger("qianshu.baseline_job")


def detect_traffic_spike(db: Session, today: date) -> list:
    """CDN 域名流量 vs 同日型基线（今天的数据=昨日已采；today 取"有数据的最新日"）。"""
    from sqlalchemy import func
    latest_day = db.query(func.max(CdnDomainDaily.stat_date)).scalar()
    if latest_day is None:
        return []
    today = latest_day
    rows = db.query(CdnDomainDaily).filter(
        CdnDomainDaily.stat_date >= today - timedelta(days=settings.BASELINE_WINDOW_DAYS),
        CdnDomainDaily.stat_date <= today,
    ).all()
    by_domain: dict = {}
    for r in rows:
        gb = (r.traffic_bytes or 0) / 1024 ** 3
        by_domain.setdefault(r.domain, []).append((r.stat_date, gb))

    found: list = []
    for domain, pairs in by_domain.items():
        pairs.sort()
        today_gb = dict(pairs).get(today)
        if today_gb is None:
            continue
        hist = [(d, v) for d, v in pairs if d < today]
        chk = day_series_check(
            hist, today, today_gb, lambda d: day_kind(db, d),
            unit="GB", abs_min=settings.BASELINE_MIN_TRAFFIC_GB,
        )
        if chk["verdict"] != V_SPIKE:
            continue
        notes = "\n".join(f"- {n}" for n in chk.get("notes", []))
        a = _add(
            db, today, "traffic_spike", f"cdn:{domain}", domain,
            f"CDN 流量突增: {domain} {today_gb:,.1f}GB",
            (f"CDN/DCDN 域名流量超同类日基线 {today.isoformat()}（周期基线判定）\n\n"
             f"- 域名: {domain}\n"
             f"- 当日: {today_gb:,.1f} GB\n"
             f"- 同类日基线: {chk['baseline']:,.1f} GB（MAD ±{chk['mad']:,.1f}）\n"
             f"- 为基线的 {chk['ratio']:.1f} 倍\n"
             f"{notes}\n"
             f"- 看哪里涨：成本页「域名消耗」页签；排查用一键诊断（cost_analysis 场景）"),
            value=round(today_gb, 2), baseline=chk["baseline"], ratio=chk["ratio"],
            level=chk["level"] or "warning",
        )
        if a:
            found.append(a)
    return found


def detect_growth_accel(db: Session, today: date) -> list:
    """磁盘/空间日增速突变。ECS（VM 每日峰值）+ RDS（空间日快照）。"""
    found: list = []
    # ---- ECS 磁盘使用率（%）：按实例取各分区最满，日增速单位=百分点 ----
    try:
        end_s = int(time.time())
        start_s = end_s - (settings.BASELINE_GROWTH_WINDOW + 3) * 86400
        prom = ("max by (instance_id) ("
                "max_over_time(aliyun_ecs_diskusage_utilization[1d]))")
        result = vm_query_range(prom, start_s, end_s, 86400)
    except Exception as e:
        logger.warning("ECS 磁盘增速查询失败: %s", e)
        result = []
    from app.models.asset import Asset

    names = {a.instance_id: (a.name or a.instance_id)
             for a in db.query(Asset).filter(Asset.product == "ecs").all()}
    # aux 辅助机豁免资源类监测（D_exemption_layers：Sentry 这类机器只看关机+5xx 外溢）
    aux_ids = {a.instance_id for a in db.query(Asset).filter(
        Asset.product == "ecs", Asset.tier == "aux").all()}
    for series in result:
        iid = (series.get("metric") or {}).get("instance_id", "")
        if not iid or iid in aux_ids:
            continue
        pairs = []
        for ts, v in series.get("values", []):
            try:
                pairs.append((date.fromtimestamp(int(float(ts))), float(v)))
            except (ValueError, TypeError, OSError):
                continue
        chk = growth_check(pairs, unit="%", min_delta=settings.BASELINE_GROWTH_MIN_DELTA)
        if chk["verdict"] != V_SPIKE:
            continue
        # 当前值与日峰值必须分开取数：
        # 当前值必须单独取实测（原始序列最新时刻、各分区最满）——日峰值序列的
        # 最后一点仍是"24h 峰值"（含扩容前的高点），标成"当前"就是误导
        cur_val = pairs[-1][1] if pairs else 0.0
        try:
            raw = vm_query_range(
                f'aliyun_ecs_diskusage_utilization{{instance_id="{iid}"}}',
                end_s - 1800, end_s, 300)
            inst_vals = [float(v) for sr in raw for _, v in sr.get("values", [])]
            if inst_vals:
                cur_val = max(inst_vals)   # 当前最满分区的实测值
        except Exception:
            pass
        peak_val = chk.get("value", cur_val)
        # 变更关联：近 48h 同资源的云上操作（如 ResizeDisk）自动附注——异常要和"最近改了什么"串起来
        change_note = ""
        try:
            from app.models.change_event import ChangeEvent
            from sqlalchemy import or_ as _or
            from datetime import datetime as _dt, timedelta as _td
            import json as _json
            # 扩容事件的资源是磁盘 ID（d-xxx），须经资产明细映射到实例
            disk_ids = set()
            try:
                extra = _json.loads((db.query(Asset).filter(Asset.instance_id == iid).first().extra) or "{}")
                for d in (extra.get("disks") or extra.get("disk") or []):
                    did = d.get("disk_id") or d.get("DiskId") or d.get("id")
                    if did:
                        disk_ids.add(str(did))
            except Exception:
                pass
            conds = [ChangeEvent.summary.like(f"%{names.get(iid, iid)}%")]
            if disk_ids:
                conds.append(ChangeEvent.resource.in_(list(disk_ids)[:20]))
            ch_rows = (db.query(ChangeEvent)
                       .filter(ChangeEvent.event_time >= _dt.now() - _td(hours=48))
                       .filter(_or(*conds))
                       .order_by(ChangeEvent.event_time.desc()).limit(3).all())
            if ch_rows:
                change_note = ("\n- 变更关联（近 48h 有操作，先看是不是处置动作）：\n"
                               + "\n".join(f"  - {c.event_time:%m-%d %H:%M} {c.event_name} {c.resource}"
                                            for c in ch_rows))
        except Exception:
            change_note = ""
        notes = "\n".join(f"- {n}" for n in chk.get("notes", []))
        a = _add(
            db, today, "growth_accel", f"ecs_disk:{iid}", names.get(iid, iid),
            f"磁盘增速突变: {names.get(iid, iid)} +{chk['growth']:.1f}%/天",
            (f"磁盘使用率日增速突变（周期基线判定）\n\n"
             f"- 实例: {names.get(iid, iid)}（{iid}）\n"
             f"- 当前使用率: {cur_val:.1f}%（近 24h 峰值 {peak_val:.1f}%——峰值含扩容/处置前状态）\n"
             f"- 今日增长: +{chk['growth']:.1f} 个百分点/天\n"
             f"- 日常增速: +{chk['baseline_growth']:.2f} 个百分点/天\n"
             f"{notes}{change_note}\n"
             f"- 排查：是否日志/临时文件暴涨；可用一键诊断定位"),
            value=chk["growth"], baseline=chk["baseline_growth"],
            ratio=chk.get("ratio", 0), level=chk["level"] or "warning",
        )
        if a:
            found.append(a)

    # ---- RDS 空间：用 data_size（稳定增长主体）判增速，不用 disk_used ----
    # RDS 空间增速以数据文件为口径：
    # disk_used 含备份/日志轮转摆动（实测日摆 std 18GB vs 净增长 0.57GB/天），
    # 用它算日增速会周期性误报；backup_size 是备份集不占实例盘。写满风险看 data 分量。
    rows = db.query(DbaSpaceDaily).filter(
        DbaSpaceDaily.stat_date >= today - timedelta(days=settings.BASELINE_GROWTH_WINDOW + 3),
    ).order_by(DbaSpaceDaily.stat_date).all()
    by_inst: dict = {}
    used_by_inst: dict = {}
    for r in rows:
        gb = (r.data_size or 0) / 1024 ** 3
        by_inst.setdefault(r.instance_id, []).append((r.stat_date, gb))
        used_by_inst.setdefault(r.instance_id, []).append((r.disk_used or 0) / 1024 ** 3)
    for iid, pairs in by_inst.items():
        chk = growth_check(pairs, unit="GB", min_delta=settings.BASELINE_MIN_SPACE_GB)
        if chk["verdict"] != V_SPIKE:
            continue
        notes = "\n".join(f"- {n}" for n in chk.get("notes", []))
        used_pairs = used_by_inst.get(iid, [])
        swing = ""
        if len(used_pairs) >= 2:
            used_deltas = [used_pairs[i] - used_pairs[i - 1] for i in range(1, len(used_pairs))]
            big = max(used_deltas, key=abs)
            if abs(big) > abs(chk["growth"]) * 2:
                swing = (f"- 磁盘总量日摆动最大 {big:+.1f} GB（备份/日志轮转波动，"
                         f"非增长主体，不要按波动处置）\n")
        a = _add(
            db, today, "growth_accel", f"rds_space:{iid}", iid,
            f"RDS 数据文件增速突变: {iid} +{chk['growth']:.2f}GB/天",
            (f"RDS 数据文件日增速突变（周期基线判定；口径=数据文件，排除备份/日志轮转波动）\n\n"
             f"- 实例: {iid}\n"
             f"- 当前数据文件: {chk.get('value', 0):.2f} GB（日快照口径，每日 09:40 采集）\n"
             f"- 今日增速: +{chk['growth']:.2f} GB/天（数据文件）\n"
             f"- 日常增速: +{chk['baseline_growth']:.2f} GB/天\n"
             f"{swing}{notes}\n"
             f"- 排查：data 真涨才看大表膨胀（DBA 页空间嫌疑）；波动不用管"),
            value=chk["growth"], baseline=chk["baseline_growth"],
            ratio=chk.get("ratio", 0), level=chk["level"] or "warning",
        )
        if a:
            found.append(a)
    return found


def _close_unreproduced(db: Session, today: date, types: tuple, new_keys: set) -> int:
    """变化型事件（增速/流量）未复现即收口。

    这类事件的语义是"正在发生的变化"，变化停止后事件就应结束，不能继续展示旧数字
    冒充现状。区别于持续型事件（池将耗尽），后者仍适用 3 日未复现关闭规则。
    """
    from datetime import datetime as _dt
    stale = (db.query(CostAnomaly)
             .filter(CostAnomaly.type.in_(types),
                     CostAnomaly.ack_status.in_(("open", "acknowledged")),
                     CostAnomaly.last_seen < today))
    closed = 0
    for a in stale.all():
        if a.target_key in new_keys:
            continue   # 当前运行中刚触发的事件不收口
        a.ack_status = "resolved"
        a.ack_note = (a.ack_note or "") + f"\n[变化已回落：自 {a.last_seen} 后未再检出，自动收口]"
        closed += 1
    return closed


def run_baseline_checks(db: Session) -> dict:
    """每日一次：流量突增 + 增速突变。返回统计；调用方负责 commit/通知。"""
    today = date.today()
    traffic = detect_traffic_spike(db, today)
    growth = detect_growth_accel(db, today)
    # 未复现的变化型事件收口，当前运行新检出的 target 不动
    new_keys = {("traffic_spike", a.target_key) for a in traffic} | {
        ("growth_accel", a.target_key) for a in growth}
    closed = _close_unreproduced(
        db, today, ("traffic_spike", "growth_accel"),
        {k for t, k in new_keys})
    return {"traffic": len(traffic), "growth": len(growth), "auto_closed": closed}
    result = {"traffic": len(traffic), "growth": len(growth)}
    logger.info("基线检测完成: 流量 %d 条 / 增速 %d 条", result["traffic"], result["growth"])
    return result
