#!/usr/bin/env python3
"""SLS 集中确认的真实 Chrome 契约；所有 API 均为本进程 mock，不连接真实云。

运行：先启动 5273 前端，再执行 python3 tests/quality/sls_discovery_browser.py。
Chrome 使用独立 9334 调试端口和独立临时目录，不杀其他浏览器进程。
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
PROFILE = tempfile.mkdtemp(prefix="qianshu-sls-chrome-")
PORT = 9334
PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print(("  PASS " if condition else "  FAIL ") + name + ("" if condition else f"  <-- {detail}"))


def catalog():
    rows = []
    for p in range(21):
        for s in range(35):
            project, logstore = f"p{p:02d}", f"s{s:02d}"
            kind = s % 7
            base = {
                "project": project, "logstore": logstore, "latest_at": 1_800_000_000,
                "sample_count": 5, "field_count": 3, "fields": ["level", "message", "service"],
                "query_status": "ok", "data_status": "available", "error": "",
                "adoptable": True, "adoption_scope": "primary_only", "name_hint": "",
            }
            if kind == 0:
                row = dict(base, role="application_json", confidence=0.88,
                           evidence=[{"code": "structured_application_fields",
                                      "detail": "结构化日志包含级别字段（level）、消息字段（message）及 3 个字段"}])
            elif kind == 1:
                row = dict(base, role="application_text", confidence=0.56, field_count=1, fields=["content"],
                           evidence=[{"code": "single_message_field", "detail": "样本只有消息字段，建议人工确认"}])
            elif kind == 2:
                row = dict(base, role="cdn", confidence=0.95,
                           fields=["domain", "request_uri", "http_code", "hit"],
                           evidence=[{"code": "cdn_field_fingerprint", "detail": "命中 CDN 访问日志字段"}])
            elif kind == 3:
                row = dict(base, role="slb", confidence=0.96, adoption_scope="multiple",
                           fields=["http_host", "upstream_addr", "status", "slbid"],
                           evidence=[{"code": "slb_field_fingerprint", "detail": "命中 SLB 访问日志字段"}])
            elif kind == 4:
                special = {"p00": "waf", "p01": "ingress", "p02": "system"}.get(project, "unknown")
                row = dict(base, role=special, confidence=0.97 if special != "unknown" else 0,
                           adoptable=False, adoption_scope="unsupported" if special != "unknown" else "primary_only",
                           fields=["foo"], evidence=[{"code": "insufficient_fingerprint", "detail": "字段不足"}])
            elif kind == 5:
                row = dict(base, role="unknown", confidence=0, adoptable=False, sample_count=0,
                           latest_at=None, data_status="no_recent_data", fields=[], field_count=0,
                           evidence=[{"code": "no_recent_data", "detail": "近 24 小时无日志"}])
            else:
                row = dict(base, role="unknown", confidence=0, adoptable=False, sample_count=0,
                           latest_at=None, query_status="failed", data_status="unknown",
                           fields=[], field_count=0, error="mock sample permission denied",
                           evidence=[{"code": "query_failed", "detail": "日志样本查询失败"}])
            rows.append(row)
    counts = {role: sum(x["role"] == role for x in rows)
              for role in ("slb", "cdn", "application_json", "application_text",
                           "waf", "ingress", "system", "unknown")}
    return {
        "account_id": 7, "status": "failed", "sample_window_seconds": 86400,
        "sample_limit": 5, "privacy": "derived_fields_only", "detail": "105 个 Logstore 样本查询失败，保留其他结果",
        "projects": [{"project": f"p{p:02d}", "status": "ready", "logstore_count": 35, "error": ""}
                     for p in range(21)],
        "logstores": rows,
        "coverage": {"projects": 21, "project_failures": 0, "project_listing_complete": True,
                     "project_listing_error": "", "logstores": 735, "sampled": 630,
                     "query_failures": 105, "no_recent_data": 105, "role_counts": counts,
                     "recommended": 420, "complete": False},
    }


DISCOVERY = catalog()
EMPTY_DISCOVERY = {
    "account_id": 8, "status": "skipped", "detail": "该账号没有 Project 或 Logstore，无需配置 SLS 目标",
    "coverage": {"projects": 0, "project_failures": 0, "project_listing_complete": True,
                 "logstores": 0, "sampled": 0, "query_failures": 0, "no_recent_data": 0,
                 "role_counts": {}, "recommended": 0, "complete": True},
    "projects": [], "logstores": [],
}
TARGET = {"key": "sls_targets", "kind": "sls_targets", "label": "SLS 目标确认与查询验证",
          "status": "pending", "detail": "请审阅推荐并验证启用，或明确暂不配置"}
POSTS = []
REQUESTS = []
HELD = {}
HOLD_ACCOUNT = None


def run_payload():
    target_status = TARGET["status"]
    return {"items": [{
        "id": 1, "account_id": 7, "status": "ready" if target_status == "ready" else "attention",
        "stage": "complete", "attempts": 1,
        "items": [{"key": "credential", "status": "ready"},
                  {"key": "sls_discovery", "status": "ready"}, dict(TARGET)],
        "counts": {"ready": 3 if target_status == "ready" else 2, "pending": int(target_status == "pending"),
                   "failed": 0, "skipped": int(target_status == "skipped")},
        "issues": [], "account": {"id": 7, "name": "mock-aliyun-a", "vendor": "aliyun", "enabled": True},
    }, {
        "id": 2, "account_id": 8, "status": "ready", "stage": "complete", "attempts": 1,
        "items": [{"key": "credential", "status": "ready"},
                  {"key": "sls_discovery", "status": "skipped"},
                  {"key": "sls_targets", "status": "skipped", "detail": EMPTY_DISCOVERY["detail"]}],
        "counts": {"ready": 1, "pending": 0, "failed": 0, "skipped": 2},
        "issues": [], "account": {"id": 8, "name": "mock-aliyun-b", "vendor": "aliyun", "enabled": True},
    }]}


def api_response(method, path, body):
    if path == "/auth/me":
        return 200, {"id": 1, "username": "mock-admin", "role": "admin", "must_change_password": False}
    if path == "/bootstrap/status":
        return 200, {"initialized": True, "init_done": True, "has_admin": True, "has_credential": True,
                     "accounts_total": 1, "accounts_enabled": 1, "accounts_ready": 0,
                     "accounts_running": 0, "accounts_with_gaps": 1, "onboarding_ready": False}
    if path == "/bootstrap/report":
        return 200, {"faces": [], "gaps": [], "capabilities": []}
    if path == "/bootstrap/monitor-sources":
        return 200, {"metrics": [], "logs": [], "builtin": None}
    if path == "/clouds/accounts":
        return 200, {"items": [
            {"id": 7, "name": "mock-aliyun-a", "vendor": "aliyun", "vendor_label": "阿里云", "enabled": True},
            {"id": 8, "name": "mock-aliyun-b", "vendor": "aliyun", "vendor_label": "阿里云", "enabled": True},
        ]}
    if path == "/clouds/onboarding-runs":
        return 200, run_payload()
    if path == "/clouds/accounts/7/sls-discovery" and method == "GET":
        return 200, DISCOVERY
    if path == "/clouds/accounts/8/sls-discovery" and method == "GET":
        return 200, EMPTY_DISCOVERY
    if path == "/bootstrap/sls-catalog" and method == "GET":
        return 200, {"projects": [
            {"project": "p00", "status": "ready", "logstores": ["s00", "s03"], "error": ""},
            {"project": "broken", "status": "failed", "logstores": [], "error": "mock project permission denied"},
        ], "coverage": {"projects": 2, "project_failures": 1, "logstores": 2, "complete": False}}
    if path == "/bootstrap/apply-config" and method == "POST":
        POSTS.append((path, body))
        target_count = (len(body.get("slb_targets", [])) + bool(body.get("app_log")) + bool(body.get("cdn_target")))
        TARGET.update(status="ready", detail=f"{target_count} 个目标真实查询成功；其中 0 个近 15 分钟无数据")
        return 200, {"message": "SLS 目标已真实查询并生效", "persisted": True, "verified": [], "no_recent_data": 0}
    if path == "/clouds/accounts/7/sls-discovery/skip" and method == "POST":
        POSTS.append((path, body))
        TARGET.update(status="skipped", detail="用户明确暂不配置 SLS 目标")
        return 200, {"ok": True, "onboarding": run_payload()["items"][0]}
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
        nonlocal_mid = None
        global mid
        mid += 1
        ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))

    def handle_event(msg):
        global HOLD_ACCOUNT
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
            held_account = (7 if path == "/clouds/accounts/7/sls-discovery" else
                            8 if path == "/clouds/accounts/8/sls-discovery" else None)
            if request["method"] == "GET" and held_account is not None and held_account == HOLD_ACCOUNT:
                HELD[held_account] = (params["requestId"], status, payload)
                HOLD_ACCOUNT = None
                return
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
        expression = f"""(() => {{ const el = [...document.querySelectorAll({json.dumps(selector)})]
          .find(e => e.getClientRects().length && (e.innerText || '').trim().startsWith({json.dumps(text)}));
          if (!el) return false; el.click(); return true; }})()"""
        return evaljs(expression)

    def click_mouse(selector):
        point = evaljs(f"""(() => {{ const el = document.querySelector({json.dumps(selector)});
          if (!el) return null; el.scrollIntoView({{block: 'center'}}); const r = el.getBoundingClientRect();
          return {{x: r.x + r.width / 2, y: r.y + r.height / 2}}; }})()""")
        if not point:
            return False
        for event in ("mousePressed", "mouseReleased"):
            send("Input.dispatchMouseEvent", {"type": event, "x": point["x"], "y": point["y"],
                                               "button": "left", "clickCount": 1})
        return True

    def choose_account(name):
        click_mouse(".sls-account-tools .el-select__wrapper")
        wait_for(f"[...document.querySelectorAll('.el-select-dropdown__item')].some(e => e.getClientRects().length && e.innerText.trim() === {json.dumps(name)})")
        return evaljs(f"""(() => {{ const el = [...document.querySelectorAll('.el-select-dropdown__item')]
          .find(e => e.getClientRects().length && e.innerText.trim() === {json.dumps(name)});
          if (!el) return false; el.click(); return true; }})()""")

    def release_discovery(account_id):
        request_id, status, payload = HELD.pop(account_id)
        fire("Fetch.fulfillRequest", {
            "requestId": request_id, "responseCode": status,
            "responseHeaders": [{"name": "Content-Type", "value": "application/json"}],
            "body": base64.b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode(),
        })

    send("Page.enable")
    send("Runtime.enable")
    send("Network.enable")
    send("Fetch.enable", {"patterns": [{"urlPattern": f"{SITE}/api/*", "requestStage": "Request"}]})
    send("Emulation.setDeviceMetricsOverride", {"width": 1440, "height": 1100, "deviceScaleFactor": 1, "mobile": False})
    send("Page.navigate", {"url": f"{SITE}/login"})
    wait_for("document.readyState === 'complete'")
    evaljs("localStorage.setItem('qianshu.token', 'mock-browser-token'); localStorage.setItem('qianshu.account', '0'); true")
    send("Page.navigate", {"url": f"{SITE}/bootstrap"})
    check("集中确认工作区真实渲染", bool(wait_for("document.body.innerText.includes('日志自动发现与接入') && document.body.innerText.includes('735')")), REQUESTS)
    check("多个高置信主目标候选不擅自预选", bool(wait_for(
        "document.querySelectorAll('.sls-discovery .el-table__body-wrapper tbody tr').length > 0"
        " && document.querySelectorAll('.sls-discovery .el-radio.is-checked').length === 0")))
    check("低置信文本日志不预选", evaljs("[...document.querySelectorAll('.sls-discovery .el-radio.is-checked input')].every(e => e.value !== 'p00/s01')"))
    check("用户明确选择应用与 CDN 主目标", click_text(".sls-discovery .el-radio", "设为应用主日志")
          and click_text(".sls-discovery .el-radio", "设为 CDN 主日志")
          and bool(wait_for("document.querySelectorAll('.sls-discovery .el-radio.is-checked').length === 2")))

    check("切换完整目录筛选", click_text(".sls-filter-row .el-radio-button", "全部"))
    check("WAF 等新角色有明确中文标签且不可采纳", bool(wait_for(
        "[...document.querySelectorAll('.sls-discovery .el-table__body-wrapper tbody tr')]"
        ".some(e => e.innerText.includes('s04') && e.innerText.includes('WAF 访问日志（仅识别）')"
        " && e.innerText.includes('保持未接入'))")))
    check("735 条目录按页渲染而非丢弃或一次渲染", bool(wait_for("document.querySelector('.sls-pagination')?.innerText.includes('735') && document.querySelectorAll('.sls-discovery .el-table__body-wrapper tbody tr').length === 12")))
    evaljs("document.querySelector('.sls-pagination .btn-next')?.click()")
    check("前端分页可浏览后续目标", bool(wait_for("document.querySelector('.sls-discovery .el-table__body-wrapper')?.innerText.includes('s12')")))
    click_text(".sls-filter-row .el-radio-button", "可采纳")
    wait_for("document.querySelector('.sls-discovery .el-table__body-wrapper')?.innerText.includes('s00')")

    evaljs("document.querySelector('.sls-discovery .el-table__expand-icon')?.click()")
    check("展开行展示字段证据而非原始日志", bool(wait_for("document.body.innerText.includes('结构化日志包含级别字段') && document.body.innerText.includes('字段指纹')")))
    check("选择 SLB", click_text(".sls-discovery .el-checkbox", "选用 SLB"))
    wait_for("!!document.querySelector('.sls-discovery .el-select__wrapper')")
    click_mouse(".sls-discovery .el-select__wrapper")
    wait_for("[...document.querySelectorAll('.el-select-dropdown__item')].some(e => e.getClientRects().length && e.innerText.includes('外层 SLB'))")
    check("SLB 必须人工选择外层", click_text(".el-select-dropdown__item", "外层 SLB"))
    wait_for("document.body.innerText.includes('验证并应用 3 个目标')")
    screenshot = send("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True})
    if screenshot.get("data"):
        pathlib.Path("/tmp/qianshu-sls-discovery.png").write_bytes(base64.b64decode(screenshot["data"]))

    check("提交集中验证", click_text(".sls-actions .el-button--primary", "验证并应用"))
    wait_for("document.body.innerText.includes('3 个目标真实查询成功')")
    apply_body = next((body for path, body in POSTS if path == "/bootstrap/apply-config"), {})
    check("采纳请求绑定同一账号与真实选择", apply_body.get("account_id") == 7
          and apply_body.get("app_log") == ["p00", "s00"]
          and apply_body.get("cdn_target") == ["p00", "s02"]
          and apply_body.get("slb_targets") == [{"project": "p00", "logstore": "s03", "chain": "outer"}], apply_body)

    check("打开暂不配置确认", click_text(".sls-actions .el-button", "暂不配置日志"))
    check("确认用户决定", click_text(".el-message-box__btns .el-button--primary", "暂不配置日志"))
    check("跳过不等于查询验证通过", bool(wait_for("document.body.innerText.includes('用户明确暂不配置 SLS 目标')")))
    check("跳过请求仍绑定当前账号", any(path == "/clouds/accounts/7/sls-discovery/skip" for path, _ in POSTS))

    check("高级手选默认折叠且可展开", click_text(".sls-manual .el-collapse-item__header", "高级手动配置"))
    check("高级目录保留独立枚举失败", click_text(".sls-manual .el-button", "拉取 SLS 目录")
          and bool(wait_for("document.querySelector('.sls-manual')?.innerText.includes('mock project permission denied')")))
    click_mouse(".sls-manual .el-table__body-wrapper tbody tr:first-child .el-select__wrapper")
    wait_for("[...document.querySelectorAll('.el-select-dropdown__item')].some(e => e.getClientRects().length && e.innerText.trim() === 'p00')")
    evaljs("""(() => { const el = [...document.querySelectorAll('.el-select-dropdown__item')]
      .find(e => e.getClientRects().length && e.innerText.trim() === 'p00');
      if (!el) return false; el.click(); return true; })()""")
    check("高级手选可选择明确目标", bool(wait_for(
        "document.querySelector('.sls-manual .el-table__body-wrapper tbody tr')?.innerText.includes('p00')"
        " && document.querySelector('.sls-manual .el-table__body-wrapper tbody tr')?.innerText.includes('s00')")))

    HOLD_ACCOUNT = 8
    choose_account("mock-aliyun-b")
    check("切换账号立即清空旧目录与选择", bool(wait_for(
        "document.querySelector('.sls-account-tools .el-select__wrapper')?.innerText.includes('mock-aliyun-b')"
        " && !document.querySelector('.sls-discovery')?.innerText.includes('735')"
        " && !document.querySelector('.sls-discovery .el-table__body-wrapper')")))
    release_discovery(8)
    check("空目录与合法跳过分别呈现", bool(wait_for(
        "document.querySelector('.sls-discovery')?.innerText.includes('该账号没有 Project 或 Logstore')"
        " && document.querySelector('.sls-discovery')?.innerText.includes('该账号没有发现 Logstore')")))

    HOLD_ACCOUNT = 7
    choose_account("mock-aliyun-a")
    deadline = time.time() + 10
    while 7 not in HELD and time.time() < deadline:
        evaljs("true")
        time.sleep(0.1)
    choose_account("mock-aliyun-b")
    wait_for("document.querySelector('.sls-discovery')?.innerText.includes('该账号没有发现 Logstore')")
    release_discovery(7)
    evaljs("new Promise(resolve => setTimeout(() => resolve(true), 250))")
    check("旧账号慢响应不能覆盖当前账号", evaljs(
        "document.querySelector('.sls-account-tools .el-select__wrapper')?.innerText.includes('mock-aliyun-b')"
        " && document.querySelector('.sls-discovery')?.innerText.includes('该账号没有发现 Logstore')"
        " && !document.querySelector('.sls-discovery')?.innerText.includes('735')"))
    choose_account("mock-aliyun-a")
    wait_for("document.querySelector('.sls-discovery')?.innerText.includes('735')")
    click_text(".sls-manual .el-button", "拉取 SLS 目录")
    check("切换账号清除高级手选旧目标", bool(wait_for(
        "document.querySelector('.sls-manual .el-table__body-wrapper tbody tr')?.innerText.includes('选择 project')"
        " && document.querySelector('.sls-manual .el-table__body-wrapper tbody tr')?.innerText.includes('选择 logstore')"
        " && !document.querySelector('.sls-manual .el-table__body-wrapper tbody tr')?.innerText.includes('p00')")))
    check("浏览器无 JS 异常", not errors, errors[:3])
    if FAIL:
        print("API 请求：", REQUESTS)
        print("页面末尾文本：", (evaljs("document.body.innerText.slice(-3000)") or "")[:3000])
        final_shot = send("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True})
        if final_shot.get("data"):
            pathlib.Path("/tmp/qianshu-sls-discovery-failed.png").write_bytes(base64.b64decode(final_shot["data"]))
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

print(f"\nSLS 集中确认 Chrome 契约: {len(PASS)}/{len(PASS) + len(FAIL)} 通过")
if FAIL:
    raise SystemExit(1)
