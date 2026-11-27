"""集成中心 API：接入点+配置教程+状态，全部产品内可见。

GET  /integrations/overview    三类集成总览（事件源/数据源/能力开放）
GET  /integrations/guide/{key} 单项接入教程（统一五段：这是什么/配置步骤/接入点/注意事项/测试连通）
GET  /integrations/ds-guides   数据源类型教程（新增数据源弹窗用，15 类）
GET  /integrations/registry    集成管理：全部已接入集成 + 真实状态
POST /integrations/test/{key}  事件源链路自检（只读查真实事件到达证据）
POST /integrations/check-channel/{id}  通知渠道只读自检（绝不发送消息）

测试必须验证真实链路状态，验证不到就诚实说明"无法自证，只能检查 X"：
事件源查该接入点最近真实到达的外部事件（有=已连通，无=「未验证」并引导从源端发一条），
不能向自身接入点自发样本事件冒充连通；数据源做真实版本探测（在数据源弹窗/集成管理行内）；
通知渠道只做只读凭证自检+历史发送记录检查（对外发测试消息只能用户主动触发，绝不自动发）。

接入点模板只写占位符 {base_url}/{webhook_url}/{host}，渲染时按请求头推导
（X-Forwarded-Proto/Host 优先，兼容 nginx/域名/直连 IP），前端展示层再按
window.location.origin 归一——IP 访问显 IP，域名访问显域名。
"""
from __future__ import annotations

import copy
import logging
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import get_current_user, require_roles
from app.core.i18n_msg import lang_of
from app.models.user import User

logger = logging.getLogger("qianshu.integrations_api")
router = APIRouter(prefix="/integrations", tags=["integrations"])


# ---------------- 接入点基地址（按请求推导，不写死 IP/端口） ----------------

def _base_url_of(request: Request) -> str:
    """平台访问地址：PUBLIC_BASE_URL 配置了就以其为准
    （部署在域名/443 后无法从请求头推导时的权威值）；默认空=按请求头推导
    （X-Forwarded-* 优先，兼容 nginx/域名/直连 IP）——用户用什么地址访问就显示什么。"""
    from app.core.config import settings

    if settings.PUBLIC_BASE_URL:
        return settings.PUBLIC_BASE_URL.rstrip("/")
    scheme = (request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
              or request.url.scheme)
    host = (request.headers.get("x-forwarded-host", "").split(",")[0].strip()
            or request.headers.get("host", "").strip())
    return f"{scheme}://{host}" if host else settings.PUBLIC_BASE_URL


def _fill(text: str, base: str) -> str:
    """占位替换：{base_url}=平台地址，{webhook_url}=事件接入点，{host}=主机名（OTLP 拼端口用）。"""
    host = urlsplit(base).hostname or ""
    return (text.replace("{base_url}", base)
            .replace("{webhook_url}", f"{base}/api/events/webhook")
            .replace("{host}", host))


# ---------------- 教程库（产品内直读，不依赖 docs/） ----------------
# 统一五段结构：summary=①这是什么  steps=②配置步骤  endpoint=③接入点
# notes=④注意事项  test_hint/testable=⑤测试连通；code_blocks=步骤附带代码块（带复制）
# 双语载体：展示文案字段一律 {"zh":..,"en":..}
# （列表逐项双语）；语言无关的 URL/专名/纯代码保持 str（app.core.i18n_msg.pick 透传）。
# 出口按 Accept-Language 选文（lang_of），响应契约不变（字段仍是 str），前端零改动；
# zh 文案逐字保留旧版，en 为客户可见的地道译文。

GUIDES = {
    "gitlab": {
        "name": "GitLab", "category": "event", "icon": "Share",
        "summary": {
            "zh": "代码推送 / MR / 流水线事件进入千枢变更时间线——排障第一问「最近发了什么版」自动覆盖。",
            "en": "Push / MR / pipeline events flow into the QianShu change timeline — the first question in any incident, 'what shipped recently', is answered automatically.",
        },
        "steps": [
            {"zh": "GitLab 项目 → Settings → Webhooks（群组级：Group → Settings → Webhooks）",
             "en": "In your GitLab project go to Settings → Webhooks (for a group: Group → Settings → Webhooks)"},
            {"zh": "URL 填：{webhook_url}/gitlab",
             "en": "Set the URL to: {webhook_url}/gitlab"},
            {"zh": "Secret token 填千枢的 EVENT_WEBHOOK_TOKEN（密钥类仅存后端 .env，手改 .env 后重启生效）",
             "en": "Set the secret token to QianShu's EVENT_WEBHOOK_TOKEN (secrets live only in the backend .env; hand-edit .env and restart to take effect)"},
            {"zh": "Trigger 勾选：Push events / Merge request events / Pipeline events",
             "en": "Check the triggers: Push events / Merge request events / Pipeline events"},
            {"zh": "点 Test → Push events 发送，千枢「变更记录」出现即接通",
             "en": "Click Test → Push events. The integration is live once the event shows up under Change Records in QianShu"},
        ],
        "endpoint": "{webhook_url}/gitlab",
        "notes": [
            {"zh": "协议：X-Gitlab-Event 头（Push Hook / Merge Request Hook / Pipeline Hook）",
             "en": "Protocol: X-Gitlab-Event header (Push Hook / Merge Request Hook / Pipeline Hook)"},
            {"zh": "Secret 与 EVENT_WEBHOOK_TOKEN 不一致会被拒收——两端必须一致",
             "en": "Events are rejected if the secret does not match EVENT_WEBHOOK_TOKEN — both sides must use the same value"},
            {"zh": "群组级 Webhook 对组内全部仓库生效，不必逐仓库配置",
             "en": "A group-level webhook covers every repository in the group — no per-repo setup needed"},
        ],
        "test_hint": {
            "zh": "「检查链路」查该接入点最近真实到达的 GitLab 事件（只读）：近 7 天有事件=已连通（条数+最近时间）；尚无事件=如实提示「未验证」，可从 GitLab 的 Test 按钮发一条后复核",
            "en": "'Check link' looks for real GitLab events that actually arrived at this endpoint (read-only): events in the last 7 days mean connected (count + latest time); with no events yet it honestly reports 'Unverified' — send one from GitLab's Test button and check again",
        },
        "testable": True,
    },
    "gitea": {
        "name": "Gitea", "category": "event", "icon": "Compass",
        "summary": {
            "zh": "私有化部署的 Gitea 仓库的 Push / PR 事件进入千枢变更时间线。",
            "en": "Push / PR events from your self-hosted Gitea repositories flow into the QianShu change timeline.",
        },
        "steps": [
            {"zh": "仓库 → Settings → Webhooks → Add Webhook → Gitea",
             "en": "In the repo go to Settings → Webhooks → Add Webhook → Gitea"},
            {"zh": "URL 填：{webhook_url}/gitea",
             "en": "Set the URL to: {webhook_url}/gitea"},
            {"zh": "Secret 填 EVENT_WEBHOOK_TOKEN",
             "en": "Set the secret to EVENT_WEBHOOK_TOKEN"},
            {"zh": "触发事件勾选 Push / Pull Request，保存后 Test Delivery",
             "en": "Check Push / Pull Request as trigger events, save, then use Test Delivery"},
        ],
        "endpoint": "{webhook_url}/gitea",
        "notes": [
            {"zh": "协议与 GitLab 同族（object_kind 事件模型）",
             "en": "Same protocol family as GitLab (object_kind event model)"},
            {"zh": "Secret 走 X-Gitea-Token 头传递，已被千枢校验",
             "en": "The secret travels in the X-Gitea-Token header and is validated by QianShu"},
        ],
        "test_hint": {
            "zh": "「检查链路」查该接入点最近真实到达的事件（只读）：近 7 天有事件=已连通；尚无事件=「未验证」——可从 Gitea 的 Test Delivery 发一条后复核",
            "en": "'Check link' looks for real events that actually arrived at this endpoint (read-only): events in the last 7 days mean connected; with no events yet it reports 'Unverified' — send one via Gitea's Test Delivery and check again",
        },
        "testable": True,
    },
    "github": {
        "name": "GitHub", "category": "event", "icon": "Platform",
        "summary": {
            "zh": "PR / Release / Workflow 事件进入千枢变更时间线。",
            "en": "PR / Release / workflow events flow into the QianShu change timeline.",
        },
        "steps": [
            {"zh": "仓库 → Settings → Webhooks → Add webhook",
             "en": "In the repo go to Settings → Webhooks → Add webhook"},
            {"zh": "Payload URL 填：{webhook_url}/github",
             "en": "Set the Payload URL to: {webhook_url}/github"},
            {"zh": "Content type 选 application/json；Secret 填 EVENT_WEBHOOK_TOKEN",
             "en": "Choose application/json as the content type; set the secret to EVENT_WEBHOOK_TOKEN"},
            {"zh": "事件选 Let me select：Pull requests / Releases / Workflow runs",
             "en": "Under 'Let me select', pick: Pull requests / Releases / Workflow runs"},
            {"zh": "Recent Deliveries 里 Test 发送，千枢变更记录出现即接通",
             "en": "Send a Test from Recent Deliveries. The integration is live once it shows up in QianShu's change records"},
        ],
        "endpoint": "{webhook_url}/github",
        "notes": [
            {"zh": "协议：X-Hub-Signature-256 标准 HMAC-SHA256 验签（已支持）",
             "en": "Protocol: standard X-Hub-Signature-256 HMAC-SHA256 verification (supported)"},
            {"zh": "GitHub.com 与 GitHub Enterprise 均可用——GitHub 侧需能回调到千枢接入点",
             "en": "Works with both GitHub.com and GitHub Enterprise — GitHub must be able to reach the QianShu endpoint"},
        ],
        "test_hint": {
            "zh": "「检查链路」查该接入点最近真实到达的事件（只读）：近 7 天有事件=已连通；尚无事件=「未验证」——可从 GitHub 的 Recent Deliveries Test 发一条后复核",
            "en": "'Check link' looks for real events that actually arrived at this endpoint (read-only): events in the last 7 days mean connected; with no events yet it reports 'Unverified' — send one via the Test button in GitHub's Recent Deliveries and check again",
        },
        "testable": True,
    },
    "jenkins": {
        "name": "Jenkins", "category": "event", "icon": "Tools",
        "summary": {
            "zh": "构建开始 / 结束事件进入千枢变更时间线——发版时刻与故障时刻对齐。",
            "en": "Build start / finish events flow into the QianShu change timeline — release times line up with incident times.",
        },
        "steps": [
            {"zh": "Jenkins 安装「Notification」插件（Manage Jenkins → Plugins）",
             "en": "Install the 'Notification' plugin in Jenkins (Manage Jenkins → Plugins)"},
            {"zh": "目标 Job → Configure → Build Environment 勾选 Generic Webhook Notification",
             "en": "Open the target job → Configure → under Build Environment check Generic Webhook Notification"},
            {"zh": "URL 填：{webhook_url}/jenkins，Token 填 EVENT_WEBHOOK_TOKEN",
             "en": "Set the URL to {webhook_url}/jenkins and the token to EVENT_WEBHOOK_TOKEN"},
            {"zh": "或在 Pipeline 里直接推送（见下方命令块）",
             "en": "Or push directly from your Pipeline (see the command block below)"},
            {"zh": "构建一次，千枢变更记录出现即接通",
             "en": "Run one build. The integration is live once it shows up in QianShu's change records"},
        ],
        "endpoint": "{webhook_url}/jenkins",
        "code_blocks": [{
            "title": {"zh": "Pipeline 推送示例", "en": "Pipeline push example"},
            "code": "sh 'curl -X POST {webhook_url}/jenkins -H \"X-Webhook-Token: <token>\" -d @payload.json'",
        }],
        "notes": [
            {"zh": "协议：接受 {name, build:{number,status,phase,scm}} 形态 payload",
             "en": "Protocol: accepts a {name, build:{number,status,phase,scm}} payload"},
            {"zh": "Token 走 X-Webhook-Token 头，与 EVENT_WEBHOOK_TOKEN 一致",
             "en": "The token travels in the X-Webhook-Token header and must match EVENT_WEBHOOK_TOKEN"},
        ],
        "test_hint": {
            "zh": "「检查链路」查该接入点最近真实到达的事件（只读）：近 7 天有事件=已连通；尚无事件=「未验证」——构建一次（或 Pipeline 推送）后复核",
            "en": "'Check link' looks for real events that actually arrived at this endpoint (read-only): events in the last 7 days mean connected; with no events yet it reports 'Unverified' — run a build (or push from your Pipeline) and check again",
        },
        "testable": True,
    },
    "skywalking": {
        "name": {"zh": "SkyWalking / Zipkin 告警", "en": "SkyWalking / Zipkin alerts"},
        "category": "event", "icon": "TrendCharts",
        "summary": {
            "zh": "APM 告警进入千枢变更事件时间线；当前不生成告警中心的 firing 记录，也不走告警通知、确认和恢复闭环。",
            "en": "APM alerts enter the QianShu change-event timeline; they do not currently create firing records in Alert Center or use alert notification, acknowledgement and recovery workflows.",
        },
        "steps": [
            {"zh": "SkyWalking：告警规则里配置 webhook → {webhook_url}/skywalking",
             "en": "SkyWalking: configure a webhook in your alert rules → {webhook_url}/skywalking"},
            {"zh": "Zipkin：告警 / 通知插件同理 → {webhook_url}/zipkin",
             "en": "Zipkin: do the same in your alert / notification plugin → {webhook_url}/zipkin"},
            {"zh": "token 头：X-Webhook-Token: <EVENT_WEBHOOK_TOKEN>",
             "en": "Token header: X-Webhook-Token: <EVENT_WEBHOOK_TOKEN>"},
        ],
        "endpoint": "{webhook_url}/skywalking",
        "notes": [
            {"zh": "协议：接受 {name/level/message} 形态的告警 payload",
             "en": "Protocol: accepts alert payloads of the form {name/level/message}"},
            {"zh": "全量 trace 不存——产品边界：千枢收告警与服务目录，不做 trace 分析",
             "en": "Full traces are not stored — product boundary: QianShu ingests alerts and service catalogs, not trace analysis"},
        ],
        "test_hint": {
            "zh": "「检查链路」查该接入点最近真实到达的告警事件（只读）：近 7 天有事件=已连通；尚无事件=「未验证」——可从 SkyWalking 告警规则发一条测试后复核",
            "en": "'Check link' looks for real alert events that actually arrived at this endpoint (read-only): events in the last 7 days mean connected; with no events yet it reports 'Unverified' — fire a test alert from a SkyWalking alert rule and check again",
        },
        "testable": True,
    },
    "prometheus": {
        "name": "Prometheus / VictoriaMetrics", "category": "metric", "icon": "DataLine",
        "ds_type": "prometheus",
        "summary": {
            "zh": "注册已有 Prometheus / VictoriaMetrics 地址并执行真实只读探活；当前不自动进入统一查询、告警、画像或诊断链路。",
            "en": "Register an existing Prometheus / VictoriaMetrics endpoint and run a real read-only probe; it is not currently routed into unified queries, alerts, profiling or diagnosis.",
        },
        "steps": [
            {"zh": "确认 Prometheus HTTP API 对千枢可达（默认 :9090；VictoriaMetrics 默认 :8428）",
             "en": "Make sure the Prometheus HTTP API is reachable from QianShu (default :9090; VictoriaMetrics default :8428)"},
            {"zh": "「新增数据源」选 Prometheus / VictoriaMetrics，填地址（如 http://<prom>:9090）",
             "en": "In 'Add data source' pick Prometheus / VictoriaMetrics and fill in the URL (e.g. http://<prom>:9090)"},
            {"zh": "「测试连通」取到版本后保存",
             "en": "Click 'Test connectivity' and save once a version is returned"},
            {"zh": "K8s 监控同理：kube-state-metrics / cAdvisor → Prometheus → 这里注册",
             "en": "Kubernetes monitoring works the same way: kube-state-metrics / cAdvisor → Prometheus → register it here"},
        ],
        "endpoint": "",
        "notes": [
            {"zh": "Prometheus 与 VictoriaMetrics 使用兼容的探活接口；探活不等于查询、同步或告警已接通",
             "en": "Prometheus and VictoriaMetrics expose compatible probe endpoints; a successful probe does not mean queries, synchronization or alerts are connected"},
            {"zh": "云托管入口（腾讯云可观测 / AWS AMP / 火山 TLS）在「鉴权头」填 Authorization（Basic/Bearer）",
             "en": "For managed offerings (Tencent Cloud Observability / AWS AMP / Volcengine TLS) put Authorization (Basic/Bearer) in the 'Auth header' field"},
            {"zh": "当前边界：内置告警、巡检、画像和诊断仍只查询系统内置指标库",
             "en": "Current limit: built-in alerts, inspections, profiling and diagnosis still query only the built-in metric store"},
        ],
        "test_hint": {
            "zh": "「新增数据源」弹窗里点「测试连通」——取 /api/v1/status/buildinfo 版本（VM 另抓 vm_app_version），显示「连通 · 版本 x.y.z」才算通",
            "en": "Click 'Test connectivity' in the 'Add data source' dialog — it fetches the /api/v1/status/buildinfo version (for VM it also reads vm_app_version). It counts as connected only when 'Connected · version x.y.z' appears",
        },
        "testable": False,
    },
    "loki": {
        "name": "Grafana Loki", "category": "log", "icon": "Document",
        "ds_type": "loki",
        "summary": {
            "zh": "已有日志源接入——LogQL 查询已适配，日志检索 / 错误画像直接可用。",
            "en": "Hook up your existing log source — LogQL queries are supported, so log search and error profiling work out of the box.",
        },
        "steps": [
            {"zh": "确认 Loki API 对千枢可达（默认 :3100）",
             "en": "Make sure the Loki API is reachable from QianShu (default :3100)"},
            {"zh": "「新增数据源」选 Loki，填地址",
             "en": "In 'Add data source' pick Loki and fill in the URL"},
            {"zh": "「测试连通」取到版本后保存",
             "en": "Click 'Test connectivity' and save once a version is returned"},
        ],
        "endpoint": "",
        "notes": [
            {"zh": "查询走 LogQL（/loki/api/v1/query_range）",
             "en": "Queries run via LogQL (/loki/api/v1/query_range)"},
            {"zh": "多租户环境在「鉴权头」填 X-Scope-OrgID",
             "en": "In multi-tenant setups put X-Scope-OrgID in the 'Auth header' field"},
        ],
        "test_hint": {
            "zh": "「新增数据源」弹窗里点「测试连通」——取 /loki/api/v1/status/buildinfo 版本（旧版退 /ready）",
            "en": "Click 'Test connectivity' in the 'Add data source' dialog — it fetches the /loki/api/v1/status/buildinfo version (older builds fall back to /ready)",
        },
        "testable": False,
    },
    "otlp": {
        "name": "OpenTelemetry", "category": "metric", "icon": "Connection",
        "summary": {
            "zh": "应用 / Agent 按 OTLP 标准上报，零改造进千枢（compose 自带 otel-collector，指标自动落 VictoriaMetrics）。",
            "en": "Your apps / agents report over standard OTLP and land in QianShu with zero code changes (the compose stack ships otel-collector; metrics land in VictoriaMetrics automatically).",
        },
        "steps": [
            {"zh": "应用配置 OTLP exporter：endpoint=http://{host}:4318（HTTP）或 :4317（gRPC）",
             "en": "Configure an OTLP exporter in your app: endpoint=http://{host}:4318 (HTTP) or :4317 (gRPC)"},
            {"zh": "OTel SDK 示例见下方代码块（endpoint 换成你的千枢地址）",
             "en": "See the code block below for an OTel SDK example (swap in your QianShu address as the endpoint)"},
            {"zh": "指标经 otel-collector 自动写入 VictoriaMetrics（无需改千枢）",
             "en": "Metrics are written to VictoriaMetrics by otel-collector automatically (nothing to change in QianShu)"},
            {"zh": "trace 一期落 span 指标；全量 trace 分析是明确产品边界（可接 SkyWalking 告警）",
             "en": "Traces land as span metrics in this release; full trace analysis is a deliberate product boundary (route SkyWalking alerts in instead)"},
        ],
        "endpoint": "http://{host}:4318",
        "code_blocks": [{
            "title": {"zh": "Java SDK 示例", "en": "Java SDK example"},
            "code": 'OtlpHttpSpanExporter.builder()\n    .setEndpoint("http://{host}:4318")\n    .build()',
        }],
        "notes": [
            {"zh": "标准 OTLP 协议，任何语言 SDK 直通",
             "en": "Standard OTLP protocol — SDKs in any language work as-is"},
            {"zh": "OTLP 端口（4317 gRPC / 4318 HTTP）需对应用网络可达——按千枢部署机的实际地址填",
             "en": "The OTLP ports (4317 gRPC / 4318 HTTP) must be reachable from your apps — use the real address of the QianShu host"},
        ],
        "test_hint": {
            "zh": "推送型接入：上报后在「监控仪表盘」能看到 otel_* 指标即接通（无拉取式测试按钮）",
            "en": "Push-based integration: once data is reported, otel_* metrics appearing on the Monitoring dashboard means it is connected (there is no pull-based test button)",
        },
        "testable": False,
    },
    "k8s": {
        "name": {"zh": "Kubernetes 集群", "en": "Kubernetes cluster"},
        "category": "metric", "icon": "Grid",
        "summary": {
            "zh": "以只读 ServiceAccount 接入集群：资源清单、容器日志（含已销毁 Pod 历史）、失败分析全进千枢。",
            "en": "Connect a cluster with a read-only ServiceAccount: resource inventory, container logs (including history of destroyed Pods) and failure analysis all flow into QianShu.",
        },
        "steps": [
            {"zh": "集群创建只读 ServiceAccount：kubectl create serviceaccount qianshu-viewer -n kube-system",
             "en": "Create a read-only ServiceAccount in the cluster: kubectl create serviceaccount qianshu-viewer -n kube-system"},
            {"zh": "下发最小权限（见下方 YAML，整段 kubectl apply -f）：内置 view + nodes 只读 + pods/exec（终端可选）",
             "en": "Grant the minimal permissions (the YAML below — kubectl apply -f the whole thing): built-in view + read-only nodes + pods/exec (terminal, optional)"},
            {"zh": "取 Token（见下方命令块，含 1.24+ 与旧版两种）",
             "en": "Fetch the token (see the command block below — covers both 1.24+ and older versions)"},
            {"zh": "API 地址：kubectl cluster-info（形如 https://<master>:6443）",
             "en": "API address: kubectl cluster-info (looks like https://<master>:6443)"},
            {"zh": "回到本页「接入集群」填入——连通测试通过后点接入",
             "en": "Come back to this page, click 'Connect cluster' and fill them in — connect once the connectivity test passes"},
            {"zh": "接入后自动生效：资源清单 10 分钟同步 / 容器日志 2 分钟采集 / 失败自动分析",
             "en": "Once connected it works automatically: resource inventory syncs every 10 minutes / container logs are collected every 2 minutes / failures are analyzed automatically"},
        ],
        "endpoint": "",
        "code_blocks": [{
            "title": {"zh": "ClusterRole/Binding YAML（整段 kubectl apply -f）",
                      "en": "ClusterRole/Binding YAML (kubectl apply -f the whole thing)"},
            "code": {
                "zh": """# 千枢只读接入：ServiceAccount + 最小权限（view + nodes + pods/exec）
apiVersion: v1
kind: ServiceAccount
metadata:
  name: qianshu-viewer
  namespace: kube-system
---
# ① 内置 view：Pod/Service/Ingress/Deployment/ConfigMap/Namespace 等只读
#    注意：view 不含 nodes、不含 secrets
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: qianshu-viewer-view
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: view
subjects:
  - kind: ServiceAccount
    name: qianshu-viewer
    namespace: kube-system
---
# ② 补 view 不含的部分：nodes 只读（节点清单/详情必需）+ pods/exec（终端）
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: qianshu-viewer-ext
rules:
  # nodes：内置 view 不含 nodes，缺此段节点拉取失败（403）
  - apiGroups: [""]
    resources: ["nodes"]
    verbs: ["get", "list", "watch"]
  # pods/exec：仅「进入容器」终端需要；不用终端可删除本条
  - apiGroups: [""]
    resources: ["pods/exec"]
    verbs: ["create"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: qianshu-viewer-ext
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: qianshu-viewer-ext
subjects:
  - kind: ServiceAccount
    name: qianshu-viewer
    namespace: kube-system""",
                "en": """# QianShu read-only onboarding: ServiceAccount + minimal permissions (view + nodes + pods/exec)
apiVersion: v1
kind: ServiceAccount
metadata:
  name: qianshu-viewer
  namespace: kube-system
---
# (1) Built-in view: read-only on Pod/Service/Ingress/Deployment/ConfigMap/Namespace, etc.
#     Note: view does NOT cover nodes or secrets
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: qianshu-viewer-view
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: view
subjects:
  - kind: ServiceAccount
    name: qianshu-viewer
    namespace: kube-system
---
# (2) Covers what view misses: read-only nodes (required for node list/detail) + pods/exec (terminal)
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: qianshu-viewer-ext
rules:
  # nodes: built-in view has no nodes; without this block node fetches fail with 403
  - apiGroups: [""]
    resources: ["nodes"]
    verbs: ["get", "list", "watch"]
  # pods/exec: only needed for the 'exec into container' terminal; delete this rule if unused
  - apiGroups: [""]
    resources: ["pods/exec"]
    verbs: ["create"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: qianshu-viewer-ext
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: qianshu-viewer-ext
subjects:
  - kind: ServiceAccount
    name: qianshu-viewer
    namespace: kube-system""",
            },
        }, {
            "title": {"zh": "取 Token 命令", "en": "Fetch the token"},
            "code": {
                "zh": """# 1.24+（推荐）
kubectl create token qianshu-viewer -n kube-system --duration=8760h
# 1.23 及以下（从 SA secret 取）
SECRET=$(kubectl -n kube-system get sa qianshu-viewer -o jsonpath='{.secrets[0].name}') && \\
kubectl -n kube-system get secret $SECRET -o jsonpath='{.data.token}' | base64 -d""",
                "en": """# 1.24+ (recommended)
kubectl create token qianshu-viewer -n kube-system --duration=8760h
# 1.23 and older (read from the SA secret)
SECRET=$(kubectl -n kube-system get sa qianshu-viewer -o jsonpath='{.secrets[0].name}') && \\
kubectl -n kube-system get secret $SECRET -o jsonpath='{.data.token}' | base64 -d""",
            },
        }],
        "notes": [
            {"zh": "最小权限清单（只读，逐项）：① 内置 view——Pod/Service/Ingress/Deployment/ConfigMap/Namespace 等只读；"
                   "② nodes get/list/watch——view 不含 nodes，缺此段节点拉取 403；③ pods/exec——仅「进入容器」终端需要，不需要可删",
             "en": "Minimal permission list (all read-only): (1) built-in view — read-only on Pod/Service/Ingress/Deployment/ConfigMap/Namespace, etc.; "
                   "(2) nodes get/list/watch — view has no nodes, without this block node fetches return 403; (3) pods/exec — only for the 'exec into container' terminal, delete if unneeded"},
            {"zh": "view 不含 secrets：千枢 Secret 明细走管理面通道，不依赖 SA",
             "en": "view does not cover secrets: QianShu reads Secret details through its management-plane channel, not the SA"},
            {"zh": "权限面只读——千枢不做删除 / 写变更（写路径人工门）",
             "en": "The permission surface is read-only — QianShu performs no deletes / write changes (write paths sit behind a human gate)"},
        ],
        "test_hint": {
            "zh": "「接入集群」对话框里点「连通测试」——返回集群版本信息即通",
            "en": "Click 'Test connectivity' in the 'Connect cluster' dialog — a cluster version in the response means it is connected",
        },
        "testable": False,
    },
    "zabbix": {
        "name": "Zabbix", "category": "metric", "icon": "Monitor",
        "ds_type": "zabbix",
        "summary": {
            "zh": "已有 Zabbix 监控直接接入：探活取真实版本，最新监控项值查询已适配（item.get / history.get）。",
            "en": "Plug in your existing Zabbix monitoring: the probe reads the real version, and latest-item-value queries are supported (item.get / history.get).",
        },
        "steps": [
            {"zh": "API 地址填 Zabbix Web 根地址（千枢自动拼 /api_jsonrpc.php，也可直接填完整地址）",
             "en": "Enter the Zabbix web root as the API address (QianShu appends /api_jsonrpc.php automatically; you can also paste the full URL)"},
            {"zh": "认证二选一：API Token（Zabbix 5.2+ 推荐，Users → API tokens 新建只读 Token）或账号 / 密码",
             "en": "Pick one auth method: API token (recommended on Zabbix 5.2+, create a read-only token under Users → API tokens) or username / password"},
            {"zh": "「测试连通」取到版本后保存",
             "en": "Click 'Test connectivity' and save once a version is returned"},
            {"zh": "配置凭证后自动验证查询通道（最新监控项值）",
             "en": "Once credentials are set, the query channel (latest item values) is verified automatically"},
        ],
        "endpoint": "",
        "notes": [
            {"zh": "协议：JSON-RPC 2.0（/api_jsonrpc.php），apiinfo.version 免认证，查询=user.login + item.get + history.get，全部只读",
             "en": "Protocol: JSON-RPC 2.0 (/api_jsonrpc.php); apiinfo.version needs no auth; queries = user.login + item.get + history.get, all read-only"},
            {"zh": "只读最小权限：apiinfo.version + item.get / history.get + user.login——建议建只读角色给 item/host 读",
             "en": "Minimal read-only permissions: apiinfo.version + item.get / history.get + user.login — create a read-only role with item/host read access"},
            {"zh": "Zabbix 默认 guest 仅 apiinfo——不配凭证只能验版本，查不到监控项",
             "en": "Zabbix's default guest role only covers apiinfo — without credentials you can verify the version but not read items"},
        ],
        "test_hint": {
            "zh": "「新增数据源」弹窗里点「测试连通」——调 apiinfo.version 取真实版本，显示「连通 · Zabbix x.y.z」才算通",
            "en": "Click 'Test connectivity' in the 'Add data source' dialog — it calls apiinfo.version for the real version. It counts as connected only when 'Connected · Zabbix x.y.z' appears",
        },
        "testable": False,
    },
    "aliyun": {
        "name": {"zh": "阿里云（深度支持）", "en": "Alibaba Cloud (deep support)"},
        "category": "cloud", "icon": "Cloudy",
        "summary": {
            "zh": "资产 / 指标 / 日志 / 账单 / 审计五面全接——当前深度支持的云。",
            "en": "All five surfaces are integrated — assets / metrics / logs / billing / audit. This is the cloud with first-class support today.",
        },
        "steps": [
            {"zh": "RAM 控制台建只读子账号（权限清单：ECS/RDS/SLB/Redis/CDN/OSS/WAF/ES 只读 + CMS 只读 + SLS 只读 + BSS 只读 + ActionTrail 只读）",
             "en": "Create a read-only RAM sub-account (permissions: read-only ECS/RDS/SLB/Redis/CDN/OSS/WAF/ES + read-only CMS + read-only SLS + read-only BSS + read-only ActionTrail)"},
            {"zh": "千枢「云账号管理」（或首登安装向导）填 AK → 五面探测自动亮灯",
             "en": "Enter the AK under 'Cloud Accounts' in QianShu (or the first-login wizard) → all five probes light up automatically"},
            {"zh": "SLS 日志目标在向导里勾选（SLB 访问日志 / 应用日志 / CDN 日志）；配置唯一入口=安装向导/系统设置，勿手改 .env（手改需执行 sync 入册，冲突对账会留痕）",
             "en": "Pick your SLS log targets in the wizard (SLB access logs / application logs / CDN logs). The only configuration entry is the wizard / System Settings — do not hand-edit .env (run the sync command to register manual edits; conflicts are reconciled with a trace)"},
        ],
        "endpoint": "",
        "notes": [
            {"zh": "只读 AK，写路径永远人工门",
             "en": "Read-only AK — write paths always sit behind a human gate"},
            {"zh": "凭证统一在「云账号管理」维护，数据源弹窗不重复收集 AK",
             "en": "Credentials are maintained centrally under 'Cloud Accounts'; the data-source dialog never asks for an AK again"},
        ],
        "test_hint": {
            "zh": "「云账号管理」里「测试可用性」——五面探测亮灯即接通",
            "en": "Click 'Test availability' under 'Cloud Accounts' — the integration is live once all five probes light up",
        },
        "testable": False,
    },
    "tencent": {
        "name": {"zh": "腾讯云（Prometheus 兼容接入）", "en": "Tencent Cloud (Prometheus-compatible)"},
        "category": "cloud", "icon": "Sunrise",
        "summary": {
            "zh": "注册腾讯云可观测平台的 Prometheus 兼容端点，支持真实只读探活；统一查询和内置告警仍需按下方能力边界使用。",
            "en": "Register a Tencent Cloud Observability Prometheus-compatible endpoint for a real read-only probe; unified queries and built-in alerts remain subject to the capability limits below.",
        },
        "steps": [
            {"zh": "腾讯云可观测 - Prometheus → 实例详情 → HTTP API 地址（形如 https://<region>.prometheus.tencentcloud.com）；"
                   "TEM 等托管形态同理——只要是 Prometheus HTTP API 兼容端点即可注册",
             "en": "Tencent Cloud Observability - Prometheus → instance details → HTTP API address (looks like https://<region>.prometheus.tencentcloud.com); "
                  "managed forms such as TEM work the same way — any Prometheus HTTP API-compatible endpoint can be registered"},
            {"zh": "鉴权：实例的 Basic Auth 账号 / Token（或 API 密钥）",
             "en": "Auth: the instance's Basic Auth user / token (or an API key)"},
            {"zh": "「新增数据源」选 Prometheus，地址填 HTTP API 地址，「鉴权头」填 Authorization（见下方示例）",
             "en": "In 'Add data source' pick Prometheus, paste the HTTP API address, and put Authorization in the 'Auth header' field (see the example below)"},
            {"zh": "「测试连通」取到版本后保存；再用下方 PromQL 冒烟查询验证查询链路（返回 JSON 即通）",
             "en": "Click 'Test connectivity', save once a version is returned; then run the PromQL smoke query below to verify the query path (a JSON response means it works)"},
            {"zh": "当前边界：探活成功不代表已进入平台统一查询；内置告警、巡检、画像和 Ask 诊断仍使用系统内置指标库",
             "en": "Current limit: a successful probe does not register this endpoint for unified queries; built-in alerts, inspections, profiling and Ask diagnosis still use the built-in metric store"},
        ],
        "endpoint": "",
        "code_blocks": [
            {
                "title": {"zh": "鉴权头示例", "en": "Auth header example"},
                "code": "Authorization: Basic <base64(user:pass)>",
            },
            {
                "title": {"zh": "PromQL 冒烟查询（curl）", "en": "PromQL smoke query (curl)"},
                "code": {"zh": 'curl -u <user>:<pass> \\\n  "<HTTP API 地址>/api/v1/query?query=up"',
                         "en": 'curl -u <user>:<pass> \\\n  "<HTTP API address>/api/v1/query?query=up"'},
            },
        ],
        "notes": [
            {"zh": "腾讯云 Prometheus 兼容 Prometheus HTTP API；headers 鉴权已支持（Basic/Bearer 均可）",
             "en": "Tencent Cloud Prometheus is compatible with the Prometheus HTTP API; header-based auth is supported (Basic or Bearer)"},
            {"zh": "探活取 /api/v1/status/buildinfo 版本；腾讯域名（*.prometheus.tencentcloud.com）识别后如实标注「腾讯云可观测 Prometheus 兼容端点」",
             "en": "The probe reads the version from /api/v1/status/buildinfo; Tencent domains (*.prometheus.tencentcloud.com) are recognized and honestly labeled as Tencent Cloud Observability Prometheus-compatible endpoints"},
            {"zh": "指标名/label 原样透传——PromQL 用该实例自身的指标口径写规则即可",
             "en": "Metric names / labels pass through as-is — write rules in PromQL using the instance's own metric conventions"},
        ],
        "test_hint": {
            "zh": "「新增数据源」弹窗里点「测试连通」——取到版本即接通；再跑一次 PromQL 冒烟查询确认查询链路",
            "en": "Click 'Test connectivity' in the 'Add data source' dialog — a returned version means it is connected; run the PromQL smoke query once more to confirm the query path",
        },
        "testable": False,
    },
    "aws": {
        "name": {"zh": "AWS / 火山云（Prometheus 兼容接入）", "en": "AWS / Volcengine (Prometheus-compatible)"},
        "category": "cloud", "icon": "Ship",
        "summary": {
            "zh": "AWS AMP / 火山引擎 TLS 的 Prometheus 兼容端点直接注册进指标源。",
            "en": "Register the Prometheus-compatible endpoints of AWS AMP / Volcengine TLS straight into your metric sources.",
        },
        "steps": [
            {"zh": "AWS AMP（托管 Prometheus）→ 工作区端点 URL + SigV4 代理 / Grafana 代理地址",
             "en": "AWS AMP (managed Prometheus) → workspace endpoint URL + SigV4 proxy / Grafana proxy address"},
            {"zh": "火山引擎：可观测 TLS 的 Prometheus 查询端点（https://tls-beijing.volces.com/api/v1/query 形态）",
             "en": "Volcengine: the Observability TLS Prometheus query endpoint (looks like https://tls-beijing.volces.com/api/v1/query)"},
            {"zh": "「新增数据源」选 Prometheus，地址填端点，「鉴权头」填 Bearer Token（见下方示例）",
             "en": "In 'Add data source' pick Prometheus, paste the endpoint, and put a Bearer token in the 'Auth header' field (see the example below)"},
            {"zh": "云账单 / 资产采集按订单（每云知识包需实测踩坑）",
             "en": "Cloud billing / asset collection ships per order (each cloud's knowledge pack is field-tested first)"},
        ],
        "endpoint": "",
        "code_blocks": [{
            "title": {"zh": "鉴权头示例", "en": "Auth header example"},
            "code": "Authorization: Bearer <token>",
        }],
        "notes": [
            {"zh": "Prometheus HTTP API 兼容入口即插即用；深度采集按订单",
             "en": "Any Prometheus HTTP API-compatible endpoint works out of the box; deeper collection ships per order"},
        ],
        "test_hint": {
            "zh": "「新增数据源」弹窗里点「测试连通」——取到版本即接通",
            "en": "Click 'Test connectivity' in the 'Add data source' dialog — a returned version means it is connected",
        },
        "testable": False,
    },
    "vsphere": {
        "name": {"zh": "vCenter / ESXi（VMware 虚拟化）", "en": "vCenter / ESXi (VMware virtualization)"},
        "category": "cloud", "icon": "Cpu",
        "summary": {
            "zh": "VMware 虚拟化分步接入：①地址与凭证 ②SSL 校验（自签证书）③测试连通取版本 ④完成；支持虚拟机/ESXi 主机清单、指标采集与拓扑识别，虚拟机支持页面开关机/重启。",
            "en": "VMware virtualization onboarding in steps: (1) address and credentials (2) SSL verification (self-signed certs) (3) connectivity test to read the version (4) done. Supports VM/ESXi host inventory, metrics and topology identification; VMs support power on/off/reboot from the UI.",
        },
        "steps": [
            {"zh": "① 地址与凭证：vCenter 填 vCenter 地址（IP / 域名，如 vcenter.corp）+ 数据中心名（可选）；ESXi 直连填宿主机管理地址。"
                   "采集可只用只读账号（vCenter 角色只给 Read-only、授权范围选目标数据中心 / 集群；ESXi 用只读本地账号）；"
                   "要用页面的虚拟机电源按钮，该账号需额外授予最小交互权限（见下方权限说明），不给则点击时明确报权限不足",
             "en": "(1) Address and credentials: for vCenter enter the vCenter address (IP / domain, e.g. vcenter.corp) plus the datacenter name (optional); for direct ESXi enter the host's management address. "
                  "Read-only accounts are enough for collection (on vCenter give only the Read-only role, scoped to the target datacenter / cluster; on ESXi use a read-only local account). "
                  "To use the VM power buttons in the UI, grant that account the minimum interact permissions (see the permission note below); without them the action fails with an explicit permission error"},
            {"zh": "② SSL 校验选项：vCenter/ESXi 默认自签证书——「跳过 SSL 校验（自签证书）」默认开（仅内网可信环境使用）；"
                   "有正规 CA 证书链时可关闭走严格校验",
             "en": "(2) SSL verification: vCenter/ESXi ships self-signed certificates by default — 'Skip SSL verification (self-signed cert)' is on by default (only for trusted internal networks); "
                  "turn it off for strict verification when you have a proper CA certificate chain"},
            {"zh": "③ 测试连通（取版本）：「云账号管理」新增账号选 vCenter/ESXi → 填地址 / 账号 → 点「测试可用性」→ "
                   "返回「版本 x.y.z」= 探活取到真实版本（取到版本才算通）",
             "en": "(3) Connectivity test (reads the version): under 'Cloud Accounts' add an account, pick vCenter/ESXi → fill in the address / credentials → click 'Test availability' → "
                  "'Version x.y.z' in the response means the probe read the real version (a version is required to count as connected)"},
            {"zh": "④ 完成：保存入台账并同步虚拟机/主机清单；拓扑按数据源分版展示（私有云），虚拟机可在运维页开关机/重启",
             "en": "(4) Done: save it into the inventory and sync the VM/host list; topology shows it under the Private Cloud data source, and VMs can be powered on/off/rebooted from the Ops page"},
        ],
        "endpoint": {
            "zh": "千枢「云账号管理」→ 新增账号（vendor=vCenter / ESXi 裸机）；首登向导「自建资产」步也可登记",
            "en": "QianShu 'Cloud Accounts' → add an account (vendor = vCenter / bare ESXi); you can also register it in the 'Self-hosted assets' step of the first-login wizard",
        },
        "notes": [
            {"zh": "权限说明：采集只需只读权限（探活取版本 + 虚拟机/主机清单 + 性能指标）；"
                   "虚拟机页面电源操作需要最小交互权限：VirtualMachine.Interact.PowerOn / PowerOff / Reset "
                   "与 Guest Shutdown / Reboot（vCenter 角色可按数据中心/集群再限定范围）",
             "en": "Permissions: collection needs read-only access only (version probe + VM/host inventory + performance metrics). "
                   "VM power actions from the UI need the minimum interact permissions: VirtualMachine.Interact.PowerOn / PowerOff / Reset "
                   "and Guest Shutdown / Reboot (scope the vCenter role to the target datacenter/cluster)"},
            {"zh": "自签证书安全提示：跳过 SSL 校验仅限内网可信环境（会接受任意证书，含中间人）；公网 / 跨域接入应使用正规证书或保持校验开启",
             "en": "Self-signed certificate warning: skip SSL verification only in trusted internal networks (it accepts any certificate, including man-in-the-middle); public / cross-domain access should use proper certificates or keep verification on"},
            {"zh": "虚拟机电源操作的安全门：逐字符确认实例 ID + 10 分钟实例互斥锁 + 全量审计；"
                   "VMware Tools 运行时优雅关机/重启，未运行时确认框会红色警示「将强制断电」",
             "en": "VM power safety gates: type the instance ID to confirm + a 10-minute per-instance lock + full audit; "
                   "graceful shutdown/reboot when VMware Tools is running, and the dialog warns in red that power will be cut when it is not"},
        ],
        "test_hint": {
            "zh": "「云账号管理」里「测试可用性」——显示「版本 x.y.z」即探活成功；若提示 SSL 校验失败，回表单开「跳过 SSL 校验」再测",
            "en": "Click 'Test availability' under 'Cloud Accounts' — 'Version x.y.z' means the probe succeeded. If it reports an SSL verification failure, go back to the form, turn on 'Skip SSL verification' and test again",
        },
        "testable": False,
    },
    "kvm": {
        "name": {"zh": "KVM / libvirt（虚拟化）", "en": "KVM / libvirt (virtualization)"},
        "category": "cloud", "icon": "Cpu",
        "summary": {
            "zh": "KVM 虚拟化分步接入：填 libvirt URI，探活走只读连接取 libvirt/hypervisor 版本（与 virsh version 同源）；本期支持登记与连通测试，采集能力随版本更新自动启用。",
            "en": "KVM virtualization onboarding in steps: enter the libvirt URI; the probe opens a read-only connection to read the libvirt/hypervisor version (same source as virsh version). This release supports registration and connectivity testing; collection capabilities turn on automatically with version updates.",
        },
        "steps": [
            {"zh": "① 地址与凭证：「云账号管理」→ 新增账号 → 厂商选「KVM (libvirt)」→ 填 libvirt URI（形态见下）；"
                   "账号 / 密码可选（SSH 密钥 / SASL 场景留空）",
             "en": "(1) Address and credentials: 'Cloud Accounts' → add an account → pick vendor 'KVM (libvirt)' → enter the libvirt URI (formats below); "
                  "username / password are optional (leave empty for SSH key / SASL setups)"},
            {"zh": "② URI 形态：qemu+ssh://user@host/system（推荐，SSH 隧道 + 密钥认证）· qemu+tcp://host:16509/system"
                   "（libvirtd TCP 监听 + SASL）· qemu:///system（本机）· esx://host/sdk（ESXi 裸连，vCenter 场景走 vCenter/ESXi 接入）",
             "en": "(2) URI formats: qemu+ssh://user@host/system (recommended, SSH tunnel + key auth) · qemu+tcp://host:16509/system "
                  "(libvirtd TCP listener + SASL) · qemu:///system (local host) · esx://host/sdk (direct ESXi; for vCenter setups use the vCenter/ESXi integration instead)"},
            {"zh": "③ 测试连通（取版本）：点「测试可用性」→ 返回「libvirt x.y.z · QEMU …」= 探活取到版本",
             "en": "(3) Connectivity test (reads the version): click 'Test availability' → 'libvirt x.y.z · QEMU …' in the response means the probe read the version"},
            {"zh": "④ 完成：保存入台账；本期支持登记与连通测试，采集能力（libvirt over ssh/tcp 虚拟机清单）随版本更新自动启用",
             "en": "(4) Done: save it into the inventory. This release supports registration and connectivity testing; collection capabilities (VM inventory over libvirt ssh/tcp) turn on automatically with version updates"},
        ],
        "endpoint": {
            "zh": "千枢「云账号管理」→ 新增账号（vendor=KVM (libvirt)）",
            "en": "QianShu 'Cloud Accounts' → add an account (vendor = KVM (libvirt))",
        },
        "notes": [
            {"zh": "只读账号说明：探活走 libvirt 只读连接（openReadOnly），零变更权限面；qemu+ssh 用 SSH 密钥认证（建议专号 + 只读）；"
                   "qemu+tcp 走 SASL 只读账号，宿主机 libvirtd 侧可用 polkit 限制只读操作",
             "en": "Read-only accounts: the probe opens a read-only libvirt connection (openReadOnly) — zero change permissions; qemu+ssh uses SSH key auth (a dedicated read-only account is recommended); "
                  "qemu+tcp uses a read-only SASL account, and polkit on the host can restrict libvirtd to read-only operations"},
            {"zh": "千枢主机需装 libvirt 客户端（libvirt-python 或 libvirt-clients 的 virsh）才能探活——缺失时测试会明确提示装什么",
             "en": "The QianShu host needs a libvirt client (libvirt-python, or virsh from libvirt-clients) for the probe to work — the test tells you exactly what to install if it is missing"},
            {"zh": "网络方向：千枢主机到 KVM 宿主机的 SSH(22) / libvirt TCP(16509) 可达",
             "en": "Network direction: the QianShu host must reach the KVM host over SSH(22) / libvirt TCP(16509)"},
        ],
        "test_hint": {
            "zh": "「云账号管理」里「测试可用性」——显示「libvirt x.y.z · QEMU …」即探活成功；提示缺客户端则先在千枢主机安装 libvirt-clients 再测",
            "en": "Click 'Test availability' under 'Cloud Accounts' — 'libvirt x.y.z · QEMU …' means the probe succeeded. If it says the client is missing, install libvirt-clients on the QianShu host first and test again",
        },
        "testable": False,
    },
}

CATEGORY_LABELS = {
    "event": {"zh": "事件源", "en": "Event sources"},
    "metric": {"zh": "指标源", "en": "Metric sources"},
    "log": {"zh": "日志源", "en": "Log sources"},
    "cloud": {"zh": "云厂商", "en": "Cloud vendors"},
}


# ---------------- 真实链路证据 ----------------
# 原则：测试只做只读取证——事件源查 change_events 最近真实到达、数据源探版本、
# 通知渠道查凭证完整性+历史发送记录；取不到证据就诚实报「未验证/无法自证」，
# 绝不自发样本事件充"连通"，也绝不自动对外发消息。

EVENT_KEYS = ("gitlab", "gitea", "github", "jenkins", "skywalking", "zipkin")
EVENT_NAMES = {"gitlab": "GitLab", "gitea": "Gitea", "github": "GitHub",
               "jenkins": "Jenkins", "skywalking": "SkyWalking", "zipkin": "Zipkin"}
RECENT_DAYS = 7   # 「真通」证据窗口：近 7 天有真实事件/发送记录
STATUS_TEXT = {"connected": "已连通", "unverified": "未验证", "error": "异常", "disabled": "已停用"}


def _status_label(st: str, lang: str = "zh") -> str:
    """状态标签按 lang 出文（MSG: int.status.*，zh 原文=STATUS_TEXT 值逐字不变）；
    未知状态原样返回（防新增状态被吞）。"""
    from app.core.i18n_msg import t

    return t(f"int.status.{st}", lang) if st in STATUS_TEXT else st


def _event_evidence(db: Session, key: str) -> dict:
    """该接入点的真实事件到达证据（只读查 change_events）。

    webhook 入库时 service=真实源名（gitlab/gitea/github/jenkins/skywalking/zipkin，
    见 events_webhook._ci_source）；历史数据不回洗——旧 gitea 事件曾硬编码入库
    service='gitlab'，继续按 raw 是否含 object_kind 拆分归位
    （GitLab payload 恒带 object_kind，Gitea 不带）。
    """
    from app.models.change_event import ChangeEvent
    from sqlalchemy import or_

    q = db.query(ChangeEvent)
    if key == "gitlab":
        q = q.filter(ChangeEvent.service == "gitlab",
                     ChangeEvent.raw.like('%"object_kind"%'))
    elif key == "gitea":
        q = q.filter(or_(
            ChangeEvent.service == "gitea",   # 新数据：入库已记真实 source
            (ChangeEvent.service == "gitlab")  # 历史数据：硬编码时代的 gitea 事件
            & ~ChangeEvent.raw.like('%"object_kind"%')))
    else:
        q = q.filter(ChangeEvent.service == key)
    total = q.count()
    last = q.order_by(ChangeEvent.event_time.desc()).first()
    cutoff = datetime.now() - timedelta(days=RECENT_DAYS)
    recent = q.filter(ChangeEvent.event_time >= cutoff).count()
    return {"count_total": total, "count_recent": recent,
            "last_at": last.event_time.isoformat(timespec="seconds") if last else None}


def _event_verdict(db: Session, key: str, lang: str = "zh") -> dict:
    """事件源链路判定（三态）：connected=近 N 天真收到事件 / unverified=无证据 / error=配置缺失。
    message 按 lang 出文（MSG: int.event.*，zh 文案保持兼容）。"""
    from app.core.config import settings
    from app.core.i18n_msg import t

    name = EVENT_NAMES.get(key, key)
    ev = _event_evidence(db, key)
    if not settings.EVENT_WEBHOOK_TOKEN:
        return {"ok": False, "verdict": "error",
                "message": t("int.event.token_missing", lang),
                "evidence": ev}
    if ev["count_recent"] > 0:
        return {"ok": True, "verdict": "connected",
                "message": t("int.event.connected", lang, days=RECENT_DAYS,
                             count=ev["count_recent"], last=ev["last_at"]),
                "evidence": ev}
    if ev["count_total"] > 0:
        return {"ok": None, "verdict": "unverified",
                "message": t("int.event.stale", lang, total=ev["count_total"],
                             last=ev["last_at"], days=RECENT_DAYS, name=name),
                "evidence": ev}
    return {"ok": None, "verdict": "unverified",
            "message": t("int.event.none", lang, name=name),
            "evidence": ev}


def _channel_send_evidence(db: Session, channel_name: str) -> dict:
    """渠道真实发送记录（只读查 alert_records.notified_channels——评估器记录的成功送达渠道名）。"""
    from app.models.alert_record import AlertRecord

    q = db.query(AlertRecord).filter(
        AlertRecord.notified_channels.like(f'%"{channel_name}"%'))
    total = q.count()
    last = q.order_by(AlertRecord.fired_at.desc()).first()
    cutoff = datetime.now() - timedelta(days=RECENT_DAYS)
    recent = q.filter(AlertRecord.fired_at >= cutoff).count()
    return {"count_total": total, "count_recent": recent,
            "last_at": last.fired_at.isoformat(timespec="seconds") if last else None}


def _channel_verdict(row, ev: dict, lang: str = "zh") -> dict:
    """通知渠道判定（只读）：不发送任何消息——对外发消息只能用户主动触发（红线）。
    message 按 lang 出文（MSG: int.channel.* / int.problem.*，zh 原文逐字不变）。"""
    from app.core.i18n_msg import t

    problems = []
    from app.core.security import decrypt_secret

    # 邮件渠道已下线：历史 email 行不参与渠道判定/清单，
    # 只保留在表里可删除，避免产品面重新长出已移除的能力。
    try:
        url = decrypt_secret(row.webhook_url_cipher or "")
        if not url.startswith(("http://", "https://")) or not urlsplit(url).hostname:
            problems.append(t("int.problem.bad_webhook", lang))
    except Exception:
        problems.append(t("int.problem.decrypt_fail", lang))
    if not row.enabled:
        return {"ok": None, "verdict": "disabled",
                "message": t("int.channel.disabled", lang),
                "evidence": ev}
    if problems:
        sep = "；" if (lang or "zh") == "zh" else "; "
        return {"ok": False, "verdict": "error",
                "message": t("int.channel.error", lang, problems=sep.join(problems)),
                "evidence": ev}
    if ev["count_recent"] > 0:
        return {"ok": True, "verdict": "connected",
                "message": t("int.channel.connected", lang, days=RECENT_DAYS, last=ev["last_at"]),
                "evidence": ev}
    if ev["count_total"] > 0:
        return {"ok": None, "verdict": "unverified",
                "message": t("int.channel.stale", lang, last=ev["last_at"], days=RECENT_DAYS),
                "evidence": ev}
    return {"ok": None, "verdict": "unverified",
            "message": t("int.channel.none", lang),
            "evidence": ev}


# ---------------- 数据源探测缓存（overview 与 registry 同页双取，别打两轮探测） ----------------
_PROBE_CACHE: dict = {"at": 0.0, "data": None}
_PROBE_TTL = 30.0


def _probe_cached(db: Session) -> dict:
    import time as _time

    now = _time.time()
    if _PROBE_CACHE["data"] is not None and now - _PROBE_CACHE["at"] < _PROBE_TTL:
        return _PROBE_CACHE["data"]
    from app.services.datasource import probe_all

    data = probe_all(db)
    _PROBE_CACHE.update(at=now, data=data)
    return data


# ---------------- 数据源类型教程（新增数据源弹窗：15 类） ----------------
# auth：表单凭证形态（zabbix=账号/Token；headers=鉴权头；sls=走云账号凭证；none=无）
# kind：默认注册类别（es/http 两可，前端允许用户改）

DS_TYPE_GUIDES = {
    "prometheus": {
        "label": "Prometheus", "kind": "metric", "auth": "headers",
        "url_placeholder": "http://<prometheus>:9090",
        "summary": {
            "zh": "注册 Prometheus 指标源并执行真实只读探活；当前不自动接入统一 PromQL 查询或内置告警。",
            "en": "Register a Prometheus metric source and run a real read-only probe; this does not currently enable unified PromQL queries or built-in alerts.",
        },
        "steps": [
            {"zh": "确认 Prometheus HTTP API 对千枢可达（默认 :9090）",
             "en": "Make sure the Prometheus HTTP API is reachable from QianShu (default :9090)"},
            {"zh": "填名称与地址（如 http://<prom>:9090）；云托管入口在「鉴权头」填 Authorization",
             "en": "Fill in the name and URL (e.g. http://<prom>:9090); for managed offerings put Authorization in the 'Auth header' field"},
            {"zh": "点「测试连通」取到版本后保存",
             "en": "Click 'Test connectivity' and save once a version is returned"},
        ],
        "notes": [
            {"zh": "探活取 /api/v1/status/buildinfo 真实版本", "en": "The probe reads the real version from /api/v1/status/buildinfo"},
            {"zh": "当前只完成注册与只读探活；告警、巡检、画像和诊断仍查询系统内置指标库",
             "en": "Currently supports registration and read-only probing only; alerts, inspections, profiling and diagnosis still query the built-in metric store"},
        ],
        "test_hint": {
            "zh": "取 buildinfo 版本——显示「连通 · Prometheus x.y.z」才算通",
            "en": "Reads the buildinfo version — it counts as connected only when 'Connected · Prometheus x.y.z' appears",
        },
    },
    "vm": {
        "label": "VictoriaMetrics", "kind": "metric", "auth": "headers",
        "url_placeholder": "http://<vm>:8428",
        "summary": {
            "zh": "VictoriaMetrics 指标源接入（与 Prometheus API 兼容，同一查询层）。",
            "en": "VictoriaMetrics metric source (API-compatible with Prometheus — one query layer).",
        },
        "steps": [
            {"zh": "确认 VictoriaMetrics HTTP API 对千枢可达（默认 :8428）",
             "en": "Make sure the VictoriaMetrics HTTP API is reachable from QianShu (default :8428)"},
            {"zh": "填名称与地址；鉴权入口在「鉴权头」填",
             "en": "Fill in the name and URL; put any auth in the 'Auth header' field"},
            {"zh": "点「测试连通」取到真实 VM 版本后保存",
             "en": "Click 'Test connectivity' and save once the real VM version is returned"},
        ],
        "notes": [
            {"zh": "探活取 /metrics 的 vm_app_version（自指标，比 buildinfo 的兼容版本号准）",
             "en": "The probe reads vm_app_version from /metrics (its own metric — more accurate than the compatibility version in buildinfo)"},
            {"zh": "千枢自带的 builtin-vm 已内置，无需重复注册",
             "en": "The bundled builtin-vm ships with QianShu — no need to register it again"},
        ],
        "test_hint": {
            "zh": "取 vm_app_version——显示「连通 · VictoriaMetrics x.y.z」才算通",
            "en": "Reads vm_app_version — it counts as connected only when 'Connected · VictoriaMetrics x.y.z' appears",
        },
    },
    "zabbix": {
        "label": "Zabbix", "kind": "metric", "auth": "zabbix",
        "url_placeholder": {"zh": "http://<zabbix>:80（自动拼 /api_jsonrpc.php）",
                            "en": "http://<zabbix>:80 (/api_jsonrpc.php is appended automatically)"},
        "summary": {
            "zh": "已有 Zabbix 监控接入：探活取真实版本，最新监控项值查询已适配（item.get / history.get）。",
            "en": "Plug in your existing Zabbix monitoring: the probe reads the real version, and latest-item-value queries are supported (item.get / history.get).",
        },
        "steps": [
            {"zh": "API 地址填 Zabbix Web 根地址（自动拼 /api_jsonrpc.php，也可直接填完整地址）",
             "en": "Enter the Zabbix web root as the API address (/api_jsonrpc.php is appended automatically; you can also paste the full URL)"},
            {"zh": "认证二选一：API Token（Zabbix 5.2+ 推荐，Users → API tokens 新建只读 Token）或账号 / 密码",
             "en": "Pick one auth method: API token (recommended on Zabbix 5.2+, create a read-only token under Users → API tokens) or username / password"},
            {"zh": "点「测试连通」取到版本后保存；配置凭证后自动验证查询通道",
             "en": "Click 'Test connectivity' and save once a version is returned; once credentials are set the query channel is verified automatically"},
        ],
        "notes": [
            {"zh": "只读最小权限：apiinfo.version + item.get / history.get + user.login",
             "en": "Minimal read-only permissions: apiinfo.version + item.get / history.get + user.login"},
            {"zh": "探活不需要认证；凭证只用于查询通道（Zabbix 默认 guest 仅 apiinfo）",
             "en": "The probe needs no auth; credentials are only used for the query channel (Zabbix's default guest role covers apiinfo only)"},
        ],
        "test_hint": {
            "zh": "调 apiinfo.version 免认证取版本——显示「连通 · Zabbix x.y.z」才算通",
            "en": "Calls apiinfo.version (no auth) for the version — it counts as connected only when 'Connected · Zabbix x.y.z' appears",
        },
    },
    "influxdb": {
        "label": "InfluxDB", "kind": "metric", "auth": "headers",
        "url_placeholder": "http://<influxdb>:8086",
        "summary": {
            "zh": "InfluxDB 接入登记：探活取真实版本；本期支持登记与连通测试，查询 / 采集能力随版本更新自动启用。",
            "en": "InfluxDB registration: the probe reads the real version. This release supports registration and connectivity testing; query / collection capabilities turn on automatically with version updates.",
        },
        "steps": [
            {"zh": "地址填 InfluxDB API（默认 :8086）",
             "en": "Enter the InfluxDB API as the address (default :8086)"},
            {"zh": "开启鉴权的实例在「鉴权头」填 Token（Authorization: Token xxx）",
             "en": "For instances with auth enabled put the token in the 'Auth header' field (Authorization: Token xxx)"},
            {"zh": "点「测试连通」取到版本后保存",
             "en": "Click 'Test connectivity' and save once a version is returned"},
        ],
        "notes": [
            {"zh": "探活走 /api/v2/ping 与 /ping（X-Influxdb-Version 头），/health 兜底",
             "en": "The probe uses /api/v2/ping and /ping (X-Influxdb-Version header), falling back to /health"},
            {"zh": "本期支持登记与连通测试，查询 / 采集能力随版本更新自动启用",
             "en": "This release supports registration and connectivity testing; query / collection capabilities turn on automatically with version updates"},
        ],
        "test_hint": {
            "zh": "取 X-Influxdb-Version——显示「连通 · InfluxDB x.y.z」才算通",
            "en": "Reads X-Influxdb-Version — it counts as connected only when 'Connected · InfluxDB x.y.z' appears",
        },
    },
    "graphite": {
        "label": "Graphite", "kind": "metric", "auth": "headers",
        "url_placeholder": "http://<graphite>:80",
        "summary": {
            "zh": "Graphite 接入登记：探活取真实版本；本期支持登记与连通测试，查询能力随版本更新自动启用。",
            "en": "Graphite registration: the probe reads the real version. This release supports registration and connectivity testing; query capabilities turn on automatically with version updates.",
        },
        "steps": [
            {"zh": "地址填 Graphite-web render API（通常 :80 或 :8080）",
             "en": "Enter the Graphite-web render API as the address (usually :80 or :8080)"},
            {"zh": "点「测试连通」取到版本后保存",
             "en": "Click 'Test connectivity' and save once a version is returned"},
        ],
        "notes": [
            {"zh": "探活取 /metrics/version（退 /version），版本端点未暴露时验 /metrics/find 可达",
             "en": "The probe reads /metrics/version (falling back to /version); if no version endpoint is exposed it verifies /metrics/find is reachable"},
            {"zh": "本期支持登记与连通测试，查询能力随版本更新自动启用",
             "en": "This release supports registration and connectivity testing; query capabilities turn on automatically with version updates"},
        ],
        "test_hint": {
            "zh": "取版本端点——显示「连通 · 版本 x.y.z」才算通",
            "en": "Reads the version endpoint — it counts as connected only when 'Connected · version x.y.z' appears",
        },
    },
    "es": {
        "label": "Elasticsearch", "kind": "metric", "auth": "headers",
        "url_placeholder": "http://<es>:9200",
        "summary": {
            "zh": "Elasticsearch 可作指标源 / 日志源注册：探活取版本与集群名。",
            "en": "Elasticsearch can be registered as a metric source / log source: the probe reads the version and cluster name.",
        },
        "steps": [
            {"zh": "地址填 ES HTTP 端点（默认 :9200）",
             "en": "Enter the ES HTTP endpoint as the address (default :9200)"},
            {"zh": "开启鉴权的集群在「鉴权头」填 Basic/Bearer",
             "en": "For clusters with auth enabled put Basic/Bearer in the 'Auth header' field"},
            {"zh": "点「测试连通」取到版本与集群名后保存",
             "en": "Click 'Test connectivity' and save once the version and cluster name are returned"},
        ],
        "notes": [
            {"zh": "探活 GET / 取 version.number + cluster_name",
             "en": "The probe does GET / for version.number + cluster_name"},
            {"zh": "指标 / 日志两种注册形态按需要选（本期支持登记与连通测试，查询能力随版本更新自动启用）",
             "en": "Pick the metric / log registration form as needed (this release supports registration and connectivity testing; query capabilities turn on automatically with version updates)"},
        ],
        "test_hint": {
            "zh": "取 GET / 版本与集群名——显示「连通 · Elasticsearch x.y.z」才算通",
            "en": "Reads the version and cluster name via GET / — it counts as connected only when 'Connected · Elasticsearch x.y.z' appears",
        },
    },
    "opentsdb": {
        "label": "OpenTSDB", "kind": "metric", "auth": "headers",
        "url_placeholder": "http://<opentsdb>:4242",
        "summary": {
            "zh": "OpenTSDB 接入登记：探活取真实版本。",
            "en": "OpenTSDB registration: the probe reads the real version.",
        },
        "steps": [
            {"zh": "地址填 OpenTSDB API（默认 :4242）",
             "en": "Enter the OpenTSDB API as the address (default :4242)"},
            {"zh": "点「测试连通」取到版本后保存",
             "en": "Click 'Test connectivity' and save once a version is returned"},
        ],
        "notes": [
            {"zh": "探活取 /api/version（退 /api/config）", "en": "The probe reads /api/version (falling back to /api/config)"},
            {"zh": "本期支持登记与连通测试，查询能力随版本更新自动启用",
             "en": "This release supports registration and connectivity testing; query capabilities turn on automatically with version updates"},
        ],
        "test_hint": {
            "zh": "取 /api/version——显示「连通 · 版本 x.y.z」才算通",
            "en": "Reads /api/version — it counts as connected only when 'Connected · version x.y.z' appears",
        },
    },
    "http": {
        "label": {"zh": "自定义 HTTP API", "en": "Custom HTTP API"},
        "kind": "metric", "auth": "headers",
        "url_placeholder": "http://<host>:<port>/<path>",
        "summary": {
            "zh": "非标准监控接口也能登记：GET 配置地址，响应里嗅探版本字段。",
            "en": "Non-standard monitoring endpoints can be registered too: it GETs the configured URL and sniffs the response for a version field.",
        },
        "steps": [
            {"zh": "地址填任意返回 JSON / 文本的 HTTP 端点",
             "en": "Enter any HTTP endpoint that returns JSON / text as the address"},
            {"zh": "需要鉴权的在「鉴权头」填",
             "en": "If auth is required put it in the 'Auth header' field"},
            {"zh": "点「测试连通」按响应判定（响应含版本字段 = 连通并显示版本）",
             "en": "Click 'Test connectivity' — the verdict comes from the response (a version field in the response means connected, with the version shown)"},
        ],
        "notes": [
            {"zh": "探活 GET 配置地址：响应体嗅探 version / ver / build 字段",
             "en": "The probe GETs the configured URL and sniffs the body for version / ver / build fields"},
            {"zh": "HTTP 5xx 按不通；其余状态码如实报",
             "en": "HTTP 5xx counts as not connected; other status codes are reported as-is"},
            {"zh": "本期支持登记与连通测试，查询能力随版本更新自动启用",
             "en": "This release supports registration and connectivity testing; query capabilities turn on automatically with version updates"},
        ],
        "test_hint": {
            "zh": "GET 配置地址嗅探版本字段——有版本显示「连通 · 版本 x.y.z」",
            "en": "GETs the configured URL and sniffs for a version field — with one, it shows 'Connected · version x.y.z'",
        },
    },
    "loki": {
        "label": "Loki", "kind": "log", "auth": "headers",
        "url_placeholder": "http://<loki>:3100",
        "summary": {
            "zh": "Grafana Loki 日志源接入（LogQL 查询已适配，日志检索 / 错误画像直接可用）。",
            "en": "Grafana Loki log source (LogQL queries are supported — log search and error profiling work out of the box).",
        },
        "steps": [
            {"zh": "确认 Loki API 对千枢可达（默认 :3100）",
             "en": "Make sure the Loki API is reachable from QianShu (default :3100)"},
            {"zh": "填名称与地址；多租户在「鉴权头」填 X-Scope-OrgID",
             "en": "Fill in the name and URL; in multi-tenant setups put X-Scope-OrgID in the 'Auth header' field"},
            {"zh": "点「测试连通」取到版本后保存",
             "en": "Click 'Test connectivity' and save once a version is returned"},
        ],
        "notes": [
            {"zh": "查询走 LogQL（/loki/api/v1/query_range）", "en": "Queries run via LogQL (/loki/api/v1/query_range)"},
            {"zh": "探活取 /loki/api/v1/status/buildinfo（旧版退 /ready）",
             "en": "The probe reads /loki/api/v1/status/buildinfo (older builds fall back to /ready)"},
        ],
        "test_hint": {
            "zh": "取 buildinfo 版本——显示「连通 · Loki x.y.z」才算通",
            "en": "Reads the buildinfo version — it counts as connected only when 'Connected · Loki x.y.z' appears",
        },
    },
    "sls": {
        "label": {"zh": "阿里云 SLS", "en": "Alibaba Cloud SLS"},
        "kind": "log", "auth": "sls",
        "url_placeholder": "https://<region>.log.aliyuncs.com",
        "summary": {
            "zh": "SLS 日志源走阿里云凭证（云账号管理里的 AK）：探活列可见 project 数。",
            "en": "The SLS log source uses your Alibaba Cloud credentials (the AK stored under Cloud Accounts): the probe lists how many projects are visible.",
        },
        "steps": [
            {"zh": "先在「云账号管理」配置阿里云只读 AK（SLS 只读权限）",
             "en": "First configure a read-only Alibaba Cloud AK under 'Cloud Accounts' (with read-only SLS permissions)"},
            {"zh": "地址填 SLS endpoint（如 https://cn-hangzhou.log.aliyuncs.com）",
             "en": "Enter the SLS endpoint as the address (e.g. https://cn-hangzhou.log.aliyuncs.com)"},
            {"zh": "点「测试连通」列出可见 project 数即通",
             "en": "Click 'Test connectivity' — the count of visible projects means it is connected"},
        ],
        "notes": [
            {"zh": "凭证不在本表单配置——统一走云账号管理（一处配置多处复用）",
             "en": "Credentials are not entered in this form — they live under 'Cloud Accounts' (configure once, reuse everywhere)"},
            {"zh": "探活 = ListProject（只读）；查询适配器已实现",
             "en": "The probe is ListProject (read-only); the query adapter is implemented"},
        ],
        "test_hint": {
            "zh": "调 SLS ListProject——显示「连通 · 可见 project N 个」",
            "en": "Calls SLS ListProject — shows 'Connected · N visible projects'",
        },
    },
    "cls": {
        "label": {"zh": "腾讯云 CLS", "en": "Tencent Cloud CLS"},
        "kind": "log", "auth": "cls",
        "url_placeholder": {"zh": "https://cls.<region>.tencentcloudapi.com（region 从域名取）",
                            "en": "https://cls.<region>.tencentcloudapi.com (region is read from the host)"},
        "summary": {
            "zh": "腾讯云日志服务 CLS 检索接入：走腾讯云凭证（云账号管理里的 AK），探活列可见日志主题数，日志检索已适配。",
            "en": "Tencent Cloud Log Service search: it uses your Tencent Cloud credentials (the AK stored under Cloud Accounts); the probe lists how many log topics are visible, and log search is supported.",
        },
        "steps": [
            {"zh": "先在「云账号管理」配置腾讯云只读 AK（CLS 只读权限：DescribeTopics/SearchLog）",
             "en": "First configure a read-only Tencent Cloud AK under 'Cloud Accounts' (with read-only CLS permissions: DescribeTopics / SearchLog)"},
            {"zh": "地址填 CLS 区域端点（如 https://cls.ap-guangzhou.tencentcloudapi.com）——region 按域名解析",
             "en": "Enter the CLS regional endpoint as the address (e.g. https://cls.ap-guangzhou.tencentcloudapi.com) — the region is parsed from the host"},
            {"zh": "点「测试连通」列出可见日志主题数即通",
             "en": "Click 'Test connectivity' — the count of visible log topics means it is connected"},
        ],
        "notes": [
            {"zh": "凭证不在本表单配置——统一走云账号管理（一处配置多处复用，与 SLS 同款）",
             "en": "Credentials are not entered in this form — they live under 'Cloud Accounts' (configure once, reuse everywhere; same as SLS)"},
            {"zh": "探活 = DescribeTopics（只读）；查询适配器已实现（SearchLog 检索/SQL 分析，TC3 签名纯标准库）",
             "en": "The probe is DescribeTopics (read-only); the query adapter is implemented (SearchLog search / SQL analysis, TC3 signing in pure stdlib)"},
        ],
        "test_hint": {
            "zh": "调 CLS DescribeTopics——显示「连通 · 可见日志主题 N 个」",
            "en": "Calls CLS DescribeTopics — shows 'Connected · N visible log topics'",
        },
    },
    "graylog": {
        "label": "Graylog", "kind": "log", "auth": "headers",
        "url_placeholder": "http://<graylog>:9000",
        "summary": {
            "zh": "Graylog 接入登记：探活验证 API 可达与版本。",
            "en": "Graylog registration: the probe verifies the API is reachable and reads the version.",
        },
        "steps": [
            {"zh": "地址填 Graylog API（默认 :9000）",
             "en": "Enter the Graylog API as the address (default :9000)"},
            {"zh": "开启鉴权的在「鉴权头」填 Token",
             "en": "If auth is enabled put the token in the 'Auth header' field"},
            {"zh": "点「测试连通」取到版本后保存",
             "en": "Click 'Test connectivity' and save once a version is returned"},
        ],
        "notes": [
            {"zh": "探活取 /api 版本字段（cluster_id / node_id 签名识别）",
             "en": "The probe reads the version fields from /api (identified by the cluster_id / node_id signature)"},
            {"zh": "401/403 只说明地址可达，结果标为「未验证」；需补有效鉴权后重测，不算已连通",
             "en": "A 401/403 only proves address reachability; the result is 'Unverified', not connected — provide valid authentication and test again"},
            {"zh": "本期支持登记与连通测试，查询能力随版本更新自动启用",
             "en": "This release supports registration and connectivity testing; query capabilities turn on automatically with version updates"},
        ],
        "test_hint": {
            "zh": "取 /api 版本字段——显示「连通 · 版本 x.y.z」才算通",
            "en": "Reads the version fields from /api — it counts as connected only when 'Connected · version x.y.z' appears",
        },
    },
    "filebeat": {
        "label": "Filebeat", "kind": "log", "auth": "headers",
        "url_placeholder": "http://<filebeat>:5066",
        "summary": {
            "zh": "Filebeat 采集端登记：探活验证 HTTP 监控端点与版本。",
            "en": "Filebeat shipper registration: the probe verifies the HTTP monitoring endpoint and reads the version.",
        },
        "steps": [
            {"zh": "Filebeat 开 HTTP 监控端点（http.enabled: true，默认 :5066）",
             "en": "Enable the Filebeat HTTP endpoint (http.enabled: true, default :5066)"},
            {"zh": "地址填监控端点（如 http://<host>:5066）",
             "en": "Enter the monitoring endpoint as the address (e.g. http://<host>:5066)"},
            {"zh": "点「测试连通」取到版本后保存",
             "en": "Click 'Test connectivity' and save once a version is returned"},
        ],
        "notes": [
            {"zh": "探活取 / /state / /stats 的 version 字段",
             "en": "The probe reads the version field from / /state / /stats"},
            {"zh": "本期支持登记与连通测试，查询能力随版本更新自动启用——先登记台账",
             "en": "This release supports registration and connectivity testing; query capabilities turn on automatically with version updates — register it into the inventory first"},
        ],
        "test_hint": {
            "zh": "取监控端点 version 字段——显示「连通 · 版本 x.y.z」才算通",
            "en": "Reads the version field from the monitoring endpoint — it counts as connected only when 'Connected · version x.y.z' appears",
        },
    },
    "skywalking": {
        "label": {"zh": "SkyWalking（APM）", "en": "SkyWalking (APM)"},
        "kind": "log", "auth": "headers",
        "url_placeholder": "http://<oap>:12800",
        "summary": {
            "zh": "SkyWalking 服务目录接入：服务清单进资产与拓扑（trace 不存，全量分析不做）。",
            "en": "SkyWalking service catalog integration: the service list feeds assets and topology (traces are not stored; full analysis is out of scope).",
        },
        "steps": [
            {"zh": "地址填 SkyWalking OAP（默认 :12800）",
             "en": "Enter the SkyWalking OAP address (default :12800)"},
            {"zh": "点「测试连通」列出服务数即通",
             "en": "Click 'Test connectivity' — the service count means it is connected"},
            {"zh": "告警接入走事件源的 SkyWalking / Zipkin 告警卡（webhook）",
             "en": "For alerts use the SkyWalking / Zipkin alert card under event sources (webhook)"},
        ],
        "notes": [
            {"zh": "探活走 GraphQL getAllServices（只读）", "en": "The probe calls GraphQL getAllServices (read-only)"},
            {"zh": "服务清单进资产（product=apm_service）与拓扑 middleware 层",
             "en": "The service list feeds assets (product=apm_service) and the topology middleware layer"},
            {"zh": "全量 trace 不存——产品边界", "en": "Full traces are not stored — product boundary"},
        ],
        "test_hint": {
            "zh": "列服务目录——显示「连通 · N 个服务」",
            "en": "Lists the service catalog — shows 'Connected · N services'",
        },
    },
    "zipkin": {
        "label": {"zh": "Zipkin（APM）", "en": "Zipkin (APM)"},
        "kind": "log", "auth": "headers",
        "url_placeholder": "http://<zipkin>:9411",
        "summary": {
            "zh": "Zipkin 服务目录接入：服务清单进资产与拓扑。",
            "en": "Zipkin service catalog integration: the service list feeds assets and topology.",
        },
        "steps": [
            {"zh": "地址填 Zipkin API（默认 :9411）",
             "en": "Enter the Zipkin API address (default :9411)"},
            {"zh": "点「测试连通」列出服务数即通",
             "en": "Click 'Test connectivity' — the service count means it is connected"},
        ],
        "notes": [
            {"zh": "探活走 GET /api/v2/services（只读）", "en": "The probe calls GET /api/v2/services (read-only)"},
            {"zh": "全量 trace 不存——产品边界", "en": "Full traces are not stored — product boundary"},
        ],
        "test_hint": {
            "zh": "列服务目录——显示「连通 · N 个服务」",
            "en": "Lists the service catalog — shows 'Connected · N services'",
        },
    },
}


def _localize(value, lang: str):
    """双语载体按语言取文（str/URL 等语言无关值透传），再做占位填充的前置步。"""
    from app.core.i18n_msg import pick

    return pick(value, lang)


def _render_guide(key: str, base: str, lang: str = "zh") -> dict:
    """统一五段渲染：按 Accept-Language 选文 → 占位替换；GUIDES 优先，纯数据源类型键退 DS_TYPE_GUIDES。"""
    if key in GUIDES:
        out = copy.deepcopy(GUIDES[key])
    elif key in DS_TYPE_GUIDES:
        ds = DS_TYPE_GUIDES[key]
        out = {"name": ds.get("label", key),
               "category": "metric" if ds.get("kind") == "metric" else "log",
               "icon": "DataLine", "ds_type": key,
               "summary": ds.get("summary", ""), "steps": list(ds.get("steps") or []),
               "endpoint": "", "notes": list(ds.get("notes") or []),
               "code_blocks": [], "test_hint": ds.get("test_hint", ""), "testable": False}
    else:
        raise HTTPException(404, f"无此集成: {key}")
    for f in ("name", "summary", "endpoint", "test_hint"):
        out[f] = _localize(out.get(f, ""), lang)
    for f in ("summary", "endpoint", "test_hint"):
        if isinstance(out.get(f), str):
            out[f] = _fill(out[f], base)
    out["steps"] = [_fill(_localize(s, lang), base) for s in out.get("steps") or []]
    out["notes"] = [_fill(_localize(s, lang), base) for s in out.get("notes") or []]
    out["code_blocks"] = [{"title": _localize(c["title"], lang),
                           "code": _fill(_localize(c["code"], lang), base)}
                          for c in out.get("code_blocks") or []]
    out.setdefault("endpoint", "")
    out.setdefault("notes", [])
    out.setdefault("code_blocks", [])
    out.setdefault("testable", False)
    out["key"] = key
    out["base_url"] = base
    out["webhook_url"] = (f"{base}/api/events/webhook/{key}"
                          if out.get("category") == "event" else "")
    return out


@router.get("/overview")
def integrations_overview(request: Request, db: Session = Depends(get_db),
                          user: User = Depends(get_current_user)):
    """集成总览：webhook 接入点 + 各源状态（事件源附真实到达证据）。
    教程摘要/类别名按 Accept-Language 出文。"""
    from app.core.config import settings

    base = _base_url_of(request)
    lang = lang_of(request)
    webhook_base = f"{base}/api/events/webhook"
    ds = _probe_cached(db)
    return {
        "webhook": {
            "base_url": webhook_base,
            "token_configured": bool(settings.EVENT_WEBHOOK_TOKEN),
            "endpoints": {k: f"{webhook_base}/{k}" for k in ("gitlab", "gitea", "github", "jenkins", "skywalking", "zipkin")},
        },
        "datasources": ds,
        # 事件源真实链路证据（卡片状态用；只读查 change_events，不再拿"token 已配置"当连通）
        "event_status": {k: _event_verdict(db, k, lang) for k in EVENT_KEYS},
        "guides": [{"key": k, "name": _localize(g["name"], lang), "category": g["category"],
                    "icon": g.get("icon", ""), "ds_type": g.get("ds_type", ""),
                    "summary": _localize(g["summary"], lang)} for k, g in GUIDES.items()],
        "category_labels": {k: _localize(v, lang) for k, v in CATEGORY_LABELS.items()},
    }


@router.get("/guide/{key}")
def integrations_guide(key: str, request: Request,
                       user: User = Depends(get_current_user)):
    """单项接入教程（统一五段；{base_url}/{webhook_url}/{host} 按请求头动态生成；按 Accept-Language 出文）。"""
    return _render_guide(key, _base_url_of(request), lang_of(request))


@router.get("/ds-guides")
def integrations_ds_guides(request: Request,
                           user: User = Depends(get_current_user)):
    """数据源类型教程（新增数据源弹窗用）：15 类的这是什么 / 步骤 / 注意事项 / 测试说明（按 Accept-Language 出文）。"""
    base = _base_url_of(request)
    lang = lang_of(request)
    out = {}
    for typ, g in DS_TYPE_GUIDES.items():
        row = copy.deepcopy(g)
        for f in ("label", "summary", "url_placeholder", "test_hint"):
            row[f] = _localize(row.get(f, ""), lang)
        for f in ("summary", "url_placeholder", "test_hint"):
            if isinstance(row.get(f), str):
                row[f] = _fill(row[f], base)
        row["steps"] = [_fill(_localize(s, lang), base) for s in row.get("steps") or []]
        row["notes"] = [_fill(_localize(s, lang), base) for s in row.get("notes") or []]
        out[typ] = row
    return out


@router.post("/test/{key}")
def integrations_test(key: str, request: Request, db: Session = Depends(get_db),
                      user: User = Depends(require_roles("admin", "operator"))):
    """事件源链路自检：只读查真实事件到达证据，不自发样本事件。

    返回三态 verdict：connected=近 7 天真收到事件（真通）/ unverified=无证据（无法自证，
    引导从源端发一条）/ error=配置缺失（token 未配）。数据源类在「新增数据源」弹窗 /
    集成管理行内测试（真实版本探测）。
    """
    if key not in EVENT_KEYS:
        raise HTTPException(400, "此集成不支持链路自检（数据源类在「新增数据源」弹窗里测试连通——真实版本探测）")
    return _event_verdict(db, key, lang_of(request))


@router.post("/check-channel/{channel_id}")
def integrations_check_channel(channel_id: int, request: Request, db: Session = Depends(get_db),
                               user: User = Depends(require_roles("admin", "operator"))):
    """通知渠道只读自检（红线：绝不自动对外发消息——测试消息只能用户主动触发）。

    只能验证：凭证可解密、webhook 地址形态合法、历史真实发送记录。
    真实送达无法自证时如实说明（message 按 Accept-Language 出文）。
    """
    from app.models.alert_channel import AlertChannel

    row = db.get(AlertChannel, channel_id)
    if row is None or row.type == "email":
        # 邮件渠道已下线：历史行不出现在清单，也不允许单独自检（与列表口径一致）
        raise HTTPException(404, "渠道不存在")
    ev = _channel_send_evidence(db, row.name)
    out = _channel_verdict(row, ev, lang_of(request))
    out["channel"] = {"id": row.id, "name": row.name, "type": row.type, "enabled": row.enabled}
    return out


# ---------------- 集成管理：已接入集成全量清单 + 真实状态 ----------------
# 状态全部有真实数据支撑：事件源=change_events 到达证据 / 数据源=版本探测 /
# 渠道=alert_records 发送记录 / K8s=探测回写 status+synced_at / 云账号=status+last_test_at。
# 没有证据就是「未验证」——不许摆设状态。

def _registry_rows(request: Request, db: Session) -> list:
    from app.core.config import settings
    from app.core.i18n_msg import pick, t
    from app.core.security import decrypt_secret
    from app.models.alert_channel import AlertChannel
    from app.models.cloud_account import CloudAccount
    from app.models.k8s import K8sCluster
    from app.services.cloud.provider import vendor_label
    from app.services.datasource import log_sources, metric_sources

    rows = []
    base = _base_url_of(request)
    lang = lang_of(request)   # 行内 status_text/detail/last_data_label/category_label 按 Accept-Language 出文

    # 1) 事件源 ×6（固定接入点；状态=真实事件到达证据）
    for key in EVENT_KEYS:
        v = _event_verdict(db, key, lang)
        ev = v.get("evidence") or {}
        rows.append({
            "id": f"event:{key}", "category": "event",
            "category_label": pick(CATEGORY_LABELS["event"], lang),
            "name": EVENT_NAMES[key], "type": key,
            "endpoint": f"{base}/api/events/webhook/{key}",
            "status": v["verdict"], "status_text": _status_label(v["verdict"], lang),
            "detail": v["message"], "last_data_at": ev.get("last_at"),
            "last_data_label": t("int.last.event", lang) if ev.get("last_at") else "",
            "enabled": True, "builtin": False, "ref_id": key, "cfg_kind": "",
            "editable": False, "testable": True, "removable": False,
        })

    # 2) 数据源（注册表+真实版本探测；builtin-vm 随部署内置）
    ds = _probe_cached(db)
    probes = {(p.get("name"), p.get("type")): p
              for p in (ds.get("metrics") or []) + (ds.get("logs") or [])}
    for cfg_kind, sources, cat, label in (
            ("metric_sources", metric_sources(), "metric", "指标源"),
            ("log_sources", log_sources(), "log", "日志源")):
        for s in sources:
            p = probes.get((s.get("name"), s.get("type"))) or {}
            if p.get("pending") or p.get("ok") is None:
                st = "unverified"
            elif p.get("ok"):
                st = "connected"
            else:
                st = "error"
            builtin = bool(s.get("type") == "vm" and s.get("name") == "builtin-vm")
            rows.append({
                "id": f"ds:{cfg_kind}:{s.get('name')}", "category": cat,
                "category_label": pick(CATEGORY_LABELS[cat], lang),
                "name": s.get("name"), "type": s.get("type"),
                "endpoint": s.get("url") or "（内置/本机）",
                "status": st, "status_text": _status_label(st, lang),
                "detail": p.get("detail") or t("int.detail.ds_not_probed", lang),
                "last_data_at": None, "last_data_label": "",
                "enabled": True, "builtin": builtin,
                "ref_id": s.get("name"), "cfg_kind": cfg_kind,
                "editable": not builtin, "testable": True, "removable": not builtin,
            })

    # 3) 通知渠道（状态=历史真实发送记录；停用开关真实生效——评估器只推 enabled 渠道）
    for ch in (db.query(AlertChannel)
               .filter(AlertChannel.type != "email")   # 邮件渠道已下线，历史行不进清单
               .order_by(AlertChannel.id).all()):
        ev = _channel_send_evidence(db, ch.name)
        v = _channel_verdict(ch, ev, lang)
        try:
            url = decrypt_secret(ch.webhook_url_cipher or "")
            parts = urlsplit(url)
            endpoint = f"{parts.scheme}://{parts.netloc}{parts.path}" if parts.hostname else "(解密失败)"
        except Exception:
            endpoint = "(解密失败)"
        rows.append({
            "id": f"channel:{ch.id}", "category": "channel",
            "category_label": pick(EXTRA_CATEGORY_LABELS["channel"], lang),
            "name": ch.name, "type": ch.type, "endpoint": endpoint,
            "status": v["verdict"], "status_text": _status_label(v["verdict"], lang),
            "detail": v["message"], "last_data_at": ev.get("last_at"),
            "last_data_label": t("int.last.send", lang) if ev.get("last_at") else "",
            "enabled": bool(ch.enabled), "builtin": False, "ref_id": ch.id, "cfg_kind": "",
            "editable": True, "testable": True, "removable": False,
        })

    # 4) K8s 集群（状态=连通测试回写 status/status_note；最近同步=资源采集证据）
    for c in db.query(K8sCluster).order_by(K8sCluster.id).all():
        st = {"ok": "connected", "error": "error"}.get(c.status, "unverified")
        rows.append({
            "id": f"k8s:{c.id}", "category": "k8s",
            "category_label": pick(EXTRA_CATEGORY_LABELS["k8s"], lang),
            "name": c.name, "type": "k8s", "endpoint": c.api_url,
            "status": st, "status_text": _status_label(st, lang),
            "detail": c.status_note or (
                t("int.detail.k8s_last_sync", lang, ts=c.synced_at.isoformat(timespec="seconds"))
                if c.synced_at else t("int.detail.k8s_not_probed", lang)),
            "last_data_at": c.synced_at.isoformat(timespec="seconds") if c.synced_at else None,
            "last_data_label": t("int.last.sync", lang) if c.synced_at else "",
            "enabled": True, "builtin": False, "ref_id": c.id, "cfg_kind": "",
            "editable": True, "testable": True, "removable": True,
        })

    # 5) 云账号（状态=「测试可用性」回写 status/last_test_at）
    for a in db.query(CloudAccount).order_by(CloudAccount.id).all():
        if not a.enabled:
            st = "disabled"
        elif a.status == "ok":
            st = "connected"
        elif a.status == "error":
            st = "error"
        else:
            st = "unverified"
        rows.append({
            "id": f"cloud:{a.id}", "category": "cloud",
            "category_label": pick(EXTRA_CATEGORY_LABELS["cloud"], lang),
            "name": a.name, "type": a.vendor,
            "endpoint": f"{vendor_label(a.vendor)}{(' · ' + a.region) if a.region else ''}",
            "status": st, "status_text": _status_label(st, lang),
            "detail": (t("int.detail.cloud_last_test", lang, ts=a.last_test_at.isoformat(timespec="seconds"))
                       if a.last_test_at else t("int.detail.cloud_not_tested", lang)),
            "last_data_at": a.last_test_at.isoformat(timespec="seconds") if a.last_test_at else None,
            "last_data_label": t("int.last.test", lang) if a.last_test_at else "",
            "enabled": bool(a.enabled), "builtin": False, "ref_id": a.id, "cfg_kind": "",
            "editable": True, "testable": True, "removable": False,
        })
    return rows


# registry 额外类别名（channel/k8s/cloud 三类只在集成管理出现；与 CATEGORY_LABELS 同载体同出口）
EXTRA_CATEGORY_LABELS = {
    "channel": {"zh": "通知渠道", "en": "Notification channels"},
    "k8s": {"zh": "Kubernetes", "en": "Kubernetes"},
    "cloud": {"zh": "云账号", "en": "Cloud accounts"},
}


@router.get("/registry")
def integrations_registry(request: Request, db: Session = Depends(get_db),
                          user: User = Depends(get_current_user)):
    """集成管理：全部已接入集成（事件源/指标源/日志源/通知渠道/K8s/云账号）+ 真实状态。
    category_labels/status_labels/行内 status_text/detail/last_data_label 均按
    Accept-Language 出文，zh 文案保持兼容。"""
    lang = lang_of(request)
    rows = _registry_rows(request, db)
    summary = {"total": len(rows)}
    for st in STATUS_TEXT:
        summary[st] = sum(1 for r in rows if r["status"] == st)
    summary["by_category"] = {}
    for r in rows:
        summary["by_category"].setdefault(r["category"], 0)
        summary["by_category"][r["category"]] += 1
    labels = {**CATEGORY_LABELS, **EXTRA_CATEGORY_LABELS}
    return {"rows": rows, "summary": summary,
            "category_labels": {k: _localize(v, lang) for k, v in labels.items()},
            "status_labels": {st: _status_label(st, lang) for st in STATUS_TEXT}}
