#!/usr/bin/env python3
"""保留策略系统参数隔离 mock 测试：保存 + 热生效 + 清存量。

运行：cd /opt/qianshu/backend && python3 tests/test_retention_params_mock.py
隔离性：DATABASE_URL/MASTER_KEY 在 import app.* 之前改写为临时值；env_store.ENV_PATH
指向临时 .env 文件（不写真实 backend/.env）、不碰 admin 账号、零网络、不重启任何服务。

验证项：
  A 保存：PUT /settings/retention 持久化 .env + settings 单例 + 回显；审计 settings.update
  B 立即清存量：llm/audit/k8s 超期行被清、msg_days=0（永久）一行不动
  C 热生效（跨 worker）：settings 单例置旧值后清理循环仍按 .env 新值跑（resolver 动态读）
  D 0=永久（K8s）：k8s_days=0 后 cleanup_logs 不删任何行
  E 权限：operator 保存 403；GET 全员可读
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, datetime, timedelta

# ---- 隔离环境必须先于 app 导入设置（env 覆盖 .env）----
_TMPDIR = tempfile.mkdtemp(prefix="qianshu-ret-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("ret")
os.environ["MASTER_KEY"] = "ab" * 32
os.environ["CAPTCHA_ENABLED"] = "false"
os.environ["METRIC_SOURCES"] = "[]"
os.environ["LOG_SOURCES"] = "[]"
os.environ["VM_URL"] = "http://127.0.0.1:1"
os.environ["HOLIDAY_SYNC_ENABLED"] = "false"
os.environ["PUBLIC_BASE_URL"] = ""

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio  # noqa: E402

import httpx  # noqa: E402

from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models.audit import AuditLog  # noqa: E402
from app.models.in_app_message import InAppMessage  # noqa: E402
from app.models.k8s import K8sPodLog  # noqa: E402
from app.models.llm_call_log import LlmCallLog  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services import env_store  # noqa: E402

# env_store 指向临时 .env（真实 backend/.env 不许写）
env_store.ENV_PATH = os.path.join(_TMPDIR, "envfile")
open(env_store.ENV_PATH, "w").write("")
env_store._CACHE["mtime"], env_store._CACHE["data"] = None, {}

from app.services import retention  # noqa: E402
from app.services.db_maint import prune_old_data  # noqa: E402
from app.services.k8s_monitor import cleanup_logs, log_retention_days  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def _req(method: str, url: str, token: str = "", **kw):
    async def _go():
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 7)),
                base_url="http://testserver") as c:
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            return await c.request(method, url, headers=headers, **kw)
    return asyncio.run(_go())


# ---- 建表 + 一次性测试用户（绝不建/碰 admin 账号）----
Base.metadata.create_all(engine)
db = SessionLocal()
db.add(User(username="retadmin", password_hash=hash_password("Test-Only-Pw1"),
            role="admin", is_active=True, must_change_password=False))
db.add(User(username="retoper", password_hash=hash_password("Test-Only-Pw1"),
            role="operator", is_active=True, must_change_password=False))
now = datetime.now()
today = date.today()
db.add_all([
    LlmCallLog(setting_id=0, model="m", call_date=today - timedelta(days=40)),   # llm=30 后应删
    LlmCallLog(setting_id=0, model="m", call_date=today - timedelta(days=5)),    # 保留
    AuditLog(action="login", username="x", created_at=now - timedelta(days=200)),  # audit=100 后应删
    AuditLog(action="login", username="x", created_at=now),                        # 保留
    InAppMessage(user_id=0, title="old", created_at=now - timedelta(days=200)),  # msg=0 永久：保留
    InAppMessage(user_id=0, title="new", created_at=now),
    K8sPodLog(cluster_id=0, namespace="ns", pod="p", ts=now - timedelta(days=10)),  # k8s=5 后应删
    K8sPodLog(cluster_id=0, namespace="ns", pod="p", ts=now),
])
db.commit()
db.close()

tok_admin = _req("POST", "/api/auth/login", json={"username": "retadmin", "password": "Test-Only-Pw1"}).json()["token"]
tok_oper = _req("POST", "/api/auth/login", json={"username": "retoper", "password": "Test-Only-Pw1"}).json()["token"]

BODY = {"vm_days": 90, "vl_days": 20, "k8s_days": 5, "llm_days": 30,
        "audit_days": 100, "msg_days": 0, "event_days": 180}

# ================= A 保存 =================
r = _req("PUT", "/api/settings/retention", tok_admin, json=BODY)
j = r.json() if r.status_code == 200 else {}
check("A1 保存成功且值回显", r.status_code == 200 and j.get("values", {}).get("llm_days") == 30, r.text[:200])
check("A2 .env 持久化成功", j.get("persisted") is True)
env_txt = open(env_store.ENV_PATH).read()
check("A3 .env 含 7 个保留键", all(f"{k}={v}" in env_txt for k, v in
      [("VM_RETENTION_DAYS", 90), ("VL_RETENTION_DAYS", 20), ("K8S_LOG_RETENTION_DAYS", 5),
       ("RETENTION_LLM_DAYS", 30), ("RETENTION_AUDIT_DAYS", 100), ("RETENTION_MSG_DAYS", 0),
       ("RETENTION_EVENT_DAYS", 180)]), env_txt[:300])
check("A4 settings 单例已热更", retention.llm_days() == 30 and log_retention_days() == 5)

db = SessionLocal()
row = (db.query(AuditLog).filter(AuditLog.action == "settings.update").order_by(AuditLog.id.desc()).first())
db.close()
check("A5 审计 settings.update 留痕", row is not None and "retention" in str(row.detail), str(row)[:120])

# ================= B 立即清存量 =================
cleaned = j.get("cleaned", {})
check("B1 llm 超期行已清", cleaned.get("llm_call_logs") == 1, str(cleaned))
check("B2 audit 超期行已清", cleaned.get("audit_logs") == 1, str(cleaned))
check("B3 k8s 超期行已清", cleaned.get("k8s_pod_logs") == 1, str(cleaned))
check("B4 msg=永久未清任何行", "in_app_messages" not in cleaned, str(cleaned))
db = SessionLocal()
check("B5 存量核对 llm", db.query(LlmCallLog).count() == 1)
check("B6 存量核对 msg 全保留", db.query(InAppMessage).count() == 2)
check("B7 存量核对 k8s", db.query(K8sPodLog).count() == 1)
db.close()

# ================= C 热生效（跨 worker：单例置旧值，循环仍按 .env 新值）=================
from app.core.config import settings  # noqa: E402

settings.RETENTION_LLM_DAYS = 999        # 模拟别的 worker 里的陈旧单例
settings.K8S_LOG_RETENTION_DAYS = 999
db = SessionLocal()
db.add(LlmCallLog(setting_id=0, model="m", call_date=today - timedelta(days=40)))
db.commit()
deleted = prune_old_data(db)
db.commit()
n_llm = db.query(LlmCallLog).count()
db.close()
check("C1 清理循环按 .env 新值跑（不读单例旧值）", deleted.get("llm_call_logs") == 1 and n_llm == 1, str(deleted))
check("C2 k8s 保留天数动态解析", log_retention_days() == 5, str(log_retention_days()))
check("C3 动态解析接口回显", retention.all_values()["llm_days"] == 30
      and retention.all_values()["vm_days"] == 90)

# ================= D 0=永久（K8s）=================
body2 = dict(BODY, k8s_days=0)
r = _req("PUT", "/api/settings/retention", tok_admin, json=body2)
check("D1 k8s_days=0 保存成功", r.status_code == 200, r.text[:150])
db = SessionLocal()
db.add(K8sPodLog(cluster_id=0, namespace="ns", pod="p", ts=now - timedelta(days=100)))
db.commit()
removed = cleanup_logs(db)
kept = db.query(K8sPodLog).count()
db.close()
check("D2 0=永久不删任何行", removed == 0 and kept == 2, f"removed={removed} kept={kept}")
check("D3 log_retention_days 返回 0", log_retention_days() == 0)

# ================= E 权限与读 =================
r = _req("PUT", "/api/settings/retention", tok_oper, json=BODY)
check("E1 operator 保存被拒 403", r.status_code == 403, f"got {r.status_code}")
r = _req("GET", "/api/settings/retention", tok_oper)
j = r.json() if r.status_code == 200 else {}
check("E2 GET 全员可读且值正确", r.status_code == 200 and j.get("k8s_days") == 0
      and j.get("audit_days") == 100, r.text[:200])

print(f"\n保留参数 mock: {len(PASS)}/{len(PASS) + len(FAIL)} 通过")
if FAIL:
    print("失败项:", FAIL)
    raise SystemExit(1)
