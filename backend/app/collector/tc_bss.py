"""腾讯云 BSS（费用中心）只读封装。

只读数据源（SDK 签名依据 tencentcloud.billing.v20180709 introspection）：
- DescribeBillDetail：账单明细（PeriodType=2 按天，BeginTime/EndTime 取当日，Offset/Limit
  翻页）——即控制台"账单明细"口径。返回 DetailSet（BillDetail 列表），每条 BillDetail
  含 ComponentSet（BillDetailComponent 列表），一个组件=一个计费项。
- DescribeAccountBalance：账户余额（现金/赠送/欠费），只作采集状态旁注，不入账。

金额口径（与阿里 collector/bss.py 对齐入 cost_daily，source='tc_bill'）：
  pretax_amount       = RealCost（优惠后总价——判断"花了多少钱"的唯一口径）
  pretax_gross_amount = Cost（组件原价）
  payment_amount      = CashPayAmount（现金账户支出，不作花费依据）
  usage               = UsedAmount（组件用量）
  subscription_type   = PayModeName 归一（"包年包月"->Subscription、"按量*"->PayAsYouGo，
                        其余原值透传截 16 位——cost_daily 列宽）
  billing_item        = ComponentCodeName·ItemCodeName（计费场景标签，同阿里"计费场景"口径）
  product_code/name   = BusinessCode / BusinessCodeName（回退 ProductCode/ProductCodeName）
  region              = RegionId（回退 RegionName）

字段映射以公开 API 文档和 SDK 模型为准；接入真实账号时仍需核对字段名与金额口径。
"""
from __future__ import annotations

import logging
from datetime import date

logger = logging.getLogger("qianshu.tc_bss")

_MAX_PAGES = 200
_PAGE_SIZE = 100   # DescribeBillDetail 上限


class TCBSSError(RuntimeError):
    """code 属性保存业务错误码，便于上层区分软/硬错误（与 BSSError 同构）。"""

    def __init__(self, msg: str, code: str = ""):
        super().__init__(msg)
        self.code = code


def _billing_client(ak: str, sk: str, region: str = "ap-guangzhou"):
    """腾讯云计费 client（bssopenapi -> tencentcloud.billing.v20180709，全局服务，
    region 仅 SDK 构造需要）。seam：mock 测试替换本函数。"""
    from tencentcloud.billing.v20180709 import billing_client
    from tencentcloud.common import credential as tencent_credential

    return billing_client.BillingClient(tencent_credential.Credential(ak, sk), region)


def _f(v) -> float:
    """腾讯账单金额/用量字段是字符串（SDK introspection 确认），None/空按 0。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _s(v) -> str:
    return str(v) if v is not None else ""


def _pay_mode(name) -> str:
    """PayModeName -> cost_daily.subscription_type 归一（同阿里 Subscription/PayAsYouGo 口径）。"""
    n = _s(name)
    if "包年" in n or "包月" in n:
        return "Subscription"
    if "按量" in n:
        return "PayAsYouGo"
    return n[:16]


def bill_label(detail, comp) -> str:
    """计费场景标签：组件大类·组件名称（对照阿里 bill_label 的"计费场景"口径）。"""
    cat = _s(getattr(comp, "ComponentCodeName", ""))
    item = _s(getattr(comp, "ItemCodeName", ""))
    if cat and item and cat != item:
        return f"{cat}·{item}"[:128]
    return (item or cat)[:128]


def fetch_day_bill(ak: str, sk: str, day: date, region: str = "ap-guangzhou") -> list:
    """拉取一天的账单明细（只读查询），返回 BillDetail 条目列表。

    PeriodType=2（按天）+ BeginTime/EndTime 取当日 00:00:00~23:59:59，Offset/Limit 翻页。
    """
    from tencentcloud.billing.v20180709 import models

    client = _billing_client(ak, sk, region)
    items: list = []
    offset = 0
    total = None
    for _ in range(_MAX_PAGES):
        req = models.DescribeBillDetailRequest()
        req.PeriodType = "2"
        req.BeginTime = f"{day.isoformat()} 00:00:00"
        req.EndTime = f"{day.isoformat()} 23:59:59"
        req.Offset = offset
        req.Limit = _PAGE_SIZE
        req.NeedRecordNum = "1"
        try:
            resp = client.DescribeBillDetail(req)
        except Exception as e:  # 网络/鉴权/业务错误统一包装（保留错误码）
            code = _s(getattr(e, "code", "") or getattr(e, "get_code", lambda: "")())
            raise TCBSSError(f"DescribeBillDetail 失败 {day}: {str(e)[:300]}", code=code) from e
        batch = list(getattr(resp, "DetailSet", None) or [])
        items.extend(batch)
        total = getattr(resp, "Total", None)
        offset += len(batch)
        if not batch or (total is not None and offset >= int(total or 0)):
            break
    else:
        logger.warning("腾讯账单翻页超过 %d 页，可能被截断: %s", _MAX_PAGES, day)
    logger.info("腾讯账单明细 %s: %d 条", day, len(items))
    return items


def fetch_account_balance(ak: str, sk: str, region: str = "ap-guangzhou") -> dict:
    """账户余额（只读）：{balance, cash, owe, uin}——采集状态旁注用，不入账。"""
    from tencentcloud.billing.v20180709 import models

    client = _billing_client(ak, sk, region)
    try:
        resp = client.DescribeAccountBalance(models.DescribeAccountBalanceRequest())
    except Exception as e:
        code = _s(getattr(e, "code", "") or getattr(e, "get_code", lambda: "")())
        raise TCBSSError(f"DescribeAccountBalance 失败: {str(e)[:300]}", code=code) from e
    return {
        "balance": _f(getattr(resp, "RealBalance", 0)),        # 可用余额
        "cash": _f(getattr(resp, "CashAccountBalance", 0)),    # 现金账户
        "owe": _f(getattr(resp, "OweAmount", 0)),              # 欠费金额
        "uin": _s(getattr(resp, "Uin", "")),
    }


def aggregate_bill(items: list) -> list:
    """账单条目按 (product, instance, billing_item, subscription_type) 聚合成入库行。

    与阿里 aggregate_bill 同构（source='tc_bill'）：一个 BillDetail 的每个组件
    ComponentSet[i] 展开为一个计费项，同 key 组件金额求和。
    """
    agg: dict = {}

    for it in items:
        product_code = (_s(getattr(it, "BusinessCode", ""))
                        or _s(getattr(it, "ProductCode", "")))
        product_name = (_s(getattr(it, "BusinessCodeName", ""))
                        or _s(getattr(it, "ProductCodeName", "")))
        instance_id = _s(getattr(it, "ResourceId", ""))
        instance_name = _s(getattr(it, "ResourceName", ""))
        sub_type = _pay_mode(getattr(it, "PayModeName", ""))
        region = (_s(getattr(it, "RegionId", ""))
                  or _s(getattr(it, "RegionName", "")))

        comps = list(getattr(it, "ComponentSet", None) or [])
        if not comps:  # 空组件防御：留一条零金额行，实例不丢
            logger.warning("账单条目无组件（%s/%s），按零金额入账", product_code, instance_id)
            comps = [None]

        for comp in comps:
            label = bill_label(it, comp) if comp is not None else ""
            k = (product_code, instance_id, label, sub_type)
            row = agg.get(k)
            if row is None:
                row = {
                    "source": "tc_bill",
                    "product_code": product_code,
                    "product_name": product_name,
                    "instance_id": instance_id,
                    "instance_name": instance_name,
                    "billing_item": label,
                    "subscription_type": sub_type,
                    "region": region,
                    "pretax_amount": 0.0,
                    "pretax_gross_amount": 0.0,
                    "payment_amount": 0.0,
                    "usage": 0.0,
                }
                agg[k] = row
            if comp is not None:
                # 金额口径对齐阿里 pretax/pretax_gross/payment/usage（见模块 docstring）
                row["pretax_amount"] += _f(getattr(comp, "RealCost", 0))
                row["pretax_gross_amount"] += _f(getattr(comp, "Cost", 0))
                row["payment_amount"] += _f(getattr(comp, "CashPayAmount", 0))
                row["usage"] += _f(getattr(comp, "UsedAmount", 0))
            if not row["product_name"]:
                row["product_name"] = product_name
            if not row["instance_name"]:
                row["instance_name"] = instance_name
    return list(agg.values())
