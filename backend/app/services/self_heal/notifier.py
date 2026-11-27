"""站内信通知收口：只写 InAppMessage，绝不触碰企微/钉钉/短信/电话。

每类状态每个问题最多一条：新问题、待授权、待用户、恢复、复发、升级人工。
调用方负责“只在状态变化时调用”，notifier 本身不做轮询。
"""
from __future__ import annotations

from app.models.issue import IssueCase
from app.services.notify import notify_inapp

_KIND_TITLES = {
    "detected": "发现平台问题",
    "awaiting_approval": "需要授权修复",
    "awaiting_user": "需要你处理",
    "recovered": "问题已恢复",
    "recurring": "问题复发",
    "escalated": "问题已升级人工",
    "plan_rejected": "修复计划被拒绝",
    "diagnosed": "问题诊断完成",
    "ignored": "问题已忽略",
}


def _brief(text: str, limit: int = 800) -> str:
    text = (text or "").strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def notify_issue(db, issue: IssueCase, kind: str, *, detail: str = "") -> int:
    """写站内信。返回写入条数；调用方负责 commit。"""
    title = f"[平台自愈] {_KIND_TITLES.get(kind, kind)}：{(issue.title or '平台问题')[:70]}"
    next_hint = {
        "detected": "可在「智能体 → 问题收件箱」查看原因、证据与下一步。",
        "awaiting_approval": "计划需要管理员按完整计划哈希授权，未授权不会执行。",
        "awaiting_user": "请按协助步骤处理后，在问题详情点「我已处理，继续」；智能体会重新预检。",
        "recovered": "问题已通过验证恢复；若再次出现会自动升级人工。",
        "recurring": "同一问题在恢复后再次出现，已停止自动重试，请人工确认。",
        "diagnosed": "诊断结论已写回问题详情；可在收件箱查看证据、方案与下一步。",
        "escalated": "自动处理已停止，问题保留证据等待人工处理。",
        "plan_rejected": "修复计划未通过服务端安全校验，未执行任何动作。",
        "ignored": "该问题已被管理员忽略，不再自动提醒。",
    }.get(kind, "")
    lines = [
        f"**{issue.title or '平台问题'}**",
        "",
        f"- 来源：`{issue.source}`；能力：`{issue.capability or '平台'}`；"
        f"账号：`{issue.account_id or '平台'}`",
        f"- 状态：`{issue.status}`；原因码：`{issue.reason_code or '未知'}`；"
        f"发生次数：{issue.occurrences or 1}",
    ]
    if issue.summary_md:
        lines += ["", _brief(issue.summary_md)]
    if detail:
        lines += ["", _brief(detail)]
    if issue.root_cause_md:
        lines += ["", "**原因摘要**", _brief(issue.root_cause_md, 500)]
    if issue.impact_md:
        lines += ["", "**影响范围**", _brief(issue.impact_md, 400)]
    if next_hint:
        lines += ["", f"> {next_hint}"]
    level = "critical" if issue.severity == "critical" else (
        "warning" if issue.severity == "warning" else "info")
    return notify_inapp(
        db, title[:255], "\n".join(lines), level=level, msg_type="agent_issue",
        ref_id=issue.id, audience="admins",
    )
