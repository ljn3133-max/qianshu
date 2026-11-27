"""巡检日报：把巡检事实清单交给 LLM 汇总成 markdown 日报。

流程：取当日最新 ok 的巡检 run -> 事实按类目拼装 -> LLM 生成日报（purpose=inspection，
全程 llm_call_logs 留痕）-> 按天 upsert inspection_reports -> 通知：
- 站内信对全部活跃用户必达（不受推送开关影响）
- 机器人渠道推送受 push_settings（scene=inspection）开关控制

安全红线：LLM 只读输入、只产文本；事实清单来自规则扫描，无任何写云操作。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date

from sqlalchemy.orm import Session

from app.models.alert_channel import AlertChannel
from app.models.inspection import InspectionItem, InspectionRun
from app.models.inspection_report import InspectionReport, PushSetting
from app.services.llm import LLMError, chat
from app.services.notify import send_to_channel

logger = logging.getLogger("qianshu.report")

_lock = threading.Lock()          # 与手动触发互斥（生成 + 推送不并发）
_last_status: dict = {"running": False, "last_result": None}

_CATEGORY_LABELS = {
    "cost": "成本异动", "health": "资源健康", "slb": "SLB 摘要",
    "logs": "日志摘要", "idle": "闲置识别",
}

_REPORT_SYSTEM = """你是阿里云运维平台的巡检报告撰写者。系统已执行一轮规则巡检，产出事实清单。
请把事实清单汇总成一份中文 markdown 日报，规则：
- 开头一句话总体结论（今天最需要关注的事）。
- 每条关注项必须回答两件事：哪个环节/对象出了问题（点名：哪台机器哪个盘/哪个域名/哪张表）+ 去哪个方向处理
  （控制台哪个面/找谁/买容量/做治理）。"建议关注""建议优化"这类空话禁止出现。
- 之后分节：严重问题（critical）、需要关注（warning）、巡检摘要（其余 info 事实的浓缩）。
- 每条事实保留关键数字与实例名/实例ID（可用短ID），不要编造清单之外的数据。
- 对"闲置识别"类事实给一句明确的优化建议（缩容/降配/释放/确认备用）。
- 篇幅克制：critical/warning 逐条列出，info 类聚合叙述，总长不超过 800 字。
- 全文 markdown，重点加粗；结尾不需要客套话。"""


def get_status() -> dict:
    return dict(_last_status)


def _latest_ok_run(db: Session, day: date = None) -> InspectionRun:
    """当日（缺省今天）最新一次 ok 的巡检 run。"""
    q = db.query(InspectionRun).filter(InspectionRun.status == "ok")
    if day is not None:
        q = q.filter(InspectionRun.run_date == day)
    run = q.order_by(InspectionRun.id.desc()).first()
    if run is None:
        raise ValueError("当日没有已完成的巡检记录，请先执行巡检")
    return run


def _facts_block(db: Session, run: InspectionRun) -> tuple:
    """事实清单 -> (拼接文本, 最高级别, 各类目计数)。"""
    items = db.query(InspectionItem).filter(InspectionItem.run_id == run.id).all()
    level_rank = {"critical": 0, "warning": 1, "info": 2}
    items.sort(key=lambda i: (level_rank.get(i.level, 3), i.id))
    top_level = "info"
    for i in items:
        if i.level == "critical":
            top_level = "critical"
            break
        if i.level == "warning":
            top_level = "warning"

    lines = [f"巡检日期：{run.run_date.isoformat()}（共 {len(items)} 条事实，扫描器异常：{run.error or '无'}）"]
    cur_cat = None
    for i in items:
        if i.category != cur_cat:
            cur_cat = i.category
            lines.append(f"\n## {_CATEGORY_LABELS.get(cur_cat, cur_cat)}")
        detail = (i.detail or "").strip().replace("\n", "；")
        lines.append(f"- [{i.level}] {i.title}" + (f"｜{detail}" if detail else ""))
    return "\n".join(lines), top_level, {c: sum(1 for i in items if i.category == c) for c in set(i.category for i in items)}


def _push_setting(db: Session) -> PushSetting:
    ps = db.query(PushSetting).filter(PushSetting.scene == "inspection").first()
    if ps is None:
        ps = PushSetting(scene="inspection", enabled=False, channel_ids="[]")
        db.add(ps)
        db.commit()
    return ps


def generate_report(db: Session, notify: bool = False, day: date = None,
                    lang: str = "zh") -> dict:
    """生成（或重生成）当日巡检日报。notify=True 时站内信必达 + 按开关推渠道。

    lang：请求界面语言，require_model 引导文案按 lang 出文；
    定时任务/缺省 zh 保持原行为。
    """
    if not _lock.acquire(blocking=False):
        return {"busy": True, "message": "日报生成中，请稍候"}

    _last_status["running"] = True
    try:
        started = time.monotonic()
        run = _latest_ok_run(db, day)
        facts, top_level, _counts = _facts_block(db, run)

        from app.models.ai_setting import AiSetting
        from app.services.ai_scenarios import require_model

        # 场景矩阵解析：inspection 场景绑定的连接优先，未绑定=兜底默认连接；
        # 停用/无模型 → LLMError 引导（None 路径不崩）。require_model 的 dict 已含连接参数，
        # chat() 吃 ORM 行，按 setting_id 取回同一连接（不走 get_active_setting 全局唯一口径）。
        cfg = require_model(db, "inspection", lang=lang)
        setting = db.get(AiSetting, cfg["setting_id"])
        if setting is None:
            raise LLMError("尚未启用任何 LLM 配置，请先在 设置->AI 模型 中添加并启用")

        # 按天 upsert：重生成覆盖当日旧报告
        report = db.query(InspectionReport).filter(
            InspectionReport.report_date == run.run_date).first()
        if report is None:
            report = InspectionReport(report_date=run.run_date)
            db.add(report)
        report.run_id = run.id
        report.status = "generating"
        report.error = ""
        db.commit()

        try:
            answer = chat(
                setting,
                [
                    {"role": "system", "content": _REPORT_SYSTEM},
                    {"role": "user", "content": f"事实清单：\n\n{facts}"},
                ],
                purpose="inspection", db=db, temperature=0.2,
            )
        except Exception as e:  # LLMError / 网络栈 / 意外异常，都要把报告标 error
            report.status = "error"
            report.error = f"{type(e).__name__}: {str(e)[:400]}"
            db.commit()
            raise

        content = answer["content"].strip()
        report.content_md = content
        report.summary = content.split("\n", 1)[0].lstrip("# ").strip()[:250]
        report.level = top_level
        report.provider_label = setting.provider_label
        report.model = setting.model
        report.token_usage = json.dumps(answer.get("usage", {}), ensure_ascii=False)
        report.status = "ok"
        report.pushed_channels = "[]"

        pushed: list = []
        if notify:
            pushed = _notify(db, report, run)
            report.pushed_channels = json.dumps(pushed, ensure_ascii=False)
        db.commit()

        result = {
            "report_id": report.id, "report_date": report.report_date.isoformat(),
            "run_id": run.id, "level": report.level, "summary": report.summary,
            "pushed_channels": pushed, "usage": answer.get("usage", {}),
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        _last_status["last_result"] = result
        logger.info("巡检日报完成 report=%s date=%s pushed=%s", report.id, report.report_date, pushed)
        return result
    finally:
        _last_status["running"] = False
        _lock.release()


def _notify(db: Session, report: InspectionReport, run: InspectionRun) -> list:
    """站内信全员必达 + 按推送开关发机器人。返回已推送渠道名。"""
    from app.services.monitor_policy import can_push
    from app.services.notify import notify_inapp

    title = f"巡检日报 {report.report_date.isoformat()}：{report.summary[:80]}"
    # 站内信正文用完整报告；巡检模块关了站内信就整条不写（数据仍在报告页可查）
    notify_inapp(db, title, report.content_md, level=report.level,
                 msg_type="inspection", ref_id=report.id, audience="active",
                 module="inspection")
    # 机器人渠道（受推送开关控制）
    ps = _push_setting(db)
    pushed: list = []
    if not ps or not ps.enabled or not can_push(db, "inspection", 0):
        return pushed
    try:
        channel_ids = set(json.loads(ps.channel_ids or "[]"))
    except ValueError:
        channel_ids = set()
    if not channel_ids:
        return pushed
    for ch in db.query(AlertChannel).filter(AlertChannel.enabled == True).all():  # noqa: E712
        if ch.id not in channel_ids:
            continue
        try:
            send_to_channel(ch, title, report.content_md)
            pushed.append(ch.name)
        except Exception as e:  # NotifyError 等，单渠道失败不阻断
            logger.warning("巡检日报推送失败 渠道=%s: %s", ch.name, e)
    return pushed
