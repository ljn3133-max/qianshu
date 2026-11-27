"""智能体首页建议：把"系统感知"变成"今天先做什么"。

设计目标：回答前先查系统快照，避免脱离事实作答，人只做决策。工作台如果只给一个
空白聊天框，用户仍然要自己想知道问什么——这里把快照事实 + 今日待办 + 待确认变更 +
账号同步状态聚合成可行动卡片，每条都带"一键追问"的 prompt 与直达链接，
Agent.vue 空态直接渲染。

数据源全部只读（不探测、不写库）：build_snapshot / dashboard.summary /
agent_change_tickets。任何一项失败都不影响其余建议（逐段 try）。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models.agent_change import AgentChangeTicket
from app.models.agent_session import AgentSession
from app.services.copilot.snapshot import build_snapshot
from app.services.dashboard import summary as dashboard_summary

_NON_TERMINAL = ("pending", "draft", "confirmed", "executing", "running", "partial")
_SEV_ORDER = {"critical": 0, "warning": 1, "info": 2, "success": 3}
_MAX_ITEMS = 5
_STALE_HOURS = 24


def _age_hours(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return (datetime.now() - datetime.fromisoformat(iso)).total_seconds() / 3600
    except (TypeError, ValueError):
        return None


def _pending_tickets(db: Session, user) -> list:
    q = db.query(AgentChangeTicket).filter(AgentChangeTicket.status.in_(_NON_TERMINAL))
    if getattr(user, "role", "") != "admin":
        q = (q.join(AgentSession, AgentSession.id == AgentChangeTicket.session_id)
             .filter(AgentSession.user_id == user.id))
    try:
        return q.all()
    except Exception:
        return []


def _account_items(snap: dict) -> list:
    """云账号同步异常/未同步/停用——都是"资产底账不可信"的前置信号。"""
    out = []
    for a in (snap.get("cloud_accounts") or {}).get("items") or []:
        if not a.get("vendor"):
            continue
        name = a.get("name") or f"账号 {a.get('id')}"
        status = a.get("status") or "untested"
        age = _age_hours(a.get("last_sync_at"))
        if a.get("enabled") is False:
            out.append({
                "key": f"account_disabled_{a.get('id')}", "sev": "info", "cat": "账号",
                "title": f"云账号「{name}」已停用",
                "detail": "停用后不再自动同步资产与账单；如只是临时排查，记得重新启用。",
                "prompt": f"云账号「{name}」当前是停用状态，帮我确认它停用会影响哪些数据和告警。",
                "link": "/clouds", "link_label": "去云账号", "action": "route",
            })
        elif status == "error":
            out.append({
                "key": f"account_error_{a.get('id')}", "sev": "critical", "cat": "账号",
                "title": f"云账号「{name}」连接异常",
                "detail": "最近一次测试/同步失败，资产与账单可能停更——先看凭证与网络。",
                "prompt": f"云账号「{name}」连接异常，帮我定位最可能的原因和排查步骤。",
                "link": "/clouds", "link_label": "去排查", "action": "route",
            })
        elif age is None:
            out.append({
                "key": f"account_never_{a.get('id')}", "sev": "warning", "cat": "账号",
                "title": f"云账号「{name}」还没有同步过资产",
                "detail": "接入完成但资产底账为空，总览/规则/成本都不会有它的数据。",
                "prompt": f"云账号「{name}」还没有同步过资产，帮我判断是接入未完成还是同步失败，并给出下一步。",
                "link": "/clouds", "link_label": "去同步", "action": "route",
            })
        elif age > _STALE_HOURS:
            out.append({
                "key": f"account_stale_{a.get('id')}", "sev": "warning", "cat": "账号",
                "title": f"云账号「{name}」已 {int(age)} 小时未同步",
                "detail": "资产变动、到期与账单可能不是最新口径。",
                "prompt": f"云账号「{name}」已经 {int(age)} 小时没有同步了，帮我确认最近一次同步为什么没成功。",
                "link": "/clouds", "link_label": "去检查", "action": "route",
            })
    return out


def build(db: Session, user) -> dict:
    """返回 {generated_at, facts, items}。items 最多 5 条，按严重度排序。"""
    try:
        snap = build_snapshot(db)
    except Exception:
        snap = {}
    try:
        dash = dashboard_summary(db)
    except Exception:
        dash = {}
    stats = dash.get("stats") or {}
    tickets = _pending_tickets(db, user)

    items: list[dict] = []

    # 1) 待确认变更——智能体最该被看见的"人要做的事"
    if tickets:
        n = len(tickets)
        items.append({
            "key": "pending_changes",
            "sev": "critical" if any(t.status in ("pending", "draft") for t in tickets) else "warning",
            "cat": "变更",
            "title": f"{n} 项变更等你确认",
            "detail": "变更不会自动执行；确认后才落地，执行后可一键回滚，全程留痕。",
            "prompt": "列出当前待我确认的变更工单，逐项说明改什么、影响面和回滚点。",
            "link": "", "link_label": "去处理", "action": "tickets",
        })

    # 2) 未归属资产——总览/规则/成本都看不见的"黑账"
    unassigned = (snap.get("cloud_accounts") or {}).get("unassigned_total") or 0
    if unassigned:
        items.append({
            "key": "unassigned_assets", "sev": "warning", "cat": "资产",
            "title": f"{unassigned} 项资产未归属任何云账号",
            "detail": "它们不进账号维度的总览、自动规则与成本分析——先确认归属。",
            "prompt": f"有 {unassigned} 项资产未归属任何云账号，帮我梳理它们是什么、该怎么归属。",
            "link": "/ops", "link_label": "去看资产", "action": "route",
        })

    # 3) 账号侧异常
    items.extend(_account_items(snap))

    # 4) 今日总览待办（告警/成本/到期/巡检）——最多 3 条，避免首页变成第二块总览
    for td in (dash.get("todos") or [])[:3]:
        if td.get("sev") not in ("critical", "warning"):
            continue
        title = td.get("title") or ""
        hint = td.get("hint") or td.get("detail") or ""
        items.append({
            "key": "todo_" + str(len(items)) + "_" + str(td.get("id") or title)[:24],
            "sev": td.get("sev") or "warning",
            "cat": td.get("cat") or "待办",
            "title": title,
            "detail": hint,
            "prompt": f"帮我处理这条待办：{title}。{hint} 请先查系统快照，再给处理步骤。",
            "link": td.get("link") or "",
            "link_label": "去处理", "action": "route",
        })

    # 5) 数据未就绪——"绿屏没事"最误导，这里如实说
    for reason in (dash.get("not_ready_reasons") or [])[:1]:
        items.append({
            "key": "data_not_ready", "sev": "warning", "cat": "数据",
            "title": "数据面未就绪",
            "detail": reason,
            "prompt": f"系统提示「{reason}」，帮我定位原因并给出修复步骤。",
            "link": "/settings", "link_label": "去设置", "action": "route",
        })

    # 6) 没有迫切事项时，给"主动经营"的下一步（不是假报问题）
    if len(items) < 3:
        items.append({
            "key": "quick_cost", "sev": "info", "cat": "建议",
            "title": "做一次成本体检",
            "detail": "近 30 天账单构成、突增项与可执行节省建议，全程只读。",
            "prompt": "帮我做一次成本体检：近30天账单构成、花费最高的产品和资源、异常波动，并给出可执行的节省建议。",
            "link": "/cost", "link_label": "看成本", "action": "route",
        })
    if len(items) < 3:
        items.append({
            "key": "quick_rules", "sev": "info", "cat": "建议",
            "title": "检查告警规则覆盖",
            "detail": "看已接入资产是否都有规则覆盖，避免「资产在、告警不在」。",
            "prompt": "帮我检查告警规则覆盖：哪些已接入的产品/账号没有规则，建议补哪些常见规则。",
            "link": "/alerts", "link_label": "看告警", "action": "route",
        })

    # 去重（同类账号只留第一条）+ 排序 + 截断
    seen = set()
    deduped = []
    for it in items:
        k = it["key"]
        if k in seen:
            continue
        seen.add(k)
        deduped.append(it)
    deduped.sort(key=lambda x: _SEV_ORDER.get(x.get("sev"), 9))
    deduped = deduped[:_MAX_ITEMS]

    facts = {
        "cloud_accounts": (snap.get("cloud_accounts") or {}).get("total", 0),
        "assets": (snap.get("assets") or {}).get("total", 0),
        "datasources": (snap.get("datasources") or {}).get("enabled_count", 0),
        "topology_edges": (snap.get("topology") or {}).get("edges", 0),
        "alerts_firing": stats.get("alerts_firing", 0),
        "todos": dash.get("todo_count", 0),
        "pending_changes": len(tickets),
        "unassigned": unassigned,
    }
    return {
        "generated_at": (snap.get("generated_at")
                         or datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        "facts": facts,
        "items": deduped,
    }
