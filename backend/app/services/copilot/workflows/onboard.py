"""引导域工作流：onboard_init——安装向导收尾一键初始化。

产品目标：用户走完安装向导点进系统就是"开箱可用"——该生成的数据（资产/拓扑/链路/
画像/全景对象）由本工作流直接生成，该确认的写入（默认告警规则）只建待确认工单。

步骤（五步 + 产物）：
  1) 盘点已配置源（云账号/数据源/SLS 目标/机器）→ readiness 清单
  2) 触发云资产同步（多云 provider，只读采集）+ 链路草稿生成 + 拓扑扫描重建
     （SLS 未配时链路草稿由 link_draft 干净跳过，如实进报告）
  3) 行为画像首轮采样（profile_engine.run_profiles）+ 全景对象生成
     （object_profile.run_object_profiles 现有机制；OBJECT_PROFILE_V2=False 时如实跳过）
  4) 默认告警规则生成：按资产类型出 CPU/内存/磁盘/连接/5xx 阈值规则，
     一律走 engine.change_step -> change_tools.save_alert_rule **建待确认工单**，
     绝不静默写入；已存在/已在待确认工单里的规则跳过（幂等可重跑）；
     证书到期/资源包余量不在告警规则表（metric/sls）能力内，由到期总账与
     资源包监控内置覆盖，如实说明不硬凑
  5) 逐项验证数据可获取（云账号真实探活 / 数据源真实探测 / SLS 真读 / 机器 / 本地数据面）
  产物 = 初始化报告（生成了什么 / 跳过了什么为什么 / 验证结果 / 下一步建议）

红线（与工作流库同一红线）：
  - 告警规则等写入一律待确认工单，工作流绝不静默执行写入
  - 云资产同步/画像/拓扑/链路=只读采集生成本地数据，可直接执行
  - 凭证不落步骤 detail / 产物 / 运行记录
  - 分析取数全部只读；不执行 LLM/脚本原文
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func

from app.services.cloud.provider import RESOURCE_TYPES
from app.services.copilot.workflows import engine

# 步骤名（ctx["data"] 的键，与 spec.steps 一一对应）
STEP_INV = "盘点已配置源"
STEP_SYNC = "同步资产与拓扑"
STEP_PROFILE = "画像与全景对象"
STEP_RULES = "生成默认告警规则"
STEP_VERIFY = "验证数据可获取"

_PRODUCT_LABELS = {**RESOURCE_TYPES, "ecs": "ECS", "rds": "RDS", "redis": "Redis", "slb": "SLB"}

# 每资产类型的默认阈值规则。指标名与 collector/cms.METRIC_CONFIG 同源（metric 规则
# 口径 product+metric 自动拼 VM 指标名）；阈值先例=巡检阈值（INSPECTION_HEALTH_WARN/CRIT
# = 80/90）的"90 严重线"用作告警阈值，持续窗口 300s 与规则表默认一致。
# (metric, 中文标签, comparator, threshold, level, duration)
DEFAULT_METRIC_RULES: dict = {
    "ecs": [
        ("CPUUtilization", "CPU使用率", ">", 90.0, "warning", 300),
        ("memory_usedutilization", "内存使用率", ">", 90.0, "warning", 300),
        ("diskusage_utilization", "磁盘使用率", ">", 90.0, "warning", 300),
    ],
    "rds": [
        ("CpuUsage", "CPU使用率", ">", 90.0, "warning", 300),
        ("DiskUsage", "磁盘使用率", ">", 90.0, "warning", 300),
        ("ConnectionUsage", "连接数使用率", ">", 90.0, "warning", 300),
    ],
    "redis": [
        ("ShardingCpuUsage", "CPU使用率", ">", 90.0, "warning", 300),
        ("ShardingMemoryUsage", "内存使用率", ">", 90.0, "warning", 300),
    ],
    "slb": [
        ("InstanceStatusCode5xx", "5xx状态码", ">", 10.0, "warning", 300),
    ],
    # vSphere 原生指标：vmware_vm_* / vmware_host_* 序列，
    # collector/cms.ALERT_METRIC_CONFIG 同源；磁盘是绝对量(bytes)不套 90% 阈值
    "vmware_vm": [
        ("cpu_usage_percent", "CPU使用率", ">", 90.0, "warning", 300),
        ("mem_usage_percent", "内存使用率", ">", 90.0, "warning", 300),
    ],
    "vmware_host": [
        ("cpu_usage_percent", "CPU使用率", ">", 90.0, "warning", 300),
        ("mem_usage_percent", "内存使用率", ">", 90.0, "warning", 300),
    ],
}

# 告警规则表（alert_rules）只支持 metric/sls 两类，证书到期/资源包余量不在其中——
# 由到期总账（30/7 分级）与资源包池监控/购买建议内置覆盖，无需阈值规则，如实说明。
_BUILTIN_COVERED = [
    "证书/域名到期：到期总账内置（过期不汇报、30/7 天分级提醒），随资产同步自动纳入",
    "资源包余量：资源包池监控 + 购买建议内置（池余量/耗尽日期跟踪），随账单采集自动纳入",
]


# ---------------- 1) 盘点已配置源 ----------------

def _inv_sources(db, params, ctx):
    from app.models.asset import Asset
    from app.models.cloud_account import CloudAccount
    from app.services import datasource as ds
    from app.services.log_query import source_entries
    from app.services.sls_targets import (
        app_log,
        cdn_target,
        configured as sls_configured,
        slb_targets,
    )

    accounts = (db.query(CloudAccount)
                .filter(CloudAccount.enabled == True)  # noqa: E712
                .order_by(CloudAccount.id).all())
    metric_src = ds.metric_sources()
    # 日志源口径与日志查询页同源：阿里云凭证可用时会合成隐式 SLS 源，不能只数
    # LOG_SOURCES；否则阿里云凭证可查询时，向导仍会误报"日志源 0 个"。
    log_entries = source_entries(db)
    ready_logs = [e for e in log_entries if e.get("status") == "ready"]
    log_names = [str(e.get("name") or e.get("type") or "?") for e in ready_logs]
    n_hosts = (db.query(func.count(Asset.id))
               .filter(Asset.product == "host").scalar() or 0)
    sls_ok = sls_configured()

    data = {
        "cloud_accounts": [{"name": a.name, "vendor": a.vendor} for a in accounts],
        "metric_sources": [str(s.get("name") or s.get("type") or "?") for s in metric_src],
        "log_sources": log_names,
        "log_sources_total": len(log_entries),
        "sls": {
            "configured": sls_ok,
            "slb_pairs": len(slb_targets()),
            "app_log": bool(app_log()[0]),
            "cdn": bool(cdn_target()[0]),
        },
        "hosts": n_hosts,
    }
    acc_desc = "、".join(f"{a['name']}({a['vendor']})" for a in data["cloud_accounts"]) or "无"
    sls_desc = ("已配置（SLB %d 对 / 应用日志 %s / CDN %s）"
                % (data["sls"]["slb_pairs"],
                   "有" if data["sls"]["app_log"] else "无",
                   "有" if data["sls"]["cdn"] else "无")) if sls_ok else "未配置"
    detail = (f"云账号 {len(accounts)} 个（{acc_desc}）；指标源 {len(metric_src)} 个"
              f"（{'、'.join(data['metric_sources'][:5]) or '无'}）；"
              f"可查询日志源 {len(ready_logs)} 个（{'、'.join(log_names[:5]) or '无'}）；"
              f"SLS 目标：{sls_desc}；机器（Agent 上报）{n_hosts} 台")
    return {"status": engine.STEP_OK, "detail": detail, "data": data}


# ---------------- 2) 云资产同步 + 链路草稿 + 拓扑扫描 ----------------

def _sync_assets(db, params, ctx):
    inv = ctx["data"].get(STEP_INV) or {}
    parts: list = []
    data: dict = {}

    # 2a) 云资产同步必须复用统一 sync_job，保留产品级失败并更新同一个 Onboarding Run。
    # 新建账号的后台接入仍在进行时，本工作流只读进度，不并发再跑一次相同云调用。
    if inv.get("cloud_accounts"):
        from app.models.cloud_account import CloudAccount
        from app.services.cloud.sync_job import ONBOARDING_STATUSES, sync_accounts

        accounts = (db.query(CloudAccount)
                    .filter(CloudAccount.enabled == True)  # noqa: E712
                    .order_by(CloudAccount.id).all())
        active = {a.id: a for a in accounts if a.status in ONBOARDING_STATUSES}
        pending_ids = [a.id for a in accounts if a.id not in active]
        rows = []
        if pending_ids:
            out = sync_accounts(db, account_ids=pending_ids, force=True)
            rows = out["results"]

        synced_by_id = {}
        for row in rows:
            aid = row.get("account_id")
            entry = synced_by_id.setdefault(aid, {
                "account": row.get("account") or str(aid), "ok": True,
                "total": 0, "created": 0, "updated": 0, "errors": [],
            })
            for field in ("total", "created", "updated"):
                entry[field] += int(row.get(field) or 0)
            if row.get("ok") is False or row.get("failed_regions"):
                entry["ok"] = False
                reason = row.get("error") or row.get("note") or "；".join(row.get("failed_regions") or [])
                entry["errors"].append(str(reason)[:150])
            if row.get("skipped"):
                entry["skipped"] = True
                entry["note"] = row.get("note") or "本期未启用资产采集"
        synced = []
        for acc in accounts:
            if acc.id in active:
                synced.append({"account": acc.name, "ok": True, "running": True,
                               "note": f"账号后台接入进行中（{acc.status}），本次不重复同步"})
                continue
            entry = synced_by_id.get(acc.id)
            if entry is None:
                entry = {"account": acc.name, "ok": False, "error": "本轮未返回同步结果"}
            if entry.get("errors"):
                entry["error"] = "；".join(entry.pop("errors"))
            synced.append(entry)
        data["sync"] = synced
        ok_n = sum(1 for s in synced if s.get("ok"))
        parts.append("云资产同步：%d/%d 个账号成功或进行中（%s）" % (
            ok_n, len(synced),
            "；".join((f"{s['account']} 后台接入中" if s.get("running")
                       else f"{s['account']} 跳过（厂商未支持采集）" if s.get("skipped")
                       else f"{s['account']}{' ' + str(s.get('total', 0)) + ' 项' if s.get('ok') else ' 失败:' + str(s.get('error', ''))[:60]}")
                      for s in synced[:4])))
    else:
        data["sync"] = {"skipped": "无云账号"}
        parts.append("云资产同步：未登记云账号，跳过（向导保存账号后重跑可补同步）")

    # 2b) 链路草稿（SLS 未配时 link_draft 干净跳过并回 message，如实进报告）
    from app.services.link_draft import generate_link_draft

    try:
        ld = generate_link_draft(db)
    except Exception as e:  # noqa: BLE001
        ld = {"error": str(e)[:150]}
    data["link_draft"] = ld
    if ld.get("skipped"):
        parts.append(f"链路草稿：跳过（{ld.get('skipped')}）——{str(ld.get('message') or '')[:80]}")
    elif ld.get("error"):
        parts.append(f"链路草稿：生成异常 {ld['error']}")
    elif ld.get("busy"):
        parts.append("链路草稿：生成中（已有任务在跑），本次跳过")
    else:
        parts.append("链路草稿：%s" % "、".join(
            f"{k}={v}" for k, v in list(ld.items())[:5] if isinstance(v, (int, float, str))))

    # 2c) 拓扑扫描（只重建图 + diff 留痕；资产同步已在 2a 走多云 provider，
    #     不再用 full=True 重复走阿里采集器）
    from app.services import topology

    try:
        scan = topology.run_scan(db, trigger="onboard", full=False)
        stats = scan.get("stats") or {}
        data["topology"] = {"scan_id": scan.get("scan_id"), "stats": stats}
        parts.append("拓扑扫描：%s" % "、".join(
            f"{k}={v}" for k, v in list(stats.items())[:5] if isinstance(v, (int, float))))
    except Exception as e:  # noqa: BLE001
        data["topology"] = {"error": str(e)[:150]}
        parts.append(f"拓扑扫描：失败 {str(e)[:80]}")

    sync_failed = (isinstance(data.get("sync"), list)
                   and any(not row.get("ok") for row in data["sync"]))
    status = (engine.STEP_FAILED
              if sync_failed or (data.get("topology") or {}).get("error")
              else engine.STEP_OK)
    return {"status": status, "detail": "；".join(parts), "data": data}


# ---------------- 3) 行为画像 + 全景对象 ----------------

def _profiles(db, params, ctx):
    from app.core.config import settings

    parts: list = []
    data: dict = {}
    try:
        from app.services import profile_engine

        pr = profile_engine.run_profiles(db)
        data["profiles"] = pr
        parts.append("行为画像首轮采样：画像 %s 个 / 当日快照 %s / 事件样本 %s"
                     % (pr.get("profiles", 0), pr.get("snapshots", 0), pr.get("event_samples", 0)))
    except Exception as e:  # noqa: BLE001
        data["profiles"] = {"error": str(e)[:150]}
        parts.append(f"行为画像采样失败：{str(e)[:80]}")

    if getattr(settings, "OBJECT_PROFILE_V2", True):
        try:
            from app.services import object_profile as op

            r = op.run_object_profiles(db)
            data["object_profiles"] = r
            parts.append("全景对象：%s 个对象快照 / 基线 %s / 变化事件 %s"
                         % (r.get("snapshots", 0), r.get("learned", 0), r.get("events", 0)))
        except Exception as e:  # noqa: BLE001
            data["object_profiles"] = {"error": str(e)[:150]}
            parts.append(f"全景对象生成失败：{str(e)[:80]}")
    else:
        data["object_profiles"] = {"skipped": "OBJECT_PROFILE_V2=False"}
        parts.append("全景对象：未启用（OBJECT_PROFILE_V2=False），跳过")

    failed = any(isinstance(v, dict) and v.get("error") for v in data.values())
    return {"status": engine.STEP_FAILED if failed else engine.STEP_OK,
            "detail": "；".join(parts), "data": data}


# ---------------- 4) 默认告警规则（只建待确认工单） ----------------

def _rule_defs_for(products: list) -> list:
    out = []
    for product in products:
        for metric, label, cmp_, thr, level, dur in DEFAULT_METRIC_RULES.get(product, []):
            out.append({
                "name": f"默认规则·{_PRODUCT_LABELS.get(product, product)}·{label}",
                "source": "metric", "level": level,
                "product": product, "metric": metric,
                "comparator": cmp_, "threshold": thr, "duration": dur,
            })
    return out


def _pending_proposed_names(db) -> set:
    """已在待确认工单里的 save_alert_rule 规则名（幂等重跑不重复建单）。"""
    from app.models.agent_change import AgentChangeTicket

    names: set = set()
    for row in (db.query(AgentChangeTicket)
                .filter(AgentChangeTicket.status == "pending").all()):
        try:
            import json as _json
            items = _json.loads(row.change_list_json or "[]")
        except ValueError:
            continue
        for it in items:
            if isinstance(it, dict) and it.get("tool") == "save_alert_rule":
                n = (it.get("params") or {}).get("name")
                if n:
                    names.add(n)
    return names


def _alert_rules(db, params, ctx):
    from app.models.alert_rule import AlertRule
    from app.models.asset import Asset

    products = sorted({p for (p,) in db.query(Asset.product).distinct().all()
                       if p in DEFAULT_METRIC_RULES})
    if not products:
        return {"status": engine.STEP_SKIPPED,
                "detail": "资产库暂无 ECS/RDS/Redis/SLB 资产——跳过默认告警规则生成"
                          "（资产同步完成后重跑本工作流即可补上）",
                "data": {"skipped": "no_assets", "builtin_covered": _BUILTIN_COVERED}}

    candidates = _rule_defs_for(products)
    existing = {r.name for r in db.query(AlertRule).all()}
    proposed = _pending_proposed_names(db)
    todo = [r for r in candidates if r["name"] not in existing and r["name"] not in proposed]
    done_n = len(candidates) - len(todo)

    if not todo:
        return {"status": engine.STEP_OK,
                "detail": (f"默认告警规则已齐（{len(candidates)} 条，{done_n} 条已存在或已在待确认工单中）"
                           "——无需重复建单。证书/资源包类由内置机制覆盖：" + _BUILTIN_COVERED[0]),
                "data": {"candidates": len(candidates), "tickets": [],
                         "builtin_covered": _BUILTIN_COVERED}}

    # 变更单条目上限 5（change_tools.MAX_ITEMS）——按 5 条一张工单分批建单
    tickets = []
    step_status = engine.STEP_OK
    details = []
    for i in range(0, len(todo), 5):
        chunk = todo[i:i + 5]
        items = [{"tool": "save_alert_rule", "params": dict(r)} for r in chunk]
        title = "初始化默认告警规则（%s）" % "、".join(
            sorted({r["product"].upper() for r in chunk}))
        r = engine.change_step(
            db, ctx, title=title, items=items, ack_required=False,
            ok_detail=(f"已生成待确认变更工单（默认告警规则 {len(chunk)} 条："
                       + "、".join(x["name"] for x in chunk)
                       + "）——需用户在变更确认卡确认后才生效，不静默写入"))
        details.append(r.get("detail") or "")
        if r.get("status") == engine.STEP_OK:
            tickets.append(r.get("data") or {})
        else:
            step_status = engine.STEP_FAILED
    detail = ("默认告警规则：按资产类型（%s）生成 %d 条待确认规则（%d 张工单）；%d 条已存在/已提议跳过。"
              % ("、".join(_PRODUCT_LABELS.get(p, p) for p in products),
                 len(todo), len(tickets), done_n))
    if details:
        detail += " " + " ".join(d[:120] for d in details[:2])
    return {"status": step_status, "detail": detail,
            "data": {"proposed": [r["name"] for r in todo], "tickets": tickets,
                     "skipped_existing": done_n, "builtin_covered": _BUILTIN_COVERED}}


# ---------------- 5) 验证数据可获取（真实探测，不装） ----------------

def _probe_cloud_accounts(db, probes: list) -> None:
    from app.core.security import decrypt_secret
    from app.models.cloud_account import CloudAccount
    from app.services.cloud import provider as cloud_provider

    accounts = (db.query(CloudAccount)
                .filter(CloudAccount.enabled == True)  # noqa: E712
                .order_by(CloudAccount.id).all())
    if not accounts:
        probes.append({"source": "云账号", "ok": None,
                       "detail": "取不到：未登记云账号（向导保存账号后可再验证）"})
        return
    for acc in accounts:
        label = f"云账号·{acc.name}"
        try:
            ak = decrypt_secret(acc.ak_cipher) if acc.ak_cipher else ""
            sk = decrypt_secret(acc.secret_cipher) if acc.secret_cipher else ""
            cap = cloud_provider.capabilities_of(acc.vendor)
            if not cap.probe:
                probes.append({"source": label, "ok": None,
                               "detail": f"跳过：{cloud_provider.vendor_label(acc.vendor)}本期仅支持登记，连通测试未启用"})
                continue
            if not ak and cap.needs_ak_sk:
                probes.append({"source": label, "ok": False,
                               "detail": "取不到：账号无凭证（AK/SK 为空）"})
                continue
            res = cloud_provider.get_provider(acc.vendor).test_account(acc, ak, sk)
            ok = bool(res.get("ok"))
            msg = str(res.get("message") or res.get("detail") or "")[:180]
            probes.append({"source": label, "ok": ok,
                           "detail": ("可取到：" + msg) if ok else ("取不到：" + (msg or "连通测试未通过"))})
        except Exception as e:  # noqa: BLE001
            probes.append({"source": label, "ok": False,
                           "detail": f"取不到：{type(e).__name__}: {str(e)[:120]}"})


def _probe_datasources(db, probes: list) -> None:
    from app.services import datasource as ds

    metric_src = ds.metric_sources()
    log_src = [s for s in ds.log_sources()
               if (s.get("type") or "") not in ("skywalking", "zipkin")]
    if not metric_src and not log_src:
        probes.append({"source": "数据源", "ok": None,
                       "detail": "取不到：未配置外部数据源（内置指标库 VictoriaMetrics 随部署自带）"})
        return
    for s in metric_src:
        name = str(s.get("name") or s.get("type") or "?")
        try:
            r = ds.probe_metric_source(s, db)
            ok = bool(r.get("ok"))
            probes.append({"source": f"指标源·{name}", "ok": ok,
                           "detail": ("可取到：" if ok else "取不到：")
                                     + str(r.get("detail") or "")[:180]})
        except Exception as e:  # noqa: BLE001
            probes.append({"source": f"指标源·{name}", "ok": False,
                           "detail": f"取不到：{type(e).__name__}: {str(e)[:120]}"})
    for s in log_src:
        name = str(s.get("name") or s.get("type") or "?")
        try:
            r = ds.probe_log_source(s, db)
            ok = bool(r.get("ok"))
            probes.append({"source": f"日志源·{name}", "ok": ok,
                           "detail": ("可取到：" if ok else "取不到：")
                                     + str(r.get("detail") or "")[:180]})
        except Exception as e:  # noqa: BLE001
            probes.append({"source": f"日志源·{name}", "ok": False,
                           "detail": f"取不到：{type(e).__name__}: {str(e)[:120]}"})
    apm_n = sum(1 for s in ds.log_sources() if (s.get("type") or "") in ("skywalking", "zipkin"))
    if apm_n:
        probes.append({"source": "APM 源", "ok": None,
                       "detail": f"已登记 {apm_n} 个（服务目录由后台采集更新，验证步骤不重复写入）"})


def _probe_sls(db, probes: list) -> None:
    from app.services.sls_targets import configured as sls_configured, slb_targets

    if not sls_configured():
        probes.append({"source": "SLS 日志目标", "ok": None,
                       "detail": "取不到：SLS 目标未配置（安装向导「补数据」或 SLS_* 环境变量配置后可取）"})
        return
    targets = slb_targets()
    project = targets[0][0] if targets else None
    try:
        from app.collector.credential import CredentialError, resolve_aliyun_credential
        from app.collector.sls import list_logstores

        ak, sk, _ = resolve_aliyun_credential(db)
        if project:
            stores = list_logstores(ak, sk, project)
            probes.append({"source": "SLS 日志目标", "ok": True,
                           "detail": f"可取到：{project} 下 {len(stores)} 个 logstore"})
        else:
            probes.append({"source": "SLS 日志目标", "ok": True,
                           "detail": "可取到：目标已配置（应用/CDN 日志）"})
    except CredentialError as e:
        probes.append({"source": "SLS 日志目标", "ok": False,
                       "detail": f"取不到：无可用凭证（{str(e)[:80]}）"})
    except Exception as e:  # noqa: BLE001
        probes.append({"source": "SLS 日志目标", "ok": False,
                       "detail": f"取不到：{type(e).__name__}: {str(e)[:120]}"})


def _probe_local(db, probes: list) -> None:
    from app.models.asset import Asset
    from app.models.cost_daily import CostDaily

    n_assets = db.query(Asset).filter(Asset.status != "Deleted").count()
    probes.append({"source": "资产台账", "ok": n_assets > 0,
                   "detail": (f"可取到：{n_assets} 个资产在库" if n_assets
                              else "取不到：资产库为空（完成云资产同步后可取）")})
    try:
        from app.services.bootstrap import _metric_series_present

        has_series = _metric_series_present()
        probes.append({"source": "指标库（VictoriaMetrics）", "ok": has_series,
                       "detail": ("可取到：近 30 分钟有序列" if has_series
                                  else "取不到：窗口内无序列（等一个采集周期；仍为空时"
                                       "检查云监控权限 / vCenter 统计级别 / Agent 上报）")})
    except Exception as e:  # noqa: BLE001
        probes.append({"source": "指标库（VictoriaMetrics）", "ok": False,
                       "detail": f"取不到：{type(e).__name__}: {str(e)[:120]}"})
    try:
        from datetime import timedelta

        from app.services.bootstrap import _billable_accounts_present

        n_cost = (db.query(CostDaily)
                  .filter(CostDaily.stat_date >= datetime.now().date() - timedelta(days=3))
                  .count())
        if n_cost:
            probes.append({"source": "账单", "ok": True,
                           "detail": f"可取到：近 3 天 {n_cost} 行"})
        elif _billable_accounts_present(db):
            probes.append({"source": "账单", "ok": False,
                           "detail": "取不到：近 3 天无账单行（T+1，次日 10 点采集属正常；"
                                     "持续为空时检查费用中心只读权限）"})
        else:
            probes.append({"source": "账单", "ok": None,
                           "detail": "不适用：未配置 aliyun/tencent 计费账号"
                                     "（vCenter/私有化环境没有账单数据）"})
    except Exception as e:  # noqa: BLE001
        probes.append({"source": "账单", "ok": False,
                       "detail": f"取不到：{type(e).__name__}: {str(e)[:120]}"})

    n_hosts = (db.query(func.count(Asset.id))
               .filter(Asset.product == "host", Asset.status != "Deleted").scalar() or 0)
    probes.append({"source": "机器（Agent）", "ok": n_hosts > 0,
                   "detail": (f"可取到：{n_hosts} 台已上报" if n_hosts
                              else "取不到：尚无 Agent 上报（「生成 Agent 安装命令」下发后可取）")})


def _verify_sources(db, params, ctx):
    probes: list = []
    _probe_cloud_accounts(db, probes)
    _probe_datasources(db, probes)
    _probe_sls(db, probes)
    _probe_local(db, probes)
    ok_n = sum(1 for p in probes if p.get("ok") is True)
    fail_n = sum(1 for p in probes if p.get("ok") is False)
    skip_n = sum(1 for p in probes if p.get("ok") is None)
    detail = f"逐项探测 {len(probes)} 项：可取到 {ok_n} / 取不到 {fail_n} / 未配置跳过 {skip_n}（结果见报告）"
    return {"status": engine.STEP_OK, "detail": detail, "data": {"probes": probes}}


# ---------------- 产物：初始化报告 ----------------

def _init_report(db, params, ctx):
    from app.services import audit as audit_svc

    inv = ctx["data"].get(STEP_INV) or {}
    sync = ctx["data"].get(STEP_SYNC) or {}
    prof = ctx["data"].get(STEP_PROFILE) or {}
    rules = ctx["data"].get(STEP_RULES) or {}
    verify = ctx["data"].get(STEP_VERIFY) or {}

    # ---- 生成/跳过清单 ----
    generated: list = []
    skipped: list = []

    sync_rows = sync.get("sync")
    if isinstance(sync_rows, dict) and sync_rows.get("skipped"):
        skipped.append(f"云资产同步：{sync_rows['skipped']}")
    elif isinstance(sync_rows, list):
        ok_n = sum(1 for s in sync_rows if s.get("ok"))
        generated.append(f"云资产同步：{ok_n}/{len(sync_rows)} 个账号"
                         + ("（%d 项资产入库）" % sum(int(s.get("total") or 0) for s in sync_rows if s.get("ok"))))
        for s in sync_rows:
            if s.get("ok") is False:
                skipped.append(f"云账号「{s.get('account')}」同步失败：{str(s.get('error') or '')[:80]}")

    ld = sync.get("link_draft") or {}
    if ld.get("skipped"):
        skipped.append(f"链路草稿：{ld.get('skipped')}（{str(ld.get('message') or '')[:100]}）")
    elif ld.get("error"):
        skipped.append(f"链路草稿生成异常：{ld['error']}")
    elif ld.get("busy"):
        skipped.append("链路草稿：已有生成任务在跑，本次跳过")
    else:
        generated.append("链路草稿已生成/刷新（draft 边按 SLB 访问日志重算，已确认/已忽略不覆盖）")

    topo = sync.get("topology") or {}
    if topo.get("error"):
        skipped.append(f"拓扑扫描失败：{topo['error']}")
    else:
        st = topo.get("stats") or {}
        generated.append("拓扑扫描完成：%s" % "、".join(
            f"{k}={v}" for k, v in list(st.items())[:4] if isinstance(v, (int, float))))

    pr = prof.get("profiles") or {}
    if pr.get("error"):
        skipped.append(f"行为画像采样失败：{pr['error']}")
    else:
        generated.append("行为画像首轮采样：%s 个画像 / %s 个当日快照"
                         % (pr.get("profiles", 0), pr.get("snapshots", 0)))
    op = prof.get("object_profiles") or {}
    if op.get("skipped"):
        skipped.append(f"全景对象：{op['skipped']}")
    elif op.get("error"):
        skipped.append(f"全景对象生成失败：{op['error']}")
    else:
        generated.append("全景对象：%s 个对象快照 / %s 个对象学到基线"
                         % (op.get("snapshots", 0), op.get("learned", 0)))

    if rules.get("skipped") == "no_assets":
        skipped.append("默认告警规则：无资产，跳过（资产同步完成后重跑即可补上）")
    else:
        proposed = rules.get("proposed") or []
        if proposed:
            tids = [t.get("ticket_id") for t in (rules.get("tickets") or [])]
            generated.append("默认告警规则：%d 条已建待确认工单（%s）——确认后才生效"
                             % (len(proposed), "、".join(f"#{t}" for t in tids if t) or "工单"))
        else:
            generated.append("默认告警规则：已齐（%s 条已存在/已提议，无需重复建单）"
                             % rules.get("candidates", 0))
        for line in (rules.get("builtin_covered") or _BUILTIN_COVERED):
            skipped.append(line + "（无需阈值规则）")

    # ---- 报告 markdown ----
    probes = verify.get("probes") or []
    probe_rows = "\n".join(
        "| %s | %s | %s |" % (
            p.get("source", ""),
            "可取到" if p.get("ok") is True else ("取不到" if p.get("ok") is False else "未配置"),
            str(p.get("detail") or "").replace("|", "／"))
        for p in probes)
    gen_lines = "\n".join(f"- {g}" for g in generated) or "- （本次没有新生成项）"
    skip_lines = "\n".join(f"- {s}" for s in skipped) or "- （无跳过项）"
    acc_desc = "、".join(f"{a['name']}({a['vendor']})" for a in (inv.get("cloud_accounts") or [])) or "无"
    sls_desc = ("已配置" if (inv.get("sls") or {}).get("configured") else "未配置")

    report = f"""## 初始化报告 · AI 引导初始化

| 项 | 内容 |
|---|---|
| 云账号 | {acc_desc} |
| 指标源 / 可查询日志源 | {len(inv.get("metric_sources") or [])} / {len(inv.get("log_sources") or [])} |
| SLS 日志目标 | {sls_desc} |
| 机器（Agent） | {inv.get("hosts", 0)} 台 |
| 告警规则待确认 | {len(rules.get("proposed") or [])} 条（不静默写入） |

### 一、生成了什么
{gen_lines}

### 二、跳过了什么 / 为什么
{skip_lines}

### 三、数据可获取验证（真实探测）
| 数据源 | 结果 | 说明 |
|---|---|---|
{probe_rows}

### 四、下一步建议
1. {"到「智能体 → 变更确认卡」确认默认告警规则工单，确认后规则才生效（可逐条调整阈值）" if (rules.get("proposed") or []) else "告警规则已就绪，可在「告警」页按业务微调阈值"}
2. {"日志查询已可用；如需链路草稿与 SLB/应用日志关联分析，到 安装向导「补数据」配置 SLS 目标后重跑本工作流" if (inv.get("log_sources") or []) and not (inv.get("sls") or {}).get("configured") else ("安装向导「补数据」配置 SLS 目标后重跑本工作流，解锁链路草稿与 SLB/应用日志关联分析" if not (inv.get("sls") or {}).get("configured") else "到「拓扑」页核对链路草稿（确认真实链路，忽略野域名）")}
3. 到「拓扑 / 画像 / 全景」页看首轮结果；画像基线随采样天数逐步收敛（首日只观察不打扰）
4. 证书/域名到期看「到期总账」，资源包余量看「成本」页——均已内置监控
"""
    highlights = generated[:4] + [
        ("默认告警规则 %d 条已生成待确认工单（绝不静默写入）" % len(rules.get("proposed") or []))
        if (rules.get("proposed") or []) else "告警规则已齐，无需建单",
        "数据可获取验证：可取到 %d 项 / 取不到 %d 项 / 未配置 %d 项"
        % (sum(1 for p in probes if p.get("ok") is True),
           sum(1 for p in probes if p.get("ok") is False),
           sum(1 for p in probes if p.get("ok") is None)),
    ]

    # ---- 审计（初始化运行留痕：agent_workflow_runs 已由引擎落，这里补 audit） ----
    try:
        user = (ctx.get("context") or {}).get("user")
        audit_svc.record(
            db, action="bootstrap.ai_init",
            user_id=getattr(user, "id", None),
            username=str(getattr(user, "username", "") or ""),
            resource_type="bootstrap", resource_id="onboard_init",
            detail={"generated": generated[:8], "skipped": skipped[:8],
                    "tickets": [t.get("ticket_id") for t in (rules.get("tickets") or [])],
                    "probes_ok": sum(1 for p in probes if p.get("ok") is True)},
        )
    except Exception:  # noqa: BLE001  审计失败不阻断产物
        pass

    return {"report_md": report, "highlights": highlights,
            "generated": generated, "skipped": skipped, "probes": probes,
            "ticket_ids": [t.get("ticket_id") for t in (rules.get("tickets") or [])],
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


# ---------------- spec 注册表 ----------------

ONBOARD_WORKFLOWS = {
    "onboard_init": {
        "name": {"zh": "AI 引导初始化", "en": "AI bootstrap initialization"},
        "domain": {"zh": "引导域", "en": "Bootstrap"},
        "description": {
            "zh": "安装向导收尾一键初始化：盘点已配置源 → 云资产同步 + 拓扑扫描 + 链路草稿 "
                  "（SLS 未配如实跳过）→ 行为画像首轮采样 + 全景对象生成 → 默认告警规则"
                  "（按资产类型，只建待确认工单不静默写入）→ 逐项验证数据可获取，产物=初始化报告",
            "en": "One-shot initialization at the end of the setup wizard: inventory configured sources "
                  "-> cloud asset sync + topology scan + link drafts (skipped with honest note when SLS "
                  "is unconfigured) -> first behavior-profile sampling + object profile generation -> "
                  "default alert rules per asset type (pending change tickets only, never silent writes) "
                  "-> per-source data availability probes. Artifact: initialization report",
        },
        "params_schema": {},
        "steps": [
            (STEP_INV, _inv_sources),
            (STEP_SYNC, _sync_assets),
            (STEP_PROFILE, _profiles),
            (STEP_RULES, _alert_rules),
            (STEP_VERIFY, _verify_sources),
        ],
        "build_artifact": _init_report,
    },
}
