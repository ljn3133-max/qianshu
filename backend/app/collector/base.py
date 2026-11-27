"""采集器共享工具：region 列表、资产 upsert。"""
from __future__ import annotations

import logging
import time
from typing import Optional

from sqlalchemy.orm import Session

from app.models.asset import Asset

logger = logging.getLogger("qianshu.sync")

_CONNECTIVITY_MARKERS = (
    "RemoteDisconnected", "Connection aborted", "Connection reset",
    "Connection refused", "Max retries exceeded", "EOF occurred",
    "Read timed out", "ConnectTimeout", "ReadTimeout", "timed out",
)


def is_connectivity_error(exc: Exception) -> bool:
    """判断是否为连接级失败（地域不可达/瞬时断连），而非权限或参数错误。

    Tea SDK 会把底层 requests/urllib3 异常包成 UnretryableException/RetryError，
    字符串里仍保留 RemoteDisconnected/Connection aborted 等标记；同时沿
    inner_exception/__cause__ 链检查真实的 ConnectionError/TimeoutError。
    """
    text = str(exc)
    if any(marker in text for marker in _CONNECTIVITY_MARKERS):
        return True
    seen: set[int] = set()
    inner = getattr(exc, "inner_exception", None)
    while inner is not None and id(inner) not in seen:
        seen.add(id(inner))
        if isinstance(inner, (ConnectionError, TimeoutError)):
            return True
        inner = getattr(inner, "inner_exception", None) or getattr(inner, "__cause__", None)
    return False


def list_regions(ak: str, sk: str) -> list:
    """用 ECS DescribeRegions 拿全量 region 列表（各产品通用）。

    连接级失败重试一次：ES 等产品会在拉 region 列表这一步遇到瞬时断连，
    若直接抛出会被上层记成整个产品失败；权限/参数类错误不重试。
    """
    from alibabacloud_ecs20140526 import models as ecs
    from alibabacloud_ecs20140526.client import Client
    from alibabacloud_tea_openapi.models import Config

    for attempt in (1, 2):
        try:
            client = Client(Config(access_key_id=ak, access_key_secret=sk,
                                   region_id="cn-hangzhou"))
            resp = client.describe_regions(ecs.DescribeRegionsRequest())
            return [r.region_id for r in resp.body.regions.region or []]
        except Exception as e:
            if attempt == 1 and is_connectivity_error(e):
                time.sleep(0.5)
                continue
            raise


def upsert_asset(
    db: Session, product: str, instance_id: str, seen_ids: set, fields: dict
) -> str:
    """按 (product, instance_id) upsert。返回 'created' | 'updated'。

    注意：session 是 autoflush=False，必须先 flush 让 pending 插入对 query 可见，
    否则同一实例出现两次会重复插入（ACK 的全局列表 API 就会这样）。
    """
    db.flush()
    row = (
        db.query(Asset)
        .filter(Asset.product == product, Asset.instance_id == instance_id)
        .first()
    )
    if row is None:
        from app.services.tenant import account_id as _acc
        fields.setdefault("account_id", _acc(db))   # 多账号打标（采集归属凭证账号）
        # VMware 默认辅助机器：虚拟化平台里大量模板/中间件/
        # 备份机不是业务核心，默认 core 会让指标告警与画像全量噪声化。只在新建时
        # 落默认值，后续用户在运维页手动改 core 不会被同步覆盖。
        if product in ("vmware_vm", "vmware_host"):
            fields.setdefault("tier", "aux")
        db.add(Asset(product=product, instance_id=instance_id, **fields))
        seen_ids.add(instance_id)
        return "created"
    # 归属自愈：账号上下文采集到的资产，
    # 更新时一并纠正 account_id——只在创建时打标会让存量行（尤其分组迁移洗入的
    # 旧数据分组 id）永远挂在"未归属"，资产总览/账号规则都看不到它们。
    from app.collector.credential import active_account_id
    _acc = active_account_id()
    if _acc is not None:
        row.account_id = _acc
    for k, v in fields.items():
        setattr(row, k, v)
    seen_ids.add(instance_id)
    return "updated"


def mark_missing_deleted(db: Session, product: str, seen_ids: set) -> int:
    """当次未见到且未标记过的 -> Deleted（保留历史，不物理删）。

    status='Manual' 是人工登记行（如外部注册域名到期时间），
    不在云账号清单里是常态，不能被清扫成 Deleted。
    账号上下文激活时只清扫本账号资产——否则账号 B 的同步
    会把账号 A 的实例误标 Deleted（跨账号幽灵删除）。
    """
    from app.collector.credential import active_account_id

    q = db.query(Asset).filter(Asset.product == product)
    acc = active_account_id()
    if acc is not None:
        q = q.filter(Asset.account_id == acc)
    missing = 0
    for row in q.all():
        if row.instance_id not in seen_ids and row.status not in ("Deleted", "Manual"):
            row.status = "Deleted"
            missing += 1
    return missing


def parse_time(v) -> Optional[object]:
    """兼容时间戳(ms, int)与 ISO 字符串两种形式，去 tz。"""
    from datetime import datetime

    if not v or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        try:
            return datetime.fromtimestamp(v / 1000)
        except (ValueError, OSError, OverflowError):
            return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None
