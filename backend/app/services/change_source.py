"""可插拔变更源：账号选择、只读拉取、统一幂等入库与结果汇总。

拉取适配器只需提供 key/label/fetch；API、调度、诊断和向导不直接依赖厂商 SDK。
Webhook 是推送入口，不实现本协议；其现有解析与入库路径保持不变。
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy.orm import Session

from app.models.change_event import ChangeEvent
from app.models.cloud_account import CloudAccount

logger = logging.getLogger("qianshu.change_source")


@dataclass
class NormalizedChange:
    external_id: str
    event_time: datetime
    service: str = ""
    event_name: str = ""
    event_source: str = ""
    user: str = ""
    resource: str = ""
    resource_type: str = ""
    error_code: str = ""
    summary: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class ChangeBatch:
    events: list[NormalizedChange] = field(default_factory=list)
    fetched: int = 0
    truncated: bool = False

    def __post_init__(self) -> None:
        if not self.fetched:
            self.fetched = len(self.events)


class ChangeSource(Protocol):
    key: str
    label: str

    def fetch(self, *, account: CloudAccount, ak: str, sk: str,
              start: datetime, end: datetime, max_pages: int = 20) -> ChangeBatch:
        """只读拉取并归一化；不得写云资源或本地数据库。"""
        ...


class ChangeSourceError(ValueError):
    pass


# 新源在此登记即可；默认厂商映射只决定未显式配置时选择哪个已注册源。
CHANGE_SOURCES: dict[str, ChangeSource] = {}
DEFAULT_SOURCE_BY_VENDOR = {"aliyun": "actiontrail"}


def register_change_source(source: ChangeSource) -> None:
    key = str(getattr(source, "key", "") or "").strip()
    label = str(getattr(source, "label", "") or "").strip()
    if not key or len(key) > 32 or not label or not callable(getattr(source, "fetch", None)):
        raise ChangeSourceError("变更源注册需要非空 key（<=32字符）/label 和 fetch 方法")
    if key in CHANGE_SOURCES and CHANGE_SOURCES[key] is not source:
        raise ChangeSourceError(f"变更源 {key} 已注册")
    CHANGE_SOURCES[key] = source


def _ensure_builtin_sources() -> None:
    if "actiontrail" not in CHANGE_SOURCES:
        from app.collector.actiontrail import AlibabaActionTrailSource
        register_change_source(AlibabaActionTrailSource())


def get_change_source(key: str) -> ChangeSource:
    _ensure_builtin_sources()
    source = CHANGE_SOURCES.get(str(key or "").strip())
    if source is None:
        raise ChangeSourceError(f"未知变更源: {key or '(空)'}；已注册: {sorted(CHANGE_SOURCES)}")
    return source


def account_extra(account: CloudAccount) -> dict:
    try:
        value = json.loads(account.extra_json or "{}")
    except (TypeError, ValueError) as exc:
        raise ChangeSourceError(f"账号 {account.name} 的 extra 配置不是有效 JSON") from exc
    if not isinstance(value, dict):
        raise ChangeSourceError(f"账号 {account.name} 的 extra 配置必须是对象")
    return value


def source_key_for_account(account: CloudAccount) -> str:
    explicit = str(account_extra(account).get("change_source") or "").strip()
    if explicit:
        # 内置默认源只接受其登记厂商；未映射的自定义源可显式用于任意账号。
        # 规则集中在注册表，API/同步消费者不出现厂商 if/else。
        mapped_vendors = {key: vendor for vendor, key in DEFAULT_SOURCE_BY_VENDOR.items()}
        required_vendor = mapped_vendors.get(explicit)
        if required_vendor and account.vendor != required_vendor:
            raise ChangeSourceError(
                f"变更源 {explicit} 仅适用于 {required_vendor}，不能用于账号 {account.name}（{account.vendor}）")
        return explicit
    key = DEFAULT_SOURCE_BY_VENDOR.get(account.vendor, "")
    if not key:
        raise ChangeSourceError(f"账号 {account.name}（{account.vendor}）未配置可用变更源")
    return key


def source_for_account(account: CloudAccount) -> ChangeSource:
    # 显式未知与厂商未支持都在这里清晰报错，消费者无需 vendor 分支。
    return get_change_source(source_key_for_account(account))


def validate_change_source_config(extra: dict, vendor: str = "") -> None:
    key = str((extra or {}).get("change_source") or "").strip()
    if key:
        get_change_source(key)
        if vendor:
            source_key_for_account(CloudAccount(
                name="配置校验", vendor=vendor,
                extra_json=json.dumps({"change_source": key}), enabled=True))


def change_sources_payload() -> dict:
    _ensure_builtin_sources()
    source_vendors = {key: vendor for vendor, key in DEFAULT_SOURCE_BY_VENDOR.items()}
    return {
        "items": [{"key": key, "label": source.label,
                   "vendor": source_vendors.get(key, "")}
                  for key, source in sorted(CHANGE_SOURCES.items())],
        "defaults": dict(DEFAULT_SOURCE_BY_VENDOR),
    }


def source_label(key: str) -> str:
    _ensure_builtin_sources()
    source = CHANGE_SOURCES.get(key)
    return source.label if source else key


def stored_source_key(value: str | None) -> str:
    """旧库空来源的显示兼容集中在适配层；消费者不识别具体厂商。"""
    return str(value or "") or DEFAULT_SOURCE_BY_VENDOR["aliyun"]


def legacy_account_source_key(db: Session) -> str:
    """无账号台账的旧部署也走同一选源规则，不让诊断复制默认厂商分支。"""
    return source_key_for_account(_legacy_account(db))


def event_storage_id(source_key: str, account_id: int, external_id: str) -> str:
    """固定 64 字符，避免不同源/账号原生日志 ID 冲突及简单截断碰撞。"""
    identity = f"change:v1\0{source_key}\0{int(account_id)}\0{external_id}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def save_change_batch(db: Session, *, source_key: str, account_id: int,
                      batch: ChangeBatch, legacy_raw_ids: bool = False) -> dict:
    """唯一云拉取入库边界：身份、建模、提交和画像直挂均在这里。

    legacy_raw_ids 仅供旧 actiontrail.sync_changes 兼容入口/旧回归使用；生产编排
    始终使用命名空间哈希。旧 ActionTrail 原生 eventId 行按同账号匹配，首轮不重复。
    """
    created = 0
    duplicates = 0
    for item in batch.events:
        external_id = str(item.external_id or "").strip()
        if not external_id or item.event_time is None:
            continue
        storage_id = event_storage_id(source_key, account_id, external_id)
        existing = db.query(ChangeEvent).filter(ChangeEvent.event_id == storage_id).first()
        if existing is None:
            existing = (db.query(ChangeEvent)
                        .filter(ChangeEvent.account_id == account_id,
                                ChangeEvent.source_type == source_key,
                                ChangeEvent.external_id == external_id)
                        .first())
        if existing is None and source_key == "actiontrail":
            # 升级前没有 source_type/external_id，event_id 直接保存原生 eventId。
            existing = (db.query(ChangeEvent)
                        .filter(ChangeEvent.account_id == account_id,
                                ChangeEvent.event_id == external_id,
                                ChangeEvent.source_type.in_(("", "actiontrail")))
                        .first())
        if existing is not None:
            duplicates += 1
            continue
        event_id = external_id if legacy_raw_ids else storage_id
        if legacy_raw_ids and db.query(ChangeEvent.id).filter(
                ChangeEvent.event_id == event_id).first() is not None:
            event_id = storage_id
        ev = ChangeEvent(
            account_id=account_id,
            event_id=event_id,
            source_type=source_key,
            external_id=external_id[:255],
            event_time=item.event_time,
            service=str(item.service or "")[:64],
            event_name=str(item.event_name or "")[:128],
            event_source=str(item.event_source or "")[:128],
            user=str(item.user or "")[:128],
            resource=str(item.resource or "")[:255],
            resource_type=str(item.resource_type or "")[:64],
            error_code=str(item.error_code or "")[:64],
            summary=str(item.summary or "")[:2000],
            raw=json.dumps(item.raw or {}, ensure_ascii=False, default=str)[:20000],
        )
        db.add(ev)
        db.flush()  # land_hook 需要 change:{id}，同时尽早暴露唯一约束冲突
        try:
            from app.services.profile_engine import land_hook
            land_hook(db, "change", ev)
        except Exception:
            logger.warning("画像联动钩子异常", exc_info=True)
        created += 1
    db.commit()
    return {"created": created, "duplicates": duplicates}


def _legacy_account(db: Session) -> CloudAccount:
    from app.services.tenant import account_id
    return CloudAccount(id=account_id(db), name="默认阿里云凭证", vendor="aliyun",
                        region="", extra_json="{}", enabled=True)


def _credentials(account: CloudAccount) -> tuple[str, str]:
    from app.core.security import decrypt_secret
    if not (account.ak_cipher and account.secret_cipher):
        raise ChangeSourceError("账号缺少 AK/SK")
    return decrypt_secret(account.ak_cipher), decrypt_secret(account.secret_cipher)


def sync_changes(db: Session, days: int = 2, notify_user: str = "",
                 account_id: int = 0, legacy_raw_ids: bool = False) -> dict:
    """按确切启用账号逐源同步；单账号失败/截断不会被汇总成功吞掉。"""
    del notify_user  # 保留旧调用签名；通知不属于只读变更拉取。
    end = datetime.now()
    start = end - timedelta(days=days)
    q = db.query(CloudAccount).filter(CloudAccount.enabled.is_(True))
    if account_id:
        q = q.filter(CloudAccount.id == account_id)
    accounts = q.order_by(CloudAccount.id).all()
    explicit_missing = bool(account_id and not accounts)
    has_any_accounts = db.query(CloudAccount.id).first() is not None
    legacy_fallback = not accounts and not account_id and not has_any_accounts
    if legacy_fallback:
        # 仅账号台账完全为空（旧部署）才保留旧阿里凭证表/环境变量路径；
        # 有账号但全部停用是用户明确停采，绝不能继续拉默认凭证。
        accounts = [_legacy_account(db)]

    results: list[dict] = []
    if not accounts and not explicit_missing:
        results.append({"ok": False, "account_id": 0, "account": "", "source": "",
                        "fetched": 0, "created": 0, "error": "没有启用中的云账号",
                        "error_code": "no_enabled_accounts"})
    if explicit_missing:
        results.append({"ok": False, "account_id": account_id, "account": "",
                        "source": "", "fetched": 0, "created": 0,
                        "error": "目标云账号不存在或已停用", "error_code": "account_unavailable"})
    for account in accounts:
        base = {"account_id": account.id, "account": account.name,
                "vendor": account.vendor, "source": "", "source_label": "",
                "fetched": 0, "created": 0, "duplicates": 0, "truncated": False}
        try:
            source = source_for_account(account)
            base.update(source=source.key, source_label=source.label)
            if legacy_fallback:
                from app.collector.credential import resolve_aliyun_credential
                ak, sk, _ = resolve_aliyun_credential(db)
            else:
                ak, sk = _credentials(account)
            batch = source.fetch(account=account, ak=ak, sk=sk,
                                 start=start, end=end, max_pages=20)
            saved = save_change_batch(db, source_key=source.key, account_id=account.id,
                                      batch=batch, legacy_raw_ids=legacy_raw_ids)
            base.update(ok=True, fetched=batch.fetched, truncated=batch.truncated, **saved)
            if batch.truncated:
                base["note"] = "拉取达到分页上限，本窗口不完整；请缩小时间窗后重试"
        except Exception as exc:
            db.rollback()
            logger.warning("账号 %s 变更源同步失败: %s", account.name, str(exc)[:200])
            base.update(ok=False, error=str(exc)[:300],
                        error_code="source_failed" if base["source"] else "source_unavailable")
        results.append(base)

    errors = [{"account_id": r.get("account_id"), "account": r.get("account", ""),
               "source": r.get("source", ""), "error": r.get("error", "")}
              for r in results if not r.get("ok")]
    return {
        "ok": bool(results) and not errors,
        "fetched": sum(int(r.get("fetched", 0) or 0) for r in results),
        "created": sum(int(r.get("created", 0) or 0) for r in results),
        "truncated": any(bool(r.get("truncated")) for r in results),
        "results": results,
        "errors": errors,
    }


def collect_account(db: Session, account: CloudAccount, *, hours: int = 24,
                    max_pages: int = 20, resource_ids: list[str] | None = None,
                    source_id: str = "") -> dict:
    """新手接入专用：显式账号、限定窗口、无通知的只读变更拉取。"""
    from app.collector.credential import account_credential

    if account is None or not account.enabled:
        raise ChangeSourceError("变更账号不存在或已停用")
    if not 1 <= hours <= 168 or not 1 <= max_pages <= 100:
        raise ValueError("变更窗口必须为 1~168 小时，分页上限必须为 1~100")
    end = datetime.now()
    start = end - timedelta(hours=hours)
    source_key = ""
    try:
        source = source_for_account(account)
        source_key = source.key
        if source_id and source.key != source_id:
            raise ChangeSourceError(f"请求变更源 {source_id} 与账号实际源 {source.key} 不一致")
        ak, sk = _credentials(account)
        with account_credential(ak, sk, account.id, source=f"cloud_account:{account.name}"):
            batch = source.fetch(account=account, ak=ak, sk=sk,
                                 start=start, end=end, max_pages=max_pages)
        if resource_ids:
            wanted = set(resource_ids)
            batch = ChangeBatch(
                events=[item for item in batch.events if item.resource in wanted],
                fetched=sum(item.resource in wanted for item in batch.events),
                truncated=batch.truncated)
        saved = save_change_batch(db, source_key=source.key, account_id=account.id, batch=batch)
    except Exception as exc:
        db.rollback()
        text = str(exc)
        lowered = text.lower()
        denied = any(x in lowered for x in ("403", "permission", "forbidden", "unauthorized"))
        unavailable = isinstance(exc, ChangeSourceError)
        return {
            "account_id": account.id, "source_id": source_key, "ok": False,
            "data_state": "unknown",
            "reason_code": ("permission_denied" if denied else
                            "source_unavailable" if unavailable else "source_failed"),
            "retryable": not (denied or unavailable), "window_start": start, "window_end": end,
            "fetched": 0, "created": 0, "duplicates": 0,
            "evidence_refs": [f"change_window:{account.id}:{source_key or 'unknown'}:{start.isoformat()}:{end.isoformat()}"],
            "observed_until": None, "truncated": False, "total_known": False,
            "error": text[:300],
        }
    available = bool(batch.events)
    window_ref = f"change_window:{account.id}:{source.key}:{start.isoformat()}:{end.isoformat()}"
    evidence = [window_ref] + [f"changes:{account.id}:{source.key}:{item.external_id}"
                              for item in batch.events if item.external_id][:99]
    return {
        "account_id": account.id, "source_id": source.key,
        "ok": not batch.truncated,
        "data_state": "available" if available else ("unknown" if batch.truncated else "no_data"),
        "reason_code": "window_truncated" if batch.truncated else ("changes_ready" if available else "empty_window"),
        # 截断需要用户缩小窗口；显式手动重试仍允许，自动编排不反复打满相同上限。
        "retryable": not batch.truncated,
        "next_action": "narrow_window" if batch.truncated else "",
        "window_start": start, "window_end": end,
        "fetched": batch.fetched, **saved, "evidence_refs": evidence,
        "observed_until": max((item.event_time for item in batch.events), default=None),
        "truncated": batch.truncated,
        "total_known": not batch.truncated, "error": "",
    }


def probe_change_source(account: CloudAccount, ak: str, sk: str,
                        hours: int = 1) -> tuple[ChangeSource, ChangeBatch]:
    """连通探测复用适配器的一页 fetch，不要求新增 probe 方法。"""
    source = source_for_account(account)
    end = datetime.now()
    batch = source.fetch(account=account, ak=ak, sk=sk,
                         start=end - timedelta(hours=hours), end=end, max_pages=1)
    return source, batch
