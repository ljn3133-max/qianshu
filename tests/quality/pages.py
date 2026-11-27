"""页面清单唯一来源。

新增路由必须同时进本清单，否则 gate.py 的"路由覆盖"检查直接红灯，
避免走查和视觉基线漏页。
"""

# 登录后的业务页面（浏览器全页面渲染 + 视觉基线共用）
PAGES = [
    "dashboard", "monitor", "anomalies", "logs", "alerts", "ops", "changes",
    "profiles", "cost", "expiry", "agent", "workflow-editor",
    "inspections", "topology", "synth", "k8s", "machines", "oncall",
    "integrations", "clouds", "diagnose", "agent/issues", "dba", "bootstrap", "settings",
]

# 公开页/特殊页：不参与登录态走查（登录页单独由"页面渲染"之外的口径人工保证）
PUBLIC_PAGES = ["login", "change-password"]

# 常规本机只读总门不打开会默认实时查询外部数据的页面；独立专项验收仍可显式覆盖。
# logs 首屏会请求真实 SLS Project/Logstore，不能用“只是 GET”推导出无外部调用。
LIVE_EXTERNAL_PAGES = {"logs"}

# 路由里存在但非独立页面的项
ROUTE_EXEMPT = {
    "links",        # redirect -> /topology?tab=links
    "ai",           # redirect -> /agent（AI 分析并入千枢智能体）
}

# 同一路由的页签深链（query 形式）：浏览器清扫逐页签渲染，不参与路由覆盖检查。
# 设置页 12→5 的整合契约：5 个新页签 + 2 个旧深链别名都必须真实可达，整合不能断链。
# 每条 = (page, query, 期望激活的顶层页签文案, 期望激活的二级页签文案)。
# 断言到激活页签，而不只看 URL 上还带着 ?tab= ——别名解析失败落到默认页时 URL 仍是原样，
# 只比 URL 会漏「深链没生效」。空串 = 该层无二级页签断言。
DEEP_LINKS = [
    ("settings", "?tab=policy", "监控与提醒", ""),
    ("settings", "?tab=notify", "通知与推送", "通知渠道"),
    ("settings", "?tab=models", "AI 与接入", "AI 模型"),
    ("settings", "?tab=org", "访问与审计", "API Token"),          # 用户/组织架构已下线，旧深链落到令牌
    ("settings", "?tab=platform", "平台维护", "系统参数"),
    ("settings", "?tab=channels", "通知与推送", "通知渠道"),     # 旧名 -> notify
    ("settings", "?tab=ownership", "访问与审计", "归属配置"),    # 旧名 -> org/ownership（拓扑「去归属」深链）
    # 拓扑按数据源分版：深链必须直达对应数据源视图，不能落回汇总被误读
    ("topology", "?tab=arch&source=aliyun", "资产架构图", ""),
    ("topology", "?tab=arch&source=private", "资产架构图", ""),
    # 腾讯产品 Ops 深链必须直达对应产品，不落回 ECS。
    ("ops", "?product=tc_cdn", "腾讯 CDN", ""),
    ("ops", "?product=tc_mongodb", "腾讯 MongoDB", ""),
    ("ops", "?product=tc_es", "腾讯 Elasticsearch", ""),
    ("ops", "?product=tc_postgres", "腾讯 PostgreSQL", ""),
]
