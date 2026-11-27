"""全局配置：环境变量 / .env 加载。敏感项一律不进代码库。"""
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 应用
    APP_NAME: str = "qianshu"
    ENV: str = "dev"  # dev / prod
    API_PREFIX: str = "/api"

    # 数据库：只支持 PostgreSQL（SQLite 已废除，不再提供数据迁移）。
    # 默认值使用本机 Unix socket 的 qianshu 库：源码安装以 qianshu 用户运行、
    # 对端 role 同名，peer 认证即可连接；compose/源码安装器会写入显式 URL。
    DATABASE_URL: str = "postgresql:///qianshu"
    # 连接池（每个 uvicorn worker / 调度进程各自持一池）
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_RECYCLE: int = 1800      # 秒；回收长闲连接，避开 LB/RDS 的 idle 断连
    DB_POOL_TIMEOUT: int = 30        # 秒；等池超时（池满时排队上限）

    # 安全
    SECRET_KEY: str = "change-me-in-env"          # JWT 签名
    MASTER_KEY: str = ""                          # AES-256-GCM 主密钥(64位hex)；加密云凭证用
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 12 * 60

    @model_validator(mode="after")
    def _validate_database_url(self):
        """SQLite 已废除：启动即拒绝非 PostgreSQL 连接串，避免静默建空库。

        只回报协议名，不回显完整 URL（可能含口令）。
        """
        url = (self.DATABASE_URL or "").strip()
        if not url.startswith(("postgresql://", "postgresql+", "postgres://")):
            scheme = url.split(":", 1)[0] or "空"
            raise ValueError(
                "千枢只支持 PostgreSQL：DATABASE_URL 必须指向 PostgreSQL"
                f"（当前协议: {scheme}）。SQLite 已废除且不提供旧库迁移，"
                "请按 docs/active/deployment.md 配置后重试")
        return self

    @model_validator(mode="after")
    def _validate_production_secrets(self):
        """生产配置拒绝弱占位密钥；开发默认值仅供隔离测试和本地启动。"""
        if self.ENV.strip().lower() not in {"prod", "production"}:
            return self
        if len(self.SECRET_KEY) < 32 or self.SECRET_KEY.lower().startswith("change-me"):
            raise ValueError("生产环境 SECRET_KEY 必须是至少 32 字符的随机值")
        if len(self.MASTER_KEY) != 64:
            raise ValueError("生产环境 MASTER_KEY 必须是 64 位十六进制随机值")
        try:
            master_key = bytes.fromhex(self.MASTER_KEY)
        except ValueError as exc:
            raise ValueError("生产环境 MASTER_KEY 必须是 64 位十六进制随机值") from exc
        if len(master_key) != 32:
            raise ValueError("生产环境 MASTER_KEY 必须是 64 位十六进制随机值")
        return self
    # 可信代理：XFF/X-Real-IP 只在 TCP 来源命中这些网段时才用于审计来源 IP。
    # 默认 "loopback, private"=本机+私网代理；可追加 CIDR；"none"=一律不信转发头；"any"=全信（不推荐）
    TRUSTED_PROXIES: str = "loopback, private"

    # 初始管理员（首次启动无用户时自动创建）
    INIT_ADMIN_USERNAME: str = "admin"
    # 空=首启随机生成（写入后端日志，仅首启一次）；install.sh 会把随机值写进 .env。
    # 可显式指定（测试/自动化环境），但开源/公网部署不应使用固定 admin123。
    INIT_ADMIN_PASSWORD: str = ""

    # Redis（缓存/限流，预留，当前未使用）
    REDIS_URL: str = "redis://localhost:6379/0"

    # 指标采集（云监控 -> VictoriaMetrics）
    VM_URL: str = "http://127.0.0.1:8428"
    # VictoriaLogs（预留日志存储，当前未直写）：非空时引擎保留清理会探活其删除 API
    VICTORIALOGS_URL: str = ""
    METRICS_COLLECT_ENABLED: bool = True
    METRICS_COLLECT_INTERVAL: int = 300      # 采集周期(秒)，与 CMS 查询粒度一致
    METRICS_COLLECT_QPS: float = 10.0        # 令牌桶限速，远低于账号配额

    # 按数据源分级监控：False=关闭分级策略，使用固定节奏和全量提醒；
    # 不影响采集本身（采集总开关仍是 METRICS_COLLECT_ENABLED / 账号 enabled）。
    MONITOR_POLICY_ENABLED: bool = True

    # 云资产定时同步：全部启用账号每 N 秒同步一次资产。
    # 0=关闭定时同步（接入即同步仍生效；页面手动同步不受影响）
    CLOUD_SYNC_INTERVAL_S: int = 21600       # 默认 6 小时

    # SLS 日志查询（只读）。端点跟 project 所在区域走，安装向导可探测，env 可覆盖。
    SLS_ENDPOINT: str = "cn-hangzhou.log.aliyuncs.com"
    SLS_QUERY_TIMEOUT: int = 60             # 单次查询 SLS 超时(秒)

    # 告警评估
    ALERT_EVAL_ENABLED: bool = True
    ALERT_EVAL_INTERVAL: int = 60           # 评估周期(秒)

    # 告警降噪：恢复迟滞、再触发抑制和恢复通知
    ALERT_HYSTERESIS: float = 0.02          # 恢复迟滞：阈值下 2% 才算恢复（防 89.9↔90 微抖；
                                        # 10% 太宽会让'清理后缓慢回落'的磁盘类长时间挂账）
    ALERT_RESUPPRESS_MINUTES: int = 60      # 同规则同目标恢复后再触发的抑制期（抑制期内只记录不推送）
    ALERT_RECOVERY_NOTIFY: bool = False     # 恢复是否推送（默认只留痕，推送量直接减半）

    # ---------------- 通知决策分诊 ----------------
    # 三档：critical 实时推企微；warning/info 进每日「今日决策清单」；全部留站内信
    NOTIFY_PUSH_MIN_LEVEL: str = "critical"   # 企微/钉钉只推该级别及以上（可改 warning）
    DECISION_DIGEST_ENABLED: bool = True
    DECISION_DIGEST_HOUR: int = 9            # 每天一条决策清单（替代碎片轰炸）
    DECISION_DIGEST_MINUTE: int = 5
    # critical 自动触发诊断 Agent，通知直接带根因（战时替人排障闭环）
    AUTO_DIAG_ENABLED: bool = True
    AUTO_DIAG_COOLDOWN_MIN: int = 360        # 同目标 6 小时内最多一次自动诊断（控 LLM 成本）

    # 成本采集（BSS T+1）
    BSS_ENDPOINT: str = "business.aliyuncs.com"
    COST_COLLECT_ENABLED: bool = True
    COST_COLLECT_HOUR: int = 10            # 每日采集时刻（T-1 账单已完整）
    COST_COLLECT_MINUTE: int = 0

    # 成本异动检测
    COST_ANOMALY_RATIO: float = 1.5        # 环比/近7日均值 突增倍数阈值
    COST_ANOMALY_MIN: float = 10.0         # 参与判断的最小日金额(元)，过滤零头噪音
    COST_ANOMALY_NEW_MIN: float = 0.1     # 新增费用项门槛：金额只定级，不否决新增项的变化信号
    COST_CRITICAL_MIN: float = 100.0       # 突增判 critical 的日金额门槛(元)，不够则 warning
    COST_INFO_MAX: float = 20.0            # 日金额低于此值降 info（不推送、不进巡检首页）
    PACK_SPIKE_RATIO: float = 5.0          # 资源包日消耗 vs 近7日均值 突增倍数
    PACK_SPIKE_MIN_PCT: float = 2.0        # 突增判定最小日消耗（占总量%，过滤微量包）
    PACK_EXHAUST_DAYS: int = 30            # 按当前速率 N 天内耗尽 -> 预警
    PACK_EXPIRY_DAYS: int = 30             # N 天内到期且余量>20% -> 浪费预警
    PACK_EXPIRY_MIN_PCT: float = 20.0

    # 巡检流水线：巡检先于 08:30 日报执行
    INSPECTION_ENABLED: bool = True
    INSPECTION_HOUR: int = 8
    INSPECTION_MINUTE: int = 10
    INSPECTION_HEALTH_WARN: float = 80.0   # 使用率类指标预警阈值(%)
    INSPECTION_HEALTH_CRIT: float = 90.0   # 使用率类指标严重阈值(%)
    INSPECTION_IDLE_CPU: float = 5.0       # 7天平均 CPU 低于此值视为闲置嫌疑(%)
    INSPECTION_IDLE_QPS: float = 0.01      # SLB 7天平均 QPS 低于此值视为无流量
    INSPECTION_EXPIRY_DAYS: int = 30       # 包年包月资产到期预警天数

    # 巡检日报：巡检 08:10 之后生成日报并通知
    INSPECTION_REPORT_ENABLED: bool = True
    INSPECTION_REPORT_HOUR: int = 8
    INSPECTION_REPORT_MINUTE: int = 30

    # DBA 空间采集：每日快照 RDS ResourceUsage 落库支撑趋势
    DBA_COLLECT_ENABLED: bool = True
    DBA_COLLECT_HOUR: int = 9
    DBA_COLLECT_MINUTE: int = 40

    # 域名/证书到期采集：每日一次，只读列表 API
    EXPIRY_SYNC_ENABLED: bool = True
    EXPIRY_SYNC_HOUR: int = 9
    EXPIRY_SYNC_MINUTE: int = 20

    # CDN 按域名每日流量采集：每日一次只读，用于分析资源包消耗对象；
    # 采昨日整日，首次启动回补 CDN_BACKFILL_DAYS 天。
    CDN_SYNC_ENABLED: bool = True
    CDN_SYNC_HOUR: int = 9
    CDN_SYNC_MINUTE: int = 35
    CDN_BACKFILL_DAYS: int = 14

    # 链路草稿每日刷新，无需手动触发即可持续更新
    LINK_DRAFT_ENABLED: bool = True
    LINK_DRAFT_HOUR: int = 4
    LINK_DRAFT_MINUTE: int = 26

    # 数据保留：无界增长表滚动清理，0=永久保留
    MAINT_ENABLED: bool = True
    MAINT_HOUR: int = 3
    MAINT_MINUTE: int = 17
    # 每日数据库备份：PostgreSQL pg_dump
    # 同一入口（services/db_backup.backup_db），默认 03:37（错开 03:17 清理与 pg-backup.sh
    # cron，避免同刻双跑 pg_dump）。目录 compose 形态配 BACKUP_DIR=/backups（卷持久化，
    # 容器重建不丢）。PG_DUMP_CONTAINER 空=自动探测 qianshu-pg/qianshu-postgres/postgres。
    BACKUP_ENABLED: bool = True
    BACKUP_HOUR: int = 3
    BACKUP_MINUTE: int = 37
    BACKUP_KEEP_DAYS: int = 14
    BACKUP_DIR: str = "/opt/qianshu/backups"
    PG_DUMP_CONTAINER: str = ""
    RETENTION_LLM_DAYS: int = 90        # llm_call_logs
    RETENTION_AUDIT_DAYS: int = 180     # audit_logs
    RETENTION_MSG_DAYS: int = 90        # in_app_messages（已读+未读都清，站内信是通知不是账）
    RETENTION_EVENT_DAYS: int = 180     # 事件/记录类（告警/异动/巡检/变更）只删已收口的

    # SLS 目标（千枢产品态：默认全空，由安装向导或环境变量注入，绝不内置任何客户环境事实）。
    # 未配置时相关能力优雅降级（日志类查询返回"未配置"，不报错不硬凑）。
    # 巡检日志摘要：[[project, logstore, 中文名, 检索条件, 指标名], ...]
    SLS_INSPECTION_TARGETS: str = '[]'
    # SLB 访问日志：[[project, logstore, chain(outer/inner)], ...]
    SLS_SLB_TARGETS: str = ''
    # 应用日志：[project, logstore]
    SLS_APP_LOG: str = ''
    # CDN 实时日志：[project, logstore]
    SLS_CDN_TARGET: str = ''
    # 全局 SLS 目标归属账号；0=升级前旧配置（沿用默认凭证解析），非 0 时必须严格匹配，禁止跨账号回退。
    SLS_ACCOUNT_ID: int = 0

    # 数据源注册表：JSON 数组，换环境改配置不改代码。
    # METRIC_SOURCES: [{"name","type":"prometheus|vm|zabbix|influxdb|graphite|es|opentsdb|http","url"}]
    #   Prometheus 与 VM API 兼容查询已实现；其余类型可保存配置，未适配的采集能力优雅降级。
    # LOG_SOURCES:    [{"name","type":"loki|sls|cls|es|graylog|filebeat|http|skywalking|zipkin","url"}]
    # builtin-vm（千枢自带指标库）不出现在用户可配列表，保存时强制保留。
    METRIC_SOURCES: str = '[{"name":"builtin-vm","type":"vm","url":""}]'
    LOG_SOURCES: str = '[]'
    # 初始化完成标志："1"=向导已完成，菜单隐藏、守卫不再强跳。
    BOOTSTRAP_INIT_DONE: str = "0"

    # 对外可见基地址：设置后（如 https://dash.example.com），教程/Agent 命令等接入点
    # 以此为准；默认空=按请求头推导（X-Forwarded-*/Host），域名访问显域名、IP 访问显 IP。
    PUBLIC_BASE_URL: str = ""
    # 事件源 webhook：GitLab/Gitea/GitHub/Jenkins → change_events。
    # token 空=未启用（403）；配置后 CI 系统带对应 header 即可推送。
    EVENT_WEBHOOK_TOKEN: str = ""
    # 混合云 Agent 注册 token：平台生成并下发给 agent，agent 只持有单向 token。
    AGENT_TOKEN: str = ""
    # qianshu-copilotd 独立运行时生命周期桥 token：同机守护进程上报自愈事件、查询最近变更；
    # token 空=桥关闭（403），daemon 侧同读 .env 此值。
    COPILOTD_BRIDGE_TOKEN: str = ""

    # ---------------- 平台内自愈（SELF-HEAL） ----------------
    # 总开关关闭时只建表、不产生问题单；observe=只记录/站内信/人工查看，
    # assist=允许 LLM 诊断与建议但 R0 也必须人工触发，auto_r0=自动 R0/R1，
    # approved_r2=管理员授权后重启平台，approved_r3=管理员看 diff 授权后改平台代码。
    # R2/R3 永不自动执行；服务器重启、系统参数、平台外资源、外部通知、数据库删除一律禁止。
    SELF_HEAL_ENABLED: bool = True
    SELF_HEAL_MODE: str = "observe"
    SELF_HEAL_LLM_ENABLED: bool = False
    SELF_HEAL_KILL_SWITCH: bool = False
    SELF_HEAL_MAX_ATTEMPTS: int = 3          # 同一问题 24h 自动修复上限（含 R0/R1）
    SELF_HEAL_MAX_PLANS_PER_DAY: int = 30    # 全局每日自动计划预算
    SELF_HEAL_MAX_TOKENS_PER_DAY: int = 200000
    SELF_HEAL_STORM_PER_MIN: int = 20        # 单来源每分钟新问题上限，超过合并为风暴单
    SELF_HEAL_PLAN_TTL_MIN: int = 1440       # 计划过期时间；过期必须重新生成/授权
    SELF_HEAL_RECURRENCE_HOURS: int = 24     # 恢复后同指纹复发升级窗口
    SELF_HEAL_BREAKER_FAILS: int = 5         # 连续执行失败熔断阈值
    SELF_HEAL_RETRY_COOLDOWN_S: int = 60     # R0/R1 自动失败后的重试冷却（秒）
    SELF_HEAL_MAX_EVIDENCE_CHARS: int = 12000

    # 千枢智能体两轮交付制：主答→审查→pass 直接交付，fail 一轮修订。
    # 成本护栏：只审 query/analyze 意图且回答含数据断言。
    COPILOT_REVIEW_ENABLED: bool = True
    COPILOT_REVIEW_TOKEN_BUDGET: int = 8000   # 单会话审查 token 累计预算，用尽熔断跳审
    # LLM 回答语言跟随界面："UI"=按请求 Accept-Language 在系统提示词尾部追加
    # Respond in {language}.（en 才追加，zh 不追加）；其他值（如 zh/fixed）=固定中文，
    # 不注入语言指令。
    LLM_ANSWER_LANG_FOLLOW: str = "UI"
    # 值班升级链：critical 告警 N 分钟无人认领 -> 逐级升级。
    ALERT_ESCALATE_MINUTES: int = 15
    ONCALL_ENABLED: bool = True
    # 测试渠道路由：True=所有外发消息强制落到测试渠道（记录不真发）。
    NOTIFY_TEST_MODE: bool = False
    # 登录图形验证码开关：True 时登录必填。
    CAPTCHA_ENABLED: bool = False
    NOTIFY_TEST_CHANNEL: str = "inapp"
    # 短信/语音模板（阿里云控制台申请后填入）
    SMS_SIGN_NAME: str = ""
    SMS_TPL_CODE: str = ""
    VOICE_TTS_CODE: str = ""
    # 企业授权文件（JSON + Ed25519 签名，产品内只嵌公钥）；空/缺失=社区模式（全功能）
    LICENSE_FILE: str = ""
    # K8s 容器日志保留天数：修改后立即按新规则清存量。
    K8S_LOG_RETENTION_DAYS: int = 7
    # VM 指标 / VL 日志保留天数：
    # 用户配置在 ≤引擎上限 内**实时生效**（查询口径过滤 + 后台渐进清理，见
    # docs/active/deployment.md）；0=永久（至引擎上限）。业务侧 RETENTION_* 走每日
    # 清理循环，保存即热生效。引擎上限另见 *_RETENTION_MAX_DAYS。
    VM_RETENTION_DAYS: int = 180
    VL_RETENTION_DAYS: int = 30
    # 存储引擎上限层（部署时定，=容器 -retentionPeriod 启动参数）：用户配置 ≤上限 实时生效，
    # 调大超过上限才需重建容器（引擎限制，如实告知）。上限勿填 0（引擎会生成 0d 无效参）。
    VM_RETENTION_MAX_DAYS: int = 365
    VL_RETENTION_MAX_DAYS: int = 180
    # 引擎超期数据渐进清理周期（分钟）：VM 死序列清理 / VL 删除任务（能力探活）
    ENGINE_RETENTION_INTERVAL_MIN: int = 360

    # 首页产品卡片：产品集随环境而异，JSON 数组串：
    # [[key, label, icon, color], ...]；默认为阿里云通用产品集，换环境改 env 即可
    PRODUCT_CARDS: str = (
        '[["ecs","ECS 云服务器","Monitor","#409eff"],'
        '["rds","RDS 数据库","Coin","#67c23a"],'
        '["redis","Redis","Lock","#f56c6c"],'
        '["slb","SLB 负载均衡","Connection","#e6a23c"],'
        '["oss","OSS 对象存储","Files","#9a6fe0"],'
        '["cdn","CDN","Promotion","#13c2c2"],'
        '["waf","WAF 防火墙","Umbrella","#fa8c16"],'
        '["ack","ACK 容器","Grid","#722ed1"],'
        '["es","Elasticsearch","Search","#52c1a"]]'
    )

    # ---------------- 监控联动 · 周期基线异常检测 ----------------
    # 节假日自动同步（含调休班次），无需手动录入；源=holiday-cn 镜像国务院安排。
    HOLIDAY_URL_TEMPLATE: str = "https://cdn.jsdelivr.net/gh/NateScarlet/holiday-cn@master/{year}.json"
    HOLIDAY_SYNC_ENABLED: bool = True
    HOLIDAY_SYNC_HOUR: int = 3
    HOLIDAY_SYNC_MINUTE: int = 42

    # 基线引擎默认参数（各检测器可覆盖）
    BASELINE_WINDOW_DAYS: int = 56          # 同类日样本窗口（8 周）
    BASELINE_MIN_SAMPLES: int = 2           # 少于该样本数 -> 只观察不告警（冷启动）
    BASELINE_RATIO_WARN: float = 1.8        # 超中位数倍数 -> warning
    BASELINE_RATIO_CRIT: float = 3.0        # 超中位数倍数 -> critical
    BASELINE_DROP_RATIO: float = 0.5        # 低于中位数该比例 -> "该忙反低"（仅工作日）
    BASELINE_GROWTH_WINDOW: int = 28        # 增速型：日增速基线窗口
    BASELINE_GROWTH_RATIO: float = 3.0      # 增速型：超日常增速倍数
    BASELINE_GROWTH_MIN_DELTA: float = 3.0  # 增速型：绝对增速下限（ECS 使用率百分点/天）

    # 各检测器的绝对增量门槛（双门槛的第二道；防低基数放大）
    BASELINE_MIN_COST_YUAN: float = 50.0    # 成本：超基线 50 元/天 才算
    BASELINE_MIN_TRAFFIC_GB: float = 20.0   # CDN 流量：20GB/天
    BASELINE_MIN_SPACE_GB: float = 1.0      # RDS 空间增速：1GB/天

    # 基线检测任务（每日一次，跑 CDN 流量异常 + 磁盘/空间增速异常）
    BASELINE_JOB_ENABLED: bool = True
    BASELINE_JOB_HOUR: int = 10
    BASELINE_JOB_MINUTE: int = 26

    # ---------------- 行为画像：画像-变化-归因-决策 ----------------
    PROFILE_JOB_ENABLED: bool = True
    PROFILE_JOB_HOUR: int = 11
    PROFILE_JOB_MINUTE: int = 7
    # 慢层双采样：日采样时刻点（每天 4~6 次，逗号分隔小时 0~23，可配置）。
    # 每个时刻采一份"当日累计口径"快照，当日多快照聚合成日构成（聚合规则见
    # profile_engine._aggregate_day）；采样任务按"下一个采样时刻"调度（app/main.py）
    PROFILE_SAMPLE_HOURS: str = "2,8,14,20"
    PROFILE_LEARN_DAYS: int = 7          # 学习/检测窗口（逐日构成样本）
    PROFILE_MIN_SAMPLES: int = 5         # 形成基线的最少样本数
    PROFILE_NEW_MEMBER_MIN_SHARE: float = 15.0   # 陌生成员进 top3 的占比下限（%）
    # 事件集成主动联动：异动检出即刻驱动画像采样和判定，主动钩子挂在
    # cost_anomaly._add 单点收口；画像漂移经同一 _add 路径进入异动事件流。
    # False=关闭主动联动与扫表补录，只保留慢层采样。
    PROFILE_EVENT_SYNC_ENABLED: bool = True
    PROFILE_EVENT_SYNC_INTERVAL_S: int = 120    # event_tick 兜底扫表的节流间隔（秒）
    # 已知系统例行进程（阿里云安全/监控 agent 等）：不参与漂移判定与基线学习——
    # 它们周期性扫描会反复进出 top3，属系统噪声不算业务构成（快照里仍可见）
    PROFILE_SYSTEM_PROCESSES: str = '["AliSecCheck","AliYunDunMonitor","AliYunDun","AliSecGuard","aliyun-service","AssistDaemon","AliYunDunUpdate","wmiprvse","svchost","lsass","csrss","System","smss","services","wininit","taskhostw","MsMpEng","SecurityHealthService"]'
    # 构成/请求量变化的统一上下文裁决。显著上升=相对与绝对门槛同时满足；
    # 缺样本、过期、未映射、查询失败均为 unverified，不得当作正常或自动恢复。
    PROFILE_CONTEXT_ENABLED: bool = True
    PROFILE_CONTEXT_CURRENT_MINUTES: int = 180      # 当前对照窗（传统快/慢层）
    PROFILE_CONTEXT_BASELINE_MINUTES: int = 360     # 当前窗之前的同实例历史窗
    PROFILE_CONTEXT_STALE_MINUTES: int = 15         # 最新实时样本/全景采集时间最大年龄
    PROFILE_CONTEXT_MIN_SAMPLES: int = 3            # 实时当前/历史窗各自最少点数；全景历史最少天数
    PROFILE_CONTEXT_CPU_RELATIVE: float = 0.20
    PROFILE_CONTEXT_CPU_ABS_PP: float = 10.0        # CPU 使用率百分点
    PROFILE_CONTEXT_MEM_RELATIVE: float = 0.20
    PROFILE_CONTEXT_MEM_ABS_PP: float = 10.0        # 内存使用率百分点
    PROFILE_CONTEXT_RT_RELATIVE: float = 0.30
    PROFILE_CONTEXT_RT_ABS_MS: float = 100.0

    # ---------------- 对象全景画像：对象×多层×联动 ----------------
    # 总开关：False=不采集不检测，继续使用 profile_engine；对象画像写入独立表。
    OBJECT_PROFILE_V2: bool = True
    OBJECT_PROFILE_JOB_HOUR: int = 11
    OBJECT_PROFILE_JOB_MINUTE: int = 25
    OBJECT_PROFILE_BACKFILL_DAYS: int = 7   # 首次运行回填天数（学习基线）
    # 变化的绝对量下限：相对倍率会放大小体量对象的抖动，低于绝对量下限的偏离
    # 视为噪声，不建事件。
    OBJECT_PROFILE_MIN_DELTA: dict = {
        "cdn": 5.0, "slb": 5000, "app": 200, "host": 10.0,
        "db": 2.0, "change": 1.0, "cost": 20.0,
    }

    # 关机检测：关机是辅助机器的重要主动信号，core/aux 均需上报。
    HOST_WATCH_ENABLED: bool = True
    HOST_WATCH_INTERVAL_MIN: int = 10   # 命脉信号，10 分钟内必见（每次仅 2~3 个 region 调用）


def _overlay_db_over_empty_env(inst) -> None:
    """空环境变量注入视为未注入，让位 env_kv 库值，适配 compose `${VAR:-}` 写法。

    只处理拒写名单外、os.environ 显式空串且 env_kv 中有非空值的键，避免 compose
    重建时空注入覆盖 UI/库保存值。非空显式注入仍按 env > .env 优先级处理，并在与
    配置库双轨时启动对账留痕；密钥拒写名单键（INIT_ADMIN_PASSWORD"空=随机"等）一律不动。
    """
    try:
        import os
        from app.services.env_store import _db_allowed, _db_data

        dbv = _db_data()
    except Exception:
        return
    for k, v in dbv.items():
        if not v or not _db_allowed(k) or k not in Settings.model_fields:
            continue
        if os.environ.get(k) != "":   # None=未注入（pydantic 自然回落 .env）；非空=显式注入照旧
            continue
        try:
            cur = getattr(inst, k, None)
            if isinstance(cur, bool):
                nv = v.strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(cur, int) and not isinstance(cur, bool):
                nv = int(v)
            elif isinstance(cur, float):
                nv = float(v)
            elif isinstance(cur, (dict, list)):
                import json
                nv = json.loads(v)
            else:
                nv = v
            if cur != nv:
                setattr(inst, k, nv)
        except Exception:
            continue


class _HotSettings:
    """settings 热读代理，保证多 worker 使用一致的运行时配置。

    env_store.set_keys 将运行时可写配置（如 BOOTSTRAP_INIT_DONE/PRODUCT_CARDS/
    METRIC_SOURCES/SLS_*/INSPECTION_*）写入 .env。多 worker 下每个进程都有独立单例，
    因此代理在 .env mtime 变化时重建实例：任一 worker 写入后，其余 worker 下次读取
    即可看到新值。进程 env 注入的键仍按 env > .env 优先级盖回；空串注入视为未注入，
    让位 env_kv 库值，见 _overlay_db_over_empty_env。
    """

    __slots__ = ("_inst", "_mtime")

    def __init__(self) -> None:
        inst = Settings()
        _overlay_db_over_empty_env(inst)
        object.__setattr__(self, "_inst", inst)
        try:
            import os
            object.__setattr__(self, "_mtime", os.path.getmtime(".env"))
        except OSError:
            object.__setattr__(self, "_mtime", None)

    def _live(self) -> Settings:
        import os
        try:
            mt = os.path.getmtime(".env")
        except OSError:
            mt = None
        if mt != self._mtime:
            inst = Settings()
            _overlay_db_over_empty_env(inst)
            object.__setattr__(self, "_inst", inst)
            object.__setattr__(self, "_mtime", mt)
        return self._inst

    def __getattr__(self, name):
        return getattr(self._live(), name)

    def __setattr__(self, name, value):
        setattr(self._live(), name, value)


settings = _HotSettings()
