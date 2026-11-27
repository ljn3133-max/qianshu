#!/usr/bin/env python3
"""Helm 升级集群身份的真实 Chrome 契约；API 全部 mock，零真实集群写入。

运行：先启动 5273 前端，再执行 python3 tests/quality/k8s_helm_upgrade_browser.py。
使用独立 9336 调试端口和临时目录，不杀其他浏览器进程。
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.request
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chrome_run import chrome_popen  # noqa: E402

SITE = os.environ.get("QIANSHU_SITE", "http://127.0.0.1:5273")
PROFILE = tempfile.mkdtemp(prefix="qianshu-k8s-helm-chrome-")
PORT = 9336
PASS, FAIL = [], []
POSTS = []
REQUESTS = []
HELM_GETS = []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print(("  PASS " if condition else "  FAIL ") + name + ("" if condition else f"  <-- {detail}"))


def api_response(method, path, query, body):
    if path == "/auth/me":
        return 200, {"id": 1, "username": "mock-admin", "role": "admin", "must_change_password": False}
    if path == "/bootstrap/status":
        return 200, {"initialized": True, "init_done": True, "has_admin": True,
                     "has_credential": True, "accounts_total": 1, "accounts_enabled": 1}
    if path == "/k8s/overview":
        return 200, {"clusters": [{"id": 1, "name": "cluster-a"}, {"id": 2, "name": "cluster-b"}],
                     "resources": {}, "pending_failures": 0, "log_rows": 0, "retention_days": 7}
    if path == "/k8mgmt/helm":
        cluster_id = (query.get("cluster_id") or ["0"])[0]
        HELM_GETS.append(cluster_id)
        items = [{"name": "demo-release", "namespace": "default", "chart": "repo/demo-1.0.0",
                  "app_version": "1.0.0", "status": "deployed", "updated": "2026-11-17 10:00:00"}] \
            if cluster_id == "2" else []
        return 200, {"items": items, "total": len(items), "note": ""}
    if path == "/k8mgmt/helm/demo-release/upgrade" and method == "POST":
        POSTS.append({"path": path, "query": query, "body": body})
        return 200, {"ok": True, "detail": "mock upgraded"}
    if path in ("/k8s/namespaces", "/k8s/pods"):
        return 200, []
    if path == "/k8s/log-retention":
        return 200, {"days": 7}
    if path in ("/k8mgmt/overview", "/k8mgmt/stats", "/k8mgmt/charts"):
        return 200, None
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
            parsed = urlparse(request["url"])
            path = parsed.path.removeprefix("/api")
            query = parse_qs(parsed.query)
            REQUESTS.append((request["method"], path, query))
            try:
                body = json.loads(request.get("postData") or "{}")
            except ValueError:
                body = {}
            status, payload = api_response(request["method"], path, query, body)
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

    def click_text(selector, text):
        expression = f"""(() => {{
          const el = [...document.querySelectorAll({json.dumps(selector)})].find(e =>
            e.getClientRects().length && (e.innerText || '').trim().startsWith({json.dumps(text)}));
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
    send("Page.navigate", {"url": f"{SITE}/k8s?tab=stats"})
    check("K8s Helm 清单真实渲染", bool(wait_for(
        "document.body.innerText.includes('Helm Releases') && !!document.querySelector('.ov-bar .el-select')")), REQUESTS)

    check("打开集群选择器", evaljs("(() => { const el = document.querySelector('.ov-bar .el-select__wrapper'); if (!el) return false; el.click(); return true; })()")
          and bool(wait_for("[...document.querySelectorAll('.el-select-dropdown__item')].some(e => e.getClientRects().length && e.innerText.includes('cluster-b'))")))
    check("选择集群 B", click_text(".el-select-dropdown__item", "cluster-b")
          and bool(wait_for("document.body.innerText.includes('demo-release')")))
    check("清单按集群 B 重新请求", "2" in HELM_GETS, HELM_GETS)
    check("页面当前选择保持集群 B", evaljs(
        "document.querySelector('.ov-bar .el-select')?.innerText.includes('cluster-b')"))

    opened = evaljs("""(() => {
      const row = [...document.querySelectorAll('.el-table__body-wrapper tbody tr')]
        .find(e => e.innerText.includes('demo-release'));
      const btn = [...(row?.querySelectorAll('button') || [])].find(e => e.innerText.trim() === '升级');
      if (!btn) return false; btn.click(); return true;
    })()""")
    check("打开集群 B 的升级确认框", bool(opened) and bool(wait_for(
        "document.body.innerText.includes('Helm 升级 · demo-release')")))
    check("输入 Release 名确认", evaljs("""(() => {
      const dialog = [...document.querySelectorAll('.el-dialog')]
        .find(e => e.getClientRects().length && e.innerText.includes('Helm 升级 · demo-release'));
      const inputs = [...(dialog?.querySelectorAll('input') || [])];
      const input = inputs[inputs.length - 1];
      if (!input) return false;
      input.value = 'demo-release';
      input.dispatchEvent(new Event('input', { bubbles: true }));
      return true;
    })()"""))
    confirmed = evaljs("""(() => {
      const dialog = [...document.querySelectorAll('.el-dialog')]
        .find(e => e.getClientRects().length && e.innerText.includes('Helm 升级 · demo-release'));
      const btn = [...(dialog?.querySelectorAll('button') || [])].find(e => e.innerText.trim() === '确认升级');
      if (!btn || btn.disabled) return false; btn.click(); return true;
    })()""")
    check("提交升级并收到 mock 成功结果", bool(confirmed) and bool(wait_for(
        "document.body.innerText.includes('已执行')")), POSTS)
    check("升级请求只发出一次", len(POSTS) == 1, POSTS)
    if POSTS:
        request = POSTS[0]
        check("升级请求绑定集群 B", request["body"].get("cluster_id") == 2, request)
        check("Release、namespace 与 chart 保持正确",
              request["body"].get("confirm_name") == "demo-release"
              and request["body"].get("chart_ref") == "repo/demo-1.0.0"
              and (request["query"].get("namespace") or [""])[0] == "default", request)
    check("浏览器无 JS 异常", not errors, errors[:3])
    if FAIL:
        print("API 请求：", REQUESTS)
        print("升级请求：", POSTS)
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

print(f"\nK8s Helm 升级 Chrome 契约: {len(PASS)}/{len(PASS) + len(FAIL)} 通过")
if FAIL:
    raise SystemExit(1)
