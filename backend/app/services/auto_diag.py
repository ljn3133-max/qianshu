"""critical 自动诊断。

critical 事件推送时**自动跑一次诊断 Agent**（只读取证），把根因结论作为后续消息
补进站内信，避免告警只有越限规则和数字、仍需人工另行发起诊断。

安全红线：诊断 Agent 工具全部只读（diag_tools 白名单）；LLM 输出仅作为
文本展示，绝不执行其生成的任何命令；同目标冷却 AUTO_DIAG_COOLDOWN_MIN
分钟控 LLM 成本。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

from app.core.config import settings

logger = logging.getLogger("qianshu.auto_diag")

_last_run: dict = {}   # {target_key: datetime}
_lock = threading.Lock()


def _in_cooldown(target: str) -> bool:
    with _lock:
        t = _last_run.get(target)
        return bool(t and (datetime.now() - t).total_seconds()
                    < settings.AUTO_DIAG_COOLDOWN_MIN * 60)


def fire_auto_diag(title: str, content: str, target: str, user_id: int = 1) -> bool:
    """后台跑一次诊断并把结论补进站内信。返回是否启动（冷却中则 False）。"""
    if not settings.AUTO_DIAG_ENABLED:
        return False
    if _in_cooldown(target):
        return False
    with _lock:
        _last_run[target] = datetime.now()

    def _run() -> None:
        from app.core.db import SessionLocal
        from app.models.issue import IssueCase
        from app.services.diagnosis import run_diagnosis
        from app.services.self_heal import notifier, service
        from app.services.self_heal.service import report_issue_safe

        issue = report_issue_safe(
            source="alert", severity="critical",
            title=f"严重告警触发自动诊断：{title[:120]}",
            reason_code="critical_alert", error=title,
            target={"target": target[:120]},
            source_ref={"target": target[:200], "channel": "in_app_auto_diag"},
            evidence={"target": target[:200], "auto_diag": True},
            retryable=False,
        )
        # 总开关/熔断关闭时上报返回 None：不再调用模型，问题仍由告警本身可见。
        if issue is None:
            return
        issue_id = issue.id

        from app.services.llm import LLMError

        db = SessionLocal()
        try:
            question = (
                f"平台监测到严重事件，请分析根因并给处置建议：\n"
                f"- 事件：{title}\n- 详情：{content[:600]}\n"
                f"请沿对应场景（链路/成本/数据库）取证后给出根因与建议动作。"
            )
            try:
                sess = run_diagnosis(db, user_id, question, trigger="auto")
            except LLMError as e:
                logger.warning("自动诊断失败（LLM）: %s", str(e)[:150])
                return
            except Exception:
                logger.exception("自动诊断异常")
                return
            concl = (sess.get("conclusion_md") or "").strip()
            brief = concl[:500] + ("…" if len(concl) > 500 else "")
            row = db.get(IssueCase, issue_id)
            if row is None:
                return
            row.root_cause_md = concl[:8000]
            evidence = service._loads(row.evidence_json, {})
            evidence.update({"auto_diag_session_id": sess.get("id"),
                             "auto_diag_level": sess.get("level") or "",
                             "auto_diag_conclusion": brief})
            row.evidence_json = service._dumps(evidence)
            db.add(row)
            service.add_event(db, row.id, "diagnosis_done", actor="auto_diag",
                              payload={"session_id": sess.get("id"),
                                       "level": sess.get("level") or ""})
            db.commit()
            # 结论写回同一问题单，不再另发一条独立的 [AI 分析] 消息。
            try:
                notifier.notify_issue(db, row, "diagnosed", detail=brief)
                db.commit()
            except Exception:
                db.rollback()
                logger.warning("自动诊断结论站内信写入失败（问题单已保留）", exc_info=True)
            # 决策闭环：24h 后自动复查建议是否见效
            from app.services.recheck import enqueue_recheck
            enqueue_recheck(db, kind="auto_diag", target_key=target,
                            title=title, detail=brief, hours=24)
            db.commit()
            logger.info("自动诊断完成 session=%s issue=%s target=%s",
                        sess.get("id"), issue_id, target)
        finally:
            db.close()

    threading.Thread(target=_run, daemon=True, name="auto-diag").start()
    return True
