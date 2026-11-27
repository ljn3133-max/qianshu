"""组件类型归一表：产品码 → 厂商中立组件类型。

背景：资产产品码按厂商各表（阿里云 ecs/rds/slb/oss、腾讯云 cvm/cdb/clb/cos、
自建 vmware/kvm …），拓扑/架构识别若按产品码硬编码会漏认他厂资产、同一语义
拆成多个组件。本表把全部产品码归一到「组件类型」语义：
云主机 / 数据库 / 负载均衡 / 对象存储 / CDN / 缓存 / 消息队列 / 搜索引擎 /
容器服务 / WAF / APM —— vendor 中立，同语义同类型。

兼容口径（识别优先级见 services/topology.py::classify_asset）：
  product_role（按产品码显式覆盖，config/components.json 可配）
  > kind_role（组件类型默认，本表语义）> name_rules（主机命名规则）> 应用层兜底
——原有产品码的识别结果不变，新厂商产品码经本表自动获得同一组件类型。

边界：本表只登记「拓扑图节点」资产。`domain`/`ssl_cert` 故意不在此表：
域名节点由实证链路（LinkEdge）生成、证书不是拓扑成员（零边孤岛会刷屏 link_integrity），
它们属于到期总账/证书事实，不要往这里加，否则 build_graph 会产生重复域名节点。
"""
from __future__ import annotations

# 产品码（Asset.product 取值）-> 组件类型。新厂商产品码在此登记即可全局生效。
COMPONENT_KINDS: dict[str, str] = {
    # 云主机（公有云 + 自建虚拟化 + Agent 主机）
    "ecs": "host", "cvm": "host", "host": "host",
    "vm": "host", "vmware_vm": "host", "vmware_host": "host",
    "kvm_vm": "host", "esxi_vm": "host",
    # 数据库
    "rds": "db", "cdb": "db", "db": "db",
    "tc_mongodb": "db", "tc_postgres": "db",
    # 负载均衡
    "slb": "lb", "clb": "lb", "lb": "lb",
    # 对象存储
    "oss": "object_storage", "cos": "object_storage",
    "obs": "object_storage", "s3": "object_storage",
    # 缓存
    "redis": "cache", "crs": "cache", "memcached": "cache",
    # CDN
    "cdn": "cdn", "tc_cdn": "cdn",
    # 消息队列
    "kafka": "mq", "rocketmq": "mq", "tdmq": "mq", "ons": "mq", "rabbitmq": "mq",
    # 搜索/大数据
    "es": "search", "tc_es": "search",
    # 容器服务
    "ack": "k8s", "tke": "k8s", "k8s": "k8s",
    # 安全
    "waf": "waf",
    # APM
    "apm_service": "apm",
}

# 组件类型 -> 展示名（vendor 中立；产品码级自定义走 config product_role）
KIND_LABELS: dict[str, str] = {
    "host": "云主机",
    "db": "数据库",
    "lb": "负载均衡",
    "object_storage": "对象存储",
    "cache": "缓存",
    "cdn": "CDN",
    "mq": "消息队列",
    "search": "搜索引擎",
    "k8s": "容器服务",
    "waf": "WAF",
    "apm": "APM 服务",
}

# 组件类型 -> 架构角色（与 topology.ROLE_ORDER 对齐）
KIND_ROLES: dict[str, str] = {
    "host": "app",
    "db": "db",
    "lb": "gateway",
    "object_storage": "db",
    "cache": "middleware",
    "cdn": "gateway",
    "mq": "middleware",
    "search": "bigdata",
    "k8s": "app",
    "waf": "gateway",
    "apm": "middleware",
}


def component_kind(product: str) -> str:
    """产品码 -> 组件类型；未登记的产品码返回 ''（走原有兜底，不误归类）。"""
    return COMPONENT_KINDS.get((product or "").strip().lower(), "")


def kind_label(kind: str) -> str:
    return KIND_LABELS.get(kind, kind or "")
