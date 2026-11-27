"""看门狗：周期自检 → 防抖 → auto_heal 自动修复（指数退避）。

防抖：连续 debounce_checks 次不健康才认定故障（单次抖动/启动窗口不误触发）；
退避：两次自动修复之间至少隔 heal_backoff（指数增长，封顶 heal_backoff_max），
防止"修不好就疯狂重启"。平台恢复后把事件写进千枢站内信（平台不可用时排队补报）。
"""
from __future__ import annotations

import logging
import threading
import time

from app.copilotd.checkup import run_checkup
from app.copilotd.config import CopilotdConfig
from app.copilotd.healer import run_heal

logger = logging.getLogger("qianshu.copilotd.watchdog")


class Watchdog:
    def __init__(self, cfg: CopilotdConfig, action_log, bridge, queue,
                 checkup_fn=None, heal_fn=None, now_fn=None, sleep_fn=None):
        self.cfg = cfg
        self.action_log = action_log
        self.bridge = bridge
        self.queue = queue
        self.checkup_fn = checkup_fn or run_checkup
        self.heal_fn = heal_fn or run_heal
        self.now_fn = now_fn or time.time
        self.sleep_fn = sleep_fn or time.sleep

        self.unhealthy_streak = 0
        self.next_heal_at = 0.0
        self.backoff = cfg.heal_backoff_base
        self.last_state = "init"
        self.last_check_at = 0.0
        self.last_checkup = None
        self._stop = threading.Event()
        self._thread = None

    # ---------- 单轮自检（独立成方法，测试直接驱动） ----------
    def run_once(self) -> dict:
        rep = self.checkup_fn(self.cfg)
        self.last_checkup = rep
        self.last_check_at = self.now_fn()
        healthy = bool(rep.get("healthy"))
        self.last_state = "healthy" if healthy else "unhealthy"

        if healthy:
            if self.unhealthy_streak > 0:
                # 平台恢复：留痕 + 站内信（顺带补报欠账）
                self.action_log.record(
                    "recovered", "watchdog", True,
                    detail={"unhealthy_streak": self.unhealthy_streak},
                )
                self._report_recovery()
            self.unhealthy_streak = 0
            self.backoff = self.cfg.heal_backoff_base
            return {"state": "healthy", "healed": False}

        self.unhealthy_streak += 1
        if self.unhealthy_streak == 1:
            # 首次发现异常：留痕（不动作，等防抖）
            self.action_log.record("detect", "watchdog", False,
                                   detail={"problems": rep.get("problems", [])})
            logger.warning("千枢不健康: %s", rep.get("problems"))

        now = self.now_fn()
        if not self.cfg.auto_heal:
            return {"state": "unhealthy", "healed": False, "reason": "auto_heal 关闭（只报不动）"}
        if self.unhealthy_streak < self.cfg.debounce_checks:
            return {"state": "unhealthy", "healed": False,
                    "reason": f"防抖中 {self.unhealthy_streak}/{self.cfg.debounce_checks}"}
        if now < self.next_heal_at:
            return {"state": "unhealthy", "healed": False,
                    "reason": f"退避中，{round(self.next_heal_at - now)}s 后允许下次修复"}

        logger.warning("触发自动修复（连续 %d 次不健康）", self.unhealthy_streak)
        receipt = self.heal_fn(self.cfg, self.action_log, bridge=self.bridge,
                               queue=self.queue, trigger="watchdog")
        self.next_heal_at = now + self.backoff
        self.backoff = min(self.backoff * 2, self.cfg.heal_backoff_max)
        return {"state": "unhealthy", "healed": True, "receipt_ok": receipt.get("ok")}

    def _report_recovery(self) -> None:
        if self.bridge is None or not self.bridge.enabled:
            return
        try:
            self.bridge.report(
                action="copilotd.recovered",
                title="千枢自愈：平台已恢复",
                content_md=f"qianshu-copilotd 看门狗确认千枢已恢复健康"
                           f"（此前连续 {self.unhealthy_streak} 次自检不健康）。",
                level="info",
                detail={"unhealthy_streak": self.unhealthy_streak},
            )
            self.queue.flush(self.bridge)   # 补报平台宕机期间积压的事件
        except Exception:  # noqa: BLE001
            self.queue.add(
                action="copilotd.recovered", title="千枢自愈：平台已恢复",
                content_md="qianshu-copilotd 看门狗确认千枢已恢复健康。", level="info",
                detail={"unhealthy_streak": self.unhealthy_streak},
            )

    # ---------- 后台线程 ----------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="copilotd-watchdog", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as e:  # noqa: BLE001 —— 看门狗自身绝不能死
                logger.exception("看门狗自检异常: %s", e)
            self._stop.wait(self.cfg.check_interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
