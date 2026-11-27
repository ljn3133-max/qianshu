"""工作流例行化：定时体检 + 有异动才推人 + 运行结果持久化拼接。

例行工作流定时运行、产物落库，**有异动才推人**。

三部分：
  1) 调度 run_due(db, now, runner)：小时级检查、到点即跑（分析域内置 + 启用中的自定义；
     产物=report_md 报告），
     运行走 workflows 引擎落 agent_workflow_runs；last_run_at 为幂等锚点（间隔未到不重跑）。
  2) 异动 detect_anomalies(prev_artifact, artifact)：跑完对比 highlights——
     低余量（池余量≤30% 个数>0）/ 异动新增（待处理异动>0）/ 大额变化（花费环比±20%）/
     容量风险与 7 天内到期（同低余量口径，发现即异动）。
     **有异动才发站内信**（InAppMessage，msg_type=inspection，平台站内信不是外部通知渠道），
     无异动静默（不报平安）。
  3) 拼接 workflow_run_messages(db, session_id)：listMessages 把 agent_workflow_runs 合成
     assistant 消息（msg_kind=workflow_run，含 steps/artifact/tickets 引用）——重开会话后
     工作流结果卡仍在。防与真消息重复：真消息（tool_calls_json/content_md）已引用 run_id 的
     运行不再合成；聊天链路工作流 run 的 session_id=0（真消息承载），天然不合成。

红线：全部只读取数+落库+站内信；不执行任何变更、不碰外部通知渠道、不执行 LLM/脚本原文。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger("qianshu.agent_schedule")

# 大额变化阈值：花费环比 ±20%（口径：与上一轮同工作流 highlights 的花费对比）
COST_SWING_RATIO = 0.20


# ---------------- 到点判断（小时级检查，到点即跑） ----------------

def is_due(last_run_at: Optional[datetime], interval_days: int, hour: int,
           now: Optional[datetime] = None) -> bool:
    """到点即跑口径：
      ① now.hour >= hour（执行时刻；小时级检查=当天该时刻起均可触发，重启/漂移会补跑）
      ② 间隔未到不跑（last_run_at + interval_days 防重，幂等）
    """
    now = now or datetime.now()
    if now.hour < int(hour or 0):
        return False
    if last_run_at is not None:
        gap = timedelta(days=max(1, int(interval_days or 1)))
        if now < last_run_at + gap:
            return False
    return True


def next_run_at(last_run_at: Optional[datetime], interval_days: int, hour: int,
                now: Optional[datetime] = None) -> Optional[datetime]:
    """下次执行时间（展示口径）。已到点返回 None（前端显示"待触发"）。
    与 is_due 同口径：小时级检查 → 合格时刻向上取整到整点。"""
    now = now or datetime.now()
    if is_due(last_run_at, interval_days, hour, now):
        return None
    if last_run_at is None:
        return now.replace(hour=int(hour or 0), minute=0, second=0, microsecond=0)
    earliest = last_run_at + timedelta(days=max(1, int(interval_days or 1)))
    eligible = max(earliest, earliest.replace(hour=int(hour or 0), minute=0, second=0, microsecond=0))
    cand = eligible.replace(minute=0, second=0, microsecond=0)
    if cand < eligible:
        cand += timedelta(hours=1)
    return cand


# ---------------- 有异动才推人：对比 highlights ----------------

# 亮点行 -> 信号数字（格式锚定 analysis.py 产物 highlights 文案，行格式稳定）
_RE_LOW_PACKS = re.compile(r"低余量(?:（≤30%）|资源包)\s*(\d+)\s*个")
_RE_ANOMALY = re.compile(r"异动\s*(\d+)\s*条（待处理\s*(\d+)\s*条）")
_RE_CAPACITY = re.compile(r"容量风险\s*(\d+)\s*项（critical\s*(\d+)\s*）")
_RE_EXPIRING = re.compile(r"90 天内到期\s*(\d+)\s*项（7 天内\s*(\d+)\s*）")
_RE_COST = re.compile(r"近\s*\d+\s*天花费\s*¥([\d.,]+)")


def _signals(highlights: list) -> dict:
    """从 highlights 抽取数值信号（缺失=0/None）。"""
    sig = {"low_packs": 0, "open_anomalies": 0, "capacity_risks": 0,
           "critical_risks": 0, "expiring_7d": 0, "cost_amount": None}
    for h in highlights or []:
        s = str(h)
        m = _RE_LOW_PACKS.search(s)
        if m:
            sig["low_packs"] = int(m.group(1))
        m = _RE_ANOMALY.search(s)
        if m:
            sig["open_anomalies"] = int(m.group(2))
        m = _RE_CAPACITY.search(s)
        if m:
            sig["capacity_risks"] = int(m.group(1))
            sig["critical_risks"] = int(m.group(2))
        m = _RE_EXPIRING.search(s)
        if m:
            sig["expiring_7d"] = int(m.group(2))
        m = _RE_COST.search(s)
        if m:
            try:
                sig["cost_amount"] = float(m.group(1).replace(",", ""))
            except ValueError:
                pass
    return sig


def detect_anomalies(prev_artifact: Optional[dict], artifact: dict) -> list:
    """跑完对比 highlights，返回异动清单 [{code,text}]；空列表=无异动（静默）。

    规则（"低余量 / 异动新增 / 大额变化"三类口径）：
      - 低余量/容量风险/7 天内到期：发现即异动（>0 才报，例行未除险前每轮可见——体检不报平安）
      - 异动新增：待处理异动 > 0（cost 异动事件未处理完=持续异动）
      - 大额变化：花费与上一轮环比 ±20%（无上一轮不判涨跌）
    """
    prev = _signals((prev_artifact or {}).get("highlights") or [])
    cur = _signals((artifact or {}).get("highlights") or [])
    out: list = []

    if cur["low_packs"] > 0:
        out.append({"code": "low_packs",
                    "text": f"低余量资源包 {cur['low_packs']} 个（≤30%）"})
    if cur["open_anomalies"] > 0:
        out.append({"code": "anomaly_open",
                    "text": f"待处理异动 {cur['open_anomalies']} 条"})
    if cur["capacity_risks"] > 0 or cur["critical_risks"] > 0:
        out.append({"code": "capacity_risk",
                    "text": f"容量风险 {cur['capacity_risks']} 项（critical {cur['critical_risks']}）"})
    if cur["expiring_7d"] > 0:
        out.append({"code": "expiring_7d",
                    "text": f"7 天内到期 {cur['expiring_7d']} 项"})
    if (prev_artifact is not None and prev["cost_amount"] and cur["cost_amount"] is not None):
        swing = abs(cur["cost_amount"] - prev["cost_amount"]) / prev["cost_amount"]
        if swing >= COST_SWING_RATIO:
            direction = "涨" if cur["cost_amount"] > prev["cost_amount"] else "跌"
            out.append({"code": "cost_swing",
                        "text": f"花费大额变化：环比{direction} {swing * 100:.0f}%"
                                f"（¥{prev['cost_amount']:,.2f} → ¥{cur['cost_amount']:,.2f}）"})
    return out


# ---------------- 站内信（有异动才发；平台站内信，不碰外部渠道） ----------------

def _notify(db: Session, workflow_key: str, run_id: int, artifact: dict,
            anomalies: list) -> int:
    from app.services.notify import notify_inapp

    # name 载体已双语（{"zh","en"}）：站内信为持久化数据、无请求语言上下文——固定取 zh 保持原行为；
    # 自定义工作流回落 workflow_defs.name（workflow_label 统一解析）
    name = workflow_label(db, workflow_key, "zh")
    lines = [f"- {a['text']}" for a in anomalies]
    highlights = (artifact or {}).get("highlights") or []
    if highlights:
        lines += ["", "本轮要点："] + [f"- {h}" for h in highlights]
    lines += ["", f"完整报告见「千枢智能体 → 工作流 → 运行记录」（run #{run_id}）。"]
    title = f"例行体检 · {name}发现异动（{len(anomalies)} 项）"
    level = "info" if all(a["code"] == "cost_swing" for a in anomalies) else "warning"
    # 定时体检属「巡检日报」提醒面（msg_type=inspection），受同一模块开关控制
    n = notify_inapp(db, title, "\n".join(lines), level=level, msg_type="inspection",
                     ref_id=run_id, audience="active", module="inspection")
    db.commit()
    return n


# ---------------- 调度执行（直调函数：main 挂载 / 测试直调，不启服务） ----------------

def _default_runner(db: Session, workflow_key: str, schedule) -> dict:
    """例行执行走工作流引擎（分析域内置 + 启用中的自定义）：产物落 agent_workflow_runs，
    不进任何会话。"""
    from app.services.copilot import workflows as wf_mod

    return wf_mod.run_workflow(db, workflow_key, {}, {
        "user": "scheduler", "user_id": 0, "session_id": 0, "trigger": "schedule",
    })


def _prev_artifact(db: Session, workflow_key: str, exclude_run_id: int) -> Optional[dict]:
    from app.models.agent_change import AgentWorkflowRun

    r = (db.query(AgentWorkflowRun)
         .filter(AgentWorkflowRun.workflow_key == workflow_key,
                 AgentWorkflowRun.id != exclude_run_id)
         .order_by(AgentWorkflowRun.id.desc()).first())
    if r is None:
        return None
    try:
        return json.loads(r.artifact_json or "{}")
    except ValueError:
        return {}


def schedulable_keys(db=None) -> set:
    """可定时的工作流：分析域内置 + 启用中的自定义。

    接入域/改进域/引导域内置不进例行体检；自定义工作流（workflow_defs）enabled=true
    即可定时——调度循环 run_due 照跑（走 workflows 引擎，与手动运行同执行路径）。
    db 缺省只返回内置口径（历史调用兼容）；带 db 时补自定义 key。
    """
    from app.services.copilot import workflows as wf_mod

    # domain 载体已双语：此处是业务判定（中文域值为逻辑口径），固定取 zh 比较——行为不变
    from app.core.i18n_msg import pick as _pick

    keys = {k for k, spec in wf_mod.WORKFLOWS.items() if _pick(spec.get("domain"), "zh") == "分析域"}
    if db is not None:
        from app.models.workflow_def import WorkflowDef

        keys |= {r.key for r in db.query(WorkflowDef)
                 .filter(WorkflowDef.enabled == True).all()}   # noqa: E712
    return keys


def workflow_label(db: Session, workflow_key: str, lang: str = "zh") -> str:
    """工作流显示名：内置注册表优先，自定义回落 workflow_defs.name（缺省回 key）。"""
    from app.core.i18n_msg import pick as _pick
    from app.services.copilot import workflows as wf_mod

    name = _pick((wf_mod.WORKFLOWS.get(workflow_key) or {}).get("name"), lang)
    if name:
        return name
    try:
        from app.models.workflow_def import WorkflowDef

        row = db.query(WorkflowDef).filter(WorkflowDef.key == workflow_key).first()
        return (row.name or workflow_key) if row is not None else workflow_key
    except Exception:   # noqa: BLE001  展示名尽力而为，不阻断调度/拼接
        return workflow_key


def run_due(db: Session, now: Optional[datetime] = None, runner=None) -> dict:
    """到点即跑（分析域内置 + 启用中的自定义）。返回 {checked, ran:[{...}], errors:[...]}。

    - runner 可注入（mock 测试）：runner(db, workflow_key, schedule) -> run 结果 dict
      （缺省 _default_runner 走工作流引擎）
    - 幂等：成功/有结果即置 last_run_at=now（间隔未到不再触发）；执行抛异常不置
      （下个小时级检查自动补跑），错误进 errors 不裸抛
    - 有异动才站内信：跑完对比 highlights（低余量/异动新增/大额变化），无异动静默
    """
    from app.models.agent_schedule import AgentWorkflowSchedule

    now = now or datetime.now()
    rows = (db.query(AgentWorkflowSchedule)
            .filter(AgentWorkflowSchedule.enabled == True)  # noqa: E712
            .order_by(AgentWorkflowSchedule.id.asc()).all())
    allow = schedulable_keys(db)
    ran: list = []
    errors: list = []
    for s in rows:
        if s.workflow_key not in allow:
            continue   # 防御：表被手工塞了不可定时 key（非分析域内置/已停用自定义）
        if not is_due(s.last_run_at, s.interval_days, s.hour, now):
            continue
        try:
            result = (runner or _default_runner)(db, s.workflow_key, s) or {}
        except Exception as e:  # noqa: BLE001  单个失败不阻断其他定时
            logger.exception("例行体检执行失败 schedule=%s key=%s", s.id, s.workflow_key)
            errors.append({"schedule_id": s.id, "workflow_key": s.workflow_key,
                           "error": f"{type(e).__name__}: {str(e)[:150]}"})
            continue   # 不置 last_run_at：下个小时级检查补跑
        run_id = int(result.get("run_id") or 0)
        artifact = result.get("artifact") or {}
        prev = _prev_artifact(db, s.workflow_key, run_id)
        anomalies = detect_anomalies(prev, artifact)
        notified = _notify(db, s.workflow_key, run_id, artifact, anomalies) if anomalies else 0
        s.last_run_at = now
        db.commit()
        ran.append({"schedule_id": s.id, "workflow_key": s.workflow_key,
                    "run_id": run_id, "status": result.get("status", ""),
                    "anomalies": [a["text"] for a in anomalies], "notified": notified})
    return {"checked": len(rows), "ran": ran, "errors": errors}


# ---------------- 运行结果持久化拼接（重开会话后工作流结果卡仍在） ----------------

_RE_RUN_REF_TOOL = re.compile(r'"run_id"\s*:\s*(\d+)')
_RE_RUN_REF_TEXT = re.compile(r"run #(\d+)")


def workflow_run_messages(db: Session, session_id: int) -> list:
    """把会话的 agent_workflow_runs 合成 assistant 消息（msg_kind=workflow_run）。

    口径：
      - 只拼 session_id 匹配的运行（聊天链路工作流 run 的 session_id=0，真消息已承载，不合成）
      - 真消息已引用 run_id 的运行不再合成（tool_calls_json `"run_id": N` / 正文 `run #N`）——防与真消息重复
      - 形状对齐前端工作流结果卡：id=wf-run-{id}、wfName/status/steps/artifact/tickets
      - tickets 引用：运行产物 ticket_ids（run 端点落库）-> 工单对象（ticket_out 口径）
    """
    from app.models.agent_change import AgentChangeTicket, AgentWorkflowRun
    from app.models.agent_session import AgentMessage

    runs = (db.query(AgentWorkflowRun)
            .filter(AgentWorkflowRun.session_id == int(session_id or 0))
            .order_by(AgentWorkflowRun.id.asc()).all())
    if not runs:
        return []
    referenced: set = set()
    for m in (db.query(AgentMessage)
              .filter(AgentMessage.session_id == int(session_id or 0)).all()):
        for pat in (_RE_RUN_REF_TOOL, _RE_RUN_REF_TEXT):
            for rm in pat.finditer((m.tool_calls_json or "") + "\n" + (m.content_md or "")):
                referenced.add(int(rm.group(1)))

    out: list = []
    for r in runs:
        if r.id in referenced:
            continue
        try:
            steps = json.loads(r.steps_json or "[]")
        except ValueError:
            steps = []
        try:
            artifact = json.loads(r.artifact_json or "{}")
        except ValueError:
            artifact = {}
        # 合成消息模板整体为中文（后续批次统一双语化）；name 固定取 zh 与模板一致
        # （自定义工作流回落 workflow_defs.name）
        name = workflow_label(db, r.workflow_key, "zh")
        tickets = _load_tickets(db, artifact)
        highlights = artifact.get("highlights") or []
        content = f"工作流「{name}」运行{'完成' if r.status == 'done' else r.status}（run #{r.id}）"
        if highlights:
            content += "\n" + "\n".join(f"- {h}" for h in highlights)
        out.append({
            "id": f"wf-run-{r.id}",
            "role": "assistant",
            "msg_kind": "workflow_run",
            "content_md": content,
            "tool_calls": [],
            "model": "",
            "tokens": 0,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "run_id": r.id,
            "workflow_key": r.workflow_key,
            "workflow_name": name,
            "status": r.status,
            "steps": steps,
            "artifact": artifact,
            "tickets": tickets,
        })
    return out


def _load_tickets(db: Session, artifact: dict) -> list:
    """产物 ticket_ids -> 工单对象（ticket_out 口径，与变更列表同源）。"""
    from app.models.agent_change import AgentChangeTicket

    ids = [i for i in (artifact or {}).get("ticket_ids") or [] if i]
    if not ids:
        return []
    try:
        from app.services.copilot import change_tools

        out = []
        for t in (db.query(AgentChangeTicket)
                  .filter(AgentChangeTicket.id.in_(ids))
                  .order_by(AgentChangeTicket.id.asc()).all()):
            out.append(change_tools.ticket_out(t))
        return out
    except Exception:  # noqa: BLE001  变更层异常不影响消息拼接
        logger.warning("工作流卡工单引用加载失败", exc_info=True)
        return []
