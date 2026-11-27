"""非接入来源的问题上报适配器：后台任务（job）与智能体只读工具/工作流（agent）。

只做“结构化失败 → IssueCase”的转换；不执行任何修复，也不接触外部渠道。
后台循环的 guard 必须在单轮失败后继续下一轮，因此这里永不抛异常。
"""
from __future__ import annotations

from app.services.self_heal import service

_ERROR_DETAIL_MAX = 800


def _detail(exc) -> str:
    try:
        return f"{type(exc).__name__}: {str(exc)[:_ERROR_DETAIL_MAX]}"
    except Exception:  # noqa: BLE001  非标准异常对象的字符串化失败不阻断上报
        return type(exc).__name__


def report_job_failure(job: str, exc, *, title: str = "", capability: str = "",
                       account_id: int = 0, source_ref=None) -> None:
    """后台任务单轮失败：同一任务同一异常类型合并为一条问题单。

    指纹只用异常类型而不是完整消息：定时任务重试时消息里的时间/UUID/数量
    会不断变化，逐字进指纹会把一次持续故障拆成无数条问题。
    """
    job = (job or "unknown").strip()[:64]
    detail = _detail(exc)
    service.report_issue_safe(
        source="job",
        fingerprint_parts={"error_class": f"job_{job}"},
        title=title or f"后台任务失败：{job}",
        reason_code="job_failed",
        error="job_failure",
        capability=capability,
        account_id=account_id,
        target={"job": job},
        source_ref={"job": job, **(source_ref or {})},
        evidence={"job": job, "exception": detail},
        retryable=True,
    )


def report_agent_failure(tool: str, exc, *, capability: str = "",
                         account_id: int = 0) -> None:
    """智能体工具/工作流的平台侧失败；用户参数错误不应调用本函数。"""
    tool = (tool or "unknown").strip()[:64]
    service.report_issue_safe(
        source="agent",
        fingerprint_parts={"error_class": f"agent_{tool}"},
        title=f"智能体工具执行失败：{tool}",
        reason_code="agent_tool_failed",
        error="agent_tool_failure",
        capability=capability,
        account_id=account_id,
        target={"tool": tool},
        source_ref={"tool": tool},
        evidence={"tool": tool, "exception": _detail(exc)},
        retryable=True,
    )
