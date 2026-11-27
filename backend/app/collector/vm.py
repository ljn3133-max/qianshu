"""VictoriaMetrics 读写封装（PromQL / Prometheus 文本协议兼容）。"""
from __future__ import annotations

import json
import logging
import time

import httpx

from app.core.config import settings

logger = logging.getLogger("qianshu.vm")


def _url(path: str) -> str:
    return f"{settings.VM_URL.rstrip('/')}{path}"


def write_prometheus(lines: list) -> bool:
    """批量写入 Prometheus 文本格式（行带 ms 时间戳）。失败返回 False（不抛）。"""
    if not lines:
        return True
    try:
        resp = httpx.post(
            _url("/api/v1/import/prometheus"),
            content="\n".join(lines) + "\n",
            headers={"Content-Type": "text/plain"},
            timeout=15,
        )
        if resp.status_code != 204 and resp.status_code != 200:
            logger.warning("VM 写入失败: %s %s", resp.status_code, resp.text[:200])
            return False
        return True
    except Exception as e:
        logger.warning("VM 写入异常: %s", str(e)[:200])
        return False


def query_range(promql: str, start_s: int, end_s: int, step_s: int) -> list:
    """range 查询。返回 [{metric: {...labels}, values: [[ts_s, "v"], ...]}]；失败抛异常。

    start 自动夹到 VM 保留窗口（retention.clamp_start_ts），设置变更在下一次查询生效；
    夹出空窗直接返回 []。
    """
    from app.services import retention

    start_s = retention.clamp_start_ts("vm", start_s)
    if start_s >= end_s:
        return []
    resp = httpx.get(
        _url("/api/v1/query_range"),
        params={"query": promql, "start": start_s, "end": end_s, "step": step_s},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "success":
        raise RuntimeError(f"VM 查询失败: {data.get('error', '')[:200]}")
    return data.get("data", {}).get("result", [])


def export_samples(match: str, start_s: float, end_s: float) -> list:
    """导出窗口内原始样本（含真实 ms 时间戳）；失败抛异常。

    接入回读必须核对“本轮写入的原始点”，不能用 query_range 的评估网格时间戳
    做相等匹配：range 查询返回的是 step 网格时间，不是样本自身的 timestamp。
    """
    resp = httpx.get(
        _url("/api/v1/export"),
        params={"match[]": match, "start": int(start_s), "end": int(end_s)},
        timeout=60,
    )
    resp.raise_for_status()
    rows = []
    for line in (resp.text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError as exc:
            raise RuntimeError(f"VM export 返回非 JSON 行: {line[:120]}") from exc
        if isinstance(item, dict):
            rows.append(item)
    return rows


def list_series(match: str, start_s: float, end_s: float) -> list:
    """枚举 [start, end] 窗口内有样本的序列，返回完整标签集列表 [{...}, ...]。

    VM v1.102.0 支持 match[]/start/end，`{__name__=~".+"}` 可用于全量枚举。"""
    resp = httpx.get(
        _url("/api/v1/series"),
        params={"match[]": match, "start": int(start_s), "end": int(end_s)},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "success":
        raise RuntimeError(f"VM series 查询失败: {str(data.get('error', ''))[:200]}")
    return data.get("data") or []


def series_matcher(labels: dict) -> str:
    """完整标签集 -> 精确序列选择器（钉死单条序列；值做反斜杠/引号转义）。"""
    def _esc(s: str) -> str:
        return str(s).replace("\\", "\\\\").replace('"', '\\"')
    return "{" + ",".join(f'{k}="{_esc(v)}"' for k, v in sorted(labels.items())) + "}"


def delete_series(matchers: list) -> bool:
    """整序列删除（返回是否 204）。

    delete_series **不支持时间窗**（v1.102.0 会拒绝 start/end 参数；官方文档
    "Delete API doesn't support the deletion of specific time ranges"），只能整条序列删除。
    因此调用方必须先用 list_series 排除仍有新样本的活序列（engine_retention 死序列清理），
    避免误删未超期数据。"""
    if not matchers:
        return True
    try:
        resp = httpx.post(
            _url("/api/v1/admin/tsdb/delete_series"),
            params=[("match[]", m) for m in matchers],
            timeout=60,
        )
        return resp.status_code == 204
    except Exception as e:
        logger.warning("VM delete_series 异常: %s", str(e)[:200])
        return False


def health() -> bool:
    try:
        return httpx.get(_url("/health"), timeout=5).status_code == 200
    except Exception:
        return False


def metric_name(product: str, cms_metric: str) -> str:
    """CMS 指标名 -> VM 指标名：aliyun_<product>_<metric小写>。

    vmware_* 是 vSphere 采集器原生写入的序列名（vmware_vm_cpu_usage_percent /
    vmware_host_mem_usage_percent），不能套阿里云前缀。
    """
    if product.startswith("vmware_"):
        return f"{product}_{cms_metric.lower()}"
    return f"aliyun_{product}_{cms_metric.lower()}"
