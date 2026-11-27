"""阿里云 ActionTrail ChangeSource 适配器（只读 LookupEvents）。

SDK、分页、字段归一化和阿里噪声规则全部留在本模块；编排与入库在
app.services.change_source。fetch_events/sync_changes 仅保留旧调用兼容。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from app.models.cloud_account import CloudAccount
from app.services.change_source import ChangeBatch, NormalizedChange

logger = logging.getLogger("qianshu.actiontrail")

_NOISE_PREFIXES = ("Describe", "List", "Query", "Get", "ListTag", "Check")


class EventList(list):
    """list 兼容旧调用，同时把分页截断状态交给适配器。"""
    def __init__(self, values=(), *, truncated: bool = False):
        super().__init__(values)
        self.truncated = truncated


def _client(ak: str, sk: str, region_id: str = "cn-hangzhou"):
    from alibabacloud_actiontrail20171204.client import Client
    from alibabacloud_tea_openapi.models import Config

    cfg = Config(access_key_id=ak, access_key_secret=sk, region_id=region_id)
    cfg.endpoint = f"actiontrail.{region_id}.aliyuncs.com"
    return Client(cfg)


def _fmt_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_event_time(v) -> datetime | None:
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return dt.astimezone().replace(tzinfo=None)
    except ValueError:
        return None


def _extract(e: dict) -> tuple:
    rname = e.get("resourceName") or ""
    rtype = e.get("resourceType") or ""
    if not rname:
        res = e.get("resources") or e.get("referencedResources") or []
        if res:
            r0 = res[0] if isinstance(res[0], dict) else {}
            rname = r0.get("resourceName") or r0.get("resourceId") or ""
            rtype = rtype or r0.get("resourceType") or ""
    ui = e.get("userIdentity") or {}
    user = (ui.get("userName")
            or ((ui.get("sessionContext") or {}).get("attributes") or {}).get("displayName")
            or ui.get("principalId") or "?")
    return str(rname), str(rtype), str(user)


def _summarize(e: dict) -> str:
    rname, rtype, user = _extract(e)
    err = e.get("errorCode") or ""
    tail = f"（失败: {err}）" if err else ""
    return f"{user} 对 {rtype} {rname or '-'} 执行 {e.get('eventName', '?')}{tail}"


def fetch_events(ak: str, sk: str, start: datetime, end: datetime,
                 max_pages: int = 20, region_id: str = "cn-hangzhou") -> EventList:
    """拉取 [start, end) 原始 camelCase 事件；达到页上限仍有令牌则标截断。"""
    from alibabacloud_actiontrail20171204 import models

    c = _client(ak, sk, region_id)
    out = EventList()
    token = None
    for _ in range(max_pages):
        req = models.LookupEventsRequest(
            start_time=_fmt_utc(start), end_time=_fmt_utc(end),
            max_results=50, next_token=token)
        body = c.lookup_events(req).body.to_map()
        evs = body.get("Events") or body.get("events") or []
        out.extend(evs)
        token = body.get("NextToken") or body.get("nextToken")
        if not token or not evs:
            if not evs:
                token = None  # 空页是终止，不是“达到页上限导致不完整”
            break
        time.sleep(0.2)
    out.truncated = bool(token)
    return out


def normalize_event(e: dict) -> NormalizedChange | None:
    name = e.get("eventName") or e.get("EventName") or ""
    if not name or name.startswith(_NOISE_PREFIXES):
        return None
    external_id = e.get("eventId") or e.get("EventId") or ""
    if not external_id:
        return None
    rname, rtype, user = _extract(e)
    if not rname and not (e.get("referencedResources") or []):
        return None
    event_time = _parse_event_time(e.get("eventTime") or e.get("EventTime"))
    if event_time is None:
        return None
    return NormalizedChange(
        external_id=str(external_id), event_time=event_time,
        service=str(e.get("serviceName") or ""),
        event_name=str(name), event_source=str(e.get("eventSource") or ""),
        user=user, resource=rname, resource_type=rtype,
        error_code=str(e.get("errorCode") or ""), summary=_summarize(e), raw=e,
    )


class AlibabaActionTrailSource:
    key = "actiontrail"
    label = "阿里云操作审计 ActionTrail"

    def fetch(self, *, account: CloudAccount, ak: str, sk: str,
              start: datetime, end: datetime, max_pages: int = 20) -> ChangeBatch:
        raw = fetch_events(ak, sk, start, end, max_pages=max_pages,
                           region_id=account.region or "cn-hangzhou")
        events = [item for item in (normalize_event(e) for e in raw) if item is not None]
        return ChangeBatch(events=events, fetched=len(raw),
                           truncated=bool(getattr(raw, "truncated", False)))


def sync_changes(db, days: int = 2, notify_user: str = "") -> dict:
    """旧测试/外部调用兼容；生产消费者统一走 services.change_source。"""
    from app.services.change_source import sync_changes as sync
    return sync(db, days=days, notify_user=notify_user, legacy_raw_ids=True)
