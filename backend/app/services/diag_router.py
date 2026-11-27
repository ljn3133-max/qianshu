"""诊断意图路由：先分诊，再进 ReAct 主循环。

按问题意图选择取证场景，避免成本、主机资源或数据库问题误用 SLB/ECS/应用/RDS
链路故障工具而得到无关结论。

标准（三层）：意图 -> 场景 -> (专用方法论 prompt + 工具白名单)。
- 入口一次轻量 LLM 分类（temperature 0、只输出一个小 JSON）
- 场景清单见 SCENARIOS；分类失败/超时一律兜底 link_fault（= 原行为，不会更差）
- 每个场景的工具白名单见 diag_tools.tools_for_scenario
"""
from __future__ import annotations

import json
import logging
import re

from sqlalchemy.orm import Session

from app.services.llm import LLMError, chat

logger = logging.getLogger("qianshu.diag_router")

SCENARIOS = {
    "link_fault": {
        "label": "链路故障",
        "desc": "打不开/很慢/5xx/报错/接口失败/超时/页面白屏——沿 SLB->ECS->应用->DB 链路排查",
    },
    "host_res": {
        "label": "主机资源",
        "desc": "CPU/内存/磁盘使用率告警、进程占用、资源飙高、机器卡——定位峰值时刻+吃资源的进程+业务影响",
    },
    "cost_analysis": {
        "label": "成本与资源包",
        "desc": "资源包消耗/快耗尽/费用上涨/转按量/续购/到期/账单——分析消耗趋势与费用影响",
    },
    "dba": {
        "label": "数据库专项",
        "desc": "慢SQL/数据库空间/磁盘增长/连接数/大key——数据库维度下钻",
    },
}

_ROUTE_SYSTEM = """你是运维平台的诊断分诊员。把用户的问题分类到一个诊断场景，只输出一个 JSON：
{"scenario": "link_fault|host_res|cost_analysis|dba", "reason": "一句话依据"}

场景定义：
- link_fault（链路故障）：网站/接口打不开、很慢、5xx、报错、超时、白屏、用户访问异常
- host_res（主机资源）：ECS/服务器的 CPU、内存、磁盘使用率告警或飙高、进程占用高、机器卡/慢
- cost_analysis（成本与资源包）：资源包/流量包消耗、快耗尽、费用、按量计费、账单、续购、到期、证书/域名过期
- dba（数据库专项）：慢SQL、数据库空间/磁盘、连接数、锁、大key/热key

判断依据（按优先级）：
1. 问题里提到资源包/包/消耗/流量包/费用/账单/耗尽/续购/到期/证书 -> cost_analysis
2. 问题里提到慢SQL/空间/大key/热key/数据库锁/数据库连接，或明确是 RDS/Redis/pgsql/mysql 的空间磁盘问题 -> dba
3. 问题里提到主机/服务器/ECS 的 CPU/内存/磁盘使用率、进程、资源飙高、机器卡 -> host_res
   （"ECS 内存使用率 24h峰值 99.5%""磁盘使用率过高""CPU飙高"都是 host_res，不是 link_fault）
4. 域名+打不开/慢/报错/失败/5xx/超时 -> link_fault
5. 拿不准 -> link_fault"""


def _keyword_fallback(question: str) -> tuple[str, str]:
    """分类器两轮都失败时的关键词兜底——比一律 link_fault 精准（磁盘问题
    兜进链路场景会全盘跑错工具）。"""
    q = (question or "").lower()
    if any(k in q for k in ("慢sql", "慢查询", "空间", "存储", "数据库", "rds", "pgsql", "mysql", "redis", "大key", "热key")):
        return "dba", "分类器失败，关键词判定为数据库场景"
    if any(k in q for k in ("内存", "cpu", "进程", "使用率", "飙高", "磁盘", "主机", "服务器", "ecs")):
        return "host_res", "分类器失败，关键词判定为主机资源场景"
    if any(k in q for k in ("包", "费用", "成本", "账单", "消耗", "计费", "cdn 流量", "流量")):
        return "cost_analysis", "分类器失败，关键词判定为成本场景"
    return "link_fault", "分类器失败，关键词无命中，按链路故障处理"


def route_scenario(setting, question: str, db: Session, lang: str = "zh") -> tuple[str, str]:
    """返回 (scenario, reason)。两轮 LLM 分类都失败时走关键词兜底，避免空回复
    把磁盘等非链路问题带进错误的工具场景。
    lang：请求界面语言——穿透到 chat 的回答语言注入。"""
    text = ""
    for attempt in (1, 2):
        try:
            resp = chat(
                setting,
                [
                    {"role": "system", "content": _ROUTE_SYSTEM},
                    {"role": "user", "content": f"用户问题：{question}\n请输出分类 JSON。"},
                ],
                purpose="diagnosis_route", db=db, temperature=0, max_tokens=200,
                lang=lang,
            )
        except LLMError as e:
            logger.warning("意图分类 LLM 调用失败（第 %d 轮）: %s", attempt, e)
            continue
        text = (resp.get("content") or "").strip()
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                obj = json.loads(m.group(0))
                scenario = str(obj.get("scenario") or "")
                reason = str(obj.get("reason") or "")[:120]
                if scenario in SCENARIOS:
                    return scenario, reason
            except ValueError:
                pass
        logger.warning("意图分类输出无法解析（第 %d 轮: %s）", attempt, text[:80])
    sc, why = _keyword_fallback(question)
    logger.warning("意图分类两轮失败，关键词兜底: %s", sc)
    return sc, why


def scenario_label(scenario: str) -> str:
    return SCENARIOS.get(scenario, SCENARIOS["link_fault"])["label"]
