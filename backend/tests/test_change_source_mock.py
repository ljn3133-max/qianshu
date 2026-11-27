#!/usr/bin/env python3
"""ChangeSource 隔离回归：一次性 PostgreSQL + 假源/假 SDK，零真实云调用、零运行态重启。"""
import asyncio
import importlib
import json
import os
import pkgutil
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

TMP = tempfile.mkdtemp(prefix="qianshu-change-source-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("changes")
os.environ["MASTER_KEY"] = "cd" * 32
os.environ["QIANSHU_SCHEDULER_DISABLED"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI  # noqa: E402
import httpx  # noqa: E402

from app.core.db import Base, SessionLocal, engine, get_db  # noqa: E402
import app.models as model_pkg  # noqa: E402
for mod in pkgutil.iter_modules(model_pkg.__path__):
    importlib.import_module(f"app.models.{mod.name}")
Base.metadata.create_all(engine)

from app.api.v1.changes import router as changes_router  # noqa: E402
from app.collector import actiontrail  # noqa: E402
from app.core.deps import get_current_user  # noqa: E402
from app.core.migrate import migrate_change_sources  # noqa: E402
from app.core.security import encrypt_secret  # noqa: E402
from app.models.audit import AuditLog  # noqa: E402
from app.models.change_event import ChangeEvent  # noqa: E402
from app.models.cloud_account import CloudAccount  # noqa: E402
from app.services import change_source as cs  # noqa: E402
from app.services import profile_engine  # noqa: E402

PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print(f"  [{'ok' if condition else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not condition else ""))


def event(external_id="native-1"):
    return cs.NormalizedChange(
        external_id=external_id, event_time=datetime.now(), service="ecs",
        event_name="RebootInstance", user="tester", resource="i-1", raw={"eventId": external_id})


class FakeSource:
    def __init__(self, key, handler):
        self.key = key
        self.label = f"Fake {key}"
        self.handler = handler
        self.calls = []

    def fetch(self, **kwargs):
        self.calls.append(kwargs)
        return self.handler(**kwargs)


def account(name, vendor="aliyun", source="", enabled=True, ak=None):
    extra = {"change_source": source} if source else {"nested": {"keep": name}}
    row = CloudAccount(name=name, vendor=vendor, enabled=enabled, region="cn-test",
                       ak_cipher=encrypt_secret(ak or f"ak-{name}-123456"),
                       secret_cipher=encrypt_secret(f"sk-{name}-123456"),
                       extra_json=json.dumps(extra))
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def clear_rows():
    for model in (ChangeEvent, CloudAccount, AuditLog):
        db.query(model).delete()
    db.commit()
    db.expunge_all()


db = SessionLocal()
hooks = []
profile_engine.land_hook = lambda _db, kind, ev: hooks.append((kind, ev.id, ev.event_id))

print("== A. 注册表 / 默认与显式选择 / 错误 ==")
a_default = account("default-a")
a_explicit = account("explicit-a", source="future-fake")
a_unknown = account("unknown-a", source="not-registered")
a_unsupported = account("unsupported-t", vendor="tencent")
fake_empty = FakeSource("future-fake", lambda **kw: cs.ChangeBatch(events=[event()], fetched=1))
cs.register_change_source(fake_empty)
check("A1 阿里云缺省 actiontrail", cs.source_key_for_account(a_default) == "actiontrail")
check("A2 extra.change_source 显式选择", cs.source_for_account(a_explicit) is fake_empty)
cs.probe_change_source(a_explicit, "probe-ak", "probe-sk")
check("A2b 探测复用一页 fetch", fake_empty.calls[-1]["max_pages"] == 1
      and fake_empty.calls[-1]["ak"] == "probe-ak")
wrong_vendor = CloudAccount(name="wrong", vendor="tencent",
                            extra_json=json.dumps({"change_source": "actiontrail"}))
try:
    cs.source_for_account(wrong_vendor)
    check("A2c 内置源不跨厂商借凭证", False)
except cs.ChangeSourceError:
    check("A2c 内置源不跨厂商借凭证", True)
for label, row in (("unknown", a_unknown), ("unsupported", a_unsupported)):
    try:
        cs.source_for_account(row)
        check(f"A3 {label} 清晰失败", False)
    except cs.ChangeSourceError as exc:
        check(f"A3 {label} 清晰失败", bool(str(exc)), str(exc))
try:
    cs.register_change_source(SimpleNamespace(key="bad", label="", fetch=lambda: None))
    check("A4 注册契约校验", False)
except cs.ChangeSourceError:
    check("A4 注册契约校验", True)
clear_rows()

print("== B. 阿里归一化 / 分页 / 截断 ==")
now = datetime.now(timezone.utc)
raw = {
    "eventId": "ali-ok", "eventName": "RebootInstance",
    "eventTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "serviceName": "Ecs",
    "eventSource": "ecs.aliyuncs.com", "resourceName": "i-demo", "resourceType": "instance",
    "userIdentity": {"sessionContext": {"attributes": {"displayName": "sts-user"}}},
    "errorCode": "Denied",
}
normalized = actiontrail.normalize_event(raw)
check("B1 camelCase/STS/失败事件归一化", normalized is not None
      and normalized.external_id == "ali-ok" and normalized.user == "sts-user"
      and normalized.error_code == "Denied" and normalized.event_time.tzinfo is None)
noisy = [dict(raw, eventId="noise", eventName="DescribeInstances"),
         dict(raw, eventId="no-resource", resourceName="", resources=[], referencedResources=[]),
         dict(raw, eventId="bad-time", eventTime="not-a-time")]
check("B2 噪声/无资源/坏时间过滤", all(actiontrail.normalize_event(x) is None for x in noisy))

sdk_module = types.ModuleType("alibabacloud_actiontrail20171204")
class Request:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
sdk_module.models = SimpleNamespace(LookupEventsRequest=Request)
class Body:
    def __init__(self, value): self.value = value
    def to_map(self): return self.value
class Client:
    def __init__(self, pages): self.pages = list(pages); self.requests = []
    def lookup_events(self, req):
        self.requests.append(req)
        return SimpleNamespace(body=Body(self.pages.pop(0)))
client = Client([{"Events": [raw], "NextToken": "p2"},
                 {"Events": [dict(raw, eventId="ali-2")], "NextToken": "still-more"}])
with patch.dict(sys.modules, {"alibabacloud_actiontrail20171204": sdk_module}), \
     patch.object(actiontrail, "_client", lambda *args: client), \
     patch.object(actiontrail.time, "sleep", lambda _: None):
    fetched = actiontrail.fetch_events("a", "s", now - timedelta(hours=1), now, max_pages=2)
check("B3 NextToken 翻页与截断显式返回", len(fetched) == 2 and fetched.truncated
      and len(client.requests) == 2 and client.requests[1].next_token == "p2")
empty_client = Client([{"Events": [], "NextToken": "must-not-follow"}])
with patch.dict(sys.modules, {"alibabacloud_actiontrail20171204": sdk_module}), \
     patch.object(actiontrail, "_client", lambda *args: empty_client):
    empty = actiontrail.fetch_events("a", "s", now - timedelta(hours=1), now, max_pages=3)
check("B4 空页停止且不误报截断", len(empty) == 0 and not empty.truncated
      and len(empty_client.requests) == 1)

print("== C. 精确账号凭证 / 部分失败 / 截断汇总 ==")
def ok_batch(**kwargs):
    return cs.ChangeBatch(events=[event("shared-native")], fetched=3, truncated=True)
def fail_batch(**kwargs):
    raise RuntimeError("fixture source denied")
source_ok = FakeSource("fake-ok", ok_batch)
source_fail = FakeSource("fake-fail", fail_batch)
cs.register_change_source(source_ok)
cs.register_change_source(source_fail)
ok_acc = account("ok-account", source="fake-ok", ak="exact-ok-ak")
fail_acc = account("fail-account", source="fake-fail", ak="exact-fail-ak")
with patch("app.collector.credential.resolve_aliyun_credential",
           side_effect=AssertionError("不得跨账号回退阿里默认凭证")):
    result = cs.sync_changes(db, days=1)
check("C1 两账号独立执行且失败隔离", len(source_ok.calls) == 1 and len(source_fail.calls) == 1
      and result["created"] == 1 and len(result["errors"]) == 1 and not result["ok"], str(result))
check("C2 只使用本账号解密凭证", source_ok.calls[0]["account"].id == ok_acc.id
      and source_ok.calls[0]["ak"] == "exact-ok-ak"
      and source_fail.calls[0]["ak"] == "exact-fail-ak")
check("C3 截断与原始拉取数不被吞", result["fetched"] == 3 and result["truncated"]
      and any(r["truncated"] for r in result["results"]))
check("C4 单账号失败仍提交其他账号事件", db.query(ChangeEvent).filter(
    ChangeEvent.account_id == ok_acc.id).count() == 1)
check("C5 画像直挂在统一入库边界", any(h[0] == "change" and h[1] for h in hooks), str(hooks))
clear_rows()

print("== D. source/account/native ID 幂等与旧行兼容 ==")
a1 = account("identity-a", source="fake-ok")
a2 = account("identity-b", source="fake-ok")
batch = cs.ChangeBatch(events=[event("same-id")], fetched=1)
r1 = cs.save_change_batch(db, source_key="fake-ok", account_id=a1.id, batch=batch)
r2 = cs.save_change_batch(db, source_key="fake-ok", account_id=a2.id, batch=batch)
r3 = cs.save_change_batch(db, source_key="other-source", account_id=a1.id, batch=batch)
r4 = cs.save_change_batch(db, source_key="fake-ok", account_id=a1.id, batch=batch)
rows = db.query(ChangeEvent).filter(ChangeEvent.external_id == "same-id").all()
check("D1 同原生 ID 跨账号/来源均可入库", (r1["created"], r2["created"], r3["created"]) == (1, 1, 1)
      and len(rows) == 3 and len({r.event_id for r in rows}) == 3)
check("D2 同账号同源重跑零新增", r4["created"] == 0 and r4["duplicates"] == 1)
legacy = ChangeEvent(account_id=a1.id, event_id="legacy-raw", source_type="", external_id="",
                     event_time=datetime.now(), service="ecs", event_name="Old", raw="{}")
db.add(legacy)
db.commit()
legacy_result = cs.save_change_batch(db, source_key="actiontrail", account_id=a1.id,
                                    batch=cs.ChangeBatch(events=[event("legacy-raw")]))
check("D3 旧原生 ActionTrail 行首轮不重复", legacy_result["created"] == 0
      and db.query(ChangeEvent).filter(ChangeEvent.external_id == "legacy-raw").count() == 0)
other_legacy = cs.save_change_batch(db, source_key="actiontrail", account_id=a2.id,
                                   batch=cs.ChangeBatch(events=[event("legacy-raw")]))
check("D4 旧原生 ID 不阻塞另一账号", other_legacy["created"] == 1)
explicit_missing = cs.sync_changes(db, account_id=999999)
check("D5 指定不存在账号不静默回退", not explicit_missing["ok"]
      and explicit_missing["errors"][0]["error"] == "目标云账号不存在或已停用")
db.query(CloudAccount).update({CloudAccount.enabled: False})
db.commit()
calls_before_disabled = len(source_ok.calls)
with patch("app.collector.credential.resolve_aliyun_credential",
           side_effect=AssertionError("全部停用不得回退默认凭证")):
    disabled_result = cs.sync_changes(db)
check("D6 有台账但全部停用时不拉取", len(source_ok.calls) == calls_before_disabled
      and not disabled_result["ok"]
      and disabled_result["errors"][0]["error"] == "没有启用中的云账号", str(disabled_result))
clear_rows()

print("== E. 迁移回填 / API 形状、RBAC、审计 ==")
db.add_all([
    ChangeEvent(account_id=1, event_id="github:push:abc", source_type="", external_id="",
                event_time=datetime.now(), service="github"),
    ChangeEvent(account_id=1, event_id="old-ali", source_type="", external_id="",
                event_time=datetime.now(), service="ecs"),
])
db.commit()
migrate_change_sources(engine)
db.expire_all()
check("E1 旧来源迁移区分 webhook/ActionTrail",
      db.query(ChangeEvent).filter(ChangeEvent.event_id == "github:push:abc").one().source_type == "github"
      and db.query(ChangeEvent).filter(ChangeEvent.event_id == "old-ali").one().source_type == "actiontrail"
      and db.query(ChangeEvent).filter(ChangeEvent.event_id == "old-ali").one().external_id == "old-ali")

current = SimpleNamespace(id=7, username="operator", role="operator")
api = FastAPI()
api.include_router(changes_router)
def override_db():
    yield db
api.dependency_overrides[get_db] = override_db
api.dependency_overrides[get_current_user] = lambda: current
def api_request(method, url):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                     base_url="http://change-source-test") as client:
            return await client.request(method, url)
    return asyncio.run(run())
fake_response = {"ok": True, "fetched": 2, "created": 1, "truncated": False,
                 "results": [{"ok": True, "account_id": 1}], "errors": []}
with patch.object(cs, "sync_changes", return_value=fake_response) as mocked:
    current.role = "viewer"
    denied = api_request("POST", "/changes/sync?days=1")
    current.role = "operator"
    allowed = api_request("POST", "/changes/sync?days=1&account_id=123")
check("E2 viewer 禁止手动同步", denied.status_code == 403, denied.text)
check("E3 旧 top-level fetched/created 兼容且支持账号定向", allowed.status_code == 200
      and allowed.json()["fetched"] == 2 and allowed.json()["created"] == 1
      and mocked.call_args.kwargs["account_id"] == 123, allowed.text)
audit_row = db.query(AuditLog).filter(AuditLog.action == "change.sync").order_by(AuditLog.id.desc()).first()
check("E4 同步审计保留且不含密钥", audit_row is not None and audit_row.resource_id == "123"
      and "exact-ok-ak" not in (audit_row.detail or ""))
listed = api_request("GET", "/changes?hours=48")
check("E5 列表增加来源且保留 items/total", listed.status_code == 200
      and "items" in listed.json() and "total" in listed.json()
      and "github" in {x["key"] for x in listed.json()["sources"]}
      and all("source" in x and "source_label" in x for x in listed.json()["items"]))
legacy_blank = db.query(ChangeEvent).filter(ChangeEvent.event_id == "old-ali").one()
legacy_blank.source_type = ""
db.commit()
legacy_listed = api_request("GET", "/changes?hours=48&source=actiontrail")
legacy_unfiltered = api_request("GET", "/changes?hours=48")
check("E7 旧空来源显示兼容由统一适配层提供",
      legacy_listed.status_code == 200 and legacy_listed.json()["total"] == 0
      and any(x["event_name"] == "" and x["source"] == "actiontrail"
              for x in legacy_unfiltered.json()["items"]))
from app.api.v1 import events_webhook
webhook_events = events_webhook._parse_github(
    {"x-github-event": "push"},
    {"after": "webhook-native", "repository": {"full_name": "acme/web"},
     "sender": {"login": "ci-user"}, "ref": "refs/heads/main", "commits": []})
first_push = events_webhook._save(db, webhook_events)
second_push = events_webhook._save(db, webhook_events)
saved_push = db.query(ChangeEvent).filter(ChangeEvent.event_id == "github:push:webhook-native").one()
check("E6 既有 webhook 原生 ID 幂等和来源不受改造影响", first_push == 1 and second_push == 0
      and saved_push.source_type == "github" and saved_push.external_id == "github:push:webhook-native")
clear_rows()

print("== F. 独立调度 / 诊断失败口径 / 消费端静态边界 / extra 回写 ==")
from app.services import diag_tools
with patch.object(cs, "sync_changes", return_value={
        "ok": False, "errors": [{"account": "a", "error": "denied"}], "truncated": False}):
    diagnosis = diag_tools.recent_changes(db, {"hours": 1})
check("F0 同步失败时诊断不宣称排除变更致因", diagnosis["items"] == []
      and not diagnosis["sync"]["ok"] and "不能据此排除变更致因" in diagnosis["note"])
fresh_a = account("fresh-a", source="fake-ok")
stale_b = account("stale-b", source="fake-ok")
db.add(ChangeEvent(account_id=fresh_a.id, event_id="fresh-a-event", source_type="fake-ok",
                   external_id="fresh-a-event", event_time=datetime.now(), created_at=datetime.now()))
db.commit()
refresh_targets = []
def record_refresh(_db, days, account_id):
    refresh_targets.append(account_id)
    return {"ok": True, "fetched": 0, "created": 0, "results": [], "errors": [], "truncated": False}
with patch.object(cs, "sync_changes", side_effect=record_refresh):
    diag_tools.recent_changes(db, {"hours": 48})
check("F0b A 账号新鲜不掩盖 B 账号待同步", refresh_targets == [stale_b.id], str(refresh_targets))
db.query(ChangeEvent).delete()
db.add_all([
    ChangeEvent(account_id=fresh_a.id, event_id="github:new", source_type="github",
                external_id="github:new", event_time=datetime.now(), created_at=datetime.now()),
    ChangeEvent(account_id=stale_b.id, event_id="fresh-b-event", source_type="fake-ok",
                external_id="fresh-b-event", event_time=datetime.now(), created_at=datetime.now()),
])
db.commit()
refresh_targets = []
with patch.object(cs, "sync_changes", side_effect=record_refresh):
    diag_tools.recent_changes(db, {"hours": 48})
check("F0c webhook 新鲜不掩盖同账号拉取源", refresh_targets == [fresh_a.id], str(refresh_targets))
clear_rows()

import app.main as main  # noqa: E402
async def scheduler_check():
    main.settings.DECISION_DIGEST_ENABLED = False
    main.settings.CLOUD_SYNC_INTERVAL_S = 21600
    main._digest_task = None
    main._change_sync_task = None
    main._start_decision_digest()
    main._start_change_sync()
    task = main._change_sync_task
    independent = task is not None and main._digest_task is None
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    return independent
check("F1 digest 关闭仍创建独立变更调度", asyncio.run(scheduler_check()))
root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
consumers = ["backend/app/api/v1/changes.py", "backend/app/main.py",
             "backend/app/services/diag_tools.py", "backend/app/services/bootstrap.py"]
check("F2 消费端无直接 ActionTrail 模块导入", all(
    "app.collector.actiontrail" not in open(os.path.join(root, p), encoding="utf-8").read()
    for p in consumers))
check("F5 消费端不再保留厂商字符串兜底", all(
    "actiontrail" not in open(os.path.join(root, p), encoding="utf-8").read().lower()
    for p in consumers))
cloud_vue = open(os.path.join(root, "frontend/src/views/CloudAccounts.vue"), encoding="utf-8").read()
check("F3 extra 以原对象为底回写且保留未知键",
      "JSON.parse(JSON.stringify(editing.value.extra))" in cloud_vue
      and "if (!isVSphere()) return {}" not in cloud_vue
      and "extra.change_source = form.changeSource" in cloud_vue)
check("F4 未实现腾讯/AWS 适配器", "cloudaudit" not in cs.CHANGE_SOURCES
      and "cloudtrail" not in cs.CHANGE_SOURCES)

db.close()
print(f"\nChangeSource 回归: {len(PASS)} 通过 / {len(FAIL)} 失败")
if FAIL:
    print("失败项:", FAIL)
    sys.exit(1)
