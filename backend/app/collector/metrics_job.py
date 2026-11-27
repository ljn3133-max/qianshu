"""指标采集任务：云监控 CMS -> VictoriaMetrics 定时落库。

策略：
- 白名单制：复用 cms.METRIC_CONFIG 的核心指标；
- 批量查询：一个 (产品, 指标) 一次 CMS 调用（dimensions 带全部实例 ID）；
- 令牌桶限速：默认 10 QPS；
- 指标名 aliyun_<product>_<metric小写>，label: instance_id/region/product/node。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime

from sqlalchemy.orm import Session

from app.collector.cms import METRIC_CONFIG, NAMESPACE
from app.collector.credential import resolve_aliyun_credential
from app.collector.vm import export_samples, metric_name, write_prometheus
from app.models.asset import Asset

logger = logging.getLogger("qianshu.metrics_job")

# 接入回读：等待 VM importer 可见的上限（秒）。超时按实际读到结果判定。
_READBACK_DEADLINE_S = 20.0


class TokenBucket:
    """简单令牌桶限速（采集用，非关键路径）。"""

    def __init__(self, rate: float):
        self.rate = rate
        self.tokens = rate
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self):
        with self.lock:
            now = time.monotonic()
            self.tokens = min(self.rate, self.tokens + (now - self.last) * self.rate)
            self.last = now
            if self.tokens >= 1:
                self.tokens -= 1
                return 0.0
            wait = (1 - self.tokens) / self.rate
            return wait


# 最近一次采集状态（供页面/API 展示）
_last_status: dict = {
    "running": False,
    "last_started_at": None,
    "last_finished_at": None,
    "last_duration_s": None,
    "points_written": 0,
    "errors": [],
    "scope": "",   # 最近一次采集范围：all / 单个产品
}


def get_status() -> dict:
    return dict(_last_status)


def _build_client(ak: str, sk: str, region_id: str):
    from alibabacloud_cms20190101.client import Client
    from alibabacloud_tea_openapi.models import Config

    cfg = Config(access_key_id=ak, access_key_secret=sk, region_id=region_id)
    cfg.endpoint = f"metrics.{region_id}.aliyuncs.com"
    return Client(cfg)


def _query_batch(client, namespace: str, metric: str, instance_ids: list,
                 start_ms: int, end_ms: int, period: str) -> list:
    """一次查询多个实例的同一指标。"""
    from alibabacloud_cms20190101 import models as cms

    dims = [{"instanceId": i} for i in instance_ids]
    req = cms.DescribeMetricListRequest(
        namespace=namespace,
        metric_name=metric,
        dimensions=json.dumps(dims),
        period=period,
        start_time=str(start_ms),
        end_time=str(end_ms),
    )
    body = client.describe_metric_list(req).body
    if body.code and str(body.code) != "200":
        raise RuntimeError(f"CMS code={body.code} {body.message}")
    return json.loads(body.datapoints or "[]")


def _escape_label(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


# 采集互斥：自动循环与手动触发不并发
_run_lock = threading.Lock()


def run_collection(db: Session, product: str | None = None,
                   force: bool = False) -> dict:
    """执行一轮采集（与进行中的一轮互斥）。返回统计信息。

    product 按产品限定采集范围：
      - vmware_vm / vmware_host：只走 vSphere 通道，只写对应前缀的指标，
        不再连带触发 ECS/RDS 等云监控采集；
      - 其他 METRIC_CONFIG 产品：只采该产品资产；空=采全部（自动循环原口径）。
    force=True 为手动触发：绕过数据源优先级节奏，但只产生数据、不产生额外提醒。
    """
    if product in ("vmware_vm", "vmware_host"):
        return run_vsphere_collection(db, product=product, force=force)
    if product and product not in METRIC_CONFIG:
        raise ValueError(f"不支持的产品: {product}")
    if not _run_lock.acquire(blocking=False):
        return {**dict(_last_status), "skipped": "上一轮采集仍在进行"}
    try:
        result = _run_collection_locked(db, product=product)
    finally:
        _run_lock.release()
    result["scope"] = product or "all"
    # 画像事件补录兜底扫表：异动检出已由
    # cost_anomaly._add 的主动钩子秒级入线，这里只兜 alert_records / change_events
    # 与未挂钩落库路径（~5 分钟一轮，节流在 event_tick 内）；异常绝不影响指标采集
    try:
        from app.services.profile_engine import event_tick
        r = event_tick(db)
        if r.get("recorded"):
            logger.info("画像事件补录: %s", r)
    except Exception:
        logger.warning("画像事件补录异常", exc_info=True)
    return result


def _run_collection_locked(db: Session, product: str | None = None,
                           include_evidence: bool = False,
                           resource_ids: list[str] | None = None, region: str = "") -> dict:
    from app.core.config import settings

    _last_status["running"] = True
    _last_status["last_started_at"] = datetime.now().isoformat()
    _last_status["errors"] = []
    _last_status["scope"] = product or "all"
    started = time.monotonic()

    ak, sk, _source = resolve_aliyun_credential(db)
    bucket = TokenBucket(settings.METRICS_COLLECT_QPS)
    interval = settings.METRICS_COLLECT_INTERVAL
    period = str(interval if interval in (15, 60, 300, 900) else 300)
    end_ms = int(time.time() * 1000)
    # 窗口取 2 倍周期，容忍晚到的数据点（重复点 VM 幂等覆盖）
    start_ms = end_ms - interval * 2 * 1000

    # 按产品分组取资产（region 也影响 CMS 端点，按 region 再分组）
    # 多账号：账号上下文激活时只采本账号资产——CMS 是账号维度 API，
    # 拿 A 账号凭证查 B 账号实例会空转/报错（与 mark_missing_deleted 同口径收口）
    from app.collector.credential import active_account_id

    q = db.query(Asset).filter(Asset.status != "Deleted")
    acc = active_account_id()
    if acc is not None:
        q = q.filter(Asset.account_id == acc)
    if resource_ids:
        q = q.filter(Asset.instance_id.in_(resource_ids))
    assets = q.all()
    if region:
        assets = [a for a in assets if (a.region or "cn-hangzhou") == region]
    groups: dict = {}  # (product, region) -> [instance_id]
    for a in assets:
        if a.product in METRIC_CONFIG and (not product or a.product == product):
            groups.setdefault((a.product, a.region or "cn-hangzhou"), []).append(a.instance_id)

    points_total = 0
    lines_all: list = []
    evidence: list[dict] = []
    for (product, region), instance_ids in sorted(groups.items()):
        client = _build_client(ak, sk, region)
        for metric, _label, _unit in METRIC_CONFIG[product]:
            wait = bucket.acquire()
            if wait > 0:
                time.sleep(wait)
            try:
                datapoints = _query_batch(
                    client, NAMESPACE[product], metric, instance_ids, start_ms, end_ms, period
                )
            except Exception as e:
                msg = f"{product}/{metric}@{region}: {str(e)[:120]}"
                _last_status["errors"].append(msg)
                logger.warning("指标采集失败 %s", msg)
                continue
            for dp in datapoints:
                avg = dp.get("Average")
                if avg is None:
                    continue
                name = metric_name(product, metric)
                # 子实例序列标签：Redis 集群分片(nodeId) / ECS 磁盘分区(diskname=挂载点) /
                # TCP 连接数状态(state=总连接/已建立/非已建立/监听)。
                # 不加标签时同实例多维度的点会同标签互相覆盖，告警与图表都会失真。
                node = dp.get("nodeId", "") or dp.get("diskname", "") or dp.get("state", "")
                labels = [
                    f'instance_id="{_escape_label(dp.get("instanceId", ""))}"',
                    f'region="{_escape_label(region)}"',
                    f'product="{product}"',
                ]
                if node:
                    labels.append(f'node="{_escape_label(node)}"')
                lines_all.append(
                    f'{name}{{{",".join(labels)}}} {avg} {dp.get("timestamp", 0)}'
                )
                if include_evidence:
                    evidence.append({
                        "metric": name, "instance_id": str(dp.get("instanceId") or ""),
                        "region": region, "product": product,
                        "timestamp": int(dp.get("timestamp") or 0), "value": str(avg),
                    })
                points_total += 1

    ok = write_prometheus(lines_all)
    if not ok:
        _last_status["errors"].append("VictoriaMetrics 写入失败")

    _last_status["running"] = False
    _last_status["last_finished_at"] = datetime.now().isoformat()
    _last_status["last_duration_s"] = round(time.monotonic() - started, 1)
    _last_status["points_written"] = points_total if ok else 0
    logger.info(
        "指标采集完成: %d 组资产, %d 数据点, 耗时 %.1fs, 错误 %d",
        len(groups), points_total, _last_status["last_duration_s"], len(_last_status["errors"]),
    )
    result = dict(_last_status)
    if include_evidence:
        result["_evidence"] = evidence
        result["_write_ok"] = ok
        result["_window_start_ms"] = start_ms
        result["_window_end_ms"] = end_ms
    return result


def collect_account(db: Session, account, *, resource_ids: list[str] | None = None,
                    region: str = "") -> dict:
    """新手接入专用：显式账号采集，并验证当次写入可由 VM 按资源回读。"""
    from app.collector.credential import account_credential
    from app.core.security import decrypt_secret

    if account is None or not account.enabled or account.vendor != "aliyun":
        raise ValueError("指标账号不存在、已停用或不是阿里云账号")
    assets = (db.query(Asset)
              .filter(Asset.account_id == account.id, Asset.status != "Deleted",
                      Asset.product.in_(METRIC_CONFIG)).all())
    if resource_ids:
        assets = [a for a in assets if a.instance_id in resource_ids]
    if region:
        assets = [a for a in assets if (a.region or "cn-hangzhou") == region]
    owned = {(a.product, a.region or "cn-hangzhou", a.instance_id) for a in assets}
    base = {"account_id": account.id, "discovered": len(owned)}
    if not _run_lock.acquire(blocking=False):
        return {**base, "lock_waiting": True}
    try:
        ak = decrypt_secret(account.ak_cipher) if account.ak_cipher else ""
        sk = decrypt_secret(account.secret_cipher) if account.secret_cipher else ""
        if not ak or not sk:
            raise ValueError("该账号缺少指标采集凭证")
        with account_credential(ak, sk, account.id, source=f"cloud_account:{account.name}"):
            collected = _run_collection_locked(
                db, include_evidence=True, resource_ids=resource_ids, region=region)
    finally:
        _run_lock.release()

    errors = list(collected.get("errors") or [])
    lowered = " ".join(errors).lower()
    denied = any(x in lowered for x in ("403", "permission", "forbidden", "unauthorized"))
    if not collected.get("_write_ok"):
        return {**base, "lock_waiting": False, "ok": False, "data_state": "unknown",
                "reason_code": "vm_write_failed", "retryable": True, "checked": 0,
                "unknown": len(owned), "points_written": 0, "points_read": 0,
                "evidence_refs": [], "observed_until": None, "errors": errors}

    # 只接受当次 CMS 返回且属于本账号资产的 (metric, resource, region, timestamp)。
    expected = {}
    invalid_identity = 0
    for item in collected.get("_evidence") or []:
        key = (item["metric"], item["product"], item["region"], item["instance_id"])
        if not item["instance_id"] or (item["product"], item["region"], item["instance_id"]) not in owned:
            invalid_identity += 1
            continue
        expected.setdefault(key, set()).add(item["timestamp"])
    matched_ids: set[str] = set()
    matched_points: set[tuple[str, str, int]] = set()
    observed_until = None
    readback_errors: list[str] = []
    # 按产品/区域取窗口内原始样本，再在 Python 里按 instance_id 过滤：
    # 不把实例 ID 拼进 PromQL 正则——re.escape 会把 i-bp... 的 '-' 写成 '\-'，
    # 在 PromQL 字符串字面量里是非法转义，VM 会直接 422（接入指标永远无法就绪）。
    groups: dict[tuple, dict[str, set[int]]] = {}
    for (name, product, region, instance_id), timestamps in expected.items():
        groups.setdefault((name, product, region), {})[instance_id] = timestamps
    expected_points = sum(len(timestamps) for timestamps in expected.values())
    # VM 的 import 到可查询有秒级可见性延迟（实测 v1.102.0 约 3 秒）：
    # 写完立即回读会读不到本轮点、被误判 no_metric_data。这里短轮询到所有点可见，
    # 超时仍按真实读到的结果判定（不把“没等到”伪装成成功）。
    deadline = time.monotonic() + _READBACK_DEADLINE_S
    delay = 0.5
    if groups:
        while True:
            readback_errors = []
            for (name, product, region), by_instance in sorted(groups.items()):
                selector = (f'{name}{{product="{_escape_label(product)}",'
                            f'region="{_escape_label(region)}"}}')
                timestamps = [ts for values in by_instance.values() for ts in values]
                start_s = min(min(timestamps), collected["_window_start_ms"]) // 1000 - 60
                end_s = max(max(timestamps), collected["_window_end_ms"]) // 1000 + 60
                try:
                    series = export_samples(selector, start_s, end_s)
                except Exception as exc:
                    readback_errors.append(f"{name}@{region}: {str(exc)[:120]}")
                    continue
                for row in series:
                    labels = row.get("metric") or {}
                    instance_id = str(labels.get("instance_id") or "")
                    if (instance_id not in by_instance
                            or labels.get("product") != product
                            or labels.get("region") != region):
                        continue
                    expected_ts = by_instance[instance_id]
                    values = row.get("values") or []
                    for timestamp, _value in zip(row.get("timestamps") or [], values):
                        # export 返回样本自身 timestamp（ms），与 CMS 原始点同一口径。
                        timestamp_ms = int(timestamp)
                        if timestamp_ms not in expected_ts:
                            continue
                        matched_points.add((name, instance_id, timestamp_ms))
                        matched_ids.add(instance_id)
                        observed = datetime.fromtimestamp(timestamp_ms / 1000)
                        observed_until = max(observed_until, observed) if observed_until else observed
            if (len(matched_points) >= expected_points
                    or time.monotonic() >= deadline):
                break
            time.sleep(delay)
            delay = min(delay * 2, 3.0)
    points_read = len(matched_points)

    errors.extend(readback_errors)
    available = points_read > 0
    failed = bool(errors) or invalid_identity > 0
    if readback_errors:
        reason = "vm_readback_failed"
    elif denied:
        reason = "permission_denied"
    elif invalid_identity:
        reason = "resource_identity_incomplete"
    elif errors:
        reason = "metrics_partial_failure"
    elif not owned:
        reason = "no_supported_assets"
    elif not available:
        reason = "no_metric_data"
    else:
        reason = "metrics_verified"
    return {
        **base, "lock_waiting": False, "ok": not failed,
        "data_state": "available" if available else ("unknown" if failed else "no_data"),
        "reason_code": reason, "retryable": not denied,
        "checked": len(matched_ids), "unknown": max(0, len(owned) - len(matched_ids)),
        "points_written": collected.get("points_written") or 0, "points_read": points_read,
        "evidence_refs": [f"metrics:{account.id}:{iid}" for iid in sorted(matched_ids)][:100],
        "observed_until": observed_until, "errors": errors,
        "truncated": failed or len(matched_ids) < len(owned) or len(matched_ids) > 100,
    }


def backfill_metric_history(db: Session, product: str, metrics: list,
                            days: int = 30, period: str = "3600") -> dict:
    """从 CMS 回填历史点进 VictoriaMetrics，为缺少历史点的指标补历史档。

    监控页 1h/24h 档直查 CMS，而"近30天"档读 VM 历史；新采集的指标没有历史点时，
    30 天图为空。本函数把 CMS 历史一次性灌进 VM（行协议与常规采集同构，VM 幂等覆盖）。
    """
    from app.core.config import settings
    ak, sk, _source = resolve_aliyun_credential(db)
    bucket = TokenBucket(settings.METRICS_COLLECT_QPS)
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86400 * 1000

    groups: dict = {}
    from app.collector.credential import active_account_id

    q = db.query(Asset).filter(Asset.status != "Deleted", Asset.product == product)
    acc = active_account_id()
    if acc is not None:
        q = q.filter(Asset.account_id == acc)   # 多账号：只回填本账号资产
    for a in q.all():
        groups.setdefault(a.region or "cn-hangzhou", []).append(a.instance_id)

    written = 0
    errors: list = []
    for region, instance_ids in sorted(groups.items()):
        client = _build_client(ak, sk, region)
        for metric in metrics:
            wait = bucket.acquire()
            if wait > 0:
                time.sleep(wait)
            try:
                datapoints = _query_batch(
                    client, NAMESPACE[product], metric, instance_ids, start_ms, end_ms, period)
            except Exception as e:
                errors.append(f"{product}/{metric}@{region}: {str(e)[:100]}")
                continue
            lines: list = []
            name = metric_name(product, metric)
            for dp in datapoints:
                avg = dp.get("Average")
                if avg is None:
                    continue
                node = dp.get("nodeId", "") or dp.get("diskname", "") or dp.get("state", "")
                labels = [
                    f'instance_id="{_escape_label(dp.get("instanceId", ""))}"',
                    f'region="{_escape_label(region)}"',
                    f'product="{product}"',
                ]
                if node:
                    labels.append(f'node="{_escape_label(node)}"')
                lines.append(f'{name}{{{",".join(labels)}}} {avg} {dp.get("timestamp", 0)}')
                written += 1
                if len(lines) >= 5000:
                    if not write_prometheus(lines):
                        errors.append("VictoriaMetrics 写入失败（分片）")
                    lines = []
            if lines and not write_prometheus(lines):
                errors.append("VictoriaMetrics 写入失败（尾片）")
    logger.info("指标历史回填完成: %s %s×%dd -> %d 点，错误 %d",
                product, ",".join(metrics), days, written, len(errors))
    return {"written": written, "errors": errors}


# ---------------- vSphere（ESXi/vCenter）指标采集 ----------------

_vsphere_status: dict = {
    "running": False,
    "last_finished_at": None,
    "points_written": 0,
    "accounts": 0,
    "errors": [],
    # 采集成功但 0 样本=环境侧问题（统计级别/权限），必须让 UI 能解释"为什么没图"
    "warnings": [],
    # 每个账号当次取到的点数（多账号时定位是谁没数据；UI 排障用）
    "account_details": [],
    "scope": "",   # 当次采集范围：空=全部，vmware_vm/vmware_host=单产品
}

# vSphere 手动采集与自动循环互斥（会话开销大，且避免同一账号并发登录）
_vsphere_lock = threading.Lock()


def get_vsphere_status() -> dict:
    return dict(_vsphere_status)


def run_vsphere_collection(db: Session, product: str | None = None,
                           force: bool = False) -> dict:
    """ESXi/vCenter VM+主机 基础指标 -> VictoriaMetrics（pyvmomi 只读，services/vsphere.py）。

    指标名：vmware_vm_*（虚拟机）与 vmware_host_*（ESXi 主机）的
    cpu_usage_percent / mem_usage_percent / disk_used_bytes。每个启用 esxi/vcenter
    账号一轮，单账号失败不阻断其余。

    product=vmware_vm / vmware_host 时只写对应前缀（用户点哪个页签就只采哪个产品）；
    空=VM 与主机都采（自动循环原口径）。
    """
    if product not in (None, "vmware_vm", "vmware_host"):
        raise ValueError(f"不支持的 VMware 产品: {product}")
    if not _vsphere_lock.acquire(blocking=False):
        return {**dict(_vsphere_status), "skipped": "上一轮 VMware 采集仍在进行"}
    try:
        return _run_vsphere_locked(db, product, force=force)
    finally:
        _vsphere_lock.release()


def _run_vsphere_locked(db: Session, product: str | None,
                        force: bool = False) -> dict:
    from app.core.security import decrypt_secret
    from app.models.cloud_account import CloudAccount
    from app.services.monitor_policy import collect_due, mark_collected
    from app.services.vsphere import (collect_metrics_lines, get_last_perf_diag,
                                      reset_perf_diag)

    _vsphere_status["running"] = True
    _vsphere_status["errors"] = []
    _vsphere_status["warnings"] = []
    _vsphere_status["account_details"] = []
    _vsphere_status["scope"] = product or ""
    points = 0
    accounts = 0
    try:
        rows = (db.query(CloudAccount)
                .filter(CloudAccount.enabled == True,  # noqa: E712
                        CloudAccount.vendor.in_(("esxi", "vcenter")))
                .order_by(CloudAccount.id).all())
        for acc in rows:
            # 分级监控：低优先级账号每天窗口采一次；
            # 手动「采集当前产品」force=True 不受限。
            if not force and not collect_due(db, "metrics", acc.id):
                _vsphere_status["account_details"].append(
                    {"name": acc.name, "points": 0, "error": "",
                     "skipped": "未到采集窗口（低/中优先级节奏）"})
                continue
            accounts += 1
            detail = {"name": acc.name, "points": 0, "error": ""}
            try:
                user = decrypt_secret(acc.ak_cipher) if acc.ak_cipher else ""
                password = decrypt_secret(acc.secret_cipher) if acc.secret_cipher else ""
                reset_perf_diag()
                lines = collect_metrics_lines(acc, user, password)
                diag = get_last_perf_diag()
                if "stats_level" in diag:
                    # 版本暴露了 20 秒实时档：0=性能统计关闭，QueryPerf 必然空。
                    detail["stats_level"] = diag["stats_level"]
                    detail["interval_enabled"] = diag.get("interval_enabled")
                if diag.get("hist_levels"):
                    # vCenter 7/8 的 20 秒实时档不在 historicalInterval 里；
                    # 历史档级别全 0 = 统计关闭，全 >0 则问题更可能在只读权限。
                    detail["hist_levels"] = diag["hist_levels"]
                if diag.get("probes"):
                    # 0 样本时逐档探测：全空= vCenter 性能数据没生成，不是采集问题。
                    detail["perf_probes"] = diag["probes"]
                if product:
                    lines = [ln for ln in lines if ln.startswith(f"{product}_")]
                if lines and not write_prometheus(lines):
                    # 写库失败不能计成已写入：否则状态栏显示"写入 N 点"掩盖了失败。
                    detail["error"] = "VictoriaMetrics 写入失败"
                    _vsphere_status["errors"].append(f"{acc.name}: {detail['error']}")
                else:
                    detail["points"] = len(lines)
                    points += len(lines)
            except Exception as e:
                msg = f"{acc.name}: {type(e).__name__} {str(e)[:120]}"
                detail["error"] = str(e)[:160]
                _vsphere_status["errors"].append(msg)
                logger.warning("vSphere 指标采集失败 %s", msg)
            _vsphere_status["account_details"].append(detail)
            if not detail["error"]:
                mark_collected(db, "metrics", acc.id)
        if db.dirty:
            db.commit()
        if accounts and points == 0:
            if _vsphere_status["errors"]:
                _vsphere_status["warnings"].append(
                    "本轮 VMware 采集失败（0 个样本，账号报错见详情）："
                    "请检查账号地址/密码/SSL 与 vCenter 权限，处理后再点「采集当前产品」")
            else:
                details = _vsphere_status["account_details"]
                probe_sets = [d.get("perf_probes") for d in details
                              if isinstance(d.get("perf_probes"), dict) and d["perf_probes"]]
                dead_probes = [p for p in probe_sets
                               if all(isinstance(v, int) and v == 0 for v in p.values())]
                live_probes = [p for p in probe_sets
                               if any(isinstance(v, int) and v > 0 for v in p.values())]
                realtime = [d.get("stats_level") for d in details
                            if isinstance(d.get("stats_level"), int)]
                hist = [lv for d in details
                        for lv in (d.get("hist_levels") or []) if isinstance(lv, int)]
                # 归因顺序：先看统计级别是否被关闭（此时探测必然全空，不能误判为服务故障），
                # 再看逐档探测，最后才归因到权限。
                if realtime and all(lv == 0 for lv in realtime):
                    head = "已连接 vCenter，但其「实时(20 秒)统计级别」= 0（性能统计已关闭），" \
                           "所以本轮取到 0 个性能样本。"
                    todo = "① vCenter → 配置 → 常规 → 统计信息，把实时档设 1 并等 5–10 分钟；"
                elif hist and all(lv == 0 for lv in hist):
                    head = "已连接 vCenter，但其统计级别全为 0（性能统计已关闭），" \
                           "所以本轮取到 0 个性能样本。"
                    todo = "① vCenter → 配置 → 常规 → 统计信息，把统计级别设为 1 或更高并等 5–10 分钟；"
                elif dead_probes:
                    head = ("已连接 vCenter，接口可正常查询（统计级别未见异常），但实时(20秒)/"
                            "5分钟/30分钟三个统计档都没有有效性能样本。")
                    todo = ("① 用同一账号在 vSphere Client 打开任一 VM 的「监控 → 性能」，确认是否同样为空；"
                            "② 若 vSphere Client 有曲线而本页仍为 0，请把本页错误与 "
                            "journalctl -u qianshu-backend 的相关日志反馈；"
                            "③ 若 vSphere Client 同样为空，请 vCenter 管理员检查性能统计服务"
                            "（vpxd/perfcharts）与数据库空间，修复后回本页重试；")
                elif live_probes:
                    head = ("实时(20秒)统计档没有样本，但历史档有数据——vCenter 的实时统计可能未开启，"
                            "本轮按实时口径取不到点。")
                    todo = ("① 在 vSphere Client「监控 → 性能 → 高级」把时间范围切到「过去 1 小时」"
                            "确认实时曲线是否存在；② 仍为空请在 vCenter 检查实时统计配置；")
                elif realtime:
                    head = f"已连接 vCenter，但本轮取到 0 个性能样本（实时档统计级别={realtime[0]}）。"
                    todo = "① 确认实时档统计级别 ≥1 并等 5–10 分钟；"
                elif hist:
                    head = (f"已连接 vCenter，统计级别已开启（历史档级别={sorted(set(hist))}），"
                            "本轮仍取到 0 个性能样本，问题更可能在只读账号的性能读取权限。")
                    todo = "① 只读角色务必授予 System.View 并传播到子对象（这是读性能样本的前置权限）；"
                else:
                    head = "已连接 vCenter，但本轮取到 0 个性能样本（未能读取统计级别，多为只读权限不足）。"
                    todo = "① 检查只读角色是否具备 System.View 与性能读取权限；"
                _vsphere_status["warnings"].append(
                    head + "按「查看排查教程」处理：" + todo +
                    "处理完回本页只采 VMware 重试；仍有问题看服务端 "
                    "journalctl -u qianshu-backend")
        _vsphere_status["accounts"] = accounts
        _vsphere_status["points_written"] = points
        _vsphere_status["last_finished_at"] = datetime.now().isoformat()
        if accounts:
            logger.info("vSphere 指标采集完成: %d 账号 %d 点（scope=%s），错误 %d",
                        accounts, points, product or "all", len(_vsphere_status["errors"]))
        return get_vsphere_status()
    finally:
        _vsphere_status["running"] = False
