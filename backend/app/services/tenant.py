"""多账号服务：账号归属打标 + 查询过滤。

口径：数据打标 account_id（采集时归属凭证账号），查询层按 X-Account-Id 过滤或聚合。
默认账号 id=1（单账号部署无感）。

2026-11-09 双概念唯一化：数据表 account_id **显式指向 cloud_accounts.id**；
旧 accounts 表退为纯「数据分组」标签（展示面已改名），分组经 bridge_group_to_cloud
洗入台账（保 id），两套 id 从此同轴，展示层不再需要"合并兜底接缝"。
"""
from __future__ import annotations

import logging

logger = logging.getLogger("qianshu.tenant")

DEFAULT_ACCOUNT_ID = 1


def group_to_cloud_id(db, group_id: int) -> int:
    """数据分组 id → 云账号台账 id。

    桥接行与分组同 id（extra_json.migrated_from='accounts_p22'）；找不到对应
    台账行时原样返回（历史单账号数据不造引用，展示层自行留空）。
    """
    if not group_id:
        return 0
    try:
        import json

        from app.models.cloud_account import CloudAccount

        row = db.get(CloudAccount, group_id)
        if row is not None:
            return row.id
        for c in db.query(CloudAccount).all():
            try:
                extra = json.loads(c.extra_json or "{}")
            except Exception:
                extra = {}
            if (extra.get("data_group") or {}).get("id") == group_id:
                return c.id
    except Exception:
        logger.debug("分组→台账映射失败（原样返回）", exc_info=True)
    return group_id


def bridge_group_to_cloud(db, group) -> tuple:
    """数据分组 → 云账号台账镜像行（同 id）。

    为什么镜像：数据列 account_id 显式指向 cloud_accounts，分组行洗入台账（保 id）
    后存量/新数据零改写都落在台账语义上。口径（红线：不猜）：vendor 留空
    （厂商未登记不猜）、enabled=0（无密钥不进采集轮次），只作账号维度参照。
    幂等：同 id 已存在则不建。返回 (CloudAccount, created)。
    """
    import json

    from app.models.cloud_account import CloudAccount

    existing = db.get(CloudAccount, group.id)
    if existing is not None:
        return existing, False
    name = group.name
    taken = {c.name for c in db.query(CloudAccount.name).all()}
    if name in taken:
        name = f"{group.name}（分组{group.id}）"
    row = CloudAccount(
        id=group.id, name=name, vendor="", ak_cipher="", secret_cipher="",
        extra_json=json.dumps({
            "migrated_from": "accounts_p22",
            "data_group": {"id": group.id, "name": group.name,
                           "kind": getattr(group, "kind", ""),
                           "remark": getattr(group, "remark", "")},
        }, ensure_ascii=False),
        enabled=False, status="untested",
    )
    db.add(row)
    db.flush()
    # pg 显式 id 灌入后序列不推进：校准防撞主键
    try:
        from sqlalchemy import text as _text

        if db.get_bind().dialect.name == "postgresql":
            db.execute(_text(
                "SELECT setval(pg_get_serial_sequence('cloud_accounts','id'),"
                " COALESCE((SELECT MAX(id) FROM cloud_accounts), 0) + 1, false)"))
    except Exception:
        logger.debug("cloud_accounts 序列校准失败（忽略）", exc_info=True)
    return row, True


def account_id(db) -> int:
    """当前采集归属账号：账号上下文优先（多账号同步打标），否则默认凭证账号。

    2026-11-09 双概念统一：返回值统一为 cloud_accounts.id——旧凭证的 account_id 是
    数据分组 id，经 group_to_cloud_id 映射到台账（分组已洗入台账保 id，通常恒等）。

    2026-11-09 凭证体系迁移：旧凭证表档位改为迁移后读台账——无上下文时取
    云账号台账（启用中的 aliyun，与 resolve_aliyun_credential 同序）的 id；
    旧凭证表档保留兜底（旧行为向后兼容：旧表空时行为不变）。
    """
    try:
        from app.collector.credential import active_account_id

        acc = active_account_id()
        if acc:
            return acc
    except Exception:
        pass
    try:
        from app.models.cloud_account import CloudAccount

        row = (db.query(CloudAccount)
               .filter(CloudAccount.enabled == True,  # noqa: E712
                       CloudAccount.vendor == "aliyun")
               .order_by(CloudAccount.id).first())
        if row is not None:
            return row.id
    except Exception:
        pass
    try:
        from app.models.credential import CloudCredential

        row = (db.query(CloudCredential)
               .filter(CloudCredential.enabled == True,  # noqa: E712
                       CloudCredential.vendor == "aliyun")
               .order_by(CloudCredential.is_default.desc(), CloudCredential.id)
               .first())
        if row is not None and getattr(row, "account_id", None):
            return group_to_cloud_id(db, row.account_id)
    except Exception:
        pass
    return DEFAULT_ACCOUNT_ID


def account_filter(q, model, account_id_val):
    """查询过滤：account_id_val 为 0/None=不过滤（聚合视图）。"""
    if account_id_val:
        q = q.filter(model.account_id == account_id_val)
    return q


def account_id_param(params: dict) -> int:
    """工具参数 account_id 解析。

    缺省/空/0 = 不过滤（全部账号聚合）；非法值抛 ValueError（调用方转 ToolError
    面向 LLM 纠错）。字符串数字容忍（LLM 参数常带引号）。
    """
    v = (params or {}).get("account_id")
    if v in (None, "", 0, "0"):
        return 0
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError(f"account_id 必须是整数，收到: {v!r}")
    return max(n, 0)


def account_fields(acc_map: dict, account_id_val, with_vendor: bool = False) -> dict:
    """归属字段补丁：account_id/account_name（可选 vendor），与 alerts/expiry 同口径。

    account_id 在映射中无对应行时 name 留空（前端不显示标签）。
    """
    aid = account_id_val or 0
    acc = acc_map.get(aid) or {}
    out = {"account_id": aid, "account_name": acc.get("name", "")}
    if with_vendor:
        out["vendor"] = acc.get("vendor", "")
    return out


def account_index(db) -> dict:
    """account_id → {name, vendor} 归属标识映射。

    2026-11-09 双概念唯一化后：account_id 显式指向 cloud_accounts（主源，
    带厂商）；accounts（数据分组）兜底仅限迁移前未洗入的极老 id（兼容保留，
    不再是常态口径）。两处都无该 id 时留空（前端不显示标签，不造数据）。
    """
    out: dict = {}
    try:
        from app.models.account import Account

        for a in db.query(Account).all():
            out[a.id] = {"name": a.name, "vendor": ""}
    except Exception:
        logger.debug("accounts 映射加载失败（忽略）", exc_info=True)
    try:
        from app.models.cloud_account import CloudAccount

        for c in db.query(CloudAccount).all():
            out[c.id] = {"name": c.name, "vendor": c.vendor or ""}   # 云账号优先
    except Exception:
        logger.debug("cloud_accounts 映射加载失败（忽略）", exc_info=True)
    return out
