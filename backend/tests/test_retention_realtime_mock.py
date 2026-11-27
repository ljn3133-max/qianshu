#!/usr/bin/env python3
"""存储保留实时生效隔离 mock 测试：查询口径 + 渐进清理 + 上限语义。

运行：cd /opt/qianshu/backend && python3 tests/test_retention_realtime_mock.py
隔离性：DATABASE_URL/MASTER_KEY 在 import app.* 之前改写为临时值；env_store.ENV_PATH
指向临时 .env；VM/VL 用本地 stub HTTP 服务（127.0.0.1 随机端口，真走 collector/vm.py
的 httpx 调用路径），不碰真实 VM/VL、不碰 admin 账号、不重启任何服务。

验证项：
  A 查询口径实时：设置改 -> vm.query_range 的 start 窗口立即变（0=不夹）
  B 清理按新值删：死序列被删 / 活序列不碰；保存即清；调小保留后新死序列被删
  C 上限语义：>cap 的保存被 400 拒；effective=min(配置, 上限)；caps 回显
  D K8s 日志读路径按保留窗口过滤（0=不过滤）
  E VM 不可达不炸保存（清理失败只记录，配置照常生效）
  F 渐进：分片限量 + 游标推进（多轮补完积压，幂等）
  G VL 删除 API 能力探活：未配置/未启用/启用后提交任务/限频
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# ---- stub VM/VL（必须先起，import app 前把 VM_URL 指过来）----
STATE = {
    "series": [],            # [{"labels": {...}, "t0": float, "t1": float}]
    "query_calls": [],       # [(start, end)]
    "series_calls": [],
    "delete_calls": [],      # [[matcher, ...], ...]
    "vl_enabled": False,
    "vl_tasks": [],
    "vl_run_calls": [],
}


def _parse_matcher(m: str) -> frozenset:
    pairs = re.findall(r'(\w+)="((?:[^"\\]|\\.)*)"', m)
    return frozenset((k, v.replace('\\"', '"').replace("\\\\", "\\")) for k, v in pairs)


class StubHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静默
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _params(self):
        return parse_qs(urlparse(self.path).query)

    def do_GET(self):
        path = urlparse(self.path).path
        p = self._params()
        if path == "/api/v1/query_range":
            STATE["query_calls"].append((float(p["start"][0]), float(p["end"][0])))
            self._json({"status": "success", "data": {"result": []}})
        elif path == "/api/v1/series":
            start, end = float(p["start"][0]), float(p["end"][0])
            STATE["series_calls"].append((start, end))
            data = [s["labels"] for s in STATE["series"] if s["t0"] <= end and s["t1"] >= start]
            self._json({"status": "success", "data": data})
        elif path == "/delete/active_tasks":
            if not STATE["vl_enabled"]:
                self._json({"error": "page not found"}, 404)
            else:
                self._json(STATE["vl_tasks"])
        else:
            self._json({"error": "unknown"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        p = self._params()
        if path == "/api/v1/admin/tsdb/delete_series":
            matchers = p.get("match[]", [])
            STATE["delete_calls"].append(matchers)
            # 模拟整序列删除
            want = {_parse_matcher(m) for m in matchers}
            STATE["series"] = [s for s in STATE["series"]
                               if frozenset(s["labels"].items()) not in want]
            self.send_response(204)
            self.end_headers()
        elif path == "/delete/run_task":
            if not STATE["vl_enabled"]:
                self._json({"error": "page not found"}, 404)
                return
            STATE["vl_run_calls"].append(p.get("filter", [""])[0])
            self._json({"task_id": f"t{len(STATE['vl_run_calls'])}"})
        else:
            self._json({"error": "unknown"}, 404)


_STUB = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
threading.Thread(target=_STUB.serve_forever, daemon=True).start()
_STUB_URL = f"http://127.0.0.1:{_STUB.server_address[1]}"

# ---- 隔离环境必须先于 app 导入设置（env 覆盖 .env）----
_TMPDIR = tempfile.mkdtemp(prefix="qianshu-ret2-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("ret2")
os.environ["MASTER_KEY"] = "cd" * 32
os.environ["CAPTCHA_ENABLED"] = "false"
os.environ["METRIC_SOURCES"] = "[]"
os.environ["LOG_SOURCES"] = "[]"
os.environ["VM_URL"] = _STUB_URL
os.environ["HOLIDAY_SYNC_ENABLED"] = "false"
os.environ["PUBLIC_BASE_URL"] = ""

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio  # noqa: E402

import httpx  # noqa: E402

from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models.k8s import K8sPodLog  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services import env_store  # noqa: E402

env_store.ENV_PATH = os.path.join(_TMPDIR, "envfile")
open(env_store.ENV_PATH, "w").write("")
env_store._CACHE["mtime"], env_store._CACHE["data"] = None, {}

from app.collector import vm as vm_client  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.services import engine_retention, retention  # noqa: E402

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


def _put(body: dict, token: str):
    return _req("PUT", "/api/settings/retention", token, json=body)


BASE = {"vm_days": 30, "vl_days": 30, "k8s_days": 5, "llm_days": 30,
        "audit_days": 100, "msg_days": 0, "event_days": 180}

# ---- 建表 + 一次性测试用户（绝不建/碰 admin）----
Base.metadata.create_all(engine)
db = SessionLocal()
db.add(User(username="hotadmin", password_hash=hash_password("Test-Only-Pw2"),
            role="admin", is_active=True, must_change_password=False))
db.commit()
db.close()
TOK = _req("POST", "/api/auth/login", json={"username": "hotadmin", "password": "Test-Only-Pw2"}).json()["token"]

now = time.time()
D = 86400


def _series(name: str, inst: str, t0_days_ago: float, t1_days_ago: float) -> dict:
    return {"labels": {"__name__": name, "instance": inst},
            "t0": now - t0_days_ago * D, "t1": now - t1_days_ago * D}


# ================= A 查询口径实时 =================
r = _put(dict(BASE, vm_days=30), TOK)
check("A0 基线保存成功", r.status_code == 200, r.text[:200])
STATE["query_calls"].clear()
vm_client.query_range("up", now - 60 * D, now, 60)
s, e = STATE["query_calls"][-1]
check("A1 vm_days=30 时查询窗被夹到 30 天", s >= now - 30 * D - 5, f"start={s} floor={now - 30 * D}")

_put(dict(BASE, vm_days=7), TOK)   # 改设置
STATE["query_calls"].clear()
vm_client.query_range("up", now - 60 * D, now, 60)
s, _ = STATE["query_calls"][-1]
check("A2 设置改小后查询窗立即变（7 天）", s >= now - 7 * D - 5, f"start={s}")

_put(dict(BASE, vm_days=0), TOK)   # 0=永久
STATE["query_calls"].clear()
vm_client.query_range("up", now - 60 * D, now, 60)
s, _ = STATE["query_calls"][-1]
check("A3 0=永久不夹窗", abs(s - (now - 60 * D)) < 5, f"start={s}")

_put(dict(BASE, vm_days=30), TOK)
STATE["query_calls"].clear()
res = vm_client.query_range("up", now - 3600, now, 60)
check("A4 窗内查询不受影响且可返回", res == [] and abs(STATE["query_calls"][-1][0] - (now - 3600)) < 5,
      str(STATE["query_calls"][-1:]))
_put(dict(BASE, vm_days=1), TOK)
res = vm_client.query_range("up", now - 3 * D, now - 2 * D, 60)
check("A5 夹出空窗短路返回 []（不发请求）", res == [], f"res={res}")
_put(dict(BASE, vm_days=30), TOK)

# ================= B 清理按新值删（保存即清 + 死/活序列语义） =================
STATE["series"] = [
    _series("m_dead", "a", 45, 40),    # 30 天保留下已全超期 -> 应删
    _series("m_alive", "b", 45, 1),    # 跨保留线 -> 不许删
    _series("m_dead2", "c", 20, 15),   # 30 天下未超期；7 天下才超期
]
STATE["delete_calls"].clear()
env_store.set_keys({"ENGINE_CLEAN_VM_CURSOR": "0", "ENGINE_CLEAN_VM_TOP": "0"})
env_store._CACHE["mtime"] = None   # 强制重读
r = _put(dict(BASE, vm_days=30), TOK)
check("B1 保存 200 且返回 engine 清理结果", r.status_code == 200 and "engine" in r.json(), r.text[:200])
all_m = [m for call in STATE["delete_calls"] for m in call]
check("B2 死序列（m_dead）被删", any('m_dead"' in m and 'm_dead2"' not in m for m in all_m), str(all_m))
check("B3 活序列（m_alive）不碰", not any("m_alive" in m for m in all_m), str(all_m))
check("B4 未超期序列（m_dead2）不碰", not any("m_dead2" in m for m in all_m), str(all_m))
names_left = {s["labels"]["__name__"] for s in STATE["series"]}
check("B5 stub 侧核对：仅 m_dead 被物理删除", names_left == {"m_alive", "m_dead2"}, str(names_left))

STATE["delete_calls"].clear()
r = engine_retention.clean_all()
check("B6 稳态幂等：再跑一轮不再删（游标已推进）",
      sum(len(c) for c in STATE["delete_calls"]) == 0 and r["vm"].get("deleted_series", 0) == 0, str(r))

STATE["delete_calls"].clear()
r = _put(dict(BASE, vm_days=7), TOK)   # 调小保留：m_dead2 变超期
all_m = [m for call in STATE["delete_calls"] for m in call]
check("B7 调小保留后新死序列（m_dead2）保存即删", any("m_dead2" in m for m in all_m), str(all_m))
check("B8 活序列依旧不碰", not any("m_alive" in m for m in all_m), str(all_m))
check("B9 保存响应含 m_dead2 清理数", r.json().get("engine", {}).get("vm", {}).get("deleted_series") == 1,
      str(r.json().get("engine")))

# ================= C 上限语义 =================
settings.VM_RETENTION_MAX_DAYS = 100
r = _put(dict(BASE, vm_days=200), TOK)
check("C1 超上限保存被 400 拒（调大上限需重建容器）",
      r.status_code == 400 and "重建" in r.json().get("detail", ""), f"{r.status_code} {r.text[:150]}")
r = _put(dict(BASE, vm_days=100), TOK)
check("C2 =上限保存成功", r.status_code == 200, r.text[:150])
j = _req("GET", "/api/settings/retention", TOK).json()
check("C3 caps 回显引擎上限", j.get("caps", {}).get("vm") == 100, str(j.get("caps")))
env_store.set_keys({"VM_RETENTION_DAYS": "999"})
env_store._CACHE["mtime"] = None
check("C4 effective=min(配置, 上限)", retention.effective_days("vm") == 100,
      str(retention.effective_days("vm")))
env_store.set_keys({"VM_RETENTION_DAYS": "30"})
env_store._CACHE["mtime"] = None
settings.VM_RETENTION_MAX_DAYS = 365

# ================= D K8s 日志读路径过滤 =================
_put(dict(BASE, k8s_days=5), TOK)   # 先保存（顺带清存量），再插数据隔离"读口径"与"物理清"
now_dt = datetime.now()
db = SessionLocal()
db.add_all([
    K8sPodLog(cluster_id=0, namespace="ns", pod="p", ts=now_dt - timedelta(days=10)),
    K8sPodLog(cluster_id=0, namespace="ns", pod="p", ts=now_dt),
])
db.commit()
n_rows = db.query(K8sPodLog).count()
db.close()
j = _req("GET", "/api/k8s/logs", TOK).json()
ts_list = [i["ts"] for i in j.get("items", [])]
check("D1 保留窗口外的日志读不到（行仍在库里）",
      n_rows == 2 and len(ts_list) == 1 and ts_list[0].startswith(now_dt.strftime("%Y-%m-%d")),
      f"rows={n_rows} got={ts_list}")
_put(dict(BASE, k8s_days=0), TOK)   # 0=永久：物理清也不删（cleanup_logs days<=0 跳过）
j = _req("GET", "/api/k8s/logs", TOK).json()
check("D2 0=永久读路径不夹窗", len(j.get("items", [])) == 2, str(len(j.get("items", []))))
_put(dict(BASE, k8s_days=5), TOK)

# ================= E VM 不可达不炸保存 =================
real_url = settings.VM_URL
settings.VM_URL = "http://127.0.0.1:1"   # 不可达端口
r = _put(dict(BASE, vm_days=30), TOK)
eng = r.json().get("engine", {}) if r.status_code == 200 else {}
check("E1 VM 不可达保存仍 200", r.status_code == 200, r.text[:150])
check("E2 引擎清理失败只记录不抛", "error" in eng.get("vm", {}) or eng.get("vm", {}).get("skipped"),
      str(eng))
check("E3 配置仍热生效（查询口径照常）", retention.effective_days("vm") == 30)
settings.VM_URL = real_url

# ================= F 渐进（分片限量 + 游标推进） =================
STATE["series"] = [_series("m_old1", "x", 210, 205), _series("m_old2", "y", 120, 115),
                   _series("m_old3", "z", 35, 32)]
env_store.set_keys({"ENGINE_CLEAN_VM_CURSOR": "0", "ENGINE_CLEAN_VM_TOP": "0"})
env_store._CACHE["mtime"] = None
STATE["delete_calls"].clear()
r1 = engine_retention.clean_vm_expired(chunk_days=30, max_chunks=2)
deleted_first = r1.get("deleted_series", 0)
check("F1 分片限量：一轮只处理 2 片且积压未完", r1.get("windows") == 2 and r1.get("backlog_left") is True,
      str(r1))
total_passes = 1
while r1.get("backlog_left") and total_passes < 30:
    r1 = engine_retention.clean_vm_expired(chunk_days=30, max_chunks=2)
    total_passes += 1
names_left = {s["labels"]["__name__"] for s in STATE["series"]}
check("F2 多轮补完积压：全部死序列删净", names_left == set(), str(names_left))
check("F3 首轮未全删（渐进而非一次大删）", deleted_first < 3, f"deleted_first={deleted_first}")
r1 = engine_retention.clean_vm_expired(chunk_days=30, max_chunks=2)
check("F4 游标到位后空跑幂等", r1.get("deleted_series", 0) == 0 and not r1.get("backlog_left"), str(r1))

# ================= G VL 删除 API 能力探活 =================
g = engine_retention.clean_vl_expired()
check("G1 未配置 VL 跳过（如实回报）", g.get("skipped") == "vl_not_configured", str(g))
settings.VICTORIALOGS_URL = _STUB_URL
STATE["vl_enabled"] = False
g = engine_retention.clean_vl_expired()
check("G2 未启用 -delete.enable（探活 404）如实回报", g.get("skipped") == "vl_delete_api_disabled",
      str(g))
STATE["vl_enabled"] = True
env_store.set_keys({"ENGINE_CLEAN_VL_CURSOR": "0", "ENGINE_CLEAN_VL_LAST_TS": "0"})
env_store._CACHE["mtime"] = None
g = engine_retention.clean_vl_expired()
check("G3 启用后提交删除任务（LogsQL 时间过滤）",
      g.get("task_id", "").startswith("t") and "_time:[" in STATE["vl_run_calls"][-1], str(g))
env_store.set_keys({"ENGINE_CLEAN_VL_CURSOR": "0"})   # 窗口仍在，但刚提交过
env_store._CACHE["mtime"] = None
g = engine_retention.clean_vl_expired()
check("G4 限频 24h：窗口存在也不再提交", g.get("skipped") == "throttled", str(g))
settings.VICTORIALOGS_URL = ""

print(f"\n实时生效 mock: {len(PASS)}/{len(PASS) + len(FAIL)} 通过")
if FAIL:
    print("失败项:", FAIL)
    raise SystemExit(1)
