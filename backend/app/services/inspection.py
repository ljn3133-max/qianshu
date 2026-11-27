"""巡检流水线：纯规则扫描，产出事实清单落库。

五个扫描器（互不阻断，单个失败记入 run.error）：
- cost  成本异动：读成本检测最新检测日异动 + 昨日成本合计（info）
- health 资源健康：VM 近 24h 峰值使用率 vs 阈值（80/90）
- slb   SLB 摘要：QPS/RT 概览（读 VM；5xx 走日志扫描器的 SLS 精确计数）
- logs  日志摘要：SLS 只读统计（外层 SLB / 内层 SLB 5xx 按实例分组 + Top URI / 应用错误数）
- idle  闲置识别：停止的 ECS / 7 天低 CPU / 无流量 SLB / 即将到期资产
- idle  闲置识别：停止的 ECS / 7 天低 CPU / 无流量 SLB / 即将到期资产

日报模块在此基础上让 LLM 汇总成日报。本模块不做任何通知、不做任何写云操作。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.collector.sls import query_logs as sls_query_logs
from app.collector.vm import query_range as vm_query_range
from app.core.config import settings
from app.models.asset import Asset
from app.models.cost_anomaly import CostAnomaly
from app.models.cost_daily import CostDaily
from app.models.inspection import InspectionItem, InspectionRun
from app.models.resource_pack_snapshot import ResourcePackSnapshot
from app.services.pack_impact import impact_analysis, pool_active

logger = logging.getLogger("qianshu.inspection")

_lock = threading.Lock()          # 与手动触发互斥
_last_status: dict = {"running": False, "last_run_at": None, "last_result": None}

# health 扫描的指标集（与 METRIC_CONFIG 保持一致的序列名）
_HEALTH_METRICS = [
    # (product, cms_metric, 中文名, 是否百分比阈值型)
    ("ecs", "CPUUtilization", "CPU使用率", True),
    ("ecs", "memory_usedutilization", "内存使用率", True),
    ("ecs", "diskusage_utilization", "磁盘使用率", True),  # 多分区序列，_vm_last_values 取最满
    ("rds", "CpuUsage", "CPU使用率", True),
    ("rds", "MemoryUsage", "内存使用率", True),
    ("rds", "DiskUsage", "磁盘使用率", True),
    ("rds", "ConnectionUsage", "连接数使用率", True),
    ("rds", "IOPSUsage", "IOPS使用率", True),
    ("redis", "ShardingCpuUsage", "CPU使用率", True),
    ("redis", "ShardingMemoryUsage", "内存使用率", True),
    ("redis", "ShardingConnectionUsage", "连接数使用率", True),
]

# SLS 日志摘要的目标（只读统计，不写）。千枢产品态：默认空，安装向导/环境变量
# SLS_INSPECTION_TARGETS 注入；未配置=巡检不做日志摘要（其余巡检项照常）
def _sls_targets() -> list:
    try:
        import json
        raw = json.loads(settings.SLS_INSPECTION_TARGETS)
        return [tuple(x) for x in raw]
    except Exception:
        logger.warning("SLS_INSPECTION_TARGETS 配置解析失败，视为未配置")
        return []



def get_status() -> dict:
    return dict(_last_status)


# ---------------- 基础小工具 ----------------

def _item(category: str, level: str, title: str, detail: str = "",
          target_key: str = "", target_name: str = "",
          value: float = None, unit: str = "") -> dict:
    return {
        "category": category, "level": level, "title": title, "detail": detail,
        "target_key": target_key, "target_name": target_name,
        "value": value, "unit": unit,
    }


def _vm_last_values(promql: str) -> dict:
    """max/avg_over_time 类查询 -> {instance_id: 最近一个值}。

    同一实例可能有多条序列（Redis 集群分片 / ECS 磁盘分区，node 标签区分），
    取最大值——不能"后写覆盖"，否则磁盘只评到碰巧排最后的分区。
    """
    now_s = int(time.time())
    result = vm_query_range(promql, now_s - 3600, now_s, 1800)
    out: dict = {}   # {instance_id: (value, 分区名)}
    for r in result:
        try:
            val = float(r["values"][-1][1])
        except (KeyError, IndexError, ValueError, TypeError):
            continue
        iid = (r.get("metric") or {}).get("instance_id", "")
        part = (r.get("metric") or {}).get("node", "")
        if iid:
            cur = out.get(iid)
            if cur is None or val > cur[0]:
                out[iid] = (val, part)   # 记住最满的那条序列是哪个分区
    return out


def _name_map(db: Session, product: str) -> dict:
    return {
        a.instance_id: (a.name or a.instance_id)
        for a in db.query(Asset).filter(Asset.product == product).all()
    }


# ---------------- 扫描器 1：成本异动 ----------------

def _scan_cost(db: Session) -> list:
    items: list = []
    # 昨日（或库内最新一天）成本合计
    latest_day = db.query(func.max(CostDaily.stat_date)).scalar()
    if latest_day:
        # pretax_amount（账单金额）是判断花费的唯一口径
        total = db.query(func.sum(CostDaily.pretax_amount)).filter(
            CostDaily.stat_date == latest_day,
        ).scalar() or 0.0
        items.append(_item(
            "cost", "info",
            f"{latest_day.isoformat()} 成本合计 {_money(total)}（账单金额）",
            f"数据口径 {latest_day.isoformat()} 日成本（T+1，成本采集每日 {settings.COST_COLLECT_HOUR}:00）。"
            f"金额=账单金额（pretax_amount，判断花费以此为准）。",
            target_key="__total__", value=round(float(total), 2), unit="元",
        ))
    # 巡检只报"待处理"（open 且近 7 日仍复现）的异动，
    # 已确认/忽略/自动关闭的不进巡检；刚耗尽的包附带转按量影响（带钱的告警）。
    cutoff = date.today() - timedelta(days=7)
    rows = (
        db.query(CostAnomaly)
        .filter(CostAnomaly.ack_status == "open", CostAnomaly.last_seen >= cutoff)
        .order_by(CostAnomaly.last_seen.desc())
        .limit(20)
        .all()
    )
    _lvl = {"critical": 0, "warning": 1, "info": 2}
    rows.sort(key=lambda a: (_lvl.get(a.level, 3), -(a.value or 0)))
    if rows:
        for a in rows:
            detail = a.content
            if a.type == "pack_exhausted":
                # 池级守卫（D_pool_level）：活池里的死包是轮转，不报转按量影响
                if pool_active(db, a.target_key, _pack_products(db, a.target_key)):
                    detail += "\n- 历史包耗尽属正常轮转（池内仍有额度），无转按量影响"
                else:
                    imp = impact_analysis(db, a.target_key, _pack_products(db, a.target_key))
                    # extra_daily 使用账单金额口径计算日均多花
                    if imp.get("available") and imp.get("extra_daily", 0) > 0:
                        detail += (f"\n- 转按量影响（实测）: 日均 +¥{imp['extra_daily']:,.2f}"
                                   f"，月度约 +¥{imp['extra_monthly']:,.0f}")
            items.append(_item(
                "cost", a.level, f"[{a.last_seen.isoformat() if a.last_seen else ''}] {a.title}",
                detail, target_key=a.target_key, target_name=a.target_name,
                value=a.value or None, unit="",
            ))
    else:
        items.append(_item("cost", "info", "无待处理成本异动",
                            "近 7 日无异动待处理（检测每日 10:00 随成本采集运行；"
                            "已确认/已忽略/已自动关闭的异动不在此列出）。"))
    return items


def _pack_products(db: Session, instance_id: str) -> str:
    """取某资源包的抵扣范围串（巡检影响量化用；找不到返回空串）。"""
    s = (
        db.query(ResourcePackSnapshot)
        .filter(ResourcePackSnapshot.instance_id == instance_id)
        .order_by(ResourcePackSnapshot.snapshot_date.desc())
        .first()
    )
    return s.applicable_products if s else ""


def _money(v: float) -> str:
    return f"¥{v:,.2f}"


# ---------------- 扫描器 2：资源健康 ----------------

def _scan_health(db: Session) -> list:
    items: list = []
    warn, crit = settings.INSPECTION_HEALTH_WARN, settings.INSPECTION_HEALTH_CRIT
    series_checked = 0
    for product, cms_metric, label, is_pct in _HEALTH_METRICS:
        series = f"aliyun_{product}_{cms_metric.lower()}"
        try:
            vals = _vm_last_values(f"max_over_time({series}[24h])")
        except Exception as e:
            logger.warning("health 查询失败 %s: %s", series, e)
            continue
        if not vals:
            continue
        series_checked += len(vals)
        names = _name_map(db, product)
        breached = []
        for iid, (v, part) in vals.items():
            if v >= crit:
                lv = "critical"
            elif v >= warn:
                lv = "warning"
            else:
                continue
            breached.append((iid, v, part, lv))
        if breached:
            breached.sort(key=lambda x: -x[1])
            for iid, v, part, lv in breached:
                items.append(_item(
                    "health", lv, f"{product.upper()} {names.get(iid, iid)} {label} 24h峰值 {v:.1f}%"
                                  + (f"（{part}）" if part else ""),
                    f"实例 {iid}，{label}近24小时峰值 {v:.1f}%（预警阈值 {warn}%，严重阈值 {crit}%）。",
                    target_key=iid, target_name=names.get(iid, ""), value=round(v, 2), unit="%",
                ))
    if not items:
        items.append(_item("health", "info", "资源健康：全部检测指标 24h 峰值均在阈值内",
                           f"检测 ECS/RDS/Redis 共 {series_checked} 条指标序列，无超 {warn}% 预警阈值的实例。"))
    return items


# ---------------- 扫描器 3：SLB 摘要 ----------------

def _scan_slb(db: Session) -> list:
    items: list = []
    names = _name_map(db, "slb")
    try:
        qps = _vm_last_values("avg_over_time(aliyun_slb_instanceqps[24h])")
        rt = _vm_last_values("avg_over_time(aliyun_slb_instancert[24h])")
    except Exception as e:
        logger.warning("slb 查询失败: %s", e)
        return items
    if not qps and not rt:
        return items

    # _vm_last_values 返回 (值, 分区) 元组，必须先解包再求和，
    # 否则会报 "int + tuple" 并导致整个 SLB 扫描器失败
    qps_v = {k: v[0] for k, v in qps.items()}
    rt_v = {k: v[0] for k, v in rt.items()}
    total_qps = sum(qps_v.values())
    top_qps = sorted(qps_v.items(), key=lambda x: -x[1])[:3]
    top_rt = sorted(rt_v.items(), key=lambda x: -x[1])[:3]
    detail_lines = [
        f"- 总 QPS（24h 均值）: {total_qps:.1f}",
        f"- QPS Top3: " + ", ".join(f"{names.get(k, k)}={v:.1f}" for k, v in top_qps),
        f"- 平均 RT Top3: " + ", ".join(f"{names.get(k, k)}={v:.0f}ms" for k, v in top_rt),
    ]
    items.append(_item(
        "slb", "info",
        (f"SLB 概览：{len(qps)} 个实例，总 QPS {total_qps:.1f}，平均 RT 最高 {top_rt[0][1]:.0f}ms"
         if top_rt else f"SLB 概览：{len(qps)} 个实例"),
        "\n".join(detail_lines), target_key="__summary__", value=round(total_qps, 2), unit="QPS",
    ))
    return items


# ---------------- 扫描器 4：日志摘要 ----------------

def _scan_logs(db: Session) -> list:
    from app.collector.credential import resolve_sls_credential

    items: list = []
    ak, sk, _src = resolve_sls_credential(db)
    end_s = int(time.time())
    start_s = end_s - 86400
    for project, logstore, label, cond, what in _sls_targets():
        try:
            r = sls_query_logs(ak, sk, project, logstore,
                              f"{cond} | select count(*) as cnt",
                              start=start_s, end=end_s, limit=1)
            cnt = _first_number(r["logs"])
            if cnt is None:
                continue
        except Exception as e:
            logger.warning("日志摘要查询失败 %s/%s: %s", project, logstore, e)
            continue
        if cnt <= 0:
            items.append(_item("logs", "info", f"{label}：近24h 无{what}",
                               f"{project}/{logstore} 近24小时{what} 0 条。"))
            continue
        detail = f"{project}/{logstore} 近24小时{what} {cnt:.0f} 条。"
        # SLB 日志带 slbid/request_uri 维度：给每个 SLB 实例出一条事实 + Top URI
        if "SLB" in label:
            names = {a.instance_id: (a.name or a.instance_id)
                     for a in db.query(Asset).filter(Asset.product == "slb").all()}
            by_slb: list = []
            try:
                by_slb = sls_query_logs(ak, sk, project, logstore,
                                       f"{cond} | select slbid, count(*) as cnt "
                                       f"group by slbid order by cnt desc limit 10",
                                       start=start_s, end=end_s, limit=10)["logs"]
            except Exception as e:
                logger.warning("日志按SLB分组查询失败 %s: %s", logstore, e)
            for row in by_slb:
                slbid = str(row.get("slbid", ""))
                n = _first_number([row])
                if not slbid or n is None:
                    continue
                detail += f"\n- {names.get(slbid, slbid)}: {n:.0f} 条"
                items.append(_item(
                    "logs", "warning",
                    f"SLB {names.get(slbid, slbid)} 近24h {what} {n:.0f} 条",
                    f"{project}/{logstore} 中 slbid={slbid} 近24小时{what} {n:.0f} 条。",
                    target_key=slbid, target_name=names.get(slbid, ""), value=n, unit="条",
                ))
            try:
                r2 = sls_query_logs(ak, sk, project, logstore,
                                    f"{cond} | select request_uri, count(*) as cnt "
                                    f"group by request_uri order by cnt desc limit 5",
                                    start=start_s, end=end_s, limit=5)
                if r2["logs"]:
                    tops = ", ".join(
                        f"{row.get('request_uri', '?')}={_first_number([row]) or '?'}"
                        for row in r2["logs"])
                    detail += f"\nTop URI: {tops}"
            except Exception as e:
                logger.warning("日志 Top URI 查询失败 %s: %s", logstore, e)
        items.append(_item(
            "logs", "warning", f"{label}：近24h {what} {cnt:.0f} 条",
            detail, target_key=f"{project}/{logstore}", value=cnt, unit="条",
        ))
    if not items:
        items.append(_item("logs", "info", "日志摘要：无可用数据（SLS 查询失败或未配置）",
                           "详见巡检运行记录的 error 字段。"))
    return items


def _first_number(logs: list):
    for row in logs:
        for v in row.values():
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


# ---------------- 扫描器 5：闲置识别 ----------------

def _scan_idle(db: Session) -> list:
    items: list = []
    # 1) 停止的 ECS
    stopped = db.query(Asset).filter(Asset.product == "ecs", Asset.status.ilike("%stop%")).all()
    for a in stopped:
        items.append(_item(
            "idle", "info", f"ECS {a.name or a.instance_id} 处于停止状态（{a.status}）",
            f"实例 {a.instance_id}，地域 {a.region}，计费 {a.charge_type}。"
            + ("包年包月仍在计费，建议确认是否可释放。" if a.charge_type == "PrePaid" else ""),
            target_key=a.instance_id, target_name=a.name or "",
        ))
    # 2) ECS 7 天平均 CPU 过低（运行中才有意义）
    try:
        cpu_avg = _vm_last_values("avg_over_time(aliyun_ecs_cpuutilization[7d])")
        running = {a.instance_id: a for a in db.query(Asset).filter(Asset.product == "ecs").all()
                   if "stop" not in (a.status or "").lower()}
        low = [(iid, v[0]) for iid, v in cpu_avg.items()
               if iid in running and v[0] < settings.INSPECTION_IDLE_CPU]
        for iid, v in sorted(low, key=lambda x: x[1]):
            items.append(_item(
                "idle", "info", f"ECS {running[iid].name or iid} 7天平均 CPU 仅 {v:.1f}%（闲置嫌疑）",
                f"实例 {iid}，近7天平均 CPU {v:.1f}%，低于闲置阈值 {settings.INSPECTION_IDLE_CPU}%。"
                f"建议结合业务确认是否可缩容或降配。",
                target_key=iid, target_name=running[iid].name or "", value=round(v, 2), unit="%",
            ))
    except Exception as e:
        logger.warning("闲置 CPU 查询失败: %s", e)
    # 3) SLB 7 天无流量
    try:
        qps_avg = {k: v[0] for k, v in _vm_last_values("avg_over_time(aliyun_slb_instanceqps[7d])").items()}
        names = _name_map(db, "slb")
        for iid, v in sorted(qps_avg.items(), key=lambda x: x[1]):
            if v < settings.INSPECTION_IDLE_QPS:
                items.append(_item(
                    "idle", "info", f"SLB {names.get(iid, iid)} 7天平均 QPS≈0（无流量）",
                    f"实例 {iid} 近7天平均 QPS {v:.4f}，可能为闲置或备用实例，建议确认。",
                    target_key=iid, target_name=names.get(iid, ""), value=round(v, 4), unit="QPS",
                ))
    except Exception as e:
        logger.warning("闲置 SLB 查询失败: %s", e)
    # 4) 包年包月资产即将到期
    deadline = datetime.now() + timedelta(days=settings.INSPECTION_EXPIRY_DAYS)
    expiring = db.query(Asset).filter(
        Asset.charge_type == "PrePaid", Asset.expire_time.isnot(None),
        Asset.expire_time <= deadline,
    ).order_by(Asset.expire_time).all()
    for a in expiring:
        days = (a.expire_time.date() - date.today()).days
        lv = "warning" if days <= 7 else "info"
        items.append(_item(
            "idle", lv,
            f"{a.product.upper()} {a.name or a.instance_id} "
            + ("已过期" if days < 0 else f"{days} 天后到期"),
            f"实例 {a.instance_id}，到期时间 {a.expire_time.strftime('%Y-%m-%d')}"
            f"（{'已过期 ' + str(-days) + ' 天' if days < 0 else str(days) + ' 天'}）。"
            f"计费方式 {a.charge_type}，请确认续期或迁移计划。",
            target_key=a.instance_id, target_name=a.name or "", value=days, unit="天",
        ))
    if not items:
        items.append(_item("idle", "info", "闲置识别：未发现停止/低负载/无流量/即将到期的资源",
                           "停止实例、7天低CPU、SLB无流量、30天内到期 四项检查均无命中。"))
    return items


# ---------------- 编排 ----------------

_SCANNERS = [
    ("cost", _scan_cost),
    ("health", _scan_health),
    ("slb", _scan_slb),
    ("logs", _scan_logs),
    ("idle", _scan_idle),
]


def run_inspection(db: Session) -> dict:
    """执行一轮巡检并落库。返回 {run_id, item_count, scanners, duration_ms}。"""
    if not _lock.acquire(blocking=False):
        return {"busy": True, "message": "巡检正在执行中"}

    started = time.monotonic()
    _last_status.update({"running": True, "last_run_at": datetime.now().isoformat()})
    try:
        run = InspectionRun(run_date=date.today(), status="running")
        db.add(run)
        db.commit()

        items: list = []
        scanner_status: dict = {}
        errors: list = []
        for name, fn in _SCANNERS:
            t0 = time.monotonic()
            try:
                got = fn(db) or []
                scanner_status[name] = {"ok": True, "items": len(got),
                                        "ms": int((time.monotonic() - t0) * 1000)}
            except Exception as e:
                logger.exception("巡检扫描器 %s 失败", name)
                got = []
                scanner_status[name] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}",
                                        "ms": int((time.monotonic() - t0) * 1000)}
                errors.append(f"{name}: {str(e)[:200]}")
            items.extend(got)

        for it in items:
            db.add(InspectionItem(run_id=run.id, **it))
        run.item_count = len(items)
        run.error = "; ".join(errors)
        run.status = "ok"  # 单扫描器失败已记录在 error，整体仍算完成
        run.finished_at = datetime.now()
        db.commit()
    except Exception:
        # 整体异常（如落库失败）：把 run 标记为 error，避免永久停在 running
        try:
            run.status = "error"
            run.error = "巡检执行异常，见后端日志"
            run.finished_at = datetime.now()
            db.commit()
        except Exception:
            logger.exception("巡检 run 状态回写失败")
        raise
    finally:
        _last_status["running"] = False
        _lock.release()

    result = {
        "run_id": run.id, "item_count": len(items),
        "scanners": scanner_status,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    _last_status.update({"last_result": result})
    logger.info("巡检完成 run=%s items=%s errors=%s", run.id, len(items), errors or "无")
    return result
