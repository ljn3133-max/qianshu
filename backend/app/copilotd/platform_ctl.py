"""千枢平台进程控制：拉起 / 优雅终止 / 健康轮询。

安全边界：
  - 终止只对 cmdline 含 app.main:app 的千枢进程执行；端口若被非千枢进程占用，
    拒绝终止并如实回报（绝不误杀他人进程，也不碰 8000 邻居服务）；
  - 拉起只走 config 白名单校验过的 start_cmd（shlex 解析，不走 shell）；
  - 输出重定向到平台日志文件，start_new_session=True（等价 setsid nohup）。
"""
from __future__ import annotations

import os
import shlex
import signal
import subprocess
import time

from app.copilotd import procs
from app.copilotd.config import FORBIDDEN_TOUCH_PORTS, CopilotdConfig

_STOP_WAIT_S = 10.0     # SIGTERM 后等待退出的秒数，超时才 SIGKILL


def _port_guard(cfg: CopilotdConfig) -> dict:
    """端口红线（第二道保险，config 校验之外）：8000 邻居服务绝不触碰。"""
    port = procs.platform_port(cfg)
    if port in FORBIDDEN_TOUCH_PORTS:
        return {"ok": False, "detail": f"端口 {port} 是邻居服务，daemon 拒绝触碰"}
    return {}


def start_platform(cfg: CopilotdConfig) -> dict:
    """按 start_cmd 拉起千枢（detach，日志追加到 cfg.log_path）。"""
    guard = _port_guard(cfg)
    if guard:
        return {"ok": False, "pid": None, "already_running": False, **guard}
    existing = procs.find_platform_pid(procs.platform_port(cfg))
    if existing:
        return {"ok": True, "pid": existing, "already_running": True,
                "detail": f"千枢进程已在运行 pid={existing}"}
    argv = shlex.split(cfg.start_cmd)   # 白名单已在 config.validate 把关
    try:
        os.makedirs(os.path.dirname(cfg.log_path) or ".", exist_ok=True)
        log_f = open(cfg.log_path, "ab")
    except OSError as e:
        return {"ok": False, "pid": None, "already_running": False,
                "detail": f"打开平台日志失败: {e}"}
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cfg.resolved_workdir,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,   # 等价 setsid：与 daemon 生命周期解耦
            env=os.environ.copy(),
        )
    except OSError as e:
        log_f.close()
        return {"ok": False, "pid": None, "already_running": False,
                "detail": f"拉起失败: {e}"}
    finally:
        pass
    log_f.close()   # 子进程持有 fd，父进程可关
    return {"ok": True, "pid": proc.pid, "already_running": False,
            "detail": f"已拉起 pid={proc.pid}: {' '.join(argv)}"}


def stop_platform(cfg: CopilotdConfig) -> dict:
    """优雅终止千枢（TERM→等待→KILL）。只动千枢自身进程。"""
    guard = _port_guard(cfg)
    if guard:
        return {"ok": False, "stopped": False, "pid": None, **guard}
    port = procs.platform_port(cfg)
    listener = procs.port_listener_pid(port)
    pid = procs.find_platform_pid(port)

    # 端口被非千枢进程占用：拒绝任何终止动作（他人进程红线）
    if listener and not procs.is_platform_proc(listener):
        return {"ok": False, "stopped": False, "pid": listener,
                "detail": f"端口 {port} 由非千枢进程占用（pid={listener}"
                          f" {procs.read_cmdline(listener)[:80]}），拒绝终止"}
    if not pid:
        return {"ok": True, "stopped": False, "pid": None,
                "detail": "千枢进程未运行，无需终止"}

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        return {"ok": False, "stopped": False, "pid": pid, "detail": f"SIGTERM 失败: {e}"}
    deadline = time.time() + _STOP_WAIT_S
    while time.time() < deadline:
        if not procs.proc_alive(pid):
            return {"ok": True, "stopped": True, "pid": pid, "detail": f"已优雅退出 pid={pid}"}
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    for _ in range(20):
        if not procs.proc_alive(pid):
            break
        time.sleep(0.2)
    return {"ok": True, "stopped": True, "pid": pid,
            "detail": f"SIGTERM 超时，已 SIGKILL pid={pid}"}


def wait_healthy(cfg: CopilotdConfig, timeout: float = None) -> dict:
    """轮询健康直到 HTTP 2xx 或超时。"""
    timeout = timeout if timeout is not None else cfg.start_timeout
    t0 = time.time()
    attempts = 0
    last = {"status": None, "error": "not attempted"}
    while time.time() - t0 < timeout:
        attempts += 1
        last = procs.http_probe(cfg.health_url, timeout=5.0)
        if last["ok"]:
            return {"ok": True, "attempts": attempts,
                    "elapsed_s": round(time.time() - t0, 2),
                    "last_status": last["status"]}
        time.sleep(1.0)
    return {"ok": False, "attempts": attempts,
            "elapsed_s": round(time.time() - t0, 2),
            "last_status": last.get("status"), "error": last.get("error", "")[:120]}
