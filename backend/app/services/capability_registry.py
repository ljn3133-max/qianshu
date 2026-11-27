"""能力-数据依赖矩阵（千枢底座体检的机器表，v3 方案核心）。

产品原则：能力入口由数据面就绪度动态开关——缺数据的能力不显示（不是置灰），
每个缺口必须同时说清"缺了会怎样"和"补了能得到什么"（客户为能力补数据，
不为清单补数据）。本表是 UI 与报告共用的单一事实源。
"""

# 五个数据面（探测/体检的单位）
FACES = ("assets", "metrics", "logs", "cost", "changes")
FACE_LABELS = {
    "assets": "资产",
    "metrics": "指标",
    "logs": "日志",
    "cost": "账单",
    "changes": "变更",
}

# 能力 -> 依赖的数据面（缺任一则该能力不可用）
CAPABILITIES = [
    {"key": "inspect", "name": "巡检与日报", "requires": ("assets", "metrics")},
    {"key": "alert", "name": "告警与基线检测", "requires": ("assets", "metrics")},
    {"key": "host_diag", "name": "主机资源诊断", "requires": ("assets", "metrics")},
    {"key": "fault_diag", "name": "故障诊断（链路下钻）", "requires": ("assets", "metrics", "logs")},
    {"key": "profile", "name": "行为画像与漂移检测", "requires": ("assets", "metrics", "logs")},
    {"key": "topology", "name": "链路拓扑", "requires": ("assets", "logs")},
    {"key": "cost", "name": "成本分析与账单答疑", "requires": ("assets", "cost")},
    {"key": "change", "name": "变更时间线（排障第一问）", "requires": ("assets", "changes")},
]

# 缺口话术（说人话：缺了会怎样 / 怎么补 / 补了能得到什么）
GAP_HINTS = {
    "assets": {
        "impact": "平台不知道你有什么——巡检/告警/成本全部无从谈起",
        "fix": "在安装向导填一副只读 AK（ECS/RDS/SLB 等列表权限）",
        "unlock": "全部能力的前置",
    },
    "metrics": {
        "impact": "只能看资产清单，看不到水位——告警、巡检、资源诊断都开不了",
        "fix": "开通云监控只读权限（CMS）",
        "unlock": "巡检/告警/资源诊断/画像",
    },
    "logs": {
        "impact": "诊断只能到'哪台机器'，到不了'哪个进程在干什么'；链路拓扑、画像的日志层缺位",
        "fix": "到 云账号管理 添加/启用只读阿里云账号（平台自动复用其凭证查 SLS）；"
               "需要 SLB/应用日志关联分析再到 安装向导 选择日志目标；"
               "ELK/Loki/腾讯云 CLS 到 集成中心-数据源 登记",
        "unlock": "故障下钻到应用层、链路拓扑、行为画像",
    },
    "cost": {
        "impact": "成本侧能力不适用（私有环境无账单时如实隐藏，不硬凑）",
        "fix": "开通费用中心只读权限（BSS OpenAPI）",
        "unlock": "成本分析/账单答疑/资源包池化追踪",
    },
    "changes": {
        "impact": "排障时'最近改了什么'要人工回忆",
        "fix": "在云账号管理选择变更审计源并开通对应只读权限（如 ActionTrail）",
        "unlock": "变更时间线，排障第一问自动化",
    },
}


def capability_readiness(ready_faces: set) -> list:
    """[{name, ready, missing}]——ready_faces 为已就绪数据面集合。"""
    out = []
    for c in CAPABILITIES:
        missing = [f for f in c["requires"] if f not in ready_faces]
        out.append({"name": c["name"], "ready": not missing, "missing": missing})
    return out


def gap_suggestions(ready_faces: set) -> list:
    """缺口建议（按解锁能力数排序——平台替客户排优先级，不讓客户自己排）。"""
    gaps = []
    for face in FACES:
        if face in ready_faces:
            continue
        blocked = [c["name"] for c in CAPABILITIES if face in c["requires"]]
        hint = GAP_HINTS[face]
        gaps.append({
            "face": face,
            "label": FACE_LABELS[face],
            "impact": hint["impact"],
            "fix": hint["fix"],
            "unlock": hint["unlock"],
            "blocked_capabilities": blocked,
        })
    gaps.sort(key=lambda g: -len(g["blocked_capabilities"]))
    return gaps
