"""平台修复：诊断 →（平台活着）查最近变更提示可回滚 → 重启平台 → 轮询验证 → 回执。

每步留痕（steps 逐条进回执 + jsonl）；动作同时调平台审计/站内信（平台活着时），
不可用则进补报队列。并发保护：同一时刻只允许一个 heal/restart 在跑（防抖之外的
第二道保险，watchdog 与 API 不会叠跑）。
"""
from __future__ import annotations

import threading
import time

from app.copilotd import platform_ctl
from app.copilotd.bridge import ReportQueue
from app.copilotd.config import CopilotdConfig
from app.copilotd.diagnose import run_diagnose

_HEAL_LOCK = threading.Lock()


def _now() -> float:
    return time.time()


def _report(ctx_action: str, title: str, content: str, level: str,
            detail: dict, bridge, queue: ReportQueue) -> str:
    """上报平台（站内信+审计）；失败进补报队列。返回 sent/queued/disabled。"""
    if bridge is None or not bridge.enabled:
        return "disabled"
    try:
        bridge.report(action=ctx_action, title=title, content_md=content,
                      level=level, detail=detail)
        queue.flush(bridge)   # 顺带补报历史欠账
        return "sent"
    except Exception:  # noqa: BLE001
        queue.add(action=ctx_action, title=title, content_md=content,
                  level=level, detail=detail)
        return "queued"


def run_restart(cfg: CopilotdConfig, action_log, bridge=None, queue: ReportQueue = None,
                trigger: str = "api") -> dict:
    """重启平台：优雅终止 → start_cmd 拉起 → 健康轮询 → 回执。"""
    return _run(cfg, action_log, bridge, queue, trigger, heal=False)


def run_heal(cfg: CopilotdConfig, action_log, bridge=None, queue: ReportQueue = None,
             trigger: str = "api") -> dict:
    """定位并修复千枢（含诊断与最近变更提示）。"""
    return _run(cfg, action_log, bridge, queue, trigger, heal=True)


def _run(cfg: CopilotdConfig, action_log, bridge, queue: ReportQueue,
         trigger: str, heal: bool) -> dict:
    queue = queue or ReportQueue()
    if not _HEAL_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "已有修复/重启在进行中", "action": "heal" if heal else "restart"}
    t0 = _now()
    steps: list = []
    action_name = "heal" if heal else "restart"

    def step(name: str, ok: bool, detail: dict, s0: float) -> None:
        steps.append({"step": name, "ok": ok, "ms": int((_now() - s0) * 1000), "detail": detail})

    try:
        diagnose = None
        rollback_hint = ""
        if heal:
            # 1) 定位（⑰-定位）
            s0 = _now()
            diagnose = run_diagnose(cfg, bridge=bridge)
            step("diagnose", True, {"verdict": diagnose["verdict"],
                                    "problems": diagnose["problems"]}, s0)
            # 2) 平台活着则查最近变更，提示可回滚
            s0 = _now()
            if diagnose["recent_changes"].get("available"):
                rollback_hint = diagnose["recent_changes"].get("rollback_hint", "")
                step("query_recent_changes", True,
                     {"rollback_hint": rollback_hint,
                      "tickets": len(diagnose["recent_changes"].get("change_tickets", []))}, s0)
            else:
                step("query_recent_changes", False,
                     {"skipped": diagnose["recent_changes"].get("reason", "平台不可用"),
                      "note": "恢复后补报，不阻断修复"}, s0)

        # 3) 优雅终止（只动千枢自身；他人进程占用端口会在此被拒绝）
        s0 = _now()
        stop = platform_ctl.stop_platform(cfg)
        step("stop_platform", stop["ok"], stop, s0)
        if not stop["ok"]:
            receipt = _finish(action_name, trigger, False, steps, t0, diagnose,
                              None, rollback_hint, "not_attempted")
            action_log.record(action_name, trigger, False, steps=steps,
                              detail={"error": stop["detail"]},
                              duration_s=receipt["duration_s"])
            return receipt

        # 4) start_cmd 拉起
        s0 = _now()
        start = platform_ctl.start_platform(cfg)
        step("start_platform", start["ok"], start, s0)
        if not start["ok"]:
            receipt = _finish(action_name, trigger, False, steps, t0, diagnose,
                              None, rollback_hint, "not_attempted")
            action_log.record(action_name, trigger, False, steps=steps,
                              detail={"error": start["detail"]},
                              duration_s=receipt["duration_s"])
            return receipt

        # 5) 健康轮询验证
        s0 = _now()
        verify = platform_ctl.wait_healthy(cfg)
        step("verify_health", verify["ok"], verify, s0)

        # 6) 回执上报（站内信+审计；失败进补报队列）
        s0 = _now()
        level = "warning" if not verify["ok"] else "info"
        content = (f"qianshu-copilotd {'定位修复' if heal else '重启'}千枢："
                   f"验证{'通过' if verify['ok'] else '失败'}（{verify.get('elapsed_s')}s / "
                   f"{verify.get('attempts')} 次探测）。"
                   + (f"最近变更提示：{rollback_hint}。" if rollback_hint else ""))
        reported = _report("copilotd.heal" if heal else "copilotd.restart",
                           f"千枢自愈：{'修复' if heal else '重启'}{'成功' if verify['ok'] else '未验证通过'}",
                           content, level,
                           {"steps": [{"step": s["step"], "ok": s["ok"]} for s in steps],
                            "verify": verify, "trigger": trigger},
                           bridge, queue)
        step("report", reported in ("sent", "disabled", "queued"), {"result": reported}, s0)

        receipt = _finish(action_name, trigger, verify["ok"], steps, t0, diagnose,
                          verify, rollback_hint, reported)
        action_log.record(action_name, trigger, verify["ok"], steps=steps,
                          detail={"reported": reported, "verify": verify,
                                  "rollback_hint": rollback_hint},
                          duration_s=receipt["duration_s"])
        return receipt
    finally:
        _HEAL_LOCK.release()


def _finish(action: str, trigger: str, ok: bool, steps: list, t0: float,
            diagnose, verify, rollback_hint: str, reported: str) -> dict:
    return {
        "ok": ok,
        "action": action,
        "trigger": trigger,
        "started_at": t0,
        "duration_s": round(_now() - t0, 2),
        "steps": steps,
        "verdict_before": diagnose["verdict"] if diagnose else None,
        "problems_before": diagnose["problems"] if diagnose else None,
        "verify": verify,
        "rollback_hint": rollback_hint,
        "reported": reported,
    }
