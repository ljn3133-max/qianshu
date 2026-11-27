"""值班闭环服务：认领（ack）+ 无人响应升级链。

流程：critical 告警产生 → 通知值班表 level=1 → N 分钟无人认领 → 升级 level=2 → …
认领后停止升级（ack≠恢复：ack=有人在管，recovered=问题没了）。

安全红线机制化（NOTIFY_TEST_MODE）：测试场景强制落测试渠道（默认站内信），
外发消息只在真实运行中发生——红线从"人的记性"变成"机制"。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.alert_record import AlertRecord
from app.models.oncall import AlertAck, AlertEscalation, OncallContact

logger = logging.getLogger("qianshu.oncall")


def ack_alert(db: Session, alert_id: int, user: str, note: str = "", user_id: int = 0) -> dict:
    """认领告警：停止升级链、留痕。

    身份引用统一：ack_by=username 快照，ack_by_id=users.id 双写。
    """
    if db.query(AlertAck).filter(AlertAck.alert_id == alert_id).first():
        return {"ok": True, "already": True}
    db.add(AlertAck(alert_id=alert_id, ack_by=user, ack_by_id=user_id or 0, note=note[:500]))
    (db.query(AlertEscalation)
     .filter(AlertEscalation.alert_id == alert_id, AlertEscalation.acked == False)  # noqa: E712
     .update({AlertEscalation.acked: True}, synchronize_session=False))
    db.commit()
    return {"ok": True}


def is_acked(db: Session, alert_id: int) -> bool:
    return db.query(AlertAck).filter(AlertAck.alert_id == alert_id).first() is not None


_FAILED_DETAIL_PREFIX = "FAILED: "


def escalation_status(row: AlertEscalation) -> tuple[bool, str]:
    """从现有 detail 还原发送状态；历史记录没有前缀，保持既有的成功口径。"""
    detail = row.detail or ""
    if detail.startswith(_FAILED_DETAIL_PREFIX):
        return False, detail[len(_FAILED_DETAIL_PREFIX):]
    return True, detail


def _notify_contact(db: Session, contact: OncallContact, title: str, content: str,
                    module: str = "oncall", account_id: int = 0) -> dict:
    """升级通知：主通道走统一出口（渠道表=唯一发送配置），值班表只管人与顺序。

    联系人上的手机号仅用于短信/电话升级（渠道 API 按订单接入，未接时站内信兜底）。
    值班表不自带发送体系，与告警渠道配置不冲突。
    module/account_id：升级链也受模块/账号去向约束，
    低优先级账号关掉外推/站内信后不会再被升级短信轰炸。
    """
    from app.services.notify import notify_all_channels

    r = notify_all_channels(db, f"{title}（@{contact.name}）",
                            content + f"\n\n本级值班人: {contact.name}"
                                      + (f" / {contact.contact}" if contact.contact else ""),
                            level="critical", module=module, account_id=account_id)
    # 短信/电话升级（有手机号时）：真渠道调用（AK 走平台凭证；测试模式绝不真发）
    direct_ok = None
    if contact.channel in ("sms", "phone") and contact.contact:
        try:
            from app.collector.credential import resolve_aliyun_credential
            from app.services.notify import send_sms, send_voice_call

            ak, sk, _ = resolve_aliyun_credential(db)
            if contact.channel == "sms":
                r2 = send_sms(ak, sk, contact.contact, settings.SMS_SIGN_NAME,
                              settings.SMS_TPL_CODE, '{"alert":"%s"}' % title[:30])
            else:
                r2 = send_voice_call(ak, sk, contact.contact, settings.VOICE_TTS_CODE)
            r["sms"] = r2
            direct_ok = bool(r2.get("ok"))
            if not direct_ok:
                r.setdefault("errors", []).append(
                    f"{contact.channel}: {r2.get('detail') or '发送失败'}")
        except Exception as e:
            direct_ok = False
            r.setdefault("errors", []).append(
                f"{contact.channel}: {str(e)[:160] or type(e).__name__}")
            logger.info("短信/电话升级失败（已落站内信/群渠道兜底）: %s", str(e)[:80])
    # 邮件升级已下线（2026-11-12）：历史 email 联系人仍会收到群渠道/站内信兜底，
    # 但不再单独发邮件。
    # 短信/电话是联系人指定的直达渠道，站内信/群成功不能把它伪装成已完成；
    # 其他联系人保持至少一路送达即成功，部分渠道错误仅留痕。
    r["ok"] = direct_ok if direct_ok is not None else bool(r.get("sent"))
    return r


def escalate_due(db: Session) -> dict:
    """扫描到期未认领的 critical 告警；成功级别不再重试，失败级别下轮重试。"""
    if not settings.ONCALL_ENABLED:
        return {"escalated": [], "failed": []}
    window = datetime.now() - timedelta(minutes=settings.ALERT_ESCALATE_MINUTES)
    rows = (db.query(AlertRecord)
            .filter(AlertRecord.status == "firing", AlertRecord.level == "critical",
                    AlertRecord.fired_at <= window)
            .all())
    escalated, failed = [], []
    for alert in rows:
        if is_acked(db, alert.id):
            continue
        history = (db.query(AlertEscalation)
                   .filter(AlertEscalation.alert_id == alert.id).all())
        done_levels = {e.level for e in history if escalation_status(e)[0]}
        # 下一个未成功通知的级别；失败尝试留在 history，但不占用 done_levels。
        contacts = (db.query(OncallContact)
                    .filter(OncallContact.enabled == True)  # noqa: E712
                    .order_by(OncallContact.level).all())
        next_c = next((c for c in contacts if c.level not in done_levels), None)
        if next_c is None:
            continue   # 全部通知过了（或值班表为空——站内信兜底在告警产生时已有）
        # 分级提醒：值班链总开关（oncall 模块）与该告警自身的模块/账号去向都要放行，
        # 否则「vCenter 低优先级、已关外推」会被升级短信绕开
        from app.services.monitor_policy import can_push
        alert_module = getattr(alert, "module", None) or "alert"
        alert_account = getattr(alert, "account_id", 0) or 0
        if (not can_push(db, "oncall", 0)
                or not can_push(db, alert_module, alert_account)):
            continue
        title = f"[升级 L{next_c.level}] {alert.title}"
        content = (f"告警 {settings.ALERT_ESCALATE_MINUTES} 分钟无人认领，升级到你。\\n"
                   f"- 级别: {alert.level}\\n- 目标: {alert.target_name or alert.target}\\n"
                   f"- 详情: {(alert.content or '')[:200]}\\n请尽快认领处理。")
        r = _notify_contact(db, next_c, title, content,
                            module=alert_module, account_id=alert_account)
        ok = bool(r.get("ok"))
        detail = "; ".join(str(e) for e in r.get("errors", []) if str(e))[:200]
        if not ok and not detail:
            detail = "通知发送失败"
        stored_detail = f"{_FAILED_DETAIL_PREFIX}{detail}"[:200] if not ok else detail
        db.add(AlertEscalation(alert_id=alert.id, level=next_c.level,
                               contact_name=next_c.name, channel=r.get("channel", next_c.channel),
                               detail=stored_detail))
        item = {"alert_id": alert.id, "level": next_c.level,
                "contact": next_c.name, "detail": detail}
        (escalated if ok else failed).append(item)
    db.commit()
    return {"escalated": escalated, "failed": failed}
