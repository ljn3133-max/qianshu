"""R3：把已审批的结构化补丁交给现有 change_tools/code_mod 管道。

不新写文件写入器；快照、原子应用、验证门、失败自动回滚全部复用现有实现。
自愈核心/权限/审计/copilotd 路径由 code_mod.CODE_DENY + validator 双重拒绝。
"""
from __future__ import annotations

import json
import logging

from app.models.issue import IssueCase, IssuePlan
from app.services.self_heal import service

logger = logging.getLogger("qianshu.self_heal.code_repair")


def _plan_payload(plan: IssuePlan) -> dict:
    data = json.loads(plan.plan_json or "{}")
    for step in data.get("steps") or []:
        if step.get("recipe") == "patch_platform_code":
            patch = (step.get("params") or {}).get("plan") or {}
            patch = dict(patch)
            patch.setdefault("summary", data.get("summary") or issue_title(data))
            patch.setdefault("impact", data.get("impact") or "")
            patch["files"] = patch.get("files") or []
            return patch
    return {}


def issue_title(data: dict) -> str:
    return str(data.get("summary") or "")[:200]


def execute_patch(db, issue: IssueCase, plan: IssuePlan, user) -> dict:
    """执行已审批补丁。返回 {ok, ticket_id, snapshot_id, result}；失败抛异常。"""
    from app.api.v1.clouds import ACK_TEXT
    from app.services.copilot import change_gate, change_tools

    patch = _plan_payload(plan)
    if not patch.get("files"):
        raise ValueError("计划中没有代码补丁")
    items = [{"tool": "code_modify", "params": {"plan": patch}}]
    ticket = change_tools.build_ticket(
        db, session_id=0, msg_id=0, items=items, user=user,
        title=f"[平台自愈] 问题 #{issue.id} 代码修复")
    ticket_id = int(ticket.get("id") or 0)
    service.add_event(db, issue.id, "repair_started", payload={
        "plan_id": plan.id, "recipe": "patch_platform_code",
        "ticket_id": ticket_id, "files": len(patch.get("files") or [])})
    db.commit()
    try:
        gate = change_gate.execute_ticket(db, ticket_id, user, ack=ACK_TEXT)
    except Exception as exc:  # HTTPException/ChangeToolError
        detail = getattr(exc, "detail", None) or str(exc)
        raise RuntimeError(f"代码补丁执行被拒绝或失败：{str(detail)[:300]}") from exc
    results = gate.get("results") or []
    item = results[0] if results else {}
    if item.get("status") != "done":
        err = (item.get("result") or {}).get("error") if isinstance(item.get("result"), dict) else ""
        raise RuntimeError(f"代码补丁执行失败：{str(err or item.get('status'))[:300]}")
    payload = item.get("result") or {}
    inner = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    snapshot_id = int(inner.get("snapshot_id") or 0)
    plan.result_json = service._dumps({"ticket_id": ticket_id,
                                       "snapshot_id": snapshot_id,
                                       "gate": "done",
                                       "files": [f.get("path") for f in patch.get("files") or []]})
    db.add(plan)
    service.add_event(db, issue.id, "step_done", payload={
        "plan_id": plan.id, "recipe": "patch_platform_code",
        "ticket_id": ticket_id, "snapshot_id": snapshot_id})
    db.commit()
    return {"ok": True, "ticket_id": ticket_id, "snapshot_id": snapshot_id,
            "result": inner}


def _snapshot_id(plan: IssuePlan) -> int:
    try:
        return int((json.loads(plan.result_json or "{}") or {}).get("snapshot_id") or 0)
    except Exception:
        return 0


def verify_patch(db, issue: IssueCase, plan: IssuePlan) -> tuple[bool, str]:
    sid = _snapshot_id(plan)
    if not sid:
        return False, "没有代码快照 id"
    try:
        from app.services.copilot import code_mod
        out = code_mod.snapshot_out(sid) or {}
    except Exception as exc:  # noqa: BLE001
        return False, f"快照读取失败：{type(exc).__name__}"
    status = out.get("status")
    files = [f.get("path") for f in out.get("files") or []]
    if status == "applied":
        return True, f"补丁已落盘并通过验证门（快照 #{sid}，{len(files)} 文件）"
    return False, f"快照 #{sid} 状态 {status}，未处于已应用状态"


def rollback_patch(db, issue: IssueCase, plan: IssuePlan, user,
                   reason: str = "") -> dict:
    sid = _snapshot_id(plan)
    if not sid:
        return {"ok": False, "error": "没有快照可回滚"}
    try:
        from app.services.copilot import code_mod
        result = code_mod.rollback(sid, user)
    except Exception as exc:  # noqa: BLE001
        logger.warning("自愈代码回滚失败 issue=%s snapshot=%s", issue.id, sid, exc_info=True)
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    ok = bool(result.get("ok"))
    service.add_event(db, issue.id, "rolled_back" if ok else "rollback_failed",
                      payload={"plan_id": plan.id, "snapshot_id": sid,
                               "reason": reason[:300],
                               "error": str(result.get("error") or "")[:300]})
    db.commit()
    return result
