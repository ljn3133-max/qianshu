#!/usr/bin/env python3
"""个人 API Token 隔离 mock 测试：明文只回显一次 + Bearer 双轨鉴权。

运行：cd /opt/qianshu/backend && python3 tests/test_api_tokens_mock.py
隔离性：DATABASE_URL/MASTER_KEY 在 import app.* 之前改写为临时值——不碰真实
qianshu.db、不碰 admin 账号（全部一次性自建用户，含必须改密场景）、零网络。

验证项：
  A 创建回显：明文 qs_ 前缀只在创建响应出现一次；库存哈希（SHA-256）无明文
  B Bearer 鉴权：Authorization: Bearer <token> 与 JWT 并存，/auth/me 身份正确
  C 权限=所属用户角色：viewer/operator token 打管理端点 403，admin token 通
  D 过期拒 / 停用拒 / 吊销拒 / 他人令牌不可删
  E 审计：create/revoke 都落审计日志
  F 强制改密用户令牌被门禁拦（NEED_CHANGE_PASSWORD）
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace

# ---- 隔离环境必须先于 app 导入设置（env 覆盖 .env）----
_TMPDIR = tempfile.mkdtemp(prefix="qianshu-tok-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("tok")
os.environ["MASTER_KEY"] = "ab" * 32          # 64 hex 测试主密钥
os.environ["CAPTCHA_ENABLED"] = "false"
os.environ["METRIC_SOURCES"] = "[]"
os.environ["LOG_SOURCES"] = "[]"
os.environ["VM_URL"] = "http://127.0.0.1:1"
os.environ["HOLIDAY_SYNC_ENABLED"] = "false"
os.environ["PUBLIC_BASE_URL"] = ""

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.core.security import hash_api_token, hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models.audit import AuditLog  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.user_api_token import UserApiToken  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


# ---- 建表 + 一次性测试用户（绝不建/碰 admin 账号）----
Base.metadata.create_all(engine)
db = SessionLocal()
for uname, role, must in (("alice", "operator", False), ("bob", "viewer", False),
                          ("rootop", "admin", False), ("eve", "operator", True)):
    db.add(User(username=uname, password_hash=hash_password("Test-Only-Pw1"),
                role=role, is_active=True, must_change_password=must))
db.commit()


# ---- TestClient 同步壳（ASGITransport + asyncio；不触发 lifespan/startup）----
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
    delete=lambda url, **kw: _req("DELETE", url, **kw),
)


def login_jwt(username: str) -> str:
    r = client.post("/api/auth/login", json={"username": username, "password": "Test-Only-Pw1"})
    assert r.status_code == 200, f"login {username} 失败: {r.status_code} {r.text[:200]}"
    return r.json()["token"]


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ===== A 创建回显一次 + 库内只存哈希 =====
print("A 创建回显与存储")
jwt_alice = login_jwt("alice")
r = client.post("/api/users/me/tokens", headers=bearer(jwt_alice),
                json={"name": "采集脚本", "expires_days": 30})
d = r.json()
plain = d.get("token", "")
check("创建 200 且明文回显", r.status_code == 200 and plain.startswith("qs_") and len(plain) > 30, str(d)[:200])
check("回显带前缀/有效期", d.get("token_prefix") == plain[:11] and bool(d.get("expires_at")), str(d)[:200])
row = db.query(UserApiToken).filter(UserApiToken.name == "采集脚本").first()
check("库存哈希无明文", row is not None and row.token_hash == hash_api_token(plain)
      and row.token_hash != plain and plain not in (row.token_prefix,), str(row))
check("哈希长度 64（SHA-256 hex）", row is not None and len(row.token_hash) == 64)
tok_id = row.id

r = client.get("/api/users/me/tokens", headers=bearer(jwt_alice))
items = r.json()
one = next(x for x in items if x["id"] == tok_id)
check("列表只显前缀不显明文/哈希", r.status_code == 200 and one["token_prefix"] == plain[:11]
      and "token" not in one and "token_hash" not in one, str(one)[:200])

# ===== B Bearer 鉴权（与 JWT 并存）=====
print("B Bearer 鉴权")
r = client.get("/api/auth/me", headers=bearer(plain))
check("Bearer 令牌取身份", r.status_code == 200 and r.json()["username"] == "alice", r.text[:200])
r = client.get("/api/auth/me", headers=bearer(jwt_alice))
check("JWT 仍可用（并存）", r.status_code == 200 and r.json()["username"] == "alice")
r = client.get("/api/auth/me", headers=bearer("qs_deadbeefnotarealtoken"))
check("伪造令牌 401", r.status_code == 401, r.text[:100])
db.refresh(row)
check("last_used_at 刷新", row.last_used_at is not None)

# ===== C 权限=所属用户角色 =====
print("C 权限随用户角色")
r = client.get("/api/auth/permissions", headers=bearer(plain))
codes = list((r.json().get("catalog") or {}).keys())
perm_code = codes[0] if codes else "k8s.write"
jwt_bob = login_jwt("bob")
r_bob = client.post("/api/users/me/tokens", headers=bearer(jwt_bob), json={"name": "bob只读"})
plain_bob = r_bob.json()["token"]
jwt_root = login_jwt("rootop")
r_root = client.post("/api/users/me/tokens", headers=bearer(jwt_root), json={"name": "admin脚本"})
plain_root = r_root.json()["token"]

r = client.post("/api/auth/permissions/manage", headers=bearer(plain_bob),
                json={"user_id": 1, "code": perm_code, "effect": "allow"})
check("viewer token 打管理端点 403", r.status_code == 403, f"{r.status_code} {r.text[:120]}")
r = client.post("/api/auth/permissions/manage", headers=bearer(plain),
                json={"user_id": 1, "code": perm_code, "effect": "allow"})
check("operator token 打管理端点 403", r.status_code == 403, f"{r.status_code} {r.text[:120]}")
r = client.post("/api/auth/permissions/manage", headers=bearer(plain_root),
                json={"user_id": 2, "code": perm_code, "effect": "allow"})
check("admin token 打管理端点 200", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
r = client.get("/api/auth/me", headers=bearer(plain_bob))
check("token 身份=所属用户（viewer）", r.status_code == 200 and r.json()["role"] == "viewer", r.text[:120])

# ===== D 过期 / 停用 / 吊销 / 越权删 =====
print("D 生命周期拒绝")
r = client.post("/api/users/me/tokens", headers=bearer(jwt_alice), json={"name": "过期样本"})
t_exp = r.json()["token"]
db.query(UserApiToken).filter(UserApiToken.token_hash == hash_api_token(t_exp)).update(
    {"expires_at": datetime.now() - timedelta(days=1)})
db.commit()
r = client.get("/api/auth/me", headers=bearer(t_exp))
check("过期令牌 401", r.status_code == 401, r.text[:100])

r = client.post("/api/users/me/tokens", headers=bearer(jwt_alice), json={"name": "停用样本"})
t_off = r.json()["token"]
db.query(UserApiToken).filter(UserApiToken.token_hash == hash_api_token(t_off)).update({"enabled": False})
db.commit()
r = client.get("/api/auth/me", headers=bearer(t_off))
check("停用令牌 401", r.status_code == 401, r.text[:100])

r = client.post("/api/users/me/tokens", headers=bearer(jwt_alice), json={"name": "吊销样本"})
t_revoke = r.json()["token"]
rid = next(x["id"] for x in client.get("/api/users/me/tokens", headers=bearer(jwt_alice)).json()
           if x["name"] == "吊销样本")
r = client.delete(f"/api/users/me/tokens/{rid}", headers=bearer(jwt_alice))
check("吊销 200", r.status_code == 200, r.text[:100])
r = client.get("/api/auth/me", headers=bearer(t_revoke))
check("吊销后 401", r.status_code == 401, r.text[:100])

alice_ids = [x["id"] for x in client.get("/api/users/me/tokens", headers=bearer(jwt_alice)).json()]
bob_row = db.query(UserApiToken).filter(UserApiToken.name == "bob只读").first()
r = client.delete(f"/api/users/me/tokens/{bob_row.id}", headers=bearer(jwt_alice))
check("删他人令牌 404", r.status_code == 404, f"{r.status_code} {r.text[:100]}")
check("吊销后列表不含该行", rid not in alice_ids)

# ===== E 审计 =====
print("E 审计留痕")
acts = [a.action for a in db.query(AuditLog).filter(
    AuditLog.action.in_(["user_api_token.create", "user_api_token.revoke"])).all()]
check("create 审计", acts.count("user_api_token.create") >= 4, str(acts))
check("revoke 审计", acts.count("user_api_token.revoke") >= 1, str(acts))

# ===== F 强制改密用户被门禁拦 =====
print("F 强制改密门禁")
jwt_eve = login_jwt("eve")
r = client.post("/api/users/me/tokens", headers=bearer(jwt_eve), json={"name": "eve令牌"})
check("must_change_password 建令牌 403", r.status_code == 403
      and (r.json().get("detail") or {}).get("code") == "NEED_CHANGE_PASSWORD", r.text[:150])

print(f"\nAPI Token mock 测试: {len(PASS)}/{len(PASS) + len(FAIL)} 通过")
if FAIL:
    print("失败项:", FAIL)
sys.exit(1 if FAIL else 0)
