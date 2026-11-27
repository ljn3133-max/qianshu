#!/usr/bin/env python3
"""集成中心隔离 mock 测试：测试连通去摆设化 + 集成管理状态聚合。

运行：cd /opt/qianshu/backend && python3 tests/test_integrations_mock.py
隔离性：MASTER_KEY/DATABASE_URL/EVENT_WEBHOOK_TOKEN 在 import app.* 之前改写为临时值——
不碰真实 qianshu.db、不调任何网络（METRIC_SOURCES/LOG_SOURCES 清空零探测）、
绝不向任何真实/本地通知渠道发消息（send_to_channel 全程钉死抛错）。

验证项：
  A 事件源判定三态：token 未配=异常；近 7 天有事件=已连通（真通）；
    只有历史事件=未验证（带历史时间）；零事件=未验证（"尚无外部事件到达/无法自证"文案）；
    gitlab/gitea 共用入库通道按 raw object_kind 正确拆分；测试端点零外发零写库。
  B 通知渠道只读自检：零发送（send_to_channel 钉死）；停用=disabled；
    有发送记录=已连通；无记录=未验证（"无法自证真实送达"文案）。
  C 集成管理状态聚合：事件源/数据源/渠道/K8s/云账号全类别行齐全；
    无证据不许标"已连通"（摆设状态防线）；summary 计数正确。
  D 集成管理 CRUD 联动：新增渠道→registry 出现（未验证）→停用→disabled→启用→未验证。
  E 路由注册：/integrations/registry、/integrations/check-channel/{id} 挂进 openapi。
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace

# ---- 隔离环境必须先于 app 导入设置（env 覆盖 .env）----
_TMPDIR = tempfile.mkdtemp(prefix="qianshu-intg-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("intg")
os.environ["MASTER_KEY"] = "cd" * 32          # 64 hex 测试主密钥
os.environ["EVENT_WEBHOOK_TOKEN"] = "test-token"
os.environ["METRIC_SOURCES"] = "[]"           # 零数据源=探测零网络
os.environ["LOG_SOURCES"] = "[]"
os.environ["VM_URL"] = "http://127.0.0.1:1"   # builtin-vm 兜底探测也走死端口（零网络）
os.environ["PUBLIC_BASE_URL"] = ""

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from app.api.v1 import integrations as intg  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.core.security import encrypt_secret  # noqa: E402
from app.models.alert_channel import AlertChannel  # noqa: E402
from app.models.alert_record import AlertRecord  # noqa: E402
from app.models.change_event import ChangeEvent  # noqa: E402
from app.models.cloud_account import CloudAccount  # noqa: E402
from app.models.k8s import K8sCluster  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


# ---- 建表（全部隔离自建的临时库）----
Base.metadata.create_all(engine)
db = SessionLocal()

STUB_USER = SimpleNamespace(id=1, username="tester", role="admin", is_active=True)


def seed_event(service: str, raw: str, days_ago: float, eid: str) -> None:
    db.add(ChangeEvent(
        account_id=1, event_id=eid, event_time=datetime.now() - timedelta(days=days_ago),
        service=service, event_name="Push", event_source=service, user="u",
        resource="r", summary="s", raw=raw,
    ))
    db.commit()


RAW_GITLAB = json.dumps({"object_kind": "push", "project": {"name": "p"}})
RAW_GITEA = json.dumps({"repository": {"name": "r"}, "ref": "refs/heads/main"})

# 事件证据样本：gitlab 近 7 天 1 条 + 历史 1 条；gitea 仅历史 1 条；github 零事件
seed_event("gitlab", RAW_GITLAB, 0.1, "gitlab:push:recent")
seed_event("gitlab", RAW_GITLAB, 30, "gitlab:push:old")
seed_event("gitlab", RAW_GITEA, 30, "gitlab:push:gitea-old")
seed_event("jenkins", "{}", 0.2, "jenkins:b1:1")

# ---- 真外发防线：send_to_channel 钉死（红线：绝不对外发消息）----
import app.services.notify as _notify  # noqa: E402
import app.api.v1.alerts as _alerts_mod  # noqa: E402


def _forbid_send(*a, **k):
    raise AssertionError("红线触发：测试中不得对外发送任何消息")


_notify.send_to_channel = _forbid_send
_alerts_mod.send_to_channel = _forbid_send   # alerts.py 的 from-import 绑定也钉死

# ---- TestClient：get_db/get_current_user 走依赖覆盖（不碰真实库真实账号）----
from app.core.db import get_db  # noqa: E402
from app.core.deps import get_current_user  # noqa: E402
from app.main import app  # noqa: E402

app.dependency_overrides[get_db] = lambda: db
app.dependency_overrides[get_current_user] = lambda: STUB_USER
# starlette TestClient 与 httpx 0.28 不兼容（app= 参数已移除）；ASGITransport 又只有
# 异步接口——套一层同步壳走 asyncio.run（不触发 lifespan/startup：不启调度器、零网络）
import asyncio  # noqa: E402


def _req(method: str, url: str, **kw):
    async def _go():
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver") as c:
            return await c.request(method, url, **kw)
    return asyncio.run(_go())


client = SimpleNamespace(
    get=lambda url, **kw: _req("GET", url, **kw),
    post=lambda url, **kw: _req("POST", url, **kw),
    put=lambda url, **kw: _req("PUT", url, **kw),
    delete=lambda url, **kw: _req("DELETE", url, **kw),
)


# ===== E 路由注册 =====
print("E 路由注册")
paths = {r.path for r in app.routes}
check("registry 路由已注册", "/api/integrations/registry" in paths, str(sorted(paths)[:5]))
check("check-channel 路由已注册", "/api/integrations/check-channel/{channel_id}" in paths)

# ===== A 事件源判定三态 =====
print("A 事件源判定三态")
r = client.post("/api/integrations/test/github")
d = r.json()
check("零事件=未验证", r.status_code == 200 and d["ok"] is None and d["verdict"] == "unverified", str(d))
check("零事件文案：尚无外部事件+无法自证", "尚无外部事件" in d["message"] and "无法自证" in d["message"], d["message"])

r = client.post("/api/integrations/test/gitlab")
d = r.json()
check("近 7 天有事件=已连通", d["ok"] is True and d["verdict"] == "connected", str(d))
check("连通文案带条数+最近时间", "1 条" in d["message"] and d["evidence"]["last_at"] in d["message"], d["message"])

r = client.post("/api/integrations/test/gitea")
d = r.json()
check("gitea 仅历史事件=未验证（object_kind 拆分生效）",
      d["verdict"] == "unverified" and d["evidence"]["count_total"] == 1
      and d["evidence"]["count_recent"] == 0, str(d))
check("历史分支文案：曾收到事件", "曾收到事件" in d["message"], d["message"])

r = client.post("/api/integrations/test/jenkins")
check("jenkins 近期事件=已连通", r.json()["verdict"] == "connected", str(r.json()))

n_events = db.query(ChangeEvent).count()
r = client.post("/api/integrations/test/gitlab")
check("测试端点零写库（不发样本事件）", db.query(ChangeEvent).count() == n_events)

settings.EVENT_WEBHOOK_TOKEN = ""
r = client.post("/api/integrations/test/gitlab")
d = r.json()
check("token 未配=异常（不装通）", d["ok"] is False and d["verdict"] == "error"
      and "token 未配置" in d["message"], str(d))
settings.EVENT_WEBHOOK_TOKEN = "test-token"

r = client.post("/api/integrations/test/prometheus")
check("数据源键拒自测（指去真探测）", r.status_code == 400, str(r.status_code))

# ===== B 通知渠道只读自检 =====
print("B 通知渠道只读自检")
ch = AlertChannel(name="ops-bot", type="wecom_bot",
                  webhook_url_cipher=encrypt_secret("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abcd1234"),
                  enabled=True, verify_ssl=True)
db.add(ch)
db.add(AlertChannel(name="off-bot", type="dingtalk_bot",
                    webhook_url_cipher=encrypt_secret("https://oapi.dingtalk.com/robot/send?access_token=x"),
                    enabled=False, verify_ssl=True))
db.commit()
ch_id, off_id = ch.id, db.query(AlertChannel).filter_by(name="off-bot").one().id

r = client.post(f"/api/integrations/check-channel/{ch_id}")
d = r.json()
check("无发送记录=未验证", r.status_code == 200 and d["ok"] is None and d["verdict"] == "unverified", str(d))
check("无记录文案：无法自证真实送达", "无法自证真实送达" in d["message"], d["message"])

r = client.post(f"/api/integrations/check-channel/{off_id}")
check("停用渠道=disabled", r.json()["verdict"] == "disabled", str(r.json()))

db.add(AlertRecord(rule_id=1, rule_name="r", source="metric", target="t",
                   level="critical", title="t",
                   notified_channels=json.dumps(["ops-bot"], ensure_ascii=False),
                   fired_at=datetime.now() - timedelta(hours=1)))
db.commit()
r = client.post(f"/api/integrations/check-channel/{ch_id}")
d = r.json()
check("有发送记录=已连通", d["ok"] is True and d["verdict"] == "connected", str(d))

r = client.post("/api/integrations/check-channel/99999")
check("不存在渠道=404", r.status_code == 404, str(r.status_code))

# ===== C 集成管理状态聚合 =====
print("C 集成管理状态聚合")
settings.METRIC_SOURCES = json.dumps([
    {"name": "builtin-vm", "type": "vm", "url": ""},
    {"name": "prom-main", "type": "prometheus", "url": "http://127.0.0.1:1/none"},
])
settings.LOG_SOURCES = json.dumps([
    {"name": "loki-main", "type": "loki", "url": "http://127.0.0.1:1/none"},
])
intg._PROBE_CACHE["data"] = None   # 配置变更后强制重探测（对 127.0.0.1:1 快速拒连）
db.add(K8sCluster(name="prod-k8s", api_url="https://10.0.0.1:6443", status="ok",
                  status_note="版本 v1.29.0", synced_at=datetime.now() - timedelta(hours=2)))
db.add(K8sCluster(name="old-k8s", api_url="https://10.0.0.2:6443", status="unknown"))
db.add(CloudAccount(name="ali-prod", vendor="aliyun", enabled=True, status="untested"))
db.add(CloudAccount(name="volc-old", vendor="volc", enabled=False, status="ok",
                    last_test_at=datetime.now() - timedelta(days=10)))
db.commit()

r = client.get("/api/integrations/registry")
d = r.json()
rows = d["rows"]
by_id = {row["id"]: row for row in rows}
check("registry 返回 rows+summary", isinstance(rows, list) and "summary" in d)
check("六类行齐全",
      all(any(row["category"] == cat for row in rows)
          for cat in ("event", "metric", "log", "channel", "k8s", "cloud")),
      str([row["category"] for row in rows]))
check("事件源：有证据=已连通", by_id["event:gitlab"]["status"] == "connected", by_id["event:gitlab"]["detail"])
check("事件源：无证据=未验证", by_id["event:github"]["status"] == "unverified", by_id["event:github"]["detail"])
check("数据源 builtin-vm 在列", "ds:metric_sources:builtin-vm" in by_id)

# 数据源行内测试=真实版本探测：builtin-vm 空地址退 VM_URL（测试指向死端口，如实报不通）
r = client.post("/api/datasources/test", json={"name": "builtin-vm", "type": "vm", "url": ""})
d = r.json()
check("builtin-vm 空地址可测（真实探测）", r.status_code == 200 and d.get("ok") is False
      and "版本" in d.get("detail", ""), str(d))
r = client.post("/api/datasources/test", json={"name": "x", "type": "prometheus", "url": ""})
check("非 vm 空地址仍拒测", r.status_code == 400, str(r.status_code))
check("K8s ok=已连通+最近同步", by_id["k8s:%d" % db.query(K8sCluster).filter_by(name="prod-k8s").one().id]["status"]
      == "connected", str(by_id))
check("K8s unknown=未验证", any(row["category"] == "k8s" and row["name"] == "old-k8s"
                              and row["status"] == "unverified" for row in rows))
check("云账号未测试=未验证（不装通）", any(row["category"] == "cloud" and row["name"] == "ali-prod"
                                     and row["status"] == "unverified" for row in rows))
check("云账号停用=disabled", any(row["category"] == "cloud" and row["name"] == "volc-old"
                              and row["status"] == "disabled" for row in rows))
check("渠道有记录=已连通", by_id["channel:%d" % ch_id]["status"] == "connected", by_id["channel:%d" % ch_id]["detail"])
check("渠道停用=disabled", by_id["channel:%d" % off_id]["status"] == "disabled")
reg = client.get("/api/integrations/registry").json()
check("summary 计数自洽",
      reg["summary"]["total"] == len(reg["rows"])
      and sum(reg["summary"][s] for s in ("connected", "unverified", "error", "disabled")) == len(reg["rows"]),
      str(reg["summary"]))

# ===== D 集成管理 CRUD 联动 =====
print("D 集成管理 CRUD 联动")
r = client.post("/api/alerts/channels", json={
    "name": "new-bot", "type": "webhook",
    "webhook_url": "https://example.invalid/hook", "enabled": True,
})
check("新增渠道（集成管理入口）", r.status_code == 200, str(r.status_code) + str(r.text[:120]))
new_id = r.json()["id"]
r = client.get("/api/integrations/registry")
row = next(x for x in r.json()["rows"] if x["id"] == f"channel:{new_id}")
check("新渠道入 registry 且未验证", row["status"] == "unverified", str(row))

r = client.put(f"/api/alerts/channels/{new_id}", json={"enabled": False})
check("停用渠道", r.status_code == 200 and r.json()["enabled"] is False, str(r.json()))
row = next(x for x in client.get("/api/integrations/registry").json()["rows"]
           if x["id"] == f"channel:{new_id}")
check("停用后 registry=disabled", row["status"] == "disabled", str(row))
r = client.post(f"/api/integrations/check-channel/{new_id}")
check("停用后自检=disabled", r.json()["verdict"] == "disabled", str(r.json()))

client.put(f"/api/alerts/channels/{new_id}", json={"enabled": True})
row = next(x for x in client.get("/api/integrations/registry").json()["rows"]
           if x["id"] == f"channel:{new_id}")
check("启用后回到未验证（无证据不装通）", row["status"] == "unverified", str(row))

# ===== F 数据源配置持久化：TLS 选项不得在保存白名单中丢失 =====
print("F 数据源配置持久化")
from app.api.v1 import settings as settings_api
import app.services.env_store as env_store

original_metric = settings.METRIC_SOURCES
original_log = settings.LOG_SOURCES
original_set_keys = env_store.set_keys
persisted = {}
try:
    env_store.set_keys = lambda values: persisted.update(values) or True
    payload = settings_api.DatasourceConfigIn(
        metric_sources=[{"name": "tls-prom", "type": "prometheus",
                         "url": "https://prom.example.invalid", "verify_ssl": True}],
        log_sources=[{"name": "plain-loki", "type": "loki",
                      "url": "http://loki.example.invalid", "verify_ssl": False}],
    )
    result = settings_api.put_datasource_config(
        payload, SimpleNamespace(), db=db, user=STUB_USER)
    config = settings_api.get_datasource_config(user=STUB_USER)
    check("保存接口成功且只替换测试持久化器", result == {"ok": True, "persisted": True})
    check("指标源 verify_ssl 保存后可读回",
          config["metric_sources"][0].get("verify_ssl") is True, str(config))
    check("日志源 verify_ssl 缺省/False 可持久化",
          config["log_sources"][0].get("verify_ssl") is False, str(config))
    check("写回内容保留 TLS 选项",
          json.loads(persisted["METRIC_SOURCES"])[-1].get("verify_ssl") is True)
finally:
    settings.METRIC_SOURCES = original_metric
    settings.LOG_SOURCES = original_log
    env_store.set_keys = original_set_keys

db.close()
print(f"\n集成中心 mock 测试: {len(PASS)}/{len(PASS) + len(FAIL)} 通过")
if FAIL:
    print("失败项:", FAIL)
    raise SystemExit(1)
