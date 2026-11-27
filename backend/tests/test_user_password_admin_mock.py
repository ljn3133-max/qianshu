#!/usr/bin/env python3
"""用户密码管理隔离 mock 测试：create_user 恒强制首登改密 +
POST /users/{id}/reset-password 一次性密码回显一次。

运行：cd /opt/qianshu/backend && python3 tests/test_user_password_admin_mock.py
隔离性：DATABASE_URL/MASTER_KEY 在 import app.* 之前改写为临时值——不碰真实
qianshu.db、不碰 admin 账号（全部一次性自建用户）、零网络。

验证项：
  A create_user 恒强制：显式给密码 / 自动生成，must_change_password 都是 True
    （copilot change_tools 直调 + REST /users 管理面默认值双路径）
  B impact 文案如实：不再写"生成密码=…强制首登改密"，如实写"首次登录强制修改密码"
  C reset-password：服务端生成一次性密码回显一次；旧密码失效、新密码可登录；
    must_change_password=True；审计 action=user.password.reset 且密码不入审计
  D 强制改密门禁：拿临时密码登录后只能进改密流程（NEED_CHANGE_PASSWORD）
  E 越权 403：operator/viewer 调 reset-password 被拒；不存在用户 404
"""
from __future__ import annotations

import os
import sys
import tempfile
from types import SimpleNamespace

# ---- 隔离环境必须先于 app 导入设置（env 覆盖 .env）----
_TMPDIR = tempfile.mkdtemp(prefix="qianshu-pw-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("pw")
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
from app.core.security import hash_password, verify_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models.audit import AuditLog  # noqa: E402
from app.models.user import User  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


# ---- 建表 + 一次性测试用户（绝不建/碰 admin 账号）----
Base.metadata.create_all(engine)
db = SessionLocal()
for uname, role in (("rootop", "admin"), ("alice", "operator"), ("bob", "viewer")):
    db.add(User(username=uname, password_hash=hash_password("Test-Only-Pw1"),
                role=role, is_active=True, must_change_password=False))
db.commit()
ROOTOP_ID = db.query(User).filter(User.username == "rootop").first().id
db.close()


# ---- TestClient 同步壳（ASGITransport + asyncio；不触发 lifespan/startup）----
def _req(method: str, url: str, **kw):
    async def _go():
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver") as c:
            return await c.request(method, url, **kw)
    return asyncio.run(_go())


def _login(username: str, password: str) -> str:
    r = _req("POST", "/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, f"login {username} failed: {r.status_code} {r.text[:200]}"
    return r.json()["token"]


def _authed(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


root_token = _login("rootop", "Test-Only-Pw1")

# ---- A create_user 恒强制首登改密 ----
from app.services.copilot import change_tools  # noqa: E402
from app.services.copilot.change_tools import _h_create_user  # noqa: E402

# A1 显式给密码：must_change_password 仍 True（旧逻辑 False 的回归守卫）
out = _h_create_user(SessionLocal(), {
    "username": "carol", "password": "Given-Pw-123", "role": "viewer",
}, SimpleNamespace(id=ROOTOP_ID, username="rootop"), "ok")
check("A1 显式密码创建也强制首登改密（结果）",
      out["result"]["must_change_password"] is True and out["result"]["password_source"] == "given",
      str(out))
db = SessionLocal()
carol = db.query(User).filter(User.username == "carol").first()
check("A2 显式密码创建也强制首登改密（落库）", bool(carol.must_change_password))
check("A3 显式密码创建不回显初始密码", "initial_password" not in out["result"])
check("A4 显式密码可登录（密码来源真实生效）",
      verify_password("Given-Pw-123", carol.password_hash))
db.close()

# A5 自动生成密码：must_change True + 一次性回显
out = _h_create_user(SessionLocal(), {
    "username": "dave", "role": "operator",
}, SimpleNamespace(id=ROOTOP_ID, username="rootop"), "ok")
check("A5 自动生成密码创建强制首登改密",
      out["result"]["must_change_password"] is True
      and out["result"]["password_source"] == "generated"
      and out["result"].get("initial_password"))
db = SessionLocal()
dave = db.query(User).filter(User.username == "dave").first()
check("A6 自动生成密码落库强制改密", bool(dave.must_change_password))
check("A7 一次性密码可登录", verify_password(out["result"]["initial_password"], dave.password_hash))
db.close()

# A8 REST /users 管理面默认强制（缺省参数）
r = _req("POST", "/api/users", headers=_authed(root_token),
         json={"username": "erin", "password": "Rest-Pw-123", "role": "viewer"})
check("A8 REST 创建用户 200", r.status_code == 200, r.text[:200])
check("A9 REST 创建默认强制首登改密", r.json().get("must_change_password") is True, r.text[:200])
db = SessionLocal()
check("A10 REST 创建落库强制改密",
      bool(db.query(User).filter(User.username == "erin").first().must_change_password))
db.close()

# A11 管理面代设密码（PUT /users/{id} 带 password）同口径强制改密
db = SessionLocal()
erin_id = db.query(User).filter(User.username == "erin").first().id
db.close()
r = _req("PUT", f"/api/users/{erin_id}", headers=_authed(root_token), json={"password": "Reset-Pw-123"})
check("A11 代设密码 200", r.status_code == 200, r.text[:200])
db = SessionLocal()
check("A12 代设密码后强制首登改密",
      bool(db.query(User).filter(User.username == "erin").first().must_change_password))
db.close()

# ---- B impact 文案如实 ----
impact = change_tools.CHANGE_TOOLS["create_user"]["impact"]
check("B1 impact 不再写'生成密码=…强制首登改密'误导话术",
      "生成密码=一次性初始密码并强制首登改密" not in impact, impact)
check("B2 impact 如实写首次登录强制修改密码", "首次登录强制修改密码" in impact, impact)

# ---- C reset-password API ----
db = SessionLocal()
alice_id = db.query(User).filter(User.username == "alice").first().id
db.close()

r = _req("POST", f"/api/users/{alice_id}/reset-password", headers=_authed(root_token))
check("C1 reset-password 200", r.status_code == 200, r.text[:200])
body = r.json()
temp = body.get("temporary_password") or ""
check("C2 一次性密码回显一次且形态合格",
      bool(temp) and body.get("must_change_password") is True and body.get("username") == "alice",
      str(body))
check("C3 响应不带旧密码/哈希", "password_hash" not in body and "password" not in body, str(body))

db = SessionLocal()
alice = db.query(User).filter(User.username == "alice").first()
check("C4 旧密码立即失效", not verify_password("Test-Only-Pw1", alice.password_hash))
check("C5 新临时密码生效", verify_password(temp, alice.password_hash))
check("C6 重置后强制首登改密", bool(alice.must_change_password))
rows = (db.query(AuditLog).filter(AuditLog.action == "user.password.reset")
        .order_by(AuditLog.id.desc()).all())
check("C7 审计落库 action=user.password.reset（target=alice）",
      rows and rows[0].resource_id == str(alice_id) and "alice" in (rows[0].detail or ""),
      rows[0].detail if rows else "no row")
check("C8 密码明文绝不入审计", all(temp not in (r_.detail or "") and temp != (r_.client_ip or "")
                              for r_ in rows), str([r_.detail for r_ in rows]))
from app.services.audit import humanize  # noqa: E402
h = humanize("user.password.reset", "user", str(alice_id), rows[0].detail)
check("C9 审计中文名已登记", h["action_label"] == "重置用户密码", str(h))
db.close()

r = _req("POST", f"/api/users/{alice_id}/reset-password", headers=_authed(root_token))
check("C10 再次重置生成新一次性密码（旧临时密码作废）",
      r.status_code == 200 and r.json()["temporary_password"] != temp)
temp2 = r.json()["temporary_password"]

# ---- D 强制改密门禁：拿临时密码登录后被门禁拦 ----
r = _req("POST", "/api/auth/login", json={"username": "alice", "password": temp2})
check("D1 临时密码可登录", r.status_code == 200, r.text[:200])
check("D2 登录态标记 must_change_password",
      r.json()["user"].get("must_change_password") is True, r.text[:200])
alice_token = r.json()["token"]
r = _req("GET", "/api/users", headers=_authed(alice_token))
check("D3 未改密被 NEED_CHANGE_PASSWORD 门禁拦",
      r.status_code == 403 and "NEED_CHANGE_PASSWORD" in r.text, f"{r.status_code} {r.text[:120]}")
r = _req("POST", "/api/auth/password", headers=_authed(alice_token),
         json={"old_password": temp2, "new_password": "Changed-Pw-123"})
check("D4 改密接口放行（门禁内可自救）", r.status_code == 200, r.text[:200])
db = SessionLocal()
check("D5 改密后解除强制", not db.query(User).filter(User.username == "alice").first().must_change_password)
db.close()

# ---- E 越权/防呆 ----
alice_token2 = _login("alice", "Changed-Pw-123")
r = _req("POST", f"/api/users/{alice_id}/reset-password", headers=_authed(alice_token2))
check("E1 operator 越权 403", r.status_code == 403, f"{r.status_code}")
bob_token = _login("bob", "Test-Only-Pw1")
r = _req("POST", f"/api/users/{alice_id}/reset-password", headers=_authed(bob_token))
check("E2 viewer 越权 403", r.status_code == 403, f"{r.status_code}")
r = _req("POST", "/api/users/99999/reset-password", headers=_authed(root_token))
check("E3 不存在用户 404", r.status_code == 404, f"{r.status_code}")
r = _req("POST", f"/api/users/{alice_id}/reset-password")
check("E4 无 token 401/403", r.status_code in (401, 403), f"{r.status_code}")

print(f"\nuser_password mock: {len(PASS)} ok / {len(FAIL)} fail")
sys.exit(1 if FAIL else 0)
