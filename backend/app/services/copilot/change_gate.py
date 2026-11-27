"""变更确认门：由服务端强制校验，不能依赖前端隐藏按钮。

execute_ticket：唯一执行入口——工单非可执行状态 403；ack_required 项必须
ack=="我已知晓"（否则 403 并提示走警示）；逐项 执行前落快照 before → handler →
落 after → 项状态 done/failed（单项链式续跑，部分失败不阻断后续项）；
全部 done → 工单 done；有失败 → 工单 failed（失败项可续跑重试）。

rollback_ticket：按快照逆向写回（新建=删除，改=恢复 before）→ 工单 rolled_back，
回滚动作本身记审计（agent.change.rollback）。

skip_item：跳过态由服务端管理，项级状态机为 pending→skipped→（恢复）→pending；
skipped 项不可执行（execute_ticket 排除）、可恢复（恢复即回到待确认可执行）；全部项
收口（done/skipped/rolled_back）工单置 done，恢复跳过项则工单重开回 pending。

确认/执行/回滚/取消/跳过全部记审计（action=agent.change.*，中文标签见 services/audit.py）。
红线：只执行 CHANGE_TOOLS 白名单工具的结构化 handler——绝不执行 LLM 产出的
SQL/命令原文；快照不可覆盖（重试落新快照行，按次留存）。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.agent_change import AgentChangeSnapshot, AgentChangeTicket
from app.models.user import User
from app.services.copilot import change_tools

logger = logging.getLogger("qianshu.copilot.change_gate")

# 工单可进入确认执行的状态：pending=待确认；failed=部分失败可续跑；executing=进程中断后的
# 幂等续跑恢复（done 项天然跳过）。其余一律 403——done/rolled_back/dismissed 重放不受理。
_EXECUTABLE = ("pending", "failed", "executing")
# 变更项收口态（不可再执行）：done=已执行 / skipped=已跳过 / rolled_back=已回滚
_ITEM_CLOSED = ("done", "skipped", "rolled_back")
# 工单可调整变更项（跳过/恢复）的状态：可执行态 + done（收口后仍可恢复跳过项，恢复即重开）
_ADJUSTABLE = _EXECUTABLE + ("done",)


def _load_list(ticket: AgentChangeTicket) -> list:
    try:
        change_list = json.loads(ticket.change_list_json or "[]")
    except ValueError:
        change_list = []
    return change_list if isinstance(change_list, list) else []


def _save_list(db: Session, ticket: AgentChangeTicket, change_list: list) -> None:
    # 落库走 secure_store：password/AK 类参数/结果加密入 change_list_json（内存对象不动，
    # 调用方响应仍按明文回显一次）；展示层 ticket_out 另做掩码
    ticket.change_list_json = json.dumps(change_tools.secure_store(change_list),
                                         ensure_ascii=False, default=str)
    db.commit()


def _next_version(db: Session, ticket_id: int, item_idx: int) -> int:
    n = (db.query(AgentChangeSnapshot.id)
         .filter(AgentChangeSnapshot.ticket_id == ticket_id,
                 AgentChangeSnapshot.item_idx == item_idx).count())
    return int(n) + 1   # 按次留存：重试不覆盖旧快照，版本递增


def _ack_required(item: dict) -> bool:
    return bool(item.get("ack_required"))


def execute_ticket(db: Session, ticket_id: int, user: User, items=None, ack: str = "",
                   lang: str = "zh") -> dict:
    """确认门执行。items=None=全部未完成项；items=[idx,...]=逐项勾选执行。

    403 三道闸：①工单状态非 pending/failed（done/rolled_back/dismissed 重放不受理）
    ②所选项全部已完成（重放）③含凭证类变更但 ack !=「我已知晓」。
    403 文案走 app.core.i18n_msg 按 lang 出文（缺省中文，直调方原行为不变）。
    """
    from app.api.v1.clouds import ACK_TEXT, CRED_WARN_VERSION
    from app.core.i18n_msg import t

    ticket = db.get(AgentChangeTicket, int(ticket_id))
    if ticket is None:
        raise HTTPException(404, t("agent.ticket_not_found", lang))
    change_list = _load_list(ticket)
    if ticket.status not in _EXECUTABLE:
        raise HTTPException(403, t("agent.gate_status", lang, status=ticket.status))

    if items is not None:
        try:
            idxs = {int(i) for i in items}
        except (TypeError, ValueError):
            raise HTTPException(400, t("agent.items_must_idx", lang))
        valid = {it.get("idx") for it in change_list}
        unknown = idxs - valid
        if unknown:
            raise HTTPException(400, t("agent.item_not_found", lang, idx=sorted(unknown)))
        selected = [it for it in change_list if it.get("idx") in idxs]
    else:
        selected = list(change_list)

    eligible = [it for it in selected if it.get("status") not in _ITEM_CLOSED]
    if not eligible:
        raise HTTPException(403, t("agent.gate_no_items", lang))

    if any(_ack_required(it) for it in eligible) and (ack or "").strip() != ACK_TEXT:
        raise HTTPException(
            403, t("agent.gate_ack", lang, ack=ACK_TEXT, ver=CRED_WARN_VERSION))

    from app.services import audit
    audit.record(
        db, action="agent.change.confirm",
        user_id=user.id, username=user.username,
        resource_type="agent_change_ticket", resource_id=str(ticket.id),
        detail={"title": ticket.title,
                "items": [it.get("idx") for it in eligible],
                "ack": ack if any(_ack_required(it) for it in eligible) else ""},
    )
    ticket.status = "executing"
    ticket.confirmed_by = user.username
    ticket.confirmed_by_id = user.id   # 同时持久化用户 ID，避免仅以用户名引用确认人
    ticket.confirmed_at = datetime.now()
    db.commit()

    results = []
    for it in eligible:
        spec = change_tools.CHANGE_TOOLS.get(it.get("tool") or "")
        if spec is None:   # 工单被手工篡改/白名单收缩兜底：拒执行该项
            it["status"] = "failed"
            it["result"] = {"error": f"工具 {it.get('tool')} 不在变更白名单内，拒绝执行"}
            results.append({"idx": it.get("idx"), "tool": it.get("tool"), "status": "failed",
                            "result": it["result"]})
            _save_list(db, ticket, change_list)
            continue

        params = change_tools.item_params(it)   # 落库加密 -> 执行明文（老明文行兼容）
        try:
            before = spec["snapshot_fn"](db, params) or {}
        except Exception as e:   # noqa: BLE001  快照失败不执行（没有回滚点就不动手）
            it["status"] = "failed"
            it["result"] = {"error": f"快照失败，已中止该项（无回滚点不动手）: {type(e).__name__}: {str(e)[:150]}"}
            results.append({"idx": it.get("idx"), "tool": it.get("tool"), "status": "failed",
                            "result": it["result"]})
            _save_list(db, ticket, change_list)
            continue

        snap = AgentChangeSnapshot(
            ticket_id=ticket.id, item_idx=int(it.get("idx") or 0),
            target_type=spec["target_type"], target_ref=str(spec["ref_fn"](params))[:200],
            before_blob=json.dumps(before, ensure_ascii=False, default=str),
            after_blob="",
            version=_next_version(db, ticket.id, int(it.get("idx") or 0)),
        )
        db.add(snap)
        db.commit()

        try:
            result = spec["handler"](db, params, user, ack)
            after = spec["snapshot_fn"](db, params) or {}
            snap.after_blob = json.dumps(after, ensure_ascii=False, default=str)
            it["status"] = "done"
            it["result"] = result
        except Exception as e:   # noqa: BLE001  单项失败不阻断后续项（部分失败可续）
            logger.warning("变更项执行失败 ticket=%s idx=%s: %s", ticket.id, it.get("idx"), e)
            it["status"] = "failed"
            it["result"] = {"error": f"{type(e).__name__}: {str(e)[:300]}"}
            try:
                after = spec["snapshot_fn"](db, params) or {}
                snap.after_blob = json.dumps(after, ensure_ascii=False, default=str)
            except Exception:
                pass
        db.commit()
        results.append({"idx": it.get("idx"), "tool": it.get("tool"),
                        "status": it["status"], "result": it.get("result")})
        _save_list(db, ticket, change_list)

    remaining = [it for it in change_list if it.get("status") not in _ITEM_CLOSED]
    if not remaining:
        ticket.status = "done"
    elif any(it.get("status") == "failed" for it in remaining):
        ticket.status = "failed"   # 部分失败：失败项可续跑（再次 confirm 只跑未完成项）
    else:
        ticket.status = "pending"  # 只勾选执行了子集，其余待确认
    ticket.result_json = json.dumps(change_tools.secure_store({"results": results}),
                                    ensure_ascii=False, default=str)
    _save_list(db, ticket, change_list)

    audit.record(
        db, action="agent.change.execute",
        user_id=user.id, username=user.username,
        resource_type="agent_change_ticket", resource_id=str(ticket.id),
        result="ok" if ticket.status == "done" else "partial",
        detail={"results": [{"idx": r["idx"], "tool": r["tool"], "status": r["status"]}
                            for r in results]},
    )
    db.commit()
    return {"ticket_id": ticket.id, "status": ticket.status, "results": results}


def rollback_ticket(db: Session, ticket_id: int, user: User, lang: str = "zh") -> dict:
    """一键回滚：按快照逆向写回（新建=删除，改=恢复 before），逆序逐项处理。

    只有已执行项（status=done）可回滚；回滚本身记审计（agent.change.rollback）。
    400/404 文案按 lang 出文（MSG: agent.*；中文原文保持不变）；
    逐项 result/errors 落库载荷保持中文（持久化口径不变）。
    """
    from app.core.i18n_msg import t

    ticket = db.get(AgentChangeTicket, int(ticket_id))
    if ticket is None:
        raise HTTPException(404, t("agent.ticket_not_found", lang))
    change_list = _load_list(ticket)
    done_items = [it for it in change_list if it.get("status") == "done"]
    if not done_items:
        raise HTTPException(400, t("agent.rollback_no_done", lang))

    from app.services import audit
    rolled, errors = [], []
    for it in reversed(done_items):
        spec = change_tools.CHANGE_TOOLS.get(it.get("tool") or "")
        snaps = (db.query(AgentChangeSnapshot)
                 .filter(AgentChangeSnapshot.ticket_id == ticket.id,
                         AgentChangeSnapshot.item_idx == int(it.get("idx") or 0))
                 .order_by(AgentChangeSnapshot.id.desc()).all())
        snap = next((s for s in snaps if s.after_blob), None)   # 最近一次成功执行的快照
        if spec is None or snap is None:
            errors.append({"idx": it.get("idx"), "tool": it.get("tool"),
                           "error": "白名单无此工具或缺少执行快照，无法回滚"})
            continue
        before = json.loads(snap.before_blob or "{}")
        after = json.loads(snap.after_blob or "{}")
        try:
            r = spec["rollback_fn"](db, change_tools.item_params(it), before, after, user)
            it["status"] = "rolled_back"
            rolled.append({"idx": it.get("idx"), "tool": it.get("tool"), **(r or {})})
        except Exception as e:   # noqa: BLE001
            logger.exception("回滚失败 ticket=%s idx=%s", ticket.id, it.get("idx"))
            errors.append({"idx": it.get("idx"), "tool": it.get("tool"),
                           "error": f"{type(e).__name__}: {str(e)[:200]}"})
    db.commit()

    if errors:
        ticket.status = "failed"   # 回滚未全成：保留可查，人工介入
    else:
        ticket.status = "rolled_back"
    ticket.result_json = json.dumps(change_tools.secure_store({"rolled_back": rolled, "errors": errors}),
                                    ensure_ascii=False, default=str)
    _save_list(db, ticket, change_list)

    audit.record(
        db, action="agent.change.rollback",
        user_id=user.id, username=user.username,
        resource_type="agent_change_ticket", resource_id=str(ticket.id),
        result="ok" if not errors else "failed",
        detail={"rolled": rolled, "errors": errors},
    )
    db.commit()
    return {"rolled_back": True, "ticket_id": ticket.id, "status": ticket.status,
            "items": rolled, "errors": errors}


def dismiss_ticket(db: Session, ticket_id: int, user: User, lang: str = "zh") -> dict:
    """取消工单：只允许未执行的 pending 工单（有已执行项必须先回滚）。
    403/404 文案按 lang 出文（MSG: agent.*）。"""
    from app.core.i18n_msg import t

    ticket = db.get(AgentChangeTicket, int(ticket_id))
    if ticket is None:
        raise HTTPException(404, t("agent.ticket_not_found", lang))
    change_list = _load_list(ticket)
    if any(it.get("status") == "done" for it in change_list):
        raise HTTPException(403, t("agent.dismiss_has_done", lang))
    if ticket.status != "pending":
        raise HTTPException(403, t("agent.dismiss_bad_status", lang, status=ticket.status))

    ticket.status = "dismissed"
    _save_list(db, ticket, change_list)
    from app.services import audit
    audit.record(
        db, action="agent.change.dismiss",
        user_id=user.id, username=user.username,
        resource_type="agent_change_ticket", resource_id=str(ticket.id),
        detail={"title": ticket.title},
    )
    db.commit()
    return {"dismissed": True, "ticket_id": ticket.id}


# ---------------- 变更项跳过/恢复（状态由服务端管理） ----------------

def _recompute_status(ticket: AgentChangeTicket, change_list: list) -> None:
    """项状态变化后聚合工单状态：
      - 全部项收口（done/skipped/rolled_back）→ done（工单收口，跳过计入已处理）
      - 工单原为 done 但恢复出未完项 → 重开（pending / 有失败项 failed），恢复项可继续执行
      - 其余不动（pending/failed/executing 语义照旧）
    """
    remaining = [it for it in change_list if it.get("status") not in _ITEM_CLOSED]
    if not remaining:
        ticket.status = "done"
    elif ticket.status in ("done", "executing"):
        ticket.status = ("failed" if any(it.get("status") == "failed" for it in remaining)
                         else "pending")


def skip_item(db: Session, ticket_id: int, user: User, idx, restore: bool = False,
              lang: str = "zh") -> dict:
    """变更项跳过/恢复。项级状态机：pending → skipped（跳过）；skipped → pending（恢复）。

    - skipped 不可执行（execute_ticket 按 _ITEM_CLOSED 排除），可恢复后照常走确认门
    - 工单级：_ADJUSTABLE（pending/failed/executing/done）内可调整；rolled_back/dismissed 403
    - 幂等：已是目标态直接返回现状（不报错）；审计 agent.change.skip / agent.change.unskip
    - 400/403/note 文案按 lang 出文（MSG: agent.*；中文原文保持不变）
    """
    from app.core.i18n_msg import t

    ticket = db.get(AgentChangeTicket, int(ticket_id))
    if ticket is None:
        raise HTTPException(404, t("agent.ticket_not_found", lang))
    if ticket.status not in _ADJUSTABLE:
        raise HTTPException(403, t("agent.adjust_bad_status", lang, status=ticket.status))
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        raise HTTPException(400, t("agent.skip_bad_idx", lang))
    change_list = _load_list(ticket)
    item = next((it for it in change_list if int(it.get("idx") or 0) == idx), None)
    if item is None:
        raise HTTPException(400, t("agent.item_not_found", lang, idx=idx))
    st = item.get("status") or "pending"
    if not restore:
        if st == "skipped":
            return {"ticket_id": ticket.id, "idx": idx, "status": "skipped",
                    "restored": False, "ticket_status": ticket.status,
                    "note": t("agent.already_skipped", lang)}
        if st != "pending":
            raise HTTPException(400, t("agent.skip_only_pending", lang, status=st))
        item["status"] = "skipped"
        action = "agent.change.skip"
    else:
        if st != "skipped":
            raise HTTPException(400, t("agent.restore_only_skipped", lang))
        item["status"] = "pending"
        action = "agent.change.unskip"
    _save_list(db, ticket, change_list)
    _recompute_status(ticket, change_list)
    db.commit()

    from app.services import audit
    audit.record(
        db, action=action, user_id=user.id, username=user.username,
        resource_type="agent_change_ticket", resource_id=str(ticket.id),
        detail={"idx": idx, "tool": item.get("tool"), "title": item.get("title"),
                "status": item["status"]},
    )
    db.commit()
    return {"ticket_id": ticket.id, "idx": idx, "status": item["status"],
            "restored": bool(restore), "ticket_status": ticket.status}
