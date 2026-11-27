"""跨云管理面：多云/多账号资产·费用·告警汇总视图。

数据基础=account_id 打标；每个账号一列（资产分布/近 7 天费用/告警数），一眼看全貌。

归属回流：**聚合前补归属**——先按归属登记
（ownership_rules）解析每个实体的归属账号（登记 > 采集打标；绑定云账号台账=该
台账账号，自由文本=稳定虚拟账号），登记过的实体归到登记账号的行（台账外/自由
文本账号如实补行）；无登记保持采集账号聚合（红线：不造数据、不改原状）。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.account import Account
from app.models.alert_record import AlertRecord
from app.models.asset import Asset
from app.models.cost_daily import CostDaily
from app.models.user import User
from app.services.ipnorm import extra_ips

logger = logging.getLogger("qianshu.multicloud_api")
router = APIRouter(prefix="/multi-cloud", tags=["multi-cloud"])

# 自建/虚拟化厂商码 → 账号类型 idc（补行 kind 由登记厂商推导；厂商未登记不猜）。
# 能力位唯一事实源：非计费且具备本地探活/采集/Agent 通道的厂商。
from app.services.cloud.provider import PROVIDERS, capabilities_of  # noqa: E402

_ONPREM_VENDORS = tuple(
    v for v in PROVIDERS
    if not capabilities_of(v).billing
    and (capabilities_of(v).probe or capabilities_of(v).sync or capabilities_of(v).agent)
)


@router.get("/overview")
def multi_cloud_overview(db: Session = Depends(get_db),
                         user: User = Depends(get_current_user)):
    """跨云总览：按账号聚合资产/费用/告警。行带厂商标识。"""
    accounts = db.query(Account).order_by(Account.id).all()
    if not accounts:
        accounts = [Account(id=1, name="默认账号", kind="cloud")]
    since = datetime.now().date() - timedelta(days=7)
    # 归属标识：account_id -> {vendor, name}，与 alerts/expiry 同口径
    from app.services import ownership as own
    from app.services.tenant import account_index

    acc_map = account_index(db)
    own_rules = own.load_rules(db)

    # ---- 聚合前补归属：逐实体解析归属账号，再按归属聚合 ----
    buckets: dict = {}

    def _eff(hit, collected):
        """登记了账号归属 → 登记账号（绑定=台账 id / 自由文本=稳定虚拟 id）；
        否则采集账号（原状）。返回 (账号 id, 是否来自登记)。"""
        if hit is not None:
            if hit.get("account_id"):
                return hit["account_id"], True
            if hit.get("account_name"):
                return own.pseudo_account_id(hit.get("vendor", ""), hit["account_name"]), True
        return collected or 0, False

    def _bucket(aid: int, hit=None):
        b = buckets.setdefault(aid, {"assets": {}, "cost": 0.0, "alerts": 0,
                                     "name": "", "vendor": "", "kind": "",
                                     "registered": False})
        if hit is not None and not b["registered"]:
            got = own.hit_account(hit, acc_map)
            b["name"], b["vendor"] = got["account_name"], got["vendor"]
            if got["vendor"]:
                b["kind"] = "idc" if got["vendor"] in _ONPREM_VENDORS else "cloud"
            b["registered"] = True
        return b

    for a in db.query(Asset).filter(Asset.status != "Deleted").all():
        try:
            extra = json.loads(a.extra or "{}")
        except Exception:
            extra = {}
        ips = extra_ips(extra)   # IP 提取与归一使用同一实现
        hit = own.resolve(own_rules, instance=a.instance_id, name=a.name, ips=ips)
        aid, reg = _eff(hit, a.account_id)
        b = _bucket(aid, hit if reg else None)
        b["assets"][a.product] = b["assets"].get(a.product, 0) + 1

    # 金额口径：cost_7d=pretax_amount（账单金额，判断花费的唯一口径）
    for r in db.query(CostDaily).filter(CostDaily.stat_date >= since).all():
        hit = own.resolve(own_rules, instance=r.instance_id, name=r.instance_name)
        aid, reg = _eff(hit, r.account_id)
        _bucket(aid, hit if reg else None)["cost"] += float(r.pretax_amount or 0)

    for r in db.query(AlertRecord).all():
        hit = own.resolve(own_rules, **own.target_keys(r.target, r.target_name))
        aid, reg = _eff(hit, r.account_id)
        _bucket(aid, hit if reg else None)["alerts"] += 1

    out = []
    for a in accounts:
        b = buckets.pop(a.id, {"assets": {}, "cost": 0.0, "alerts": 0})
        out.append({
            "id": a.id, "name": a.name, "kind": a.kind,
            "vendor": (acc_map.get(a.id) or {}).get("vendor", ""),
            "assets_total": sum(b["assets"].values()),
            "assets_by_product": b["assets"],
            "cost_7d": round(b["cost"], 2),
            "alerts_total": b["alerts"],
        })
    # 登记归属的台账外/自由文本账号：如实补行（配置即回流，行内数字=该账号真实成员）；
    # 无登记的台账外账号保持不可见（原状，不造数据）
    for aid in sorted(buckets):
        b = buckets[aid]
        if not b["registered"] or not (b["assets"] or b["cost"] or b["alerts"]):
            continue
        out.append({
            "id": aid, "name": b["name"] or f"账号 {aid}",
            "kind": b["kind"],   # 厂商未登记不猜 cloud/idc（如实留空）
            "vendor": b["vendor"],
            "assets_total": sum(b["assets"].values()),
            "assets_by_product": b["assets"],
            "cost_7d": round(b["cost"], 2),
            "alerts_total": b["alerts"],
        })
    total_cost = sum(x["cost_7d"] for x in out)
    return {
        "accounts": out,
        "totals": {"accounts": len(out),
                   "assets": sum(x["assets_total"] for x in out),
                   "cost_7d": round(total_cost, 2),
                   "alerts": sum(x["alerts_total"] for x in out)},
        "note": "口径：cost_7d=账单金额（pretax_amount，判断花费以此为准）。T+1 账单，近 7 天可能补账修订",
    }
