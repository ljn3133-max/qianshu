#!/usr/bin/env python3
"""REST 目录鉴权元数据与误导文案契约。

一次性 PostgreSQL + ASGI 内存请求，不启动调度、不访问真实库或外部服务。
运行：cd /opt/qianshu/backend && python3 tests/test_rest_catalog_contract.py
"""
import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path

_TMPDIR = tempfile.mkdtemp(prefix="qianshu-rest-catalog-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("catalog")
os.environ["MASTER_KEY"] = "cd" * 32
os.environ["CAPTCHA_ENABLED"] = "false"
os.environ["METRIC_SOURCES"] = "[]"
os.environ["LOG_SOURCES"] = "[]"
os.environ["VM_URL"] = "http://127.0.0.1:1"
os.environ["HOLIDAY_SYNC_ENABLED"] = "false"
os.environ["PUBLIC_BASE_URL"] = ""

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import httpx  # noqa: E402

from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.main import _PUBLIC_OPERATIONS, app  # noqa: E402
from app.models.user import User  # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(("  ✓ " if cond else "  ✗ ") + name + (f" [{detail}]" if detail and not cond else ""))


def req(method, path, **kwargs):
    async def go():
        async with httpx.ASGITransport(app=app) as transport:
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.request(method, path, **kwargs)
    return asyncio.run(go())


def main():
    schema = app.openapi()
    methods = {"get", "post", "put", "patch", "delete"}
    operations = {
        (method.upper(), path): op
        for path, entries in schema["paths"].items()
        for method, op in entries.items() if method in methods
    }

    check("所有 HTTP 操作都有鉴权口径", all(op.get("x-auth-mode") for op in operations.values()))
    check("没有未识别的鉴权口径",
          all(op["x-auth-mode"] != "other" for op in operations.values()))
    actual_public = {key for key, op in operations.items() if op["x-auth-mode"] == "public"}
    check("公开操作与白名单逐项一致", actual_public == _PUBLIC_OPERATIONS,
          str(actual_public ^ _PUBLIC_OPERATIONS))

    expected = {
        ("POST", "/api/auth/login"): "public",
        ("GET", "/api/auth/me"): "bearer",
        ("POST", "/api/bootstrap/probe"): "optional",
        ("POST", "/api/mcp"): "bearer",
        ("GET", "/api/synth/screenshots/{fname}"): "bearer",
        ("POST", "/api/agent/report"): "agent_token",
        ("DELETE", "/api/expiry/ignores/{target_key}"): "bearer",
    }
    for key, mode in expected.items():
        check(f"{key[0]} {key[1]} = {mode}", operations.get(key, {}).get("x-auth-mode") == mode)
    for (method, path), op in operations.items():
        if path.startswith("/api/events/webhook"):
            check("Webhook 使用独立凭证", op["x-auth-mode"] == "webhook_token", f"{method} {path}")
        if path.startswith("/api/agent/lifecycle"):
            check("Copilotd 使用独立凭证", op["x-auth-mode"] == "bridge_token", f"{method} {path}")

    auth_ops = [(key, op) for key, op in operations.items() if key[1].startswith("/api/auth/")]
    check("auth 14 个操作都有具体摘要和用途说明",
          len(auth_ops) == 14 and all(op.get("summary") and op.get("description") for _, op in auth_ops))
    check("登录文档推荐 API Token 而非脚本存密码",
          "API Token" in operations[("POST", "/api/auth/login")]["description"]
          and "不要在脚本中保存登录密码" in operations[("POST", "/api/auth/login")]["description"])

    # 一次性 viewer：验证两个资产汇总口拒绝匿名访问，绝不读取或修改真实 admin。
    Base.metadata.create_all(engine)
    db = SessionLocal()
    db.add(User(username="catalog_viewer", password_hash=hash_password("Catalog-Only-Pw1"),
                role="viewer", is_active=True, must_change_password=False))
    db.commit()
    db.close()
    login = req("POST", "/api/auth/login",
                json={"username": "catalog_viewer", "password": "Catalog-Only-Pw1"})
    check("一次性 viewer 登录成功", login.status_code == 200, login.text[:200])
    token = login.json().get("token", "")
    headers = {"Authorization": f"Bearer {token}"}
    for path in ("/api/assets/stats", "/api/assets/cloud-overview"):
        anonymous = req("GET", path)
        authenticated = req("GET", path, headers=headers)
        check(f"{path} 匿名 401", anonymous.status_code == 401, str(anonymous.status_code))
        check(f"{path} viewer 200", authenticated.status_code == 200, authenticated.text[:200])

    for path, marker in (("/api/docs", "swagger-ui"), ("/api/redoc", "redoc")):
        response = req("GET", path)
        check(f"{path} 文档可用", response.status_code == 200 and marker in response.text.lower(),
              str(response.status_code))

    component = (ROOT / "frontend/src/components/ApiCatalog.vue").read_text()
    zh = (ROOT / "frontend/src/i18n/locales/zh-CN/integrations.js").read_text()
    en = (ROOT / "frontend/src/i18n/locales/en-US/integrations.js").read_text()
    check("不再向 URL 拼接登录 token",
          "openGet" not in component and "access_token" not in component
          and "qianshu.token" not in component)
    check("不再宣称无参数即可直接调用", "无参数，可直接调用" not in zh and "callable directly" not in en)
    check("快速开始改用 API Token 而非账号密码登录",
          "export QIANSHU_TOKEN" in component and "/api/auth/me" in component
          and "/api/auth/login" not in component and "curl_body" not in zh and "curl_body" not in en)
    check("目录明确操作数不是公共能力数",
          "操作数不代表独立公共能力数" in zh and "not a count of independent public capabilities" in en)
    check("目录展示逐操作 curl、鉴权与响应",
          all(text in component for text in ("curlFor(it)", "authHelp(it.authMode)", "it.responses")))
    check("分组默认折叠且搜索展开", "collapsed[tag] === true" in component
          and "collapsed[tag] !== false" in component)

    used = set(re.findall(r"t\('((?:integrations\.api|integrations\.skill\.tab_api)[^']*)'", component))
    used.update(re.findall(r"t\('((?:integrations\.api|integrations\.skill\.tab_api)[^']*)'",
                           (ROOT / "frontend/src/views/Integrations.vue").read_text()))
    used.update(f"integrations.api.auth_mode_{mode}" for mode in
                ("public", "bearer", "optional", "webhook_token", "bridge_token", "agent_token", "other"))
    used.update(f"integrations.api.auth_help_{mode}" for mode in
                ("public", "bearer", "optional", "webhook_token", "bridge_token", "agent_token", "other"))
    for lang, text in (("zh-CN", zh), ("en-US", en)):
        missing = sorted(key for key in used if f"'{key}'" not in text)
        check(f"{lang} REST 目录文案键齐全", not missing, ", ".join(missing))

    failed = [name for name, ok in RESULTS if not ok]
    print(f"\nREST 目录契约: {len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    if failed:
        print("失败：" + "; ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
