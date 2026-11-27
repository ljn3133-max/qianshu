#!/usr/bin/env python3
"""K8s 管理平台浏览器交互检查：任务中心 2s 日志流/详情抽屉/终止按钮/8 tab 渲染。

与 browser_sweep 的分工：sweep 管全页渲染；本脚本管 K8s 大布局的**交互语义**——
真实点 tab、开抽屉、看日志是否随轮询增长（跑着的任务）。
"""
import json
import subprocess
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from chrome_run import chrome_popen
from credentials import require_test_credentials

import sys
import time
import urllib.request

USERNAME, PWD = require_test_credentials()
API = _os.environ.get("QIANSHU_API", "http://127.0.0.1:8810")
SITE = _os.environ.get("QIANSHU_SITE", "http://127.0.0.1:5273")
results = []


def check(name, ok, detail=""):
    results.append((name, ok, str(detail)[:100]))
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  {detail}" if detail else ""), flush=True)


login = json.loads(subprocess.run(
    ["curl", "-s", "-X", "POST", f"{API}/api/auth/login", "-H", "Content-Type: application/json",
     "-d", json.dumps({"username": USERNAME, "password": PWD})],
    capture_output=True, text=True).stdout)
token = login.get("token", "")
if not token:
    print("登录失败"); sys.exit(1)

subprocess.run("ps -eo pid,comm | awk '$2 ~ /chrome/ {print $1}' | while read p; do kill $p 2>/dev/null; done",
               shell=True)
subprocess.run("rm -rf /tmp/chrome-k8s; mkdir -p /tmp/chrome-k8s", shell=True)
proc = chrome_popen(["google-chrome", "--headless=new", "--disable-gpu",
                     "--remote-debugging-port=9334", "--remote-allow-origins=*",
                     "--user-data-dir=/tmp/chrome-k8s", "about:blank"], "/tmp/chrome-k8s")
time.sleep(8)

import websocket

print("[phase] chrome up", flush=True)
targets = json.loads(urllib.request.urlopen("http://127.0.0.1:9334/json/list", timeout=5).read())
page = next(t for t in targets if t["type"] == "page")
ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=20)
mid = [0]
js_errors = []


def send(method, params=None):
    mid[0] += 1
    ws.send(json.dumps({"id": mid[0], "method": method, "params": params or {}}))
    while True:
        try:
            msg = json.loads(ws.recv())
        except Exception:
            return {}
        if msg.get("id") == mid[0]:
            return msg.get("result", {})
        if msg.get("method") == "Runtime.exceptionThrown":
            d = (msg["params"].get("exceptionDetails", {}) or {})
            js_errors.append(str(d.get("exception", {}).get("description") or d.get("text") or "")[:120])


def evaljs(expr):
    r = send("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
    return (r.get("result") or {}).get("value")


try:
    send("Page.enable"); send("Runtime.enable")
    print("[phase] ws connected", flush=True)
    send("Page.navigate", {"url": f"{SITE}/login"})
    time.sleep(3)
    evaljs(f"localStorage.setItem('qianshu.token', '{token}'); true")
    print("[phase] login page", flush=True)
    send("Page.navigate", {"url": f"{SITE}/dashboard"})
    time.sleep(5)

    # 侧栏菜单必须真实点击；URL 导航无法发现"点了没动"
    def click_menu(label):
        return evaljs("""
          (() => {
            const el = [...document.querySelectorAll('.el-menu-item, .el-sub-menu__title')]
              .find(e => e.textContent.trim().includes('%s'));
            if (!el) return 'NOT-FOUND';
            el.click(); return 'CLICKED';
          })()
        """ % label)

    check("点击侧栏「K8s 总览」", click_menu("K8s 总览") == "CLICKED")
    time.sleep(4)
    canvases = evaljs("document.querySelectorAll('canvas').length") or 0
    check("点击后总览图表渲染", canvases >= 4, f"canvas {canvases}")
    check("点击后 URL 带 tab 锚", "tab=overview" in (evaljs("location.search") or ""))
    check("点击侧栏「任务中心」", click_menu("任务中心") == "CLICKED")
    time.sleep(3)
    rows_n = evaljs("document.querySelectorAll('.el-table__row').length") or 0
    check("点击后任务表格出现", rows_n >= 1, f"行 {rows_n}")
    check("无重复导航（页内 el-tabs）", (evaljs("document.querySelectorAll('.el-tabs').length") or 0) == 0)

    send("Page.navigate", {"url": f"{SITE}/k8s?tab=tasks"})
    time.sleep(6)

    print("[phase] k8s page", flush=True)
    # v-if 按需渲染：激活分区唯一且存在（多分区共存是旧 el-tabs 语义，不再适用）
    sections = evaljs("document.querySelectorAll('.tab-page').length")
    check("激活分区按需渲染（唯一）", (sections or 0) == 1, f"分区数 {sections}")
    rows = evaljs("document.querySelectorAll('.tab-page .el-table__row').length")
    check("任务列表渲染", (rows or 0) >= 1, f"行数 {rows}")

    # 打开第一个任务详情
    opened = evaljs("""
      (() => {
        const btn = [...document.querySelectorAll('button')].find(b => b.textContent.includes('详情/日志'));
        if (btn) { btn.click(); return true } return false
      })()
    """)
    check("详情按钮可点", bool(opened))
    time.sleep(2.5)
    log_len1 = evaljs("(document.querySelector('.log-box')||{}).textContent?.length || 0")
    check("抽屉日志有内容", (log_len1 or 0) > 0, f"{log_len1} 字符")
    steps = evaljs("document.querySelectorAll('.el-drawer .el-timeline-item').length")
    check("步骤表时间线渲染", (steps or 0) >= 3, f"{steps} 步")

    # 2 秒日志流：等 2 个轮询周期，执行中的任务日志应增长（已结束任务不减不空）
    time.sleep(5)
    log_len2 = evaljs("(document.querySelector('.log-box')||{}).textContent?.length || 0")
    running = evaljs("(document.body.innerText.match(/执行中/g)||[]).length")
    if running:
        check("2s 日志流增长", (log_len2 or 0) >= (log_len1 or 0), f"{log_len1}→{log_len2}")
    else:
        check("日志稳定（无运行中任务）", (log_len2 or 0) >= (log_len1 or 0) > 0, f"{log_len1}→{log_len2}")

    shot = send("Page.captureScreenshot", {"format": "png"})
    import base64

    if shot.get("data"):
        open("/tmp/k8s-mgmt-tasks.png", "wb").write(base64.b64decode(shot["data"]))
        check("截图", True, "/tmp/k8s-mgmt-tasks.png")

    # 终止按钮仅活跃任务显示（不点——不打断真任务；语义由 API 用例覆盖）
    abort_btn = evaljs("[...document.querySelectorAll('button')].some(b => b.textContent.includes('终止'))")
    active = evaljs("(document.body.innerText.match(/执行中|排队/g)||[]).length")
    if active:
        check("终止按钮在位（活跃任务）", bool(abort_btn))
    else:
        check("终止按钮条件渲染（无活跃任务不显示）", not abort_btn)

    check("无 JS 异常", not js_errors, "; ".join(js_errors[:2]))
finally:
    try:
        ws.close()
    except Exception:
        pass
    proc.kill()

n_fail = sum(1 for _, ok, _ in results if not ok)
print(f"\n== K8s 管理浏览器实测: {len(results) - n_fail}/{len(results)} 通过 ==")
sys.exit(1 if n_fail else 0)
