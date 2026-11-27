"""云监控(CMS)指标查询：监控页签直查，不落库；历史指标由采集任务写入 VictoriaMetrics。

注意：
- ECS 的 CPU 指标名是 CPUUtilization（不是 CPUUtil）。
- 集群架构 Redis 实例只有 Sharding* 系列指标，且按 nodeId 返回多条曲线。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

logger = logging.getLogger("qianshu.cms")

NAMESPACE = {
    "ecs": "acs_ecs_dashboard",
    "rds": "acs_rds_dashboard",
    "redis": "acs_kvstore",
    "slb": "acs_slb_dashboard",
}

# 每个产品默认展示的指标：(metric_name, 中文标签, 单位)
# ECS 的内存/磁盘是云监控 agent 指标：memory_usedutilization 为单曲线；
# diskusage_utilization 按分区返回多曲线，带 device/diskname 字段。
METRIC_CONFIG = {
    "ecs": [
        ("CPUUtilization", "CPU使用率", "%"),
        ("memory_usedutilization", "内存使用率", "%"),
        ("diskusage_utilization", "磁盘使用率", "%"),
        ("InternetOutRate", "公网出流量", "bit/s"),
        ("InternetInRate", "公网入流量", "bit/s"),
        # 内网出入带宽 + TCP 连接数
        # 指标名实测过：concurrentConnections/ConnectionUtilization 虽在元数据里但返回全 None，
        # net_tcpconnection 真实出数（count）；内网=IntranetInRate/IntranetOutRate（bit/s）
        ("IntranetInRate", "内网入流量", "bit/s"),
        ("IntranetOutRate", "内网出流量", "bit/s"),
        ("net_tcpconnection", "TCP连接数", "count"),
    ],
    "rds": [
        ("CpuUsage", "CPU使用率", "%"),
        ("MemoryUsage", "内存使用率", "%"),
        ("DiskUsage", "磁盘使用率", "%"),
        ("ConnectionUsage", "连接数使用率", "%"),
        ("IOPSUsage", "IOPS使用率", "%"),
    ],
    "redis": [
        ("ShardingCpuUsage", "CPU使用率", "%"),
        ("ShardingMemoryUsage", "内存使用率", "%"),
        ("ShardingConnectionUsage", "连接数使用率", "%"),
        ("ShardingAvgRt", "平均响应时间", "ms"),
    ],
    "slb": [
        ("InstanceQps", "QPS", "count/s"),
        ("InstanceRt", "请求RT", "ms"),
        ("InstanceUpstreamRt", "后端RT", "ms"),
        ("InstanceStatusCode5xx", "5xx状态码", "count/s"),
        ("InstanceActiveConnection", "活跃连接", "count"),
        ("InstanceNewConnection", "新建连接", "count/s"),
    ],
}

# 告警规则可选的指标集：CMS 产品 + vSphere 原生指标。
# 与 METRIC_CONFIG 分开：METRIC_CONFIG 是 CMS 采集白名单（metrics_job 按 NAMESPACE
# 直查阿里云 CMS），把 vmware_* 混进去会让采集循环把它们当 CMS 产品查询；
# vmware_* 指标由 services/vsphere.py 写入 VM（vmware_vm_* / vmware_host_* 原生序列名），
# 规则评估按 vm.metric_name 前缀豁免拼接。磁盘是绝对量（bytes）不适用 90% 阈值，
# 故不提供磁盘指标规则。
VMWARE_METRIC_CONFIG = {
    "vmware_vm": [
        ("cpu_usage_percent", "CPU使用率", "%"),
        ("mem_usage_percent", "内存使用率", "%"),
    ],
    "vmware_host": [
        ("cpu_usage_percent", "CPU使用率", "%"),
        ("mem_usage_percent", "内存使用率", "%"),
    ],
}

ALERT_METRIC_CONFIG = {**METRIC_CONFIG, **VMWARE_METRIC_CONFIG}

# range -> (秒数, 查询粒度 period 秒)
# 多维度曲线的显示名（net_tcpconnection 按 state 分 4 条：不命名图上就是无名曲线）
STATE_LABELS = {
    "TCP_TOTAL": "总连接",
    "ESTABLISHED": "已建立",
    "NON_ESTABLISHED": "非已建立(TIME_WAIT等)",
    "LISTEN": "监听",
}


def series_display_name(raw: str) -> str:
    """维度值 -> 图例名（认识的给中文，不认识的原样）。"""
    return STATE_LABELS.get(raw, raw)


RANGE_CONFIG = {
    "1h": (3600, "60"),
    "24h": (86400, "900"),
    "7d": (7 * 86400, "900"),
}


def _client(ak: str, sk: str, region_id: str):
    from alibabacloud_cms20190101.client import Client
    from alibabacloud_tea_openapi.models import Config

    cfg = Config(access_key_id=ak, access_key_secret=sk, region_id=region_id)
    cfg.endpoint = f"metrics.{region_id}.aliyuncs.com"
    return Client(cfg)


def _query_one(client, namespace: str, metric: str, instance_id: str,
               start_ms: int, end_ms: int, period: str) -> list:
    """单指标查询，返回原始 datapoints 列表。"""
    from alibabacloud_cms20190101 import models as cms

    req = cms.DescribeMetricListRequest(
        namespace=namespace,
        metric_name=metric,
        dimensions=json.dumps([{"instanceId": instance_id}]),
        period=period,
        start_time=str(start_ms),
        end_time=str(end_ms),
    )
    body = client.describe_metric_list(req).body
    if body.code and str(body.code) != "200":
        raise RuntimeError(f"CMS code={body.code} {body.message}")
    return json.loads(body.datapoints or "[]")


def query_instance_metrics(
    ak: str, sk: str, product: str, instance_id: str, region_id: str,
    range_key: str = "24h",
) -> list:
    """查一个实例在该产品默认指标集下的数据。

    返回 [{metric, label, unit, series: [{name, points: [[ts, avg], ...]}]}]
    多条曲线用于集群 Redis（按 nodeId 分片）和 ECS 磁盘（按分区 diskname）。
    """
    namespace = NAMESPACE.get(product)
    metrics = METRIC_CONFIG.get(product)
    range_cfg = RANGE_CONFIG.get(range_key)
    if not namespace or not metrics or not range_cfg:
        raise ValueError(f"不支持的 product/range: {product}/{range_key}")

    seconds, period = range_cfg
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - seconds * 1000
    client = _client(ak, sk, region_id or "cn-hangzhou")

    result = []
    for metric, label, unit in metrics:
        entry = {"metric": metric, "label": label, "unit": unit, "series": []}
        try:
            datapoints = _query_one(client, namespace, metric, instance_id, start_ms, end_ms, period)
        except Exception as e:
            # 单指标失败不拖垮整组；前端显示空图
            logger.warning("CMS 查询失败 %s/%s: %s", product, metric, str(e)[:120])
            result.append(entry)
            continue
        series_map: dict = {}
        for dp in datapoints:
            # 多维度分曲线：Redis 集群按 nodeId 分片、ECS 磁盘按分区（diskname=挂载点）、
            # TCP 连接数按 state（总/已建立/非已建立/监听）——维度字段不命名图上就是无名曲线
            # 其余实例归并为一条
            name = dp.get("nodeId") or dp.get("diskname") or dp.get("state") or instance_id
            pts = series_map.setdefault(name, [])
            avg = dp.get("Average")
            if avg is not None:
                pts.append([dp.get("timestamp", 0), avg])
        for name, pts in series_map.items():
            if name == instance_id:
                label_name = ""
            else:
                label_name = series_display_name(name.replace(f"{instance_id}-", ""))
            entry["series"].append({"name": label_name, "points": sorted(pts, key=lambda p: p[0])})
        result.append(entry)
    return result


def get_metric_config(product: Optional[str] = None):
    """返回 {product: [{metric,label,unit}]}，可指定单个产品。

    返回 ALERT_METRIC_CONFIG（CMS + vSphere 原生指标），消费方是告警规则表单
    （/monitor/config）；监控直查/历史仍走 METRIC_CONFIG——vSphere 指标由
    services/vsphere.py 写入 VM，不经 CMS 查询。
    """
    if product:
        if product not in ALERT_METRIC_CONFIG:
            raise ValueError(f"不支持的 product: {product}")
        return {product: [
            {"metric": m, "label": l, "unit": u} for m, l, u in ALERT_METRIC_CONFIG[product]
        ]}
    return {p: [{"metric": m, "label": l, "unit": u} for m, l, u in lst]
            for p, lst in ALERT_METRIC_CONFIG.items()}
