"""阿里云 BSS（费用中心）只读封装。

两个只读数据源：
- QueryInstanceBill：按天实例账单（DAILY 粒度，pageNum 翻页）。即控制台"账单明细"口径，
  含按量消费与当日订单/退款事件；成功时 body.code="Success"，业务错误也在 HTTP 200 里。
- DescribeInstanceAmortizedCostByAmortizationPeriodDate：包年包月按日摊销。
  需用户在费用中心开通"摊销成本"（NotActiveService 时未开通）。

金额口径：PretaxAmount=账单金额(优惠后，判断花费的唯一口径)、PretaxGrossAmount=原价、
PaymentAmount=挂账现金字段（常 0，不作花费依据，不得据此推断抵扣）。
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Optional

logger = logging.getLogger("qianshu.bss")

_MAX_PAGES = 200
_PAGE_SIZE = 300  # BSS 上限


class BSSError(RuntimeError):
    """code 属性保存业务错误码（如 NotActiveService），便于上层区分软/硬错误。"""

    def __init__(self, msg: str, code: str = ""):
        super().__init__(msg)
        self.code = code


def _build_client(ak: str, sk: str, endpoint: str):
    from alibabacloud_bssopenapi20171214.client import Client
    from alibabacloud_tea_openapi.models import Config

    cfg = Config(access_key_id=ak, access_key_secret=sk)
    cfg.endpoint = endpoint
    return Client(cfg)


def _f(v) -> float:
    """SDK 数值字段可能为 None。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _s(v) -> str:
    return str(v) if v is not None else ""


def _unwrap(resp_body, api_label: str):
    """统一处理：HTTP 层异常 + body.code 业务错误（成功 Code='Success'）。"""
    code = _s(getattr(resp_body, "code", ""))
    if code and not code.lower().startswith("success"):
        raise BSSError(f"{api_label} 业务错误: {code} {resp_body.message}", code=code)
    return resp_body.data


def fetch_day_bill(ak: str, sk: str, day: date, endpoint: str = "business.aliyuncs.com") -> list:
    """拉取一天的实例账单明细（DAILY 粒度），返回条目列表（只读查询）。"""
    client = _build_client(ak, sk, endpoint)
    from alibabacloud_bssopenapi20171214 import models as bss

    items: list = []
    page = 0
    while page < _MAX_PAGES:
        page += 1
        req = bss.QueryInstanceBillRequest(
            billing_cycle=day.strftime("%Y-%m"),
            billing_date=day.strftime("%Y-%m-%d"),
            granularity="DAILY",
            page_num=page,
            page_size=_PAGE_SIZE,
        )
        try:
            body = client.query_instance_bill(req).body
        except Exception as e:  # 网络/鉴权类
            raise BSSError(f"QueryInstanceBill 失败: {str(e)[:300]}") from e
        data = _unwrap(body, "QueryInstanceBill")
        page_items = (data.items.item if data.items and data.items.item else []) or []
        items.extend(page_items)
        total = int(data.total_count or 0)
        if page * _PAGE_SIZE >= total or not page_items:
            break
    if page >= _MAX_PAGES:
        logger.warning("BSS 账单翻页超过 %d 页，可能被截断: %s", _MAX_PAGES, day)
    return items


def fetch_day_amortized(ak: str, sk: str, day: date, endpoint: str = "business.aliyuncs.com") -> list:
    """拉取一天的包年包月摊销成本（只读查询）。未开通"摊销成本"时抛 BSSError(code=NotActiveService)。"""
    client = _build_client(ak, sk, endpoint)
    from alibabacloud_bssopenapi20171214 import models as bss

    items: list = []
    token: Optional[str] = None
    page = 0
    while page < _MAX_PAGES:
        page += 1
        req = bss.DescribeInstanceAmortizedCostByAmortizationPeriodDateRequest(
            billing_cycle=day.strftime("%Y-%m"),
            amortization_date_start=day.strftime("%Y-%m-%d"),
            amortization_date_end=day.strftime("%Y-%m-%d"),
            max_results=_PAGE_SIZE,
            next_token=token,
        )
        try:
            body = client.describe_instance_amortized_cost_by_amortization_period_date(req).body
        except Exception as e:  # 含未开通(NotActiveService)等业务错误
            msg = str(e)
            if "NotActiveService" in msg:
                raise BSSError("摊销成本未开通: 请在费用与成本中心-成本分析开通后自动纳入", code="NotActiveService")
            raise BSSError(f"DescribeInstanceAmortizedCost 失败: {msg[:300]}") from e
        data = _unwrap(body, "DescribeInstanceAmortizedCost")
        page_items = (data.items.item if data.items and data.items.item else []) or []
        items.extend(page_items)
        token = getattr(data, "next_token", None)
        if not token:
            break
    if page >= _MAX_PAGES:
        logger.warning("摊销成本翻页超过 %d 页，可能被截断: %s", _MAX_PAGES, day)
    return items


def fetch_resource_packages(ak: str, sk: str, endpoint: str = "business.aliyuncs.com") -> list:
    """拉取当前全部资源包实例（只读查询，无需开通）。pageNum 翻页，一次最多 100 条。"""
    client = _build_client(ak, sk, endpoint)
    from alibabacloud_bssopenapi20171214 import models as bss

    items: list = []
    page = 0
    size = 100
    while page < _MAX_PAGES:
        page += 1
        req = bss.QueryResourcePackageInstancesRequest(page_num=page, page_size=size)
        try:
            body = client.query_resource_package_instances(req).body
        except Exception as e:  # 网络/鉴权类
            raise BSSError(f"QueryResourcePackageInstances 失败: {str(e)[:300]}") from e
        data = _unwrap(body, "QueryResourcePackageInstances")
        page_items = (data.instances.instance if data.instances and data.instances.instance else []) or []
        items.extend(page_items)
        total = int(data.total_count or 0)
        if page * size >= total or not page_items:
            break
    if page >= _MAX_PAGES:
        logger.warning("资源包翻页超过 %d 页，可能被截断", _MAX_PAGES)
    return items


# 计费场景标签的关键词（InstanceConfig 是 K:V 串，取最具区分度的 1~2 段）
_SCENARIO_KEYS = ("存储类型", "计费", "类型", "规格", "系列", "架构", "带宽", "容灾", "模式", "区域")


def bill_label(it) -> str:
    """计费场景标签。

    QueryInstanceBill DAILY 不返回 BillingItem（字段为空），场景描述在
    InstanceConfig（如 "OSS地域:华北2（北京）;存储类型:标准型存储（同城冗余）"）。
    取法：BillingItem（若有值）> InstanceConfig 的场景 K:V > ProductDetail。
    """
    bi = _s(getattr(it, "billing_item", ""))
    if bi:
        return bi[:128]
    cfg = _s(getattr(it, "instance_config", ""))
    if cfg:
        pairs = [x.strip() for x in cfg.split(";") if ":" in x]
        preferred = [x for x in pairs if any(k in x.split(":", 1)[0] for k in _SCENARIO_KEYS)]
        chosen = (preferred or pairs)[:2]
        return "；".join(chosen)[:128]
    return _s(getattr(it, "product_detail", ""))[:128]


def aggregate_bill(items: list) -> list:
    """账单条目按 (product, instance, billing_item, subscription_type) 聚合成入库行。"""
    agg: dict = {}

    def key(it):
        return (
            _s(getattr(it, "product_code", "")),
            _s(getattr(it, "instance_id", "")),
            bill_label(it),
            _s(getattr(it, "subscription_type", "")),
        )

    for it in items:
        k = key(it)
        row = agg.get(k)
        if row is None:
            row = {
                "source": "bill",
                "product_code": k[0],
                "product_name": _s(getattr(it, "product_name", "")),
                "instance_id": k[1],
                "instance_name": _s(getattr(it, "nick_name", "")),
                "billing_item": k[2],
                "subscription_type": k[3],
                "region": _s(getattr(it, "region", "")),
                "pretax_amount": 0.0,
                "pretax_gross_amount": 0.0,
                "payment_amount": 0.0,
                "usage": 0.0,
            }
            agg[k] = row
        row["pretax_amount"] += _f(getattr(it, "pretax_amount", 0))
        row["pretax_gross_amount"] += _f(getattr(it, "pretax_gross_amount", 0))
        row["payment_amount"] += _f(getattr(it, "payment_amount", 0))
        row["usage"] += _f(getattr(it, "usage", 0))
        if not row["product_name"]:
            row["product_name"] = _s(getattr(it, "product_name", ""))
        if not row["instance_name"]:
            row["instance_name"] = _s(getattr(it, "nick_name", ""))
    return list(agg.values())


def aggregate_amortized(items: list, name_map: dict) -> list:
    """摊销条目聚合成入库行。该 API 无 billing_item/nick_name：
    billing_item 固定"摊销"，实例名从本地资产表 name_map(instance_id->name) 补。"""
    agg: dict = {}

    def key(it):
        return (
            _s(getattr(it, "product_code", "")),
            _s(getattr(it, "instance_id", "")),
            "摊销",
            _s(getattr(it, "subscription_type", "")) or "Subscription",
        )

    for it in items:
        k = key(it)
        row = agg.get(k)
        if row is None:
            row = {
                "source": "amortized",
                "product_code": k[0],
                "product_name": _s(getattr(it, "product_name", "")),
                "instance_id": k[1],
                "instance_name": name_map.get(k[1], ""),
                "billing_item": "摊销",
                "subscription_type": k[3],
                "region": _s(getattr(it, "region", "")),
                "pretax_amount": 0.0,
                "pretax_gross_amount": 0.0,
                "payment_amount": 0.0,
                "usage": 0.0,
            }
            agg[k] = row
        # 本期摊销金额（current_amortization_* 是当日摊销口径）
        row["pretax_amount"] += _f(getattr(it, "current_amortization_pretax_amount", 0))
        row["pretax_gross_amount"] += _f(getattr(it, "current_amortization_pretax_gross_amount", 0))
        row["payment_amount"] += _f(getattr(it, "current_amortization_expenditure_amount", 0))
        if not row["product_name"]:
            row["product_name"] = _s(getattr(it, "product_name", ""))
    return list(agg.values())
