"""故障定位：体检 + 端口占用归属 + 最近 50 行错误日志模式 + 最近变更快照。

平台活着时调平台桥接口查 agent_change_tickets 与 audit 最近记录（用于"最近改了什么/
可回滚什么"）；平台不可用时该段标记 unavailable，不阻断定位。
"""
from __future__ import annotations

from app.copilotd import procs
from app.copilotd.checkup import run_checkup
from app.copilotd.config import CopilotdConfig


def _verdict(checkup: dict) -> str:
    problems = checkup["problems"]
    if "port_occupied_foreign" in problems:
        return "port_occupied_foreign"
    if "process_missing" in problems:
        return "platform_missing"
    if "http_unhealthy" in problems:
        return "http_unhealthy"
    if "disk_low" in problems:
        return "disk_low"
    if "log_errors" in problems:
        return "log_crash_pattern"
    return "healthy"


def run_diagnose(cfg: CopilotdConfig, bridge=None) -> dict:
    """生成结构化定位报告。bridge=None 或平台不可用时跳过变更快照。"""
    checkup = run_checkup(cfg)

    # 端口占用归属细节（谁占着、是不是千枢自己）
    port_detail = dict(checkup["checks"]["port"])
    if port_detail.get("occupant") == "foreign":
        port_detail["note"] = "端口被非千枢进程占用——daemon 不会终止他人进程，需人工介入"

    # 最近 50 行错误日志模式 + 崩溃前后文
    tail = procs.tail_lines(cfg.log_path, 50)
    log_err = procs.classify_log_errors(tail)
    error_lines = [ln[-200:] for ln in tail
                   if any(p.search(ln) for _, p in procs.ERROR_PATTERNS)][-10:]

    # 最近变更快照（平台活着才查得到）
    changes = {"available": False, "reason": "平台不可用"}
    if checkup["checks"]["http"]["ok"] and bridge is not None:
        try:
            data = bridge.recent_changes()
            tickets = data.get("change_tickets", [])
            pending = [t for t in tickets if t.get("status") == "pending"]
            changes = {
                "available": True,
                "change_tickets": tickets,
                "audit": data.get("audit", []),
                "rollback_hint": (f"最近变更 {len(tickets)} 条，其中 {len(pending)} 条待确认可回滚"
                                  if tickets else "最近无变更记录"),
            }
        except Exception as e:  # noqa: BLE001 —— 桥接口失败不阻断定位
            changes = {"available": False, "reason": f"查询最近变更失败: {str(e)[:120]}"}

    return {
        "ts": checkup["ts"],
        "verdict": _verdict(checkup),
        "healthy": checkup["healthy"],
        "problems": checkup["problems"],
        "checkup": checkup,
        "port_detail": port_detail,
        "log_evidence": {
            "path": cfg.log_path,
            "scanned_lines": len(tail),
            "patterns": log_err["patterns"],
            "error_lines": error_lines,
            "tail_last3": tail[-3:],
        },
        "recent_changes": changes,
    }
