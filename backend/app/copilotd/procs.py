"""进程/端口/HTTP/磁盘/日志探测的取证原语。

只读探测，不产生副作用；千枢进程识别靠 cmdline 含 app.main:app 标记
（终止类操作同样只认这个标记，见 platform_ctl）。
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request

from app.copilotd.config import PLATFORM_APP_TARGET, CopilotdConfig

# 日志尾错误模式分类（剧本⑯：OOM / Traceback / Address already in use …）
ERROR_PATTERNS = [
    ("port_conflict", re.compile(r"Address already in use", re.I)),
    ("oom", re.compile(r"(out of memory|MemoryError|Killed process|oom-kill)", re.I)),
    ("traceback", re.compile(r"Traceback \(most recent call last\)")),
    ("import_error", re.compile(r"(ModuleNotFoundError|ImportError)")),
    ("fatal", re.compile(r"(FATAL|CRITICAL|ERROR)")),
]


def read_cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    except OSError:
        return ""


def proc_alive(pid: int) -> bool:
    return pid > 0 and os.path.exists(f"/proc/{pid}")


def _is_platform_cmdline(cmd: str) -> bool:
    """进程身份判定（终止类操作的安全红线）：

    可执行文件必须是 python/uvicorn（bash -c 包装壳等不算——壳的 cmdline
    里同样含 app.main:app 字样，误认会 SIGTERM 到错误进程），
    且 cmdline 含 app.main:app 标记。
    """
    if PLATFORM_APP_TARGET not in cmd:
        return False
    first = os.path.basename(cmd.split()[0]) if cmd.split() else ""
    return bool(re.match(r"^(uvicorn|python|python3|python3\.\d+|python\.\d+)$", first))


def is_platform_proc(pid: int) -> bool:
    """是否千枢平台进程。"""
    return _is_platform_cmdline(read_cmdline(pid))


def find_platform_pid(port: int = 0) -> int:
    """找千枢平台进程 pid（未运行返回 0）。

    不只看端口：进程活着但端口还没绑上/卡死时也能被定位到。
    port>0 时要求 cmdline 同时带该端口标记（--port N / :N）——同机多实例/测试
    隔离时不会错认别的千枢进程；没扫到再退化为"端口属主是平台进程"。
    """
    port_marks = (f"--port {port}", f":{port}", f"port={port}") if port else ()
    fallback = 0
    for path in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            with open(path, "rb") as f:
                cmd = f.read().replace(b"\x00", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if not _is_platform_cmdline(cmd):
            continue
        if port_marks and not any(m in cmd for m in port_marks):
            continue
        try:
            return int(path.split("/")[2])
        except (ValueError, IndexError):
            continue
    if port:
        listener = port_listener_pid(port)
        if listener and is_platform_proc(listener):
            fallback = listener
    return fallback


def port_listener_pid(port: int) -> int:
    """端口监听者 pid（无监听返回 0）。ss 优先，/proc/net/tcp 兜底。"""
    pid = _port_pid_via_ss(port)
    if pid:
        return pid
    return _port_pid_via_proc(port)


def _port_pid_via_ss(port: int) -> int:
    try:
        out = subprocess.run(["ss", "-tlnpH", "sport", "=", f":{port}"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    m = re.search(r"pid=(\d+)", out)
    return int(m.group(1)) if m else 0


def _port_pid_via_proc(port: int) -> int:
    """/proc/net/tcp{,6} 拿 inode，再扫 /proc/*/fd 找属主（ss 不可用时兜底）。"""
    inode = 0
    hex_port = f"{port:04X}"
    for fn in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(fn, "r", encoding="utf-8") as f:
                lines = f.readlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) < 10:
                continue
            if parts[1].split(":")[-1] == hex_port and parts[3] == "0A":  # 0A=LISTEN
                inode = int(parts[9])
                break
        if inode:
            break
    if not inode:
        return 0
    target = f"socket:[{inode}]"
    for fd in glob.glob("/proc/[0-9]*/fd/*"):
        try:
            if os.readlink(fd) == target:
                return int(fd.split("/")[2])
        except OSError:
            continue
    return 0


def http_probe(url: str, timeout: float = 5.0) -> dict:
    """GET url，返回 {ok, status, elapsed_ms, error}。"""
    t0 = time.time()
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
        return {"ok": 200 <= status < 300, "status": status,
                "elapsed_ms": int((time.time() - t0) * 1000), "error": ""}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code,
                "elapsed_ms": int((time.time() - t0) * 1000), "error": f"HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001 —— 探测失败一律降级为结构化结果
        return {"ok": False, "status": None,
                "elapsed_ms": int((time.time() - t0) * 1000), "error": str(e)[:120]}


def disk_usage(path: str) -> dict:
    try:
        usage = shutil.disk_usage(path)
        return {"ok": True, "free_gb": round(usage.free / 2 ** 30, 2),
                "total_gb": round(usage.total / 2 ** 30, 2),
                "used_pct": round(100 * (usage.total - usage.free) / usage.total, 1)}
    except OSError as e:
        return {"ok": False, "free_gb": None, "total_gb": None, "used_pct": None,
                "error": str(e)[:120]}


def tail_lines(path: str, n: int = 50) -> list:
    """文件尾 n 行（文件不存在返回空）。"""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            chunk = min(size, 64 * 1024)
            f.seek(size - chunk)
            text = f.read().decode("utf-8", "replace")
        lines = text.splitlines()
        return lines[-n:]
    except OSError:
        return []


def classify_log_errors(lines: list) -> dict:
    """日志尾错误模式分类：{pattern: {count, last}} + total。"""
    found: dict = {}
    for line in lines:
        for name, pat in ERROR_PATTERNS:
            if pat.search(line):
                item = found.setdefault(name, {"count": 0, "last": ""})
                item["count"] += 1
                item["last"] = line[-200:]
    return {"patterns": found, "total": sum(v["count"] for v in found.values())}


def platform_port(cfg: CopilotdConfig) -> int:
    """从 api_url 解析千枢平台端口（start/stop/端口探测共用同一口径）。"""
    m = re.search(r":(\d+)(?:/|$)", cfg.api_url.rstrip("/").split("//", 1)[-1])
    return int(m.group(1)) if m else 80
