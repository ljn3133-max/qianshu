"""qianshu-copilotd 入口：配置/token → 动作留痕 → 桥接 → 看门狗 + 生命周期通道。

用法：cd /opt/qianshu/backend && python3 -m app.copilotd [--config PATH] [--no-watchdog]
日志：/tmp/qianshu-copilotd.log（独立于千枢平台日志）。
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import signal
import sys
import threading

from app.copilotd.actions import ActionLog
from app.copilotd.bridge import BridgeClient, ReportQueue
from app.copilotd.config import (DEFAULT_CONFIG_PATH, ConfigError, ensure_token,
                                 load_or_create, resolve_bridge_token)
from app.copilotd.server import Ctx, serve
from app.copilotd.watchdog import Watchdog

LOG_PATH = "/tmp/qianshu-copilotd.log"

logger = logging.getLogger("qianshu.copilotd")


def _setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        os.makedirs(os.path.dirname(LOG_PATH) or ".", exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        pass
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    if sys.stdout.isatty():   # 重定向到文件/管道时不加控制台 handler，避免同一日志双写
        root.addHandler(sh)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="qianshu-copilotd",
                                     description="千枢智能体独立运行时（平台生命周期通道）")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="配置文件路径")
    parser.add_argument("--no-watchdog", action="store_true", help="只开生命周期通道，不跑看门狗")
    args = parser.parse_args(argv)

    _setup_logging()
    try:
        cfg = load_or_create(args.config)
    except (ConfigError, ValueError, OSError) as e:
        logger.error("配置不可用，拒绝启动: %s", e)
        return 2

    token = ensure_token(cfg.token_file)
    bridge = BridgeClient(cfg.api_url, resolve_bridge_token(cfg))
    queue = ReportQueue()
    action_log = ActionLog(cfg.actions_path)
    watchdog = Watchdog(cfg, action_log, bridge, queue)
    ctx = Ctx(cfg, action_log, bridge, queue, watchdog=watchdog)

    logger.info("qianshu-copilotd 启动 pid=%s listen=%s:%s auto_heal=%s check_interval=%ss "
                "config=%s token_file=%s bridge=%s",
                os.getpid(), cfg.listen_host, cfg.listen_port, cfg.auto_heal,
                cfg.check_interval, args.config, cfg.token_file,
                "on" if bridge.enabled else "off(缺 COPILOTD_BRIDGE_TOKEN)")
    logger.info("生命周期 token 已就绪（%s，长度 %d，不打印明文）", cfg.token_file, len(token))

    if not args.no_watchdog:
        watchdog.start()

    httpd = serve(ctx)

    def _graceful(signum, _frame):
        logger.info("收到信号 %s，退出", signum)
        # shutdown() 必须在别的线程调用：serve_forever 就在主线程跑，
        # 在信号处理器（主线程）里直接调会死锁
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _graceful)
    signal.signal(signal.SIGINT, _graceful)

    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        watchdog.stop()
        httpd.server_close()
        logger.info("qianshu-copilotd 已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
