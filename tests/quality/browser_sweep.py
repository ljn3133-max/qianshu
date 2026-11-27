#!/usr/bin/env python3
"""浏览器全页面渲染检查（第三环）：真实 Chrome 遍历——渲染级验收（naturalWidth/文本/console）。"""
import json
import subprocess
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from chrome_run import chrome_popen
from credentials import require_test_credentials
from pages import DEEP_LINKS, LIVE_EXTERNAL_PAGES, PAGES as PAGE_NAMES

import sys
import time
import urllib.request

USERNAME, PWD = require_test_credentials()
API = _os.environ.get("QIANSHU_API", "http://127.0.0.1:8810")
SITE = _os.environ.get("QIANSHU_SITE", "http://127.0.0.1:5273")
READONLY = _os.environ.get("QIANSHU_QUALITY_READONLY") == "1"
SKIPPED_EXTERNAL = sorted(LIVE_EXTERNAL_PAGES) if READONLY else []

login = json.loads(subprocess.run(
    ["curl", "-s", "-X", "POST", f"{API}/api/auth/login", "-H", "Content-Type: application/json",
     "-d", json.dumps({"username": USERNAME, "password": PWD})],
    capture_output=True, text=True).stdout)
token = login.get("token", "")
if not token:
    # 登录失败必须早退、只报一条，避免将未登录误报为逐页导航失败。
    print("页面渲染: 登录失败，无法进行（检查 QIANSHU_TEST_USERNAME / PASSWORD 环境变量）")
    print("页面质量: 0 项问题")
    print("JS 错误: 登录失败未执行")
    sys.exit(2)

subprocess.run("ps -eo pid,comm | awk '$2 ~ /chrome/ {print $1}' | while read p; do kill $p 2>/dev/null; done",
               shell=True)
subprocess.run("rm -rf /tmp/chrome-q; mkdir -p /tmp/chrome-q", shell=True)
chrome_popen(["google-chrome", "--headless=new", "--disable-gpu",
              "--remote-debugging-port=9333", "--remote-allow-origins=*",
              "--user-data-dir=/tmp/chrome-q", "about:blank"], "/tmp/chrome-q")
time.sleep(8)

import websocket

targets = json.loads(urllib.request.urlopen("http://127.0.0.1:9333/json/list").read())
page = next(t for t in targets if t["type"] == "page")
ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=90)
mid = [0]
errors = []


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
            errors.append((msg["params"].get("exceptionDetails", {}).get("exception", {}).get("description") or "")[:100])


def evaljs(expr):
    r = send("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
    return r.get("result", {}).get("value")


send("Page.enable"); send("Runtime.enable")
send("Page.navigate", {"url": f"{SITE}/login"})
time.sleep(3)
evaljs(f"localStorage.setItem('qianshu.token', '{token}'); true")

# 裸 key 扫描（页面级/页签级共用）：从可见文本节点拼文本，跳过 [data-raw-ok]
# （刻意保留的技术标识，如审计动作码）和域名（dashboard.example.com 不是漏翻）。
_KEY_SCAN = r"""
      const keyText = (() => {
        let s = '';
        const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        let n;
        while ((n = w.nextNode())) {
          const el = n.parentElement;
          if (!el || el.closest('[data-raw-ok]')) continue;
          // getClientRects 走布局：祖先 tab-pane 的 display:none 也能识别；
          // 只看父节点自身的 display/visibility 会把隐藏页签内容算进来（误报源）
          if (!el.getClientRects().length) continue;
          s += n.nodeValue + ' ';
        }
        return s;
      })();
      const keyRe = /(?:dashboard|agent|layout|ops|cost|expiry|alerts|changes|profiles|topology|synth|oncall|integrations|bootstrap|settings|k8s|machines|clouds|dba|diagnose|inspections|monitor)\.[a-z][A-Za-z0-9_.]+/g;
      const raw = (keyText.match(keyRe) || []).find(
        (k) => !/\.(?:com|cn|net|org|io|co|top|xyz|cc|me|ai|cloud)$/i.test(k));
"""
PAGE_QUALITY_JS = """(() => {
      const t = document.body.innerText || '';
""" + _KEY_SCAN + """      const act = (sel) => [...document.querySelectorAll(sel)]
        .filter(e => e.getClientRects().length)   // 隐藏页签的 is-active 不算
        .map(e => (e.innerText || '').trim()).filter(Boolean);
      return {
        path: location.pathname,
        search: location.search,
        tabActive: act('.el-tabs__item.is-active'),
        subActive: act('.sub-tabs .el-tabs__item.is-active'),
        len: t.length,
        fail: /加载失败|请求失败|Network Error|Failed to fetch/.test(t),
        raw_key: raw || '',
        nan: /\\bNaN\\b|Invalid Date|\\bundefined\\b/.test(t),
        actions: document.querySelectorAll('button, .el-button, a[href], [role=button]').length
      };
    })()"""
TAB_QUALITY_JS = """(() => {
      const t = document.body.innerText || '';
""" + _KEY_SCAN + """      return {
        fail: /加载失败|请求失败|Network Error|Failed to fetch/.test(t),
        raw_key: raw || '',
        nan: /\\bNaN\\b|Invalid Date|\\bundefined\\b/.test(t)
      };
    })()"""

PAGES = [("/" + p, "", "") for p in PAGE_NAMES if p not in SKIPPED_EXTERNAL]
PAGES += [(f"/{base}{q}", top, sub) for base, q, top, sub in DEEP_LINKS]  # 页签深链也逐页渲染
ok = 0
quality_issues = []
for p, want_top, want_sub in PAGES:
    p_path, _, p_query = p.partition("?")
    sep = "&" if "?" in p else "?"
    send("Page.navigate", {"url": f"{SITE}{p}{sep}t={int(time.time())}"})
    # 条件等待：文本就绪或 9 秒兜底，避免固定 sleep 对慢页不够
    txt = 0
    for _ in range(18):
        time.sleep(0.5)
        txt = evaljs("document.body.innerText.length") or 0
        if txt > 300:
            break
    # 通用页面质量契约：不只看"有没有文字"，
    # 还查报错文案/裸 i18n key/未定义值/不可操作——新页面自动继承，不用逐页写断言。
    q = evaljs(PAGE_QUALITY_JS) or {}
    bad = q.get("fail") or q.get("len", 0) < 100
    if not bad:
        ok += 1
    else:
        print(f"  ✗ {p}: 文本 {txt}")
    if q.get("path") != p_path:
        quality_issues.append(f"{p}: 导航未落到本页（{q.get('path')}）")
    elif p_query:
        # 深链：URL 参数必须原样保留 + 对应页签必须真的被激活。
        # location.pathname 不含 query，深链必须分别校验 path、query 和激活页签。
        got = dict(x.split("=", 1) for x in (q.get("search") or "").lstrip("?").split("&") if "=" in x)
        want = dict(x.split("=", 1) for x in p_query.split("&") if "=" in x)
        lost = [f"{k}={v}" for k, v in want.items() if got.get(k) != v]
        if lost:
            quality_issues.append(f"{p}: 深链参数丢失（{lost}）")
        elif want_top and want_top not in (q.get("tabActive") or []):
            quality_issues.append(f"{p}: 深链未激活页签「{want_top}」（激活：{q.get('tabActive')}）")
        elif want_sub and want_sub not in (q.get("subActive") or []):
            quality_issues.append(f"{p}: 深链未激活二级页签「{want_sub}」（激活：{q.get('subActive')}）")
    if q.get("raw_key"):
        quality_issues.append(f"{p}: 裸 i18n key `{q['raw_key']}`")
    if q.get("nan"):
        quality_issues.append(f"{p}: 文案含 NaN/undefined/Invalid Date")
    if not q.get("fail") and q.get("actions", 0) < 1:
        quality_issues.append(f"{p}: 无可操作控件（按钮/链接）")

    # 页签级覆盖：页面整体正常 ≠ 每个页签正常。
    # 功能越来越多、页签是主要的信息分载方式，只渲染默认页签会漏掉后面所有页签的
    # 裸 key / NaN / 报错文案。这里只点 tab 头（不发写操作），复用同一质量契约，
    # 新加页签自动纳入，不用逐页写断言。
    if q.get("path") != p_path:
        continue   # 导航没落到本页时不点残留页面的页签，避免误报叠加
    tabs = evaljs(
        "(() => [...document.querySelectorAll('.el-tabs__item:not(.is-disabled)')]"
        ".map(e => (e.innerText || '').trim()).filter(Boolean).slice(0, 12))()") or []
    seen_labels = set()
    for ti, label in enumerate(tabs):
        if label in seen_labels:
            continue
        seen_labels.add(label)
        err_before = len(errors)
        evaljs(f"(() => {{ const e = document.querySelectorAll('.el-tabs__item:not(.is-disabled)')[{ti}];"
               " if (e) e.click(); return true; })()")
        time.sleep(1.0)
        tq = evaljs(TAB_QUALITY_JS) or {}
        why = []
        if tq.get("fail"):
            why.append("报错文案")
        if tq.get("raw_key"):
            why.append(f"裸 i18n key `{tq['raw_key']}`")
        if tq.get("nan"):
            why.append("NaN/undefined/Invalid Date")
        if len(errors) > err_before:
            why.append(f"JS 异常 {errors[-1]}")
        if why:
            quality_issues.append(f"{p} › 页签「{label}」: {'；'.join(why)}")
print(f"页面渲染: {ok}/{len(PAGES)} 正常")
if SKIPPED_EXTERNAL:
    print("只读外部页面跳过: " + "、".join(SKIPPED_EXTERNAL) + "（默认加载会查询真实 SLS）")
print(f"页面质量: {len(quality_issues)} 项问题")
for i in quality_issues:
    print(f"  ✗ [质量] {i}")
print(f"JS 错误: {'零' if not errors else errors[:3]}")
ws.close()
