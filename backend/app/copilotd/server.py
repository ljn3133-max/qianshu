"""生命周期通道 HTTP 服务（仅 127.0.0.1，文件 token 鉴权）。

端点（X-Copilotd-Token 必带，缺失/不匹配一律 401）：
  GET  /health     daemon 状态 + 平台体检（进程/端口/HTTP/磁盘/日志尾摘要）
  GET  /diagnose   故障定位（端口占用/进程/日志错误模式/最近变更快照）
  POST /heal       平台修复（诊断→最近变更提示→重启→验证→回执，每步留痕）
  POST /restart    重启平台（graceful kill → start_cmd → 健康轮询）
  GET  /status     历史动作（内存环形 + jsonl 落盘）

标准库实现（http.server）——不依赖 FastAPI/uvicorn，独立运行。
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.copilotd import healer
from app.copilotd.checkup import run_checkup
from app.copilotd.config import CopilotdConfig, ensure_token
from app.copilotd.diagnose import run_diagnose

logger = logging.getLogger("qianshu.copilotd.server")


class Ctx:
    """请求处理上下文（main 组装，测试可注入替身）。"""

    def __init__(self, cfg: CopilotdConfig, action_log, bridge, queue, watchdog=None):
        self.cfg = cfg
        self.action_log = action_log
        self.bridge = bridge
        self.queue = queue
        self.watchdog = watchdog
        self.started_at = time.time()


class Handler(BaseHTTPRequestHandler):
    server_version = "qianshu-copilotd"
    ctx: Ctx = None   # 由 serve() 注入

    # ---- 基础设施 ----
    def log_message(self, fmt, *args):   # 走统一 logging，不打到 stderr
        logger.debug("%s - %s", self.address_string(), fmt % args)

    def _token_ok(self) -> bool:
        supplied = self.headers.get("X-Copilotd-Token", "")
        try:
            expected = ensure_token(self.ctx.cfg.token_file)
        except OSError:
            return False
        return bool(supplied) and hmac.compare_digest(supplied, expected)

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_or_401(self) -> bool:
        if self._token_ok():
            return True
        self._send(401, {"ok": False, "error": "token 校验失败（X-Copilotd-Token）"})
        return False

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(min(length, 64 * 1024))
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    # ---- 路由 ----
    def do_GET(self):  # noqa: N802
        if not self._auth_or_401():
            return
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/health":
            self._send(200, self._health())
        elif path == "/diagnose":
            self._send(200, {"ok": True,
                             "diagnose": run_diagnose(self.ctx.cfg, bridge=self.ctx.bridge)})
        elif path == "/status":
            self._send(200, self._status())
        else:
            self._send(404, {"ok": False, "error": f"未知端点: {path}"})

    def do_POST(self):  # noqa: N802
        if not self._auth_or_401():
            return
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        body = self._read_body()
        if path == "/heal":
            receipt = healer.run_heal(self.ctx.cfg, self.ctx.action_log,
                                      bridge=self.ctx.bridge, queue=self.ctx.queue,
                                      trigger="api")
            if body.get("reason"):
                receipt.setdefault("detail", {})["reason"] = body["reason"]
            self._send(200, {"ok": receipt.get("ok", False), "receipt": receipt})
        elif path == "/restart":
            receipt = healer.run_restart(self.ctx.cfg, self.ctx.action_log,
                                         bridge=self.ctx.bridge, queue=self.ctx.queue,
                                         trigger="api")
            self._send(200, {"ok": receipt.get("ok", False), "receipt": receipt})
        else:
            self._send(404, {"ok": False, "error": f"未知端点: {path}"})

    # ---- 回执结构 ----
    def _health(self) -> dict:
        checkup = run_checkup(self.ctx.cfg)
        compact = checkup["checks"]["log"]
        return {
            "ok": True,
            "daemon": {
                "name": "qianshu-copilotd",
                "pid": os.getpid(),
                "uptime_s": round(time.time() - self.ctx.started_at, 1),
                "auto_heal": self.ctx.cfg.auto_heal,
                "check_interval": self.ctx.cfg.check_interval,
                "listen": f"{self.ctx.cfg.listen_host}:{self.ctx.cfg.listen_port}",
            },
            "platform": {
                "healthy": checkup["healthy"],
                "problems": checkup["problems"],
                "process": checkup["checks"]["process"],
                "port": checkup["checks"]["port"],
                "http": checkup["checks"]["http"],
                "disk": checkup["checks"]["disk"],
                "log_tail": {"path": compact["path"], "total_errors": compact["total_errors"],
                             "patterns": compact["patterns"], "last_line": compact["last_line"]},
            },
        }

    def _status(self) -> dict:
        wd = self.ctx.watchdog
        return {
            "ok": True,
            "total_actions": self.ctx.action_log.total,
            "actions": self.ctx.action_log.recent(50),
            "pending_reports": self.ctx.queue.pending,
            "bridge_enabled": bool(self.ctx.bridge and self.ctx.bridge.enabled),
            "watchdog": {
                "running": bool(wd and wd._thread and wd._thread.is_alive()) if wd else False,
                "last_state": wd.last_state if wd else None,
                "unhealthy_streak": wd.unhealthy_streak if wd else 0,
                "last_check_at": wd.last_check_at if wd else None,
                "next_heal_in_s": max(0, round(wd.next_heal_at - time.time())) if wd else None,
            },
        }


def serve(ctx: Ctx) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"ctx": ctx})
    httpd = ThreadingHTTPServer((ctx.cfg.listen_host, ctx.cfg.listen_port), handler)
    httpd.daemon_threads = True
    return httpd
