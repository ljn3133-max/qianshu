"""诊断问题类配方库：把同类问题的判定规律沉淀为可复用模板。

本模块是诊断知识的单一事实源，分两层：

1. DOCTRINE 检测/判定守则——每条包含规则、匿名化反例和挂载点。
   新增或修改检测逻辑前必须对照；修复后更新对应守则，不另建点状记录。
2. PLAYBOOKS 问题类配方——每个问题类包含判定特征、取证链、结论骨架和处置动作库。
   诊断按问题类自动套用配方，报告形态与取证顺序不依赖 LLM 临场发挥。

维护闭环：每次修复都要确认所属守则或配方、扫描同类挂载点，并更新对应知识。

修改配方或守则必须滚动 KNOWLEDGE_VERSION，并登记 agent/knowledge-changelog.md。
"""
from __future__ import annotations

KNOWLEDGE_VERSION = "2026.11.18"

# ---------------------------------------------------------------------------
# 一、检测判定守则（按主题编号 D_*；反例只保留可复用的误判模式）
# ---------------------------------------------------------------------------

DOCTRINE: dict = {
    "D_peak_not_current": {
        "rule": "峰值、均值、当前值三者不许混用：'当前值'必须单独取实测最新点；"
                "报告峰值必须带时间戳（'24h峰值 99.5%（10-07 19:04）'）。",
        "counter_example": "把历史窗口峰值序列的末点当作最新实测值，扩容或指标回落后仍沿用旧峰值，形成虚假的当前告警",
        "apply_to": "baseline_job/detect_growth_accel、告警 breach_value、一切'当前值'文案",
    },
    "D_change_event_lifecycle": {
        "rule": "事件生命周期按语义分两类：变化型（增速/流量突变）未复现即收口；"
                "持续型（池将耗尽）才用 3 日规则。挂旧数字冒充现状=误导。",
        "counter_example": "增速首日达到阈值、次日已经回落，事件仍沿用首日增速保持 open，直至持续型事件的收口期限",
        "apply_to": "cost_anomaly 生命周期、baseline_job._close_unreproduced、一切'突变/增速'类事件",
    },
    "D_pool_level": {
        "rule": "抵扣能力判定用池不用单包：单包耗尽/换包轮转是常态，池有余量就不算异常。"
                "轮转守卫：池 remaining_pct > 20 时单包告警降为 info 轮转记录。",
        "counter_example": "多个资源包轮转时，单包余量下降被持续报 warning；未结合池余量和预计耗尽天数降级为轮转信息",
        "apply_to": "cost_anomaly/pack_spike、pack_impact.compute_pools、资源包一切告警",
    },
    "D_same_day_baseline": {
        "rule": "周期基线按'同日型'分组（workday/rest/holiday，调休上班日=workday——"
                "分组键是实际作息不是星期几）；双门槛（倍数×绝对增量）；"
                "中位数+MAD；数据自校验（改判/降级）。",
        "counter_example": "按星期几分组会把调休补班日误当休息日；均值+σ 容易被节日样本拉偏；"
                           "基线样本不足时回落到混合组，可能只剩假期样本，使复工首日被误判 critical；"
                           "半日数据线性折算会高估早高峰型业务。硬要求三条：①同日型分组 ②样本不足"
                           "宁缺勿报不回落 ③节后复工首日流量回升属常态（放宽 2.5 倍基线）",
        "apply_to": "baseline.py 全部检测、日后一切'与历史同期对比'逻辑",
    },
    "D_time_aligned_evidence": {
        "rule": "资源类问题（CPU/内存/磁盘/进程）按**事发时刻**取证：峰值时刻 → 该时刻进程 top "
                "→ 该进程在干什么 → 该时段业务报错；禁止拿 24h 聚合或链路映射糊弄。",
        "counter_example": "内存峰值告警只沿链路查域名，没有定位峰值时刻的进程，也没有核对同时段业务报错",
        "apply_to": "diag_tools host_metrics/process_top/process_logs/biz_errors、host_res 方法论",
    },
    "D_cross_source_soft_link": {
        "rule": "跨源对应（进程↔日志↔链路）必须显式声明证据强度：归一匹配+时间窗咬合=软对应；"
                "无实例/主机维度时禁止写成硬绑定。",
        "counter_example": "trace 日志缺少实例维度，只有服务名归一和时间窗吻合时，仍把日志与某台机器写成硬绑定；"
                           "工具标注的软对应不得被报告拔高",
        "apply_to": "process_logs、link_draft AppName 关联、一切跨源 join",
    },
    "D_unit_normalization": {
        "rule": "跨行/跨源聚合前先统一量纲；数字必须自带单位与口径（金额带币种、流量带 GB/TB）。",
        "counter_example": "TB/GB 未归一就相加会得到失真比例；金额混用币种会让汇总失去意义；"
                           "没有账单金额的流量包抵扣量被放进金额列，单位与业务口径同时错位",
        "apply_to": "pack_impact、诊断工具输出、一切聚合计算",
    },
    "D_explicit_column": {
        "rule": "解析聚合结果按**列名**取值，禁止'取第一个能转 float 的值'。",
        "counter_example": "SLS 聚合行 (http_host,status,cnt) 把 status='200' 当计数，导致成功率失真；"
                           "查询函数改返回 (值,分区) 元组后，消费端仍按标量做加法或比较，导致扫描失败",
        "apply_to": "link_draft、sls 查询结果解析、一切 group-by 聚合消费",
    },
    "D_sample_before_trust": {
        "rule": "聚合数字当'业务事实'入库/交付前，先抽样看原始内容（状态码、URI、字段值），"
                "显式问一句'这数据真的是我以为的含义吗'。",
        "counter_example": "把泛解析 bot 的 404 静态资源探测当成真实链路边；账单 usage 在源头全空仍硬凑用量列；"
                           "把 RDS 空间残差当成临时文件膨胀，转而分析与波动无关的慢 SQL",
        "apply_to": "link_draft、画像构成、dba_space_trend 归因、一切'新数据面'上线首日",
    },
    "D_residual_sanity": {
        "rule": "残差/差值（总量-分项）必须先做 sanity：为负或量级荒谬=口径不可加，"
                "如实说明口径，禁止解释成业务实体；分项摆动会镜像进残差，残差增量不是'增长源'。",
        "counter_example": "backup 是不占实例盘的备份集，却仍参与 used-data-log 残差计算并得到负值；"
                           "日志轮转日的残差增大被解释为临时文件或表空间膨胀，进而开出 VACUUM、索引或慢 SQL 处置单",
        "apply_to": "dba_space_trend 归因、baseline_job 增速检测口径、一切'分项+残差'构成分析",
    },
    "D_exemption_layers": {
        "rule": "豁免是分层的：aux 资产只看关机+5xx 外溢；系统进程不进画像判定；"
                "已耗尽包不进列表只留汇总；已过期证书不汇报；'待确认'类不占首页待办。",
        "counter_example": "辅助资产、系统进程和内核线程被纳入画像漂移，大量低价值项挤占首页待办",
        "apply_to": "tier.py、profile_engine._is_system、dashboard.summary、expiry 展示口径",
    },
    "D_hysteresis_and_manual_close": {
        "rule": "恢复迟滞带宽 2%（太宽=永不恢复）；每类告警都要有手动收口通道"
                "（标记恢复/设为常态）；恢复不推送。",
        "counter_example": "恢复迟滞带宽过大，指标已经低于告警阈值却仍高于恢复线，事件长期不能恢复",
        "apply_to": "alert_eval._recovered、RESUPPRESS、告警记录操作",
    },
    "D_idempotent_rerun": {
        "rule": "同日幂等重跑不许改变人工状态（用户手动关的事件不重开、手动丢弃的不复活）；"
                "迁移脚本必须幂等（加列+存量 SQL 只在首次加列时跑）。",
        "counter_example": "resolved-reopen 状态错位会重新打开人工关闭的事件；启动迁移重复执行会把已丢弃案例恢复为 pending",
        "apply_to": "cost_anomaly._add、core/migrate.py、一切 upsert/定时重跑逻辑",
    },
    "D_self_heal_cadence": {
        "rule": "缺口自愈类任务的最坏等待时间=轮询周期：唤醒周期必须匹配用户期望的数据到位时刻，"
                "不能只挂'每日定点'。采集器无缺口时的空转成本要低。且**缺口≠全部**："
                "源数据当日可修订（T+1 账单非终态），采集窗口必须固定重拉近 N 天幂等覆盖——"
                "只补缺失的天，被修订的天永不重采。",
        "counter_example": "缺口驱动的成本采集只按每日定点运行，早间账单入库后仍要等到下一次定点才补采；"
                           "T+1 账单同日发生补账或改行时，只补缺失日期而不重拉已采日期，汇总会与原始账单不一致",
        "apply_to": "main.py 各后台 loop 的 sleep 策略、一切'补采/自愈/兜底'类任务",
    },
    "D_human_gating": {
        "rule": "AI 产出（诊断结论/案例/建议）一律不自动成为可信事实源："
                "入库、设为常态、接受基线等'定性动作'必须人工触发。",
        "counter_example": "未经人工确认的 AI 故障结论自动进入案例库，后续被当作可信事实复用",
        "apply_to": "diagnosis 案例沉淀（只留手动「入库」）、画像 accept、告警 accept-baseline",
    },
}

# ---------------------------------------------------------------------------
# 二、问题类配方（诊断侧公式化；evidence_chain 里的工具名是 diag_tools 白名单真名）
# ---------------------------------------------------------------------------

PLAYBOOKS: dict = {
    "traffic_spike": {
        "tools": ["cdn_url_breakdown", "cdn_domain_traffic", "object_chain", "cost_anomaly_list"],
        "label": "流量突增（谁在烧流量）",
        "scenario": "cost_analysis",
        "match": ("流量突增", "流量涨", "带宽突", "流量异常", "流量飙", "谁在烧流量", "流量包烧"),
        "evidence_chain": [
            "cdn_url_breakdown：URL/文件级归因（峰值日较前日谁涨了）——流量突增第一问是'哪个文件/接口涨的'",
            "cdn_domain_traffic：域名级总量与占比（突增是单域名还是全盘）",
            "object_chain：对象全景同窗（接入涨没涨/后端跟没跟/ERROR 有没有/变更有没有）——判断是真业务还是异常",
            "cost_anomaly_list：是否已有对应异动事件（收口状态）",
        ],
        "conclusion_skeleton": (
            "**流量构成**：<域名> 当日 <X>GB（常态 <M>GB），峰值日较前日涨 <N>GB\n"
            "**谁涨的**：<URL/文件 top> 贡献 <+K>GB（占增幅 <P>%）\n"
            "**定性**：<发版分发/业务真涨/异常刷流量>（看后端跟没跟、ERROR 有没有、变更有没有）\n"
            "**建议**：<治理文件/接受新常态/防盗刷/囤包对比>"
        ),
        "actions": [
            "发版后静态包下载潮=正常，标记接受即可，别当故障查",
            "后端没涨而 CDN 涨=静态/缓存侧；后端同步涨+ERROR 涨=业务侧（带样本找研发）",
            "单文件占比异常高（>50%）先查是不是被盗刷/未压缩",
            "流量与费用联动：涨的量会吃流量包，结合资源包池剩余判断要不要囤包",
        ],
        "doctrine": ("D_same_day_baseline", "D_unit_normalization"),
    },
    "billing_qa": {
        "tools": ["bill_breakdown", "bill_config", "cost_anomaly_list", "pack_pools"],
        "label": "账单答疑（这笔钱怎么来的）",
        "scenario": "cost_analysis",
        "match": ("账单", "扣费", "扣了", "计费", "收费", "费用明细", "为啥", "为什么",
                  "消耗了", "花了", "怎么来", "金额", "明细", "多少钱"),
        "evidence_chain": [
            "bill_breakdown：目标近 N 天账单行+逐日趋势+计费场景构成——先看形状："
            "随工作日/假期波动=按量计费；每日恒值=开通即计费；阶梯跳变=分段计费/新功能开通",
            "bill_config：计费相关配置实况（开着什么、免费额度是多少、哪几项收钱）——"
            "'说免费却扣钱'的谜底通常在这里：免费的是存储额度，引入费/索引费按量收",
            "cost_anomaly_list / pack_pools：若是资源包/池类问题分流到容量配方",
            "对账：账单峰值/谷值日期与业务作息、配置变更时间对齐（如某功能开通日=费用跳变日）",
        ],
        "conclusion_skeleton": (
            "**费用构成**：<计费项 A> ¥X/天 + <计费项 B> ¥Y/天，N 天合计 ¥Z（日均 ¥M）\n"
            "**计费模式**：<按量/开通即计/分段阶梯>，为什么<没人用也扣/会波动/会跳变>\n"
            "**建议**：<关哪个模块省多少/免费额度怎么对齐/哪些属正常成本>"
        ),
        "actions": [
            "免费额度对齐配置（如冷存储 ≤30 天、热存储 ≤1 天才免费）",
            "关掉不用的计费模块时先确认依赖（平台慢SQL/审计能力是否受影响），关后验证",
            "按量类费用与业务量挂钩属正常——要省只能降量或关功能，别当故障查",
            "给出月度规模：日均 ×30，供'留还是关'决策",
        ],
        "doctrine": ("D_unit_normalization", "D_sample_before_trust"),
    },
    "resource_spike": {
        "tools": ["recent_changes", "host_metrics", "process_top", "process_logs", "biz_errors"],
        "label": "资源突刺（内存/CPU/磁盘短时冲高又回落）",
        "scenario": "host_res",
        "match": ("内存", "cpu", "飙高", "突刺", "峰值", "使用率过高", "使用率 24h", "oom"),
        "evidence_chain": [
            "recent_changes：排除变更致因（无变更也是证据）",
            "host_metrics：定位峰值时刻与曲线形状（冲高回落=突刺，单调涨=转 resource_growth）",
            "process_top：峰值时刻±30min 谁在吃资源，重点 jumped 成员（峰值远超日常水位）",
            "process_logs：嫌疑进程事发时段在干什么（日志量形状+任务名样本）",
            "biz_errors：该时段 5xx/ERROR 是否同步上涨（业务受损与否定级）",
        ],
        "conclusion_skeleton": (
            "**问题环节**：<服务名/进程名> + <事发时间窗> + <它在跑什么任务>，"
            "整机 <资源> 峰值 <值>@<时刻>（告警口径与实测差异如有须标注）\n"
            "**处理方向**：研发改 <任务的批处理/泄漏点>；业务受损才升级故障，否则按资源隐患排期"
        ),
        "actions": [
            "带着 服务名+时间窗+日志样本 找研发，定位批处理规模/内存增长点（分批/限流/超时退避）",
            "为该进程加进程级资源告警（阈值=日常水位 5~7 倍）",
            "连续观察 3~5 个调度周期；再触顶且 5xx 同步上涨才按故障升级",
            "曲线从突刺转为持续高位才考虑升配（先泄漏后扩容）",
        ],
        "doctrine": ("D_time_aligned_evidence", "D_peak_not_current", "D_cross_source_soft_link"),
    },
    "resource_growth": {
        "tools": ["dba_space_trend", "space_suspects", "slowlog_top", "host_metrics", "process_logs", "recent_changes"],
        "label": "资源增长/空间膨胀（磁盘/空间上涨、增速突变、波动）",
        "scenario": "dba",
        "match": ("空间", "磁盘", "增长", "增速", "膨胀", "写满", "存储", "波动"),
        "evidence_chain": [
            "（RDS）dba_space_trend：先看**成分口径**——data（稳定增长主体）/log（轮转摆动）/"
            "backup（备份集大小，不占实例盘）/残差（口径噪声，禁止解释为业务实体）；"
            "工具输出自带波动归因与净增长，答案优先用它",
            "（RDS）波动归因：单日 used 尖峰/回落多为备份或日志轮转（看 log/backup 分量摆动），"
            "**波动不是增长，不要处置**；先分清'摆动'与'持续单调涨'再往下走",
            "（RDS）净增长主体：data 分量的日增量（稳健中位数）才是写满预测依据；"
            "增长正常（GB/天量级内）就直接给结论'无需处置，观察即可'",
            "（RDS，仅当 data 持续异常增长才走）space_suspects + slowlog_top：表级嫌疑"
            "（膨胀源=密集写语句死元组；临时文件源=超大扫描）——慢SQL 在此**只是空间归因的"
            "行为证据、禁做性能分析**；与磁盘波动无关时不要查，避免把空间问题误导为性能问题",
            "（ECS）host_metrics：增速曲线+当前值（实测）；process_logs：日志/导出任务是不是增长源",
            "recent_changes：扩容/清理动作（会被误读成'自然回落'）",
        ],
        "conclusion_skeleton": (
            "**问题环节**：<实例> 磁盘 <used>GB——波动源 <备份/日志轮转>（日摆 ±<N>GB）；"
            "稳定增长主体 <data 成分> +<M>GB/天，净增速 <K> 天写满（容量 <C>GB）\n"
            "**处理方向**：波动不处置（核对备份保留/日志归档策略即可）；"
            "增长主体异常才做表级治理 <VACUUM/索引/改写>；先优化后扩容"
        ),
        "actions": [
            "波动（备份/日志轮转）：只核对保留策略是否符合预期，不建事件不处置",
            "data 持续增长：表级 VACUUM/加索引/改写大扫描 SQL（点名到表与语句类型）",
            "扩容判据：用净增速（不是单日尖峰）预测，治理后仍 <7 天写满才扩",
            "磁盘使用率 >85% 才谈写满风险；单日尖峰不作数",
        ],
        "doctrine": ("D_residual_sanity", "D_change_event_lifecycle", "D_peak_not_current", "D_unit_normalization"),
    },
    "capacity_exhaust": {
        "tools": ["pack_pools", "pool_usage_trend", "cdn_domain_traffic", "instance_cost_top", "expiry_list"],
        "label": "容量耗尽/到期（资源包池、证书、包年资产）",
        "scenario": "cost_analysis",
        "match": ("耗尽", "资源包", "流量包", "剩", "到期", "续购", "证书", "池"),
        "evidence_chain": [
            "pack_pools：池级剩余/日耗/几天耗尽（单包状态只看轮转不告警）",
            "pool_usage_trend：消耗趋势与最近日耗（增速有无变化）",
            "cdn_domain_traffic / instance_cost_top：归因——谁在烧（域名/实例占比）",
            "expiry_list：剩余有效期与到期分布",
        ],
        "conclusion_skeleton": (
            "**问题环节**：<池/证书/资产> 剩 <余量>（<N> 天耗尽/到期），消耗大头是 <域名/实例> 占 <%>\n"
            "**处理方向**：续购 <建议量> 或治理 <大头对象>；到期的确认续费或释放"
        ),
        "actions": [
            "续购量 = 日耗 × 目标覆盖天数（写明日耗口径与来源）",
            "大头治理：单一域名/实例占 >50% 时点名找对应业务",
            "快到期用不完：调整抵扣顺序或退换",
        ],
        "doctrine": ("D_pool_level", "D_unit_normalization", "D_exemption_layers"),
    },
    "availability": {
        "tools": ["recent_changes", "find_domain", "slb_overview", "slb_5xx_detail", "backend_health", "app_errors", "db_health"],
        "label": "业务可用性（打不开/慢/5xx/超时/报错）",
        "scenario": "link_fault",
        "match": ("打不开", "很慢", "超时", "5xx", "报错", "失败", "白屏", "访问异常"),
        "evidence_chain": [
            "recent_changes：变更窗口能直接定责",
            "find_domain：把模糊输入对准到已知链路域名",
            "slb_overview：状态码分布/5xx率/RT（窗口内零流量本身是线索：DNS/WAF/网络层）",
            "slb_5xx_detail：具体 URI/状态码/后端",
            "backend_health：后端 ECS 状态/资源/流量倾斜",
            "app_errors：ERROR 日志数量与样本（带着样本找研发）",
            "db_health：下游 RDS/Redis 瓶颈",
        ],
        "conclusion_skeleton": (
            "**问题环节**：<层：DNS/网络/SLB/后端机器/应用/数据库> + <对象：哪台 ECS/哪个 URI/哪个错误样本>\n"
            "**处理方向**：按层指路（网络找安全、SLB 看监听/证书、机器看资源/进程、应用带样本找研发、DB 转 DBA 动作）"
        ),
        "actions": [
            "带错误样本（原文+条数）找研发，不带样本不许说'建议关注'",
            "后端倾斜：单台流量/资源异常先摘流或扩容该台",
            "零流量：查 DNS 解析、WAF 拦截、证书到期",
        ],
        "doctrine": ("D_exemption_layers", "D_sample_before_trust", "D_explicit_column"),
    },
    "cost_anomaly": {
        "tools": ["cost_anomaly_list", "product_cost_trend", "instance_cost_top", "pack_pools", "recent_changes"],
        "label": "成本异动（按量突增/新增费用项/转按量多花钱）",
        "scenario": "cost_analysis",
        "match": ("费用", "成本", "账单", "按量", "多花", "钱", "计费"),
        "evidence_chain": [
            "cost_anomaly_list：异动记录（类型/级别/确认状态）",
            "product_cost_trend：产品日成本趋势（按量/摊销拆分；账单 T+1）",
            "instance_cost_top：钱具体花在哪台/哪个域名",
            "pack_pools：是否'包耗尽次生'（按量突增前池应已见底）",
            "recent_changes：变更定位（回滚还是接受）",
        ],
        "conclusion_skeleton": (
            "**问题环节**：<产品/实例/计费项> 费用 +<N> 元/天（基线 <M>），"
            "疑似 <包耗尽次生/新业务/配置变更>\n"
            "**处理方向**：变更致因给回滚判断；包耗尽给续购；新业务确认后接受"
        ),
        "actions": [
            "包耗尽次生：按 capacity_exhaust 配方处理（别当独立费用问题）",
            "变更致因：带变更记录找责任人确认回滚或接受",
            "分级：≥100 元且 3 倍才算 critical；<20 元 info 不推送",
        ],
        "doctrine": ("D_unit_normalization", "D_pool_level", "D_sample_before_trust"),
    },
    "composition_drift": {
        "tools": ["process_logs", "biz_errors"],
        "label": "构成漂移（top 成员更替/陌生成员/占比突变）",
        "scenario": "host_res",
        "match": ("画像", "漂移", "构成", "top1", "陌生成员", "行为"),
        "evidence_chain": [
            "画像快照对比：正常画像 top1/常态成员 vs 当前快照",
            "process_logs / 日志构成：新成员对应的服务在干什么、哪天进来的",
            "biz_errors：漂移时段业务是否受损（定级）",
        ],
        "conclusion_skeleton": (
            "**问题环节**：<对象> 构成变化：<旧 top1> → <新 top1>（自 <日期>），<新成员> 占 <%>\n"
            "**处理方向**：是新业务就接受为常态；不是就查谁把 <新成员> 部署/启动了"
        ),
        "actions": [
            "确认为正常：画像页点「接受」，以当前快照立新基线",
            "非预期：查部署/启动记录，点名到服务与变更",
        ],
        "doctrine": ("D_exemption_layers", "D_human_gating", "D_cross_source_soft_link"),
    },
    "db_perf": {
        "tools": ["slowlog_top", "db_health"],
        "label": "数据库性能（慢SQL/锁/连接/CPU 高）",
        "scenario": "dba",
        "match": ("慢sql", "慢查询", "锁", "连接数", "数据库慢", "查询慢"),
        "evidence_chain": [
            "slowlog_top：慢 SQL Top（次数/耗时/语句）",
            "db_health：CPU/内存/磁盘/连接 vs 阈值",
            "SQL 洞察明细：仅已开洞察实例（未开的跳过，不主动开——成本红线）",
        ],
        "conclusion_skeleton": (
            "**问题环节**：<实例> <语句/索引/锁/连接> 瓶颈，Top 语句 <摘要> 耗时 <N>\n"
            "**处理方向**：<加什么索引/改写哪类语句/调整连接池>"
        ),
        "actions": [
            "点名语句给索引/改写建议（带执行次数与耗时）",
            "洞察未开的实例明示'无明细，可人工开洞察'，不代开",
        ],
        "doctrine": ("D_explicit_column", "D_unit_normalization"),
    },
}


def match_playbook(question: str) -> tuple:
    """按触发词给问题定类；返回 (key, playbook) 或 (None, None)。

    打分制（多类命中取最高分；resource_growth 的'磁盘'与 resource_spike 的
    '使用率过高'可能同时命中，靠分差拉开）。
    """
    q = (question or "").lower()
    if not q:
        return None, None
    best, best_score = None, 0
    for key, pb in PLAYBOOKS.items():
        score = sum(1 for kw in pb.get("match", ()) if kw.lower() in q)
        if score > best_score:
            best, best_score = key, score
    if best is None:
        return None, None
    return best, PLAYBOOKS[best]


def recipe_text(key: str, pb: dict) -> str:
    """把配方渲染成注入诊断 prompt 的公式块。"""
    chain = "\n".join(f"  {i + 1}. {step}" for i, step in enumerate(pb["evidence_chain"]))
    actions = "\n".join(f"  - {a}" for a in pb["actions"])
    doctrine = "、".join(pb.get("doctrine", ()))
    return (
        f"本题命中问题类配方【{pb['label']}】（{key}）——按公式套用，不要临场另起炉灶：\n"
        f"取证链（缺环=不合格）：\n{chain}\n"
        f"结论骨架：\n{pb['conclusion_skeleton']}\n"
        f"处置动作库（照此具体化，不许空话）：\n{actions}\n"
        f"适用判定守则：{doctrine}"
    )


def doctrine_text() -> str:
    """守则清单（供 prompt 或文档引用）。"""
    lines = []
    for did, d in DOCTRINE.items():
        lines.append(f"- {did}：{d['rule']}（反例：{d['counter_example']}）")
    return "\n".join(lines)
