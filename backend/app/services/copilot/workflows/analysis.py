"""分析域工作流：成本体检 / 故障诊断 / 容量体检 / 变更影响分析。

取数原则：诊断与容量复用只读工具；成本只消费统一成本事实服务，不独立汇总账单或
生成采购建议。画像漂移等未被工具覆盖的口径使用只读 ORM 查询。
金额口径（硬规则）：pretax_amount=账单金额；payment_amount=挂账现金（常为 0，仅存证）。

产物 artifact = {report_md, highlights, data...}：报告 markdown + 要点数组。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from app.services.copilot.workflows import engine


def _tool(db, name: str, params: dict) -> dict:
    """工具取数统一收口：失败返回 {"error": ...}，不打断工作流（数据缺失宁缺勿报）。"""
    try:
        return engine.run_tool(db, name, params) or {}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {str(e)[:150]}"}


def _fmt_money(v) -> str:
    try:
        return f"¥{float(v):,.2f}"
    except (TypeError, ValueError):
        return "¥-"


# ---------------- ⑤ cost_checkup 成本体检 ----------------

def _cost_window(params):
    """显式 start/end 优先；缺省与页面一致，最近 30 天且截至昨日。"""
    end = date.fromisoformat(params["end"]) if params.get("end") else date.today() - timedelta(days=1)
    start = (date.fromisoformat(params["start"]) if params.get("start")
             else end - timedelta(days=params["days"] - 1))
    return start, end


def _cost_facts(db, params, ctx):
    from app.services.cost_insights import get_cost_insights

    try:
        start, end = _cost_window(params)
        facts = get_cost_insights(db, params.get("account_id") or 0, start, end)
    except (ValueError, LookupError) as exc:
        return {"status": engine.STEP_FAILED, "detail": f"成本事实读取失败：{exc}", "data": {}}
    requested = facts["requested"]
    return {
        "status": engine.STEP_OK,
        "detail": (f"已读取 C1 成本事实：账号 {requested['account_id'] or '全部'}，"
                   f"{requested['start']} 至 {requested['end']}；"
                   f"{len(facts['accounts'])} 个账号，{len(facts['unattributed'])} 组未归属账单"),
        "data": facts,
    }


def _cost_items(ctx):
    facts = ctx["data"].get("成本事实") or {}
    return facts, [*(facts.get("accounts") or []), *(facts.get("unattributed") or [])]


def _object_label(obj):
    if not obj:
        return ""
    return " / ".join(str(x) for x in (
        obj.get("product_name") or obj.get("product_code"),
        obj.get("instance_name") or obj.get("instance_id"), obj.get("billing_item"),
    ) if x)


def _cost_answers(item):
    """三个答案只投影统一成本事实服务字段；页面和工作流不各自汇总账单。"""
    totals = item.get("totals") or {}
    products = item.get("products") or []
    top = products[0] if products else None
    total_text = _fmt_money(totals.get("total")) if totals.get("total") is not None else "—"
    if top:
        spend_text = (f"日成本 {total_text}；主要为 {_object_label(top['object'])} "
                      f"{_fmt_money(top.get('total'))}（{top.get('share_pct') or 0}%）")
    elif totals.get("total") == 0 and (item.get("coverage") or {}).get("complete"):
        spend_text = f"日成本 {total_text}；完整采集与范围证据支持真实零费用"
    elif totals.get("total") is not None:
        spend_text = f"日成本 {total_text}；没有产品排行证据，暂不下钻"
    else:
        spend_text = "日成本 —；暂无可核验金额，未按零费用处理"

    anomalies = item.get("anomalies") or []
    observations = item.get("observations") or []
    analysis = item.get("analysis") or {}
    if anomalies:
        first = anomalies[0]
        anomaly_text = (f"{_object_label(first.get('object'))} 当日 {_fmt_money(first.get('value'))}，"
                        f"基线 {_fmt_money(first.get('baseline'))}；共 {len(anomalies)} 项突增")
    elif analysis.get("anomaly_status") == "no_anomaly_in_verified_scope":
        anomaly_text = "已核验范围内没有发现费用突增"
    else:
        anomaly_text = "账单或历史覆盖不足，不能宣称没有异常"
    if observations:
        anomaly_text += f"；另有 {len(observations)} 项首次观察费用待核实"

    recommendations = item.get("recommendations") or []
    if recommendations:
        action_text = "；".join(f"{x['title']}：{x['reason']}" for x in recommendations)
    else:
        action_text = "证据不足或无需额外动作；不生成采购、SKU 或节省金额建议"
    return {
        "account": item.get("account") or {}, "range": item.get("range") or {},
        "status": item.get("status"), "reason_code": item.get("reason_code"),
        "coverage": item.get("coverage") or {},
        "spend": {"total": totals.get("total"), "top_product": top, "text": spend_text},
        "anomaly": {"status": analysis.get("anomaly_status"), "findings": anomalies,
                    "observations": observations, "text": anomaly_text},
        "action": {"recommendations": recommendations, "text": action_text},
    }


def _cost_spend(db, params, ctx):
    _, items = _cost_items(ctx)
    answers = [_cost_answers(x) for x in items]
    return {"status": engine.STEP_OK,
            "detail": "；".join(f"{x['account'].get('name') or x['account'].get('id')}：{x['spend']['text']}"
                                for x in answers) or "没有可分析账号",
            "data": {"answers": answers}}


def _cost_findings(db, params, ctx):
    _, items = _cost_items(ctx)
    answers = [_cost_answers(x) for x in items]
    return {"status": engine.STEP_OK,
            "detail": "；".join(f"{x['account'].get('name') or x['account'].get('id')}：{x['anomaly']['text']}"
                                for x in answers) or "没有可分析账号",
            "data": {"answers": answers}}


def _cost_actions(db, params, ctx):
    _, items = _cost_items(ctx)
    answers = [_cost_answers(x) for x in items]
    return {"status": engine.STEP_OK,
            "detail": "；".join(f"{x['account'].get('name') or x['account'].get('id')}：{x['action']['text']}"
                                for x in answers) or "没有可分析账号",
            "data": {"answers": answers}}


def _cost_artifact(db, params, ctx):
    facts, items = _cost_items(ctx)
    answers = [_cost_answers(x) for x in items]
    requested = facts.get("requested") or {}
    sections = []
    for item, answer in zip(items, answers):
        account = answer["account"]
        name = account.get("name") or f"未归属账单 · 原账号ID {account.get('id')}"
        coverage = answer["coverage"]
        recs = answer["action"]["recommendations"]
        evidence = []
        for finding in [*(item.get("days") or []), *answer["anomaly"]["findings"],
                        *answer["anomaly"]["observations"], *recs]:
            evidence.extend(finding.get("evidence_refs") or [])
        evidence = list(dict.fromkeys(evidence))
        evidence_text = "\n".join(f"- `{ref}`" for ref in evidence) or "- 当前没有足够证据引用"
        sections.append(f"""### {name}（账号ID {account.get('id')}）
- 窗口：{answer['range'].get('start')} 至 {answer['range'].get('end')}；状态：{answer['status']} / {answer['reason_code']}
- 覆盖：{coverage.get('complete_days', 0)}/{coverage.get('requested_days', 0)} 天；缺账单日期：{', '.join(coverage.get('missing_bill_dates') or []) or '无'}；缺摊销日期：{', '.join(coverage.get('missing_amortized_dates') or []) or '无'}
- **钱主要花在哪里**：{answer['spend']['text']}
- **哪里不正常**：{answer['anomaly']['text']}
- **现在先做什么**：{answer['action']['text']}

证据引用：
{evidence_text}
""")
    report = f"""## 成本洞察 · {requested.get('start', '—')} 至 {requested.get('end', '—')}

请求账号：{requested.get('account_id') or '全部'}。以下来自 `GET /api/cost/insights` 的同一份 C1 结果，未重新汇总账单。

{chr(10).join(sections) if sections else '当前没有可分析账号或成本事实读取失败。'}

> 日成本 = 按量 + 摊销；订单和退款另列。无数据不补零；部分覆盖不代表完整总成本或整体健康。建议只读，不包含采购数量、SKU 或预计节省金额。
"""
    highlights = []
    for answer in answers:
        prefix = answer["account"].get("name") or f"账号 {answer['account'].get('id')}"
        highlights.extend(f"{prefix}：{answer[key]['text']}" for key in ("spend", "anomaly", "action"))
    return {"report_md": report, "highlights": highlights, "insights": facts, "answers": answers,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


# ---------------- ⑥ fault_diagnosis 故障诊断 ----------------

_WIN_HOURS = {"1h": 6, "6h": 24, "24h": 48}


def _fault_timeline(db, params, ctx):
    hours = _WIN_HOURS.get(params.get("window", "24h"), 48)
    r = _tool(db, "recent_changes", {"hours": hours, "keyword": params.get("target") or ""})
    if r.get("error"):
        return {"status": engine.STEP_FAILED, "detail": f"变更时间线取数失败：{r['error']}", "data": r}
    items = r.get("items") or []
    detail = f"变更时间线（近 {hours}h）：{len(items)} 条"
    if items:
        detail += "；最近：" + "；".join(
            f"{i.get('time')} {i.get('service', '')} {i.get('event', '')}（{str(i.get('summary', ''))[:30]}）"
            for i in items[:3])
    else:
        detail += "——窗口内'没动过'本身是证据：排除变更致因"
    return {"status": engine.STEP_OK, "detail": detail,
            "data": {"items": items, "hours": hours}}


def _fault_link(db, params, ctx):
    target = (params.get("target") or "").strip()
    if not target:
        return {"status": engine.STEP_SKIPPED,
                "detail": "未提供目标（域名/实例/应用）——仅出变更时间线；带 target 重跑可出链路取证"}
    find = _tool(db, "find_domain", {"keyword": target})
    host = ""
    for i in (find.get("items") or []):
        host = i.get("host") or ""
        if host:
            break
    chain = _tool(db, "object_chain", {"object": host or target, "days": 3})
    n5xx = sum(int(i.get("cnt_5xx") or 0) for i in (find.get("items") or []))
    detail = f"链路取证（{host or target}）："
    if find.get("error") or chain.get("error"):
        detail += f"取数异常 {find.get('error') or chain.get('error')}"
    elif find.get("items"):
        detail += f"链路库命中 {len(find['items'])} 个域名，近 24h 5xx 合计 {n5xx} 条；已取多层同窗读数（CDN/SLB/应用/主机/DB/变更/钱）"
    else:
        detail += f"链路库无「{target}」匹配（可能不在 SLB 链路/拼写不同），已按原始目标取对象因果链"
    return {"status": engine.STEP_OK, "detail": detail,
            "data": {"find": find, "chain": chain, "host": host, "n5xx": n5xx}}


def _fault_align(db, params, ctx):
    """变更对齐：变更时间线 × 告警触发（±2h 时间窗）。"""
    alerts = _tool(db, "query_alert_history", {"days": 1, "limit": 20})
    changes = (ctx["data"].get("时间线") or {}).get("items") or []
    now_year = datetime.now().year

    def _parse(t: str):
        try:
            return datetime.strptime(f"{now_year}-{t}", "%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            return None

    alert_times = []
    for a in (alerts.get("items") or []):
        t = _parse(a.get("fired_at", ""))
        if t:
            alert_times.append((t, a))
    pairs, unmatched = [], []
    for a_t, a in alert_times:
        hit = None
        for c in changes:
            c_t = _parse(c.get("time", ""))
            if c_t and abs((a_t - c_t).total_seconds()) <= 2 * 3600:
                hit = c
                break
        if hit:
            pairs.append({"alert": a.get("title", "")[:60], "alert_time": a.get("fired_at", ""),
                          "change": f"{hit.get('time')} {hit.get('event', '')}（{str(hit.get('summary', ''))[:40]}）"})
        else:
            unmatched.append({"alert": a.get("title", "")[:60], "alert_time": a.get("fired_at", "")})
    detail = (f"变更对齐（±2h）：告警 {len(alert_times)} 条，与变更同窗 {len(pairs)} 条"
              f"（{pairs[0]['alert']} ↔ {pairs[0]['change']}）" if pairs else
              f"变更对齐（±2h）：告警 {len(alert_times)} 条，无一与变更同窗（变更致因可能性下降）")
    if alerts.get("error"):
        detail = f"变更对齐：告警取数失败 {alerts['error']}（仅保留变更时间线）"
    return {"status": engine.STEP_OK, "detail": detail[:500],
            "data": {"pairs": pairs, "unmatched": unmatched, "alert_count": len(alert_times)}}


def _fault_hypotheses(db, params, ctx):
    """根因假设（确定性启发式，不走 LLM）：按证据给假设+置信度，证据不足如实说。"""
    changes = (ctx["data"].get("时间线") or {}).get("items") or []
    link = ctx["data"].get("链路取证") or {}
    align = ctx["data"].get("变更对齐") or {}
    hyps: list = []

    if align.get("pairs"):
        c0 = align["pairs"][0]
        hyps.append({
            "hypothesis": "近期变更引发（变更与告警同窗）",
            "confidence": "高",
            "evidence": [f"告警「{c0.get('alert')}」与变更「{c0.get('change')}」时间差 <2h"],
        })
    elif changes:
        hyps.append({
            "hypothesis": "窗口内有变更但未与告警同窗——变更致因待排除",
            "confidence": "低",
            "evidence": [f"{len(changes)} 条变更（最近 {changes[0].get('time')} {changes[0].get('event', '')}）"],
        })

    n5xx = link.get("n5xx") or 0
    if n5xx > 0:
        hyps.append({
            "hypothesis": "接入层/后端异常（SLB 5xx）",
            "confidence": "高" if n5xx >= 50 else "中",
            "evidence": [f"链路库近 24h 5xx 合计 {n5xx} 条（{link.get('host') or params.get('target')}）"],
        })

    chain = link.get("chain") or {}
    if not chain.get("error") and chain.get("items"):
        hyps.append({
            "hypothesis": "跨层同窗读数已取（CDN/SLB/应用/主机/DB/变更/钱）——按异常层下钻",
            "confidence": "中",
            "evidence": ["object_chain 多层读数见产物 data（人工按异常层展开）"],
        })

    if not hyps:
        hyps.append({
            "hypothesis": "证据不足，无法定根因",
            "confidence": "低",
            "evidence": ["窗口内无变更、无告警同窗、无链路 5xx——建议带具体域名/实例与事发时刻重跑"],
        })
    detail = "根因假设：" + "；".join(f"{h['hypothesis']}（置信{h['confidence']}）" for h in hyps)
    return {"status": engine.STEP_OK, "detail": detail[:500], "data": {"hypotheses": hyps}}


def _fault_actions(db, params, ctx):
    """处置清单：假设 -> 动作（页面指引，只读建议，不执行任何变更）。"""
    hyps = (ctx["data"].get("根因假设") or {}).get("hypotheses") or []
    actions: list = []
    for h in hyps:
        t = h["hypothesis"]
        if "变更" in t and h["confidence"] in ("高", "中"):
            actions.append({"action": "评估回滚/灰度确认最近变更（变更页核对变更清单与时间）",
                            "reason": t, "page": "变更"})
        if "5xx" in t or "接入层" in t:
            actions.append({"action": "按域名下钻后端健康与 5xx 明细（backend_health / slb_5xx_detail），定位异常后端或 URI",
                            "reason": t, "page": "诊断 / 链路"})
        if "跨层" in t:
            actions.append({"action": "看 object_chain 各层同窗读数，对异常层下钻（主机看 host_metrics+process_top，DB 看 db_health）",
                            "reason": t, "page": "诊断"})
        if "证据不足" in t:
            actions.append({"action": "补充事发时刻与具体对象（域名/实例）重跑故障诊断，或从告警详情页深链进来",
                            "reason": t, "page": "智能体"})
    actions.append({"action": "全程只读取证；任何修复动作（重启/扩容/回滚）由人到对应页面确认执行",
                    "reason": "智能体红线：不直接执行变更", "page": "-"})
    detail = "处置清单 " + str(len(actions)) + " 条：" + "；".join(a["action"][:40] for a in actions[:3])
    return {"status": engine.STEP_OK, "detail": detail[:500], "data": {"actions": actions}}


def _fault_artifact(db, params, ctx):
    changes = (ctx["data"].get("时间线") or {}).get("items") or []
    link = ctx["data"].get("链路取证") or {}
    align = ctx["data"].get("变更对齐") or {}
    hyps = (ctx["data"].get("根因假设") or {}).get("hypotheses") or []
    actions = (ctx["data"].get("处置清单") or {}).get("actions") or []
    target = params.get("target") or "（未给目标）"

    change_rows = "\n".join(
        f"| {c.get('time', '')} | {c.get('service', '')} | {c.get('event', '')} | {str(c.get('summary', ''))[:40]} |"
        for c in changes[:10]) or "| （窗口内无变更） | - | - | - |"
    pair_rows = "\n".join(
        f"| {p.get('alert_time', '')} | {p.get('alert', '')} | {p.get('change', '')} |"
        for p in (align.get("pairs") or [])[:10]) or "| （无同窗对齐） | - | - |"
    hyp_rows = "\n".join(
        f"| {h['hypothesis']} | {h['confidence']} | {'；'.join(h['evidence'])} |" for h in hyps)
    act_rows = "\n".join(f"{i + 1}. {a['action']}（依据：{a['reason'][:40]}）" for i, a in enumerate(actions))

    report = f"""## 故障诊断报告 · {target}

### 一、变更时间线
| 时间 | 服务 | 事件 | 摘要 |
|---|---|---|---|
{change_rows}

### 二、链路取证
- 对象：{link.get('host') or target}；近 24h 5xx 合计 {link.get('n5xx') or 0} 条
- 多层同窗读数（CDN/SLB/应用/主机/DB/变更/钱）：{"已取（见 data）" if link.get('chain') and not link['chain'].get('error') else "未取到——" + str((link.get('chain') or {}).get('error') or link.get('find', {}).get('hint') or "")}

### 三、变更对齐（±2h）
| 告警时间 | 告警 | 同窗变更 |
|---|---|---|
{pair_rows}
- 无同窗对齐的告警 {len(align.get('unmatched') or [])} 条（变更致因可能性下降）

### 四、根因假设
| 假设 | 置信度 | 证据 |
|---|---|---|
{hyp_rows}

### 五、处置清单
{act_rows}

> 取证全程只读；处置动作需人工到对应页面确认执行。时间线口径：云上操作审计（应用发版等线下变更需人工补充）。
"""
    highlights = [f"{h['hypothesis']}（置信{h['confidence']}）" for h in hyps[:3]]
    highlights.append(f"处置清单 {len(actions)} 条（只读取证，不代执行）")
    return {"report_md": report, "highlights": highlights,
            "hypotheses": hyps, "actions": actions,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


# ---------------- ⑦ capacity_checkup 容量体检 ----------------

def _cap_watermark(db, params, ctx):
    """水位：磁盘/内存使用率超阈实例（VM 指标，query_metric 白名单模板）。"""
    disk = _tool(db, "query_metric", {"metric": "disk", "product": "ecs", "op": ">=",
                                      "threshold": 80, "window": "7d", "limit": 10,
                                      "account_id": params.get("account_id")})
    mem = _tool(db, "query_metric", {"metric": "memory", "product": "ecs", "op": ">=",
                                     "threshold": 85, "window": "24h", "limit": 10,
                                     "account_id": params.get("account_id")})
    errs = [r["error"] for r in (disk, mem) if r.get("error")]
    if len(errs) == 2:
        return {"status": engine.STEP_FAILED,
                "detail": f"水位取数失败（指标源不可用）：{errs[0]}", "data": {"disk": disk, "memory": mem}}
    n_disk = len(disk.get("items") or [])
    n_mem = len(mem.get("items") or [])
    detail = f"水位：磁盘 ≥80% 的 ECS {n_disk} 台，内存 ≥85% 的 ECS {n_mem} 台"
    if errs:
        detail += f"（部分取数失败：{errs[0][:80]}）"
    if not n_disk and not n_mem and not errs:
        detail += "——暂无高水位实例（或指标样本不足，宁缺勿报）"
    return {"status": engine.STEP_OK, "detail": detail, "data": {"disk": disk, "memory": mem}}


def _cap_packs(db, params, ctx):
    r = _tool(db, "pack_pools", {"account_id": params.get("account_id")})
    if r.get("error"):
        return {"status": engine.STEP_FAILED, "detail": f"包余量取数失败：{r['error']}", "data": r}
    items = r.get("items") or []
    low = sorted((i for i in items if (i.get("remaining_pct") or 100) <= 30),
                 key=lambda i: i.get("remaining_pct") or 0)
    detail = f"包余量：{len(items)} 个池"
    if low:
        detail += "；低余量：" + "、".join(
            f"{i.get('type_label') or i.get('commodity_code')} 余 {i.get('remaining_pct')}%" for i in low[:5])
    else:
        detail += "；暂无 ≤30% 低余量池" if items else "（尚无资源包快照）"
    return {"status": engine.STEP_OK, "detail": detail, "data": r}


def _cap_expiry(db, params, ctx):
    r = _tool(db, "expiry_list", {})
    if r.get("error"):
        return {"status": engine.STEP_FAILED, "detail": f"到期取数失败：{r['error']}", "data": r}
    items = r.get("items") or []
    crit = [i for i in items if (i.get("days_left") is not None and i["days_left"] <= 7)]
    warn = [i for i in items if i.get("days_left") is not None and 7 < i["days_left"] <= 30]
    detail = f"到期：90 天内 {len(items)} 项（7 天内 {len(crit)}、30 天内 {len(warn)}）"
    if crit:
        detail += "；最紧：" + "、".join(f"{i.get('name', '')[:20]}({i['days_left']}天)" for i in crit[:3])
    return {"status": engine.STEP_OK, "detail": detail, "data": r}


def _cap_disk_growth(db, params, ctx):
    """磁盘增速：RDS 空间净增长（data 日增量中位数口径，单日尖峰不作数）。"""
    rds = _tool(db, "rds_list", {"account_id": params.get("account_id")})
    if rds.get("error"):
        return {"status": engine.STEP_FAILED, "detail": f"RDS 清单取数失败：{rds['error']}", "data": r}
    insts = (rds.get("items") or [])[:3]
    if not insts:
        return {"status": engine.STEP_OK,
                "detail": "磁盘增速：本地无 RDS 资产（或未接入 DBA 数据面），跳过", "data": {"instances": []}}
    results = []
    for it in insts:
        ident = it.get("instance_id") or it.get("name") or ""
        t = _tool(db, "dba_space_trend", {"instance_id": ident})
        if t.get("error") or not (t.get("items") or []):
            results.append({"instance": ident, "note": t.get("error") or t.get("hint") or "无空间快照"})
            continue
        results.append({
            "instance": it.get("name") or ident,
            "net_growth_gb_per_day": t.get("net_growth_gb_per_day"),
            "disk_growth_gb_14d": t.get("disk_growth_gb_14d"),
            "days_to_full": t.get("days_to_full"),
            "note": str(t.get("growth_attribution") or "")[:180],
        })
    fast = [r for r in results if (r.get("net_growth_gb_per_day") or 0) >= 1.0
            or (r.get("days_to_full") is not None and r["days_to_full"] <= 60)]
    detail = f"磁盘增速：{len(results)} 个 RDS 实例（净增口径=data 日增量中位数）"
    if fast:
        detail += "；需关注：" + "、".join(
            f"{r['instance']} 日增 {r.get('net_growth_gb_per_day')}GB"
            + (f"，约 {r['days_to_full']:.0f} 天写满" if r.get("days_to_full") else "")
            for r in fast[:3])
    else:
        detail += "；暂无 ≥1GB/天 或 60 天内写满的实例"
    return {"status": engine.STEP_OK, "detail": detail[:500], "data": {"instances": results}}


def _cap_artifact(db, params, ctx):
    wm = ctx["data"].get("水位") or {}
    packs = (ctx["data"].get("包余量") or {}).get("items") or []
    expiry = (ctx["data"].get("到期") or {}).get("items") or []
    growth = (ctx["data"].get("磁盘增速") or {}).get("instances") or []
    findings: list = []

    for i in ((wm.get("disk") or {}).get("items") or [])[:10]:
        val = i.get("disk_max_7d", i.get("disk_max_24h", ""))
        findings.append({"category": "水位",
                         "item": f"{(i.get('name') or i.get('instance_id'))} 磁盘",
                         "severity": "warning", "detail": f"7d 峰值 {val}%（≥80%）"})
    for i in ((wm.get("memory") or {}).get("items") or [])[:10]:
        val = i.get("memory_max_24h", "")
        findings.append({"category": "水位",
                         "item": f"{(i.get('name') or i.get('instance_id'))} 内存",
                         "severity": "warning", "detail": f"24h 峰值 {val}%（≥85%）"})
    for i in packs:
        if (i.get("remaining_pct") or 100) <= 30:
            findings.append({"category": "包余量",
                             "item": i.get("type_label") or i.get("commodity_code") or "",
                             "severity": "critical" if (i.get("remaining_pct") or 0) <= 10 else "warning",
                             "detail": f"剩余 {i.get('remaining_pct')}%（{i.get('remaining')}）"})
    for i in expiry:
        if i.get("days_left") is None:
            continue
        findings.append({"category": "到期", "item": i.get("name", ""),
                         "severity": "critical" if i["days_left"] <= 7 else "warning",
                         "detail": f"{i.get('category_label', '')} 剩余 {i['days_left']} 天"})
    for r in growth:
        if (r.get("net_growth_gb_per_day") or 0) >= 1.0 or (r.get("days_to_full") is not None and r["days_to_full"] <= 60):
            findings.append({"category": "磁盘增速", "item": r.get("instance", ""),
                             "severity": "warning",
                             "detail": f"净增 {r.get('net_growth_gb_per_day')}GB/天"
                                       + (f"，约 {r['days_to_full']:.0f} 天写满" if r.get("days_to_full") else "")})

    findings.sort(key=lambda f: (0 if f["severity"] == "critical" else 1))
    rows = "\n".join(
        f"| {f['category']} | {f['item']} | {f['severity']} | {f['detail']} |" for f in findings[:20]) \
        or "| - | （本期无容量风险项） | - | - |"
    growth_rows = "\n".join(
        f"| {r.get('instance', '')} | {r.get('net_growth_gb_per_day', '-')} | "
        f"{r.get('disk_growth_gb_14d', '-')} | {r.get('days_to_full') if r.get('days_to_full') is not None else '-'} |"
        for r in growth) or "| （无 RDS 空间快照） | - | - | - |"

    report = f"""## 容量体检清单（生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}）

### 风险清单（{len(findings)} 项，critical 优先）
| 类别 | 对象 | 级别 | 详情 |
|---|---|---|---|
{rows}

### 磁盘增速（RDS，净增=data 日增量中位数）
| 实例 | 净增 GB/天 | 14 天增量 GB | 写满天数 |
|---|---|---|---|
{growth_rows}

> 口径：水位=VM 指标（磁盘 ≥80%/内存 ≥85%）；包余量=池口径（告警统计方式是池不是单包）；
> 到期=90 天内域名/证书/包年资产/资源包（已耗尽包不进账）；磁盘增速看净增长不看单日尖峰（备份/日志轮转摆动≠增长）。
"""
    highlights = [
        f"容量风险 {len(findings)} 项（critical {sum(1 for f in findings if f['severity'] == 'critical')}）",
        f"低余量资源包 {sum(1 for i in packs if (i.get('remaining_pct') or 100) <= 30)} 个",
        f"90 天内到期 {len(expiry)} 项（7 天内 {sum(1 for i in expiry if (i.get('days_left') or 99) <= 7)}）",
        f"RDS 磁盘增速检查 {len(growth)} 实例",
    ]
    return {"report_md": report, "highlights": highlights, "findings": findings,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


# ---------------- ⑧ change_impact 变更影响分析 ----------------

def _ci_changes(db, params, ctx):
    hours = params["hours"]
    r = _tool(db, "recent_changes", {"hours": hours, "keyword": params.get("target") or ""})
    if r.get("error"):
        return {"status": engine.STEP_FAILED, "detail": f"变更取数失败：{r['error']}", "data": r}
    items = r.get("items") or []
    detail = f"最近变更（{hours}h）：{len(items)} 条"
    if items:
        detail += "；最近：" + "；".join(
            f"{i.get('time')} {i.get('event', '')}（{str(i.get('summary', ''))[:30]}）" for i in items[:3])
    return {"status": engine.STEP_OK, "detail": detail, "data": {"items": items}}


def _ci_anomalies(db, params, ctx):
    r = _tool(db, "cost_anomaly_list", {})
    if r.get("error"):
        return {"status": engine.STEP_FAILED, "detail": f"异动取数失败：{r['error']}", "data": r}
    items = r.get("items") or []
    detail = f"异动（近 14 天）：{len(items)} 条"
    if items:
        detail += "；" + "；".join(i.get("title", "")[:35] for i in items[:3])
    return {"status": engine.STEP_OK, "detail": detail, "data": r}


def _ci_drift(db, params, ctx):
    """画像漂移：cost_anomalies 里 type=profile_drift/chain_drift 的漂移事件（只读直查）。"""
    from app.models.cost_anomaly import CostAnomaly

    cutoff = date.today() - timedelta(days=14)
    rows = (db.query(CostAnomaly)
            .filter(CostAnomaly.type.in_(("profile_drift", "chain_drift")),
                    CostAnomaly.last_seen >= cutoff)
            .order_by(CostAnomaly.last_seen.desc()).limit(20).all())
    items = [{
        "type": r.type, "level": r.level, "status": r.ack_status,
        "title": r.title, "target_key": r.target_key,
        "last_seen": r.last_seen.isoformat() if r.last_seen else "",
        "content": (r.content or "")[:200],
    } for r in rows]
    detail = f"画像漂移（近 14 天）：{len(items)} 条"
    if items:
        detail += "；" + "；".join(i["title"][:35] for i in items[:3])
    else:
        detail += "——构成未漂移（画像学习期/未配置画像时为空属正常）"
    return {"status": engine.STEP_OK, "detail": detail, "data": {"items": items}}


def _ci_align(db, params, ctx):
    """时间窗对齐：最近变更 × 异动 × 画像漂移（±24h）。"""
    changes = (ctx["data"].get("最近变更") or {}).get("items") or []
    anomalies = (ctx["data"].get("异动") or {}).get("items") or []
    drifts = (ctx["data"].get("画像漂移") or {}).get("items") or []
    now_year = datetime.now().year

    def _parse_change(t: str):
        try:
            return datetime.strptime(f"{now_year}-{t}", "%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            return None

    def _parse_day(t: str):
        try:
            return datetime.strptime(t[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            return None

    pairs, orphan = [], []
    for ev in anomalies + drifts:
        e_t = _parse_day(str(ev.get("last_seen") or ""))
        if e_t is None:
            orphan.append(ev)
            continue
        hit = None
        for c in changes:
            c_t = _parse_change(c.get("time", ""))
            if c_t and abs((e_t - c_t).total_seconds()) <= 24 * 3600:
                hit = c
                break
        if hit:
            pairs.append({"event": ev.get("title", "")[:60], "event_type": ev.get("type", ""),
                          "event_day": ev.get("last_seen", ""),
                          "change": f"{hit.get('time')} {hit.get('event', '')}（{str(hit.get('summary', ''))[:40]}）"})
        else:
            orphan.append(ev)
    detail = (f"时间窗对齐（±24h）：异动/漂移 {len(anomalies) + len(drifts)} 条，"
              f"与变更同窗 {len(pairs)} 条，孤立（无变更对齐）{len(orphan)} 条")
    if pairs:
        detail += "；最紧一对：" + pairs[0]["event"] + " ↔ " + pairs[0]["change"]
    return {"status": engine.STEP_OK, "detail": detail[:500],
            "data": {"pairs": pairs, "orphan_count": len(orphan)}}


def _ci_artifact(db, params, ctx):
    changes = (ctx["data"].get("最近变更") or {}).get("items") or []
    anomalies = (ctx["data"].get("异动") or {}).get("items") or []
    drifts = (ctx["data"].get("画像漂移") or {}).get("items") or []
    align = ctx["data"].get("时间窗对齐") or {}
    hours = params["hours"]

    change_rows = "\n".join(
        f"| {c.get('time', '')} | {c.get('service', '')} | {c.get('event', '')} | {c.get('user', '')} | {str(c.get('summary', ''))[:40]} |"
        for c in changes[:12]) or "| （窗口内无变更） | - | - | - | - |"
    ev_rows = "\n".join(
        f"| {e.get('type', '')} | {e.get('last_seen', '')} | {e.get('title', '')[:50]} | {e.get('status', '')} |"
        for e in (anomalies + drifts)[:12]) or "| （近 14 天无异动/漂移） | - | - | - |"
    pair_rows = "\n".join(
        f"| {p.get('event_day', '')} | {p.get('event', '')} | {p.get('change', '')} |"
        for p in (align.get("pairs") or [])[:12]) or "| （无同窗对齐） | - | - |"

    report = f"""## 变更影响分析（变更窗 {hours}h × 异动/漂移窗 14d，对齐 ±24h）

### 一、最近变更
| 时间 | 服务 | 事件 | 操作人 | 摘要 |
|---|---|---|---|---|
{change_rows}

### 二、异动 × 画像漂移
| 类型 | 日期 | 标题 | 状态 |
|---|---|---|---|
{ev_rows}

### 三、时间窗对齐（±24h）
| 日期 | 异动/漂移 | 同窗变更 |
|---|---|---|
{pair_rows}
- 孤立异动/漂移（无变更对齐）：{align.get('orphan_count', 0)} 条——非变更类致因（业务量/计费/构成自然变化）优先排查

### 结论口径
- 同窗对齐 = 变更与异动/漂移发生在 ±24h 内，**相关不等于因果**，需结合变更内容判断
- 云上变更审计只覆盖云操作；应用发版/配置线下变更需人工补充对照
"""
    highlights = [
        f"变更 {len(changes)} 条（{hours}h）× 异动 {len(anomalies)} 条 × 漂移 {len(drifts)} 条",
        f"±24h 同窗对齐 {len(align.get('pairs') or [])} 对，孤立 {align.get('orphan_count', 0)} 条",
    ]
    if align.get("pairs"):
        highlights.append("最紧一对：" + align["pairs"][0].get("event", "") + " ↔ " + align["pairs"][0].get("change", ""))
    return {"report_md": report, "highlights": highlights, "alignment": align,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


# ---------------- spec 注册表 ----------------

ANALYSIS_WORKFLOWS = {
    "cost_checkup": {
        "name": {"zh": "成本体检", "en": "Cost checkup"},
        "domain": {"zh": "分析域", "en": "Analysis"},
        "description": {
            "zh": "成本洞察：统一 C1 事实 → 钱主要花在哪里 → 哪里不正常 → 现在先做什么，产物=同账号同窗口证据报告",
            "en": "Cost insights: shared C1 facts → main spend → anomalies → next actions, with the same account, window and evidence",
        },
        "params_schema": {
            "days": {"type": "integer", "required": False, "default": 30, "min": 1, "max": 90,
                     "description": "体检窗口天数，默认 30；显式 start/end 优先"},
            "start": {"type": "string", "required": False, "description": "YYYY-MM-DD，与成本页显式窗口一致"},
            "end": {"type": "string", "required": False, "description": "YYYY-MM-DD，截至日期"},
            "account_id": {"type": "integer", "required": False,
                           "description": "云账号ID过滤（缺省=全部账号分账号呈现）"},
        },
        "steps": [
            ("成本事实", _cost_facts),
            ("钱主要花在哪里", _cost_spend),
            ("哪里不正常", _cost_findings),
            ("现在先做什么", _cost_actions),
        ],
        "build_artifact": _cost_artifact,
    },
    "fault_diagnosis": {
        "name": {"zh": "故障诊断", "en": "Fault diagnosis"},
        "domain": {"zh": "分析域", "en": "Analysis"},
        "description": {
            "zh": "故障诊断：变更时间线 → 链路取证 → 变更对齐 → 根因假设 → 处置清单（全程只读取证）",
            "en": "Fault diagnosis: change timeline → link evidence → change correlation → root-cause hypotheses "
                  "→ action list (read-only evidence throughout)",
        },
        "params_schema": {
            "target": {"type": "string", "required": False,
                       "description": "事发对象：域名/实例ID/应用名（不给则只出变更时间线）"},
            "window": {"type": "string", "required": False, "default": "24h",
                       "enum": ["1h", "6h", "24h"], "description": "回看窗口，默认 24h"},
        },
        "steps": [
            ("时间线", _fault_timeline),
            ("链路取证", _fault_link),
            ("变更对齐", _fault_align),
            ("根因假设", _fault_hypotheses),
            ("处置清单", _fault_actions),
        ],
        "build_artifact": _fault_artifact,
    },
    "capacity_checkup": {
        "name": {"zh": "容量体检", "en": "Capacity checkup"},
        "domain": {"zh": "分析域", "en": "Analysis"},
        "description": {
            "zh": "容量体检：水位（磁盘/内存）→ 包余量 → 到期 → 磁盘增速（RDS 净增口径），产物=风险清单",
            "en": "Capacity checkup: utilization (disk/memory) → package headroom → expiry → disk growth "
                  "(RDS net-growth basis). Artifact: risk list",
        },
        "params_schema": {
            "account_id": {"type": "integer", "required": False,
                           "description": "云账号ID过滤（缺省=全部账号聚合）"},
        },
        "steps": [
            ("水位", _cap_watermark),
            ("包余量", _cap_packs),
            ("到期", _cap_expiry),
            ("磁盘增速", _cap_disk_growth),
        ],
        "build_artifact": _cap_artifact,
    },
    "change_impact": {
        "name": {"zh": "变更影响分析", "en": "Change impact analysis"},
        "domain": {"zh": "分析域", "en": "Analysis"},
        "description": {
            "zh": "变更影响分析：最近变更 × 异动 × 画像漂移按 ±24h 时间窗对齐，识别变更致因与孤立异动",
            "en": "Change impact analysis: aligns recent changes × anomalies × profile drift within a ±24h window "
                  "to identify change-caused incidents and orphan anomalies",
        },
        "params_schema": {
            "hours": {"type": "integer", "required": False, "default": 48, "min": 1, "max": 720,
                      "description": "变更回看小时数，默认 48"},
            "target": {"type": "string", "required": False,
                       "description": "按资源/操作/人过滤变更（可选）"},
        },
        "steps": [
            ("最近变更", _ci_changes),
            ("异动", _ci_anomalies),
            ("画像漂移", _ci_drift),
            ("时间窗对齐", _ci_align),
        ],
        "build_artifact": _ci_artifact,
    },
}
