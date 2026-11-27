#!/usr/bin/env python3
"""审计来源 IP 隔离 mock 测试：真实客户端 IP 三场景 + 伪造头防护。

运行：cd /opt/qianshu/backend && python3 tests/test_client_ip_mock.py
隔离性：DATABASE_URL/MASTER_KEY 在 import app.* 之前改写为临时值——不碰真实
qianshu.db、不碰 admin 账号、零网络。

验证项：
  A 直连取 remote：来源非可信代理时无视 XFF/X-Real-IP（伪造头不被骗）
  B 经代理解 XFF：来源可信代理时解析 XFF；链含伪造条目时右起取非可信 IP（不被骗）
  C X-Real-IP 兜底：无 XFF 时退 X-Real-IP；都没有再退 TCP 对端
  D TRUSTED_PROXIES=none：一律不信转发头（含本机）
  E copilot 占位（SimpleNamespace host="copilot"）原样透传，不被转发头污染
  F 端到端：登录审计落库的 client_ip 是解析后的真实 IP（直连/代理两种）
  G 静态守卫：app/ 内不再有 request.client.host 裸取（唯一实现收口在 core/client_ip.py）
"""
from __future__ import annotations

import os
import sys
import tempfile
from types import SimpleNamespace

# ---- 隔离环境必须先于 app 导入设置（env 覆盖 .env）----
_TMPDIR = tempfile.mkdtemp(prefix="qianshu-ip-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("ip")
os.environ["MASTER_KEY"] = "ab" * 32
os.environ["CAPTCHA_ENABLED"] = "false"
os.environ["METRIC_SOURCES"] = "[]"
os.environ["LOG_SOURCES"] = "[]"
os.environ["VM_URL"] = "http://127.0.0.1:1"
os.environ["HOLIDAY_SYNC_ENABLED"] = "false"
os.environ["PUBLIC_BASE_URL"] = ""

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.client_ip import get_client_ip, is_trusted_proxy  # noqa: E402
from app.core.config import settings  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def fake_req(remote: str, headers: dict | None = None):
    return SimpleNamespace(
        client=SimpleNamespace(host=remote),
        headers=headers or {},
    )


# ---- A 直连取 remote（伪造头防护核心场景）----
check("A1 直连无头取 remote",
      get_client_ip(fake_req("203.0.113.9")) == "203.0.113.9")
check("A2 直连伪造 XFF 不被骗",
      get_client_ip(fake_req("203.0.113.9", {"x-forwarded-for": "6.6.6.6"})) == "203.0.113.9")
check("A3 直连伪造 X-Real-IP 不被骗",
      get_client_ip(fake_req("203.0.113.9", {"x-real-ip": "6.6.6.6"})) == "203.0.113.9")

# ---- B 经可信代理解 XFF ----
check("B1 本机代理 XFF 单 IP",
      get_client_ip(fake_req("127.0.0.1", {"x-forwarded-for": "198.51.100.9"})) == "198.51.100.9")
check("B2 私网代理 XFF 单 IP",
      get_client_ip(fake_req("10.1.2.3", {"x-forwarded-for": "198.51.100.9"})) == "198.51.100.9")
check("B3 XFF 链伪造在左不被骗（右起第一个非可信 IP）",
      get_client_ip(fake_req("127.0.0.1",
                            {"x-forwarded-for": "6.6.6.6, 198.51.100.9"})) == "198.51.100.9")
check("B4 XFF 链多级可信代理自右向左跳过",
      get_client_ip(fake_req("10.0.0.1",
                            {"x-forwarded-for": "198.51.100.9, 172.16.0.2, 10.0.0.5"})) == "198.51.100.9")
check("B5 XFF 带端口形态可解析",
      get_client_ip(fake_req("127.0.0.1",
                            {"x-forwarded-for": "198.51.100.9:5555"})) == "198.51.100.9")
check("B6 XFF 全链可信取最左原始来源",
      get_client_ip(fake_req("127.0.0.1",
                            {"x-forwarded-for": "10.0.0.8, 10.0.0.5"})) == "10.0.0.8")

# ---- C X-Real-IP 兜底 ----
check("C1 无 XFF 走 X-Real-IP",
      get_client_ip(fake_req("127.0.0.1", {"x-real-ip": "198.51.100.9"})) == "198.51.100.9")
check("C2 XFF 与 X-Real-IP 并存时 XFF 优先",
      get_client_ip(fake_req("127.0.0.1",
                            {"x-forwarded-for": "198.51.100.9", "x-real-ip": "6.6.6.6"})) == "198.51.100.9")
check("C3 头都缺退 TCP 对端",
      get_client_ip(fake_req("127.0.0.1")) == "127.0.0.1")

# ---- D TRUSTED_PROXIES 配置 ----
_orig = settings.TRUSTED_PROXIES
settings.TRUSTED_PROXIES = "none"
check("D1 none：本机来源也不信 XFF",
      get_client_ip(fake_req("127.0.0.1", {"x-forwarded-for": "6.6.6.6"})) == "127.0.0.1")
settings.TRUSTED_PROXIES = "203.0.113.0/24"
check("D2 自定义网段：命中才信",
      get_client_ip(fake_req("203.0.113.50", {"x-forwarded-for": "198.51.100.9"})) == "198.51.100.9")
check("D3 自定义网段：未命中不信",
      get_client_ip(fake_req("127.0.0.1", {"x-forwarded-for": "6.6.6.6"})) == "127.0.0.1")
settings.TRUSTED_PROXIES = _orig
check("D4 默认本机/私网可信、公网不可信",
      is_trusted_proxy("127.0.0.1") and is_trusted_proxy("10.0.0.1")
      and is_trusted_proxy("192.168.1.1") and is_trusted_proxy("::1")
      and not is_trusted_proxy("203.0.113.9") and not is_trusted_proxy("6.6.6.6"))

# ---- E copilot 占位透传 ----
check("E1 占位 host 原样透传（无 headers 不炸）",
      get_client_ip(SimpleNamespace(client=SimpleNamespace(host="copilot"))) == "copilot")
check("E2 无 client 返回空串",
      get_client_ip(SimpleNamespace(client=None)) == "")

# ---- F 端到端：登录审计落库 client_ip ----
from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models.audit import AuditLog  # noqa: E402

Base.metadata.create_all(engine)


def _login(client_host: str, headers: dict) -> None:
    import asyncio

    import httpx

    async def _go():
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, client=(client_host, 7)),
                base_url="http://testserver") as c:
            return await c.post("/api/auth/login",
                                json={"username": "ghost", "password": "Bad-Password-1"},
                                headers=headers)

    asyncio.run(_go())


def _last_login_ip() -> str:
    db = SessionLocal()
    try:
        row = (db.query(AuditLog).filter(AuditLog.action == "login")
               .order_by(AuditLog.id.desc()).first())
        return row.client_ip if row else ""
    finally:
        db.close()


_login("203.0.113.9", {"x-forwarded-for": "6.6.6.6"})
check("F1 直连审计 IP=TCP 对端（伪造 XFF 不入审计）", _last_login_ip() == "203.0.113.9",
      f"got {_last_login_ip()!r}")
_login("127.0.0.1", {"x-forwarded-for": "6.6.6.6, 198.51.100.9"})
check("F2 经代理审计 IP=XFF 右起真实客户端", _last_login_ip() == "198.51.100.9",
      f"got {_last_login_ip()!r}")
_login("127.0.0.1", {"x-real-ip": "198.51.100.7"})
check("F3 经代理审计 IP=X-Real-IP 兜底", _last_login_ip() == "198.51.100.7",
      f"got {_last_login_ip()!r}")

# ---- G 静态守卫：裸取 client.host 只允许在 core/client_ip.py ----
from pathlib import Path  # noqa: E402

app_root = Path(__file__).resolve().parent.parent / "app"
python_files = sorted(app_root.rglob("*.py"))
check("G0 静态扫描锚定当前源码树且结果非空", app_root.is_dir() and len(python_files) > 0,
      f"path={app_root}, files={len(python_files)}")
leaks = []
for path in python_files:
    if path == app_root / "core" / "client_ip.py":
        continue
    if "client.host" in path.read_text(encoding="utf-8"):
        leaks.append(path.relative_to(app_root).as_posix())
check("G1 全站审计挂载点统一走 helper（无裸取残留）", not leaks, str(leaks))

print(f"\nclient_ip mock: {len(PASS)} ok / {len(FAIL)} fail")
sys.exit(1 if FAIL else 0)
