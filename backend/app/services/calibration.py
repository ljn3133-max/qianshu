"""引导式校准服务：周报=平台"我学到的"+人拍板。

内容四类（全部从已有数据推导，零新增采集面）：
  new_host/new_domain  本周新发现的机器/域名（资产/链路 diff）
  baseline             本周学到的常态水位（基线建立/更新）
  unlock               数据面/能力变化播报
  suggest              需要人拍板的建议（业务映射草稿/分级建议）

推送走统一通知出口（站内信必达+渠道表）；测试验证必须 NOTIFY_TEST_MODE=True（红线机制化）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models.calibration import CalibrationItem

logger = logging.getLogger("qianshu.calibration")


def _week_key(d: datetime) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def generate_weekly(db: Session, force: bool = False) -> dict:
    """生成本周校准周报（幂等：本周已生成且非 force 则跳过）。"""
    week = _week_key(datetime.now())
    if not force and db.query(CalibrationItem).filter(CalibrationItem.week == week).first():
        return {"week": week, "skipped": True, "message": "本周已生成"}

    since = datetime.now() - timedelta(days=7)
    items = []

    # 1) 新发现的机器（assets 本周新增）
    from app.models.asset import Asset

    for a in (db.query(Asset).filter(Asset.created_at >= since,
                                     Asset.status != "Deleted").limit(30).all()):
        items.append(("new_host", f"{a.product}:{a.instance_id}",
                      f"新发现 {a.product}「{a.name or a.instance_id}」——请确认它属于哪条业务线、是否核心资源"))

    # 2) 新发现的域名（口径：从未进过校准周报的域名——校准存档即"已知"基线；
    #    link_edges 无时间戳字段，不用 created_at 拍脑袋）
    from app.models.link_edge import LinkEdge

    seen = {r.ref for r in db.query(CalibrationItem)
            .filter(CalibrationItem.item_type == "new_domain").all()}
    hosts = [h for (h,) in db.query(LinkEdge.host).distinct().limit(200).all()
             if h and h not in seen]
    for h in hosts[:20]:
        items.append(("new_domain", h, f"新域名「{h}」出现在访问链路——请确认它属于哪条业务线"))

    # 3) 本周拍板确认的常态水位（AlertBaseline.confirmed_at）
    try:
        from app.models.alert_baseline import AlertBaseline

        for b in (db.query(AlertBaseline).filter(AlertBaseline.confirmed_at >= since)
                  .limit(20).all()):
            items.append(("baseline", f"{b.rule_id}:{b.target}",
                          f"常态水位拍板记录：{b.target_name or b.target}"
                          f"（确认值 {b.baseline_value:g}，新触发线 {b.threshold_override:g}）"))
    except Exception as e:
        logger.info("基线段跳过: %s", str(e)[:60])

    # 4) 本周新建的业务线（Business）
    try:
        from app.models.business import Business

        for b in (db.query(Business).filter(Business.created_at >= since).limit(10).all()):
            items.append(("suggest", b.name, f"新业务线「{b.name}」已登记——请补充它包含的域名/服务器"))
    except Exception as e:
        logger.info("业务线段跳过: %s", str(e)[:60])

    seen_pairs = set()
    for t, ref, content in items:
        if (t, ref) in seen_pairs:
            continue   # 同对象去重（实测 ssl_cert 同名证书重复行）
        seen_pairs.add((t, ref))
        db.add(CalibrationItem(week=week, item_type=t, ref=ref[:190], content=content[:2000]))
    db.commit()

    # 推送周报（统一通知出口；生产落真实渠道，测试验证必须开 NOTIFY_TEST_MODE）
    if items:
        try:
            from app.services.notify import notify_all_channels

            body = f"本周我学到 {len(items)} 件事，请拍板：\n" + "\n".join(
                f"- {c}" for _, _, c in items[:10])
            notify_all_channels(db, f"校准周报 {week}：{len(items)} 项待确认", body,
                                level="info", module="calibration")
        except Exception as e:
            logger.warning("校准周报推送失败: %s", str(e)[:80])

    logger.info("校准周报生成 %s: %d 项", week, len(items))
    return {"week": week, "count": len(items), "items": [c for _, _, c in items]}


def weekly_report(db: Session, week: str = "") -> dict:
    """取周报（默认本周）。"""
    week = week or _week_key(datetime.now())
    rows = (db.query(CalibrationItem).filter(CalibrationItem.week == week)
            .order_by(CalibrationItem.id).all())
    return {
        "week": week,
        "total": len(rows),
        "pending": sum(1 for r in rows if r.status == "pending"),
        "items": [{"id": r.id, "type": r.item_type, "ref": r.ref,
                   "content": r.content, "status": r.status} for r in rows],
    }


def confirm_item(db: Session, item_id: int, user: str, user_id: int = 0) -> dict:
    """身份引用统一：confirmed_by=username 快照，confirmed_by_id=users.id。"""
    row = db.get(CalibrationItem, item_id)
    if row is None:
        return {"ok": False, "message": "条目不存在"}
    row.status = "confirmed"
    row.confirmed_by = user
    row.confirmed_by_id = user_id or 0
    db.commit()
    return {"ok": True}
