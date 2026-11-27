"""千枢智能体会话 API。

命名说明：`app/api/v1/agent.py` 已被主机采集探针接收端（/agent/report）占用，
本模块是「千枢智能体」（copilot）会话 API，URL 前缀同为 /api/agent（契约路径不变），
两组路由无路径冲突、各自独立成文。

SSE 事件契约（严格）：
  event: snapshot   data: {快照 json}
  event: token      data: {"text": "..."}
  event: tool       data: {"name","params_summary","result_summary","ms"}
  event: review     data: {"round","pass","issues","revised","error"}   （两轮交付制审查结论：
                    issues=[{kind,detail,suggestion}]，kind ∈ unsupported_assertion/missing_tool/
                    broken_causal/walkthrough（查询/分析）∪ forbidden_domain/risk_pattern
                    （代码变更计划 diff 审查）；事件在交付文本之前——pass=false 时随后的
                    token 是修订版全文（第一轮文本不外发），revised=true 表示已带 issues 重答）
  event: change     data: {"ticket_id","title","diff_summary","change_list":[{idx,title,impact,
                    rollback,ack_required,sensitive,tool,params,diff_summary,restart_required}]}
                    （diff_summary={files:[{path,add_lines,del_lines,diff_head(首20行)}]}，
                    事件级+项级双份；code_modify 项 restart_required=true=需重启生效）
  event: done       data: {"message_id","model","tokens"}
  event: error      data: {"message","code"}   （未配模型 code=NEED_MODEL）

消息契约（listMessages）：assistant 消息带 review 字段（审查 verdict，未审为 null）：
  review: {"pass","issues":[{kind,detail,suggestion}],"assertions":[{text,status,evidence}],
           "revised","rounds","error"} | null

红线：查询路径只读工具；变更只走 change_tools 白名单 + change_gate 确认门
（未确认 403，服务端强制）；不执行 LLM 生成的命令/SQL 原文。
"""
from __future__ import annotations

import json
import logging
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import require_roles
from app.core.i18n_msg import lang_of, t
from app.models.agent_session import AgentMessage, AgentSession
from app.models.user import User
from app.services.audit import record as audit_record
from app.services.copilot import change_gate, change_tools
from app.services.copilot import loop as copilot_loop
from app.services.copilot import snapshot as snapshot_mod
from app.services.copilot import suggestions as suggestions_mod

logger = logging.getLogger("qianshu.agent_copilot")
router = APIRouter(prefix="/agent", tags=["agent-copilot"])

_ROLES = ("admin", "operator", "viewer")
_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _get_session(db: Session, user: User, session_id: int) -> AgentSession:
    """会话归属：非 admin 只见自己的会话；admin 可见全部（运维透视）。"""
    sess = db.get(AgentSession, session_id)
    if sess is None:
        raise HTTPException(404, "会话不存在")
    if user.role != "admin" and sess.user_id != user.id:
        raise HTTPException(404, "会话不存在")
    return sess


def _session_out(db: Session, sess: AgentSession, message_count: Optional[int] = None) -> dict:
    if message_count is None:
        message_count = (db.query(func.count(AgentMessage.id))
                         .filter(AgentMessage.session_id == sess.id).scalar() or 0)
    return {
        "id": sess.id, "title": sess.title, "pinned": bool(sess.pinned),
        "status": sess.status,
        "user_id": sess.user_id,
        "last_active_at": sess.last_active_at.isoformat() if sess.last_active_at else None,
        "created_at": sess.created_at.isoformat() if sess.created_at else None,
        "message_count": int(message_count),
    }


# ---------------- 系统快照 ----------------

@router.get("/system-snapshot")
def system_snapshot(db: Session = Depends(get_db),
                    user: User = Depends(require_roles(*_ROLES))):
    """系统快照（回答前必查的同一份事实）。"""
    return snapshot_mod.build_snapshot(db)


@router.get("/suggestions")
def agent_suggestions(db: Session = Depends(get_db),
                      user: User = Depends(require_roles(*_ROLES))):
    """工作台空态：快照事实 + 今天建议。

    只读聚合：待确认变更 / 未归属资产 / 账号同步异常 / 总览待办 / 数据就绪。
    每条带 prompt（一键追问）与 link（直达页面），让用户"打开就知道先干什么"，
    而不是面对一个空白聊天框自己想问题。
    """
    return suggestions_mod.build(db, user)


# ---------------- 会话 CRUD ----------------

class SessionCreateIn(BaseModel):
    title: Optional[str] = None


class SessionPatchIn(BaseModel):
    title: Optional[str] = None
    pinned: Optional[bool] = None
    status: Optional[str] = None


@router.get("/sessions")
def list_sessions(q: str = "", page: int = 1, page_size: int = 20,
                  db: Session = Depends(get_db),
                  user: User = Depends(require_roles(*_ROLES))):
    page = max(1, page)
    page_size = max(1, min(100, page_size))
    msg_count = (db.query(AgentMessage.session_id, func.count(AgentMessage.id).label("c"))
                 .group_by(AgentMessage.session_id).subquery())
    qry = (db.query(AgentSession, msg_count.c.c)
           .outerjoin(msg_count, AgentSession.id == msg_count.c.session_id))
    if user.role != "admin":
        qry = qry.filter(AgentSession.user_id == user.id)
    if q.strip():
        like = f"%{q.strip()}%"
        qry = qry.filter(AgentSession.title.like(like))
    total = qry.count()
    rows = (qry.order_by(AgentSession.pinned.desc(), AgentSession.last_active_at.desc())
            .offset((page - 1) * page_size).limit(page_size).all())
    return {
        "items": [_session_out(db, s, int(c or 0)) for s, c in rows],
        "total": total,
    }


@router.post("/sessions")
def create_session(body: SessionCreateIn,
                   db: Session = Depends(get_db),
                   user: User = Depends(require_roles(*_ROLES))):
    """新建会话。title 可选——缺省时由首问自动截断补名。"""
    title = (body.title or "").strip() or "新会话"
    sess = AgentSession(title=title[:200], user_id=user.id)
    db.add(sess)
    db.commit()
    db.refresh(sess)
    audit_record(db, action="agent.session.create", user_id=user.id, username=user.username,
                 resource_type="agent_session", resource_id=sess.id,
                 detail={"title": title[:200]})
    db.commit()
    return _session_out(db, sess, 0)


@router.patch("/sessions/{session_id}")
def patch_session(session_id: int, body: SessionPatchIn,
                  db: Session = Depends(get_db),
                  user: User = Depends(require_roles(*_ROLES))):
    sess = _get_session(db, user, session_id)
    if body.title is not None:
        sess.title = body.title.strip()[:200] or sess.title
    if body.pinned is not None:
        sess.pinned = bool(body.pinned)
    if body.status is not None:
        if body.status not in ("active", "archived"):
            raise HTTPException(400, "status 仅支持 active/archived")
        sess.status = body.status
    db.commit()
    db.refresh(sess)
    return _session_out(db, sess)


@router.delete("/sessions/{session_id}")
def delete_session(session_id: int,
                   db: Session = Depends(get_db),
                   user: User = Depends(require_roles(*_ROLES))):
    sess = _get_session(db, user, session_id)
    title = sess.title
    db.query(AgentMessage).filter(AgentMessage.session_id == sess.id).delete()
    db.delete(sess)
    audit_record(db, action="agent.session.delete", user_id=user.id, username=user.username,
                 resource_type="agent_session", resource_id=session_id, detail={"title": title})
    db.commit()
    return {"deleted": True, "id": session_id}


# ---------------- 消息 ----------------

class MessageIn(BaseModel):
    q: str
    ctx: Optional[dict] = None


@router.get("/sessions/{session_id}/messages")
def list_messages(session_id: int,
                  db: Session = Depends(get_db),
                  user: User = Depends(require_roles(*_ROLES))):
    _get_session(db, user, session_id)
    rows = (db.query(AgentMessage)
            .filter(AgentMessage.session_id == session_id)
            .order_by(AgentMessage.id.asc()).all())
    items = []
    for r in rows:
        try:
            tool_calls = json.loads(r.tool_calls_json or "[]")
        except ValueError:
            tool_calls = []
        try:
            review = json.loads(r.review_json) if r.review_json else None
        except ValueError:
            review = None
        items.append({
            "id": r.id, "role": r.role, "content_md": r.content_md or "",
            "tool_calls": tool_calls, "model": r.model or "", "tokens": r.tokens or 0,
            "review": review,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    # 运行结果持久化：agent_workflow_runs 合成 assistant 消息（msg_kind=workflow_run，
    # 含 steps/artifact/tickets 引用）——重开会话后工作流结果卡仍在；与真消息重复的不合成
    from app.services import agent_schedule as _sched

    items.extend(_sched.workflow_run_messages(db, session_id))
    items.sort(key=lambda m: (m.get("created_at") or "", str(m.get("id"))))
    return {"items": items, "total": len(items)}


@router.post("/sessions/{session_id}/messages")
def post_message(session_id: int, body: MessageIn, request: Request,
                 db: Session = Depends(get_db),
                 user: User = Depends(require_roles(*_ROLES))):
    """发问（SSE 流式回）。body: {q: str, ctx: dict|None}。"""
    sess = _get_session(db, user, session_id)
    question = (body.q or "").strip()
    if not question:
        raise HTTPException(400, "q 不能为空")
    lang = lang_of(request)   # NEED_MODEL 等流内提示按 Accept-Language 出文

    from datetime import datetime
    from types import SimpleNamespace

    # 流内只用原始值/新对象：SSE 生成器执行时请求级 ORM 实例可能已游离（commit 过期+
    # 依赖收尾 detach），user/user_msg 等入流前必须拍平（避免 DetachedInstanceError）
    user_ctx = SimpleNamespace(id=user.id, username=user.username, role=user.role)

    # 落用户消息（先落，断线也不丢问）；首条消息附系统快照；首问自动补会话名
    is_first = (db.query(func.count(AgentMessage.id))
                .filter(AgentMessage.session_id == session_id).scalar() or 0) == 0
    snap_for_first = snapshot_mod.build_snapshot(db) if is_first else None
    user_msg = AgentMessage(
        session_id=session_id, role="user", content_md=question,
        snapshot_json=json.dumps(snap_for_first, ensure_ascii=False, default=str)
        if snap_for_first else "",
    )
    db.add(user_msg)
    if is_first and (sess.title or "").strip() in ("", "新会话"):
        sess.title = question[:20] or "新会话"
    sess.last_active_at = datetime.now()
    db.commit()
    msg_id = user_msg.id   # commit 后立即取（实例可能过期，但此刻会话仍绑定）

    copilot_loop.clear_abort(session_id)   # 新一轮清除旧中断标志

    def gen():
        out: dict = {}
        try:
            for event, data in copilot_loop.run_turn(
                    db, session_id, question, body.ctx, out,
                    should_abort=lambda: copilot_loop.is_aborted(session_id),
                    snapshot=snap_for_first,
                    user=user_ctx, msg_id=msg_id, lang=lang):
                yield _sse(event, data)
        except copilot_loop.NeedModel:
            copilot_loop.clear_abort(session_id)
            # 固定引导文案（loop 抛出的中文原文弃用）：按请求语言出文；code=NEED_MODEL 契约不变
            yield _sse("error", {"message": t("agent.need_model", lang), "code": "NEED_MODEL"})
            return
        except Exception as e:   # noqa: BLE001  流内异常→error 事件收尾，不裸抛 500
            logger.exception("copilot 回答失败 session=%s", session_id)
            copilot_loop.clear_abort(session_id)
            yield _sse("error", {"message": f"{type(e).__name__}: {str(e)[:200]}",
                                 "code": "ERROR"})
            return

        # 助手消息落库（工具轨迹/模型/tokens 可回溯）；会话活跃时间走 query.update
        # （sess 实例在流内可能已游离，挂载更新会静默丢失——同类坑一并修）
        assistant_msg = AgentMessage(
            session_id=session_id, role="assistant",
            content_md=out.get("content_md") or "",
            tool_calls_json=json.dumps(out.get("tool_calls") or [], ensure_ascii=False),
            review_json=json.dumps(out.get("review"), ensure_ascii=False, default=str)
            if out.get("review") else "",
            model=out.get("model") or "",
            tokens=int(out.get("tokens") or 0),
        )
        db.add(assistant_msg)
        db.flush()
        db.query(AgentSession).filter(AgentSession.id == session_id).update(
            {"last_active_at": datetime.now()})
        db.commit()
        db.refresh(assistant_msg)
        copilot_loop.clear_abort(session_id)   # 流式回答完成后复位中断标志
        yield _sse("done", {"message_id": assistant_msg.id,
                            "model": out.get("model") or "",
                            "tokens": int(out.get("tokens") or 0)})

    return StreamingResponse(gen(), media_type="text/event-stream", headers=_SSE_HEADERS)


@router.post("/sessions/{session_id}/abort")
def abort_turn(session_id: int,
               db: Session = Depends(get_db),
               user: User = Depends(require_roles(*_ROLES))):
    """中断本轮生成（进程内 per-session 标志，loop 每轮检查）。"""
    _get_session(db, user, session_id)
    copilot_loop.request_abort(session_id)
    return {"aborted": True}


# ---------------- 变更工单（确认门/回滚/取消） ----------------
# 服务端强制确认门：执行只发生在 confirm 内（change_gate.execute_ticket），无旁路执行端点。
# 权限（功能设计 §五）：确认/回滚/取消/跳过 admin；列表 operator/viewer 可读。

from app.models.agent_change import AgentChangeTicket  # noqa: E402  （路由层局部导入防环）


class ChangeConfirmIn(BaseModel):
    items: Optional[List[int]] = None   # 逐项勾选（缺省=全部未完成项）
    ack: str = ""                       # 凭证类变更须 "我已知晓"（服务端强制）


@router.get("/changes")
def list_changes(status: str = "",
                 db: Session = Depends(get_db),
                 _user: User = Depends(require_roles(*_ROLES))):
    """变更工单列表（跨会话待办），ticket 含 change_list 数组。status 可过滤。"""
    qry = db.query(AgentChangeTicket)
    if status.strip():
        qry = qry.filter(AgentChangeTicket.status == status.strip())
    rows = qry.order_by(AgentChangeTicket.id.desc()).limit(200).all()
    items = [change_tools.ticket_out(t) for t in rows]
    return {"items": items, "total": len(items)}


@router.post("/changes/{ticket_id}/confirm")
def confirm_change(ticket_id: int, body: ChangeConfirmIn, request: Request,
                   db: Session = Depends(get_db),
                   user: User = Depends(require_roles("admin"))):
    """确认执行（确认门：未确认 403；ack_required 项须「我已知晓」）。返回逐项结果。"""
    return change_gate.execute_ticket(db, ticket_id, user, items=body.items, ack=body.ack,
                                      lang=lang_of(request))


@router.post("/changes/{ticket_id}/rollback")
def rollback_change(ticket_id: int, request: Request,
                    db: Session = Depends(get_db),
                    user: User = Depends(require_roles("admin"))):
    """一键回滚：按快照逆向写回（新建=删除，改=恢复 before），回滚本身留痕（回执含逐项结果）。
    异常文案按 Accept-Language 出文。"""
    r = change_gate.rollback_ticket(db, ticket_id, user, lang=lang_of(request))
    return {"rolled_back": True, **{k: v for k, v in r.items() if k != "rolled_back"}}


@router.post("/changes/{ticket_id}/dismiss")
def dismiss_change(ticket_id: int, request: Request,
                   db: Session = Depends(get_db),
                   user: User = Depends(require_roles("admin"))):
    """取消工单（只允许未执行的 pending 工单；有已执行项须先回滚）。
    异常文案按 Accept-Language 出文。"""
    change_gate.dismiss_ticket(db, ticket_id, user, lang=lang_of(request))
    return {"dismissed": True, "ticket_id": ticket_id}


class ChangeSkipIn(BaseModel):
    idx: int                                # 变更项序号（change_list[].idx）
    restore: bool = False                   # True=恢复（skipped→pending）；False=跳过（pending→skipped）


@router.post("/changes/{ticket_id}/skip")
def skip_change(ticket_id: int, body: ChangeSkipIn, request: Request,
                db: Session = Depends(get_db),
                user: User = Depends(require_roles("admin"))):
    """变更项跳过/恢复，状态落服务端，刷新不丢。

    状态机：pending→skipped（跳过，不可执行）；skipped→pending（restore=true 恢复，可执行）。
    异常/note 文案按 Accept-Language 出文。
    """
    return change_gate.skip_item(db, ticket_id, user, idx=body.idx, restore=body.restore,
                                 lang=lang_of(request))


# ---------------- 代码快照（L3b 平台自改：快照列表/diff 预览/一键回滚，方案 §2.3） ----------------
# 复用确认门语义：回滚只走本端点（admin 服务端强制，无旁路执行端点）→ code_mod.rollback
# （逐文件字节恢复+回滚后验证门）→ 双审计（code.rollback / agent.change.rollback 留痕）；
# 快照不可覆盖按次留存（agent_code_snapshots），全文 before/after 只入快照不出参。

@router.get("/code-snapshots")
def list_code_snapshots(ticket_id: int = 0,
                        db: Session = Depends(get_db),
                        _user: User = Depends(require_roles(*_ROLES))):
    """代码改动快照列表（ticket_id 可选过滤）。

    items=[{id, ticket_id, created_at, status, verified,
            files:[{path,add_lines,del_lines,diff_head(首20行)}], diff_summary}]。
    """
    items = change_tools.code_snapshots_out(db, ticket_id)
    return {"items": items, "total": len(items)}


class RollbackIn(BaseModel):
    force: bool = False   # 跨快照冲突时是否强制还原（覆盖第三方改动，服务端默认拒）


class ApplyRestartIn(BaseModel):
    confirm: bool = False   # 二次确认（服务端强制：未确认 403）


@router.post("/code-snapshots/{snapshot_id}/rollback")
def rollback_code_snapshot(snapshot_id: int,
                           body: Optional[RollbackIn] = None,
                           db: Session = Depends(get_db),
                           user: User = Depends(require_roles("admin"))):
    """一键回滚代码快照（admin；复用确认门语义：服务端强制+审计留痕，逐文件字节恢复）。

    快照此前已回滚时幂等返回（already=true，不动文件）；回滚动作本身记审计。
    跨快照冲突（目标文件应用后被第三方改动）默认拒绝强还原（400 说明冲突文件），
    带 force=true 才覆盖第三方改动。
    """
    try:
        r = change_tools.code_snapshot_rollback(db, snapshot_id, user,
                                                force=bool(body and body.force))
    except change_tools.ChangeToolError as e:
        raise HTTPException(400, str(e))
    return r


@router.post("/code-snapshots/{snapshot_id}/apply-restart")
def apply_restart_code_snapshot(snapshot_id: int, body: ApplyRestartIn,
                                background: BackgroundTasks,
                                db: Session = Depends(get_db),
                                user: User = Depends(require_roles("admin"))):
    """「重启生效」：委托 qianshu-copilotd 本地通道 POST /restart 重启平台（admin + 二次确认）。

    code_modify 执行成功回执含「需重启生效」；本端点用于应用该重启动作：快照须 applied，
    通道预检通过后先记审计（code.restart）再在响应送达后异步委托重启（重启会终止本
    进程，回执由 copilotd 落动作日志并经桥上报站内信）。拒绝路径：未确认 403 /
    快照不可用 400 / copilotd 通道不可用 400。
    """
    if not body.confirm:
        raise HTTPException(403, "重启生效需二次确认（confirm=true）——重启会中断当前服务")
    try:
        return change_tools.code_apply_restart(
            db, snapshot_id, user, confirm=True,
            defer=lambda fn: background.add_task(fn))
    except change_tools.ChangeToolError as e:
        raise HTTPException(400, str(e))
