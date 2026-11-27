"""云厂商抽象：资产/运维模块化多云的 Provider 层。

设计约束：
1. CloudProvider 基类定义厂商无关接口（测试连通/同步/资源清单/电源操作），
   新厂商只加一个 Provider 子类 + 注册表一行，API/前端零改动；
2. AlibabaProvider 不重写采集——把 app/collector/ 既有阿里云采集逻辑包一层
   （经 collector/credential.py 的账号凭证上下文注入多账号凭证，采集器无感）；
3. 腾讯云通过 tencentcloud-sdk-python 支持 CVM 探活与清单同步，并支持
   CDB/Redis(CRS)/CLB/COS/CDN/MongoDB/ES/PostgreSQL 清单同步（COS 使用
   cos-python-sdk-v5）。入库走 base.upsert_asset + mark_missing_deleted 统一口径，
   清单同步均带清扫守卫；bill_day/account_balance 委托 collector/tc_bss.py。
   未实现厂商保留保守骨架，不支持的能力明确抛 NotImplementedError。
4. ESXi/vCenter 通过 services/vsphere.py 探活取版本（SSL 自签证书 verify 参数贯通），
   并通过 pyvmomi 只读采集虚拟机与主机清单（带清扫守卫）；KVM/libvirt 通过
   services/kvm.py 探活，未实现的清单能力如实说明。

产品类型枚举 RESOURCE_TYPES 与统一资产模型 Asset.product 取值对齐
（云主机/数据库/负载均衡/对象存储/CDN…），list_resources 直查资产表。
"""
from __future__ import annotations

import json
import logging
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models.asset import Asset
from app.models.cloud_account import CloudAccount

logger = logging.getLogger("qianshu.cloud.provider")

# 产品类型枚举：资产模型 product 取值 -> 云产品类型（展示/资源类型白名单共用）
# CVM 使用独立的 cvm，不复用 ecs：ecs 值被阿里云专属
# 采集链路（CMS 指标/关机检测/电源操作/清扫）按 product='ecs' 直取，混入他厂实例会
# 误查/误报/误清扫；vendor 维度由 CloudAccount.account_id 打标区分。
# 腾讯云其余产品同理不复用阿里云 product 值（cdb/crs/clb/cos）：
# rds/redis/slb/oss 被阿里云专属链路（DAS 慢SQL/空间、SLS 巡检、拓扑）按值直取。
# tc_cdn/tc_mongodb/tc_es/tc_postgres 统一带 tc_ 前缀：
# cdn/es 已是阿里云专属值（cdn/es 采集器按值直取），mongodb/postgres 无阿里云对应值
# 但同前缀统一避让；与 cvm/cdb/crs/clb/cos 的"避开阿里云专属值"取法一致。
RESOURCE_TYPES: dict[str, str] = {
    "ecs": "云主机",
    "cvm": "CVM 云主机",
    "rds": "数据库",
    "cdb": "CDB 数据库",
    "redis": "缓存",
    "crs": "Redis 缓存",
    "slb": "负载均衡",
    "clb": "CLB 负载均衡",
    "oss": "对象存储",
    "cos": "COS 对象存储",
    "cdn": "CDN",
    "waf": "WAF",
    "ack": "容器服务",
    "es": "搜索引擎",
    "tc_cdn": "腾讯 CDN",
    "tc_mongodb": "腾讯 MongoDB",
    "tc_es": "腾讯 Elasticsearch",
    "tc_postgres": "腾讯 PostgreSQL",
    "vmware_vm": "VMware 虚拟机",
    "vmware_host": "VMware 主机",
}


@dataclass(frozen=True)
class VendorCapability:
    """厂商能力位：UI/准入/体检/同步共用唯一事实源。

    集中描述"能不能测试/能不能采集/有没有账单"，避免多处硬编码厂商清单随能力变化
    产生口径漂移。本类只描述事实，档位由能力位推导，不单独维护 `tier` 字段，
    避免两个来源互相漂移。

    默认值是最保守的 register_only：新厂商漏配能力位时只会"少承诺"，
    不会把未实现的能力误显示为可用。
    """

    register: bool = True    # 可登记账号
    probe: bool = False      # test_account 有真实现（探活）
    sync: bool = False       # sync 真实产出资产
    billing: bool = False    # 有账单/成本采集
    power: bool = False      # 有电源操作
    metrics: bool = False    # 有云指标采集通道（CMS/CloudMonitor 等；vSphere perf/Agent 另计）
    agent: bool = False      # 资产经 Agent 上报，无云凭证语义
    needs_ak_sk: bool = True  # 是否要求 AK/SK 成对校验

    @property
    def tier(self) -> str:
        """能力档位（前端分组/文案用，由能力位推导）。"""
        if self.agent:
            return "agent"
        if not self.probe and not self.sync:
            return "register_only"
        if self.probe and not self.sync:
            return "probe_only"
        if self.sync and not self.billing:
            return "asset_sync"
        return "asset_billing"


# 最保守档：仅登记（既不能探活也不能采集）——骨架厂商的默认值
REGISTER_ONLY = VendorCapability()


class CloudProvider:
    """厂商无关的云管理接口。account 为 CloudAccount 模型行（测试/同步场景由 API 层解密后传 ak/sk）。"""

    name: str = ""      # 厂商标识（入库 vendor 值）
    label: str = ""     # 展示名（UI 徽标/摘要）
    # 能力位（子类按真实实现声明；基类最保守，漏配只会少承诺）
    capability: VendorCapability = REGISTER_ONLY
    # 该厂商支持采集的产品类型（子类按 SDK 能力裁剪）
    supported_resource_types: tuple = tuple(RESOURCE_TYPES)

    def test_account(self, account: CloudAccount, ak: str, sk: str) -> dict:
        """连通性测试（只读 API）。返回 {ok, message}。"""
        raise NotImplementedError(f"{self.label}：本期仅支持登记，连通测试与采集能力随版本更新自动启用")

    def sync(self, db: Session, account: CloudAccount,
             products: Optional[list] = None) -> list:
        """按账号全量同步资产。products 为空=同步该厂商全部支持产品。

        返回 [{product, total, created, updated, missing_marked, failed_regions, ...}]。
        """
        raise NotImplementedError(f"{self.label}：本期仅支持登记，连通测试与采集能力随版本更新自动启用")

    def list_resources(self, db: Session, resource_type: str,
                       account: Optional[CloudAccount] = None) -> list:
        """资源清单：统一资产模型直查（全厂商共用口径，子类一般无需覆写）。"""
        q = (
            db.query(Asset)
            .filter(Asset.product == resource_type, Asset.status != "Deleted")
        )
        if account is not None:
            q = q.filter(Asset.account_id == account.id)
        return [
            {
                "instance_id": r.instance_id, "name": r.name, "region": r.region,
                "zone": r.zone, "status": r.status, "product": r.product,
            }
            for r in q.order_by(Asset.name).all()
        ]

    def power_action(self, account: CloudAccount, asset: Asset, action: str,
                     ak: str, sk: str) -> dict:
        """电源操作（reboot/stop/start）。危险操作的确认门/互斥锁/审计在 API 层（ecs_ops.py）。"""
        raise NotImplementedError(f"{self.label}：本期仅支持登记，连通测试与采集能力随版本更新自动启用")

    def fetch_forward_rules(self, account: CloudAccount, ak: str, sk: str) -> list:
        """负载均衡转发规则清单（只读），供链路边规则感知使用。

        厂商无关返回形态（link_draft 消费，禁止消费方写厂商专属分支）：
        [{rule_id, rule_name, domain, url, backends: ["ip:port", ...], vgroup_name,
          lb_id, lb_name, listener_port, address_type: internet/intranet/"",
          vendor, region}]
        无采集能力的厂商返回 []（不抛——链路草稿主数据是 SLS 日志，规则只是标注）。
        """
        return []


class AlibabaProvider(CloudProvider):
    """阿里云：包装 app/collector/ 既有采集逻辑（不重写采集，包一层）。"""

    name = "aliyun"
    label = "阿里云"
    capability = VendorCapability(probe=True, sync=True, billing=True,
                                  power=True, metrics=True)
    supported_resource_types = ("ecs", "rds", "redis", "slb", "oss", "cdn", "waf", "ack", "es")

    def test_account(self, account: CloudAccount, ak: str, sk: str) -> dict:
        """只读连通测试：ECS DescribeRegions（零副作用，凭证有效即返回区域数）。"""
        from alibabacloud_ecs20140526 import models as ecs
        from alibabacloud_ecs20140526.client import Client
        from alibabacloud_tea_openapi.models import Config

        region = account.region or "cn-hangzhou"
        client = Client(Config(access_key_id=ak, access_key_secret=sk, region_id=region))
        resp = client.describe_regions(ecs.DescribeRegionsRequest())
        n = len(resp.body.regions.region or [])
        return {"ok": True, "message": f"凭证有效，可见区域 {n} 个"}

    def sync(self, db: Session, account: CloudAccount,
             products: Optional[list] = None) -> list:
        """按账号跑既有采集器。凭证经 account_credential 上下文注入——
        resolve_aliyun_credential 优先取上下文，采集器代码零改动即按账号取密。
        """
        from app.collector.credential import account_credential

        ak = sk = ""
        if account.ak_cipher and account.secret_cipher:
            from app.core.security import decrypt_secret

            ak = decrypt_secret(account.ak_cipher)
            sk = decrypt_secret(account.secret_cipher)
        wanted = [p for p in ALIYUN_SYNC_HANDLERS if not products or p in products]
        results = []
        # 账号无密钥（过渡期旧数据）时不注入上下文，走默认凭证通道
        ctx = (account_credential(ak, sk, account.id, source=f"cloud_account:{account.name}")
               if ak and sk else nullcontext())
        with ctx:
            for product in wanted:
                try:
                    results.append({"product": product, **ALIYUN_SYNC_HANDLERS[product](db)})
                except Exception as e:  # 单产品失败不阻断其余产品
                    logger.exception("账号 %s 产品 %s 同步失败", account.name, product)
                    results.append({"product": product, "ok": False, "error": str(e)[:200]})
        return results

    def power_action(self, account: CloudAccount, asset: Asset, action: str,
                     ak: str, sk: str) -> dict:
        """电源操作 SDK 下发。审计入口仍走 /assets/{id}/actions（ecs_ops.py），
        本方法是 Provider 层能力位，供后续 ecs_ops 收口复用。"""
        from alibabacloud_ecs20140526 import models as ecs
        from alibabacloud_ecs20140526.client import Client
        from alibabacloud_tea_openapi.models import Config

        client = Client(Config(access_key_id=ak, access_key_secret=sk,
                               region_id=asset.region or "cn-hangzhou"))
        if action == "reboot":
            client.reboot_instance(ecs.RebootInstanceRequest(instance_id=asset.instance_id, force_stop=False))
            expected = "Rebooting"
        elif action == "stop":
            client.stop_instance(ecs.StopInstanceRequest(instance_id=asset.instance_id, force_stop=False))
            expected = "Stopping"
        elif action == "start":
            client.start_instance(ecs.StartInstanceRequest(instance_id=asset.instance_id))
            expected = "Starting"
        else:
            raise ValueError(f"不支持的操作: {action}")
        return {"ok": True, "action": action, "instance_id": asset.instance_id,
                "expected_status": expected}

    def fetch_forward_rules(self, account: CloudAccount, ak: str, sk: str) -> list:
        """阿里 SLB URL 转发规则（只读）：DescribeRules + DescribeVServerGroups 系。

        每个 HTTP/HTTPS 监听的转发规则（domain+url -> 虚拟服务器组），后端组成员走
        DescribeVServerGroupAttribute（SDK 方法名 describe_vserver_group_attribute）。
        范围=账号主区域（account.region）——规则标注
        场景不全区域扫，跨区域账号在主区域外的规则由日志路径维度兜底。
        失败逐层降级（单 LB/单监听失败不断整体）。
        """
        from alibabacloud_slb20140515 import models as slbm

        region = account.region or "cn-hangzhou"
        client = _slb_client(ak, sk, region)
        out: list = []
        vgroup_cache: dict = {}

        def _members(vgroup_id: str) -> list:
            if not vgroup_id:
                return []
            if vgroup_id in vgroup_cache:
                return vgroup_cache[vgroup_id]
            members: list = []
            try:
                # SDK 方法名是 describe_vserver_group_attribute（模型类却叫
                # DescribeVServerGroupAttributeRequest——方法/模型命名不一致，
                # 以实际 SDK 签名为准，勿凭文档猜）
                va = client.describe_vserver_group_attribute(
                    slbm.DescribeVServerGroupAttributeRequest(
                        vserver_group_id=vgroup_id)).body
                for b in ((va.backend_servers.backend_server if va.backend_servers else None) or []):
                    ip = getattr(b, "server_ip", "") or ""
                    port = getattr(b, "port", None)
                    if ip and port:
                        members.append(f"{ip}:{port}")
            except Exception as e:
                logger.warning("SLB 虚拟服务器组 %s 成员拉取失败: %s", vgroup_id, str(e)[:80])
            vgroup_cache[vgroup_id] = members
            return members

        lbs: list = []
        page = 1
        while True:
            body = client.describe_load_balancers(
                slbm.DescribeLoadBalancersRequest(page_number=page, page_size=100)).body
            batch = list((body.load_balancers.load_balancer if body.load_balancers else None) or [])
            lbs.extend(batch)
            if not batch or len(lbs) >= (body.total_count or 0):
                break
            page += 1

        for lb in lbs:
            lb_id = getattr(lb, "load_balancer_id", "") or ""
            lb_name = getattr(lb, "load_balancer_name", "") or lb_id
            address_type = getattr(lb, "address_type", "") or ""
            try:
                listeners = (client.describe_load_balancer_listeners(
                    slbm.DescribeLoadBalancerListenersRequest(
                        load_balancer_id=lb_id)).body.listeners or [])
            except Exception as e:
                logger.warning("SLB %s 监听拉取失败: %s", lb_id, str(e)[:80])
                continue
            for ln in listeners:
                proto = (getattr(ln, "listener_protocol", "") or "").lower()
                port = getattr(ln, "listener_port", None)
                if proto not in ("http", "https") or not port:
                    continue
                try:
                    rules = client.describe_rules(
                        slbm.DescribeRulesRequest(
                            load_balancer_id=lb_id, listener_port=port,
                            listener_protocol=proto.upper())).body.rules
                except Exception as e:
                    logger.warning("SLB %s:%s 转发规则拉取失败: %s", lb_id, port, str(e)[:80])
                    continue
                for rule in ((rules.rule if rules else None) or []):
                    vgid = getattr(rule, "vserver_group_id", "") or ""
                    out.append({
                        "vendor": self.name, "region": region,
                        "lb_id": lb_id, "lb_name": lb_name,
                        "listener_port": port,
                        "rule_id": getattr(rule, "rule_id", "") or "",
                        "rule_name": getattr(rule, "rule_name", "") or "",
                        "domain": getattr(rule, "domain", "") or "",
                        "url": getattr(rule, "url", "") or "",
                        "vgroup_name": vgid,
                        "backends": _members(vgid),
                        "address_type": address_type,
                    })
        return out


class _StubProvider(CloudProvider):
    """未实现厂商的保守骨架：接口保持稳定，具体 SDK 能力由子类实现。

    新厂商接入三步：继承本类 -> 实现四个方法 -> 注册表加一行。
    能力位保持最保守的「仅登记」，实现对应能力后必须在子类显式升级能力位。
    """

    capability = REGISTER_ONLY

    def test_account(self, account, ak, sk):
        raise NotImplementedError(f"{self.label}：本期仅支持登记，连通测试与采集能力随版本更新自动启用")

    def sync(self, db, account, products=None):
        raise NotImplementedError(f"{self.label}：本期仅支持登记，连通测试与采集能力随版本更新自动启用")

    def power_action(self, account, asset, action, ak, sk):
        raise NotImplementedError(f"{self.label}：本期仅支持登记，连通测试与采集能力随版本更新自动启用")


class TencentProvider(CloudProvider):
    """腾讯云 Provider，基于 tencentcloud-sdk-python。

    已接产品：云主机 CVM（探活 DescribeInstances + 全量清单同步）、云数据库 CDB
    （cdb.DescribeDBInstances）、缓存 Redis/CRS（redis.DescribeInstances）、
    负载均衡 CLB（clb.DescribeLoadBalancers）、对象存储 COS（cos-python-sdk-v5
    list_buckets）、CDN 域名（cdn.DescribeDomains）、MongoDB
    （mongodb.DescribeDBInstances）、ES（es.DescribeInstances）、PostgreSQL
    （postgres.DescribeDBInstances）。TDSQL/MySQL 商业版与 cdb 同 API（见
    TENCENT_SYNC_HANDLERS 处注释）不重复实现。

    入库口径与阿里云一致：各实例落统一资产模型（product='cvm'/'cdb'/'crs'/'clb'/'cos'/
    'tc_cdn'/'tc_mongodb'/'tc_es'/'tc_postgres'，不复用 ecs/rds/redis/slb/oss/cdn/es——
    避免阿里云专属链路误查误扫，cvm 同款理由），经 base.upsert_asset /
    mark_missing_deleted 入库与清扫，账号维度由 account_credential 上下文打标
    （Asset.account_id）。
    账单采集：bill_day / account_balance 委托 collector/tc_bss.py
    （BSS DescribeBillDetail/DescribeAccountBalance，只读），与阿里 collector/bss.py
    同口径入 cost_daily（source='tc_bill'）。
    """

    name = "tencent"
    label = "腾讯云"
    # 指标走 Prometheus 兼容端点，不直采 CMS；电源操作未接
    capability = VendorCapability(probe=True, sync=True, billing=True)
    # 产品类型：云主机/数据库/缓存/负载均衡/对象存储/CDN/MongoDB/ES/PostgreSQL
    supported_resource_types = ("cvm", "cdb", "crs", "clb", "cos",
                                "tc_cdn", "tc_mongodb", "tc_es", "tc_postgres")

    def test_account(self, account: CloudAccount, ak: str, sk: str) -> dict:
        """只读连通测试：CVM DescribeInstances 探活（Limit=1 零副作用，凭证有效即返回）。"""
        from tencentcloud.common import credential as tencent_credential
        from tencentcloud.cvm.v20170312 import cvm_client, models

        region = account.region or "ap-guangzhou"
        client = cvm_client.CvmClient(tencent_credential.Credential(ak, sk), region)
        req = models.DescribeInstancesRequest()
        req.Limit = 1
        req.Offset = 0
        resp = client.DescribeInstances(req)
        return {"ok": True, "message": f"凭证有效，可见实例 {resp.TotalCount or 0} 台"}

    def sync(self, db: Session, account: CloudAccount,
             products: Optional[list] = None) -> list:
        """按账号同步 CVM 实例清单入 assets（product='cvm'）。

        凭证经 account_credential 上下文注入——upsert_asset 打标 / mark_missing_deleted
        清扫范围自动收口到本账号（与 AlibabaProvider 同机制，采集器无感）。
        products 为空=同步该厂商全部支持产品；显式产品过滤无匹配时返回空结果。
        """
        from app.collector.credential import account_credential

        ak = sk = ""
        if account.ak_cipher and account.secret_cipher:
            from app.core.security import decrypt_secret

            ak = decrypt_secret(account.ak_cipher)
            sk = decrypt_secret(account.secret_cipher)
        wanted = [p for p in TENCENT_SYNC_HANDLERS if not products or p in products]
        if not wanted:
            return []
        results = []
        # CVM 必须有密钥才可采集（自建/过渡期无密钥账号直接报错更明确）
        if not (ak and sk):
            return [{"product": p, "ok": False,
                     "error": "腾讯云账号未配置 AK/SK（云账号管理中补录后重试）"} for p in wanted]
        ctx = account_credential(ak, sk, account.id, source=f"cloud_account:{account.name}")
        with ctx:
            for product in wanted:
                try:
                    results.append({"product": product,
                                    **TENCENT_SYNC_HANDLERS[product](db, ak, sk)})
                except Exception as e:  # 单产品失败不阻断其余产品
                    logger.exception("账号 %s 产品 %s 同步失败", account.name, product)
                    results.append({"product": product, "ok": False, "error": str(e)[:200]})
        return results

    def list_resources(self, db: Session, resource_type: str,
                       account: Optional[CloudAccount] = None) -> list:
        """腾讯云资源清单：统一资产模型直查（基类实现），限定本厂商支持的产品类型。"""
        if resource_type not in self.supported_resource_types:
            return []
        return super().list_resources(db, resource_type, account)

    def fetch_forward_rules(self, account: CloudAccount, ak: str, sk: str) -> list:
        """腾讯 CLB 七层转发规则（只读）。

        clb.v20180317 SDK 没有独立的 DescribeForwardRules 方法：规则在
        DescribeListeners 返回的 Rules（RuleOutput：Domain/Url/LocationId），规则级后端
        走 DescribeTargets 的 RuleTargets（按 LocationId 匹配）。范围=账号主区域。
        """
        from tencentcloud.clb.v20180317 import models

        region = account.region or "ap-guangzhou"
        client = _tencent_client("clb", "v20180317", ak, sk, region)
        out: list = []

        def _rule_targets(lb_id: str) -> dict:
            """{LocationId: ["ip:port", ...]}——DescribeTargets 的规则级后端。"""
            req = models.DescribeTargetsRequest()
            req.LoadBalancerId = lb_id
            resp = client.DescribeTargets(req)
            out_map: dict = {}
            for ln in (resp.Listeners or []):
                for rt in (getattr(ln, "Rules", None) or []):
                    members = []
                    for t in (getattr(rt, "Targets", None) or []):
                        port = getattr(t, "Port", None)
                        ips = list(getattr(t, "PrivateIpAddresses", None) or [])
                        ip = ips[0] if ips else (getattr(t, "EniIp", "") or "")
                        if ip and port:
                            members.append(f"{ip}:{port}")
                    out_map[getattr(rt, "LocationId", "") or ""] = members
            return out_map

        lbs: list = []
        offset = 0
        while True:
            req = models.DescribeLoadBalancersRequest()
            req.Offset, req.Limit = offset, 100
            resp = client.DescribeLoadBalancers(req)
            batch = resp.LoadBalancerSet or []
            lbs.extend(batch)
            offset += len(batch)
            if not batch or offset >= (resp.TotalCount or 0):
                break

        for lb in lbs:
            lb_id = getattr(lb, "LoadBalancerId", "") or ""
            lb_name = getattr(lb, "LoadBalancerName", "") or lb_id
            address_type = {"OPEN": "internet", "INTERNAL": "intranet"}.get(
                getattr(lb, "LoadBalancerType", "") or "", "")
            try:
                lreq = models.DescribeListenersRequest()
                lreq.LoadBalancerId = lb_id
                listeners = client.DescribeListeners(lreq).Listeners or []
            except Exception as e:
                logger.warning("CLB %s 监听拉取失败: %s", lb_id, str(e)[:80])
                continue
            try:
                targets = _rule_targets(lb_id)
            except Exception as e:
                logger.warning("CLB %s 规则后端拉取失败: %s", lb_id, str(e)[:80])
                targets = {}
            for ln in listeners:
                proto = (getattr(ln, "Protocol", "") or "").upper()
                if proto not in ("HTTP", "HTTPS"):
                    continue
                for rule in (getattr(ln, "Rules", None) or []):
                    loc = getattr(rule, "LocationId", "") or ""
                    out.append({
                        "vendor": self.name, "region": region,
                        "lb_id": lb_id, "lb_name": lb_name,
                        "listener_port": getattr(ln, "Port", None),
                        "rule_id": loc,
                        "rule_name": "",   # CLB 规则无名字，消费方回退 domain+url
                        "domain": getattr(rule, "Domain", "") or "",
                        "url": getattr(rule, "Url", "") or "",
                        "vgroup_name": "",
                        "backends": targets.get(loc, []),
                        "address_type": address_type,
                    })
        return out


    def bill_day(self, ak: str, sk: str, day) -> list:
        """一天的账单明细（只读，BSS DescribeBillDetail）——委托 collector/tc_bss.py。"""
        from app.collector.tc_bss import fetch_day_bill

        return fetch_day_bill(ak, sk, day)

    def account_balance(self, ak: str, sk: str) -> dict:
        """账户余额（只读，BSS DescribeAccountBalance）——采集状态旁注用。"""
        from app.collector.tc_bss import fetch_account_balance

        return fetch_account_balance(ak, sk)


class VolcengineProvider(_StubProvider):
    name = "volc"
    label = "火山引擎"


class BaiduProvider(_StubProvider):
    name = "baidu"
    label = "百度智能云"


class HuaweiProvider(_StubProvider):
    name = "huawei"
    label = "华为云"


class AwsProvider(_StubProvider):
    name = "aws"
    label = "AWS"


class AzureProvider(_StubProvider):
    name = "azure"
    label = "Azure"


class OnpremProvider(CloudProvider):
    """自建（IDC/物理机）：无云 API——资产经 Agent 上报/机器库登记，测试即登记确认。"""

    name = "onprem"
    label = "自建"
    capability = VendorCapability(probe=True, agent=True, needs_ak_sk=False)

    def test_account(self, account: CloudAccount, ak: str, sk: str) -> dict:
        return {"ok": True, "message": "自建资产无需云凭证（Agent 上报/机器库登记）"}

    def sync(self, db: Session, account: CloudAccount,
             products: Optional[list] = None) -> list:
        """自建无云清单可拉：Agent 上报是数据入口，这里返回空结果并说明。"""
        return [{"product": "host", "ok": True, "total": 0, "created": 0, "updated": 0,
                 "missing_marked": 0, "failed_regions": [],
                 "note": "自建资产经 Agent 上报，无需云同步"}]


def sync_vmware_vms(db: Session, account: CloudAccount, user: str, password: str) -> dict:
    """vCenter/ESXi 虚拟机清单同步（pyvmomi 只读采集）：product='vmware_vm'。

    清扫守卫（与 sync_cvm 同款）：采集失败=清单不可信，跳过缺失清扫
    （sweep_skipped=True），防把整片 VM 误标 Deleted；成功才收口清扫。
    """
    from app.collector.base import mark_missing_deleted, upsert_asset
    from app.services.vsphere import _conn_params, collect_inventory

    rows = collect_inventory(account, user, password)
    seen_ids: set = set()
    created = updated = 0
    for r in rows:
        fields = {
            "name": r.get("name") or "",
            "status": r.get("status") or "",
            # vCenter 地址不是"地域"，不写入；VM 所属 ESXi 主机放 zone（详情可见）
            "region": "",
            "zone": (r.get("host_name") or "")[:32],
            "instance_type": f"{r.get('cpu_count') or 0}C{max((r.get('memory_mb') or 0) // 1024, 0)}G",
            "charge_type": "",
            "expire_time": None,
            "tags": "",
            "extra": json.dumps({
                **(r.get("extra") or {}),
                "cpu_count": r.get("cpu_count") or 0,
                "memory_mb": r.get("memory_mb") or 0,
                "guest_os": r.get("guest_os") or "",
                "guest_ip": r.get("guest_ip") or "",
                "tools_status": (r.get("extra") or {}).get("tools_status") or "",
                "disk_total_gb": r.get("disk_total_gb") or 0,
                "disk_count": r.get("disk_count") or 0,
                "disks": r.get("disks") or [],
            }, ensure_ascii=False),
            "raw_meta": "",
            "synced_at": datetime.now(),
            # 归属显式落账号：upsert 只在新建时补 account_id，
            # 历史误归到默认 aliyun 账号的行靠这里在下次同步时纠正
            "account_id": account.id,
        }
        if upsert_asset(db, "vmware_vm", r["instance_id"], seen_ids, fields) == "created":
            created += 1
        else:
            updated += 1

    missing_marked = mark_missing_deleted(db, "vmware_vm", seen_ids)   # 清单已完整取得才清扫
    db.commit()
    logger.info("vmware_vm 同步完成: total=%d created=%d updated=%d deleted=%d",
                len(seen_ids), created, updated, missing_marked)
    return {
        "product": "vmware_vm", "total": len(seen_ids), "created": created,
        "updated": updated, "missing_marked": missing_marked,
        "sweep_skipped": False, "failed_regions": [],
    }


def sync_vmware_hosts(db: Session, account: CloudAccount, user: str, password: str) -> dict:
    """vCenter/ESXi 主机（HostSystem）清单同步：product='vmware_host'。

    清扫守卫（与 sync_vmware_vms 同款）：采集失败=清单不可信，跳过缺失清扫
    （sweep_skipped=True），防把整片主机误标 Deleted；成功才收口清扫。
    """
    from app.collector.base import mark_missing_deleted, upsert_asset
    from app.services.vsphere import _conn_params, collect_host_inventory

    rows = collect_host_inventory(account, user, password)
    seen_ids: set = set()
    created = updated = 0
    for r in rows:
        fields = {
            "name": r.get("name") or "",
            "status": r.get("status") or "",
            # vCenter 地址不是"地域"，不写入；主机所属集群放 zone（详情可见）
            "region": "",
            "zone": ((r.get("extra") or {}).get("cluster") or "")[:32],
            "instance_type": f"{r.get('cpu_count') or 0}C{max((r.get('memory_mb') or 0) // 1024, 0)}G",
            "charge_type": "",
            "expire_time": None,
            "tags": "",
            "extra": json.dumps({
                **(r.get("extra") or {}),
                "cpu_count": r.get("cpu_count") or 0,
                "memory_mb": r.get("memory_mb") or 0,
                "guest_os": r.get("guest_os") or "",
                "mgmt_ip": r.get("mgmt_ip") or "",
                "disk_total_gb": r.get("disk_total_gb") or 0,
                "disk_count": r.get("disk_count") or 0,
            }, ensure_ascii=False),
            "raw_meta": "",
            "synced_at": datetime.now(),
            # 归属显式落账号：同样用于纠正历史误归属，理由见 sync_vmware_vms
            "account_id": account.id,
        }
        if upsert_asset(db, "vmware_host", r["instance_id"], seen_ids, fields) == "created":
            created += 1
        else:
            updated += 1

    missing_marked = mark_missing_deleted(db, "vmware_host", seen_ids)   # 清单已完整取得才清扫
    db.commit()
    logger.info("vmware_host 同步完成: total=%d created=%d updated=%d deleted=%d",
                len(seen_ids), created, updated, missing_marked)
    return {
        "product": "vmware_host", "total": len(seen_ids), "created": created,
        "updated": updated, "missing_marked": missing_marked,
        "sweep_skipped": False, "failed_regions": [],
    }


class _VSphereProvider(_StubProvider):
    """ESXi/vCenter 共用的探活、清单与指标采集实现。

    测试=HTTP 免认证取版本（services/vsphere.py：/sdk/vimServiceVersions.xml + 登录页
    文案）；同步=pyvmomi 只读采集虚拟机+主机清单（sync_vmware_vms/sync_vmware_hosts，
    product='vmware_vm'/'vmware_host'，upsert_asset+mark_missing_deleted 清扫守卫）；
    指标采集见 collector/metrics_job.py run_vsphere_collection
    （VM/主机 cpu/mem/disk -> VictoriaMetrics）。
    SSL 自签证书：extra.ssl_skip=true（表单「跳过 SSL 校验」默认开）-> verify=False；
    关闭时严格校验证书链，失败给人话提示。
    """

    # 资产与指标均自采（vSphere perf 单独通道），无 AK/SK 语义；
    # power=True：虚拟机电源操作经 services/vsphere_ops.py（智能软关）。
    capability = VendorCapability(probe=True, sync=True, power=True, needs_ak_sk=False)
    # 资产类型：vSphere 虚拟机 + ESXi 物理主机（拓扑 component_kinds 识别为 host）
    supported_resource_types = ("vmware_vm", "vmware_host")

    def power_action(self, account: CloudAccount, asset: Asset, action: str,
                     ak: str, sk: str) -> dict:
        """虚拟机电源操作（ak/sk = vSphere 用户名/密码，与 sync 同语义）。

        确认门/互斥锁/审计在 API 层（ecs_ops.py）；本方法只做动作下发。
        ESXi 宿主机（vmware_host）不走此路径——物理断电不在授权范围。
        """
        from app.services import vsphere_ops

        return vsphere_ops.power_action(account, asset, action, username=ak, password=sk)

    def sync(self, db: Session, account: CloudAccount,
             products: Optional[list] = None) -> list:
        """虚拟机/主机清单同步（只读）。凭证=账号表单的只读账号/密码（ak/sk 字段）。

        单产品失败不阻断其余产品（与 TencentProvider.sync 同口径）；采集失败
        如实报错不假装（清扫在失败时天然跳过）。
        """
        from app.collector.credential import account_credential
        from app.core.security import decrypt_secret

        handlers = {"vmware_vm": sync_vmware_vms, "vmware_host": sync_vmware_hosts}
        wanted = [p for p in handlers if not products or p in products]
        if not wanted:
            return []
        user = decrypt_secret(account.ak_cipher) if account.ak_cipher else ""
        password = decrypt_secret(account.secret_cipher) if account.secret_cipher else ""
        results = []
        # 账号上下文：与阿里云/腾讯云同机制——资产打标本账号，
        # 清扫只清本账号。缺此上下文时 vSphere 资产会落到默认 aliyun 账号归属，
        # 总览按账号聚合看不到，多 vCenter 之间还会互相清扫成 Deleted。
        ctx = account_credential(user, password, account.id, source=f"cloud_account:{account.name}")
        with ctx:
            for product in wanted:
                try:
                    results.append(handlers[product](db, account, user, password))
                except Exception as e:  # 采集失败如实报错，不假装（清扫在失败时天然跳过）
                    logger.exception("账号 %s %s 清单同步失败", account.name, product)
                    results.append({"product": product, "ok": False,
                                    "sweep_skipped": True, "failed_regions": [],
                                    "error": str(e)[:200]})
        return results

    def test_account(self, account: CloudAccount, ak: str, sk: str) -> dict:
        from app.services.vsphere import probe_vsphere

        try:
            extra = json.loads(account.extra_json) if account.extra_json else {}
        except ValueError:
            extra = {}
        host = str(extra.get("host") or "").strip() or (account.region or "").strip()
        if not host:
            return {"ok": False,
                    "message": "缺主机地址（表单「主机地址」/ extra.host）"}
        # vCenter/ESXi 默认自签证书：未显式关闭跳过即跳过校验（verify=False）
        ssl_skip = extra.get("ssl_skip", True)
        r = probe_vsphere(host, verify=not ssl_skip)
        return {"ok": r["ok"], "message": r["detail"]}


class EsxiProvider(_VSphereProvider):
    """ESXi 裸机 Provider。

    向导/云账号表单收集主机地址/账号（extra.host）；测试探活取 ESXi 版本（真信息）；
    同步=pyvmomi 只读采集虚拟机/主机清单（vmware_vm / vmware_host）。
    """

    name = "esxi"
    label = "ESXi 裸机"


class VcenterProvider(_VSphereProvider):
    """vCenter 统一管理（收集地址/账号/数据中心，extra.datacenter）。

    测试探活取 vCenter 版本（真信息）；同步=pyvmomi 只读采集虚拟机/主机清单。
    """

    name = "vcenter"
    label = "vCenter"


class KvmProvider(_StubProvider):
    """KVM/libvirt 虚拟化：libvirt URI 探活取版本。

    表单收集 libvirt URI（extra.host，如 qemu+ssh://user@host/system）+ 可选账号/密码；
    测试=只读连接取 libvirt/hypervisor 版本（services/kvm.py：libvirt.openReadOnly
    或 virsh version 兜底）；虚拟机清单采集尚未实现，同步如实返回未接入说明。
    """

    name = "kvm"
    label = "KVM (libvirt)"
    capability = VendorCapability(probe=True, needs_ak_sk=False)

    def test_account(self, account: CloudAccount, ak: str, sk: str) -> dict:
        from app.services.kvm import probe_kvm

        try:
            extra = json.loads(account.extra_json) if account.extra_json else {}
        except ValueError:
            extra = {}
        uri = str(extra.get("host") or "").strip() or (account.region or "").strip()
        if not uri:
            return {"ok": False,
                    "message": "缺 libvirt URI（表单「libvirt URI」，如 qemu+ssh://user@host/system）"}
        r = probe_kvm(uri)
        return {"ok": r["ok"], "message": r["detail"]}

    def sync(self, db: Session, account: CloudAccount,
             products: Optional[list] = None) -> list:
        raise NotImplementedError(
            f"{self.label}：仅探活，虚拟机清单采集未启用（libvirt over ssh/tcp）")


# 注册表：vendor -> Provider 实例（新厂商在此登记）
PROVIDERS: dict[str, CloudProvider] = {
    p.name: p for p in (
        AlibabaProvider(), TencentProvider(), VolcengineProvider(),
        BaiduProvider(), HuaweiProvider(), AwsProvider(), AzureProvider(),
        OnpremProvider(), EsxiProvider(), VcenterProvider(), KvmProvider(),
    )
}


def get_provider(vendor: str) -> CloudProvider:
    provider = PROVIDERS.get(vendor)
    if provider is None:
        raise ValueError(f"未知云厂商: {vendor}（可选: {sorted(PROVIDERS)}）")
    return provider


def vendor_label(vendor: str) -> str:
    provider = PROVIDERS.get(vendor)
    return provider.label if provider else vendor


_UNKNOWN_CAPABILITY = VendorCapability(register=False)


def capabilities_of(vendor: str) -> VendorCapability:
    """厂商能力位（未知厂商一律按"不可登记"处理，避免放任脏 vendor 值）。"""
    provider = PROVIDERS.get(vendor)
    return provider.capability if provider else _UNKNOWN_CAPABILITY


def vendors_with(cap: str) -> tuple:
    """具备某项能力的厂商集合（替代各处的硬编码厂商清单）。"""
    if cap not in VendorCapability.__dataclass_fields__:
        raise ValueError(f"未知能力位: {cap}")
    return tuple(name for name, p in PROVIDERS.items()
                 if getattr(p.capability, cap))


def vendors_payload() -> list:
    """GET /clouds/vendors 响应体（前端能力标注的唯一事实源，避免第二份清单）。

    onprem 不在此清单：裸机/IDC 没有云凭证，属于资产与机器管理，不是云账号厂商。
    Provider 仍保留用于历史账号的标签/能力查询，不影响存量数据。
    """
    return [
        {
            "vendor": name,
            "label": p.label,
            "label_key": f"clouds.vendor.{name}",
            "tier": p.capability.tier,
            "capabilities": asdict(p.capability),
        }
        for name, p in PROVIDERS.items()
        if name != "onprem"
    ]


# 阿里云产品 -> 采集器入口（Provider 层维护单一事实，
# assets.py 的 /assets/sync 引用本表，避免路由层另维护采集清单）
from app.collector.ack import sync_ack  # noqa: E402
from app.collector.cdn import sync_cdn  # noqa: E402
from app.collector.domain import sync_certs, sync_domains  # noqa: E402
from app.collector.ecs import sync_ecs  # noqa: E402
from app.collector.es import sync_es  # noqa: E402
from app.collector.oss import sync_oss  # noqa: E402
from app.collector.rds import sync_rds  # noqa: E402
from app.collector.redis import sync_redis  # noqa: E402
from app.collector.slb import sync_slb  # noqa: E402
from app.collector.waf import sync_waf  # noqa: E402

ALIYUN_SYNC_HANDLERS: dict = {
    "ecs": sync_ecs, "rds": sync_rds, "redis": sync_redis, "slb": sync_slb,
    "oss": sync_oss, "cdn": sync_cdn, "waf": sync_waf, "ack": sync_ack,
    "es": sync_es, "domain": sync_domains, "ssl_cert": sync_certs,
}


# ---------------- 腾讯云 CVM 采集 ----------------

def _cvm_client(ak: str, sk: str, region: str):
    from tencentcloud.common import credential as tencent_credential
    from tencentcloud.cvm.v20170312 import cvm_client

    return cvm_client.CvmClient(tencent_credential.Credential(ak, sk), region)


def _cvm_regions(ak: str, sk: str) -> list:
    """DescribeRegions 拿账号可见区域列表（与阿里云 list_regions 同位）。"""
    from tencentcloud.cvm.v20170312 import models

    resp = _cvm_client(ak, sk, "ap-guangzhou").DescribeRegions(models.DescribeRegionsRequest())
    return [r.Region for r in (resp.RegionSet or []) if getattr(r, "Region", "")]


def _cvm_extra(inst) -> dict:
    """实例关键信息整理进 extra（对齐阿里云 _instance_extra 口径）。"""
    private_ips = list(getattr(inst, "PrivateIpAddresses", None) or [])
    public_ips = list(getattr(inst, "PublicIpAddresses", None) or [])
    vpc = getattr(inst, "VirtualPrivateCloud", None)
    system_disk = getattr(inst, "SystemDisk", None)
    return {
        "cpu": getattr(inst, "CPU", 0) or 0,
        "memory_gb": getattr(inst, "Memory", 0) or 0,   # 腾讯云 Memory 单位即 GB
        "ip_internal": private_ips[0] if private_ips else "",
        "ip_public": public_ips[0] if public_ips else "",
        "os_name": getattr(inst, "OsName", "") or "",
        "image_id": getattr(inst, "ImageId", "") or "",
        "vpc_id": getattr(vpc, "VpcId", "") if vpc else "",
        "subnet_id": getattr(vpc, "SubnetId", "") if vpc else "",
        "uuid": getattr(inst, "Uuid", "") or "",
        "system_disk_gb": getattr(system_disk, "DiskSize", 0) if system_disk else 0,
    }


def _cvm_fields(inst, region: str) -> dict:
    from app.collector.base import parse_time

    placement = getattr(inst, "Placement", None)
    tags = [
        {"key": t.Key, "value": t.Value}
        for t in (getattr(inst, "Tags", None) or [])
    ]
    return dict(
        name=getattr(inst, "InstanceName", "") or "",
        region=region,
        zone=getattr(placement, "Zone", "") if placement else "",
        status=getattr(inst, "InstanceState", "") or "",
        instance_type=getattr(inst, "InstanceType", "") or "",
        charge_type=getattr(inst, "InstanceChargeType", "") or "",
        expire_time=parse_time(getattr(inst, "ExpiredTime", None)),
        tags=json.dumps(tags, ensure_ascii=False),
        extra=json.dumps(_cvm_extra(inst), ensure_ascii=False),
        raw_meta="",
        synced_at=datetime.now(),
    )


def sync_cvm(db: Session, ak: str, sk: str) -> dict:
    """全量同步腾讯云 CVM 实例（统一资产入库：upsert_asset + mark_missing_deleted）。

    返回 {total, created, updated, missing_marked, failed_regions}，与 sync_ecs 同构。
    """
    from app.collector.base import mark_missing_deleted, upsert_asset

    seen_ids: set = set()
    created = updated = 0
    failed_regions: list = []
    for region in _cvm_regions(ak, sk):
        client = _cvm_client(ak, sk, region)
        try:
            offset, instances = 0, []
            from tencentcloud.cvm.v20170312 import models

            while True:
                req = models.DescribeInstancesRequest()
                req.Limit = 100
                req.Offset = offset
                resp = client.DescribeInstances(req)
                batch = resp.InstanceSet or []
                instances.extend(batch)
                offset += len(batch)
                if not batch or offset >= (resp.TotalCount or 0):
                    break
        except Exception as e:
            # 单 region 失败不影响其他 region（对齐 sync_ecs 口径）
            failed_regions.append(f"{region}: {str(e)[:80]}")
            continue

        for inst in instances:
            if upsert_asset(db, "cvm", inst.InstanceId, seen_ids, _cvm_fields(inst, region)) == "created":
                created += 1
            else:
                updated += 1

    # 当次同步未见到且未标记过的 -> Deleted（保留历史，不物理删；账号级清扫收口见 mark_missing_deleted）
    # 清扫守卫（cvm/cdb/crs/clb 使用同一生命周期口径）：
    # 任一 region 失败=该 region 清单不可信，跳过缺失清扫防把整片实例误标 Deleted；
    # 全 region 成功才收口清扫（保守口径：宁可少清一轮，不可误清资产）。
    missing_marked = 0
    sweep_skipped = False
    if failed_regions:
        sweep_skipped = True
        logger.warning("%s 存在失败 region，跳过缺失清扫: %s", "CVM", failed_regions)
    else:
        missing_marked = mark_missing_deleted(db, "cvm", seen_ids)

    db.commit()
    logger.info(
        "CVM 同步完成: total=%d created=%d updated=%d deleted=%d failed_regions=%d",
        len(seen_ids), created, updated, missing_marked, len(failed_regions),
    )
    return {
        "total": len(seen_ids), "created": created, "updated": updated,
        "missing_marked": missing_marked, "sweep_skipped": sweep_skipped,
        "failed_regions": failed_regions,
    }


# ---------------- 腾讯云 CDB / CRS / CLB / COS 采集 ----------------
# 产品 key 口径（与 cvm 同款理由）：cdb/crs/clb/cos 不复用阿里云 rds/redis/slb/oss——
# 那批 product 值被阿里云专属链路（DAS 慢SQL/空间、SLS 巡检、拓扑）按值直取。

def _slb_client(ak: str, sk: str, region: str):
    """阿里云 SLB client 构造（转发规则拉取用；与 _tencent_client 同位的厂商侧入口）。"""
    from alibabacloud_slb20140515.client import Client as SlbClient
    from alibabacloud_tea_openapi.models import Config

    return SlbClient(Config(access_key_id=ak, access_key_secret=sk, region_id=region))


def _tencent_client(service: str, version: str, ak: str, sk: str, region: str):
    """腾讯云通用 client 构造（service 如 cdb/redis/clb）。"""
    import importlib

    from tencentcloud.common import credential as tencent_credential

    mod = importlib.import_module(f"tencentcloud.{service}.{version}.{service}_client")
    cls = getattr(mod, f"{service.capitalize()}Client")
    return cls(tencent_credential.Credential(ak, sk), region)


def _cdb_status(n) -> str:
    # 实例状态（腾讯云 CDB 公开文档）：0 创建中 / 1 运行中 / 4 隔离中 / 5 已回收 / 6 回收中 / 7 下线中
    return {0: "creating", 1: "running", 4: "isolated", 5: "recycled",
            6: "recycling", 7: "offline"}.get(n, str(n))


def _crs_status(n) -> str:
    # 实例状态（腾讯云 Redis 公开文档）：0 待初始化 / 1 流程中 / 2 运行中 / -2 已隔离 / -3 待回收
    return {0: "initializing", 1: "processing", 2: "running",
            -2: "isolated", -3: "recycled"}.get(n, str(n))


def _clb_status(n) -> str:
    # 实例状态（腾讯云 CLB 公开文档）：0 创建中 / 1 正常；其余按原值透传
    return {0: "creating", 1: "running"}.get(n, str(n))


def _as_text(v) -> str:
    return "" if v is None else str(v)


def _tag_pairs(obj, attrs=("InstanceTags", "Tags")) -> list:
    """腾讯云标签形态归一（TagKey/TagValue 与 Key/Value 两种都收；取首个非空属性防重复）。"""
    for attr in attrs:
        items = getattr(obj, attr, None) or []
        if items:
            return [{"key": _as_text(getattr(t, "TagKey", getattr(t, "Key", ""))),
                     "value": _as_text(getattr(t, "TagValue", getattr(t, "Value", "")))}
                    for t in items]
    return []


def sync_cdb(db: Session, ak: str, sk: str) -> dict:
    """全量同步腾讯云 CDB（云数据库 MySQL）实例（product='cdb'）。与 sync_cvm 同构。"""
    from tencentcloud.cdb.v20170320 import models

    from app.collector.base import mark_missing_deleted, parse_time, upsert_asset

    seen_ids: set = set()
    created = updated = 0
    failed_regions: list = []
    for region in _cvm_regions(ak, sk):
        client = _tencent_client("cdb", "v20170320", ak, sk, region)
        try:
            offset, instances = 0, []
            while True:
                req = models.DescribeDBInstancesRequest()
                req.Limit = 100
                req.Offset = offset
                resp = client.DescribeDBInstances(req)
                batch = resp.Items or []
                instances.extend(batch)
                offset += len(batch)
                if not batch or offset >= (resp.TotalCount or 0):
                    break
        except Exception as e:
            failed_regions.append(f"{region}: {str(e)[:80]}")
            continue

        for inst in instances:
            extra = {
                "engine": "mysql",
                "engine_version": _as_text(getattr(inst, "EngineVersion", "")),
                "instance_role": "Primary",
                "memory_mb": getattr(inst, "Memory", 0) or 0,
                "storage_capacity_gb": getattr(inst, "Volume", 0) or 0,
                "cpu": getattr(inst, "Cpu", 0) or 0,
                "qps": getattr(inst, "Qps", 0) or 0,
                "connection_string": _as_text(getattr(inst, "Vip", "")),
                "port": getattr(inst, "Vport", 0) or 0,
                "wan_domain": _as_text(getattr(inst, "WanDomain", "")),
                "wan_port": getattr(inst, "WanPort", 0) or 0,
                "vpc_id": _as_text(getattr(inst, "UniqVpcId", "") or getattr(inst, "VpcId", "")),
                "subnet_id": _as_text(getattr(inst, "UniqSubnetId", "") or getattr(inst, "SubnetId", "")),
                "task_status": _as_text(getattr(inst, "TaskStatus", "")),
                "project_id": _as_text(getattr(inst, "ProjectId", "")),
            }
            fields = dict(
                name=_as_text(getattr(inst, "InstanceName", "")) or _as_text(inst.InstanceId),
                region=region,
                zone=_as_text(getattr(inst, "Zone", "")),
                status=_cdb_status(getattr(inst, "Status", "")),
                instance_type=_as_text(getattr(inst, "DeviceClass", "") or getattr(inst, "InstanceType", "")),
                charge_type={1: "PrePaid", 0: "PostPaid"}.get(getattr(inst, "PayType", None),
                                                             _as_text(getattr(inst, "PayType", ""))),
                expire_time=parse_time(getattr(inst, "DeadlineTime", None)),
                tags="[]",
                extra=json.dumps(extra, ensure_ascii=False),
                raw_meta="",
                synced_at=datetime.now(),
            )
            if upsert_asset(db, "cdb", inst.InstanceId, seen_ids, fields) == "created":
                created += 1
            else:
                updated += 1

    # 清扫守卫（cvm/cdb/crs/clb 使用同一生命周期口径）：
    # 任一 region 失败=该 region 清单不可信，跳过缺失清扫防把整片实例误标 Deleted；
    # 全 region 成功才收口清扫（保守口径：宁可少清一轮，不可误清资产）。
    missing_marked = 0
    sweep_skipped = False
    if failed_regions:
        sweep_skipped = True
        logger.warning("%s 存在失败 region，跳过缺失清扫: %s", "CDB", failed_regions)
    else:
        missing_marked = mark_missing_deleted(db, "cdb", seen_ids)
    db.commit()
    logger.info("CDB 同步完成: total=%d created=%d updated=%d deleted=%d failed_regions=%d",
                len(seen_ids), created, updated, missing_marked, len(failed_regions))
    return {"total": len(seen_ids), "created": created, "updated": updated,
            "missing_marked": missing_marked, "sweep_skipped": sweep_skipped,
            "failed_regions": failed_regions}


def sync_crs(db: Session, ak: str, sk: str) -> dict:
    """全量同步腾讯云 Redis（CRS）实例（product='crs'）。与 sync_cvm 同构。"""
    from tencentcloud.redis.v20180412 import models

    from app.collector.base import mark_missing_deleted, parse_time, upsert_asset

    seen_ids: set = set()
    created = updated = 0
    failed_regions: list = []
    for region in _cvm_regions(ak, sk):
        client = _tencent_client("redis", "v20180412", ak, sk, region)
        try:
            offset, instances = 0, []
            while True:
                req = models.DescribeInstancesRequest()
                req.Limit = 100
                req.Offset = offset
                resp = client.DescribeInstances(req)
                batch = resp.InstanceSet or []
                instances.extend(batch)
                offset += len(batch)
                if not batch or offset >= (resp.TotalCount or 0):
                    break
        except Exception as e:
            failed_regions.append(f"{region}: {str(e)[:80]}")
            continue

        for inst in instances:
            # Createtime 为秒级 unix 时间戳（区别 parse_time 的毫秒口径），单独转
            created_at = None
            ct = getattr(inst, "Createtime", None)
            if isinstance(ct, (int, float)) and ct:
                try:
                    created_at = datetime.fromtimestamp(ct)
                except (ValueError, OSError, OverflowError):
                    created_at = None
            extra = {
                "engine_version": _as_text(getattr(inst, "Engine", "")),
                "capacity_mb": getattr(inst, "Size", 0) or 0,
                "max_connections": getattr(inst, "ClientLimit", 0) or 0,
                "connection_domain": _as_text(getattr(inst, "WanIp", "")),
                "port": getattr(inst, "Port", 0) or 0,
                "private_ip": _as_text(getattr(inst, "WanIp", "")),
                "vpc_id": _as_text(getattr(inst, "UniqVpcId", "") or getattr(inst, "VpcId", "")),
                "subnet_id": _as_text(getattr(inst, "UniqSubnetId", "") or getattr(inst, "SubnetId", "")),
                "node_type": _as_text(getattr(inst, "Type", "")),
                "product_version": _as_text(getattr(inst, "ProductType", "")),
                "shard_num": getattr(inst, "RedisShardNum", 0) or 0,
                "replicas_num": getattr(inst, "RedisReplicasNum", 0) or 0,
                "create_time": created_at.strftime("%Y-%m-%d %H:%M:%S") if created_at else "",
            }
            fields = dict(
                name=_as_text(getattr(inst, "InstanceName", "")) or _as_text(inst.InstanceId),
                region=_as_text(getattr(inst, "RegionId", "")) or region,
                zone=_as_text(getattr(inst, "ZoneId", "")),
                status=_crs_status(getattr(inst, "Status", "")),
                instance_type=_as_text(getattr(inst, "Type", "")),
                charge_type={0: "PrePaid", 1: "PostPaid"}.get(getattr(inst, "BillingMode", None),
                                                             _as_text(getattr(inst, "BillingMode", ""))),
                expire_time=parse_time(getattr(inst, "DeadlineTime", None)),
                tags=json.dumps(_tag_pairs(inst), ensure_ascii=False),
                extra=json.dumps(extra, ensure_ascii=False),
                raw_meta="",
                synced_at=datetime.now(),
            )
            if upsert_asset(db, "crs", inst.InstanceId, seen_ids, fields) == "created":
                created += 1
            else:
                updated += 1

    # 清扫守卫（cvm/cdb/crs/clb 使用同一生命周期口径）：
    # 任一 region 失败=该 region 清单不可信，跳过缺失清扫防把整片实例误标 Deleted；
    # 全 region 成功才收口清扫（保守口径：宁可少清一轮，不可误清资产）。
    missing_marked = 0
    sweep_skipped = False
    if failed_regions:
        sweep_skipped = True
        logger.warning("%s 存在失败 region，跳过缺失清扫: %s", "CRS", failed_regions)
    else:
        missing_marked = mark_missing_deleted(db, "crs", seen_ids)
    db.commit()
    logger.info("CRS 同步完成: total=%d created=%d updated=%d deleted=%d failed_regions=%d",
                len(seen_ids), created, updated, missing_marked, len(failed_regions))
    return {"total": len(seen_ids), "created": created, "updated": updated,
            "missing_marked": missing_marked, "sweep_skipped": sweep_skipped,
            "failed_regions": failed_regions}


def sync_clb(db: Session, ak: str, sk: str) -> dict:
    """全量同步腾讯云 CLB 负载均衡实例（product='clb'）。与 sync_cvm 同构。"""
    from tencentcloud.clb.v20180317 import models

    from app.collector.base import mark_missing_deleted, parse_time, upsert_asset

    seen_ids: set = set()
    created = updated = 0
    failed_regions: list = []
    for region in _cvm_regions(ak, sk):
        client = _tencent_client("clb", "v20180317", ak, sk, region)
        try:
            offset, lbs = 0, []
            while True:
                req = models.DescribeLoadBalancersRequest()
                req.Limit = 100
                req.Offset = offset
                resp = client.DescribeLoadBalancers(req)
                batch = resp.LoadBalancerSet or []
                lbs.extend(batch)
                offset += len(batch)
                if not batch or offset >= (resp.TotalCount or 0):
                    break
        except Exception as e:
            failed_regions.append(f"{region}: {str(e)[:80]}")
            continue

        for lb in lbs:
            vips = [_as_text(v) for v in (getattr(lb, "LoadBalancerVips", None) or [])]
            lb_type = _as_text(getattr(lb, "LoadBalancerType", ""))
            created_at = parse_time(getattr(lb, "CreateTime", None))
            extra = {
                "address": ",".join(vips),
                "address_type": {"OPEN": "internet", "INTERNAL": "intranet"}.get(lb_type, lb_type),
                "network_type": _as_text(getattr(lb, "AddressIPVersion", "")),
                "forward": _as_text(getattr(lb, "Forward", "")),
                "domain": _as_text(getattr(lb, "Domain", "")),
                "vpc_id": _as_text(getattr(lb, "VpcId", "")),
                "subnet_id": _as_text(getattr(lb, "SubnetId", "")),
                "master_zone": _as_text(getattr(lb, "MasterZone", "")),
                "project_id": _as_text(getattr(lb, "ProjectId", "")),
                "create_time": created_at.strftime("%Y-%m-%d %H:%M:%S") if created_at else "",
            }
            fields = dict(
                name=_as_text(getattr(lb, "LoadBalancerName", "")) or _as_text(lb.LoadBalancerId),
                region=region,
                zone=_as_text(getattr(lb, "MasterZone", "")),
                status=_clb_status(getattr(lb, "Status", "")),
                instance_type=lb_type,
                charge_type=_as_text(getattr(lb, "ChargeType", "")),
                expire_time=parse_time(getattr(lb, "ExpireTime", None)),
                tags=json.dumps(_tag_pairs(lb), ensure_ascii=False),
                extra=json.dumps(extra, ensure_ascii=False),
                raw_meta="",
                synced_at=datetime.now(),
            )
            if upsert_asset(db, "clb", lb.LoadBalancerId, seen_ids, fields) == "created":
                created += 1
            else:
                updated += 1

    # 清扫守卫（cvm/cdb/crs/clb 使用同一生命周期口径）：
    # 任一 region 失败=该 region 清单不可信，跳过缺失清扫防把整片实例误标 Deleted；
    # 全 region 成功才收口清扫（保守口径：宁可少清一轮，不可误清资产）。
    missing_marked = 0
    sweep_skipped = False
    if failed_regions:
        sweep_skipped = True
        logger.warning("%s 存在失败 region，跳过缺失清扫: %s", "CLB", failed_regions)
    else:
        missing_marked = mark_missing_deleted(db, "clb", seen_ids)
    db.commit()
    logger.info("CLB 同步完成: total=%d created=%d updated=%d deleted=%d failed_regions=%d",
                len(seen_ids), created, updated, missing_marked, len(failed_regions))
    return {"total": len(seen_ids), "created": created, "updated": updated,
            "missing_marked": missing_marked, "sweep_skipped": sweep_skipped,
            "failed_regions": failed_regions}


def sync_cos(db: Session, ak: str, sk: str) -> dict:
    """全量同步腾讯云 COS 存储桶（product='cos'）。

    COS 无按 region 遍历的产品清单接口——cos-python-sdk-v5 list_buckets（GET Service）
    一次拉全账号桶（自带 Region），分页走 Marker/IsTruncated。
    """
    from qcloud_cos import CosConfig, CosS3Client

    from app.collector.base import mark_missing_deleted, parse_time, upsert_asset

    client = CosS3Client(CosConfig(SecretId=ak, SecretKey=sk, Region="ap-guangzhou"))
    seen_ids: set = set()
    created = updated = 0
    failed_regions: list = []
    marker, pages = "", 0
    try:
        while True:
            resp = client.list_buckets(Marker=marker, MaxKeys=2000)
            buckets = ((resp.get("Buckets") or {}).get("Bucket")) or []
            if not isinstance(buckets, list):
                buckets = [buckets]
            for b in buckets:
                name = _as_text(b.get("Name", ""))
                if not name:
                    continue
                created_at = parse_time(b.get("CreationDate"))
                extra = {
                    "storage_class": _as_text(b.get("StorageClass", "")),
                    "creation_date": created_at.strftime("%Y-%m-%d %H:%M:%S") if created_at else "",
                    "bucket_region": _as_text(b.get("Region", "") or b.get("Location", "")),
                }
                fields = dict(
                    name=name,
                    region=_as_text(b.get("Region", "") or b.get("Location", "")),
                    zone="",
                    status="Active",
                    instance_type=_as_text(b.get("StorageClass", "")),
                    charge_type="PostPaid",
                    expire_time=None,
                    tags="[]",
                    extra=json.dumps(extra, ensure_ascii=False),
                    raw_meta="",
                    synced_at=datetime.now(),
                )
                if upsert_asset(db, "cos", name, seen_ids, fields) == "created":
                    created += 1
                else:
                    updated += 1
            pages += 1
            if str(resp.get("IsTruncated", "false")).lower() != "true" or not buckets or pages >= 20:
                break
            marker = _as_text(buckets[-1].get("Name", "")) or _as_text(resp.get("NextMarker", ""))
            if not marker:
                break
    except Exception as e:  # 清扫守卫：分页中途失败=清单不完整
        failed_regions.append(f"list_buckets: {str(e)[:80]}")

    # 清扫守卫：COS 是全局单请求（无 region 遍历），拉取失败
    # 走不到这里（异常上抛=整批不动）；但分页中途异常也可能只拉到半份桶清单——
    # 与 region 系同口径：拉取异常=清单不可信，跳过缺失清扫防把半份桶误标 Deleted。
    missing_marked = 0
    sweep_skipped = False
    if failed_regions:
        sweep_skipped = True
        logger.warning("COS 清单拉取不完整，跳过缺失清扫: %s", failed_regions)
    else:
        missing_marked = mark_missing_deleted(db, "cos", seen_ids)
    db.commit()
    logger.info("COS 同步完成: total=%d created=%d updated=%d deleted=%d sweep_skipped=%s",
                len(seen_ids), created, updated, missing_marked, sweep_skipped)
    return {"total": len(seen_ids), "created": created, "updated": updated,
            "missing_marked": missing_marked, "sweep_skipped": sweep_skipped,
            "failed_regions": failed_regions}


# ---------------- 腾讯云 CDN / MongoDB / ES / PostgreSQL 采集 ----------------
# 产品 key（tc_cdn/tc_mongodb/tc_es/tc_postgres）：cdn/es 是阿里云专属值（阿里云 cdn/es
# 采集器按 product 直取），mongodb/postgres 无阿里云同名值但同前缀统一避让——与
# cvm/cdb/crs/clb/cos 的"避开阿里云专属值"取法一致。
# TDSQL/MySQL 商业版：与 cdb 同 API（cdb.DescribeDBInstances，cdb 模型含 TDSQL 引擎参数
# CpuModel/EngineTypes 覆盖 TDSQL 形态实例），无需另设产品 key；
# 不重复实现（tc_cdb 不存在；TDSQL 实例由 sync_cdb 一并入库）。


def _tc_mongodb_status(n) -> str:
    # 实例状态（腾讯云 MongoDB 公开文档）：0 待初始化 / 1 流程处理中 / 2 运行中 /
    # -2 已隔离（包年包月）/ -3 已隔离（按量计费）
    return {0: "initializing", 1: "processing", 2: "running",
            -2: "isolated", -3: "isolated"}.get(n, str(n))


def _tc_es_status(n) -> str:
    # 实例状态（腾讯云 ES 公开文档）：0 处理中 / 1 正常 / -1 停止 / -2 销毁中 /
    # -3 已销毁 / -4 隔离中 / 2 创建集群时初始化中
    return {0: "processing", 1: "running", -1: "stopped", -2: "destroying",
            -3: "destroyed", -4: "isolated", 2: "initializing"}.get(n, str(n))


def sync_tc_cdn(db: Session, ak: str, sk: str) -> dict:
    """全量同步腾讯云 CDN 加速域名（product='tc_cdn'）。

    CDN 是全局产品（域名不分 region）：SDK 实际清单方法为 cdn.DescribeDomains，
    使用 Offset/Limit 分页一次拉全账号域名。
    """
    from app.collector.base import mark_missing_deleted, upsert_asset

    client = _tencent_client("cdn", "v20180606", ak, sk, "ap-guangzhou")
    seen_ids: set = set()
    created = updated = 0
    failed_regions: list = []
    offset, domains = 0, []
    try:
        from tencentcloud.cdn.v20180606 import models

        while True:
            req = models.DescribeDomainsRequest()
            req.Offset = offset
            req.Limit = 100
            resp = client.DescribeDomains(req)
            batch = resp.Domains or []
            domains.extend(batch)
            offset += len(batch)
            if not batch or offset >= (resp.TotalNumber or 0):
                break
    except Exception as e:  # 清扫守卫：拉取失败=清单不完整
        failed_regions.append(f"DescribeDomains: {str(e)[:80]}")

    for d in domains:
        origin = getattr(d, "Origin", None)
        extra = {
            "cname": _as_text(getattr(d, "Cname", "")),
            "service_type": _as_text(getattr(d, "ServiceType", "")),
            "area": _as_text(getattr(d, "Area", "")),
            "project_id": _as_text(getattr(d, "ProjectId", "")),
            "origin_type": _as_text(getattr(origin, "OriginType", "")) if origin else "",
            "disable": _as_text(getattr(d, "Disable", "")),
            "read_only": bool(getattr(d, "Readonly", False)),
            "create_time": _as_text(getattr(d, "CreateTime", "")),
            "update_time": _as_text(getattr(d, "UpdateTime", "")),
        }
        fields = dict(
            name=_as_text(getattr(d, "Domain", "")),
            region="global",
            zone="",
            status=_as_text(getattr(d, "Status", "")),
            instance_type=_as_text(getattr(d, "ServiceType", "")),
            charge_type="PostPaid",
            expire_time=None,
            tags="[]",
            extra=json.dumps(extra, ensure_ascii=False),
            raw_meta="",
            synced_at=datetime.now(),
        )
        if upsert_asset(db, "tc_cdn", _as_text(getattr(d, "Domain", "")), seen_ids, fields) == "created":
            created += 1
        else:
            updated += 1

    # 清扫守卫：拉取失败/分页中断=清单不可信，跳过缺失清扫
    # 防把整批域名误标 Deleted；全量成功才收口清扫（宁可少清一轮，不可误清资产）。
    missing_marked = 0
    sweep_skipped = False
    if failed_regions:
        sweep_skipped = True
        logger.warning("TC CDN 清单拉取不完整，跳过缺失清扫: %s", failed_regions)
    else:
        missing_marked = mark_missing_deleted(db, "tc_cdn", seen_ids)
    db.commit()
    logger.info("TC CDN 同步完成: total=%d created=%d updated=%d deleted=%d sweep_skipped=%s",
                len(seen_ids), created, updated, missing_marked, sweep_skipped)
    return {"total": len(seen_ids), "created": created, "updated": updated,
            "missing_marked": missing_marked, "sweep_skipped": sweep_skipped,
            "failed_regions": failed_regions}


def sync_tc_mongodb(db: Session, ak: str, sk: str) -> dict:
    """全量同步腾讯云 MongoDB 实例（product='tc_mongodb'）。与 sync_cdb 同构。"""
    from tencentcloud.mongodb.v20190725 import models

    from app.collector.base import mark_missing_deleted, parse_time, upsert_asset

    seen_ids: set = set()
    created = updated = 0
    failed_regions: list = []
    for region in _cvm_regions(ak, sk):
        client = _tencent_client("mongodb", "v20190725", ak, sk, region)
        try:
            offset, instances = 0, []
            while True:
                req = models.DescribeDBInstancesRequest()
                req.Limit = 100
                req.Offset = offset
                resp = client.DescribeDBInstances(req)
                batch = resp.InstanceDetails or []
                instances.extend(batch)
                offset += len(batch)
                if not batch or offset >= (resp.TotalCount or 0):
                    break
        except Exception as e:
            failed_regions.append(f"{region}: {str(e)[:80]}")
            continue

        for inst in instances:
            extra = {
                "engine": "mongodb",
                "engine_version": _as_text(getattr(inst, "MongoVersion", "")),
                "cluster_type": {0: "replica_set", 1: "sharded"}.get(
                    getattr(inst, "ClusterType", None),
                    _as_text(getattr(inst, "ClusterType", ""))),
                "memory_mb": getattr(inst, "Memory", 0) or 0,
                "storage_mb": getattr(inst, "Volume", 0) or 0,
                "cpu": getattr(inst, "CpuNum", 0) or 0,
                "machine_type": _as_text(getattr(inst, "MachineType", "")),
                "secondary_num": getattr(inst, "SecondaryNum", 0) or 0,
                "replication_set_num": getattr(inst, "ReplicationSetNum", 0) or 0,
                "connection_string": _as_text(getattr(inst, "Vip", "")),
                "port": getattr(inst, "Vport", 0) or 0,
                "net_type": {0: "basic", 1: "vpc"}.get(getattr(inst, "NetType", None),
                                                      _as_text(getattr(inst, "NetType", ""))),
                "vpc_id": _as_text(getattr(inst, "VpcId", "")),
                "subnet_id": _as_text(getattr(inst, "SubnetId", "")),
                "project_id": _as_text(getattr(inst, "ProjectId", "")),
                "create_time": _as_text(getattr(inst, "CreateTime", "")),
            }
            fields = dict(
                name=_as_text(getattr(inst, "InstanceName", "")) or _as_text(inst.InstanceId),
                region=_as_text(getattr(inst, "Region", "")) or region,
                zone=_as_text(getattr(inst, "Zone", "")),
                status=_tc_mongodb_status(getattr(inst, "Status", "")),
                instance_type=_as_text(getattr(inst, "MachineType", "")),
                charge_type={1: "PrePaid", 0: "PostPaid"}.get(getattr(inst, "PayMode", None),
                                                             _as_text(getattr(inst, "PayMode", ""))),
                expire_time=parse_time(getattr(inst, "DeadLine", None)),
                tags=json.dumps(_tag_pairs(inst), ensure_ascii=False),
                extra=json.dumps(extra, ensure_ascii=False),
                raw_meta="",
                synced_at=datetime.now(),
            )
            if upsert_asset(db, "tc_mongodb", inst.InstanceId, seen_ids, fields) == "created":
                created += 1
            else:
                updated += 1

    # 清扫守卫：任一 region 失败=该 region 清单不可信，跳过缺失
    # 清扫防把整片实例误标 Deleted；全 region 成功才收口清扫（宁可少清一轮，不可误清资产）。
    missing_marked = 0
    sweep_skipped = False
    if failed_regions:
        sweep_skipped = True
        logger.warning("TC MongoDB 存在失败 region，跳过缺失清扫: %s", failed_regions)
    else:
        missing_marked = mark_missing_deleted(db, "tc_mongodb", seen_ids)
    db.commit()
    logger.info("TC MongoDB 同步完成: total=%d created=%d updated=%d deleted=%d failed_regions=%d",
                len(seen_ids), created, updated, missing_marked, len(failed_regions))
    return {"total": len(seen_ids), "created": created, "updated": updated,
            "missing_marked": missing_marked, "sweep_skipped": sweep_skipped,
            "failed_regions": failed_regions}


def sync_tc_es(db: Session, ak: str, sk: str) -> dict:
    """全量同步腾讯云 ES 实例（product='tc_es'）。与 sync_cdb 同构。

    SDK 实际方法为 DescribeInstances，其"查询用户该地域下"语义表明清单按 region
    维度返回，因此逐 region 拉取。
    """
    from tencentcloud.es.v20180416 import models

    from app.collector.base import mark_missing_deleted, parse_time, upsert_asset

    seen_ids: set = set()
    created = updated = 0
    failed_regions: list = []
    for region in _cvm_regions(ak, sk):
        client = _tencent_client("es", "v20180416", ak, sk, region)
        try:
            offset, instances = 0, []
            while True:
                req = models.DescribeInstancesRequest()
                req.Limit = 100
                req.Offset = offset
                resp = client.DescribeInstances(req)
                batch = resp.InstanceList or []
                instances.extend(batch)
                offset += len(batch)
                if not batch or offset >= (resp.TotalCount or 0):
                    break
        except Exception as e:
            failed_regions.append(f"{region}: {str(e)[:80]}")
            continue

        for inst in instances:
            node_type = _as_text(getattr(inst, "NodeType", ""))
            extra = {
                "engine": "elasticsearch",
                "engine_version": _as_text(getattr(inst, "EsVersion", "")),
                "node_num": getattr(inst, "NodeNum", 0) or 0,
                "cpu": getattr(inst, "CpuNum", 0) or 0,
                "memory_gb": getattr(inst, "MemSize", 0) or 0,
                "disk_gb": getattr(inst, "DiskSize", 0) or 0,
                "disk_type": _as_text(getattr(inst, "DiskType", "")),
                "es_domain": _as_text(getattr(inst, "EsDomain", "")),
                "es_vip": _as_text(getattr(inst, "EsVip", "")),
                "es_port": getattr(inst, "EsPort", 0) or 0,
                "license_type": _as_text(getattr(inst, "LicenseType", "")),
                "create_time": _as_text(getattr(inst, "CreateTime", "")),
            }
            fields = dict(
                name=_as_text(getattr(inst, "InstanceName", "")) or _as_text(inst.InstanceId),
                region=_as_text(getattr(inst, "Region", "")) or region,
                zone=_as_text(getattr(inst, "Zone", "")),
                status=_tc_es_status(getattr(inst, "Status", "")),
                instance_type=node_type,
                charge_type={"PREPAID": "PrePaid", "POSTPAID": "PostPaid"}.get(
                    _as_text(getattr(inst, "ChargeType", "")).upper(),
                    _as_text(getattr(inst, "ChargeType", ""))),
                expire_time=parse_time(getattr(inst, "Deadline", None)),
                tags=json.dumps(_tag_pairs(inst, attrs=("TagList",)), ensure_ascii=False),
                extra=json.dumps(extra, ensure_ascii=False),
                raw_meta="",
                synced_at=datetime.now(),
            )
            if upsert_asset(db, "tc_es", inst.InstanceId, seen_ids, fields) == "created":
                created += 1
            else:
                updated += 1

    # 清扫守卫：任一 region 失败=该 region 清单不可信，跳过缺失
    # 清扫防把整片实例误标 Deleted；全 region 成功才收口清扫（宁可少清一轮，不可误清资产）。
    missing_marked = 0
    sweep_skipped = False
    if failed_regions:
        sweep_skipped = True
        logger.warning("TC ES 存在失败 region，跳过缺失清扫: %s", failed_regions)
    else:
        missing_marked = mark_missing_deleted(db, "tc_es", seen_ids)
    db.commit()
    logger.info("TC ES 同步完成: total=%d created=%d updated=%d deleted=%d failed_regions=%d",
                len(seen_ids), created, updated, missing_marked, len(failed_regions))
    return {"total": len(seen_ids), "created": created, "updated": updated,
            "missing_marked": missing_marked, "sweep_skipped": sweep_skipped,
            "failed_regions": failed_regions}


def sync_tc_postgres(db: Session, ak: str, sk: str) -> dict:
    """全量同步腾讯云 PostgreSQL 实例（product='tc_postgres'）。与 sync_cdb 同构。"""
    from tencentcloud.postgres.v20170312 import models

    from app.collector.base import mark_missing_deleted, parse_time, upsert_asset

    seen_ids: set = set()
    created = updated = 0
    failed_regions: list = []
    for region in _cvm_regions(ak, sk):
        client = _tencent_client("postgres", "v20170312", ak, sk, region)
        try:
            offset, instances = 0, []
            while True:
                req = models.DescribeDBInstancesRequest()
                req.Limit = 100
                req.Offset = offset
                resp = client.DescribeDBInstances(req)
                batch = resp.DBInstanceSet or []
                instances.extend(batch)
                offset += len(batch)
                if not batch or offset >= (resp.TotalCount or 0):
                    break
        except Exception as e:
            failed_regions.append(f"{region}: {str(e)[:80]}")
            continue

        for inst in instances:
            net_info = getattr(inst, "DBInstanceNetInfo", None) or []
            net0 = net_info[0] if net_info else None
            extra = {
                "engine": _as_text(getattr(inst, "DBEngine", "")) or "postgresql",
                "engine_version": _as_text(getattr(inst, "DBVersion", "")),
                "major_version": _as_text(getattr(inst, "DBMajorVersion", "")),
                "memory_gb": getattr(inst, "DBInstanceMemory", 0) or 0,
                "storage_gb": getattr(inst, "DBInstanceStorage", 0) or 0,
                "cpu": getattr(inst, "DBInstanceCpu", 0) or 0,
                "connection_string": _as_text(getattr(net0, "Address", "")) if net0 else "",
                "port": getattr(net0, "Port", 0) if net0 else 0,
                "vpc_id": _as_text(getattr(inst, "VpcId", "")),
                "subnet_id": _as_text(getattr(inst, "SubnetId", "")),
                "project_id": _as_text(getattr(inst, "ProjectId", "")),
                "master_instance_id": _as_text(getattr(inst, "MasterDBInstanceId", "")),
                "create_time": _as_text(getattr(inst, "CreateTime", "")),
            }
            fields = dict(
                name=_as_text(getattr(inst, "DBInstanceName", "")) or _as_text(inst.DBInstanceId),
                region=_as_text(getattr(inst, "Region", "")) or region,
                zone=_as_text(getattr(inst, "Zone", "")),
                status=_as_text(getattr(inst, "DBInstanceStatus", "")),
                instance_type=_as_text(getattr(inst, "DBInstanceClass", "")),
                charge_type={"prepaid": "PrePaid", "postpaid": "PostPaid"}.get(
                    _as_text(getattr(inst, "PayType", "")).lower(),
                    _as_text(getattr(inst, "PayType", ""))),
                expire_time=parse_time(getattr(inst, "ExpireTime", None)),
                tags=json.dumps(_tag_pairs(inst, attrs=("TagList",)), ensure_ascii=False),
                extra=json.dumps(extra, ensure_ascii=False),
                raw_meta="",
                synced_at=datetime.now(),
            )
            if upsert_asset(db, "tc_postgres", inst.DBInstanceId, seen_ids, fields) == "created":
                created += 1
            else:
                updated += 1

    # 清扫守卫：任一 region 失败=该 region 清单不可信，跳过缺失
    # 清扫防把整片实例误标 Deleted；全 region 成功才收口清扫（宁可少清一轮，不可误清资产）。
    missing_marked = 0
    sweep_skipped = False
    if failed_regions:
        sweep_skipped = True
        logger.warning("TC PostgreSQL 存在失败 region，跳过缺失清扫: %s", failed_regions)
    else:
        missing_marked = mark_missing_deleted(db, "tc_postgres", seen_ids)
    db.commit()
    logger.info("TC PostgreSQL 同步完成: total=%d created=%d updated=%d deleted=%d failed_regions=%d",
                len(seen_ids), created, updated, missing_marked, len(failed_regions))
    return {"total": len(seen_ids), "created": created, "updated": updated,
            "missing_marked": missing_marked, "sweep_skipped": sweep_skipped,
            "failed_regions": failed_regions}


# 腾讯云产品 -> 采集入口
TENCENT_SYNC_HANDLERS: dict = {
    "cvm": sync_cvm,
    "cdb": sync_cdb,
    "crs": sync_crs,
    "clb": sync_clb,
    "cos": sync_cos,
    "tc_cdn": sync_tc_cdn,
    "tc_mongodb": sync_tc_mongodb,
    "tc_es": sync_tc_es,
    "tc_postgres": sync_tc_postgres,
}
