#!/usr/bin/env python3
"""滑块拼图验证码隔离 mock 测试：一次性票据 + 融合口径。

运行：cd /opt/qianshu/backend && python3 tests/test_slider_captcha_mock.py
隔离性：DATABASE_URL/MASTER_KEY 先于 import 改写为临时值——不碰真实 qianshu.db、
不碰 admin 账号（一次性自建用户）、零网络。

验证项：
  A 开关：CAPTCHA_ENABLED=false 时 challenge/enabled=false 且登录免验证（既有行为不动）
  B 挑战：seed 派生缺口位（与前端同公式），challenge 一次性、过期拒
  C 校验：错位拒 / 对齐签发票据；票据一次性、过期拒
  D 登录融合：滑块票据与 SVG 图形验证码二选一同校验点；缺票拒；SVG 旧路仍通
"""
import os
import sys
import tempfile
import time
from types import SimpleNamespace

# ---- 隔离环境必须先于 app 导入设置（env 覆盖 .env）----
_TMPDIR = tempfile.mkdtemp(prefix="qianshu-cap-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("cap")
os.environ["MASTER_KEY"] = "cd" * 32
os.environ["CAPTCHA_ENABLED"] = "false"
os.environ["METRIC_SOURCES"] = "[]"
os.environ["LOG_SOURCES"] = "[]"
os.environ["VM_URL"] = "http://127.0.0.1:1"
os.environ["HOLIDAY_SYNC_ENABLED"] = "false"
os.environ["PUBLIC_BASE_URL"] = ""

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models.user import User  # noqa: E402
from app.api.v1 import auth as auth_mod  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


Base.metadata.create_all(engine)
db = SessionLocal()
db.add(User(username="carol", password_hash=hash_password("Test-Only-Pw1"),
            role="operator", is_active=True, must_change_password=False))
db.commit()


# ---- TestClient 同步壳（不触发 lifespan/startup）----
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
)

LOGIN = {"username": "carol", "password": "Test-Only-Pw1"}


def solve(challenge: dict) -> int:
    """按前后端同一公式派生缺口位：min_x + seed % range。"""
    return challenge["min_x"] + challenge["seed"] % challenge["range"]


# ===== A 开关关闭：免验证（既有登录行为不动）=====
print("A CAPTCHA_ENABLED=0 跳过")
settings.CAPTCHA_ENABLED = False
r = client.get("/api/auth/slider/challenge")
check("关闭时 challenge enabled=false", r.status_code == 200 and r.json() == {"enabled": False}, r.text[:120])
r = client.post("/api/auth/login", json=LOGIN)
check("关闭时登录免验证 200", r.status_code == 200 and r.json().get("token"), r.text[:150])

# ===== B 挑战一次性 + 过期拒 =====
print("B 滑块挑战")
settings.CAPTCHA_ENABLED = True
r = client.get("/api/auth/slider/challenge")
ch = r.json()
check("开启时挑战下发", r.status_code == 200 and ch.get("enabled") is True
      and ch.get("challenge_id", "").startswith("s") and ch.get("seed"), r.text[:200])
x_ok = solve(ch)
check("缺口位在有效范围", ch["min_x"] <= x_ok < ch["min_x"] + ch["range"])

r = client.post("/api/auth/slider/verify", json={"challenge_id": ch["challenge_id"], "offset": 0})
check("错位拒 400", r.status_code == 400 and "未对齐" in r.text, r.text[:120])
r = client.post("/api/auth/slider/verify", json={"challenge_id": ch["challenge_id"], "offset": x_ok})
check("challenge 一次性（错位后即焚）", r.status_code == 400 and "过期" in r.text, r.text[:120])

ch2 = client.get("/api/auth/slider/challenge").json()
cid2 = ch2["challenge_id"]
auth_mod._SLIDER[cid2] = (auth_mod._SLIDER[cid2][0], time.time() - 1)   # 摆成已过期
r = client.post("/api/auth/slider/verify", json={"challenge_id": cid2, "offset": solve(ch2)})
check("challenge 过期拒", r.status_code == 400 and "过期" in r.text, r.text[:120])

# ===== C 对齐签发票据：一次性 + 过期拒 =====
print("C 票据一次性/过期")
ch3 = client.get("/api/auth/slider/challenge").json()
r = client.post("/api/auth/slider/verify", json={"challenge_id": ch3["challenge_id"], "offset": solve(ch3)})
d = r.json()
ticket = d.get("ticket", "")
check("对齐签发票据", r.status_code == 200 and d.get("ok") is True and ticket.startswith("st"), r.text[:150])

r = client.post("/api/auth/login", json={**LOGIN, "slider_ticket": ticket})
check("票据登录 200", r.status_code == 200 and r.json().get("token"), r.text[:150])
r = client.post("/api/auth/login", json={**LOGIN, "slider_ticket": ticket})
check("票据一次性（重放拒）", r.status_code == 400 and "验证码" in r.text, r.text[:150])

ch4 = client.get("/api/auth/slider/challenge").json()
r = client.post("/api/auth/slider/verify", json={"challenge_id": ch4["challenge_id"], "offset": solve(ch4)})
ticket2 = r.json()["ticket"]
auth_mod._SLIDER_TICKETS[ticket2] = time.time() - 1   # 摆成已过期
r = client.post("/api/auth/login", json={**LOGIN, "slider_ticket": ticket2})
check("票据过期拒", r.status_code == 400 and "验证码" in r.text, r.text[:150])

# ===== D 登录融合（单开关单校验点）=====
print("D 登录融合")
r = client.post("/api/auth/login", json=LOGIN)
check("启用后缺票登录拒", r.status_code == 400 and "验证码" in r.text, r.text[:150])

cap = client.get("/api/auth/captcha").json()
check("SVG 验证码旧路仍在", cap.get("enabled") is True and cap.get("svg"), str(cap)[:120])
code = auth_mod._CAPTCHA[cap["id"]][0]
r = client.post("/api/auth/login", json={**LOGIN, "captcha_id": cap["id"], "captcha_code": code.lower()})
check("SVG 验证码登录 200（融合二选一）", r.status_code == 200 and r.json().get("token"), r.text[:150])
r = client.post("/api/auth/login", json={**LOGIN, "captcha_id": cap["id"], "captcha_code": code})
check("SVG 票据一次性（重放拒）", r.status_code == 400, r.text[:150])

ch5 = client.get("/api/auth/slider/challenge").json()
r = client.post("/api/auth/slider/verify", json={"challenge_id": ch5["challenge_id"], "offset": solve(ch5)})
r = client.post("/api/auth/login", json={**LOGIN, "slider_ticket": r.json()["ticket"]})
check("滑块票据登录 200（二选一）", r.status_code == 200 and r.json().get("token"), r.text[:150])

settings.CAPTCHA_ENABLED = False
r = client.post("/api/auth/login", json=LOGIN)
check("开关可关（恢复免验证）", r.status_code == 200 and r.json().get("token"), r.text[:150])

print(f"\n滑块验证码 mock 测试: {len(PASS)}/{len(PASS) + len(FAIL)} 通过")
if FAIL:
    print("失败项:", FAIL)
sys.exit(1 if FAIL else 0)
