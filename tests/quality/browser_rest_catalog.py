#!/usr/bin/env python3
"""REST 目录真实 Chrome 验收：一次性测试用户由调用方提供，绝不默认使用 admin。

QIANSHU_TEST_USERNAME / PASSWORD 必填；只读浏览页面，另创建并吊销本人一次性 API Token。
运行：QIANSHU_TEST_USERNAME=<临时用户> PASSWORD=<临时口令> python3 tests/quality/browser_rest_catalog.py
"""
import base64
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chrome_run import chrome_popen  # noqa: E402

API = os.environ.get("QIANSHU_API", "http://127.0.0.1:8810")
SITE = os.environ.get("QIANSHU_SITE", "http://127.0.0.1:5273")
USERNAME = os.environ.get("QIANSHU_TEST_USERNAME", "")
PASSWORD = os.environ.get("PASSWORD", "")
if not USERNAME or not PASSWORD or USERNAME == "admin":
    print("必须使用非 admin 一次性测试用户（QIANSHU_TEST_USERNAME/PASSWORD）")
    sys.exit(2)

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(("  ✓ " if cond else "  ✗ ") + name + (f" [{detail}]" if detail and not cond else ""))


def request(method, path, token="", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            body = response.read().decode()
            return response.status, json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        try:
            body = json.loads(body)
        except Exception:
            pass
        return exc.code, body


status, login = request("POST", "/api/auth/login", payload={"username": USERNAME, "password": PASSWORD})
jwt = login.get("token", "") if status == 200 and isinstance(login, dict) else ""
if not jwt:
    print("一次性用户登录失败，停止浏览器验收")
    sys.exit(2)

api_token = ""
token_id = None
profile = tempfile.mkdtemp(prefix="chrome-rest-catalog-")
port = 9347
proc = None
ws = None
try:
    status, created = request("POST", "/api/users/me/tokens", jwt,
                              {"name": "REST目录验收一次性令牌", "expires_days": 1})
    api_token = created.get("token", "") if status == 200 and isinstance(created, dict) else ""
    token_id = created.get("id") if isinstance(created, dict) else None
    check("创建本人一次性 API Token", status == 200 and api_token.startswith("qs_") and bool(token_id))
    status, identity = request("GET", "/api/auth/me", api_token)
    check("qs_ Token 实际 Bearer 调用成功", status == 200 and identity.get("username") == USERNAME)
    for path in ("/api/assets/stats", "/api/assets/cloud-overview"):
        anon_status, _ = request("GET", path)
        auth_status, _ = request("GET", path, api_token)
        check(f"{path} 匿名拒绝且 Token 可查", anon_status == 401 and auth_status == 200,
              f"anonymous={anon_status}, bearer={auth_status}")

    proc = chrome_popen([
        "google-chrome", "--headless=new", "--disable-gpu", "--no-first-run",
        "--window-size=1440,1100", f"--remote-debugging-port={port}",
        "--remote-allow-origins=*", f"--user-data-dir={profile}", "about:blank",
    ], profile)
    for _ in range(40):
        try:
            targets = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1).read())
            if any(t["type"] == "page" for t in targets):
                break
        except Exception:
            time.sleep(0.25)
    else:
        raise RuntimeError("Chrome 调试端口未就绪")

    import websocket
    page = next(t for t in targets if t["type"] == "page")
    ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=45)
    mid = [0]
    errors = []

    def send(method, params=None):
        mid[0] += 1
        ws.send(json.dumps({"id": mid[0], "method": method, "params": params or {}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == mid[0]:
                return msg.get("result", {})
            if msg.get("method") == "Runtime.exceptionThrown":
                detail = msg["params"].get("exceptionDetails", {})
                errors.append((detail.get("exception", {}).get("description") or detail.get("text") or "")[:300])

    def evaljs(expression):
        result = send("Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": True,
        })
        return result.get("result", {}).get("value")

    def wait_for(expression, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if evaljs(expression):
                return True
            time.sleep(0.3)
        return False

    def search(value):
        return evaljs(f"""(() => {{
          const el = document.querySelector('.api-search input');
          if (!el) return false;
          Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, {json.dumps(value)});
          el.dispatchEvent(new Event('input', {{bubbles: true}}));
          return true;
        }})()""")

    send("Page.enable")
    send("Runtime.enable")
    send("Page.navigate", {"url": f"{SITE}/login"})
    wait_for("document.readyState === 'complete'")
    evaljs(f"localStorage.setItem('qianshu.token', {json.dumps(jwt)}); true")
    send("Page.navigate", {"url": f"{SITE}/integrations?tab=skills"})
    check("集成中心加载", wait_for("document.querySelectorAll('.el-radio-button').length >= 4"))
    evaljs("""(() => {
      const el = [...document.querySelectorAll('.el-radio-button')]
        .find(e => (e.innerText || '').includes('内部 REST API'));
      if (el) el.click();
      return !!el;
    })()""")
    check("REST 目录加载", wait_for("document.querySelector('.api-hero') && !document.querySelector('.api-loading')"))
    hero = evaljs("document.querySelector('.api-hero')?.innerText || ''") or ""
    check("计数口径为 HTTP 操作和路径", "个 HTTP 操作" in hero and "条路径" in hero)
    check("明确内部目录而非公共能力数", "操作数不代表独立公共能力数" in hero)
    check("快速开始使用 Token 做只读身份校验",
          "QIANSHU_TOKEN" in hero and "/api/auth/me" in hero and "/api/auth/login" not in hero)
    check("Swagger/ReDoc/OpenAPI 链接可用",
          evaljs("['docs','redoc','openapi.json'].every(x => document.querySelector(`.api-doc-links a[href='/api/${x}']`))"))
    check("分组默认折叠", (evaljs("document.querySelectorAll('.api-group').length") or 0) > 20
          and evaljs("document.querySelectorAll('.api-item').length") == 0)
    check("不存在 URL 携带 access_token 的链接",
          evaljs("document.querySelectorAll('a[href*=\"access_token\"]').length") == 0)

    check("搜索 auth/me", search("auth/me") and wait_for(
        "[...document.querySelectorAll('.api-path')].some(x => x.textContent === '/api/auth/me')"))
    evaljs("[...document.querySelectorAll('.api-item')].find(x => x.querySelector('.api-path')?.textContent === '/api/auth/me')?.querySelector('.api-row')?.click(); true")
    check("auth/me 详情展开", wait_for("!!document.querySelector('.api-detail')"))
    detail = evaljs("document.querySelector('.api-detail')?.innerText || ''") or ""
    row_text = evaljs("document.querySelector('.api-row')?.innerText || ''") or ""
    check("逐接口标明 Bearer 登录", "Bearer 登录" in row_text
          and "Authorization: Bearer" in detail)
    check("curl 使用环境变量且不携带 URL token",
          "curl -X GET" in detail and "$QIANSU_TOKEN" in detail and "access_token" not in detail)
    check("展示成功响应模型", "UserInfo" in detail and "成功响应" in detail)

    out = Path(__file__).resolve().parents[2] / "docs" / "tmp"
    out.mkdir(parents=True, exist_ok=True)
    shot = send("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
    if shot.get("data"):
        (out / "rest-catalog-auth-me.png").write_bytes(base64.b64decode(shot["data"]))

    for keyword, label in (
        ("bootstrap/probe", "登录可选"),
        ("agent/report", "Agent 凭证"),
        ("events/webhook", "Webhook 凭证"),
    ):
        search(keyword)
        check(f"{keyword} 搜索到操作", wait_for("document.querySelectorAll('.api-item').length >= 1"))
        row = evaljs("document.querySelector('.api-row')?.innerText || ''") or ""
        check(f"{keyword} 标明{label}", label in row, row[:200])

    search("auth/login")
    wait_for("document.querySelectorAll('.api-item').length === 1")
    evaljs("document.querySelector('.api-row')?.click(); true")
    wait_for("!!document.querySelector('.api-detail')")
    login_detail = evaljs("document.querySelector('.api-detail')?.innerText || ''") or ""
    row_text = evaljs("document.querySelector('.api-row')?.innerText || ''") or ""
    check("登录标明公开且给出参数样例", "无需登录" in row_text
          and "<username>" in login_detail and "<password>" in login_detail)
    check("非 GET 操作有写影响提醒", "可能创建、修改或删除数据" in login_detail)

    body = evaljs("document.body.innerText || ''") or ""
    check("无旧版直接调用误导", "无参数，可直接调用" not in body and "URL 附 access_token" not in body)
    check("无裸 i18n key / 异常占位", "integrations.api." not in body
          and not any(x in body for x in ("NaN", "Invalid Date", "undefined")))
    check("浏览器 JS 异常为零", not errors, "; ".join(errors))
finally:
    if ws is not None:
        try:
            ws.close()
        except Exception:
            pass
    if proc is not None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
            proc.wait(timeout=5)
    shutil.rmtree(profile, ignore_errors=True)
    if token_id is not None:
        status, _ = request("DELETE", f"/api/users/me/tokens/{token_id}", jwt)
        check("一次性 API Token 已吊销", status == 200, str(status))
    if api_token:
        status, _ = request("GET", "/api/auth/me", api_token)
        check("吊销后 Token 不可再用", status == 401, str(status))

failed = [name for name, ok in RESULTS if not ok]
print(f"\nREST 目录真实 Chrome / API Token 验收: {len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
if failed:
    print("失败：" + "; ".join(failed))
sys.exit(1 if failed else 0)
