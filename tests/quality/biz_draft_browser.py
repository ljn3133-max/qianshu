#!/usr/bin/env python3
"""业务归属草稿的真实 Chrome 契约；全部 API 由本进程 mock，不连接真实云或业务库。

运行：先启动 5273 前端，再执行 python3 tests/quality/biz_draft_browser.py。
使用独立 9335 调试端口和临时目录，不杀其他浏览器进程。
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import shutil
import sys
import tempfile
import time
import urllib.request
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chrome_run import chrome_popen  # noqa: E402

SITE = os.environ.get("QIANSHU_SITE", "http://127.0.0.1:5273")
PROFILE = tempfile.mkdtemp(prefix="qianshu-biz-chrome-")
PORT = 9335
PASS, FAIL = [], []
POSTS = []
REQUESTS = []
BUSINESSES = [{
    "id": 1, "name": "manual-biz", "code": "manual", "status": "confirmed",
    "description": "人工登记", "created_at": "2026-11-16T10:00:00", "ident_counts": {},
}]
DRAFT = {
    "id": 2, "name": "orders", "code": "orders", "status": "draft",
    "description": "自动草稿：来自已确认日志链路或云资源业务标签；确认前不影响告警、巡检和成本归属",
    "created_at": "2026-11-16T10:01:00", "ident_counts": {"host": 1, "ecs_name": 1},
}
DRAFT_RESULT = {
    "created": 1, "businesses": ["orders"], "identifiers": 2, "skipped_existing": [],
    "candidates": [{"name": "orders", "code": "orders", "status": "draft",
                    "sources": ["confirmed_log_edges"], "request_count": 150, "asset_count": 0,
                    "identifiers": [{"ident_type": "host", "ident_value": "orders.demo.com"},
                                    {"ident_type": "ecs_name", "ident_value": "orders-ecs"}]}],
    "conflicts": [
        {"kind": "shared_identifier", "ident_type": "app_name", "value": "shared-app",
         "candidates": ["blue", "red"], "reason": "identifier_belongs_to_multiple_candidates"},
        {"kind": "asset_tag", "ident_type": "instance", "value": "i-conflict",
         "candidates": ["A", "B"], "reason": "conflicting_business_tags"},
    ],
    "unresolved_hosts": [
        {"host": "www.demo.com", "reason": "generic_or_invalid_host_label"},
        {"host": "api.demo.com", "reason": "generic_or_invalid_host_label"},
    ],
    "evidence": {"confirmed_log_edges": 9, "tagged_assets": 3},
    "hint": "草稿不参与归属；请核对证据并逐条确认，未知与冲突保持未归属",
}
ORDER_MAP = {
    "business": DRAFT,
    "identifiers": [
        {"id": 11, "ident_type": "host", "ident_value": "orders.demo.com",
         "note": "自动草稿证据：confirmed_log_edges；请核对后确认业务", "created_by": "mock-admin",
         "created_at": "2026-11-16T10:01:00"},
        {"id": 12, "ident_type": "ecs_name", "ident_value": "orders-ecs",
         "note": "自动草稿证据：confirmed_log_edges；请核对后确认业务", "created_by": "mock-admin",
         "created_at": "2026-11-16T10:01:00"},
    ],
    "domains": [{"host": "orders.demo.com", "chains": ["outer"], "req_count": 150,
                 "cnt_5xx": 0, "rate_5xx": 0, "level": "ok", "signals": [],
                 "backends": ["orders-ecs"]}],
    "backends": ["orders-ecs"], "apps": ["orders-app"], "chains": ["outer"],
    "edge_status": {"confirmed": 2}, "total_req_24h": 150,
}


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print(("  PASS " if condition else "  FAIL ") + name + ("" if condition else f"  <-- {detail}"))


def api_response(method, path, body):
    if path == "/auth/me":
        return 200, {"id": 1, "username": "mock-admin", "role": "admin", "must_change_password": False}
    if path == "/bootstrap/status":
        return 200, {"initialized": True, "init_done": True, "has_admin": True,
                     "has_credential": True, "accounts_total": 1, "accounts_enabled": 1}
    if path == "/topology/arch":
        return 200, {"groups": [], "links": [], "stats": {}, "sources": []}
    if path == "/topology/scans":
        return 200, []
    if path == "/links/status":
        return 200, {"running": False, "counts": {"confirmed": 9}, "last_result": None}
    if path in ("/links/hosts", "/links"):
        return 200, {"total": 0, "items": []}
    if path == "/links/businesses/draft" and method == "POST":
        POSTS.append((path, body))
        if not any(x["id"] == DRAFT["id"] for x in BUSINESSES):
            BUSINESSES.append(DRAFT)
        return 200, DRAFT_RESULT
    if path == "/links/businesses" and method == "GET":
        return 200, {"total": len(BUSINESSES), "items": BUSINESSES}
    if path == "/links/businesses/1/map":
        return 200, {"business": BUSINESSES[0], "identifiers": [], "domains": [],
                     "backends": [], "apps": [], "chains": [], "edge_status": {}, "total_req_24h": 0}
    if path == "/links/businesses/2/map":
        return 200, ORDER_MAP
    if path == "/links/businesses/2/confirm" and method == "POST":
        POSTS.append((path, body))
        DRAFT["status"] = "confirmed"
        return 200, {"confirmed": True, "business": DRAFT}
    return 200, {"items": []}


proc = None
ws = None
try:
    import websocket

    proc = chrome_popen(["google-chrome", "--headless=new", "--disable-gpu",
                         f"--remote-debugging-port={PORT}", "--remote-allow-origins=*",
                         f"--user-data-dir={PROFILE}", "about:blank"], PROFILE)
    page = None
    for _ in range(40):
        try:
            targets = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=1).read())
            page = next((x for x in targets if x["type"] == "page"), None)
            if page:
                break
        except Exception:
            pass
        time.sleep(0.25)
    if not page:
        raise RuntimeError("Chrome 调试端口未就绪")
    ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=30)
    mid = 0
    errors = []

    def fire(method, params=None):
        global mid
        mid += 1
        ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))

    def handle_event(msg):
        if msg.get("method") == "Runtime.exceptionThrown":
            detail = msg["params"].get("exceptionDetails", {})
            errors.append((detail.get("exception", {}).get("description") or detail.get("text") or "")[:1500])
        elif msg.get("method") == "Fetch.requestPaused":
            params = msg["params"]
            request = params["request"]
            path = urlparse(request["url"]).path.removeprefix("/api")
            REQUESTS.append((request["method"], path))
            try:
                body = json.loads(request.get("postData") or "{}")
            except ValueError:
                body = {}
            status, payload = api_response(request["method"], path, body)
            fire("Fetch.fulfillRequest", {
                "requestId": params["requestId"], "responseCode": status,
                "responseHeaders": [{"name": "Content-Type", "value": "application/json"}],
                "body": base64.b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode(),
            })

    def send(method, params=None):
        global mid
        mid += 1
        current = mid
        ws.send(json.dumps({"id": current, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == current:
                return msg.get("result", {})
            handle_event(msg)

    def evaljs(expression):
        result = send("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})
        if "exceptionDetails" in result:
            errors.append(str(result["exceptionDetails"])[:1500])
        return result.get("result", {}).get("value")

    def wait_for(expression, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            value = evaljs(expression)
            if value:
                return value
            time.sleep(0.25)
        return False

    def click_text(selector, text, contains=False):
        expression = f"""(() => {{
          const el = [...document.querySelectorAll({json.dumps(selector)})].find(e => {{
            const value = (e.innerText || '').trim();
            return e.getClientRects().length && value{'.includes' if contains else '.startsWith'}({json.dumps(text)});
          }});
          if (!el) return false; el.click(); return true;
        }})()"""
        return evaljs(expression)

    send("Page.enable")
    send("Runtime.enable")
    send("Network.enable")
    send("Fetch.enable", {"patterns": [{"urlPattern": f"{SITE}/api/*", "requestStage": "Request"}]})
    send("Emulation.setDeviceMetricsOverride", {"width": 1600, "height": 1100, "deviceScaleFactor": 1, "mobile": False})
    send("Page.navigate", {"url": f"{SITE}/login"})
    wait_for("document.readyState === 'complete'")
    evaljs("localStorage.setItem('qianshu.token', 'mock-browser-token'); localStorage.setItem('qianshu.account', '0'); true")
    send("Page.navigate", {"url": f"{SITE}/topology?tab=links"})
    check("链路页真实渲染", bool(wait_for(
        "document.body.innerText.includes('业务映射') && document.body.innerText.includes('生成 / 刷新草稿')")), REQUESTS)
    check("进入既有业务映射页签", click_text(".el-tabs__item", "业务映射")
          and bool(wait_for("document.body.innerText.includes('生成业务草稿')")))
    check("确认门说明首屏可见", evaljs(
        "document.body.innerText.includes('自动建议只是草稿')"
        " && document.body.innerText.includes('逐条核对并确认后才参与归属')"))

    check("生成业务草稿", click_text(".el-button--success", "生成业务草稿")
          and bool(wait_for("document.body.innerText.includes('本次建议检查结果')")))
    check("证据量与未决域名可见", evaljs(
        "document.body.innerText.includes('已确认日志边 9 条')"
        " && document.body.innerText.includes('带明确业务标签的资源 3 项')"
        " && document.body.innerText.includes('www.demo.com')"))
    check("共享标识与标签冲突说明可见", evaljs(
        "document.body.innerText.includes('shared-app')"
        " && document.body.innerText.includes('同一标识指向多个候选业务')"
        " && document.body.innerText.includes('同一资源有多个业务标签')"))
    check("新建议显示草稿徽标且不直接生效", evaljs(
        "[...document.querySelectorAll('.el-table__body-wrapper tbody tr')]"
        ".some(e => e.innerText.includes('orders') && e.innerText.includes('草稿'))"))

    check("打开 orders 证据预览", click_text(".el-table__body-wrapper tbody tr", "orders")
          and bool(wait_for("document.body.innerText.includes('证据预览（草稿未生效）')")))
    check("标识与真实流量证据可见", evaljs(
        "document.body.innerText.includes('orders.demo.com')"
        " && document.body.innerText.includes('orders-ecs')"
        " && document.body.innerText.includes('150')"))
    check("打开单业务确认对话框", evaljs("""(() => { const row = [...document.querySelectorAll('.el-table__body-wrapper tbody tr')]
      .find(e => e.innerText.includes('orders')); const btn = [...(row?.querySelectorAll('button') || [])]
      .find(e => e.innerText.trim() === '确认'); if (!btn) return false; btn.click(); return true; })()""")
          and bool(wait_for("document.body.innerText.includes('已核对「orders」的 2 个归属标识')")))
    check("取消确认不写接口且仍为草稿", click_text(".el-message-box__btns .el-button", "取消")
          and not any(path.endswith("/confirm") for path, _ in POSTS)
          and DRAFT["status"] == "draft")
    wait_for("document.querySelectorAll('.el-message-box').length === 0")

    evaljs("""(() => { const row = [...document.querySelectorAll('.el-table__body-wrapper tbody tr')]
      .find(e => e.innerText.includes('orders')); const btn = [...(row?.querySelectorAll('button') || [])]
      .find(e => e.innerText.trim() === '确认'); if (btn) btn.click(); return !!btn; })()""")
    wait_for("[...document.querySelectorAll('.el-message-box__btns .el-button--primary')].some(e => e.getClientRects().length)")
    check("人工确认一个草稿", evaljs(
          "(() => { const b = [...document.querySelectorAll('.el-message-box__btns .el-button--primary')]"
          ".find(e => e.getClientRects().length); if (!b) return false; b.click(); return true; })()"))
    check("确认后解析结果生效", bool(wait_for("document.body.innerText.includes('解析结果（域名 → 后端')")))
    check("确认请求只提交目标业务一次", [path for path, _ in POSTS if path.endswith("/confirm")]
          == ["/links/businesses/2/confirm"], POSTS)
    check("确认后状态与归属文案更新", evaljs("""(() => {
      const table = [...document.querySelectorAll('.el-table')]
        .find(e => e.innerText.includes('短码') && e.innerText.includes('标识数'));
      const rows = [...(table?.querySelectorAll('.el-table__body-wrapper tbody tr') || [])]
        .filter(e => e.innerText.includes('orders'));
      return !document.body.innerText.includes('证据预览（草稿未生效）')
        && rows.length === 1 && rows[0].innerText.includes('已确认')
        && !rows[0].innerText.includes('草稿');
    })()"""))
    screenshot = send("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True})
    if screenshot.get("data"):
        pathlib.Path("/tmp/qianshu-biz-draft.png").write_bytes(base64.b64decode(screenshot["data"]))
    check("浏览器无 JS 异常", not errors, errors[:3])
    if FAIL:
        print("API 请求：", REQUESTS)
        print("页面末尾文本：", (evaljs("document.body.innerText.slice(-3000)") or "")[:3000])
finally:
    if ws is not None:
        ws.close()
    if proc is not None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    shutil.rmtree(PROFILE, ignore_errors=True)

print(f"\n业务归属草稿 Chrome 契约: {len(PASS)}/{len(PASS) + len(FAIL)} 通过")
if FAIL:
    raise SystemExit(1)
