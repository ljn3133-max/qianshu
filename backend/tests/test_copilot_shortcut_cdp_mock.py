#!/usr/bin/env python3
"""千枢智能体 · ⌘K/Ctrl+K 全局快捷键 + 会话面板默认收起 mock CDP 测试。

运行：cd /opt/qianshu/backend && python3 tests/test_copilot_shortcut_cdp_mock.py
形态：真 Chrome（headless，降权 nobody 走 chrome_run）+ CDP（Runtime.evaluate 直查 DOM）
+ Page.addScriptToEvaluateOnNewDocument 注入 /api mock 层（XHR/fetch 全拦）——**零后端依赖**
（不碰 8810/不启服务），页面走 vite dev（5273）拿真实组件（CopilotFloat.vue / Agent.vue
编译产物热加载）。与 test_agent_card_cdp_mock.py 同形态。

验证项：
  1 任意页 Ctrl+K（linux）/⌘K（mac）唤起浮窗；打开即预填当前页 ctx seed prompt
    （含页面名 + query 关键参数 + 「请…」续写位），光标停在省略号前
  2 Esc 收起；再按一次收起 / 再按展开（toggle）
  3 el-select 等组件内部按键不误伤（target 守卫放行）
  4 /agent 工作台默认不显示浮窗球（不双入口），Ctrl+K 仍可显式唤起
  5 会话面板：无偏好记录默认关；点开为悬浮抽屉（消息流 .center 宽度不被挤压）；
    偏好记忆 localStorage（刷新后仍开）；清偏好刷新回默认口径（关）
  6 i18n 新增键（shortcut_tip / seed / seed_params）zh/en 成对
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "tests", "quality"))
from chrome_run import chrome_popen  # noqa: E402  统一降权启动 Chrome（tests/quality/chrome_run.py）

SITE = os.environ.get("QIANSHU_SITE", "http://127.0.0.1:5273")
CDP_PORT = 9348   # 避开 browser_sweep 9333 / 视觉 9344 / 变更卡 9347，防并行互踩
PROFILE = "/tmp/chrome-cmdk-cdp"
PANEL_KEY = "qianshu.agent.panel_open"

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


# ---------------- i18n zh/en 成对（静态检查，先于浏览器） ----------------

def check_i18n():
    def keys(path):
        return set(re.findall(r"'(agent\.[\w.]+)'\s*:", open(path).read()))
    zh = keys("/opt/qianshu/frontend/src/i18n/locales/zh-CN/agent.js")
    en = keys("/opt/qianshu/frontend/src/i18n/locales/en-US/agent.js")
    check("6 i18n agent 键 zh/en 成对", zh == en,
          f"zh-only={sorted(zh - en)[:5]} en-only={sorted(en - zh)[:5]}")
    for k in ("agent.copilot.shortcut_tip", "agent.copilot.seed", "agent.copilot.seed_params"):
        check(f"6 i18n 新键 {k} 两边都有", k in zh and k in en)


# ---------------- /api mock 层（注入页面，XHR/fetch 全拦 + 调用留痕） ----------------

MOCK_JS = r"""
(() => {
  localStorage.setItem('qianshu.token', 'mock-token');
  localStorage.setItem('qianshu.account', '0');
  window.__mockCalls = [];
  function apiResponse(method, path, body) {
    window.__mockCalls.push({ method: method, url: path, body: body });
    if (/\/auth\/me$/.test(path)) {
      return { id: 1, username: 'admin', role: 'admin', must_change_password: false,
               permission_codes: ['agent.change', 'agent.query'] };
    }
    if (/\/bootstrap\/status$/.test(path)) return { init_done: true };
    if (/\/ai\/settings$/.test(path)) return [{ id: 1, name: 'mock-model', enabled: true }];
    if (/\/agent\/sessions$/.test(path)) {
      return { items: [{ id: 1, title: 'mock 会话', message_count: 2, pinned: false,
                         last_active_at: '2026-11-06T09:00:00' }], total: 1 };
    }
    if (/\/agent\/system-snapshot$/.test(path)) {
      return { snapshot: { cloud_accounts: [{ name: 'mock-oss' }], assets: [], topology: {} } };
    }
    if (/\/agent\/changes$/.test(path)) return { items: [], total: 0 };
    if (/\/messages\/inbox$/.test(path)) return { total: 0, unread: 0, items: [] };
    // Alerts 页 el-table 的 rules/channels 是纯数组契约（对象会让 el-table 报 rows 不可迭代）
    if (/\/alerts\/(rules|channels)$/.test(path)) return [];
    return { items: [], total: 0 };
  }
  function handle(method, url, bodyRaw) {
    const path = String(url || '').split('?')[0];
    let body = null;
    try { body = bodyRaw ? JSON.parse(bodyRaw) : null; } catch (e) { body = null; }
    return apiResponse(String(method || 'GET').toUpperCase(), path, body);
  }
  class FakeXHR {
    constructor() {
      this.readyState = 0; this.status = 0; this.responseText = ''; this.response = '';
      this.upload = { addEventListener() {}, removeEventListener() {} };
      this._listeners = {};
    }
    open(method, url) { this._method = method; this._url = url; this.readyState = 1; }
    setRequestHeader() {} abort() {} overrideMimeType() {}
    addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); }
    removeEventListener(t, fn) {
      this._listeners[t] = (this._listeners[t] || []).filter((f) => f !== fn);
    }
    getAllResponseHeaders() { return 'content-type: application/json\r\n'; }
    getResponseHeader(k) { return String(k).toLowerCase() === 'content-type' ? 'application/json' : null; }
    send(bodyRaw) {
      const payload = handle(this._method, this._url, bodyRaw);
      this.status = 200; this.statusText = 'OK'; this.readyState = 4;
      this.responseText = JSON.stringify(payload); this.response = this.responseText;
      if (this.onreadystatechange) this.onreadystatechange();
      if (this.onload) this.onload();
      if (this.onloadend) this.onloadend();
      (this._listeners.load || []).forEach((f) => f({ target: this }));
      (this._listeners.loadend || []).forEach((f) => f({ target: this }));
    }
  }
  window.XMLHttpRequest = FakeXHR;
  window.fetch = (input, init) => {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const method = (init && init.method) || 'GET';
    const payload = handle(method, url, (init && init.body) || null);
    return Promise.resolve(new Response(JSON.stringify(payload),
      { status: 200, headers: { 'content-type': 'application/json' } }));
  };
})();
"""

# 键盘模拟（真实 keydown 冒泡到 window 监听）：linux 头less 用 Ctrl+K，与 ⌘K 同分支
KEY_CTRL_K = ("(() => { document.body.dispatchEvent(new KeyboardEvent('keydown',"
              " { key: 'k', code: 'KeyK', ctrlKey: true, bubbles: true, cancelable: true }));"
              " return true; })()")
KEY_ESC = ("(() => { document.body.dispatchEvent(new KeyboardEvent('keydown',"
           " { key: 'Escape', code: 'Escape', bubbles: true, cancelable: true }));"
           " return true; })()")
# el-select 场景不误伤：事件 target 落在 .el-select 内应被守卫放行
KEY_K_IN_SELECT = ("(() => {"
                   " const d = document.createElement('div'); d.className = 'el-select';"
                   " document.body.appendChild(d);"
                   " d.dispatchEvent(new KeyboardEvent('keydown',"
                   "  { key: 'k', code: 'KeyK', ctrlKey: true, bubbles: true, cancelable: true }));"
                   " const hit = !!document.querySelector('.cf-panel');"
                   " d.remove(); return hit; })()")

DRAWER_STATE = ("(() => { const d = document.querySelector('.el-drawer');"
                " if (!d) return 'closed';"
                " return d.getBoundingClientRect().width > 0 ? 'open' : 'closed'; })()")

PANEL_PREF = f"localStorage.getItem('{PANEL_KEY}')"

# ---------------- Chrome + CDP ----------------

proc = None
ws = None
mid = [0]
page_errors = []


def cdp_send(method, params=None, timeout=30):
    mid[0] += 1
    ws.send(json.dumps({"id": mid[0], "method": method, "params": params or {}}))
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            msg = json.loads(ws.recv())
        except Exception:
            continue
        if msg.get("id") == mid[0]:
            return msg.get("result", {})
        if msg.get("method") == "Runtime.exceptionThrown":
            page_errors.append(
                (msg["params"].get("exceptionDetails", {}).get("exception", {}).get("description") or "")[:160])
    return {}


def evaljs(expr):
    r = cdp_send("Runtime.evaluate",
                 {"expression": expr, "returnByValue": True, "awaitPromise": True})
    return (r.get("result") or {}).get("value")


def wait_js(expr, want, timeout=40, label=""):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = evaljs(expr)
        if v == want:
            return True
        time.sleep(0.5)
    print(f"    (等待超时: {label} -> {evaljs(expr)!r})")
    return False


def main():
    global proc, ws
    check_i18n()

    try:
        urllib.request.urlopen(f"{SITE}/", timeout=5)
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] vite dev 不可达（{SITE}）：{e} —— 本测试需要前端 dev 服务在跑")
        raise SystemExit(1)

    subprocess.run(f"rm -rf {PROFILE}; mkdir -p {PROFILE}", shell=True)
    proc = chrome_popen(["google-chrome", "--headless=new", "--disable-gpu",
                         f"--remote-debugging-port={CDP_PORT}", "--remote-allow-origins=*",
                         f"--user-data-dir={PROFILE}", "about:blank"], PROFILE)
    try:
        import websocket

        targets = None
        for _ in range(30):
            try:
                targets = json.loads(urllib.request.urlopen(
                    f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=3).read())
                break
            except Exception:  # noqa: BLE001
                time.sleep(1)
        page = next(t for t in targets if t["type"] == "page")
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=60)

        cdp_send("Page.enable")
        cdp_send("Runtime.enable")
        cdp_send("Page.addScriptToEvaluateOnNewDocument", {"source": MOCK_JS})

        # ---- 任意页（/alerts?tab=active&host=web01）：⌘K 唤起 + seed prompt ----
        print("== 1 任意页 Ctrl+K 唤起浮窗 + ctx seed prompt 预填 ==")
        cdp_send("Page.navigate", {"url": f"{SITE}/alerts?tab=active&host=web01"})
        ok = wait_js("document.querySelectorAll('.cf-ball').length", 1, 40, "悬浮球")
        check("1 非工作台页渲染悬浮球", ok)
        check("1 初始浮窗收起", evaljs("document.querySelectorAll('.cf-panel').length") == 0)
        evaljs(KEY_CTRL_K)
        ok = wait_js("document.querySelectorAll('.cf-panel').length", 1, 15, "⌘K 唤起")
        check("1 任意页 Ctrl+K 唤起浮窗", ok)
        seed = evaljs("document.querySelector('.cf-panel textarea')?.value || ''")
        check("1 seed prompt 含页面名（告警中心）+ 关键参数 + 「请…」续写位",
              '当前页面：' in (seed or '') and '告警中心' in (seed or '')
              and 'tab=active' in (seed or '') and 'host=web01' in (seed or '')
              and (seed or '').endswith('请…'),
              repr(seed))
        caret = evaljs("""(() => {
          const ta = document.querySelector('.cf-panel textarea');
          if (!ta || document.activeElement !== ta) return -1;
          return ta.selectionStart;
        })()""")
        check("1 光标就位可直接续写（聚焦输入框，光标停在省略号前）",
              caret == len(seed or '') - 1 and caret > 0, f"caret={caret} seed={seed!r}")

        # ---- 2 Esc 收起；再按一次收起/展开 ----
        print("== 2 Esc 收起；再按一次收起/展开 ==")
        evaljs(KEY_ESC)
        ok = wait_js("document.querySelectorAll('.cf-panel').length", 0, 10, "Esc 收起")
        check("2 Esc 收起浮窗", ok)
        evaljs(KEY_CTRL_K)
        ok = wait_js("document.querySelectorAll('.cf-panel').length", 1, 10, "再次唤起")
        check("2 再按一次展开", ok)
        evaljs(KEY_CTRL_K)
        ok = wait_js("document.querySelectorAll('.cf-panel').length", 0, 10, "再按收起")
        check("2 再按一次收起", ok)

        # ---- 3 el-select 场景不误伤（target 守卫） ----
        print("== 3 el-select 等组件内部按键不误伤 ==")
        hit = evaljs(KEY_K_IN_SELECT)
        check("3 el-select 内 Ctrl+K 不唤起浮窗（守卫放行）", hit is False, f"hit={hit}")
        evaljs(KEY_CTRL_K)
        ok = wait_js("document.querySelectorAll('.cf-panel').length", 1, 10, "普通目标唤起")
        check("3 普通位置 Ctrl+K 仍唤起（守卫不误伤全局键）", ok)
        evaljs(KEY_ESC)
        wait_js("document.querySelectorAll('.cf-panel').length", 0, 10, "复位收起")

        # ---- 4 /agent：默认不双入口，⌘K 显式唤起 ----
        print("== 4 /agent 工作台：默认无浮窗球，Ctrl+K 显式唤起 ==")
        cdp_send("Page.navigate", {"url": f"{SITE}/agent"})
        ok = wait_js("document.querySelectorAll('.agent-page').length", 1, 40, "工作台")
        check("4 /agent 渲染工作台", ok)
        check("4 /agent 默认不显示浮窗球（不双入口）",
              evaljs("document.querySelectorAll('.copilot-float').length") == 0)
        evaljs(KEY_CTRL_K)
        ok = wait_js("document.querySelectorAll('.cf-panel').length", 1, 15, "工作台唤起")
        check("4 /agent 也可靠 Ctrl+K 唤起浮窗（任意页）", ok)
        evaljs(KEY_ESC)
        ok = wait_js("document.querySelectorAll('.copilot-float').length", 0, 10, "收起后隐藏")
        check("4 收起后 /agent 恢复无浮窗（不双入口）", ok)

        # ---- 5 会话面板：默认关 / 悬浮抽屉不挤压 / 偏好记忆 / 清偏好回默认 ----
        print("== 5 会话面板默认关 + 悬浮抽屉 + 偏好记忆 ==")
        check("5 无偏好记录时面板默认关（drawer 不可见）",
              evaljs(DRAWER_STATE) == 'closed' and evaljs(PANEL_PREF) is None,
              f"state={evaljs(DRAWER_STATE)} pref={evaljs(PANEL_PREF)!r}")
        w0 = evaljs("document.querySelector('.center').getBoundingClientRect().width")
        evaljs("""(() => {
          const btn = [...document.querySelectorAll('.center-head button')]
            .find(b => b.textContent.includes('会话面板'));
          if (btn) btn.click();
          return !!btn;
        })()""")
        ok = wait_js(DRAWER_STATE, "open", 15, "抽屉展开")
        w1 = evaljs("document.querySelector('.center').getBoundingClientRect().width")
        check("5 点按钮展开会话面板（悬浮抽屉）", ok)
        check("5 悬浮抽屉不挤压消息流宽度（.center 宽度不变）",
              abs((w0 or 0) - (w1 or 0)) < 1, f"w0={w0} w1={w1}")
        check("5 展开偏好记忆 localStorage（panel_open='1'）",
              evaljs(PANEL_PREF) == '1', f"pref={evaljs(PANEL_PREF)!r}")

        print("== 5b 刷新后偏好记忆（仍开）；清偏好刷新回默认（关） ==")
        cdp_send("Page.reload")
        ok = wait_js(DRAWER_STATE, "open", 40, "刷新后抽屉仍开")
        check("5b 刷新后按偏好记忆恢复（面板仍开）",
              ok and evaljs(PANEL_PREF) == '1', f"state={evaljs(DRAWER_STATE)} pref={evaljs(PANEL_PREF)!r}")
        evaljs(f"localStorage.removeItem('{PANEL_KEY}'); true")
        cdp_send("Page.reload")
        ok = wait_js("document.querySelectorAll('.agent-page').length", 1, 40, "刷新回工作台")
        time.sleep(1.0)   # 等 drawer 判定渲染状态
        check("5b 清偏好后刷新回默认口径（面板关）",
              ok and evaljs(DRAWER_STATE) == 'closed' and evaljs(PANEL_PREF) is None,
              f"state={evaljs(DRAWER_STATE)} pref={evaljs(PANEL_PREF)!r}")

        check("页面无未捕获异常", not page_errors, str(page_errors[:3]))
    finally:
        try:
            if ws is not None:
                ws.close()
        except Exception:  # noqa: BLE001
            pass
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                proc.kill()

    print(f"\n== 结果: {len(PASS)} 通过 / {len(FAIL)} 失败 ==")
    if FAIL:
        print("失败项: " + " | ".join(FAIL))
        raise SystemExit(1)
    raise SystemExit(0)


if __name__ == "__main__":
    main()
