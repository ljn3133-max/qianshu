"""今日决策清单：每天一条，聚合需要人工决策的事项，避免碎片通知。

三档分诊里它是第二档：
- 🔴 critical 实时推（告警/关机/外溢）
- 🟡 **本清单**（每天一条）：要买的包、要确认的构成漂移、要续的到期、未处理异动
- ⚪ 其余站内信留档

渠道推送跟随「巡检日报推送」开关（push_settings）——对外发消息的总闸
在用户手里（安全红线）。
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.alert_record import AlertRecord
from app.models.cost_anomaly import CostAnomaly
from app.models.in_app_message import InAppMessage
from app.models.inspection_report import PushSetting
from app.services.purchase_advice import purchase_advice

logger = logging.getLogger("qianshu.decision_digest")


def build_digest(db: Session) -> dict:
    """汇总今日决策项，返回 {title, content, counts}。

    清单本身就是站内信，每节按来源模块/账号的去向过滤；
    低优先级账号已通知过一次的事项不再每天重复占位，等 next_notify_at 到期再说。
    """
    from datetime import date, timedelta

    from app.services.monitor_policy import can_inapp, module_of_anomaly, priority_of

    today = date.today()
    sections: list = []

    def _show_anomaly(a) -> bool:
        if not can_inapp(db, module_of_anomaly(a), getattr(a, "account_id", 0)):
            return False
        # 低优先级：只保留「还没通知过」的；确认/忽略后的重申由提醒任务统一投放
        if priority_of(db, getattr(a, "account_id", 0)) == "low":
            return not (a.notify_count or 0)
        return True

    # 1) 正在烧钱/要动手的
    advice = purchase_advice(db)
    buy_items = [i for i in advice.get("items", []) if i["kind"] in ("buy", "cost")] \
        if can_inapp(db, "cost", 0) else []
    if buy_items:
        # 金额统一使用账单金额（pretax_amount），判断花费以此为准
        sections.append("**要花钱拍板的**\n" + "\n".join(
            f"- {i['title']}｜{i['detail']}" for i in buy_items[:5])
            + "\n（金额口径：账单金额为准；payment_amount 是挂账现金字段（常 0），不作花费依据；"
              "T+1 账单非终态，近 7 天可能补账修订）")

    # 2) 待确认的构成漂移（"是不是新业务"要人定）
    drifts = (db.query(CostAnomaly)
              .filter(CostAnomaly.type == "profile_drift", CostAnomaly.ack_status == "open",
                      CostAnomaly.last_seen >= today - timedelta(days=7)).all())
    drifts = [a for a in drifts if _show_anomaly(a)]
    if drifts:
        sections.append("**要确认是不是正常变化的**\n" + "\n".join(
            f"- {a.title}" for a in drifts[:5]))

    # 3) 未处理的其他异动（critical/warning）
    opens = (db.query(CostAnomaly)
             .filter(CostAnomaly.ack_status == "open",
                     CostAnomaly.last_seen >= today - timedelta(days=7),
                     CostAnomaly.level.in_(("critical", "warning")))
             .order_by(CostAnomaly.level).limit(8).all())
    opens = [a for a in opens if a.type not in ("profile_drift",) and _show_anomaly(a)]
    if opens:
        sections.append("**未处理的异动**\n" + "\n".join(
            f"- [{a.level}] {a.title}" for a in opens[:6]))

    # 4) 正在触发的告警（含内置关机/外溢）
    firing = (db.query(AlertRecord)
              .filter(AlertRecord.status == "firing",
                      AlertRecord.ack_status == "none")
              .order_by(AlertRecord.level).limit(32).all())
    firing = [r for r in firing
              if can_inapp(db, getattr(r, "module", None) or "alert",
                           getattr(r, "account_id", 0))
              and not (priority_of(db, getattr(r, "account_id", 0)) == "low"
                       and (r.notify_count or 0))][:8]
    if firing:
        sections.append("**正在触发的告警**\n" + "\n".join(
            f"- [{r.level}] {r.rule_name} · {r.target_name or r.target}"
            + (f"（当前 {r.breach_value:g}）" if r.breach_value is not None else "")
            for r in firing[:6]))

    if not sections:
        content = "今天没有需要你拍板的事——各监测面无异常。"
    else:
        content = "\n\n".join(sections)
        # 零丢失保障：首页只显示 8 条（新事项优先），本清单是全量总账
        total_open = len(drifts) + len(opens) + len(firing)
        if total_open > 8:
            content += f"\n\n> 注：首页只展示最新的 8 件，本清单是全量总账（共 {total_open} 件在途）。"
        content += "\n\n> 打开平台点【去看】处理；确认/接受后自动从清单消失。"
    title = f"[今日决策] {today.isoformat()} 需要你拍板 {len(sections)} 类事项"
    return {"title": title, "content": content, "sections": len(sections)}


def run_digest(db: Session, push: bool = True) -> dict:
    """生成并发送今日决策清单。push=True 时按 push_settings 总闸推渠道。

    幂等：当日已生成过则跳过——当天失败自愈重试不许重复推送。"""
    from datetime import date as _date

    from app.services.monitor_policy import can_inapp, can_push
    from app.services.notify import notify_inapp

    today_s = _date.today().isoformat()
    exists = (db.query(InAppMessage.id)
              .filter(InAppMessage.msg_type == "digest",
                      InAppMessage.title.like(f"[今日决策] {today_s}%")).first())
    if exists is not None:
        return {"title": "", "content": "", "sections": 0, "skipped": True,
                "pushed_channels": []}
    if not can_inapp(db, "digest", 0):
        return {"title": "", "content": "", "sections": 0, "skipped": True,
                "blocked": "inapp", "pushed_channels": []}
    d = build_digest(db)
    notify_inapp(db, d["title"], d["content"], level="info", msg_type="digest",
                 audience="active", module="digest")
    db.commit()

    pushed = []
    if push and can_push(db, "digest", 0):
        ps = (db.query(PushSetting).filter(PushSetting.scene == "digest").first()
              or db.query(PushSetting).first())   # 无独立配置时跟随日报总闸
        if ps is not None and getattr(ps, "enabled", False):
            from app.models.alert_channel import AlertChannel
            from app.services.notify import NotifyError, send_to_channel
            import json as _json
            try:
                ids = _json.loads(ps.channel_ids or "[]")
            except ValueError:
                ids = []
            for ch in db.query(AlertChannel).filter(AlertChannel.enabled == True).all():  # noqa: E712
                if ids and ch.id not in ids:
                    continue
                try:
                    send_to_channel(ch, d["title"], d["content"])
                    pushed.append(ch.name)
                except NotifyError as e:
                    logger.warning("决策清单推送失败 %s: %s", ch.name, e)
    return {**d, "pushed_channels": pushed}
