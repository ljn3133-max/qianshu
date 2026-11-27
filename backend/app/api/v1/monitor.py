"""监控指标查询 API（云监控直查 + VictoriaMetrics 历史趋势，只读）。"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.core.client_ip import get_client_ip
from app.collector.cms import (METRIC_CONFIG, VMWARE_METRIC_CONFIG, get_metric_config,
                               query_instance_metrics)
from app.collector.credential import CredentialError, resolve_aliyun_credential
from app.collector.metrics_job import get_status as collect_status
from app.collector.metrics_job import get_vsphere_status
from app.collector.metrics_job import run_collection
from app.collector.vm import metric_name as vm_metric_name
from app.collector.vm import query_range as vm_query_range
from app.core.db import get_db
from app.core.deps import require_roles
from app.models.asset import Asset
from app.models.user import User
from app.services import audit

logger = logging.getLogger("qianshu.monitor")
router = APIRouter(prefix="/monitor", tags=["monitor"])

# 历史档位（读 VictoriaMetrics 本地库）: range -> (秒, 查询步长秒)
HISTORY_RANGES = {
    "7d": (7 * 86400, 1800),
    "30d": (30 * 86400, 3600),
}

# VMware 指标只存在于本地 VM（vSphere 采集器写入），没有 CMS 直查通道；
# 四档全部走 VM，避免告警页点进去没有曲线。
VM_RANGES = {
    "1h": (3600, 60),
    "24h": (86400, 300),
    "7d": (7 * 86400, 1800),
    "30d": (30 * 86400, 3600),
}


def _query_vmware(product: str, instance_id: str, range_key: str) -> list:
    """VMware 资产指标（CPU/内存）从 VictoriaMetrics 查，返回与 CMS 同构的结构。"""
    seconds, step = VM_RANGES[range_key]
    end_s = int(time.time())
    start_s = end_s - seconds
    metrics = []
    for metric, label, unit in VMWARE_METRIC_CONFIG[product]:
        entry = {"metric": metric, "label": label, "unit": unit, "series": []}
        promql = f'{vm_metric_name(product, metric)}{{instance_id="{instance_id}"}}'
        try:
            result = vm_query_range(promql, start_s, end_s, step)
        except Exception as e:
            logger.warning("VM 查询失败 %s/%s: %s", product, metric, str(e)[:120])
            metrics.append(entry)
            continue
        for series in result:
            name = series.get("metric", {}).get("node", "")
            points = [[int(float(ts)) * 1000, float(v)] for ts, v in series.get("values", [])]
            entry["series"].append({"name": name, "points": points})
        metrics.append(entry)
    return metrics


@router.get("/config")
def metric_config(
    product: str = Query(""),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    try:
        return get_metric_config(product or None)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/collect/status")
def get_collect_status(
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    status = collect_status()
    # vSphere 是独立采集通道：0 样本时状态里带可行动警告（UI 解释"接了 vCenter 为什么没图"）
    status["vsphere"] = get_vsphere_status()
    return status


@router.post("/collect/run")
def trigger_collect(
    request: Request,
    product: str = Query("", description="只采该产品（空=全部）；VMware 传 vmware_vm/vmware_host"),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "operator")),
):
    """手动触发一轮指标采集：指定 product 时只采该产品，与当前页签结果一致。

    product 为空保持全量采集（老调用方兼容）；VMware 页签传 vmware_vm/vmware_host，
    避免全量采集只返回 ECS/RDS 点数而让人误以为 VMware 已出数。
    """
    try:
        result = run_collection(db, product=product or None, force=True)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.exception("手动采集失败")
        raise HTTPException(502, f"采集失败: {str(e)[:200]}")
    audit.record(
        db, action="metrics.collect", user_id=user.id, username=user.username,
        resource_type="metrics", resource_id=product or "all", detail=result,
        client_ip=get_client_ip(request),
    )
    db.commit()
    return result

@router.get("/{product}/{instance_id}")
def instance_metrics(
    product: str,
    instance_id: str,
    range: str = Query("24h"),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    is_vmware = product in VMWARE_METRIC_CONFIG
    if range not in (VM_RANGES if is_vmware else {"1h": 0, "24h": 0, "7d": 0}):
        raise HTTPException(400, "range 仅支持 1h/24h/7d" if not is_vmware else "range 仅支持 1h/24h/7d/30d")
    # 从资产表取 region（CMS 需要 region 端点），同时校验实例存在
    asset = (
        db.query(Asset)
        .filter(Asset.product == product, Asset.instance_id == instance_id)
        .first()
    )
    if asset is None:
        raise HTTPException(404, f"资产不存在: {product}/{instance_id}")

    # VMware 指标只在本地 VM，不依赖阿里云凭证
    if is_vmware:
        return {
            "product": product, "instance_id": instance_id, "range": range,
            "instance_name": asset.name,
            "metrics": _query_vmware(product, instance_id, range),
        }

    try:
        ak, sk, _source = resolve_aliyun_credential(db)
    except CredentialError:
        # 未配置凭证属于正常空态：返回可行动说明，不抛 500
        return {"product": product, "instance_id": instance_id, "range": range,
                "instance_name": asset.name, "metrics": [],
                "note": "未配置阿里云凭证：请到「云账号管理」添加只读账号后查看指标",
                "no_credential": True}
    try:
        metrics = query_instance_metrics(ak, sk, product, instance_id, asset.region, range)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.exception("指标查询失败")
        raise HTTPException(502, f"指标查询失败: {str(e)[:200]}")
    return {
        "product": product,
        "instance_id": instance_id,
        "range": range,
        "instance_name": asset.name,
        "metrics": metrics,
    }


@router.get("/history/{product}/{instance_id}")
def instance_history(
    product: str,
    instance_id: str,
    range: str = Query("30d"),
    db: Session = Depends(get_db),
    _user: User = Depends(require_roles("admin", "operator", "viewer")),
):
    """历史趋势（读 VictoriaMetrics 本地采集数据，与直查接口同构返回）。"""
    ranges = VM_RANGES if product in VMWARE_METRIC_CONFIG else HISTORY_RANGES
    range_cfg = ranges.get(range)
    metric_defs = VMWARE_METRIC_CONFIG.get(product) or METRIC_CONFIG.get(product)
    if not metric_defs or not range_cfg:
        raise HTTPException(400, f"不支持的历史档位: {range}（可选: {list(ranges)}）")
    asset = (
        db.query(Asset)
        .filter(Asset.product == product, Asset.instance_id == instance_id)
        .first()
    )
    if asset is None:
        raise HTTPException(404, f"资产不存在: {product}/{instance_id}")

    seconds, step = range_cfg
    end_s = int(time.time())
    start_s = end_s - seconds
    metrics = []
    for metric, label, unit in metric_defs:
        promql = f'{vm_metric_name(product, metric)}{{instance_id="{instance_id}"}}'
        entry = {"metric": metric, "label": label, "unit": unit, "series": []}
        try:
            result = vm_query_range(promql, start_s, end_s, step)
        except Exception as e:
            logger.warning("VM 历史查询失败 %s/%s: %s", product, metric, str(e)[:120])
            metrics.append(entry)
            continue
        for series in result:
            name = series.get("metric", {}).get("node", "")
            points = [[int(float(ts)) * 1000, float(v)] for ts, v in series.get("values", [])]
            entry["series"].append({"name": name, "points": points})
        # 维度名转图例名（TCP state 等）；旧回填产生的无名合并曲线在有命名曲线时丢弃
        from app.collector.cms import series_display_name
        for s in entry["series"]:
            s["name"] = series_display_name(s["name"]) if s["name"] else ""
        if any(s["name"] for s in entry["series"]):
            entry["series"] = [s for s in entry["series"] if s["name"]]
        entry["series"].sort(key=lambda s: s["name"])
        metrics.append(entry)
    return {
        "product": product,
        "instance_id": instance_id,
        "range": range,
        "instance_name": asset.name,
        "metrics": metrics,
    }

