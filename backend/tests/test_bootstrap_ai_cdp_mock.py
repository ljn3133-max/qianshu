#!/usr/bin/env python3
"""安装向导精简主线与高级 AI 入口的隔离 CDP 走查。

运行：cd /opt/qianshu/backend && python3 tests/test_bootstrap_ai_cdp_mock.py
形态：真 Chrome（headless，降权 nobody 走 chrome_run）+ CDP（Runtime.evaluate 直查 DOM）
+ 注入 /api mock 层（XHR/fetch 全拦；SSE 发问走假流）——**零后端依赖**（不碰 8810），
页面走 vite dev（5273）拿真实 Bootstrap.vue 编译产物。

走查项（真点击逐项）：
  1 新手主线为欢迎、授权接入、其他环境、查看结果；手工测试和 AI 初始化只在高级折叠。
  2 厂商和凭证同页；AI 建议仍需点「采用」，不自动改表单。
  3 不执行手工连通测试也可提交；安全确认后两秒内出现真实 Run 受理身份。
  4 受理后同一 Run 卡消费五类能力证据，不把保存成功当作全部就绪，不持久化明文。
  5 高级 AI 初始化仍复用工作流，保留步骤、待确认工单和报告。
  6 页面全程零未捕获异常。
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
CDP_PORT = 9348   # 避开 browser_sweep（9333）与 card CDP（9347）
PROFILE = "/tmp/chrome-bootstrap-ai-cdp"

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


# ---------------- /api mock 层（注入页面，XHR/fetch 全拦 + 调用留痕） ----------------

MOCK_JS = r"""
(() => {
  localStorage.setItem('qianshu.token', 'mock-token');
  localStorage.setItem('qianshu.account', '0');
  window.__mockCalls = [];
  window.__accounts = [];
  window.__runs = {};
  window.__loseCreateVendor = '';
  function makeRun(account) {
    const capabilities = ['assets', 'metrics', 'logs', 'cost', 'changes'].map((capability) => ({
      capability: capability, execution: 'running', data_state: 'unknown',
      reason_code: '', next_action: '', retryable: false, scope: {}, evidence_refs: [],
    }));
    return {
      id: account.onboarding_run_id, account_id: account.id, status: 'running', stage: 'preparing',
      attempts: 1, capabilities: capabilities, issues: [], items: [], counts: {},
      account: { id: account.id, name: account.name, vendor: account.vendor, status: 'probing', enabled: true },
      created_at: '2026-11-17T08:00:00Z', updated_at: '2026-11-17T08:00:00Z', finished_at: null,
    };
  }
  function apiResponse(method, path, body) {
    window.__mockCalls.push({ method: method, url: path, body: body });
    if (/\/bootstrap\/status$/.test(path)) {
      return { initialized: true, has_admin: true, has_credential: true,
               init_done: false, accounts_total: 0 };
    }
    if (/\/bootstrap\/report$/.test(path)) {
      return { faces: [], capabilities: [], gaps: [], generated_at: '' };
    }
    if (/\/clouds\/accounts\/test$/.test(path)) {
      return { ok: true, message: '凭证有效，可见区域 3 个' };
    }
    if (/\/clouds\/accounts$/.test(path) && method === 'POST') {
      const id = body && body.vendor === 'tencent' ? 2 : 1;
      const account = { id: id, name: body.name, vendor: body.vendor, region: body.region || '',
                        enabled: true, status: 'probing', onboarding_run_id: 100 + id,
                        onboarding_status: 'running' };
      window.__accounts = window.__accounts.filter((x) => x.id !== id).concat([account]);
      window.__runs[id] = makeRun(account);
      return account;
    }
    if (/\/clouds\/accounts$/.test(path) && method === 'GET') {
      return { items: window.__accounts, total: window.__accounts.length };
    }
    if (/\/clouds\/onboarding-runs$/.test(path)) {
      return { items: Object.values(window.__runs), total: Object.keys(window.__runs).length };
    }
    const runMatch = path.match(/\/clouds\/accounts\/(\d+)\/onboarding$/);
    if (runMatch) return window.__runs[Number(runMatch[1])] || {};
    if (/\/agent\/workflows\/onboard_init\/run$/.test(path)) {
      return {
        run_id: 7, status: 'done',
        steps: [
          { step: '盘点已配置源', status: 'ok', ms: 12, detail: '云账号 1 个（prod-aliyun）；SLS 目标：未配置' },
          { step: '同步资产与拓扑', status: 'ok', ms: 830, detail: '云资产同步：1/1 个账号成功；链路草稿：跳过（SLS 目标未配置）；拓扑扫描：groups=3' },
          { step: '画像与全景对象', status: 'ok', ms: 410, detail: '行为画像首轮采样：画像 3 个 / 当日快照 2' },
          { step: '生成默认告警规则', status: 'ok', ms: 25, detail: '默认告警规则：按资产类型（ECS、RDS）生成 6 条待确认规则（2 张工单）' },
          { step: '验证数据可获取', status: 'ok', ms: 260, detail: '逐项探测 7 项：可取到 4 / 取不到 1 / 未配置跳过 2' },
        ],
        artifact: {
          report_md: '## 初始化报告 · AI 引导初始化\n\n### 一、生成了什么\n- 默认告警规则：6 条已建待确认工单（#41、#42）\n\n### 二、跳过了什么 / 为什么\n- 链路草稿：SLS 目标未配置（安装向导「补数据」配置后可生成）\n\n### 三、数据可获取验证（真实探测）\n| 数据源 | 结果 | 说明 |\n|---|---|---|\n| 云账号·prod-aliyun | 可取到 | 凭证有效 |\n\n### 四、下一步建议\n1. 到变更确认卡确认默认告警规则工单',
          highlights: ['默认告警规则：6 条已建待确认工单（#41、#42）——确认后才生效',
                       '数据可获取验证：可取到 4 项 / 取不到 1 项 / 未配置 2 项'],
        },
        tickets: [{ ticket_id: 41 }, { ticket_id: 42 }],
        content_md: '', answer: '', change_items: [],
      };
    }
    if (/\/agent\/workflows\/runs$/.test(path)) return { items: [], total: 0 };
    if (/\/agent\/sessions\/\d+\/messages$/.test(path)) return {};   // 走 SSE 分支（fetch 层拦）
    if (/\/agent\/sessions$/.test(path) && method === 'POST') {
      return { id: 55, title: '安装向导 · AI 建议', pinned: false, status: 'active',
               user_id: 1, message_count: 0 };
    }
    if (/\/auth\/me$/.test(path)) {
      return { id: 1, username: 'admin', role: 'admin', must_change_password: false,
               permission_codes: [] };
    }
    if (/\/messages\/inbox$/.test(path)) return { total: 0, unread: 0, items: [] };
    if (/\/ai\/settings$/.test(path)) return [{ id: 1, name: 'mock-model', enabled: true }];
    return { items: [], total: 0 };
  }
  function sseFor(bodyRaw) {
    // 假 copilot 流：按问题给不同建议（严格「字段=值 | 理由」约定，供解析/采用）。
    // SSE 块之间必须空行分隔（前端按 \n\n 切块解析）。
    let q = '';
    try { q = JSON.parse(bodyRaw || '{}').q || ''; } catch (e) { q = ''; }
    const lines = /第 3 步|step 3/i.test(q)
      ? ['event: token', 'data: {"token":"name=阿里云生产账号 | 按厂商+环境命名\\n"}', '',
         'event: token', 'data: {"token":"region=cn-hangzhou | 华东资源集中，起步推荐\\n"}', '',
         'event: done', 'data: {"message_id": 2}']
      : ['event: token', 'data: {"token":"vendors=aliyun,tencent | 混合云起步建议\\n"}', '',
         'event: done', 'data: {"message_id": 1}'];
    return lines.join('\n') + '\n';
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
      const path = String(this._url || '').split('?')[0];
      let payload = handle(this._method, this._url, bodyRaw);
      let loseCreate = false;
      if (window.__loseCreateVendor && /\/clouds\/accounts$/.test(path)
          && String(this._method).toUpperCase() === 'POST') {
        try {
          loseCreate = JSON.parse(bodyRaw || '{}').vendor === window.__loseCreateVendor;
        } catch (e) {}
        if (loseCreate) {
          window.__loseCreateVendor = '';
          payload = { detail: 'mock response lost after account commit' };
        }
      }
      this.status = loseCreate ? 503 : 200; this.statusText = loseCreate ? 'Unavailable' : 'OK'; this.readyState = 4;
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
    const bodyRaw = (init && init.body) || null;
    if (/\/agent\/sessions\/\d+\/messages$/.test(String(url).split('?')[0])) {
      // SSE 假流（copilot 会话接口轻量调用路径）
      return Promise.resolve(new Response(sseFor(bodyRaw),
        { status: 200, headers: { 'content-type': 'text/event-stream' } }));
    }
    const payload = handle(method, url, bodyRaw);
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


def click_js(expr):
    return evaljs(f"(() => {{ const el = {expr}; if (!el) return false; el.click(); return true; }})()")


def set_input_js(expr, value):
    return evaljs(f"""(() => {{
      const inp = {expr};
      if (!inp) return false;
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      setter.call(inp, {json.dumps(value)});
      inp.dispatchEvent(new Event('input', {{ bubbles: true }}));
      return true;
    }})()""")


def main():
    global proc, ws
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
        cdp_send("Page.navigate", {"url": f"{SITE}/bootstrap"})

        print("== 1 新手主线：四步，高级能力不阻塞 ==")
        ok = wait_js("document.querySelectorAll('.wiz-steps .el-step').length", 4, 40, "向导四步")
        check("1 四步向导渲染", ok)
        step_titles = evaljs("[...document.querySelectorAll('.wiz-steps .el-step__title')].map(e => e.textContent.trim())")
        check("1 主线为欢迎/授权/其他环境/结果",
              step_titles == ['欢迎', '授权接入', '其他环境（可选）', '查看结果'], str(step_titles))
        body_txt = evaljs("document.body.innerText || ''")
        check("1 AI 初始化保留为高级折叠而非主线",
              '高级：AI 初始化' in (body_txt or '') and 'AI 初始化（开箱可用）' not in (body_txt or ''), (body_txt or '')[-400:])

        print("== 2 授权页：厂商与凭证同页，AI 建议确认后采用 ==")
        click_js("""[...document.querySelectorAll('.wiz-actions button')]
          .find(b => (b.textContent || '').includes('下一步'))""")
        ok = wait_js("document.querySelectorAll('.wiz-check').length", 7, 20, "厂商多选")
        check("2 厂商和凭证合并一页", ok and wait_js("document.querySelectorAll('.wiz-vendor-card').length", 1, 10, "默认凭证卡"))
        check("2 手工连通测试默认折叠且不作为下一步条件",
              not evaljs("""[...document.querySelectorAll('.wiz-vendor-card')]
                .some(c => (c.innerText || '').includes('测试可用性'))"""))
        check("2 高级折叠区有明确上下间距", evaljs("""(() => {
          const style = getComputedStyle(document.querySelector('.wiz-body > .advanced-inline'));
          return style.marginTop === '8px' && style.marginBottom === '8px';
        })()"""))
        # 展开厂商建议高级折叠；AI 只出建议，点击采用才修改选择。
        click_js("document.querySelector('.wiz-body > .advanced-inline .el-collapse-item__header')")
        click_js("""[...document.querySelectorAll('.wiz-body > .advanced-inline button')]
          .find(b => (b.textContent || '').includes('让 AI 建议'))""")
        ok = wait_js("document.querySelectorAll('.wiz-body > .advanced-inline .ai-suggest-item').length", 1, 25, "厂商建议")
        check("2 高级 AI 建议出文本", ok)
        check("2 未采用前仍只有一个默认厂商",
              evaljs("document.querySelectorAll('.wiz-check.is-checked').length") == 1)
        click_js("""document.querySelector('.wiz-body > .advanced-inline .ai-suggest-item button')""")
        ok = wait_js("document.querySelectorAll('.wiz-check.is-checked').length", 2, 15, "采用两个厂商")
        check("2 点采用才增加厂商", ok and wait_js("document.querySelectorAll('.wiz-vendor-card').length", 2, 10, "两凭证卡"))
        click_js("document.querySelector('.wiz-vendor-card .credential-authorization .el-collapse-item__header')")
        check("2 无凭证短卡含五类最小只读权限和官方入口",
              wait_js("[...document.querySelectorAll('.wiz-vendor-card')].every(c => c.querySelectorAll('.credential-permissions li').length === 5)", True, 10, "每卡五类权限")
              and evaljs("document.querySelector('.wiz-vendor-card .credential-official').href") == 'https://ram.console.aliyun.com/permissions')
        request_text = evaljs("document.querySelector('.wiz-vendor-card .credential-request-text textarea').value") or ''
        check("2 可复制授权申请不包含凭证或外部写入指令",
              '只读子账号' in request_text and 'AK/SK' in request_text
              and 'AK-aliyun' not in request_text and '删除' not in request_text, request_text[:200])
        set_input_js("document.querySelector('#bootstrap-aliyun-ak')", 'AK-REMOVED-MOCK')
        set_input_js("document.querySelector('#bootstrap-aliyun-sk')", 'SK-REMOVED-MOCK')
        click_js("[...document.querySelectorAll('.wiz-check')].find(x => (x.innerText || '').includes('阿里云'))")
        wait_js("document.querySelectorAll('.wiz-vendor-card').length", 1, 10, "取消阿里云")
        click_js("[...document.querySelectorAll('.wiz-check')].find(x => (x.innerText || '').includes('阿里云'))")
        check("2 取消厂商后重选不恢复旧明文", wait_js("document.querySelectorAll('.wiz-vendor-card').length", 2, 10, "重选阿里云")
              and evaljs("document.querySelector('#bootstrap-aliyun-ak').value") == ''
              and evaljs("document.querySelector('#bootstrap-aliyun-sk').value") == '')
        set_input_js("document.querySelector('#bootstrap-aliyun-ak')", 'AK-PUBLIC-OFF-MOCK')
        set_input_js("document.querySelector('#bootstrap-aliyun-sk')", 'SK-PUBLIC-OFF-MOCK')
        click_js("[...document.querySelectorAll('.wiz-actions button')].find(b => (b.textContent || '').includes('上一步'))")
        click_js("document.querySelectorAll('.el-radio')[1]")
        click_js("document.querySelectorAll('.el-radio')[0]")
        click_js("[...document.querySelectorAll('.wiz-actions button')].find(b => (b.textContent || '').includes('下一步'))")
        check("2 关闭再开启公有云不恢复旧明文", wait_js("document.querySelectorAll('.wiz-vendor-card').length", 2, 10, "返回授权页")
              and evaljs("[...document.querySelectorAll('.wiz-vendor-card')].every(c => [...c.querySelectorAll('input[type=password]')].every(i => i.value === ''))"))

        print("== 3 无手工测试提交：安全确认后两秒内受理 ==")
        for vendor in ('aliyun', 'tencent'):
            check(f"3 {vendor} 名称/AK/SK 填入受保护表单",
                  set_input_js(f"document.querySelector('#bootstrap-{vendor}-name')", f'prod-{vendor}')
                  and set_input_js(f"document.querySelector('#bootstrap-{vendor}-ak')", f'AK-{vendor}-MOCK-ONLY')
                  and set_input_js(f"document.querySelector('#bootstrap-{vendor}-sk')", f'SK-{vendor}-MOCK-ONLY'))
        next_enabled = evaljs("""(() => { const b = [...document.querySelectorAll('.wiz-actions button')]
          .find(x => (x.textContent || '').includes('下一步')); return !!b && !b.disabled; })()""")
        check("3 未做连通测试也允许进入自动准备", bool(next_enabled))
        click_js("""[...document.querySelectorAll('.wiz-actions button')]
          .find(b => (b.textContent || '').includes('下一步'))""")
        time.sleep(0.3)
        click_js("""[...document.querySelectorAll('.wiz-actions button')]
          .find(b => (b.textContent || '').includes('下一步'))""")
        ok = wait_js("document.querySelectorAll('.wiz-summary tbody tr').length", 2, 20, "摘要两行")
        check("3 完成页只承诺自动验证，不冒充扫描完成", ok)
        click_js("""[...document.querySelectorAll('.wiz-actions button')]
          .find(b => (b.textContent || '').includes('保存并开始接入'))""")
        ok = wait_js("document.querySelectorAll('.el-message-box input').length", 1, 10, "安全确认")
        check("3 保留云端凭证安全确认", ok)
        set_input_js("document.querySelector('.el-message-box input')", '我已知晓')
        wait_js("""(() => { const b = [...document.querySelectorAll('.el-message-box__btns button')]
          .find(x => (x.textContent || '').includes('我已知晓')); return !!b && !b.disabled; })()""", True, 5, "确认可用")
        started = time.monotonic()
        evaljs("window.__loseCreateVendor = 'aliyun'")
        click_js("""[...document.querySelectorAll('.el-message-box__btns button')]
          .find(b => (b.textContent || '').includes('我已知晓'))""")
        accepted = False
        accepted_spacing = False
        while time.monotonic() - started < 4:
            accepted_alerts = evaljs("document.querySelectorAll('[data-accepted-run-id]').length") or 0
            if accepted_alerts:
                accepted_spacing = bool(evaljs("""[...document.querySelectorAll('[data-accepted-run-id]')]
                  .every(e => getComputedStyle(e).marginBottom === '10px')"""))
            run_ids = evaljs("[...document.querySelectorAll('.task-result')].map(e => Number(e.dataset.runId)).sort().join(',')")
            if accepted_alerts == 2 or run_ids == '101,102':
                accepted = True
                break
            time.sleep(0.05)
        elapsed = time.monotonic() - started
        check("3 两秒内显示两个真实 Run 受理身份", accepted and elapsed <= 2.0, f"elapsed={elapsed:.3f}s")
        check("3 连续受理提示与任务卡有明确间距", accepted and accepted_spacing)
        posts = json.loads(evaljs("JSON.stringify(window.__mockCalls.filter(c => c.method === 'POST' && /\\/clouds\\/accounts$/.test(c.url)))") or "[]")
        check("3 每账号只创建一次并返回 Run", len(posts) == 2
              and {p['body']['vendor'] for p in posts} == {'aliyun', 'tencent'}, str(len(posts)))
        check("3 已受理但响应丢失时恢复原 Run 而不重复建号", accepted and len(posts) == 2
              and evaljs("window.__loseCreateVendor") == '')
        check("3 未调用手工连通测试", not json.loads(evaljs(
            "JSON.stringify(window.__mockCalls.filter(c => /\\/clouds\\/accounts\\/test$/.test(c.url)))") or "[]"))
        storage = evaljs("JSON.stringify({local: {...localStorage}, session: {...sessionStorage}})") or ''
        check("3 明文不进入浏览器持久存储", 'AK-aliyun-MOCK-ONLY' not in storage and 'SK-tencent-MOCK-ONLY' not in storage)

        print("== 4 同一 Run 消费能力证据，不把保存成功当就绪 ==")
        ok = wait_js("document.querySelectorAll('.task-result').length", 2, 20, "两张 Run 卡")
        check("4 受理后恢复同一 Run 卡", ok and evaljs(
            "[...document.querySelectorAll('.task-result')].map(e => Number(e.dataset.runId)).sort().join(',')") == '101,102')
        check("4 每卡最多三个数字、五能力仍在准备",
              evaljs("[...document.querySelectorAll('.task-result')].every(e => e.querySelectorAll('.task-numbers > div').length === 3 && (e.innerText || '').includes('5'))"))
        check("4 不出现无条件全部就绪", '全部就绪' not in (evaljs("document.querySelector('.onboarding-card').innerText") or ''))

        print("== 5 高级 AI 初始化：原工作流和必要确认保留 ==")
        check("5 高级初始化与后续工具有明确间距",
              evaljs("getComputedStyle(document.querySelector('.advanced-tools')).marginBottom") == '14px')
        click_js("document.querySelector('.advanced-tools > .el-collapse-item > .el-collapse-item__header')")
        click_js("""[...document.querySelectorAll('.advanced-tools button')]
          .find(b => (b.textContent || '').includes('AI 初始化'))""")
        ok = wait_js("document.querySelectorAll('.advanced-tools .ai-steps .ai-step-row').length", 5, 25, "5 步流水")
        check("5 AI 初始化复用工作流并保留五步",
              ok and len(json.loads(evaljs("JSON.stringify(window.__mockCalls.filter(c => /onboard_init\\/run$/.test(c.url)))") or "[]")) == 1)
        body_txt = evaljs("document.querySelector('.advanced-tools').innerText || ''")
        check("5 报告与待确认工单提示保留",
              '初始化报告' in (body_txt or '') and '下一步建议' in (body_txt or '')
              and '待确认变更工单' in (body_txt or '') and '绝不静默写入' in (body_txt or ''), (body_txt or '')[-500:])
        check("5 highlights 保留", (evaljs("document.querySelectorAll('.advanced-tools .ai-highlight').length") or 0) >= 2)

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
