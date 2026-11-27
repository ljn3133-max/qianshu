"""改进域工作流：拓扑重画 / 告警降噪调参 / 巡检规则定制 / 仪表盘定制。

智能体改进千枢平台内产物时必须遵守以下安全边界：
  - 变更步骤一律走 engine.change_step() → change_tools.build_ticket——**只建待确认工单，
    绝不静默改**；执行由用户在变更确认卡完成（版本快照+一键回滚由变更层兜底）
  - 取数全部只读（tools_read.execute / 只读 ORM）；不执行 LLM/脚本原文
  - 产物 artifact = {report_md, suggestions}：改进说明（改什么/依据/怎么撤销）+ 建议清单
"""
from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import func

from app.services.copilot.workflows import engine


def _tool(db, name: str, params: dict) -> dict:
    """工具取数统一收口：失败返回 {"error": ...}，不打断工作流（数据缺失宁缺勿报）。"""
    try:
        return engine.run_tool(db, name, params) or {}
    except Exception as e:   # noqa: BLE001
        return {"error": f"{type(e).__name__}: {str(e)[:150]}"}


# ---------------- ⑨ topology_refine 拓扑重画 ----------------

_OWNERSHIP_WORDS = ("归属", "属于", "归到", "挂到", "归给")
_IGNORE_WORDS = ("野域名", "忽略", "去掉", "噪点", "不要", "后端不对", "后端错", "不对",
                 "错了", "错的", "重画", "去噪")
_CONFIRM_WORDS = ("确认", "保留", "没问题", "正确", "对的", "是对的")
_HOST_PAT = re.compile(r"[a-zA-Z0-9][\w.-]*\.[a-zA-Z]{2,}(?:\.[a-zA-Z]{2,})?")
_BIZ_PAT = re.compile(r"(?:归属到?|属于|归到|归给|挂到)\s*([^\s，。！？、；：:（）()]+)")


def _parse_target(feedback: str, target: str) -> str:
    target = (target or "").strip()
    if target:
        return target
    m = _HOST_PAT.search(feedback or "")
    return m.group(0) if m else ""


def _topo_parse(db, params, ctx):
    """用户反馈 → 结构化改进动作（归属登记 save_ownership_rule / 边状态 save_edge_status）。

    启发式定性（显式 action 参数优先）：归属类词→ownership；野域名/不对/噪点类→忽略边；
    确认/保留类→确认边。定不了性=不建单（宁缺勿报，产物给补充指引）。
    """
    from app.models.link_edge import LinkEdge
    from app.models.ownership import OwnershipRule

    feedback = params.get("feedback") or ""
    target = _parse_target(feedback, params.get("target") or "")
    action = params.get("action", "auto")
    if action == "auto":
        if any(w in feedback for w in _OWNERSHIP_WORDS):
            action = "ownership"
        elif any(w in feedback for w in _IGNORE_WORDS):
            action = "edge_ignore"
        elif any(w in feedback for w in _CONFIRM_WORDS):
            action = "edge_confirm"
        else:
            action = ""

    business = (params.get("business") or "").strip()
    if not business:
        m = _BIZ_PAT.search(feedback)
        business = m.group(1) if m else ""

    # 现状核对（只读）：目标的链路边与归属登记
    edges = []
    if target:
        edges = (db.query(LinkEdge)
                 .filter(LinkEdge.host.ilike(f"%{target}%"))
                 .order_by(LinkEdge.req_count.desc()).limit(20).all())
    ownership_now = []
    if target:
        from app.services.ownership import norm_key
        value = norm_key(params.get("key_type", "domain"), target)
        ownership_now = [{"key_type": r.key_type, "key_value": r.key_value,
                          "business": r.business or "", "vendor": r.vendor or ""}
                         for r in (db.query(OwnershipRule)
                                   .filter(OwnershipRule.key_value == value)
                                   .limit(5).all())]

    actions: list = []
    if not target:
        note = "反馈里没识别出目标对象（域名/实例）——不建单；请补充对象后重跑（如：野域名 www.a.com 忽略）"
    elif action == "ownership":
        actions.append({
            "tool": "save_ownership_rule",
            "params": {"key_type": params.get("key_type", "domain"), "key_value": target,
                       "business": business, "note": f"拓扑重画反馈：{feedback[:80]}"}})
        note = f"解析为归属登记：{target} → {business or '（未给业务名，登记备注为主）'}"
    elif action in ("edge_ignore", "edge_confirm"):
        ids = []
        raw_ids = (params.get("edge_ids") or "").strip()
        if raw_ids:
            ids = [int(x) for x in re.split(r"[,，\s]+", raw_ids) if x.strip().isdigit()]
        else:
            ids = [e.id for e in edges]
        if ids:
            actions.append({
                "tool": "save_edge_status",
                "params": {"ids": ids,
                           "status": "ignored" if action == "edge_ignore" else "confirmed",
                           "note": f"拓扑重画反馈：{feedback[:100]}"}})
            note = (f"解析为边状态变更：{target} 共 {len(ids)} 条边 → "
                    f"{'忽略（去噪点）' if action == 'edge_ignore' else '确认保留'}")
        else:
            note = (f"目标「{target}」在链路边库里无匹配边（可能不在 SLB 链路/拼写不同）——"
                    f"不建单；可在拓扑页核对后带 edge_ids 重跑")
    else:
        note = ("反馈未定性出改进动作（归属登记/边忽略/边确认）——不建单不改数；"
                "请补充动作关键词：归属到 X / 野域名忽略 / 确认保留，或显式给 action 参数")

    return {"status": engine.STEP_OK, "detail": note[:500],
            "data": {"actions": actions, "target": target, "action": action,
                     "business": business,
                     "edges": [{"id": e.id, "host": e.host, "upstream_addr": e.upstream_addr,
                                "ecs_name": e.ecs_name or "", "status": e.status}
                               for e in edges[:10]],
                     "ownership_now": ownership_now}}


def _topo_ticket(db, params, ctx):
    actions = (ctx["data"].get("解析反馈") or {}).get("actions") or []
    if not actions:
        return {"status": engine.STEP_SKIPPED,
                "detail": "无可执行改进动作——不建变更工单（改进域绝不静默改数，按上一步指引补充信息重跑）"}
    target = (ctx["data"].get("解析反馈") or {}).get("target") or ""
    title = f"拓扑重画：{target or '（多对象）'}"
    return engine.change_step(
        db, ctx, title=title, items=actions,
        ok_detail=f"已生成待确认变更工单（{title}）——只建单不执行，确认后生效且可一键回滚")


def _topo_artifact(db, params, ctx):
    parsed = ctx["data"].get("解析反馈") or {}
    actions = parsed.get("actions") or []
    edges = parsed.get("edges") or []
    tickets = ctx.get("tickets") or []
    tids = "、".join(f"#{t.get('ticket_id')}" for t in tickets) or "（未建单）"
    feedback = params.get("feedback") or ""
    target = parsed.get("target") or "（未识别目标）"

    edge_rows = "\n".join(
        f"| {e['id']} | {e['host']} | {e['upstream_addr']} | {e['ecs_name']} | {e['status']} |"
        for e in edges[:10]) or "| （链路边库无匹配） | - | - | - | - |"
    act_rows = "\n".join(
        f"{i + 1}. **{a['tool']}**：`{a['params']}`" for i, a in enumerate(actions)) \
        or "（未解析出可执行动作）"

    report = f"""## 拓扑重画改进说明 · {target}

| 项 | 内容 |
|---|---|
| 用户反馈 | {feedback[:200]} |
| 解析动作 | {parsed.get('action') or '（未定性）'} |
| 变更工单 | {tids}（**待确认**，未执行） |

### 一、现状核对（链路边）
| 边 ID | 域名 | 后端 | ECS | 状态 |
|---|---|---|---|---|
{edge_rows}

### 二、拟改进动作（确认后才生效）
{act_rows}

### 三、怎么撤销
- 变更确认卡执行前：点「取消」即无事发生
- 执行后：工单页「一键回滚」按版本快照恢复（改前状态已自动落快照）
"""
    suggestions = [
        "本流程只建待确认工单，绝不静默改拓扑——请在变更确认卡逐项核对后确认",
        "改后到 拓扑 页复核：边状态/归属是否符合预期（重新生成草稿不覆盖已确认状态）",
    ]
    if not actions:
        suggestions.append("补充信息重跑：目标对象（域名/实例）+ 动作（归属到 X / 野域名忽略 / 确认保留）")
    highlights = [f"拓扑改进动作 {len(actions)} 项（工单 {tids}，待确认）",
                  f"链路边现状 {len(edges)} 条已核对"]
    return {"report_md": report, "suggestions": suggestions, "highlights": highlights,
            "actions": actions,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


# ---------------- ⑩ alert_tune 告警降噪调参 ----------------

def _alert_data(db, params, ctx):
    days = params["days"]
    r = _tool(db, "query_alert_history", {"days": days, "limit": 50})
    if r.get("error"):
        return {"status": engine.STEP_FAILED, "detail": f"告警取数失败：{r['error']}", "data": r}
    items = r.get("items") or []
    firing = sum(1 for i in items if (i.get("status") or "") == "firing")
    detail = f"近 {days} 天告警 {r.get('total', len(items))} 条（取样 {len(items)}，firing {firing}）"
    return {"status": engine.STEP_OK, "detail": detail,
            "data": {"items": items, "days": days, "total": r.get("total", len(items))}}


def _alert_analyze(db, params, ctx):
    """噪音分析 + 调参建议（确定性启发式，不走 LLM）：
    触发次数高（≥10）的规则建议调阈值/延长持续窗；基线/画像类告警只给建议不调参。"""
    from app.models.alert_rule import AlertRule

    items = (ctx["data"].get("告警取数") or {}).get("items") or []
    by_rule: dict = {}
    for i in items:
        name = i.get("rule_name") or "（未命名规则）"
        ent = by_rule.setdefault(name, {"rule_name": name, "count": 0, "firing": 0,
                                        "recovered": 0, "samples": []})
        ent["count"] += 1
        if (i.get("status") or "") == "firing":
            ent["firing"] += 1
        else:
            ent["recovered"] += 1
        if len(ent["samples"]) < 3:
            ent["samples"].append(f"{i.get('fired_at', '')} {i.get('title', '')[:40]}")
    ranked = sorted(by_rule.values(), key=lambda x: -x["count"])
    if params.get("rule_name"):
        ranked = [r for r in ranked if r["rule_name"] == params["rule_name"]] or ranked[:0]

    proposals: list = []
    suggestions: list = []
    for r in ranked[:5]:
        name = r["rule_name"]
        row = db.query(AlertRule).filter(AlertRule.name == name).first()
        if r["count"] < 10:
            continue   # 触发不密集，不调参（宁缺勿报）
        if row is None:
            suggestions.append(
                f"「{name}」近窗触发 {r['count']} 次——非阈值规则（基线/画像/内置检测产生），"
                f"不走阈值调参；建议走「设为常态/确认知晓」或静默窗口收口")
            continue
        comparator = row.comparator or ">"
        factor = 1.2 if r["count"] >= 20 else 1.1
        if comparator in ("<", "<="):
            new_threshold = round(float(row.threshold) * (2 - factor), 2)   # 下行比较符反向收严
            direction = "下调（<比较符反向收严）"
        else:
            new_threshold = round(float(row.threshold) * factor, 2)
            direction = "上调"
        new_duration = min(int(row.duration or 300) * 2, 3600) if r["count"] >= 20 else int(row.duration or 300)
        import json as _json
        try:
            silences = _json.loads(row.silences or "[]")
        except ValueError:
            silences = []
        item_params = {"name": name, "source": row.source,
                       "threshold": new_threshold, "duration": new_duration}
        if silences:
            item_params["silences"] = silences
        proposals.append({"params": item_params, "reason":
                          f"近窗触发 {r['count']} 次（firing {r['firing']}）——阈值 {row.threshold}→"
                          f"{new_threshold}（{direction}），持续窗 {row.duration}s→{new_duration}s"})
        suggestions.append(
            f"「{name}」触发 {r['count']} 次：阈值 {row.threshold}→{new_threshold}（{direction}），"
            f"持续窗 {row.duration}s→{new_duration}s（确认后生效，可回滚）")

    if not ranked:
        suggestions.append("窗口内无告警触发——暂无降噪需求，无需调参")
    elif not proposals:
        suggestions.append("有告警但均不密集或非阈值规则——未给出阈值调参（宁缺勿报）；"
                           "可指定 rule_name 或延长 days 重跑")
    detail = (f"噪音分析：{len(ranked)} 条规则，调参建议 {len(proposals)} 条"
              + ("；" + "；".join(s[:60] for s in suggestions[:2]) if suggestions else ""))
    return {"status": engine.STEP_OK, "detail": detail[:500],
            "data": {"by_rule": ranked, "proposals": proposals, "suggestions": suggestions}}


def _alert_ticket(db, params, ctx):
    proposals = (ctx["data"].get("调参建议") or {}).get("proposals") or []
    if not proposals:
        return {"status": engine.STEP_SKIPPED,
                "detail": "无阈值调参建议——不建变更工单（基线/画像类告警走「设为常态」，不走阈值规则）"}
    items = [{"tool": "save_alert_rule", "params": p["params"]} for p in proposals[:3]]
    return engine.change_step(
        db, ctx, title=f"告警降噪调参（{len(items)} 条规则）", items=items,
        ok_detail=f"已生成待确认调参工单（{len(items)} 条规则）——只建单不执行，确认后生效且可一键回滚")


def _alert_artifact(db, params, ctx):
    data = ctx["data"].get("调参建议") or {}
    ranked = data.get("by_rule") or []
    proposals = data.get("proposals") or []
    suggestions = data.get("suggestions") or []
    tickets = ctx.get("tickets") or []
    tids = "、".join(f"#{t.get('ticket_id')}" for t in tickets) or "（未建单）"
    days = params["days"]

    rule_rows = "\n".join(
        f"| {r['rule_name'][:40]} | {r['count']} | {r['firing']} | {r['recovered']} |"
        for r in ranked[:10]) or "| （窗口内无告警） | - | - | - |"
    prop_rows = "\n".join(
        f"{i + 1}. `{p['params']['name']}`：{p['reason']}" for i, p in enumerate(proposals)) \
        or "（无阈值调参建议）"

    report = f"""## 告警降噪调参建议（近 {days} 天）

### 一、告警噪音分布（按规则）
| 规则 | 触发次数 | firing | recovered |
|---|---|---|---|
{rule_rows}

### 二、调参建议（阈值/抑制参数，工单 {tids} 待确认）
{prop_rows}

### 三、口径
- 只对**阈值规则**（alert_rules 有登记）给调参；基线/画像/内置检测类走「设为常态/静默」
- 上调阈值/延长持续窗会降低灵敏度——确认前请核对业务容忍度；全部可一键回滚
"""
    highlights = [f"规则 {len(ranked)} 条，调参建议 {len(proposals)} 条（工单 {tids}，待确认）"]
    highlights += suggestions[:2]
    return {"report_md": report, "suggestions": suggestions, "highlights": highlights,
            "proposals": proposals,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


# ---------------- ⑪ inspection_custom 巡检规则定制 ----------------

# 与 change_tools.INSPECTION_RULE_SETTINGS 同源（键=工具参数键，值=settings 属性/口径标签）
_INSPECT_KEYS = {
    "health_warn": ("INSPECTION_HEALTH_WARN", "使用率预警阈值 %"),
    "health_crit": ("INSPECTION_HEALTH_CRIT", "使用率严重阈值 %"),
    "idle_cpu": ("INSPECTION_IDLE_CPU", "闲置 CPU 阈值 %"),
    "idle_qps": ("INSPECTION_IDLE_QPS", "无流量 QPS 阈值"),
    "expiry_days": ("INSPECTION_EXPIRY_DAYS", "到期预警天数"),
}
_INSPECT_PATTERNS = [
    ("health_warn", r"(?:磁盘|内存|cpu|使用率|水位|预警|警告)[^\d]{0,8}(\d+(?:\.\d+)?)\s*%"),
    ("health_crit", r"(?:严重|crit|红色)[^\d]{0,8}(\d+(?:\.\d+)?)\s*%"),
    ("idle_cpu", r"(?:闲置|低负载|空闲)[^\d]{0,8}(\d+(?:\.\d+)?)\s*%"),
    ("idle_qps", r"(?:无流量|qps|没有流量)[^\d]{0,8}(\d+(?:\.\d+)?)"),
    ("expiry_days", r"(?:到期|过期)[^\d]{0,8}(\d+)\s*天"),
]


def _insp_current() -> dict:
    from app.core.config import settings

    return {k: getattr(settings, attr) for k, (attr, _label) in _INSPECT_KEYS.items()}


def _insp_parse(db, params, ctx):
    """需求 → 阈值变更集：显式数值参数优先，否则从需求文本启发式抽取。"""
    requirement = params.get("requirement") or ""
    changes: dict = {}
    for key in _INSPECT_KEYS:
        if key in params:
            changes[key] = params[key]
    for key, pattern in _INSPECT_PATTERNS:
        if key in changes:
            continue
        m = re.search(pattern, requirement, re.I)
        if m:
            v = float(m.group(1))
            changes[key] = int(v) if key == "expiry_days" else v
    current = _insp_current()
    note = (f"解析出 {len(changes)} 项阈值变更："
            + "、".join(f"{k}→{v}" for k, v in changes.items())) if changes else \
        "需求未解析出可调整阈值（health_warn/health_crit/idle_cpu/idle_qps/expiry_days）——将给出当前规则清单与可调项说明"
    return {"status": engine.STEP_OK, "detail": note[:500],
            "data": {"changes": changes, "current": current}}


def _insp_suggest(db, params, ctx):
    parsed = ctx["data"].get("需求解析") or {}
    changes = parsed.get("changes") or {}
    current = parsed.get("current") or {}
    valid: dict = {}
    problems: list = []
    for k, v in changes.items():
        if k in ("health_warn", "health_crit", "idle_cpu") and not (0 < float(v) <= 100):
            problems.append(f"{k}={v} 超出 (0,100]")
        elif k == "expiry_days" and not (1 <= int(v) <= 365):
            problems.append(f"expiry_days={v} 超出 [1,365]")
        else:
            valid[k] = int(v) if k == "expiry_days" else float(v)
    if "health_warn" in valid and "health_crit" in valid \
            and float(valid["health_warn"]) >= float(valid["health_crit"]):
        problems.append(f"health_warn={valid['health_warn']} 必须小于 health_crit={valid['health_crit']}")
        valid.pop("health_warn", None)
        valid.pop("health_crit", None)
    suggestions = [
        f"{_INSPECT_KEYS[k][1]}：{current.get(k)} → {v}（确认后生效，可回滚）"
        for k, v in valid.items()]
    suggestions += [f"不采纳：{p}" for p in problems]
    if not valid:
        suggestions.append("当前巡检规则："
                           + "、".join(f"{_INSPECT_KEYS[k][1]}={current.get(k)}" for k in _INSPECT_KEYS))
    detail = f"建议规则 {len(valid)} 项" + ("；" + "；".join(s[:60] for s in suggestions[:2]) if suggestions else "")
    return {"status": engine.STEP_OK, "detail": detail[:500],
            "data": {"valid": valid, "suggestions": suggestions, "problems": problems}}


def _insp_ticket(db, params, ctx):
    valid = (ctx["data"].get("建议规则") or {}).get("valid") or {}
    if not valid:
        return {"status": engine.STEP_SKIPPED,
                "detail": "无可落库的规则变更——不建变更工单（按建议补充需求或显式阈值参数重跑）"}
    item_params = dict(valid)
    item_params["note"] = (params.get("requirement") or "")[:100]
    return engine.change_step(
        db, ctx, title=f"巡检规则定制（{len(valid)} 项阈值）",
        items=[{"tool": "save_inspection_rule", "params": item_params}],
        ok_detail="已生成待确认巡检规则工单——只建单不执行，确认后生效且可一键回滚")


def _insp_artifact(db, params, ctx):
    parsed = ctx["data"].get("需求解析") or {}
    suggest = ctx["data"].get("建议规则") or {}
    valid = suggest.get("valid") or {}
    current = parsed.get("current") or {}
    suggestions = suggest.get("suggestions") or []
    tickets = ctx.get("tickets") or []
    tids = "、".join(f"#{t.get('ticket_id')}" for t in tickets) or "（未建单）"

    rows = "\n".join(
        f"| {_INSPECT_KEYS[k][1]} | {current.get(k)} | {valid.get(k, '（不改）')} |"
        for k in _INSPECT_KEYS) or "| - | - | - |"

    report = f"""## 巡检规则定制建议（工单 {tids} 待确认）

需求：{params.get('requirement') or '（未给需求文本）'}

| 规则项 | 当前 | 拟改为 |
|---|---|---|
{rows}

> 口径：阈值影响巡检判定（health=VM 24h 峰值使用率；idle=7 天均值 CPU/SLB QPS；
> expiry=包年资产到期预警天数）。只建单不执行；确认后热生效+持久化 .env，可一键回滚。
"""
    highlights = [f"巡检规则变更 {len(valid)} 项（工单 {tids}，待确认）"]
    highlights += suggestions[:2]
    return {"report_md": report, "suggestions": suggestions, "highlights": highlights,
            "changes": valid,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


# ---------------- ⑫ dashboard_custom 仪表盘定制 ----------------

# 与 core/config.py PRODUCT_CARDS 默认值同源（key -> label/icon/color）
_CARD_CATALOG = {
    "ecs": ("ECS 云服务器", "Monitor", "#409eff"),
    "rds": ("RDS 数据库", "Coin", "#67c23a"),
    "redis": ("Redis", "Lock", "#f56c6c"),
    "slb": ("SLB 负载均衡", "Connection", "#e6a23c"),
    "oss": ("OSS 对象存储", "Files", "#9a6fe0"),
    "cdn": ("CDN", "Promotion", "#13c2c2"),
    "waf": ("WAF 防火墙", "Umbrella", "#fa8c16"),
    "ack": ("ACK 容器", "Grid", "#722ed1"),
    "es": ("Elasticsearch", "Search", "#52c1a"),
}
_ROLE_PREFS = [
    (("db", "数据库", "dba", "rds", "redis"), ["rds", "redis", "es", "ecs"]),
    (("网络", "cdn", "slb", "waf", "流量"), ["slb", "cdn", "waf", "ecs"]),
    (("容器", "k8s", "ack", "容器"), ["ack", "es", "ecs"]),
    (("成本", "费用", "账单", "全都要", "全部"), list(_CARD_CATALOG)),
]


def _dash_current(db, params, ctx):
    from app.core.config import settings
    from app.models.asset import Asset

    try:
        rows = json_loads(settings.PRODUCT_CARDS or "[]")
    except ValueError:
        rows = []
    current = [{"key": r[0], "label": r[1]} for r in rows
               if isinstance(r, (list, tuple)) and len(r) >= 4]
    products = [p for (p,) in db.query(func.distinct(Asset.product)).all() if p]
    detail = f"现状：{len(current)} 张产品卡（{'、'.join(c['key'] for c in current[:6])}）；" \
             f"在用资产产品 {len(products)} 类（{'、'.join(products[:6])}）"
    return {"status": engine.STEP_OK, "detail": detail[:500],
            "data": {"current": current, "products": products}}


def json_loads(s):
    import json
    return json.loads(s or "[]")


def _dash_suggest(db, params, ctx):
    data = ctx["data"].get("现状盘点") or {}
    current = data.get("current") or []
    products = data.get("products") or []
    text = f"{params.get('role') or ''} {params.get('preference') or ''}".lower()

    keys: list = []
    raw_cards = (params.get("cards") or "").strip()
    if raw_cards:
        keys = [k.strip() for k in re.split(r"[,，\s]+", raw_cards) if k.strip()]
    else:
        for words, pref in _ROLE_PREFS:
            if any(w in text for w in words):
                keys = list(pref)
                break
        if not keys:
            # 无偏好：在用资产产品优先，再按目录补齐（最多 6 张）
            keys = [k for k in _CARD_CATALOG if k in products]
            keys += [k for k in _CARD_CATALOG if k not in keys]
            keys = keys[:6]
    # 去重保序 + 目录兜底（未知 key 给中性样式）
    seen: set = set()
    suggested = []
    for k in keys:
        if k in seen:
            continue
        seen.add(k)
        label, icon, color = _CARD_CATALOG.get(k, (k.upper(), "Monitor", "#409eff"))
        suggested.append({"key": k, "label": label, "icon": icon, "color": color})

    cur_keys = [c["key"] for c in current]
    same = [c["key"] for c in suggested] == cur_keys
    suggestions = [f"建议保留 {len(suggested)} 张卡：{'、'.join(c['key'] for c in suggested)}"
                   + ("（与现状一致，无需变更）" if same else "（确认后生效，可回滚）")]
    if products:
        suggestions.append("在用资产产品：" + "、".join(products[:8]) + "——卡片组合已优先覆盖")
    detail = f"卡片组合建议 {len(suggested)} 张（{'、'.join(c['key'] for c in suggested[:6])}）" \
             + ("，与现状一致" if same else "，待确认变更")
    return {"status": engine.STEP_OK, "detail": detail[:500],
            "data": {"suggested": suggested, "suggestions": suggestions, "same": same}}


def _dash_ticket(db, params, ctx):
    suggest = ctx["data"].get("组合建议") or {}
    if suggest.get("same"):
        return {"status": engine.STEP_SKIPPED,
                "detail": "建议组合与现状一致——不建变更工单（无需变更）"}
    suggested = suggest.get("suggested") or []
    if not suggested:
        return {"status": engine.STEP_SKIPPED, "detail": "未生成卡片组合建议——不建变更工单"}
    return engine.change_step(
        db, ctx, title=f"仪表盘定制（{len(suggested)} 张产品卡）",
        items=[{"tool": "update_dashboard_cards", "params": {"cards": suggested}}],
        ok_detail="已生成待确认仪表盘卡片工单——只建单不执行，确认后生效且可一键回滚")


def _dash_artifact(db, params, ctx):
    data = ctx["data"].get("现状盘点") or {}
    suggest = ctx["data"].get("组合建议") or {}
    current = data.get("current") or []
    suggested = suggest.get("suggested") or []
    suggestions = suggest.get("suggestions") or []
    tickets = ctx.get("tickets") or []
    tids = "、".join(f"#{t.get('ticket_id')}" for t in tickets) or "（无需变更）"

    cur_rows = "、".join(c["key"] for c in current) or "（空）"
    new_rows = "\n".join(f"| {c['key']} | {c['label']} | {c['icon']} |" for c in suggested) \
        or "| - | - | - |"

    report = f"""## 仪表盘定制建议（工单 {tids}）

| 项 | 内容 |
|---|---|
| 角色/偏好 | {params.get('role') or '（未给）'} / {params.get('preference') or '（未给）'} |
| 当前卡片 | {cur_rows} |

### 建议卡片组合
| key | 显示名 | 图标 |
|---|---|---|
{new_rows}

> 整体替换首页产品卡（含顺序）；只建单不执行，确认后生效，一键回滚恢复原组合。
"""
    highlights = [f"建议 {len(suggested)} 张产品卡（工单 {tids}）"]
    highlights += suggestions[:2]
    return {"report_md": report, "suggestions": suggestions, "highlights": highlights,
            "cards": suggested,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


# ---------------- spec 注册表 ----------------

IMPROVE_WORKFLOWS = {
    "topology_refine": {
        "name": {"zh": "拓扑重画", "en": "Topology redraw"},
        "domain": {"zh": "改进域", "en": "Improvement"},
        "description": {
            "zh": "拓扑重画：用户反馈（后端不对/野域名/归属）→ 解析成归属登记或边状态动作 → "
                  "建待确认工单（绝不静默改），产物=改进说明",
            "en": "Topology redraw: user feedback (wrong backend / rogue domain / ownership) → parsed into an "
                  "ownership registration or edge-status action → raises a pending-confirmation ticket "
                  "(never a silent change). Artifact: improvement notes",
        },
        "params_schema": {
            "feedback": {"type": "string", "required": True, "description": "用户反馈原文"},
            "target": {"type": "string", "required": False,
                       "description": "目标域名/对象（缺省从反馈文本抽取）"},
            "action": {"type": "string", "required": False, "default": "auto",
                       "enum": ["auto", "ownership", "edge_ignore", "edge_confirm"],
                       "description": "动作类型，缺省按反馈文本启发式判断"},
            "business": {"type": "string", "required": False,
                         "description": "归属业务名（action=ownership 用）"},
            "key_type": {"type": "string", "required": False, "default": "domain",
                         "enum": ["domain", "instance", "ip", "name"],
                         "description": "归属键类型"},
            "edge_ids": {"type": "string", "required": False,
                         "description": "边 ID 列表（逗号分隔；缺省按 target 找边）"},
        },
        "steps": [
            ("解析反馈", _topo_parse),
            ("建变更工单", _topo_ticket),
        ],
        "build_artifact": _topo_artifact,
    },
    "alert_tune": {
        "name": {"zh": "告警降噪调参", "en": "Alert noise tuning"},
        "domain": {"zh": "改进域", "en": "Improvement"},
        "description": {
            "zh": "告警降噪调参：拉近 30 天告警/误报数据（只读）→ 噪音分析与调参建议 → "
                  "save_alert_rule 待确认工单（阈值/抑制参数）",
            "en": "Alert noise tuning: pulls the last 30 days of alerts / false positives (read-only) → noise "
                  "analysis and tuning advice → save_alert_rule pending-confirmation ticket (threshold / suppression params)",
        },
        "params_schema": {
            "days": {"type": "integer", "required": False, "default": 30, "min": 7, "max": 90,
                     "description": "告警回看窗口天数，默认 30"},
            "rule_name": {"type": "string", "required": False,
                          "description": "指定规则名（缺省自动挑触发最密集的）"},
        },
        "steps": [
            ("告警取数", _alert_data),
            ("调参建议", _alert_analyze),
            ("建变更工单", _alert_ticket),
        ],
        "build_artifact": _alert_artifact,
    },
    "inspection_custom": {
        "name": {"zh": "巡检规则定制", "en": "Custom inspection rules"},
        "domain": {"zh": "改进域", "en": "Improvement"},
        "description": {
            "zh": "巡检规则定制：需求 → 建议规则（健康/闲置/到期阈值）→ "
                  "save_inspection_rule 待确认工单",
            "en": "Custom inspection rules: requirement → suggested rules (health / idle / expiry thresholds) "
                  "→ save_inspection_rule pending-confirmation ticket",
        },
        "params_schema": {
            "requirement": {"type": "string", "required": True, "description": "巡检需求描述"},
            "health_warn": {"type": "number", "required": False, "description": "使用率预警阈值 %"},
            "health_crit": {"type": "number", "required": False, "description": "使用率严重阈值 %"},
            "idle_cpu": {"type": "number", "required": False, "description": "闲置 CPU 阈值 %"},
            "idle_qps": {"type": "number", "required": False, "description": "无流量 QPS 阈值"},
            "expiry_days": {"type": "integer", "required": False, "description": "到期预警天数"},
        },
        "steps": [
            ("需求解析", _insp_parse),
            ("建议规则", _insp_suggest),
            ("建变更工单", _insp_ticket),
        ],
        "build_artifact": _insp_artifact,
    },
    "dashboard_custom": {
        "name": {"zh": "仪表盘定制", "en": "Dashboard customization"},
        "domain": {"zh": "改进域", "en": "Improvement"},
        "description": {
            "zh": "仪表盘定制：角色/偏好 → 卡片组合建议（按在用资产与偏好排序）→ "
                  "update_dashboard_cards 待确认工单",
            "en": "Dashboard customization: role / preference → suggested card set (sorted by in-use assets "
                  "and preference) → update_dashboard_cards pending-confirmation ticket",
        },
        "params_schema": {
            "role": {"type": "string", "required": False, "description": "角色（dba/网络/容器/成本…）"},
            "preference": {"type": "string", "required": False, "description": "偏好描述"},
            "cards": {"type": "string", "required": False,
                      "description": "显式卡片组合（逗号分隔 key，如 ecs,rds,redis）"},
        },
        "steps": [
            ("现状盘点", _dash_current),
            ("组合建议", _dash_suggest),
            ("建变更工单", _dash_ticket),
        ],
        "build_artifact": _dash_artifact,
    },
}
