"""安装向导与底座体检。

产品原则：不是客户配平台，是平台摸客户家底。
- probe：拿一副 AK（向导临时输入或已存凭证）对五个数据面各做一次最小只读探测，
  绿/黄/红 + 说人话的 detail/hint；不写任何数据、不保存临时凭证
- report：底座体检报告——本地数据面就绪度 + 能力矩阵 + 缺口建议（按解锁能力数排序）
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.services.capability_registry import (
    FACE_LABELS,
    capability_readiness,
    gap_suggestions,
)

logger = logging.getLogger("qianshu.bootstrap")


def _face(key: str, status: str, detail: str, hint: str = "") -> dict:
    return {"key": key, "label": FACE_LABELS[key], "status": status,
            "detail": detail, "hint": hint}


# ---------------------------------------------------------------- 探测（调云 API，全只读）


def _probe_assets(ak: str, sk: str) -> dict:
    """资产面：DescribeRegions + 各 region 首页实例计数（只拉第一页，不写库）。"""
    from app.collector.ecs import _build_client
    from alibabacloud_ecs20140526 import models as ecs

    client = _build_client(ak, sk, "cn-hangzhou")
    regions = [r.region_id for r in
               client.describe_regions(ecs.DescribeRegionsRequest()).body.regions.region]
    total, hit = 0, []
    for region in regions[:8]:   # 探测不做全量遍历，前 8 个常见 region 足够定性
        try:
            c = _build_client(ak, sk, region)
            r = c.describe_instances(ecs.DescribeInstancesRequest(
                region_id=region, page_size=100, page_number=1)).body
            n = len(r.instances.instance or [])
            total += n
            if n:
                hit.append(f"{region}:{n}")
        except Exception as e:
            logger.info("资产探测 %s 跳过: %s", region, str(e)[:60])
    if total:
        return _face("assets", "green", f"探测到 ECS 实例 {total} 台（{', '.join(hit[:4])}…）")
    return _face("assets", "yellow", "AK 可用但未探测到 ECS 实例",
                 "可能资源在其他地域，或该 AK 无 ECS 只读权限")


def _probe_metrics(ak: str, sk: str, assets_face: dict) -> dict:
    """指标面：云监控取一台实例的 CPU 一个点（最小代价验证 CMS 可达）。"""
    from app.collector.cms import _client as cms_client
    from alibabacloud_cms20190101 import models as cms

    if assets_face["status"] == "red":
        return _face("metrics", "yellow", "资产面未通，无法选实例验证云监控")
    try:
        client = cms_client(ak, sk, "cn-hangzhou")
        resp = client.describe_metric_list(
            cms.DescribeMetricListRequest(
                namespace="acs_ecs_dashboard", metric_name="CPUUtilization",
                period="60",
                start_time=str(int(time.time() * 1000) - 600),
                end_time=str(int(time.time() * 1000)),
            ))
        import json as _json
        try:
            n = len(_json.loads((resp.body.datapoints or "[]")))
        except Exception:
            n = 0
        if n:
            return _face("metrics", "green", f"云监控可达，已取到 {n} 个数据点")
        return _face("metrics", "yellow", "云监控 API 可达但窗口内无数据点",
                     "确认云监控 agent 在跑，或稍后再探")
    except Exception as e:
        msg = str(e)[:120]
        if "Forbidden" in msg or "auth" in msg.lower():
            return _face("metrics", "red", "云监控无权限", "AK 需加云监控只读权限（CMS）")
        return _face("metrics", "yellow", f"云监控调用未通：{msg[:60]}")


def _probe_logs(ak: str, sk: str) -> dict:
    """日志面：SLS project 列表（只读）。"""
    from app.collector.sls import list_projects

    try:
        projects = list_projects(ak, sk)
    except Exception as e:
        return _face("logs", "red", f"SLS 不可达：{str(e)[:80]}",
                     "确认 AK 有 SLS 只读权限，且 SLS_ENDPOINT 与 project 区域一致")
    if projects:
        return _face("logs", "green",
                     f"SLS 可达，{len(projects)} 个 project（如 {', '.join(projects[:3])}）",
                     "在安装向导下一步选择 SLB/应用日志目标即可解锁日志类能力")
    return _face("logs", "yellow", "SLS 可达但无 project", "账号尚未开通/使用日志服务")


def _probe_cost(ak: str, sk: str) -> dict:
    """账单面：昨日 BSS 账单一页（只读）。"""
    from app.collector.bss import fetch_day_bill

    yesterday = datetime.now().date() - timedelta(days=1)
    try:
        rows = fetch_day_bill(ak, sk, yesterday)
    except Exception as e:
        return _face("cost", "red", f"费用中心不可达：{str(e)[:80]}",
                     "AK 需加费用中心只读权限（BSS OpenAPI）")
    if rows:
        return _face("cost", "green", f"昨日账单 {len(rows)} 行可读")
    return _face("cost", "yellow", "费用 API 可达，昨日无账单行", "新账号/未出账，属正常")


def _probe_changes(ak: str, sk: str) -> dict:
    """变更面：账号默认 ChangeSource 近 1 小时事件一页（只读，不入库）。"""
    from app.models.cloud_account import CloudAccount
    from app.services.change_source import probe_change_source

    account = CloudAccount(name="安装向导", vendor="aliyun", region="", extra_json="{}")
    try:
        source, batch = probe_change_source(account, ak, sk, hours=1)
    except Exception as e:
        return _face("changes", "red", f"变更审计不可达：{str(e)[:80]}",
                     "检查账号变更源配置与只读权限")
    detail = f"{source.label} 可达（近 1 小时 {batch.fetched} 条事件）"
    if batch.truncated:
        detail += "；仅探一页，未遍历完整窗口"
    return _face("changes", "green", detail)


def probe_five_faces(ak: str, sk: str) -> list:
    """五面探测（各面独立 try/except，一面失败不影响其余）。"""
    faces: list = []

    def _safe(fn, *args):
        try:
            return fn(*args)
        except Exception as e:
            logger.warning("探测失败 %s: %s", getattr(fn, "__name__", "?"), str(e)[:120])
            return None

    assets_face = (_safe(_probe_assets, ak, sk)
                   or _face("assets", "red", "资产 API 不可达（AK 无效或无 ECS 权限）",
                            "检查 AK/SK 与只读权限"))
    faces.append(assets_face)
    faces.append(_safe(_probe_metrics, ak, sk, assets_face)
                 or _face("metrics", "red", "云监控探测失败"))
    faces.append(_safe(_probe_logs, ak, sk) or _face("logs", "red", "SLS 探测失败"))
    faces.append(_safe(_probe_cost, ak, sk) or _face("cost", "red", "费用中心探测失败"))
    faces.append(_safe(_probe_changes, ak, sk) or _face("changes", "red", "操作审计探测失败"))
    return faces


# ---------------------------------------------------------------- 体检报告（本地数据，不调云）


def _metric_series_present() -> bool:
    """近 30 分钟是否有任何指标序列（云监控 ECS / VMware VM / VMware 主机）。

    云监控 ECS、VMware VM、VMware 主机三个通道任一有数据即就绪，避免纯 vCenter
    部署在采集 vmware_* 时仍被判为"指标库无序列"。VM 不可达时抛异常，由调用方呈现红色面。
    """
    from app.collector import vm

    end = int(time.time())
    last_err: Exception | None = None
    for name in ("aliyun_ecs_cpuutilization", "vmware_vm_cpu_usage_percent",
                 "vmware_host_cpu_usage_percent"):
        try:
            if vm.query_range(name, end - 1800, end, 300):
                return True
        except Exception as e:  # noqa: BLE001  首次成功即返回；全失败才上抛
            last_err = e
    if last_err is not None:
        raise last_err
    return False


def _billable_accounts_present(db: Session) -> bool:
    """是否有计费能力账号（云账号表或旧环境变量）。

    纯 vCenter/私有化环境本来就没有账单，不应显示成"账单暂无数据（T+1）"。
    """
    from app.models.cloud_account import CloudAccount
    from app.services.cloud.provider import vendors_with

    n = (db.query(CloudAccount)
         .filter(CloudAccount.enabled.is_(True),
                 CloudAccount.vendor.in_(vendors_with("billing"))).count())
    if n:
        return True
    return bool(os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_ID")
                or os.environ.get("TENCENTCLOUD_SECRET_ID"))


def local_report(db: Session) -> dict:
    """底座体检报告：本地数据面就绪度 + 能力矩阵 + 缺口建议。"""
    from app.models.asset import Asset
    from app.models.change_event import ChangeEvent
    from app.models.cost_daily import CostDaily
    from app.services.log_query import source_entries
    from app.services.sls_targets import configured as sls_configured

    ready: set = set()
    faces: list = []

    # 资产面
    n_assets = db.query(Asset).filter(Asset.status != "Deleted").count()
    if n_assets:
        ready.add("assets")
        faces.append(_face("assets", "green", f"{n_assets} 个资产在库（最近一次同步）"))
    else:
        faces.append(_face("assets", "red", "资产库为空", "走安装向导填 AK 或点一次资产同步"))

    # 指标面：云监控 / VMware / Agent 任一有数据即算就绪
    try:
        if _metric_series_present():
            ready.add("metrics")
            faces.append(_face("metrics", "green", "指标在采（VictoriaMetrics 有序列）"))
        else:
            faces.append(_face("metrics", "yellow", "指标库无序列",
                               "等一个采集周期（5 分钟）；仍为空时检查云监控权限、"
                               "vCenter 统计级别或 Agent 上报"))
    except Exception:
        faces.append(_face("metrics", "red", "指标库（VictoriaMetrics）不可达",
                           "检查 VM_URL 配置与容器状态"))

    # 日志面：与日志查询页同源——有可查询日志源即就绪（阿里云凭证可用时 SLS 隐式源
    # 也能查）。SLS 目标只是"日志→链路/应用关联分析"的增强，不作为未接入的判据。
    ready_logs = [e for e in source_entries(db) if e.get("status") == "ready"]
    if ready_logs:
        ready.add("logs")
        names = "、".join(e.get("name") or e.get("type") or "?" for e in ready_logs)
        face = _face("logs", "green", f"可查询日志源 {len(ready_logs)} 个（{names}）")
        if not sls_configured():
            face["hint"] = ("如需 SLB/应用日志关联分析，到 安装向导 选择 SLS "
                            "project/logstore（查询本身已可用）")
        faces.append(face)
    else:
        faces.append(_face("logs", "red", "无可查询日志源",
                           "云账号管理添加/启用只读阿里云账号（自动复用其凭证查 SLS），"
                           "或到 集成中心-数据源 登记 Loki/ES/腾讯云 CLS"))

    # 账单面：近 3 天有账单行即就绪；没有计费类账号时不适用（不是"缺账单"）
    try:
        n_cost = (db.query(CostDaily)
                  .filter(CostDaily.stat_date >= datetime.now().date() - timedelta(days=3))
                  .count())
        if n_cost:
            ready.add("cost")
            faces.append(_face("cost", "green", f"账单在采（近 3 天 {n_cost} 行）"))
        elif _billable_accounts_present(db):
            faces.append(_face("cost", "yellow", "账单暂无数据（T+1，次日 10 点采集）",
                               "新接入账号属正常；持续为空时检查费用中心只读权限"))
        else:
            faces.append(_face("cost", "yellow", "未配置计费类云账号（账单不适用）",
                               "vCenter/私有化环境没有账单数据；接入阿里云/腾讯云账号后自动采集"))
    except Exception:
        faces.append(_face("cost", "red", "账单库不可读"))

    # 变更面
    try:
        n_changes = (db.query(ChangeEvent)
                     .filter(ChangeEvent.event_time >= datetime.now() - timedelta(days=7))
                     .count())
        if n_changes:
            ready.add("changes")
            faces.append(_face("changes", "green", f"审计在采（近 7 天 {n_changes} 条变更）"))
        else:
            faces.append(_face("changes", "yellow", "近 7 天无变更记录",
                               "等采集周期，或到云账号管理检查变更源配置与只读权限"))
    except Exception:
        faces.append(_face("changes", "red", "变更库不可读"))

    return {
        "faces": faces,
        "capabilities": capability_readiness(ready),
        "gaps": gap_suggestions(ready),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
