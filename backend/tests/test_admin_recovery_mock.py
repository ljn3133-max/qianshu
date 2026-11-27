#!/usr/bin/env python3
"""管理员密码找回兜底隔离 mock 测试：CLI 重置 + 登录页提示可见性。

运行：cd /opt/qianshu/backend && python3 tests/test_admin_recovery_mock.py
隔离性：DATABASE_URL/MASTER_KEY 在 import app.* 之前改写为临时值——不碰真实
qianshu.db、不碰 admin 账号（全部一次性自建用户）、零网络。CLI 子进程同指临时库。

验证项：
  A CLI reset（--password）：旧密码失效、新密码可登录、强制首登改密门禁（403
    NEED_CHANGE_PASSWORD）→ 改密后正常；审计 user.password.reset（source=cli）且密码不入审计
  B CLI reset（自动生成）：一次性密码只打印一次，可登录
  C CLI 边界：用户不存在 exit 1；弱密码 --password 拒绝（exit 2）且原密码不变
  D 登录页找回提示：本机来源连错 <3 次不显示；≥3 次显示（含命令与用户名）；
    外部公网来源永不显示（含伪造 XFF 场景）
  E 内部 API 保留：POST /users/{id}/reset-password 仍在且行为不变（审计不变）
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from types import SimpleNamespace

# ---- 隔离环境必须先于 app 导入设置（env 覆盖 .env）----
_TMPDIR = tempfile.mkdtemp(prefix="qianshu-rec-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("rec")
os.environ["MASTER_KEY"] = "ab" * 32
os.environ["CAPTCHA_ENABLED"] = "false"
os.environ["METRIC_SOURCES"] = "[]"
os.environ["LOG_SOURCES"] = "[]"
os.environ["VM_URL"] = "http://127.0.0.1:1"
os.environ["HOLIDAY_SYNC_ENABLED"] = "false"
os.environ["PUBLIC_BASE_URL"] = ""

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

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
for uname, role in (("recop", "admin"), ("recuser", "operator"), ("recuser2", "viewer"),
                    ("recuser3", "viewer")):
    db.add(User(username=uname, password_hash=hash_password("Test-Only-Pw1"),
                role=role, is_active=True, must_change_password=False))
db.commit()
db.close()


def _req(method: str, url: str, client=("127.0.0.1", 7), **kw):
    async def _go():
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, client=client),
                base_url="http://testserver") as c:
            return await c.request(method, url, **kw)
    return asyncio.run(_go())


def _login(username: str, password: str, client=("127.0.0.1", 7), headers=None):
    return _req("POST", "/api/auth/login", client=client, headers=headers or {},
                json={"username": username, "password": password})


def _run_cli(*args: str):
    env = dict(os.environ)
    env["DATABASE_URL"] = os.environ["DATABASE_URL"]   # 子进程同指临时库
    return subprocess.run(
        [sys.executable, os.path.join(BACKEND, "scripts", "qianshu-passwd.py"), *args],
        capture_output=True, text=True, env=env, timeout=60)


# ================= A CLI reset（指定密码）=================
out = _run_cli("reset", "recuser", "--password", "Cli-Reset-Pw9!")
check("A1 CLI 指定密码重置成功", out.returncode == 0, out.stderr[:200])

r = _login("recuser", "Test-Only-Pw1")
check("A2 旧密码已失效", r.status_code == 401, f"got {r.status_code}")
r = _login("recuser", "Cli-Reset-Pw9!")
check("A3 新密码可登录", r.status_code == 200, r.text[:200])
tok = r.json().get("token", "") if r.status_code == 200 else ""
info = r.json().get("user", {}) if r.status_code == 200 else {}
check("A4 强制首登改密标志已置位", info.get("must_change_password") is True)

r = _req("GET", "/api/users", headers={"Authorization": f"Bearer {tok}"})
detail = r.json().get("detail", {}) if r.status_code == 403 else {}
check("A5 未改密被门禁拦截（NEED_CHANGE_PASSWORD）",
      r.status_code == 403 and detail.get("code") == "NEED_CHANGE_PASSWORD",
      f"got {r.status_code} {r.text[:120]}")

r = _req("POST", "/api/auth/password", headers={"Authorization": f"Bearer {tok}"},
         json={"old_password": "Cli-Reset-Pw9!", "new_password": "Final-Login-Pw7!"})
check("A6 改密接口放行", r.status_code == 200, r.text[:200])
r = _login("recuser", "Final-Login-Pw7!")
info = r.json().get("user", {}) if r.status_code == 200 else {}
check("A7 改密后正常登录且标志清除",
      r.status_code == 200 and info.get("must_change_password") is False, r.text[:120])

db = SessionLocal()
rows = db.query(AuditLog).filter(AuditLog.action == "user.password.reset",
                                 AuditLog.username == "cli").all()
db.close()
detail_ok = rows and rows[0].detail and "cli" in str(rows[0].detail) and "source" in str(rows[0].detail)
pwd_not_in_audit = rows and "Cli-Reset-Pw9!" not in str(rows[0].detail)
check("A8 审计留痕 user.password.reset（source=cli）且密码不入审计", bool(detail_ok and pwd_not_in_audit),
      str(rows[0].detail)[:120] if rows else "no audit row")

# ================= B CLI reset（自动生成一次性密码）=================
out = _run_cli("reset", "recuser2")
check("B1 自动生成密码重置成功", out.returncode == 0, out.stderr[:200])
gen_pwd = ""
for ln in out.stdout.splitlines():
    ln = ln.strip()
    if ln.startswith("Qs-"):
        gen_pwd = ln
check("B2 一次性密码打印一次", bool(gen_pwd) and out.stdout.count(gen_pwd) == 1, out.stdout[:300])
r = _login("recuser2", gen_pwd)
check("B3 生成密码可登录", r.status_code == 200, r.text[:200])

# ================= C CLI 边界 =================
out = _run_cli("reset", "no-such-user")
check("C1 不存在用户 exit 1", out.returncode == 1, out.stderr[:120])
out = _run_cli("reset", "recuser2", "--password", "12345678")
check("C2 弱密码被拒绝 exit 2", out.returncode == 2, out.stderr[:120])
r = _login("recuser2", "12345678")
check("C3 弱密码未生效（原密码仍可用）", r.status_code == 401, f"got {r.status_code}")
r = _login("recuser2", gen_pwd)
check("C4 原密码未受影响", r.status_code == 200, r.text[:120])

# ================= D 登录页找回提示（独立账号 recuser3，避免前序失败串扰）=================
def _hint(username="recuser3", client=("127.0.0.1", 7), headers=None):
    r = _req("GET", "/api/auth/recovery-hint", client=client, headers=headers or {},
             params={"username": username})
    return r.json().get("hint", "") if r.status_code == 200 else f"<http {r.status_code}>"

# 连错 2 次（本机）：不显示
_login("recuser3", "wrong-pw-x1")
_login("recuser3", "wrong-pw-x2")
h = _hint()
check("D1 本机连错 2 次不显示提示", h == "", repr(h))
# 第 3 次错：本机显示
_login("recuser3", "wrong-pw-x3")
h = _hint()
check("D2 本机连错 ≥3 次显示提示", "qianshu-passwd.py" in h and "recuser3" in h, repr(h))
check("D3 提示含部署文档指引", "管理员找回" in h, repr(h))
# 外部公网来源：即使已 ≥3 次也不显示
h_ext = _hint(client=("203.0.113.9", 7))
check("D4 外部来源永不显示", h_ext == "", repr(h_ext))
# 外部 + 伪造 XFF 冒充本机：不被骗
h_spoof = _hint(client=("203.0.113.9", 7), headers={"x-forwarded-for": "127.0.0.1"})
check("D5 外部伪造 XFF 也拿不到提示", h_spoof == "", repr(h_spoof))
# 本机 + 伪造公网 XFF：解析后视为外部来源，不显示（宁紧勿松）
h_local_spoof = _hint(client=("127.0.0.1", 7), headers={"x-forwarded-for": "203.0.113.9"})
check("D6 本机伪造公网 XFF 同样不显示（宁紧勿松）", h_local_spoof == "", repr(h_local_spoof))
# 其他账号未达门槛：不显示（recuser2 仅 C3 失败 1 次）
h_other = _hint(username="recuser2")
check("D7 其他账号不受牵连", h_other == "", repr(h_other))

# ================= E 内部 API 保留 =================
paths = {getattr(r, "path", "") for r in app.routes}
check("E1 reset-password API 仍在（内部能力）", "/api/users/{user_id}/reset-password" in paths,
      str([p for p in paths if "reset" in p]))
tok_admin = _login("recop", "Test-Only-Pw1").json()["token"]
db = SessionLocal()
rid = db.query(User).filter(User.username == "recuser").first().id
db.close()
r = _req("POST", f"/api/users/{rid}/reset-password", headers={"Authorization": f"Bearer {tok_admin}"})
j = r.json() if r.status_code == 200 else {}
check("E2 API 行为不变（一次性密码+强制改密+审计）",
      r.status_code == 200 and j.get("must_change_password") is True
      and bool(j.get("temporary_password")), r.text[:150])
r2 = _login("recuser", j.get("temporary_password", ""))
check("E3 API 临时密码可登录", r2.status_code == 200, r2.text[:150])
db = SessionLocal()
n = db.query(AuditLog).filter(AuditLog.action == "user.password.reset",
                              AuditLog.username == "recop").count()
db.close()
check("E4 API 审计不变（操作者口径）", n >= 1)

print(f"\n管理员找回 mock: {len(PASS)}/{len(PASS) + len(FAIL)} 通过")
if FAIL:
    print("失败项:", FAIL)
    raise SystemExit(1)
