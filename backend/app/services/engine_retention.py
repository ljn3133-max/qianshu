"""存储引擎超期数据渐进清理。

设计见 docs/active/deployment.md。两层实时生效里的"软保留-物理删"半边：
查询口径（retention.clamp_start_ts）保证"看不见"，本模块保证"真删掉"。

引擎能力边界：
- VM delete_series 不支持时间窗（v1.102.0 及官方文档均要求整条序列删除），因此 VM
  只清理「死序列」：全部样本都已超期（最后样本 < 保留线）的序列整条删除，绝不碰仍有
  新样本的活序列；跨保留线的活序列旧样本由查询口径隐藏，并随引擎上限
  （-retentionPeriod）自动过期。枚举窗口分片 + 游标推进，每轮限量，避免一次大删。
- VL 有按 LogsQL 过滤的删除 API（POST /delete/run_task?filter=...），但需容器
  -delete.enable 开启（默认关；探活 404/405 = 未启用）。删除任务会重写全量存储，
  因此限频 24h 一次，异步提交不阻塞。

两路都逐次动态取值（services/retention.py 热读）：设置一改，下一轮清理立即按新值走。
游标存 .env（env_store）：清理只在调度 leader 跑（app/main.py），无并发写；
游标丢失只导致重扫一遍已删窗口（幂等），不会误删。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import httpx

from app.collector import vm as vm_client
from app.core.config import settings
from app.services import retention
from app.services.env_store import get_key, set_keys

logger = logging.getLogger("qianshu.engine_retention")

_ALL_SERIES = '{__name__=~".+"}'
_CURSOR_VM = "ENGINE_CLEAN_VM_CURSOR"     # D：已清理区间下界
_TOP_VM = "ENGINE_CLEAN_VM_TOP"           # T：已清理区间上界（清理区间 = [D, T]）
_CURSOR_VL = "ENGINE_CLEAN_VL_CURSOR"
_LAST_VL = "ENGINE_CLEAN_VL_LAST_TS"
_VL_MIN_INTERVAL_S = 24 * 3600        # VL 删除任务限频（官方：删除重写全量存储，不宜频繁）
_DELETE_BATCH = 200                    # 每个 delete_series 请求最多带的序列选择器数


# ---------------- 游标（env_store 持久化；丢则从引擎上限下界重扫，幂等） ----------------

def _cursor_get(key: str) -> float:
    raw = get_key(key)
    try:
        return float(str(raw).strip()) if raw is not None else 0.0
    except ValueError:
        return 0.0


def _cursor_set(key: str, ts: float) -> None:
    set_keys({key: str(int(ts))})


# ---------------- VM：死序列清理（安全子集，渐进） ----------------

def clean_vm_expired(now: float | None = None, chunk_days: int = 30,
                     max_chunks: int = 10) -> dict:
    """删掉"全部样本已超期"的 VM 序列。返回 {deleted_series, windows, cutoff, ...}。

    渐进（双游标，已清理区间 [D, T] 不重复枚举）：
      ① 顶段增量 [T, 保留线]——稳态只差几十小时的量；调小保留时一次性补齐新超期段；
      ② 积压回溯 [D-chunk, D] 向历史方向推进——首轮/调小保留后的存量逐轮补完。
    每轮最多 max_chunks 片（chunk_days/片），限速不一次大删。"""
    days = retention.effective_days("vm")
    if days <= 0:
        return {"skipped": "permanent"}
    now = float(now if now is not None else time.time())
    cutoff = now - days * 86400
    floor = now - (retention.cap_days("vm") or 3650) * 86400   # 引擎上限外本就没数据
    d = _cursor_get(_CURSOR_VM) or cutoff     # 游标缺失=首轮：从保留线向历史回溯
    t = _cursor_get(_TOP_VM) or cutoff
    d = max(d, floor)

    # 活序列集：保留线之后仍有样本的序列（这些序列的新样本绝不能删）
    alive = {frozenset(l.items()) for l in vm_client.list_series(_ALL_SERIES, cutoff, now + 60)}
    deleted = 0
    windows = 0

    def _clean_window(w0: float, w1: float) -> bool:
        """删 [w0, w1] 内枚举到的死序列。False=删除失败（游标不推进，下轮重试）。"""
        nonlocal deleted, windows
        cands = vm_client.list_series(_ALL_SERIES, w0, w1)
        dead = [l for l in cands if frozenset(l.items()) not in alive]
        for i in range(0, len(dead), _DELETE_BATCH):
            batch = [vm_client.series_matcher(l) for l in dead[i:i + _DELETE_BATCH]]
            if not vm_client.delete_series(batch):
                logger.warning("VM 死序列删除失败（本轮中断，游标不推进）: %s 条", len(batch))
                return False
        deleted += len(dead)
        windows += 1
        return True

    # ① 顶段增量 [t, cutoff]
    while t < cutoff and windows < max_chunks:
        t1 = min(t + chunk_days * 86400, cutoff)
        if not _clean_window(t, t1):
            return {"deleted_series": deleted, "windows": windows, "cutoff": cutoff,
                    "cursor": d, "top": t, "error": "delete_failed"}
        t = t1
    # ② 积压回溯：D 向历史方向分片推进
    while d > floor and windows < max_chunks:
        w0 = max(d - chunk_days * 86400, floor)
        if not _clean_window(w0, d):
            return {"deleted_series": deleted, "windows": windows, "cutoff": cutoff,
                    "cursor": d, "top": t, "error": "delete_failed"}
        d = w0
    _cursor_set(_CURSOR_VM, d)
    _cursor_set(_TOP_VM, t)
    if deleted:
        logger.info("VM 死序列清理: %d 条序列（%d 片窗口，区间 [%d, %d]）",
                    deleted, windows, int(d), int(t))
    return {"deleted_series": deleted, "windows": windows, "cutoff": cutoff,
            "cursor": d, "top": t,
            "backlog_left": d > floor or t < cutoff}


# ---------------- VL：删除任务（能力探活 + 限频） ----------------

def clean_vl_expired(now: float | None = None) -> dict:
    """按保留线提交 VL 删除任务（LogsQL 时间过滤）。

    未配置 / 未启用 -delete.enable / 任务在跑 / 限频内 → 跳过并如实回报状态。
    VL 是预留日志存储（当前后端未直写），此处保留数据落地后的删除路径。"""
    days = retention.effective_days("vl")
    if days <= 0:
        return {"skipped": "permanent"}
    base = (settings.VICTORIALOGS_URL or "").rstrip("/")
    if not base:
        return {"skipped": "vl_not_configured"}
    now = float(now if now is not None else time.time())
    cutoff = now - days * 86400
    if cutoff <= _cursor_get(_CURSOR_VL):
        return {"skipped": "no_expired_window"}
    last = _cursor_get(_LAST_VL)
    if last and now - last < _VL_MIN_INTERVAL_S:
        return {"skipped": "throttled", "next_ok_in_h": round((_VL_MIN_INTERVAL_S - (now - last)) / 3600, 1)}
    try:
        probe = httpx.get(f"{base}/delete/active_tasks", timeout=10)
    except Exception as e:
        return {"skipped": "vl_unreachable", "error": str(e)[:120]}
    if probe.status_code != 200:
        return {"skipped": "vl_delete_api_disabled", "status": probe.status_code,
                "hint": "需重建 victorialogs 容器加 -delete.enable（引擎限制，默认关闭）"}
    try:
        active = probe.json() or []
    except Exception:
        active = []
    if active:
        return {"skipped": "task_running"}
    # LogsQL 时间过滤（官方文档 _time:[min,max) 语法）：删 [游标, 保留线) 全部超期日志
    lo = _cursor_get(_CURSOR_VL) or (now - (retention.cap_days("vl") or 3650) * 86400)

    def fmt(ts: float) -> str:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    log_filter = f"_time:[{fmt(lo)}, {fmt(cutoff)})"
    try:
        resp = httpx.post(f"{base}/delete/run_task", params={"filter": log_filter}, timeout=15)
    except Exception as e:
        return {"error": "vl_delete_submit_failed", "detail": str(e)[:120]}
    if resp.status_code != 200:
        return {"error": "vl_delete_submit_failed", "status": resp.status_code}
    _cursor_set(_CURSOR_VL, cutoff)
    _cursor_set(_LAST_VL, now)
    logger.info("VL 超期日志删除任务已提交: %s", log_filter)
    return {"task_id": (resp.json() or {}).get("task_id"), "filter": log_filter, "window_start": lo}


# ---------------- 总入口（保存即清 + 周期循环都走这里） ----------------

def clean_all(now: float | None = None, max_chunks: int = 10) -> dict:
    """VM + VL 独立执行，单个引擎失败不影响另一个（清理尽力而为，查询口径兜底）。

    max_chunks：VM 单次执行的分片预算（保存触发的同步调用可给小预算，积压由周期循环补完）。"""
    out: dict = {}
    try:
        out["vm"] = clean_vm_expired(now=now, max_chunks=max_chunks)
    except Exception as e:
        logger.exception("VM 引擎保留清理异常")
        out["vm"] = {"error": str(e)[:160]}
    try:
        out["vl"] = clean_vl_expired(now=now)
    except Exception as e:
        logger.exception("VL 引擎保留清理异常")
        out["vl"] = {"error": str(e)[:160]}
    return out
