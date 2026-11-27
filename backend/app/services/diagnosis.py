"""诊断 Agent 编排：意图分诊 -> ReAct 循环 + 只读取证工具。

流程：
0. 意图分诊（diag_router）：问题 -> link_fault / cost_analysis / dba 场景，
   每个场景有自己的方法论 prompt 和工具白名单（防止"看 CDN 消耗"被拿去走链路排查）
1. 用户问题（如 "www.example.com 打不开"）+ 该场景工具表 -> LLM 选择下一步工具（严格 JSON）
2. 工具执行（白名单参数化，见 diag_tools 安全说明）-> 观测结果回填对话
3. 循环直到 LLM 输出 final（最多 _MAX_STEPS 轮，超时强制收敛）
4. 最终报告：症状 / 排查路径与证据 / 结论（根因+置信度）/ 建议动作（人工执行）

安全红线：LLM 只产出工具名+参数与结论文本；工具全部只读；
绝不执行 LLM 生成的 SQL/PromQL 原文；建议动作只出文本，由人执行。

留痕 diagnosis_sessions（问题/场景/步骤/结论/token/耗时），LLM 调用 purpose=diagnosis*。
"""
from __future__ import annotations

import json
import logging
import re
import time

from sqlalchemy.orm import Session

from app.models.diagnosis import DiagnosisSession
from app.services import diag_tools
from app.services.diag_router import route_scenario, scenario_label
from app.services.diag_tools import ToolError, tools_for_scenario
from app.services.playbooks import match_playbook, recipe_text
from app.services.llm import LLMError, chat

logger = logging.getLogger("qianshu.diagnosis")

# 多假设问题需要给下游数据库证据预留步数；关键词预取避免预算被主机与进程查询耗尽。
_MAX_STEPS = 8              # 最多工具调用轮数
_OBS_MAX_CHARS = 30000     # 单次观测结果入提示词前的截断长度
_MAX_TOKENS = 393216       # 当前模型提供方允许的单次输出上限；更换模型时需重新核对

_DIAG_SYSTEM = """你是一名资深运维诊断专家，负责排查当前多云/私有云环境中业务的{scenario_label}问题。
用户报告了一个现象。你通过调用只读排查工具逐层取证，最终定位根因并给出处置建议。
{context}

可用工具：
{tool_desc}

每一轮只输出一个 JSON（不要输出其他任何内容）：
{{"thought": "当前判断与下一步打算（一句话）", "action": {{"tool": "工具名", "params": {{...}}}}}}

当证据足够收敛时，输出最终报告（同样只输出 JSON）：
{{"thought": "结论摘要", "final": true, "level": "critical/warning/ok", "answer": "markdown 报告"}}

注意：answer 力求详尽（完整证据链与数字），但必须保证 JSON 完整闭合，绝不截断。

最终报告（answer 字段，markdown）必须包含：
## 症状 -- 复述问题
## 排查路径与证据 -- 每步查了什么、看到什么数字
## 关联变更 -- 事发前后的云上变更逐条列出（时间/系统/操作/人；无变更就写"窗口内无变更，排除变更致因"）
## 结论 -- **必须挖到环节、点名到对象**，两行固定格式：
   **问题环节**：<哪一层/哪个组件/哪个对象出了问题——链路问题指明层（DNS/网络/入口/后端主机/应用/数据库/缓存）
   和对象（哪台主机/哪个接口/哪个错误）；成本指明产品/实例/域名/计费项；DBA 指明表/空间成分>
   （置信度：高/中/低；确认无异常就明说"未见异常"并给出观察面）
   **处理方向**：<去哪处理——控制台哪个面/找研发/改配置/买容量/做治理，一句话指路>
## 建议动作 -- 可直接照做的步骤（具体到对象与动作），你没有执行权限

报告验收标准：让读者清晰知道哪个环节出了问题、应该去哪个方向处理。
结论里没有"环节+对象"和"处理方向"两行 = 不合格报告。

硬性规则：
- **通用第一步：先看上下文里的【关联变更】证据块（时间/系统/操作/人）**——"最近改了什么"是
  排障第一问，变更窗口能直接定责；证据块不够细（要更精确的时间窗/更多资源）再用
  recent_changes 深挖；无变更也是证据（排除变更致因）
- **答其所问**：只查与问题直接相关的面——磁盘问题不查慢SQL、成本问题不查日志，
  与问题无关的工具不要调用，无关步骤不要出现在报告里
- **缓存/数据库归因必须有正向读数**：判断"Redis 缓存爆了 / 应用 OOM"时，db_health（Redis 内存、
  平均RT、连接数）与 backend_health/process_top（应用内存）必须同时具备；系统已预取的证据直接引用。
  db_health.status=no_data/error 时不得断言正常或异常；partial 只能评价有样本的指标，
  不得给未覆盖项做正常背书；ok 才表示全部指标有样本（是否超阈值仍看 items）
- **资源类问题（CPU/内存/磁盘/进程）先做时间对齐的取证**：峰值时刻（host_metrics）
  -> 该时刻进程 top（process_top）-> 该进程在干什么（process_logs）-> 该时段业务报错（biz_errors），
  缺任何一环就是不合格；链路/域名映射只是背景，不是主证据，不得绕过时间对齐取证回答资源问题
- **结论先行**：报告第一段就是"根因 + 建议动作"，证据和过程在后面；不要为了凑格式写空话
- **建议动作必须具体可执行**：不许"建议优化""建议关注"这类空话——要给出对哪个对象做什么
  （哪张表 VACUUM/哪个索引/改写哪条 SQL/哪台机器做什么）、预期效果、什么情况才升级（如扩容）
- **每个结论都要指路**：用户看完必须知道"哪个环节出了问题、去哪个方向处理"——
  结论含糊（"可能与 XX 有关"）等于没结论，宁可写"证据不足以定位到环节，下一步需要 XX（谁/哪里查）"
- 所有结论必须引用观测到的具体数字，禁止编造
- level：critical=业务已受损或费用影响重大；warning=有异常迹象但不致命；ok=未见异常
- **金额口径（硬规则）**：金额结论一律以 pretax_amount（账单金额）为准；
  payment_amount 是挂账现金字段（常为 0），**禁止用它判断花费或推断抵扣**；
  资源包抵扣情况需查资源包流水，不得从金额字段推断；T+1 账单非终态，近 7 天可能补账修订
- 回答用中文"""

# 各场景的方法论与背景（diag_router 负责意图分诊并选择场景）
def _db_facts(db) -> str:
    """dba 场景的数据库现状：运行时从资产库生成，避免把环境事实写进 prompt 字面量。"""
    import json as _json
    from app.models.asset import Asset

    try:
        rows = db.query(Asset).filter(Asset.product.in_(("rds", "redis"))).all()
    except Exception:
        rows = []
    if not rows:
        return ("数据库现状：资产库暂无 RDS/Redis 记录（可能未同步，先用 rds_list 实时拉清单）。")
    engines: dict = {}
    regions: set = set()
    n_redis = 0
    for a in rows:
        if a.product == "redis":
            n_redis += 1
            continue
        try:
            eng = (_json.loads(a.extra or "{}").get("engine") or "?").strip()
        except Exception:
            eng = "?"
        engines[eng] = engines.get(eng, 0) + 1
        if a.region:
            regions.add(a.region)
    eng_txt = " + ".join(f"{v} {k}" for k, v in sorted(engines.items())) or "0"
    region_txt = f"，{','.join(sorted(regions))}" if regions else ""
    return (f"数据库现状：{len(rows) - n_redis} 个 RDS（{eng_txt}{region_txt}）"
            + (f" + {n_redis} 个云 Redis。" if n_redis else "。"))


def _change_evidence(db) -> str:
    """近 48 小时云上变更的显式证据块（时间/系统/操作/人）。

    变更事实直接进入诊断上下文，报告的【关联变更】段据此成文；无变更也如实记录。
    同时关联画像漂移事件与 change_events 的时间窗，提供漂移前后的变更对照。
    """
    from datetime import datetime, timedelta

    from app.models.change_event import ChangeEvent
    try:
        rows = (db.query(ChangeEvent)
                .filter(ChangeEvent.event_time >= datetime.now() - timedelta(hours=48))
                .order_by(ChangeEvent.event_time.desc()).limit(20).all())
    except Exception:
        rows = []
    lines = ["【关联变更证据块】近 48 小时云上变更（时间 / 系统 / 操作 / 人）："]
    for c in rows:
        lines.append(f"- {c.event_time:%m-%d %H:%M} | {c.service or '?'} | {c.event_name}"
                     f" | {c.user or '未知操作者'} | {c.resource}")
    if not rows:
        lines.append("（近 48 小时无变更记录——排除变更致因也是证据）")

    # ---- 画像漂移 × 变更时间窗关联 ----
    try:
        from app.services.profile_engine import drift_change_windows
        wins = drift_change_windows(db)
    except Exception:
        wins = []
    if wins:
        lines.append("")
        lines.append("【画像漂移 × 变更时间窗关联】近 7 天画像漂移事件与检出窗口内的云上变更：")
        for w in wins:
            ts = str(w.get("ts", ""))[:16].replace("T", " ")
            notes = ("；变化：" + "；".join(w.get("notes") or [])) if w.get("notes") else ""
            lines.append(f"- {ts} 画像漂移 {w.get('title')}（{w.get('key')}）{notes}")
            for c in w.get("changes") or []:
                lines.append(f"    · 窗口内变更 {str(c['ts'])[:16].replace('T', ' ')}"
                             f" | {c['service'] or '?'} | {c['event_name']}"
                             f" | {c['user'] or '未知操作者'} | {c['resource']}")
            if not w.get("changes"):
                lines.append("    · 窗口内无变更（排除变更致因）")
    return "\n".join(lines)


def _architecture_evidence(db) -> str:
    """把最近一次拓扑扫描的真实架构压缩进诊断上下文（只读，不触发新扫描）。"""
    from app.services.topology import scan_snapshot

    try:
        snap = scan_snapshot(db)
    except Exception as e:
        # PostgreSQL 出错后同一事务被标记 aborted，不 rollback 会让后续工具查询全部失败
        db.rollback()
        logger.warning("诊断读取架构快照失败: %s", e)
        snap = None
    if not snap or not snap.get("arch"):
        return ("【架构快照】无可用完整快照。不得假设厂商、产品或固定链路；"
                "只能依据后续工具实际返回的证据判断，并明确架构证据缺口。")
    arch = snap["arch"]
    groups = arch.get("groups") or []
    links = arch.get("links") or []
    lines = [
        f"【架构快照】scan_id={snap['scan_id']}，扫描时间={snap['scanned_at']}。",
        "这是扫描时点的资产与依赖事实，不是实时流量；不得补写快照中不存在的厂商或链路。",
        f"组件组 {len(groups)} 个：",
    ]
    labels = {g.get("id"): g.get("label") or g.get("id") for g in groups}
    for g in groups[:80]:
        scope = g.get("account_label") or g.get("source_label") or "未归属"
        members = [m.get("label") for m in (g.get("members") or []) if m.get("label")]
        suffix = "，成员=" + "、".join(members[:6]) if members else ""
        if len(members) > 6:
            suffix += f" 等{len(members)}个"
        lines.append(f"- [{g.get('role') or '?'}] {g.get('label') or g.get('component') or '?'}"
                     f" | {scope} | count={g.get('count', 0)}{suffix}")
    if len(groups) > 80:
        lines.append(f"- …其余 {len(groups) - 80} 个组件组省略")
    lines.append(f"依赖连线 {len(links)} 条：")
    for link in links[:100]:
        src = labels.get(link.get("source"), link.get("source"))
        dst = labels.get(link.get("target"), link.get("target"))
        types = "/".join(link.get("types") or []) or "unknown"
        lines.append(f"- {src} -> {dst} | type={types} | weight={link.get('weight', 0)}")
    if len(links) > 100:
        lines.append(f"- …其余 {len(links) - 100} 条连线省略")
    if not links:
        lines.append("- （无依赖连线；不得自行推导固定调用链）")
    return "\n".join(lines)


# 链路问题提到缓存、数据库、OOM 或慢时，db_health 是关键证据。命中关键词先预取，
# 避免工具预算被主机与进程查询耗尽；预取只读，失败时保留证据缺口并允许后续重试。
_DB_PREFETCH_KEYWORDS = ("redis", "缓存", "cache", "数据库", "mysql", "rds", "oom",
                         "内存溢出", "内存", "慢", "延迟", "响应时间", "卡顿")


def _needs_db_prefetch(question: str) -> bool:
    q = (question or "").lower()
    return any(k in q for k in _DB_PREFETCH_KEYWORDS)


def _scenario_context(scenario: str, db) -> str:
    ctx = _SCENARIO_CONTEXT.get(scenario, _SCENARIO_CONTEXT["link_fault"])
    if "{db_facts}" in ctx:
        ctx = ctx.replace("{db_facts}", _db_facts(db))
    if "{change_evidence}" in ctx:
        ctx = ctx.replace("{change_evidence}", _change_evidence(db))
    if "{architecture_evidence}" in ctx:
        ctx = ctx.replace("{architecture_evidence}", _architecture_evidence(db))
    return ctx


_SCENARIO_CONTEXT = {
    "link_fault": (
        "{architecture_evidence}"
        "\n排查必须围绕上面的真实组件与连线展开。工具名是能力接口，不代表当前环境一定部署了对应产品；"
        "没有架构快照时不得套用任何厂商的参考链路。"
        "\n1. 有域名/入口线索时，先用 find_domain 对准已知链路域名；查不到就如实记录，不发明映射"
        "\n2. 入口层：快照或实时证据表明存在 SLB 时，用 slb_overview 看状态码/5xx/RT；"
        "有 5xx 再用 slb_5xx_detail；用户点名接口或路径慢时用 path_latency_detail 取平均RT/p95。"
        "无 SLB 证据时直接走实际入口，不把 SLB 当成必经层"
        "\n3. 后端层：backend_health 看实际后端主机的资产状态、可用资源峰值和流量倾斜；"
        "非 ECS 主机没有对应指标时写证据缺口，不按 ECS 指标判断"
        "\n4. 应用层：app_errors 看 ERROR 日志数量与样本内容"
        "\n5. 下游层：db_health 当前只覆盖 RDS/Redis，结合 status/coverage 判断证据完整性；"
        "其他数据库或缓存无指标时不得宣称正常。问题涉及 Redis/缓存/OOM 且系统已预取时直接引用，不要重复查"
        "\n6. 窗口无流量本身只是入口/链路线索，必须结合真实架构区分 DNS、网络、安全组件或未纳管路径"
        "\n**深挖到环节**：结论必须判定故障层并点名实际对象（哪台主机/哪个 URI/哪个错误样本/哪个进程）。"
        "处理方向按实际组件指路：网络/DNS/安全->找网络或安全；负载均衡->查监听/证书/带宽；"
        "后端主机->处理具体资源或进程；应用->带错误样本找研发；数据库->慢SQL/空间/连接转 DBA。"
        "\n\n{change_evidence}"
    ),
    "host_res": (
        "资源类问题（CPU/内存/磁盘使用率、进程占用）的取证是**时间对齐**的三步，"
        "不要绕去链路/域名找答案；资源峰值必须继续查明对应时刻的消耗进程和业务报错："
        "\n1. host_metrics 定位事发时刻：看 mem/cpu/disk 的 peak_time 与曲线形状"
        "（缓慢单调涨=容量问题；短时冲高又回落=某任务/进程突刺）"
        "\n2. process_top 查该时刻谁在吃资源（metric 与告警一致：内存告警查 memory）："
        "重点看 jumped 成员（峰值远超自身日常水位）——那就是嫌疑进程，点名到 .exe/服务名"
        "\n3. process_logs 把嫌疑进程挖到'它在跑什么'：进程名归一匹配日志 AppName"
        "（Beisen./Senyun. 前缀、.exe、尾部序号都归一），看事发时段日志量形状"
        "（骤降/反弹=闷头干重活）与内容样本里的任务名——结论要回答'它当时在干什么'"
        "\n4. biz_errors 查该时段业务是否受损：ERROR 与 5xx 的小时分布是否跟着资源峰值一起涨"
        "\n5. recent_changes 查事发前后变更（扩容/重启/部署），排除处置动作干扰"
        "\n6. 只有当该实例确实在链路上时，才用 find_domain/backend_health 补业务映射；"
        "查不到域名不算失败，直接写'未纳入已知链路'即可"
        "\n**深挖到环节**：结论必须点名到 进程/服务名（哪个 .exe、峰值多少、何时突变）"
        "和业务影响（5xx/ERROR 有没有涨）；处理方向对照：单进程突刺->找研发看该服务该时段"
        "任务/泄漏（给出服务名与时间窗）；持续高位->清理或扩容（先泄漏后升配）；"
        "业务已受损->按故障升级处理；业务未受损->按资源隐患排期处理"
        "\n\n{change_evidence}"
    ),
    "cost_analysis": (
        "统计说明：金额结论一律以 pretax_amount（账单金额）为准；payment_amount 是挂账现金字段（常为 0），"
        "禁止用它判断花费或推断抵扣（资源包抵扣情况查资源包流水，不得从金额字段推断），"
        "包耗尽后账单金额才会突增。T+1 账单非终态，近 7 天可能补账修订。"
        "账单数据 T+1（今天只能查到昨天）。"
        "\n1. pack_pools 先看全部抵扣能力池（告警统计方式是池不是单包，单包快耗尽池内还有额度属正常轮转）"
        "\n2. pool_usage_trend 查目标池的每日消耗趋势、平均/最近日消耗、几天耗尽"
        "\n3. product_cost_trend 对照该产品按量费用趋势（池耗尽前按量应平稳，耗尽后会突增）"
        "\n4. CDN 流量包要定位到具体域名用 cdn_domain_traffic（按域名每日下行流量，"
        "资源包抵扣的就是这个统计方式）；费用定位到实例用 instance_cost_top；相关异动历史用 cost_anomaly_list"
        "\n5. 涉及到期/续购判断时用 expiry_list 看剩余有效期"
        "\n6. 结论要回答三件事：每天消耗多少、趋势如何、按当前速率几天耗尽/已多花多少钱"
        "\n**深挖到环节**：结论必须点名到 产品/实例/域名/计费项（谁在花、谁在烧量），处理方向对照："
        "包将耗尽->续购（给出建议购入量）；流量/费用集中在单个域名或实例->治理该对象（找对应业务）；"
        "快到期用不完->调整抵扣顺序或退换；按量突增->先对照下方【关联变更】证据块（时间/系统/操作/人）"
        "定责（谁/什么变动引起的），需要更细时间窗再用 recent_changes 深挖，然后决定是回滚还是接受"
        "\n\n{change_evidence}"
    ),
    "dba": (
        "{db_facts}"
        "**按问题类型选路径，不跑题**："
        "\n【空间/磁盘类】（磁盘涨、空间不足、增速异常、波动）：rds_list 确认实例 ->"
        " dba_space_trend 看**波动与净增长的区分**（统计说明：data=稳定增长主体；log=轮转摆动；"
        "backup=备份集不占实例盘；残差=统计噪声禁止解释成业务实体）——"
        "**单日 used 尖峰多为备份/日志轮转的波动，波动不是增长、不要处置**；"
        "写满预测用 data 净增速（工具已给 days_to_full）"
        "\n仅当 data 持续异常增长时才下钻：space_suspects 表级嫌疑"
        "（膨胀源=写语句大扫描；临时文件源=大扫描+distinct/order/unnest）+ 慢SQL 扫描行数佐证。"
        "**慢SQL 只是空间归因的行为证据、禁止做性能分析；波动问题查慢SQL 答非所问**"
        "。增长正常就直接结论'无需处置，观察即可'；异常时结论点名到表并给"
        " VACUUM/加什么索引/改写哪类 SQL/何时扩容"
        "\n【性能/慢类】（慢查询、响应慢、CPU 高）：slowlog_top（MySQL 聚合 / PG DAS 明细）"
        " -> db_health 看资源瓶颈"
        "\n【容量规划类】：dba_space_trend 趋势 + 预测写满时间"
        "\nSQL 洞察未开的实例没有明细（成本原因），页面已明示，不要臆测"
        "\n**处理方向**：空间问题给表级动作（VACUUM/索引/改写哪条 SQL）；性能问题给语句/索引改造；"
        "容量问题给扩容判断标准（先优化后扩容）"
        "\n\n{change_evidence}"
    ),
}




def _retrieve_cases(db: Session, question: str, host: str = "", top: int = 3) -> list:
    """经验回流：按问题关键词检索相似历史案例。

    每次诊断前参考同类问题的已确认根因与处置经验。使用可解释的词法匹配，
    不引入向量库；待确认候选不参与检索。
    """
    import re as _re
    from app.models.diagnosis_case import DiagnosisCase
    words = {w for w in _re.findall(r"[\w\.\-]{3,}", question.lower())}
    if host:
        words.add(host.lower())
    rows = (db.query(DiagnosisCase)
            .filter(DiagnosisCase.status == "accepted")   # 只用已确认经验，待确认候选不污染检索
            .order_by(DiagnosisCase.id.desc()).limit(200).all())
    scored = []
    for c in rows:
        hay = f"{c.question} {c.tags} {c.root_cause}".lower()
        score = sum(1 for w in words if w in hay)
        if score:
            scored.append((score, c))
    scored.sort(key=lambda x: (-x[0], -x[1].id))
    out = []
    for score, c in scored[:top]:
        out.append({
            "question": c.question[:120],
            "root_cause": (c.root_cause or "")[:200],
            "solution": (c.solution or "")[:200],
            "tags": c.tags, "_score": score,
        })
    return out


def _tool_desc_block(tools: dict) -> str:
    lines = []
    for name, spec in tools.items():
        params = "; ".join(f"{k}: {v}" for k, v in spec["params"].items())
        lines.append(f"- {name}: {spec['desc']}\n  params: {params or '无参数'}")
    return "\n".join(lines)


def _extract_json(text: str) -> dict:
    """防御式提取 LLM 回复中的 JSON（容忍 ```json 包裹/前后缀）。"""
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise LLMError(f"诊断输出不是 JSON: {text[:200]}")
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        fixed = re.sub(r",\s*([}\]])", r"\1", m.group(0))
        try:
            obj = json.loads(fixed)
        except ValueError:
            raise LLMError(f"诊断 JSON 解析失败: {text[:200]}")
    if not isinstance(obj, dict):
        raise LLMError("诊断输出不是对象")
    return obj


def _obs_summary(result: dict) -> str:
    """观测结果给 LLM 的文本（截断）。"""
    s = json.dumps(result, ensure_ascii=False)
    if len(s) > _OBS_MAX_CHARS:
        s = s[:_OBS_MAX_CHARS] + f"...（已截断，共{len(s)}字符）"
    return s


def _chat_decision(setting, messages: list, db: Session, lang: str = "zh"):
    """一次 LLM 决策调用 + 防御式 JSON 解析。

    输出超长被截断导致 JSON 不闭合时，追加提醒重试一次（要求精简）。
    返回 (resp, decision)；两次都失败抛 LLMError。
    lang：请求界面语言——LLM 回答语言跟随界面，穿透到 chat 的提示词注入。
    """
    resp = chat(setting, messages, purpose="diagnosis", db=db,
                temperature=0, max_tokens=_MAX_TOKENS, lang=lang)
    try:
        return resp, _extract_json(resp["content"])
    except LLMError:
        logger.warning("诊断决策 JSON 解析失败（可能截断），重试一次")
        retry = list(messages) + [
            {"role": "assistant", "content": resp["content"]},
            {"role": "user", "content":
                "上一次输出不是合法 JSON（可能未闭合或夹带了 JSON 以外的内容）。"
                "请重新输出完整 JSON：确认输出为单个完整 JSON 对象，"
                "answer 内不要出现裸换行转义错误，确保引号/花括号全部闭合。"},
        ]
        resp2 = chat(setting, retry, purpose="diagnosis", db=db,
                     temperature=0, max_tokens=_MAX_TOKENS, lang=lang)
        return resp2, _extract_json(resp2["content"])


def run_diagnosis(db: Session, user_id: int, question: str, trigger: str = "manual",
                  lang: str = "zh") -> dict:
    """执行一次完整诊断。返回 session 详情 dict；失败抛 LLMError。

    lang：请求界面语言（zh/en）——require_model 引导文案与 LLM 回答语言跟随界面；
    定时任务/服务层缺省 zh 保持原行为。
    """
    started = time.monotonic()
    from app.models.ai_setting import AiSetting
    from app.services.ai_scenarios import require_model

    # 场景矩阵解析：diagnosis 场景绑定的连接优先，未绑定=兜底默认连接；
    # 停用/无模型 → LLMError 引导（None 路径不崩）。require_model 的 dict 已含连接参数，
    # chat() 吃 ORM 行，按 setting_id 取回同一连接（不走 get_active_setting 全局唯一口径）。
    cfg = require_model(db, "diagnosis", lang=lang)
    setting = db.get(AiSetting, cfg["setting_id"])
    if setting is None:
        raise LLMError("尚未启用任何 LLM 配置，请先在 设置->AI 模型 中添加并启用")

    # 意图分诊：场景决定方法论与工具白名单（失败兜底 link_fault）
    scenario, route_reason = route_scenario(setting, question, db, lang=lang)
    tools = tools_for_scenario(scenario)
    logger.info("诊断分诊: scenario=%s reason=%s question=%s", scenario, route_reason, question[:60])

    # 问题类配方：同类问题模板化套用取证与结论结构。
    # 命中配方则注入取证链公式/结论骨架/处置库，并保证取证链工具可用（只读白名单）
    pb_key, pb = match_playbook(question)
    recipe = ""
    if pb is not None:
        recipe = recipe_text(pb_key, pb)
        for t in pb.get("tools", ()):
            spec = diag_tools.TOOLS.get(t)
            if spec is not None:
                tools.setdefault(t, spec)
        logger.info("问题类配方: %s（场景 %s，配方场景 %s）", pb_key, scenario, pb.get("scenario"))

    prior_cases = _retrieve_cases(db, question)
    if prior_cases:
        logger.info("经验回流: 命中 %d 条相似案例", len(prior_cases))
    session = DiagnosisSession(question=question[:500], created_by=user_id, scenario=scenario,
                                trigger=trigger)
    db.add(session)
    db.commit()

    steps: list = [{
        "step": 0, "thought": f"意图分诊：{scenario_label(scenario)}（{route_reason}）",
        "tool": "route", "params": {},
        "summary": f"场景={scenario}" + (f"，问题类={pb_key}" if pb_key else ""),
        "duration_ms": 0, "error": "",
    }]
    usage_total: dict = {}
    conclusion = ""
    level = "unknown"
    tool_calls = 0

    # 预取下游数据库指标（见 _DB_PREFETCH_KEYWORDS 注释）：占用第 1 步，LLM 从第 2 步起决策。
    prefetch_obs = ""
    first_step = 1
    if scenario == "link_fault" and _needs_db_prefetch(question):
        t0 = time.monotonic()
        result, err = None, ""
        try:
            result = diag_tools.execute_tool(db, "db_health", {"window": "24h"})
        except ToolError as e:
            result, err = None, str(e)
        except Exception as e:  # VM 不可达等；不阻断诊断
            logger.exception("db_health 预取失败")
            result, err = None, f"{type(e).__name__}: {str(e)[:200]}"
        tool_calls += 1
        duration_ms = int((time.monotonic() - t0) * 1000)
        if result is None:
            prefetch_obs = f"db_health 预取失败：{err}。仍需在后续步骤自行查 db_health。"
            summary = f"预取失败：{err[:120]}"
        else:
            prefetch_obs = _obs_summary(result)
            summary = _brief(result)
        steps.append({
            "step": 1, "thought": "问题涉及缓存/数据库/OOM/慢，先预取下游指标（只读）",
            "tool": "db_health", "params": {"window": "24h"},
            "summary": summary, "duration_ms": duration_ms, "error": err,
        })
        first_step = 2

    messages: list = [
        {"role": "system", "content": _DIAG_SYSTEM.format(
            scenario_label=scenario_label(scenario),
            context=_scenario_context(scenario, db),
            tool_desc=_tool_desc_block(tools),
        )},
        {"role": "user", "content": (
            f"问题：{question}"
            + (f"\n\n{recipe}\n" if recipe else "")
            + (f"\n\n【预取证据：db_health（系统已先查，直接引用，勿重复调用）】\n{prefetch_obs}"
               if prefetch_obs else "")
            + (f"\n历史相似案例（仅供参考思路，数字一律以本次观测为准）：\n"
               + "\n".join(f"- 现象: {c['question']} | 根因: {c['root_cause'] or '未填'} | 处置: {c['solution'] or '未填'}"
                            for c in prior_cases)
               if prior_cases else "")
        )},
    ]

    try:
        for step_no in range(first_step, _MAX_STEPS + 1):
            resp, decision = _chat_decision(setting, messages, db, lang=lang)
            usage_total[f"step{step_no}"] = resp["usage"]

            if decision.get("final"):
                conclusion = str(decision.get("answer") or "")
                level = str(decision.get("level") or "unknown")
                steps.append({
                    "step": step_no, "thought": str(decision.get("thought") or ""),
                    "tool": "", "params": {}, "summary": "输出最终结论",
                    "duration_ms": 0, "error": "",
                })
                break

            tool = str(decision.get("action", {}).get("tool") or "").strip()
            params = decision.get("action", {}).get("params") or {}
            thought = str(decision.get("thought") or "")
            if tool and tool not in tools:
                # 场景白名单外的工具（LLM 幻觉）：当作错误反馈，不执行
                result, err = None, f"工具 {tool} 不属于当前场景，可用: {', '.join(tools)}"
                steps.append({
                    "step": step_no, "thought": thought, "tool": tool,
                    "params": params, "summary": f"拒绝：非本场景工具",
                    "duration_ms": 0, "error": err,
                })
                messages.append({"role": "assistant", "content": resp["content"]})
                messages.append({"role": "user",
                                 "content": f"工具 {tool} 不可用：{err}。请换用场景内工具。"})
                continue
            t0 = time.monotonic()
            try:
                result = diag_tools.execute_tool(db, tool, params)
                err = ""
            except ToolError as e:
                result, err = None, str(e)
            except Exception as e:  # VM/SLS 不可达等
                logger.exception("诊断工具执行失败 %s", tool)
                result, err = None, f"{type(e).__name__}: {str(e)[:200]}"
            duration_ms = int((time.monotonic() - t0) * 1000)
            tool_calls += 1

            if result is None:
                obs = f"工具执行失败：{err}。请修正参数重试，或换一个工具。"
                summary = f"失败：{err[:120]}"
            else:
                obs = _obs_summary(result)
                summary = _brief(result)
            steps.append({
                "step": step_no, "thought": thought, "tool": tool,
                "params": params, "summary": summary,
                "duration_ms": duration_ms, "error": err,
            })
            logger.info("诊断 session=%s step=%s tool=%s %s",
                        session.id, step_no, tool, summary[:80])

            messages.append({"role": "assistant", "content": resp["content"]})
            messages.append({"role": "user", "content": f"工具 {tool} 执行结果：\n{obs}"})
        else:
            # 步数用尽仍未收敛：强制一次最终汇报
            messages.append({"role": "user",
                             "content": "已达最大排查步数，请基于以上全部证据立即输出最终报告 JSON。"})
            resp, decision = _chat_decision(setting, messages, db, lang=lang)
            usage_total["final"] = resp["usage"]
            conclusion = str(decision.get("answer") or "")
            level = str(decision.get("level") or "unknown")
            steps.append({
                "step": len(steps), "thought": "步数用尽，强制收敛",
                "tool": "", "params": {}, "summary": "输出最终结论",
                "duration_ms": 0, "error": "",
            })

        # 从问题或步骤里解析目标域名（展示用；只有链路场景的工具参数才是域名——
        # 成本/DBA 场景的 keyword 是"cdn"这类产品词，列在"目标域名"下会误导）
        host = _guess_host(steps) if scenario in ("link_fault", "host_res") else ""
        duration_ms = int((time.monotonic() - started) * 1000)
        session.host = host
        session.status = "ok"
        session.level = level if level in ("critical", "warning", "ok") else "unknown"
        session.conclusion_md = conclusion
        session.steps = json.dumps(steps, ensure_ascii=False)
        session.token_usage = json.dumps(usage_total, ensure_ascii=False)
        session.tool_calls = tool_calls
        session.duration_ms = duration_ms
        # 案例属于可信经验，只有人工点击「入库」才写 diagnosis_cases；
        # 自动诊断结果只留在诊断历史，不直接污染后续检索。
        db.commit()
        logger.info("诊断完成 session=%s level=%s tools=%s %sms",
                    session.id, session.level, tool_calls, duration_ms)
        return session_out(session)
    except Exception as e:
        session.status = "error"
        session.error = f"{type(e).__name__}: {str(e)[:300]}"
        session.steps = json.dumps(steps, ensure_ascii=False)
        session.duration_ms = int((time.monotonic() - started) * 1000)
        db.commit()
        raise


def _brief(result: dict) -> str:
    """工具结果的一句话摘要（前端时间线展示用）。"""
    try:
        items = result.get("items")
        if isinstance(items, list):
            if not items:
                note = result.get("note") or result.get("hint") or "无数据"
                return str(note)[:150]
            first = items[0]
            keys = ("host", "chain", "uri", "ecs_name", "name", "upstream", "product", "app_name")
            label = next((str(first.get(k)) for k in keys if first.get(k)), str(first)[:60])
            return f"{len(items)} 条结果，如 {label[:80]}"
        if "error_count" in result:
            return f"ERROR 日志 {result['error_count']:,} 条"
        return str(result)[:150]
    except Exception:
        return "（摘要生成失败）"


def _guess_host(steps: list) -> str:
    """从工具参数里猜目标域名/实例（展示用）。"""
    for s in steps:
        for key in ("host", "app_name", "instance", "name", "keyword"):
            v = (s.get("params") or {}).get(key)
            if v:
                return str(v)
    return ""


def session_out(s: DiagnosisSession) -> dict:
    def _j(raw, default):
        try:
            return json.loads(raw) if raw else default
        except ValueError:
            return default
    return {
        "id": s.id,
        "question": s.question,
        "host": s.host,
        "scenario": s.scenario or "link_fault",
        "trigger": s.trigger or "manual",
        "status": s.status,
        "level": s.level,
        "conclusion_md": s.conclusion_md,
        "steps": _j(s.steps, []),
        "token_usage": _j(s.token_usage, {}),
        "error": s.error,
        "tool_calls": s.tool_calls,
        "duration_ms": s.duration_ms,
        "created_by": s.created_by,
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }


_KEEP_SESSIONS = 50      # 诊断历史保留条数（已入库案例的会话永不清理）


def prune_sessions(db: Session, keep: int = _KEEP_SESSIONS) -> int:
    """诊断历史只保留最近 keep 条；已沉淀为案例的会话与未完成(running)的除外。

    在每次诊断完成后调用。返回实际删除条数（调用方负责 commit）。
    """
    from app.models.diagnosis_case import DiagnosisCase  # 局部导入避免环

    case_session_ids = {
        cid for (cid,) in db.query(DiagnosisCase.session_id)
        .filter(DiagnosisCase.status != "discarded") if cid is not None
    }
    rows = (db.query(DiagnosisSession)
            .filter(DiagnosisSession.status != "running")
            .order_by(DiagnosisSession.id.desc())
            .all())
    doomed = [s for s in rows[keep:] if s.id not in case_session_ids]
    for s in doomed:
        db.delete(s)
    return len(doomed)
