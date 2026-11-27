"""千枢健康体检：进程/端口/HTTP/磁盘/日志尾 → 结构化体检报告。

健康口径（watchdog 只认 live 信号，不因历史日志行反复重启）：
  healthy = 进程在 且 端口在听 且 HTTP 2xx 且 磁盘余量足；
  日志尾错误模式作为证据附带（出现在 problems 里但不拉低 healthy）。
"""
from __future__ import annotations

import time

from app.copilotd import procs
from app.copilotd.config import CopilotdConfig


def run_checkup(cfg: CopilotdConfig) -> dict:
    port = procs.platform_port(cfg)
    pid = procs.find_platform_pid(port)
    listener = procs.port_listener_pid(port)

    process = {
        "ok": pid > 0,
        "pid": pid or None,
        "cmdline": procs.read_cmdline(pid)[:200] if pid else "",
    }

    if listener and listener != pid and not procs.is_platform_proc(listener):
        port_check = {"ok": False, "port": port, "listener_pid": listener,
                      "occupant": "foreign", "occupant_cmdline": procs.read_cmdline(listener)[:200]}
    elif listener:
        port_check = {"ok": True, "port": port, "listener_pid": listener,
                      "occupant": "platform" if procs.is_platform_proc(listener) else "unknown",
                      "occupant_cmdline": ""}
    else:
        port_check = {"ok": False, "port": port, "listener_pid": None,
                      "occupant": None, "occupant_cmdline": ""}

    http = procs.http_probe(cfg.health_url)
    http_check = {"ok": http["ok"], "url": cfg.health_url,
                  "status": http["status"], "elapsed_ms": http["elapsed_ms"],
                  "error": http["error"]}

    disk = procs.disk_usage(cfg.platform_dir)
    disk["ok"] = bool(disk.get("ok")) and (disk.get("free_gb") or 0) >= cfg.disk_min_gb
    disk["min_gb"] = cfg.disk_min_gb

    tail = procs.tail_lines(cfg.log_path, 50)
    log_err = procs.classify_log_errors(tail)
    log_check = {"ok": log_err["total"] == 0, "path": cfg.log_path,
                 "tail_lines": len(tail), "patterns": log_err["patterns"],
                 "total_errors": log_err["total"],
                 "last_line": tail[-1][-200:] if tail else ""}

    problems = []
    if not process["ok"]:
        problems.append("process_missing")
    if not port_check["ok"]:
        problems.append("port_occupied_foreign" if port_check["occupant"] == "foreign"
                        else "port_not_listening")
    if not http_check["ok"]:
        problems.append("http_unhealthy")
    if not disk["ok"]:
        problems.append("disk_low")
    if not log_check["ok"]:
        problems.append("log_errors")

    live_keys = ("process_missing", "port_not_listening", "port_occupied_foreign",
                 "http_unhealthy", "disk_low")
    healthy = not any(p in live_keys for p in problems)

    return {
        "ts": time.time(),
        "healthy": healthy,
        "checks": {"process": process, "port": port_check, "http": http_check,
                   "disk": disk, "log": log_check},
        "problems": problems,
    }
