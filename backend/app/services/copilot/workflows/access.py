"""接入域工作流：云账号 / 数据源 / K8s 集群 / ESXi·vCenter·KVM。

共同安全约束：
  - 需要写入的步骤一律走 engine.change_step() -> change_tools.build_ticket，
    产物=待确认工单，执行由用户在变更确认卡完成；工作流自己绝不执行变更
  - 云账号凭证不进入智能体参数、步骤 detail / 产物 / 运行记录，只由受保护表单提交
  - 探活=只读连通测试（ping/probe），不带任何写操作
"""
from __future__ import annotations

from datetime import datetime
from urllib.parse import urlencode

from sqlalchemy import func

from app.services.copilot.workflows import engine

# 云厂商枚举（与 snapshot.VENDOR_LABELS 同源）
_VENDOR_LABELS = {
    "aliyun": "阿里云", "tencent": "腾讯云", "volc": "火山云", "baidu": "百度云",
    "huawei": "华为云", "aws": "AWS", "azure": "Azure", "onprem": "自建/IDC",
}

# 数据源类型 -> kind（与 services/datasource.py 探测器同名）
_METRIC_TYPES = ("prometheus", "victoriametrics", "zabbix", "influxdb", "opentsdb", "graphite")
_LOG_TYPES = ("elasticsearch", "loki", "graylog", "filebeat", "sls", "skywalking", "zipkin")

_SA_CHECKLIST = [
    "ServiceAccount 绑定只读权限（建议 ClusterRole `view` + `pods/log`，不给 write/delete）",
    "Token 有效期与轮换策略确认（千枢凭证加密入库、只进不出）",
    "命名空间范围：ns_scope 建议按需收敛（空=全部命名空间）",
    "API Server 可达性：千枢服务器能访问 api_url:6443（安全组/防火墙放行）",
    "验证命令（集群侧执行，只读）：kubectl auth whoami / kubectl get nodes",
]


# ---------------- ① access_cloud 接入云账号 ----------------

def _cloud_validate(db, params, ctx):
    from app.models.cloud_account import CloudAccount

    name = params["name"]
    vendor = _VENDOR_LABELS.get(params["vendor"], params["vendor"])
    existing = db.query(CloudAccount).filter(CloudAccount.name == name).first()
    if existing is not None:
        return {"status": engine.STEP_OK,
                "detail": f"账号名={name}，厂商={vendor}；账号已存在，本次只读取既有接入任务",
                "data": {"name": name, "vendor": params["vendor"],
                         "existing_account": True, "account_id": existing.id}}
    return {"status": engine.STEP_OK,
            "detail": f"参数齐备：厂商={vendor}，账号名={name}；智能体不收集凭证，"
                      "请在受保护表单提交只读凭证并完成安全确认",
            "data": {"name": name, "vendor": params["vendor"]}}


def _cloud_form_ref(params: dict) -> dict:
    query = {"new": "1", "vendor": params["vendor"], "name": params["name"],
             "region": params.get("region") or ""}
    return {"kind": "cloud_credential_form", "ack_required": True,
            "route": {"path": "/clouds", "query": query},
            "href": "/clouds?" + urlencode(query)}


def _cloud_form_step(db, params, ctx):
    if ctx["data"].get("校验参数", {}).get("existing_account"):
        return {"status": engine.STEP_SKIPPED,
                "detail": "账号已存在，无需重新提交凭证；继续读取同一接入任务"}
    return {"status": engine.STEP_OK,
            "detail": "已生成受保护凭证表单引用；凭证只由用户在表单中提交，"
                      "不进入聊天、模型或变更工单",
            "data": _cloud_form_ref(params)}


def _cloud_task_ref(run: dict) -> dict:
    """把已序列化的 Run 证据转成可恢复、可跳转的任务引用。"""
    account = run["account"]
    object_ref = {"kind": "cloud_account", "id": account["id"],
                  "name": account["name"], "vendor": account["vendor"]}
    capabilities = []
    evidence_refs = []
    for item in run.get("capabilities") or []:
        refs = list(dict.fromkeys(
            str(ref) for ref in item.get("evidence_refs") or [] if str(ref).strip()))
        if not refs:
            # 默认的 waiting/not_verified 能力不是证据，不能编造时间窗或引用。
            continue
        scope = item.get("scope") or {}
        observed_from = item.get("observed_from")
        observed_until = item.get("observed_until")
        if scope.get("bill_date"):
            observed_from = observed_from or f"{scope['bill_date']}T00:00:00"
            observed_until = observed_until or f"{scope['bill_date']}T23:59:59"
        capabilities.append({
            "capability": item["capability"],
            "scope": scope,
            "execution": item.get("execution"),
            "data_state": item.get("data_state"),
            "time_range": {"checked_at": item.get("checked_at"),
                           "observed_from": observed_from,
                           "observed_until": observed_until},
            "evidence_refs": refs,
        })
        evidence_refs.extend(ref for ref in refs if ref not in evidence_refs)
    query = {"account_id": str(account["id"]), "run_id": str(run["id"]),
             "object": account["name"], "object_kind": object_ref["kind"],
             "object_id": str(account["id"])}
    if evidence_refs:
        query["evidence"] = ",".join(evidence_refs)
    evidence_from = [item["time_range"]["observed_from"] for item in capabilities
                     if item["time_range"].get("observed_from")]
    evidence_until = [item["time_range"]["observed_until"] for item in capabilities
                      if item["time_range"].get("observed_until")]
    if evidence_from:
        query["from"] = min(evidence_from)
    if evidence_until:
        query["to"] = max(evidence_until)
    return {
        "kind": "onboarding_run",
        "account_id": account["id"],
        "run_id": run["id"],
        "object": object_ref,
        "capabilities": capabilities,
        "evidence_refs": evidence_refs,
        "route": {"path": "/clouds", "query": query},
        "href": "/clouds?" + urlencode(query),
    }


def _cloud_run_status(db, params, ctx):
    """只读取唯一 Onboarding Run；不生成测试或全量同步工单。"""
    if "校验参数" not in ctx["data"]:
        return {"status": engine.STEP_SKIPPED,
                "detail": "参数校验未通过，不查询接入任务（先修正参数重跑）"}
    from app.models.cloud_account import CloudAccount
    from app.services import onboarding

    account = db.query(CloudAccount).filter(CloudAccount.name == params["name"]).first()
    if account is None:
        return {"status": engine.STEP_OK,
                "detail": "在受保护表单提交凭证并完成安全确认后，平台会自动完成连通验证、"
                          "分产品资产同步和默认规则播种；无需再测试或创建同步工单，接入结果可在同一任务中查看"}
    run = onboarding.get_run(db, account.id)
    if run is None:
        return {"status": engine.STEP_OK,
                "detail": "账号已存在，但还没有接入任务；重新接入后由平台自动执行并生成任务"}
    result = onboarding.serialize(run, account)
    result["task_ref"] = _cloud_task_ref(result)
    labels = {"running": "接入中", "waiting": "等待启用", "ready": "已就绪",
              "attention": "需要处理", "failed": "接入失败"}
    issues = "；".join(f"{x.get('label')}：{x.get('detail')}" for x in result["issues"][:3])
    return {"status": engine.STEP_OK,
            "detail": f"接入任务 #{run.id}：{labels.get(run.status, run.status)}"
                      + (f"；待处理：{issues}" if issues else "；平台会自动推进，无需重复同步"),
            "data": result}


def _cloud_topology(db, params, ctx):
    from app.models.link_edge import LinkEdge

    try:
        total = db.query(func.count(LinkEdge.id)).scalar() or 0
        hosts = db.query(func.count(func.distinct(LinkEdge.host))).scalar() or 0
        confirmed = (db.query(func.count(LinkEdge.id))
                     .filter(LinkEdge.status == "confirmed").scalar() or 0)
    except Exception as e:  # noqa: BLE001
        return {"status": engine.STEP_FAILED, "detail": f"拓扑库读取失败：{str(e)[:120]}"}
    return {"status": engine.STEP_OK,
            "detail": f"当前拓扑覆盖：{total} 条边 / {hosts} 个域名（已确认 {confirmed}）；"
                      f"新账号资产同步完成后，请到 拓扑 页确认新节点归属与链路（不覆盖已有确认）",
            "data": {"edges": total, "hosts": hosts, "confirmed": confirmed}}


def _cloud_artifact(db, params, ctx):
    name = params["name"]
    vendor = _VENDOR_LABELS.get(params["vendor"], params["vendor"])
    tickets = ctx.get("tickets") or []
    existing_account = bool((ctx["data"].get("校验参数") or {}).get("existing_account"))
    form = ctx["data"].get("受保护凭证表单") or {}
    credential_detail = ("本次仅读取既有接入任务，未读取或回显账号凭证" if existing_account else
                         "由用户在受保护表单直接提交，智能体不收集、不回显")
    next_step = ("1. 在同一接入任务查看进度和失败项；没有 Run 时重新接入，由平台自动执行"
                 if existing_account else
                 "1. 打开受保护凭证表单，提交只读 RAM/子账号凭证并完成 **「我已知晓」安全警示**")
    audit_detail = ("既有账号只读查询不新增建号确认审计" if existing_account else
                    "表单提交时的安全确认记入审计（cloud.account.ack）")
    topo = ctx["data"].get("拓扑覆盖检查") or {}
    run = ctx["data"].get("接入任务状态") or {}
    run_status = run.get("status") or ("尚无接入任务" if existing_account else "提交受保护表单后自动创建")
    report = f"""## 接入报告 · 云账号「{name}」（{vendor}）

| 项 | 内容 |
|---|---|
| 账号名 | {name} |
| 厂商 | {vendor} |
| 凭证 | {credential_detail} |
| 接入任务 | {run_status} |
| 拓扑现状 | {topo.get('edges', 0)} 条边 / {topo.get('hosts', 0)} 域名（已确认 {topo.get('confirmed', 0)}） |

### 后续动作
{next_step}
2. 平台自动验证凭证、同步各产品资产并播种默认规则；在同一接入任务查看进度和失败项，无需手工测试或重复同步
3. 任务就绪后，拓扑页确认新节点归属；成本页核对账单口径（pretax_amount=账单金额）

### 安全须知
- 千枢与智能体对云资源 **只读**；账号登记由受保护表单提交，安全确认仍由用户完成
- 凭证不进入聊天、模型或智能体产物；{audit_detail}
"""
    highlights = ([
        f"云账号「{name}」（{vendor}）已存在，本次仅读取同一接入任务",
        "未读取或回显既有账号凭证，未创建变更工单",
        "接入进度、能力证据和失败项均来自同一 Onboarding Run",
    ] if existing_account else [
        f"云账号「{name}」（{vendor}）的受保护凭证表单引用已生成",
        "智能体未收集凭证，未创建建号或同步工单",
        "用户在表单提交凭证并完成安全确认后，由同一后台任务自动接入",
    ])
    artifact = {"report_md": report, "highlights": highlights,
                "account": {"name": name, "vendor": vendor},
                "onboarding_run": run or None,
                "ticket_ids": [t.get("ticket_id") for t in tickets],
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    if form:
        artifact["credential_form"] = form
    if run.get("task_ref"):
        artifact["task_ref"] = run["task_ref"]
    return artifact


# ---------------- ② access_datasource 接入数据源 ----------------

_TYPE_ADVICE = {
    "victoriametrics": "轻量自建首选（千枢默认指标源同款），单二进制、PromQL 兼容",
    "prometheus": "已有 Prometheus 直接接；注意保留期与查询超时",
    "zabbix": "传统监控迁移场景；接只读 API 账号即可，历史数据不动",
    "influxdb": "InfluxDB 1.x/2.x 需区分鉴权方式（用户名密码 / Token）",
    "opentsdb": "老系统兼容场景；仅支持写入型指标查询",
    "graphite": "老系统兼容场景",
    "elasticsearch": "日志检索首选；建议只读账号 + 收敛索引模式",
    "loki": "K8s 日志场景首选；查询走 LogQL 只读",
    "graylog": "已有 Graylog 直接接查询 API",
    "filebeat": "轻量文件日志采集；先确认要采的日志路径",
    "sls": "阿里云日志服务；走只读 AK（建议 sls:GetLogs* 只读策略）",
    "skywalking": "APM 链路；接只读 API，服务目录自动进资产",
    "zipkin": "APM 链路；接只读 API",
}


def _ds_advice(db, params, ctx):
    typ = params["type"]
    kind = "指标" if typ in _METRIC_TYPES else "日志"
    return {"status": engine.STEP_OK,
            "detail": f"选型建议（{kind}源 {typ}）：{_TYPE_ADVICE.get(typ, '按现有生态选型')}",
            "data": {"kind": kind, "type": typ}}


def _ds_guide(db, params, ctx):
    typ = params["type"]
    kind = ctx["data"].get("选型建议", {}).get("kind", "")
    url = params.get("url") or ""
    guide = (
        f"配置指引（{kind}源 {typ}）：集成中心 → 数据源 → 新增{kind}源；"
        f"类型选 {typ}，名称填「{params['name']}」，URL 填 {url or '（服务地址，形如 http://host:port）'}，"
        "鉴权用只读账号（Token/Basic/Bearer 按类型）；配置完成后点「测试连通」再启用。"
        "注意：数据源登记暂无变更工具封装（变更白名单未含），本步只出指引、请在集成中心人工登记"
    )
    return {"status": engine.STEP_OK, "detail": guide}


def _ds_probe(db, params, ctx):
    from app.services import datasource as ds

    url = params.get("url") or ""
    if not url:
        return {"status": engine.STEP_SKIPPED,
                "detail": "未提供 url——请先在集成中心完成配置后点「测试连通」"}
    typ = params["type"]
    src = {"type": typ, "name": params["name"], "url": url}
    try:
        if typ in _METRIC_TYPES:
            r = ds.probe_metric_source(src, db)
        else:
            r = ds.probe_log_source(src, db)
    except Exception as e:  # noqa: BLE001
        return {"status": engine.STEP_OK,
                "detail": f"探活执行异常（按不通计）：{type(e).__name__}: {str(e)[:120]}"}
    ok = bool(r.get("ok"))
    return {"status": engine.STEP_OK,
            "detail": f"连通性：{'通' if ok else '不通'} — {str(r.get('detail') or '')[:200]}",
            "data": {"probe_ok": ok, "probe_detail": str(r.get("detail") or "")[:300]}}


def _ds_enable_check(db, params, ctx):
    from app.services.datasource import log_sources, metric_sources

    typ = params["type"]
    existing = metric_sources() if typ in _METRIC_TYPES else log_sources()
    same = [s for s in existing if (s.get("type") or "") == typ]
    named = [s for s in existing if (s.get("name") or "") == params["name"]]
    if named:
        detail = f"启用检查：已存在同名数据源「{params['name']}」——勿重复启用，改配置即可"
    elif same:
        detail = f"启用检查：平台已有 {len(same)} 个 {typ} 源（{'、'.join(s.get('name', '') for s in same[:3])}）；新源启用后互不影响"
    else:
        detail = f"启用检查：当前无 {typ} 类数据源，本次接入将成为首个；启用后底座体检/画像/巡检将自动有数据面"
    return {"status": engine.STEP_OK,
            "detail": detail + "；启用=配置写入 .env 后重启生效（或集成中心保存即生效），启用后跑一次 底座体检 确认取数",
            "data": {"existing_same_type": len(same), "name_exists": bool(named)}}


def _ds_artifact(db, params, ctx):
    kind = ctx["data"].get("选型建议", {}).get("kind", "")
    probe = ctx["data"].get("测试连通") or {}
    report = f"""## 接入报告 · 数据源「{params['name']}」（{kind}源 {params['type']}）

| 项 | 内容 |
|---|---|
| 名称 | {params['name']} |
| 类型 | {params['type']}（{kind}源） |
| 地址 | {params.get('url') or '（未提供）'} |
| 连通性 | {'通' if probe.get('probe_ok') else ('不通' if probe else '未测')} |

### 配置要点
- 鉴权一律只读账号；URL/Token 等敏感项只进配置文件与加密凭证，不进对话与产物
- 启用后到 底座体检 确认数据面取数正常

### 后续动作
1. 集成中心 → 数据源 按指引填写并保存
2. 点「测试连通」——取到版本/系统信息才算通
3. 启用后跑底座体检核对
"""
    conn = "通" if probe.get("probe_ok") else ("不通" if probe else "未测")
    return {"report_md": report,
            "highlights": [
                f"数据源「{params['name']}」接入指引已生成（{params['type']}）",
                f"连通性：{conn}",
                "数据源登记暂无变更工具，走集成中心人工登记",
            ],
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


# ---------------- ③ access_k8s 接入 K8s 集群 ----------------

def _k8s_guide(db, params, ctx):
    return {"status": engine.STEP_OK,
            "detail": "登记指引：① 集群侧建只读 ServiceAccount（见第 3 步清单）；"
                      "② 千枢 K8s 管理 → 添加集群，填名称/ api_url / Token（勾选只读、按需收敛 ns_scope）；"
                      "③ 点「连通测试」通过后保存。集群登记暂无变更工具封装，走 K8s 管理页人工登记",
            "data": {"name": params["name"], "api_url": params["api_url"]}}


def _k8s_probe(db, params, ctx):
    from app.collector.k8s import ping

    token = params.get("token") or ""
    try:
        r = ping(params["api_url"], token, bool(params.get("verify_tls", True)))
    except Exception as e:  # noqa: BLE001
        return {"status": engine.STEP_OK,
                "detail": f"探活执行异常（按不通计）：{type(e).__name__}: {str(e)[:120]}",
                "data": {"probe_ok": False}}
    ok = bool(r.get("ok")) if isinstance(r, dict) else bool(r)
    detail = str((r or {}).get("detail") or r or "")[:200] if isinstance(r, dict) else str(r)[:200]
    return {"status": engine.STEP_OK,
            "detail": f"探活 {params['api_url']}：{'通' if ok else '不通'} — {detail}"
                      + ("（Token 已用于探测，不进入步骤产物）" if token else "（未提供 Token，匿名探测）"),
            "data": {"probe_ok": ok}}


def _k8s_sa_checklist(db, params, ctx):
    return {"status": engine.STEP_OK,
            "detail": "SA 权限检查清单：" + "；".join(_SA_CHECKLIST),
            "data": {"checklist": _SA_CHECKLIST}}


def _k8s_artifact(db, params, ctx):
    probe = ctx["data"].get("探活") or {}
    report = f"""## 接入报告 · K8s 集群「{params['name']}」

| 项 | 内容 |
|---|---|
| 集群名 | {params['name']} |
| API Server | {params['api_url']} |
| TLS 校验 | {'开启' if params.get('verify_tls', True) else '关闭（自签）'} |
| 探活 | {'通' if probe.get('probe_ok') else '不通/未测'} |

### SA 权限检查清单
""" + "\n".join(f"- [ ] {c}" for c in _SA_CHECKLIST) + """

### 后续动作
1. 按清单建只读 SA/Token → K8s 管理 → 添加集群 → 连通测试 → 保存
2. 扫描一次资源清单，确认 Pod/Node 数据进平台
3. Token 轮换策略到期前更新（千枢凭证加密入库，只进不出）
"""
    return {"report_md": report,
            "highlights": [
                f"K8s 集群「{params['name']}」接入指引+SA 权限清单已生成",
                f"探活：{'通' if probe.get('probe_ok') else '不通/未测'}",
                "登记走 K8s 管理页（变更层暂未封装 k8s 登记工具）",
            ],
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


# ---------------- ④ access_virt 接入 ESXi/vCenter/KVM ----------------

_VIRT_LABELS = {"esxi": "ESXi", "vcenter": "vCenter", "kvm": "KVM/libvirt"}
_VIRT_GUIDE = {
    "esxi": "ESXi：vSphere Client 建只读角色（除 System.* 外只给只读权限）并绑定专用账号；"
            "登记地址填主机 IP（探活走 /sdk 与登录页取版本）",
    "vcenter": "vCenter：建只读 SSO 账号（只读权限组）；登记地址填 vCenter 域名/IP；"
               "自签证书场景登记时跳过 SSL 校验",
    "kvm": "KVM：libvirt 只读连接（qemu:///system 或 qemu+tcp://host/system，禁用 write 权限）；"
           "登记地址填 libvirt URI",
}


def _virt_guide(db, params, ctx):
    return {"status": engine.STEP_OK,
            "detail": f"登记指引（{_VIRT_LABELS[params['kind']]}）：{_VIRT_GUIDE[params['kind']]}"
                      "；登记走 虚拟化/集成 页面人工填写（变更层暂未封装虚拟化登记工具）",
            "data": {"kind": params["kind"]}}


def _virt_probe(db, params, ctx):
    kind = params["kind"]
    host = params["host"]
    try:
        if kind == "kvm":
            from app.services.kvm import probe_kvm
            r = probe_kvm(host)
        else:
            from app.services.vsphere import probe_vsphere
            r = probe_vsphere(host, verify=bool(params.get("verify", False)))
    except Exception as e:  # noqa: BLE001
        return {"status": engine.STEP_OK,
                "detail": f"探活执行异常（按不通计）：{type(e).__name__}: {str(e)[:120]}",
                "data": {"probe_ok": False}}
    ok = bool(r.get("ok"))
    return {"status": engine.STEP_OK,
            "detail": f"探活 {host}：{'通' if ok else '不通'} — {str(r.get('detail') or '')[:200]}",
            "data": {"probe_ok": ok, "version": str(r.get("version") or ""),
                     "product": str(r.get("product") or "")}}


def _virt_collect_check(db, params, ctx):
    probe = ctx["data"].get("探活") or {}
    items = [
        "采集账号只读（vCenter 只读组 / ESXi 只读角色 / libvirt 只读连接）",
        "网络可达：千枢服务器到管理地址 443/80（vSphere）或 libvirt 端口（KVM）",
        "采集频率与超时按默认（5 分钟级），大环境注意 API 配额",
        "登记后先看 主机/VM 清单是否进来，再接巡检与画像",
    ]
    ver = probe.get("version") or ""
    detail = "采集检查清单：" + "；".join(items)
    if ver:
        detail = f"探测到版本 {ver}（{probe.get('product') or ''}），采集能力随版本自动启用；" + detail
    return {"status": engine.STEP_OK, "detail": detail, "data": {"checklist": items}}


def _virt_artifact(db, params, ctx):
    kind = params["kind"]
    probe = ctx["data"].get("探活") or {}
    report = f"""## 接入报告 · 虚拟化「{params.get('name') or params['host']}」（{_VIRT_LABELS[kind]}）

| 项 | 内容 |
|---|---|
| 类型 | {_VIRT_LABELS[kind]} |
| 管理地址 | {params['host']} |
| 探活 | {'通' if probe.get('probe_ok') else '不通/未测'}（{probe.get('version') or '无版本信息'}） |

### 登记指引
{_VIRT_GUIDE[kind]}

### 采集检查
""" + "\n".join(f"- [ ] {c}" for c in (ctx["data"].get("采集检查") or {}).get("checklist", [])) + """
"""
    return {"report_md": report,
            "highlights": [
                f"{_VIRT_LABELS[kind]}（{params['host']}）接入指引已生成",
                f"探活：{'通' if probe.get('probe_ok') else '不通/未测'}",
                "登记走虚拟化/集成页人工填写，采集检查清单已列",
            ],
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


# ---------------- spec 注册表 ----------------

ACCESS_WORKFLOWS = {
    "access_cloud": {
        "name": {"zh": "接入云账号", "en": "Onboard cloud account"},
        "domain": {"zh": "接入域", "en": "Onboarding"},
        "description": {
            "zh": "云账号接入：既有账号读取统一 Run；新账号返回受保护凭证表单，"
                  "智能体不收集凭证、不创建建号工单；表单提交后自动探活、分产品同步和播种",
            "en": "Cloud account onboarding: read the shared run for an existing account or return a protected "
                  "credential form for a new account. The agent collects no credentials and creates no account ticket; "
                  "form submission triggers automatic probe, per-product sync and rule seeding.",
        },
        "params_schema": {
            "vendor": {"type": "string", "required": True,
                       "enum": list(_VENDOR_LABELS.keys()),
                       "description": "云厂商：aliyun/tencent/volc/baidu/huawei/aws/azure/onprem"},
            "name": {"type": "string", "required": True, "description": "账号名称（平台内唯一）"},
            "region": {"type": "string", "required": False, "description": "默认地域，如 cn-hangzhou"},
        },
        "steps": [
            ("校验参数", _cloud_validate),
            ("受保护凭证表单", _cloud_form_step),
            ("接入任务状态", _cloud_run_status),
            ("拓扑覆盖检查", _cloud_topology),
        ],
        "build_artifact": _cloud_artifact,
    },
    "access_datasource": {
        "name": {"zh": "接入数据源", "en": "Onboard data source"},
        "domain": {"zh": "接入域", "en": "Onboarding"},
        "description": {
            "zh": "数据源接入：选型建议 → 配置指引 → 测试连通（真实探活） → 启用检查，产物=接入报告",
            "en": "Data source onboarding: type advice → setup guide → connectivity test (real probe) "
                  "→ enablement check. Artifact: onboarding report",
        },
        "params_schema": {
            "type": {"type": "string", "required": True,
                     "enum": list(_METRIC_TYPES) + list(_LOG_TYPES),
                     "description": "数据源类型（指标：prometheus/victoriametrics/zabbix/influxdb/opentsdb/graphite；"
                                    "日志：elasticsearch/loki/graylog/filebeat/sls/skywalking/zipkin）"},
            "name": {"type": "string", "required": True, "description": "数据源名称"},
            "url": {"type": "string", "required": False, "description": "服务地址（http(s)://host:port），用于测试连通"},
        },
        "steps": [
            ("选型建议", _ds_advice),
            ("配置指引", _ds_guide),
            ("测试连通", _ds_probe),
            ("启用检查", _ds_enable_check),
        ],
        "build_artifact": _ds_artifact,
    },
    "access_k8s": {
        "name": {"zh": "接入 K8s 集群", "en": "Onboard Kubernetes cluster"},
        "domain": {"zh": "接入域", "en": "Onboarding"},
        "description": {
            "zh": "K8s 集群接入：登记指引 → 探活（API Server 连通） → SA 权限检查清单，产物=接入报告",
            "en": "Kubernetes cluster onboarding: registration guide → probe (API server connectivity) "
                  "→ SA permission checklist. Artifact: onboarding report",
        },
        "params_schema": {
            "name": {"type": "string", "required": True, "description": "集群名称"},
            "api_url": {"type": "string", "required": True, "description": "API Server 地址，如 https://1.2.3.4:6443"},
            "token": {"type": "string", "required": False, "description": "只读 ServiceAccount Token（加密入库不回显）"},
            "verify_tls": {"type": "boolean", "required": False, "default": True,
                           "description": "是否校验 TLS 证书（自签证书可关）"},
        },
        "steps": [
            ("登记指引", _k8s_guide),
            ("探活", _k8s_probe),
            ("SA 权限检查清单", _k8s_sa_checklist),
        ],
        "build_artifact": _k8s_artifact,
    },
    "access_virt": {
        "name": {"zh": "接入 ESXi/vCenter/KVM", "en": "Onboard ESXi/vCenter/KVM"},
        "domain": {"zh": "接入域", "en": "Onboarding"},
        "description": {
            "zh": "虚拟化接入：登记指引 → 探活（vSphere/KVM 真实探测取版本） → 采集检查，产物=接入报告",
            "en": "Virtualization onboarding: registration guide → probe (real vSphere/KVM version detection) "
                  "→ collection check. Artifact: onboarding report",
        },
        "params_schema": {
            "kind": {"type": "string", "required": True, "enum": ["esxi", "vcenter", "kvm"],
                     "description": "虚拟化类型"},
            "host": {"type": "string", "required": True,
                     "description": "管理地址：ESXi/vCenter 填 IP/域名，KVM 填 libvirt URI"},
            "name": {"type": "string", "required": False, "description": "登记名称（缺省用管理地址）"},
            "verify": {"type": "boolean", "required": False, "default": False,
                       "description": "HTTPS 证书校验（vCenter 自签证书默认跳过）"},
        },
        "steps": [
            ("登记指引", _virt_guide),
            ("探活", _virt_probe),
            ("采集检查", _virt_collect_check),
        ],
        "build_artifact": _virt_artifact,
    },
}
