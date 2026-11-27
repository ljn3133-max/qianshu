"""自然语言查询编排：意图路由 -> 工具执行 -> LLM 汇总回答。

两次 LLM 调用：
1. 路由：问题 + 工具表 -> {tool, params}（严格 JSON，防御式解析）
2. 回答：问题 + 工具结果 -> markdown 回答

全程留痕 ai_query_logs（问题/回答/工具轨迹/token），LLM 调用本身走 services/llm.chat
（llm_call_logs 记 purpose=nl_query）。工具全部只读（见 ai_tools 安全说明）。
"""
from __future__ import annotations

import json
import logging
import re
import time

from sqlalchemy.orm import Session

from app.models.ai_query_log import AiQueryLog
from app.services import ai_tools, diag_tools
from app.services.ai_tools import ToolError
from app.services.llm import LLMError, chat

logger = logging.getLogger("qianshu.ask")

_DATA_MAX_CHARS = 100000    # 工具结果喂给 LLM 的截断长度（deepseek-v4-pro 1M 上下文，放宽）

_ROUTING_SYSTEM = """你是一个运维平台的查询路由器。根据用户问题选择一个工具并抽取参数，只输出 JSON，不要输出其他任何内容。

可用工具：
{tool_desc}

输出格式（严格遵守，无引号包裹，直接 JSON）：
{{"tools": [{{"tool": "工具名", "params": {{...}}}}, ...], "reason": "一句话理由"}}
（tools 数组填 1~3 个工具：一步取证填一个；需要多源对照时组合，如账单答疑=账单拆解+配置实况）

规则：
- 工具分四类：资产/监控/成本/告警（query_*）；链路与主机健康（find_domain/slb_*/backend_health/app_errors）；成本与资源包/CDN 流量（pack_pools/pool_usage_trend/product_cost_trend/instance_cost_top/cost_anomaly_list/expiry_list/cdn_domain_traffic）；数据库（rds_list/slowlog_top/dba_space_trend/db_health）。按问题选最贴切的一个。
- 问题与上述都无关时，tools 填 [{{"tool": "chat", "params": {{"reply_hint": "说明你只能回答资产/监控/成本/告警类问题"}}}}]。
- 账单/计费/扣费类问题（账单答疑）：组合 bill_breakdown（拆趋势与场景）+ bill_config（实例配置实况），
  目标写实例ID/产品名；问题里有实例ID就用它。
- 阈值/百分比/窗口等问题里的数字要抽到 params（如"CPU超80" -> metric=cpu, op=">", threshold=80）。
- 时间窗口默认 24h；"最近一小时" -> window=1h；"最近7天" -> window=7d 或 days=7。
- 不知道的字段不要编造，留空即可。"""

_ANSWER_SYSTEM = """你是一个阿里云运维平台的助手。用户提了一个问题，系统已执行只读查询工具并拿到结果。
请基于结果用中文简洁回答，规则：
- 直接回答问题本身（如列出符合条件的机器/数字/结论），markdown 格式，重点内容加粗。
- 数据较多时用表格或列表呈现，超过 10 行只列 Top 10 并说明还有多少。
- 如果结果为空或与问题不匹配，如实说明，不要编造数据。
- 简短点评值得注意的点（如某实例异常高），但不要展开闲聊。
- **金额口径（硬规则）**：金额结论一律以 pretax_amount（账单金额）为准；
  payment_amount 是挂账现金字段（常为 0），**禁止用它判断花费或推断抵扣**；
  资源包抵扣情况需查资源包流水，不得从金额字段推断；T+1 账单非终态，近 7 天可能补账修订。"""


def _extract_json(text: str) -> dict:
    """从 LLM 回复中防御式提取第一个 JSON 对象（容忍 ```json 包裹/前后缀文本）。"""
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise LLMError(f"路由输出不是 JSON: {text[:200]}")
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        # 常见：尾逗号/单引号，再抢救一次
        fixed = re.sub(r",\s*([}\]])", r"\1", m.group(0))
        try:
            obj = json.loads(fixed)
        except ValueError:
            raise LLMError(f"路由 JSON 解析失败: {text[:200]}")
    if not isinstance(obj, dict):
        raise LLMError("路由输出不是对象")
    return obj


def _all_tools() -> dict:
    """Ask 可用工具 = 自带 4 个 + 诊断全部只读工具。

    共享诊断工具箱以覆盖链路、慢 SQL 和 CDN 等取证场景，避免维护两套只读工具。
    """
    return {**ai_tools.TOOLS, **diag_tools.TOOLS}


def _execute_tool(db: Session, tool: str, params: dict) -> dict:
    if tool in ai_tools.TOOLS:
        return ai_tools.execute_tool(db, tool, params)
    return diag_tools.execute_tool(db, tool, params)


def _tool_desc_block() -> str:
    lines = []
    for name, spec in _all_tools().items():
        params = "; ".join(f"{k}: {v}" for k, v in spec["params"].items())
        lines.append(f"- {name}: {spec['desc']}\n  params: {params}")
    return "\n".join(lines)


def run_ask(db: Session, user_id: int, question: str, lang: str = "zh") -> dict:
    """完整执行一次自然语言查询。返回 {answer, tool_trace, usage}；失败抛 LLMError/ToolError。

    lang：请求界面语言（zh/en）——require_model 引导文案与 LLM 回答语言跟随界面；
    缺省 zh 保持原行为。
    """
    started = time.monotonic()
    from app.models.ai_setting import AiSetting
    from app.services.ai_scenarios import require_model

    # 场景矩阵解析：ask 场景绑定的连接优先，未绑定=兜底默认连接；
    # 停用/无模型 → LLMError 引导（None 路径不崩）。require_model 的 dict 已含连接参数，
    # chat() 吃 ORM 行，按 setting_id 取回同一连接（不走 get_active_setting 全局唯一口径）。
    cfg = require_model(db, "ask", lang=lang)
    setting = db.get(AiSetting, cfg["setting_id"])
    if setting is None:
        raise LLMError("尚未启用任何 LLM 配置，请先在 设置->AI 模型 中添加并启用")

    usage_total: dict = {"routing": {}, "answer": {}}
    tool_trace: list = []

    # 1) 路由：空回复/坏 JSON 需重试，LLM 偶发空内容时不应因一次解析失败就返回 502
    route = None
    for attempt in (1, 2):
        routing = chat(
            setting,
            [
                {"role": "system", "content": _ROUTING_SYSTEM.format(tool_desc=_tool_desc_block())},
                {"role": "user", "content": question},
            ],
            purpose="nl_query",
            db=db,
            max_tokens=500,
            temperature=0,
            lang=lang,
        )
        usage_total["routing"] = routing["usage"]
        try:
            route = _extract_json(routing["content"])
            break
        except LLMError:
            logger.warning("Ask 路由输出解析失败（第 %d 轮）: %r", attempt, (routing.get("content") or "")[:60])
    if route is None:
        # 确定性关键词兜底（LLM 空回复不再 502；账单类照样走组合工具）
        q = question.lower()
        if any(k in question for k in ("账单", "扣费", "扣了", "计费", "收费", "费用", "花了", "消耗")):
            ids = re.findall(r"daspost-cn-[0-9a-z]+|[a-z]{2,5}-bp[0-9a-z]+", question, flags=re.I)
            target = ";".join(dict.fromkeys(ids)) if ids else question
            route = {"tools": [
                {"tool": "bill_breakdown", "params": {"target": target, "days": 30}},
                {"tool": "bill_config", "params": {"instance": target}},
            ], "reason": "LLM 路由失败，关键词兜底：账单答疑"}
        else:
            route = {"tools": [{"tool": "chat", "params": {}}],
                     "reason": "LLM 路由失败，关键词无命中"}
    # 兼容单工具旧格式 {"tool":...} 与新格式 {"tools":[...]}
    steps = route.get("tools")
    if not isinstance(steps, list) or not steps:
        steps = [{"tool": route.get("tool"), "params": route.get("params") or {}}]
    reason = str(route.get("reason") or "")

    # 2) 逐个执行工具（chat 跳过）；问题类配方注入回答阶段（账单答疑等按公式给结论）
    data_parts: list = []
    for st in steps[:3]:
        tool = str(st.get("tool") or "").strip()
        params = st.get("params") or {}
        if not tool or tool == "chat":
            continue
        t0 = time.monotonic()
        try:
            result = _execute_tool(db, tool, params)
            err = ""
        except (ToolError, diag_tools.ToolError) as e:
            result, err = None, str(e)
        except Exception as e:  # VM 不可达等
            logger.exception("工具执行失败 %s", tool)
            result, err = None, f"{type(e).__name__}: {str(e)[:200]}"
        duration_ms = int((time.monotonic() - t0) * 1000)
        tool_trace.append({
            "tool": tool, "params": params, "reason": reason,
            "rows": len(result.get("items", [])) if result else 0,
            "total": result.get("total") if result else 0,
            "duration_ms": duration_ms,
            "error": err,
        })
        if result is None:
            data_parts.append(f"【{tool}】执行失败：{err}")
        else:
            s = json.dumps(result, ensure_ascii=False)
            data_parts.append(f"【{tool}】\n" + (s[:_DATA_MAX_CHARS] + f'...（已截断，共{len(s)}字符）'
                                                 if len(s) > _DATA_MAX_CHARS else s))
    data_str = "\n\n".join(data_parts)

    from app.services.playbooks import match_playbook, recipe_text
    _pb_key, _pb = match_playbook(question)
    recipe_block = (f"\n\n【分析公式（按此组织回答）】\n{recipe_text(_pb_key, _pb)}"
                    if _pb is not None else "")

    # 3) 回答
    tool_desc_used = "、".join(t["tool"] for t in tool_trace) or "chat"
    user_payload = (f"用户问题：{question}\n\n选定工具：{tool_desc_used}\n\n"
                    f"工具结果：\n{data_str or '（无，工具为 chat）'}{recipe_block}")
    answer = chat(
        setting,
        [
            {"role": "system", "content": _ANSWER_SYSTEM},
            {"role": "user", "content": user_payload},
        ],
        purpose="nl_query",
        db=db,
        temperature=0.2,
        lang=lang,
    )
    usage_total["answer"] = answer["usage"]

    duration_ms = int((time.monotonic() - started) * 1000)
    # 4) 留痕
    db.add(AiQueryLog(
        user_id=user_id, question=question, answer=answer["content"],
        tools_used=json.dumps(tool_trace, ensure_ascii=False),
        token_usage=json.dumps(usage_total, ensure_ascii=False),
        status="ok", duration_ms=duration_ms,
    ))
    db.commit()
    return {
        "answer": answer["content"],
        "tool_trace": tool_trace,
        "usage": {k: v for k, v in usage_total.items()},
        "duration_ms": duration_ms,
    }
