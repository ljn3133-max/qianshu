"""全平台数据覆盖体检，并为每项缺口给出影响、补全方法和真实页面入口。

六面真实探测（只读、不装、零写库；探测失败如实记 detail 不猜）：
  logs     应用日志      与日志查询页同源（SLS 隐式凭证源 + 集成中心日志源），
                         不再只认 SLS_APP_LOG/LOG_SOURCES 而误报"未接入"
  metrics  指标          VM/Prom label values + Zabbix host.get 真实探测；分母只含平台
                         真有采集通道的资产（云监控 CMS / vSphere / Agent / K8s），
                         无通道产品线列 detail，不制造无法完成的建议
  cloud    云账号/账单   实例级账单↔资产对账 + 账单采集健康；**不做账号级错位**（付款账号
                         可只有账单、vCenter 无账单、合并出账都属正常）；已释放实例按
                         资产历史 Deleted 行识别，计费维度行（OSS/NAS/CDN 等）不参与
  alerts   告警规则      只对"有阈值规则指标通道"的产品线计缺口；无通道产品线如实列在
                         detail，不制造无法完成的建议
  graph    画像/拓扑/链路 未生成（画像/拓扑/链路）+ 未确认（漂移待确认的量）
  domains  证书/域名/到期 缺登记（六源证据 vs 域名登记表）+ 缺证书到期证据

输出：{score: 0-100, gaps: [{area, gap, impact, suggestion, entry}...], areas: [...],
      generated_at}——gaps 按面覆盖率升序（越缺越前），总览卡取前 3 条。
打分：六面等权，ratio=各面真实覆盖率（见各 _check_* 注释）；score=round(mean*100)。

工具注册：copilot/tools_read.data_completeness_check（只读工具）；自定义工作流步骤、
onboard_init 的 readiness 步骤可直接 tools_read.execute(db, "data_completeness_check", {})。
只读纪律：本模块只 SELECT + 只读探测（httpx GET / JSON-RPC 查询），零写库零变更。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

logger = logging.getLogger("qianshu.data_advisor")

# 面 -> 展示名（areas/gap.area 共用稳定键；文案中文=与全站后端口径一致）
AREA_LABELS = {
    "logs": "应用日志",
    "metrics": "指标",
    "cloud": "云账号/账单",
    "alerts": "告警规则",
    "graph": "画像/拓扑/链路",
    "domains": "证书/域名/到期",
}

# 缺口跳转入口必须指向前端真实路由，?tab= 由对应页消费：
# SLS 凭证来自云账号管理；资产同步在运维管理；SLS 目标配置在接入配置页。
# 指标缺口按通道进入 CMS 权限、VMware 重采、Agent 安装或指标源登记页面。
ENTRY = {
    "logs": "/clouds",
    "metrics": "/integrations?tab=data",
    "metrics_cloud": "/clouds",
    "metrics_vmware": "/monitor",
    "metrics_agent": "/bootstrap",
    "cloud": "/ops",                    # 有账单无资产 -> 到运维管理按产品同步
    "cloud_accounts": "/clouds",        # 账单断供/无数据 -> 核对账号权限
    "alerts": "/alerts?tab=rules",
    "profiles": "/profiles",
    "topology": "/topology",
    "links": "/bootstrap",              # SLB 访问日志目标配置（SLS_SLB_TARGETS）
    "expiry": "/expiry",                # 到到期总账页「登记外部域名」
    "expiry_domains": "/expiry?tab=domains",
    "synth": "/synth",                  # 拨测新增 HTTP 目标，自动采集证书到期
}
# 面 -> 入口（探测失败兜底也落对面的页）
AREA_ENTRY = {
    "logs": ENTRY["logs"], "metrics": ENTRY["metrics"], "cloud": ENTRY["cloud"],
    "alerts": ENTRY["alerts"], "graph": ENTRY["topology"], "domains": ENTRY["expiry"],
}

# 无产品资产台账只有「登记/证书订单」两类证据的域名也算证据，但"缺登记"只针对
# 业务证据面（link/cdn/profile/synth/diagnosis/biz）——登记表就是收口处。
_REGISTRAR_SOURCES = ("registrar", "cert")
_BIZ_EVIDENCE = ("link", "cdn", "profile", "synth", "diagnosis", "biz")

# 纯登记类资产（域名/证书订单）：由到期预警覆盖，不以"告警规则覆盖"衡量
_NON_ALERT_PRODUCTS = ("domain", "ssl_cert")

# 有内置检测、不走阈值规则的产品线（detail 如实说明，不算缺口）。
# CDN 的流量构成/域名漂移由 profile_engine/cdn_traffic 内置检测产出异动。
_BUILTIN_ALERT_COVERAGE = {
    "cdn": "CDN 流量/域名构成漂移内置检测",
}

# 账单产品码 -> 资产产品值。只收「账单 instance_id 就是资源 ID」的产品线；
# OSS/CDN/NAS/EIP 等的账单行是存储类型、区域、带宽阶梯等计费维度，不能拿来
# 和 assets.instance_id 直接比较，否则会把计费维度误报为资产缺口。
_COST_PRODUCT_TO_ASSET = {
    "ecs": "ecs", "rds": "rds", "kvstore": "redis", "redisa": "redis", "slb": "slb",
    "cvm": "cvm", "cdb": "cdb", "crs": "crs", "clb": "clb",
}
# 已实现费用采集的厂商由 provider 能力位唯一决定；
# vCenter/占位厂商没有账单能力（不是"缺账单"，是"不适用"）。
def _billing_vendors() -> tuple:
    from app.services.cloud.provider import vendors_with
    return vendors_with("billing")
# 已关机/挂起的实例不产生新指标属正常，不进"指标未覆盖"分母（各厂商大小写不一）。
_INACTIVE_STATUSES = {"stopped", "suspended", "terminated", "recycled", "offline", "isolated"}


# ---------------- 工具入口 ----------------

def check(db: Session) -> dict:
    """全平台数据覆盖体检（只读）。返回 {score, gaps, areas, generated_at}。"""
    areas: list = []
    gaps: list = []
    for fn in (_check_logs, _check_metrics, _check_cloud,
               _check_alerts, _check_graph, _check_domains):
        try:
            area, area_gaps = fn(db)
        except Exception as e:  # noqa: BLE001  单面探测失败不拉垮整体，如实记
            logger.warning("数据完整度体检 %s 面异常", fn.__name__, exc_info=True)
            key = fn.__name__.replace("_check_", "")
            area = {"area": key, "label": AREA_LABELS.get(key, key), "ratio": 0.0,
                    "probe_failed": True,   # 探测异常≠覆盖率 0%，不参与均分（见下）
                    "detail": f"探测失败：{type(e).__name__}: {str(e)[:120]}"}
            area_gaps = [{
                "area": key,
                "gap": f"{AREA_LABELS.get(key, key)}面探测失败",
                "impact": "无法确认该面数据完整性（探测异常，非数据缺失）",
                "suggestion": "稍后重试；持续失败看后端日志定位探测器报错",
                "entry": AREA_ENTRY.get(key, "/integrations"),
            }]
        areas.append(area)
        gaps.extend(area_gaps)
    # 越缺越前（面覆盖率升序），面内保持探测顺序；总览卡取前 3 条
    order = {a["area"]: a.get("ratio", 0.0) for a in areas}
    gaps.sort(key=lambda g: order.get(g["area"], 0.0))
    # 探测异常的面按 0% 计入均分，会把"没查到"说成"覆盖率 0%"（同一类误导：
    # 空数据≠0）。异常面单列 degraded，评分只在真实探测到的面上取均值。
    valid = [a for a in areas if not a.get("probe_failed")]
    degraded = len(areas) - len(valid)
    score = round(sum(a.get("ratio", 0.0) for a in valid) / len(valid) * 100) if valid else 0
    return {
        "score": max(0, min(100, score)),
        "degraded": degraded,
        "gaps": gaps,
        "areas": areas,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _gap(area: str, gap: str, impact: str, suggestion: str, entry: str) -> dict:
    return {"area": area, "gap": gap, "impact": impact,
            "suggestion": suggestion, "entry": entry}


def _sample(items: list, n: int = 5) -> str:
    s = "、".join(str(x) for x in items[:n])
    return s + ("…" if len(items) > n else "")


def _monitored_account_ids(db: Session):
    """启用中的云账号 id（含 0=未归属）；None=没有账号台账时不过滤（旧库兼容）。

    enabled=false 表示停止该账号的采集与监测，其资产不再计入就绪度缺口；
    停用账号的历史资产仍保留，不应因此产生"无规则"等假缺口。
    """
    from app.models.cloud_account import CloudAccount

    from app.services.cloud.provider import capabilities_of

    rows = db.query(CloudAccount.id, CloudAccount.enabled,
                    CloudAccount.vendor).all()
    if not rows:
        return None
    # 只登记/仅探活厂商（stub/KVM）没有可监测资产，不进分母；自建(agent)保留
    return {acc_id for acc_id, enabled, vendor in rows
            if enabled and (capabilities_of(vendor or "").sync
                            or capabilities_of(vendor or "").agent)} | {0}


def _scope_assets(query, scope):
    """给按资产维度的查询加"启用账号"过滤；scope=None 时保持原查询。"""
    from app.models.asset import Asset

    if scope is None:
        return query
    return query.filter(Asset.account_id.in_(scope))


def _norm_instance_id(value: str) -> str:
    """账单/资产实例 ID 归一：部分计费行的实例是 `资源ID;维度`（如 Redis 备份、
    NAS 容量），只取分号前的主资源 ID 参与核对。"""
    return str(value or "").split(";", 1)[0].strip().lower()


# ---------------- logs 应用日志 ----------------

def _check_logs(db: Session) -> tuple:
    """与日志查询页同源判定：能查的日志源才算覆盖。

    日志查询层会在阿里云凭证可用时合成隐式 SLS 源，因此不能只检查
    SLS_APP_LOG/LOG_SOURCES。判据统一走 log_query.source_entries（status=ready），
    避免日志页可查询而总览仍显示"未接入日志"。
    """
    from app.services.log_query import source_entries

    entries = source_entries(db)
    ready = [e for e in entries if e.get("status") == "ready"]
    pending = [e for e in entries if e.get("status") == "pending"]
    no_cred = [e for e in entries if e.get("status") == "no_credential"]
    ready_names = "、".join(e.get("name") or e.get("type") or "?" for e in ready)
    detail = f"可查询日志源 {len(ready)} 个" + (f"（{ready_names}）" if ready else "")
    if pending:
        detail += f"；已登记但查询未启用 {len(pending)} 个"
    if no_cred:
        detail += f"；缺凭证 {len(no_cred)} 个"
    area = {"area": "logs", "label": AREA_LABELS["logs"],
            "ratio": 1.0 if ready else 0.0, "detail": detail}
    if ready:
        return area, []
    hint = "（已有日志源登记，但缺少可用凭证）" if no_cred else ""
    return area, [_gap(
        "logs",
        f"无可查询的日志源{hint}",
        "诊断只能到『哪台机器』，到不了『哪个进程/哪行错误』；链路拓扑与行为画像的日志层缺位",
        "阿里云 SLS 无需在集成中心重复登记：到 云账号管理 添加/启用只读阿里云账号，"
        "平台自动复用其凭证查询 SLS；如需 ELK/Loki/腾讯云 CLS，再到 集成中心-数据源 登记",
        ENTRY["logs"],
    )]


# ---------------- metrics 指标 ----------------

def _vm_instance_ids(url: str, timeout: float = 4.0) -> set:
    """VM/Prom 兼容 label values 真实探测：instance_id 标签值=有指标的实例集合（只读 GET）。"""
    import httpx

    r = httpx.get(f"{str(url).rstrip('/')}/api/v1/label/instance_id/values",
                  timeout=timeout, verify=False, follow_redirects=True)
    r.raise_for_status()
    data = r.json().get("data") or []
    return {str(x).strip().lower() for x in data if str(x).strip()}


def _zabbix_host_names(src: dict) -> set:
    """Zabbix host.get 真实探测（只读 JSON-RPC）：受监控主机 host/name 小写集合。"""
    from app.services import zabbix as zb

    url = src.get("url") or ""
    cred = zb._auth_kwargs(src)          # 同 services 层复用（dashboard↔expiry 同款惯例）
    verify = bool(src.get("verify_ssl"))
    token = ""
    auth = ""
    if cred["token"]:
        token = cred["token"]
    elif cred["user"] and cred["password"]:
        auth = zb.login(url, cred["user"], cred["password"], verify=verify)
    else:
        raise RuntimeError("未配置认证（API Token 或用户名/密码）")
    try:
        rows = zb.rpc(url, "host.get", {"output": ["host", "name"]},
                      auth=auth, token=token, timeout=6, verify=verify)
    finally:
        if auth:
            zb.logout(url, auth, verify=verify)
    out = set()
    for h in rows or []:
        for k in ("host", "name"):
            v = str(h.get(k) or "").strip().lower()
            if v:
                out.add(v)
    return out


def _check_metrics(db: Session) -> tuple:
    """指标覆盖：分母只含平台真有采集通道的资产，缺口建议按通道分线。

    - 分母包含 VMware（vSphere 写 vmware_vm_*/vmware_host_*）与 Agent host
      （aliyun_ecs_* + product=host），已采集资产必须进入覆盖率计算。
    - 没有指标采集通道的产品线只在 detail 列明，不进分母也不报缺口；域名/证书不适用指标。
    - 已关机/挂起实例不出新指标属正常，排除并单独计数；缺口按 CMS / VMware /
      Agent / K8s 四条通道分别给可执行建议。
    覆盖判定使用 VM/Prom 的 instance_id 标签值、Zabbix 主机名/IP，命中台账键即算。
    """
    from app.collector.cms import METRIC_CONFIG, VMWARE_METRIC_CONFIG
    from app.core.config import settings
    from app.models.asset import Asset
    from app.models.machine import K8sMachine
    from app.services import datasource

    channel_products = set(METRIC_CONFIG) | set(VMWARE_METRIC_CONFIG) | {"host"}
    scope = _monitored_account_ids(db)
    q = _scope_assets(db.query(Asset).filter(Asset.status != "Deleted"), scope)

    ents: list = []
    unsupported: dict = {}
    inactive = 0
    for product, iid, name, status in q.with_entities(
            Asset.product, Asset.instance_id, Asset.name, Asset.status).all():
        prod = str(product or "").lower()
        if prod in channel_products:
            if str(status or "").lower() in _INACTIVE_STATUSES:
                inactive += 1          # 关机/挂起不出新指标属正常，不算缺口
                continue
            keys = {str(iid or "").strip().lower()}
            if name:
                keys.add(str(name).strip().lower())
            channel = ("cms" if prod in METRIC_CONFIG else
                       "vmware" if prod in VMWARE_METRIC_CONFIG else "agent")
            ents.append({"label": name or iid, "keys": {k for k in keys if k},
                         "channel": channel, "product": prod})
        elif prod not in _NON_ALERT_PRODUCTS:
            unsupported[prod] = unsupported.get(prod, 0) + 1
    for m in db.query(K8sMachine).all():
        keys = {str(m.ip or "").strip().lower(), str(m.host_name or "").strip().lower()}
        ents.append({"label": m.host_name or m.ip, "keys": {k for k in keys if k},
                     "channel": "k8s", "product": "k8s"})

    covered: set = set()
    probes: list = []
    reachable = False
    for src in datasource.metric_sources():
        typ = (src.get("type") or "").lower()
        name = src.get("name") or typ
        try:
            if typ in ("vm", "prometheus", "victoriametrics"):
                url = src.get("url") or settings.VM_URL
                ids = _vm_instance_ids(url)
                covered |= ids
                reachable = True
                probes.append(f"{name}: instance_id {len(ids)} 个")
            elif typ == "zabbix":
                hosts = _zabbix_host_names(src)
                covered |= hosts
                reachable = True
                probes.append(f"{name}: 主机 {len(hosts)} 个")
            else:
                probes.append(f"{name}({typ}): 无实例级覆盖探测器，不计覆盖")
        except Exception as e:  # noqa: BLE001  单源探测失败如实记，继续探其他源
            probes.append(f"{name}: 探测失败（{type(e).__name__}: {str(e)[:80]}）")

    missing = [e for e in ents if not (e["keys"] & covered)]
    # 没有可评估资产时覆盖率视为满（纯 OSS/CDN 等无指标产品线不是"指标缺失"）
    ratio = ((len(ents) - len(missing)) / len(ents)) if ents else 1.0
    by_ch: dict = {}
    for e in ents:
        by_ch[e["channel"]] = by_ch.get(e["channel"], 0) + 1
    detail = (f"台账 {len(ents)} 台（云监控 {by_ch.get('cms', 0)} / VMware "
              f"{by_ch.get('vmware', 0)} / Agent {by_ch.get('agent', 0)} / "
              f"K8s {by_ch.get('k8s', 0)}）/ 覆盖 {len(ents) - len(missing)} 台")
    if inactive:
        detail += f"；已排除关机/挂起 {inactive} 台（不出新指标属正常）"
    detail += "；" + "；".join(probes)
    if unsupported:
        detail += "；无指标采集通道（不参与评分）：" + "、".join(
            f"{p} {n}" for p, n in sorted(unsupported.items()))
    area = {"area": "metrics", "label": AREA_LABELS["metrics"],
            "ratio": round(ratio, 3), "detail": detail}

    gaps: list = []
    if not reachable and ents:
        gaps.append(_gap(
            "metrics",
            "指标源未接通（VM/Prometheus/Zabbix 均未配置或探测失败）",
            "看不到 CPU/内存/磁盘水位——巡检、告警、资源诊断都开不了",
            "检查内置 VictoriaMetrics 容器（VM_URL 配置），或在 集成中心-数据源 登记 "
            "Prometheus/Zabbix 指标源；云上资产可开云监控(CMS)只读由平台自动采集",
            ENTRY["metrics"],
        ))
    elif missing:
        grouped: dict = {}
        for e in missing:
            grouped.setdefault(e["channel"], []).append(e)
        specs = {
            "cms": ("{n} 台云监控产品资产无指标（{s}）",
                    "ECS/RDS/Redis/SLB 没有水位曲线与告警依据，资源诊断会跳过它们",
                    "到 云账号管理 确认账号已启用、AK 有云监控(CMS)只读权限，"
                    "并核对实例地域与账号归属（平台按产品自动采集）",
                    ENTRY["metrics_cloud"]),
            "vmware": ("{n} 台 VMware 资产无指标（{s}）",
                       "虚拟机/ESXi 主机没有 CPU/内存曲线，VMware 告警与容量判断缺依据",
                       "到 监控仪表盘 VMware 页签点『采集当前产品』重试；仍 0 样本时"
                       "按页面提示检查 vCenter 统计级别与只读角色（System.View 需传播）",
                       ENTRY["metrics_vmware"]),
            "agent": ("{n} 台 Agent 主机无指标（{s}）",
                      "这些非云主机没有水位曲线，巡检与资源诊断会跳过它们",
                      "到 安装向导 生成 Agent 安装命令并下发到机器，确认能上报到内置 "
                      "VictoriaMetrics（网络/Token 见页面说明）",
                      ENTRY["metrics_agent"]),
            "k8s": ("{n} 台 K8s 节点无指标（{s}）",
                    "节点水位不可见，K8s 相关巡检与容量判断缺依据",
                    "到 集成中心-数据源 登记 Prometheus/Zabbix，或给节点安装采集 Agent",
                    ENTRY["metrics"]),
        }
        for channel in ("cms", "vmware", "agent", "k8s"):
            items = grouped.get(channel) or []
            if not items:
                continue
            title_t, impact, sug, entry = specs[channel]
            gaps.append(_gap("metrics",
                             title_t.format(n=len(items),
                                            s=_sample([e["label"] for e in items])),
                             impact, sug, entry))
    return area, gaps


# ---------------- cloud 云账号/账单 ----------------

def _check_cloud(db: Session) -> tuple:
    """云账号/账单：实例级对账 + 账单采集健康（只读）。

    - 不做账号级「资产↔账单」错位判定：付款/管理账号可以只有账单，成员账号可以只有
      资源，vCenter/私有化账号没有云账单，这些都不是数据缺口。
    - 实例级只核对 _COST_PRODUCT_TO_ASSET 的产品线（账单 instance_id 就是资源 ID），
      分号后缀先归一；缺失实例区分"已释放"与"从未同步"，资产历史已有 Deleted 行时
      属于已释放，不报缺口。
    - 账单健康按最后账单日判断：有账单史却停更 ≥2 天才报；计费账号新接入给 2 天
      T+1 宽限；没有计费类账号时账单面不适用，不扣分。
    """
    from app.models.asset import Asset
    from app.models.cloud_account import CloudAccount
    from app.models.cost_daily import CostDaily

    today = date.today()
    scope = _monitored_account_ids(db)

    asset_q = _scope_assets(db.query(Asset).filter(Asset.status != "Deleted"), scope)
    n_assets = asset_q.count()
    asset_acc = dict(asset_q.with_entities(Asset.account_id, func.count(Asset.id))
                     .group_by(Asset.account_id).all())
    # 已知实例含 Deleted 历史行：用于区分"已释放"与"从未同步"（软删除保留历史）
    known_pairs = {
        (str(product or "").lower(), _norm_instance_id(iid))
        for product, iid in db.query(Asset.product, Asset.instance_id).all()
        if _norm_instance_id(iid)
    }
    active_pairs = {
        (str(product or "").lower(), _norm_instance_id(iid))
        for product, iid in db.query(Asset.product, Asset.instance_id)
        .filter(Asset.status != "Deleted").all()
        if _norm_instance_id(iid)
    }

    accounts = db.query(CloudAccount.id, CloudAccount.vendor, CloudAccount.enabled,
                        CloudAccount.created_at).all()
    if accounts:
        bill_accounts = {aid for aid, vendor, enabled, _c in accounts
                         if enabled and (vendor or "") in _billing_vendors()}
        bill_expected = bool(bill_accounts)   # 纯 vCenter/私有化：账单面不适用
        oldest = min((c for aid, _v, _e, c in accounts
                      if aid in bill_accounts and c), default=None)
    else:
        bill_accounts = None                  # 旧库无账号台账：沿用账单全量口径
        bill_expected = n_assets > 0
        oldest = None

    cost_q = db.query(CostDaily).filter(CostDaily.stat_date >= today - timedelta(days=30))
    if bill_accounts is not None:
        cost_q = cost_q.filter(CostDaily.account_id.in_(bill_accounts))
    cost_acc = dict(cost_q.with_entities(CostDaily.account_id, func.count(CostDaily.id))
                    .group_by(CostDaily.account_id).all())
    cost_total = sum(cost_acc.values())
    last_bill = cost_q.with_entities(func.max(CostDaily.stat_date)).scalar()

    cost_rows = cost_q.with_entities(CostDaily.product_code, CostDaily.instance_id).distinct().all()
    # 缺口只看近 7 天仍在计费的实例：更早的孤儿多为已释放，避免长期假挂账
    recent_rows = cost_q.filter(CostDaily.stat_date >= today - timedelta(days=7)) \
        .with_entities(CostDaily.product_code, CostDaily.instance_id).distinct().all()
    recent_pairs = {
        (code, iid) for code, iid in recent_rows
        if code in _COST_PRODUCT_TO_ASSET and _norm_instance_id(iid)
    }
    orphan_pairs, released_pairs = [], []
    for code, iid in sorted(recent_pairs):
        pair = (_COST_PRODUCT_TO_ASSET[code], _norm_instance_id(iid))
        if pair in active_pairs:
            continue
        if pair in known_pairs:
            released_pairs.append((code, iid))   # 资产历史已标 Deleted：已释放，正常
        else:
            orphan_pairs.append((code, iid))
    other_codes = {code for code, _iid in cost_rows
                   if code and code not in _COST_PRODUCT_TO_ASSET}

    # 账单健康：新接入给的宽限期内不报；有史停更 ≥2 天才是真问题
    issue = ""
    stale_days = (today - last_bill).days if last_bill else None
    if bill_expected:
        if not cost_total:
            age = (datetime.now() - oldest).days if oldest else None
            if bill_accounts is None or age is None or age >= 2:
                issue = "missing"
        elif stale_days is not None and stale_days >= 2:
            issue = "stale"

    checks = [
        0.0 if issue else 1.0,
        (1.0 - min(1.0, len(orphan_pairs) / max(1, len(recent_pairs))))
        if recent_pairs else 1.0,
    ]
    detail = (f"资产 {n_assets} 个（账号 {len(asset_acc)} 个）；"
              f"近 30 天账单 {cost_total} 行（账号 {len(cost_acc)} 个）")
    if last_bill:
        detail += f"，最后 {last_bill.isoformat()}"
    detail += (f"；近 7 天可核对实例级资源 {len(recent_pairs)} 个，"
               f"{len(orphan_pairs)} 个不在资产库")
    if released_pairs:
        detail += f"（另有 {len(released_pairs)} 个已释放，资产历史已标 Deleted，不计缺口）"
    if not bill_expected:
        detail += "；未配置计费类云账号（aliyun/tencent），账单核对不适用"
    elif bill_accounts is not None and not cost_total and not issue:
        detail += "；计费账号新接入，尚未到首个账单周期（T+1）"
    if other_codes:
        detail += f"；另有 {len(other_codes)} 类账单产品为计费维度或未接资产采集，不参与实例核对"
    detail += "；账号级资产/账单不构成一一对应（付款账号、vCenter、合并出账），不做错位判定"
    area = {"area": "cloud", "label": AREA_LABELS["cloud"],
            "ratio": round(sum(checks) / len(checks), 3), "detail": detail}

    gaps: list = []
    if issue == "stale":
        gaps.append(_gap(
            "cloud",
            f"账单数据断供 {stale_days} 天（最后 {last_bill.isoformat() if last_bill else '无'}）",
            "总览昨日费用、成本异动、资源包耗尽预测、到期总账都拿不到新数据",
            "到 云账号管理 检查账号是否启用、连通性测试是否通过、AK 是否有费用中心"
            "只读权限（BSS OpenAPI）；恢复后平台自动补采 T-6 起账单",
            ENTRY["cloud_accounts"],
        ))
    elif issue == "missing":
        if bill_accounts is None:
            bill_assets = sum(asset_acc.values())   # 无账号台账旧库：原有资产即应有账单
            gaps.append(_gap(
                "cloud",
                f"有资产无账单：{bill_assets} 个资产在库但近 30 天无一行账单",
                "资产/账单对账、成本分析与资源包追踪都不可用（私有化无账单环境可忽略此项）",
                "云账号管理核对费用中心只读权限（BSS OpenAPI）后等下一轮账单采集；"
                "私有化环境无需处理",
                ENTRY["cloud_accounts"],
            ))
        else:
            gaps.append(_gap(
                "cloud",
                "计费账号已接入但近 30 天没有账单数据",
                "账单按 T+1 出账；一直为空说明费用中心权限或账号状态有问题，"
                "成本分析与资源包追踪不可用",
                "到 云账号管理 对启用的阿里云/腾讯云账号做一次连通性测试，确认 AK 有"
                "费用中心只读权限（阿里云 BSS OpenAPI / 腾讯云账单元数据只读）",
                ENTRY["cloud_accounts"],
            ))
    if orphan_pairs:
        sample = [f"{code}:{iid}" for code, iid in orphan_pairs]
        gaps.append(_gap(
            "cloud",
            f"有账单无资产：近 7 天仍有计费的 {len(orphan_pairs)} 个实例级资源不在资产库（{_sample(sample)}）",
            "这些花费对不上资产画像/到期/告警——要么对应产品线没同步，要么实例刚释放",
            "到 运维管理 选择对应产品页签点『同步云资产』；确认已释放的资源会随"
            "账单窗口（7 天）自然滑出，不会长期挂账",
            ENTRY["cloud"],
        ))
    return area, gaps


# ---------------- alerts 告警规则 ----------------

def _check_alerts(db: Session) -> tuple:
    """有指标通道、可建阈值规则但缺规则的产品线（账号维度匹配）。

    适用口径：
    - 只有 ALERT_METRIC_CONFIG 里的产品线能建阈值规则；ACK/Oss/ES/WAF 等没有
      指标采集通道，列进 detail 说明，但不能作为"缺口"要求用户去建不存在的规则；
    - 域名/证书由到期预警覆盖，CDN 由流量构成漂移内置检测覆盖，不参与阈值规则评分；
    - 覆盖判定按账号：产品有资产但该账号没有规则（或全局规则 account_id=0）才算缺。
    """
    from app.collector.cms import ALERT_METRIC_CONFIG
    from app.models.alert_rule import AlertRule
    from app.models.asset import Asset

    scope = _monitored_account_ids(db)
    rows = _scope_assets(db.query(Asset).filter(Asset.status != "Deleted"), scope) \
        .with_entities(Asset.product, Asset.account_id).all()
    prod_accs: dict = {}
    for product, account_id in rows:
        if product and product not in _NON_ALERT_PRODUCTS:
            prod_accs.setdefault(product, set()).add(account_id or 0)
    products = sorted(prod_accs)

    rules = db.query(AlertRule).filter(AlertRule.enabled.is_(True)).all()
    rules_by_product: dict = {}
    for r in rules:
        if r.product:
            rules_by_product.setdefault(r.product, set()).add(r.account_id or 0)

    def _covered(product: str) -> bool:
        rule_accounts = rules_by_product.get(product, set())
        return all(0 in rule_accounts or acc in rule_accounts
                   for acc in prod_accs.get(product, set()))

    rule_products = [p for p in products if p in ALERT_METRIC_CONFIG]
    missing = [p for p in rule_products if not _covered(p)]
    no_channel = [p for p in products
                  if p not in ALERT_METRIC_CONFIG and p not in _BUILTIN_ALERT_COVERAGE]
    builtin = [p for p in products if p in _BUILTIN_ALERT_COVERAGE]
    n_sls = sum(1 for r in rules if r.source == "sls")
    ratio = (len(rule_products) - len(missing)) / len(rule_products) if rule_products else 1.0

    detail = (f"阈值规则适用产品线 {len(rule_products)} 类"
              f"（{'、'.join(rule_products) or '无'}）；启用规则 {len(rules)} 条"
              f"（metric {len(rules) - n_sls} / sls {n_sls}），覆盖 "
              f"{len(rule_products) - len(missing)} 类")
    if missing:
        detail += f"；缺口：{'、'.join(missing)}"
    if no_channel:
        detail += f"；无阈值规则指标通道（不参与评分）：{'、'.join(no_channel)}"
    if builtin:
        detail += "；内置检测覆盖：" + "、".join(
            f"{p}（{_BUILTIN_ALERT_COVERAGE[p]}）" for p in builtin)
    area = {"area": "alerts", "label": AREA_LABELS["alerts"],
            "ratio": round(ratio, 3), "detail": detail}
    if not missing:
        return area, []
    title = (f"{len(missing)} 类有指标通道的资产无告警规则覆盖：{'、'.join(missing)}"
             if rules else
             f"告警规则为空——阈值规则适用的 {len(missing)} 类资产无覆盖：{'、'.join(missing)}")
    return area, [_gap(
        "alerts",
        title,
        "这些资源宕机/水位越限时不会有人被告警，只能靠人巡",
        "到 告警中心-规则页点『自动创建规则』：平台按账号×产品找缺口，用近 7 天真实"
        "样本建议阈值，默认勾选有数据支撑的候选，勾选后一次创建；仅对有指标通道的产品线适用",
        ENTRY["alerts"],
    )]


# ---------------- graph 画像/拓扑/链路 ----------------

def _check_graph(db: Session) -> tuple:
    """未生成（画像/拓扑/链路）+ 未确认（漂移待确认的量）。ratio=四子项均值。

    待确认异动按 CostAnomaly.account_id 过滤启用账号；画像/拓扑/链路表没有账号列
    （链路是按域名聚合的跨账号事实），保持全局口径，不臆造归属。
    """
    from app.models.cost_anomaly import CostAnomaly
    from app.models.link_edge import LinkEdge
    from app.models.object_profile import ObjectProfile
    from app.models.topology_scan import TopologyScan

    scope = _monitored_account_ids(db)
    n_prof = db.query(func.count(ObjectProfile.id)).scalar() or 0
    n_learned = (db.query(func.count(ObjectProfile.id))
                 .filter(ObjectProfile.learned.is_(True)).scalar() or 0)
    topo = db.query(TopologyScan).order_by(TopologyScan.scanned_at.desc()).first()
    topo_age = (datetime.now() - topo.scanned_at).days if topo and topo.scanned_at else None
    n_links = db.query(func.count(LinkEdge.id)).scalar() or 0
    unconf_q = db.query(func.count(CostAnomaly.id)).filter(
        CostAnomaly.ack_status == "open",
        CostAnomaly.type.in_(("profile_drift", "chain_drift")))
    if scope is not None:
        unconf_q = unconf_q.filter(CostAnomaly.account_id.in_(scope))
    n_unconf = unconf_q.scalar() or 0

    sub = [
        (n_learned / n_prof) if n_prof else 0.0,
        1.0 if (topo_age is not None and topo_age <= 7) else 0.0,
        1.0 if n_links else 0.0,
        1.0 if n_unconf == 0 else max(0.0, 1.0 - n_unconf / 10),   # 待确认每 10 条扣尽
    ]
    area = {"area": "graph", "label": AREA_LABELS["graph"],
            "ratio": round(sum(sub) / len(sub), 3),
            "detail": (f"画像 {n_prof} 个（学成 {n_learned}）；拓扑最近扫描 "
                       f"{(str(topo_age) + ' 天前') if topo_age is not None else '无'}；"
                       f"链路 {n_links} 条；待确认变化 {n_unconf} 条")}

    gaps: list = []
    if not n_prof:
        gaps.append(_gap(
            "graph", "对象画像未生成（0 个对象）",
            "『正常长什么样』没有基线，行为漂移与链路联动异常检测不可用",
            "接入日志与指标后画像引擎自动学习（约 1-2 周出基线）；画像页可看学习进度",
            ENTRY["profiles"],
        ))
    elif n_learned < n_prof:
        gaps.append(_gap(
            "graph", f"{n_prof - n_learned} 个对象画像基线未学成（只记录不告警）",
            "这些对象的变化检测不会触发告警，漂移只能事后翻记录",
            "保持日志/指标连续采集 1-2 周，基线学成后自动转入检测",
            ENTRY["profiles"],
        ))
    if topo_age is None or topo_age > 7:
        gaps.append(_gap(
            "graph",
            "资产拓扑未生成" if topo_age is None else f"资产拓扑已 {topo_age} 天未刷新",
            "排障时看不到层级依赖，链路异常（多层数据不匹配）无从检测",
            "拓扑页点一次扫描（或等每日定时扫描），并保证资产同步正常",
            ENTRY["topology"],
        ))
    if not n_links:
        gaps.append(_gap(
            "graph", "链路未生成（0 条转发边）",
            "SLB→后端→应用的转发关系不可见，故障传播路径只能靠人脑",
            "到 安装向导 页加载 SLS 目录并勾选 SLB 访问日志（project/logstore），保存后"
            "热生效；或同步云转发规则后由平台自动学习",
            ENTRY["links"],
        ))
    if n_unconf:
        gaps.append(_gap(
            "graph", f"{n_unconf} 条画像/链路变化待确认",
            "变化是新业务还是异常悬而未决，漂移事件会反复提醒",
            "画像页逐条确认（接受为正常/忽略），1 分钟清完",
            ENTRY["profiles"],
        ))
    return area, gaps


# ---------------- domains 证书/域名/到期 ----------------

def _check_domains(db: Session) -> tuple:
    """缺登记（六源证据 vs 域名登记表）+ 缺证书到期证据（到期预警的前提）。

    域名是跨账号资产（链路/CDN/拨测证据本身不带账号归属），本面保持全局口径；
    按启用账号收窄会漏掉仍在使用但登记在停用账号下的域名，不如全局如实列出。
    """
    from app.models.domain_registry import DomainRegistry
    from app.services import domain_registry as dr

    idx = dr.collect(db)                      # {归一域名: {sources:set, ...事实}}
    rows = {r.domain: r for r in db.query(DomainRegistry).all()}
    evidence = set(idx)
    registered = set(rows)
    # 缺登记：有业务证据（链路/CDN/画像/拨测/诊断/业务线）但登记表没有——
    # 只有登记/证书订单证据的域名本身就是"已登记事实"，不算缺
    biz_evidence = {d for d, e in idx.items() if set(e.get("sources", ())) & set(_BIZ_EVIDENCE)}
    missing_reg = sorted(biz_evidence - registered)

    certs = dr.cert_index(db, evidence) if evidence else {}
    no_cert = []
    for d in sorted(evidence):
        row = rows.get(d)
        if certs.get(d, {}).get("facts"):
            continue
        if row is not None and row.cert_not_after:
            continue
        no_cert.append(d)

    r1 = 1.0 - min(1.0, len(missing_reg) / max(1, len(biz_evidence)))
    r2 = 1.0 - min(1.0, len(no_cert) / max(1, len(evidence)))
    area = {"area": "domains", "label": AREA_LABELS["domains"],
            "ratio": round((r1 + r2) / 2, 3),
            "detail": (f"证据域名 {len(evidence)} 个（业务证据 {len(biz_evidence)}）；"
                       f"登记 {len(registered)} 个；有证书到期证据 {len(evidence) - len(no_cert)} 个")}

    gaps: list = []
    if missing_reg:
        gaps.append(_gap(
            "domains",
            f"{len(missing_reg)} 个域名缺登记（{_sample(missing_reg)}）",
            "域名→证书→SLB→CDN→业务串不起来，故障归属与到期管理靠人脑",
            "到 到期总账 页点『登记外部域名』填写域名与到期时间；已在域名登记页出现的"
            "记录可直接补归属/业务线",
            ENTRY["expiry"],
        ))
    if no_cert:
        gaps.append(_gap(
            "domains",
            f"{len(no_cert)} 个域名/证书缺到期证据（{_sample(no_cert)}）",
            "证书过期无法提前预警——过期即站点不可用，且到期总账看不到它们",
            "到 拨测 页新增 HTTP(S) 探测目标（勾选证书到期告警，平台自动实测并采集到期时间）；"
            "同时确认证书同步所用云账号有只读权限",
            ENTRY["synth"],
        ))
    return area, gaps
