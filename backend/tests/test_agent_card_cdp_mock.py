#!/usr/bin/env python3
"""千枢智能体 · 变更卡「敏感操作二次确认」mock CDP 测试。

运行：cd /opt/qianshu/backend && python3 tests/test_agent_card_cdp_mock.py
形态：真 Chrome（headless，降权 nobody 走 chrome_run）+ CDP（Runtime.evaluate 直查 DOM）
+ Page.addScriptToEvaluateOnNewDocument 注入 /api mock 层（XHR/fetch 全拦，调用记
window.__mockCalls）——**零后端依赖**（不碰 8810/不启服务），页面走 vite dev（5273）拿真实
组件（AgentChangeCard.vue 编译产物热加载）。

验证项（五条断言）：
  1 敏感预览块渲染：含敏感项的卡渲染「你接下来要做的操作」（操作+影响面+参数摘要），
    参数只显示掩码值（password/AK 明文不回显）
  2 继续按钮逐项发 items=[idx]：敏感项点「继续」才执行，confirm body.items 仅该项
  3 跳过走服务端 skip 接口：点「跳过」发 /skip {idx} 不发 confirm、
    该项显示已跳过（不是 dismiss 整单）；点「恢复」发 /skip {restore:true} 回待确认（可恢复）
  4 混合卡（敏感+非敏感）批量只跑非敏感：「确认执行」body.items 不含敏感 idx
  5 ack_required 敏感项点「继续」前必须过「我已知晓」输入（没输不发 confirm；输后带 ack 发）
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "tests", "quality"))
from chrome_run import chrome_popen  # noqa: E402  统一降权启动 Chrome（tests/quality/chrome_run.py）

SITE = os.environ.get("QIANSHU_SITE", "http://127.0.0.1:5273")
CDP_PORT = 9347   # 避开 browser_sweep 的 9333，防并行测试互踩
PROFILE = "/tmp/chrome-card-cdp"

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


# ---------------- /api mock 层（注入页面，XHR/fetch 全拦 + 调用留痕） ----------------

MOCK_JS = r"""
(() => {
  localStorage.setItem('qianshu.token', 'mock-token');
  localStorage.setItem('qianshu.account', '0');
  const tickets = [
    { id: 101, session_id: 1, title: '混合变更：资产同步 + 密钥轮换', status: 'pending',
      created_at: '2026-11-05T10:00:00', confirmed_at: null, result: null,
      change_list: [
        { idx: 0, title: '同步云资产', impact: '资产表刷新', rollback: '重跑同步',
          ack_required: false, sensitive: false, params: { account_id: '1' }, status: '', result: '' },
        { idx: 1, title: '轮换访问密钥', impact: '云凭证', rollback: '恢复快照 v1',
          ack_required: false, sensitive: true,
          params: { access_key: 'FAKE_ALIYUN_AK_000045', password: 'mock-secret-abc' }, status: '', result: '' },
        { idx: 2, title: '调整告警阈值', impact: '告警规则', rollback: '恢复旧规则',
          ack_required: false, sensitive: false, params: { threshold: '80' }, status: '', result: '' },
      ] },
    { id: 102, session_id: 1, title: '接入云账号 tc-prod', status: 'pending',
      created_at: '2026-11-05T10:01:00', confirmed_at: null, result: null,
      change_list: [
        { idx: 0, title: '新增云账号 tc-prod', impact: '资产采集面', rollback: '删除该账号',
          ack_required: true, sensitive: true,
          params: { access_key: 'FAKE_ALIYUN_AK_0001111', secret_key: 'mock-sk-0022334455' }, status: '', result: '' },
      ] },
    { id: 103, session_id: 1, title: '清理旧日志索引', status: 'pending',
      created_at: '2026-11-05T10:02:00', confirmed_at: null, result: null,
      change_list: [
        { idx: 0, title: '删除旧日志索引', impact: '日志检索', rollback: '重建索引',
          ack_required: false, sensitive: true, params: { index_name: 'old-logs' }, status: '', result: '' },
      ] },
  ];
  window.__mockCalls = [];
  function apiResponse(method, path, body) {
    window.__mockCalls.push({ method: method, url: path, body: body });
    if (/\/agent\/changes\/\d+\/confirm$/.test(path)) {
      const tid = Number((path.match(/(\d+)/) || [])[1] || 0);
      const items = Array.isArray(body && body.items) ? body.items : [];
      const results = items.map((idx) => ({ idx: idx, status: 'done', result: 'mock-ok' }));
      // 模拟真实后端确认门：执行结果落工单项/工单状态（GET /agent/changes 回新状态——
      // 与 change_gate.execute_ticket 同口径：子集执行剩 pending、全成 done）
      const t = tickets.find((x) => x.id === tid);
      if (t) {
        for (const r of results) {
          const it = (t.change_list || []).find((x) => x.idx === r.idx);
          if (it) { it.status = r.status; it.result = r.result; }
        }
        const remaining = (t.change_list || []).filter(
          (x) => ['done', 'skipped', 'rolled_back'].indexOf(x.status) < 0);
        t.status = remaining.length ? 'pending' : 'done';
        t.confirmed_at = '2026-11-05T10:30:00';
      }
      return { ticket_id: tid, status: t ? t.status : 'done', results: results };
    }
    if (/\/agent\/changes\/\d+\/skip$/.test(path)) {
      // 跳过态落服务端（2026-11-05 #2）：与 change_gate.skip_item 同口径——
      // pending→skipped / restore 回 pending；全部项收口工单 done，恢复出未完项重开
      const tid = Number((path.match(/(\d+)/) || [])[1] || 0);
      const idx = body && body.idx;
      const t = tickets.find((x) => x.id === tid);
      let status = 'skipped';
      if (t) {
        const it = (t.change_list || []).find((x) => x.idx === idx);
        if (it) {
          if (body && body.restore) { it.status = 'pending'; status = 'pending'; }
          else { it.status = 'skipped'; status = 'skipped'; }
        }
        const remaining = (t.change_list || []).filter(
          (x) => ['done', 'skipped', 'rolled_back'].indexOf(x.status) < 0);
        if (!remaining.length) t.status = 'done';
        else if (t.status === 'done') t.status = 'pending';
      }
      return { ticket_id: tid, idx: idx, status: status,
               restored: !!(body && body.restore), ticket_status: t ? t.status : 'pending' };
    }
    if (/\/agent\/changes\/\d+\/(rollback|dismiss)$/.test(path)) return { ok: true };
    if (/\/bootstrap\/status$/.test(path)) return { init_done: true };
    if (/\/auth\/me$/.test(path)) {
      return { id: 1, username: 'admin', role: 'admin', must_change_password: false,
               permission_codes: ['agent.change', 'agent.query'] };
    }
    if (/\/ai\/settings$/.test(path)) return [{ id: 1, name: 'mock-model', enabled: true }];
    if (/\/agent\/changes$/.test(path)) return { items: tickets, total: tickets.length };
    if (/\/messages\/inbox$/.test(path)) return { total: 0, unread: 0, items: [] };
    return { items: [], total: 0 };
  }
  function handle(method, url, bodyRaw) {
    const path = String(url || '').split('?')[0];
    let body = null;
    try { body = bodyRaw ? JSON.parse(bodyRaw) : null; } catch (e) { body = null; }
    return apiResponse(String(method || 'GET').toUpperCase(), path, body);
  }
  // XHR 拦截（axios 浏览器适配层走 XHR）
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
  // fetch 拦截（SSE/兜底路径也走 mock，避免真发请求）
  window.fetch = (input, init) => {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const method = (init && init.method) || 'GET';
    const payload = handle(method, url, (init && init.body) || null);
    return Promise.resolve(new Response(JSON.stringify(payload),
      { status: 200, headers: { 'content-type': 'application/json' } }));
  };
})();
"""

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
    # 前端 vite dev（5273）提供真实组件；后端完全不依赖（/api 全 mock）
    try:
        urllib.request.urlopen(f"{SITE}/", timeout=5)
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] vite dev 不可达（{SITE}）：{e} —— 本测试需要前端 dev/静态服务在跑")
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
        # 变更卡只在“当前会话”内完整渲染：深链到 mock 工单所属会话（session_id=1），
        # 否则 /agent 是新会话状态，工单只出现在右栏跨会话精简列表里。
        cdp_send("Page.navigate", {"url": f"{SITE}/agent?session=1"})

        print("== 1 敏感预览块渲染（含掩码口径） ==")
        ok = wait_js("document.querySelectorAll('.chg-card').length", 3, 40, "3 张变更卡")
        check("1 变更卡渲染（混合/ack/跳过 3 张）", ok)
        n = evaljs("document.querySelectorAll('.sens-preview').length")
        titles = evaljs("[...document.querySelectorAll('.sens-preview .sens-title')].map(e=>e.textContent.trim())")
        check("1 敏感预览块渲染（3 个敏感项，标题=你接下来要做的操作）",
              n == 3 and all(t == '你接下来要做的操作' for t in (titles or [])),
              f"n={n} titles={titles}")
        txt = evaljs("document.querySelector('.chg-card').innerText || ''")
        check("1 参数摘要只显示掩码值（password/AK 明文不回显）",
              'FA***45' in (txt or '') and 'FAKE_ALIYUN_AK_000045' not in (txt or '')
              and 'mock-secret-abc' not in (txt or ''),
              (txt or '')[:200])

        print("== 4 混合卡批量只跑非敏感（先批量后逐项） ==")
        evaljs("""(() => {
          const card = [...document.querySelectorAll('.chg-card')]
            .find(c => (c.querySelector('.chg-title')?.textContent || '').includes('混合'));
          const btn = [...card.querySelectorAll('button')]
            .find(b => b.textContent.includes('确认执行'));
          btn.click();
          return true;
        })()""")
        time.sleep(1.0)
        calls = evaljs("JSON.stringify(window.__mockCalls.filter(c => /\\/confirm$/.test(c.url)))") or "[]"
        calls = json.loads(calls)
        mixed = [c for c in calls if c["url"].endswith("/changes/101/confirm")]
        check("4 混合卡批量确认只跑非敏感（items=[0,2]，不含敏感 1）",
              len(mixed) == 1 and (mixed[0]["body"] or {}).get("items") == [0, 2],
              str(mixed))

        print("== 2 继续按钮逐项发 items=[idx] ==")
        evaljs("""(() => {
          const card = [...document.querySelectorAll('.chg-card')]
            .find(c => (c.querySelector('.chg-title')?.textContent || '').includes('混合'));
          const btn = [...card.querySelectorAll('.sens-actions button')]
            .find(b => b.textContent.trim() === '继续');
          btn.click();
          return true;
        })()""")
        time.sleep(1.0)
        calls = json.loads(evaljs("JSON.stringify(window.__mockCalls.filter(c => /\\/confirm$/.test(c.url)))") or "[]")
        cont = [c for c in calls if c["url"].endswith("/changes/101/confirm")]
        check("2 敏感项「继续」逐项发 items=[1]",
              len(cont) == 2 and (cont[-1]["body"] or {}).get("items") == [1], str(cont))
        res = evaljs("""(() => {
          const card = [...document.querySelectorAll('.chg-card')]
            .find(c => (c.querySelector('.chg-title')?.textContent || '').includes('混合'));
          const item = [...card.querySelectorAll('.chg-item')]
            .find(x => (x.querySelector('.chg-item-title')?.textContent || '').includes('轮换'));
          return (item && item.innerText) || '';
        })()""")
        check("2 执行后该项就地显示结果（轮换访问密钥项）",
              '执行结果' in (res or '') and 'mock-ok' in (res or ''), (res or '')[:200])

        print("== 3 跳过走服务端 skip 接口（不执行不 dismiss；可恢复） ==")
        n_before = len(json.loads(evaljs("JSON.stringify(window.__mockCalls.filter(c => /\\/confirm$/.test(c.url)))") or "[]"))
        evaljs("""(() => {
          const card = [...document.querySelectorAll('.chg-card')]
            .find(c => (c.querySelector('.chg-title')?.textContent || '').includes('清理'));
          const btn = [...card.querySelectorAll('.sens-actions button')]
            .find(b => b.textContent.trim() === '跳过');
          btn.click();
          return true;
        })()""")
        time.sleep(0.8)
        calls = json.loads(evaljs("JSON.stringify(window.__mockCalls.filter(c => /\\/confirm$/.test(c.url)))") or "[]")
        skip_calls = json.loads(evaljs("JSON.stringify(window.__mockCalls.filter(c => /\\/skip$/.test(c.url)))") or "[]")
        skipped = evaljs("""(() => {
          const card = [...document.querySelectorAll('.chg-card')]
            .find(c => (c.querySelector('.chg-title')?.textContent || '').includes('清理'));
          return (card.innerText || '').includes('已跳过');
        })()""")
        check("3 跳过走 skip 接口（body {idx}）且不发 confirm、该项显示已跳过（不 dismiss 整单）",
              len(skip_calls) == 1 and skip_calls[0]["url"].endswith("/changes/103/skip")
              and (skip_calls[0]["body"] or {}).get("idx") == 0
              and len(calls) == n_before
              and not any(c["url"].endswith("/changes/103/confirm") for c in calls)
              and bool(skipped),
              f"skip={skip_calls} calls={len(calls)} skipped={skipped}")
        evaljs("""(() => {
          const card = [...document.querySelectorAll('.chg-card')]
            .find(c => (c.querySelector('.chg-title')?.textContent || '').includes('清理'));
          const btn = [...card.querySelectorAll('.sens-actions button')]
            .find(b => b.textContent.trim() === '恢复');
          btn.click();
          return true;
        })()""")
        time.sleep(0.8)
        skip_calls = json.loads(evaljs("JSON.stringify(window.__mockCalls.filter(c => /\\/skip$/.test(c.url)))") or "[]")
        restored = evaljs("""(() => {
          const card = [...document.querySelectorAll('.chg-card')]
            .find(c => (c.querySelector('.chg-title')?.textContent || '').includes('清理'));
          return (card.innerText || '').includes('待确认');
        })()""")
        check("3 恢复走 skip 接口（restore=true）且回到待确认（可恢复）",
              len(skip_calls) == 2 and (skip_calls[1]["body"] or {}).get("restore") is True
              and bool(restored), f"skip={skip_calls} restored={restored}")

        print("== 5 ack_required 敏感项：「继续」前先过「我已知晓」 ==")
        n_before = len(json.loads(evaljs("JSON.stringify(window.__mockCalls.filter(c => /\\/confirm$/.test(c.url)))") or "[]"))
        evaljs("""(() => {
          const card = [...document.querySelectorAll('.chg-card')]
            .find(c => (c.querySelector('.chg-title')?.textContent || '').includes('tc-prod'));
          const btn = [...card.querySelectorAll('.sens-actions button')]
            .find(b => b.textContent.trim() === '继续');
          btn.click();
          return true;
        })()""")
        time.sleep(0.8)
        calls = json.loads(evaljs("JSON.stringify(window.__mockCalls.filter(c => /\\/confirm$/.test(c.url)))") or "[]")
        check("5 没输「我已知晓」点继续不发 confirm",
              len(calls) == n_before and not any(c["url"].endswith("/changes/102/confirm") for c in calls),
              str(calls))
        evaljs("""(() => {
          const card = [...document.querySelectorAll('.chg-card')]
            .find(c => (c.querySelector('.chg-title')?.textContent || '').includes('tc-prod'));
          const inp = card.querySelector('.ack-input input');
          const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
          setter.call(inp, '我已知晓');
          inp.dispatchEvent(new Event('input', { bubbles: true }));
          return true;
        })()""")
        time.sleep(0.3)
        evaljs("""(() => {
          const card = [...document.querySelectorAll('.chg-card')]
            .find(c => (c.querySelector('.chg-title')?.textContent || '').includes('tc-prod'));
          const btn = [...card.querySelectorAll('.sens-actions button')]
            .find(b => b.textContent.trim() === '继续');
          btn.click();
          return true;
        })()""")
        time.sleep(1.0)
        calls = json.loads(evaljs("JSON.stringify(window.__mockCalls.filter(c => /\\/confirm$/.test(c.url)))") or "[]")
        ack_calls = [c for c in calls if c["url"].endswith("/changes/102/confirm")]
        check("5 输「我已知晓」后继续：items=[0] 且带 ack",
              len(ack_calls) == 1 and (ack_calls[0]["body"] or {}).get("items") == [0]
              and (ack_calls[0]["body"] or {}).get("ack") == "我已知晓", str(ack_calls))

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
