#!/usr/bin/env python3
"""U1 跨入口首次接入隔离浏览器验收。

真实 Chrome + CDP + 页面级 API mock；不启动/重启后端，不访问真实云，不发送通知。
覆盖：同 Run 恢复、晚到响应、断网保留、完整失败清单、viewer、受保护表单、
连续创建防重、真实证据深链、无模型智能体与工作流同 Run 去重。
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chrome_run import chrome_popen  # noqa: E402

SITE = os.environ.get("QIANSHU_SITE", "http://127.0.0.1:5273")
CDP_PORT = 9349
PROFILE = "/tmp/chrome-first-use-cdp"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


MOCK_JS = r"""
(() => {
  localStorage.setItem('qianshu.token', 'mock-token');
  localStorage.setItem('qianshu.account', '0');
  window.__mockCalls = [];
  window.__role = localStorage.getItem('qianshu.mock.role') || 'admin';
  window.__delayA = true;
  window.__failTasks = false;
  window.__workflowMode = localStorage.getItem('qianshu.mock.workflow') || 'existing';
  window.__workflowSeq = 0;
  window.__nextAccountId = 3;
  window.__loseCreateResponse = false;
  const capability = (name, execution, dataState, reason, extra) => Object.assign({
    capability: name, execution: execution, data_state: dataState, reason_code: reason || '',
    next_action: '', retryable: false, scope: {}, evidence_refs: [],
    checked_at: null, observed_until: null,
  }, extra || {});
  window.__capability = capability;
  window.__runs = {
    1: {
      id: 101, account_id: 1, status: 'attention', stage: 'done', attempts: 2,
      account: { id: 1, name: '账号 A', vendor: 'aliyun', status: 'attention', enabled: true },
      capabilities: [
        capability('assets', 'succeeded', 'available', '', {
          scope: { region: 'cn-hangzhou', resource_ids: ['i-a'] }, evidence_refs: ['assets:inventory:i-a'],
          checked_at: '2026-11-17T09:30:00', observed_until: '2026-11-17T09:29:45',
        }),
        capability('metrics', 'failed', 'unknown', 'credential_unavailable', {
          retryable: true, scope: { region: 'cn-hangzhou' }, evidence_refs: ['metrics:failed:a'],
          checked_at: '2026-11-17T09:30:02',
        }),
        capability('logs', 'waiting', 'unknown', 'confirmation_required', {
          next_action: 'confirm_logs', scope: { source_id: 'project-a/store-a' },
        }),
        capability('cost', 'failed', 'unknown', 'permission_denied', {
          retryable: true, scope: { bill_date: '2026-11-17' }, evidence_refs: ['cost:denied:a'],
          checked_at: '2026-11-17T09:30:04',
        }),
        capability('changes', 'failed', 'unknown', 'collection_failed', {
          retryable: true, scope: { source_id: 'actiontrail' }, evidence_refs: ['changes:failed:a'],
          checked_at: '2026-11-17T09:30:05',
        }),
      ],
      issues: [
        { key: 'a1', label: '指标凭证失败', detail: 'mock-a1' },
        { key: 'a2', label: '日志需要确认', detail: 'mock-a2' },
        { key: 'a3', label: '账单权限不足', detail: 'mock-a3' },
        { key: 'a4', label: '变更读取失败', detail: 'mock-a4' },
      ],
      items: [], counts: {}, created_at: '2026-11-17T09:00:00', updated_at: '2026-11-17T09:30:05', finished_at: null,
    },
    2: {
      id: 202, account_id: 2, status: 'running', stage: 'preparing', attempts: 1,
      account: { id: 2, name: '账号 B', vendor: 'tencent', status: 'probing', enabled: true },
      capabilities: [
        capability('assets', 'succeeded', 'available', '', {
          scope: { region: 'ap-guangzhou' }, evidence_refs: ['assets:inventory:b'],
          checked_at: '2026-11-17T10:00:00', observed_until: '2026-11-17T09:59:50',
        }),
        capability('metrics', 'running', 'unknown', ''),
        capability('logs', 'waiting', 'unknown', ''),
        capability('cost', 'failed', 'unknown', 'permission_denied', { retryable: true }),
        capability('changes', 'waiting', 'unknown', ''),
      ],
      issues: [{ key: 'b1', label: '账单权限不足', detail: 'mock-b1' }], items: [], counts: {},
      created_at: '2026-11-17T10:00:00', updated_at: '2026-11-17T10:00:00', finished_at: null,
    },
  };
  window.__accounts = [
    { id: 1, name: '账号 A', vendor: 'aliyun', vendor_label: '阿里云', region: 'cn-hangzhou', enabled: true,
      status: 'attention', onboarding_status: 'attention', onboarding_run_id: 101, ak_masked: 'AK****A',
      priority: 'high', notify_overview: true, notify_inapp: true, notify_push: false, repeat_days: null },
    { id: 2, name: '账号 B', vendor: 'tencent', vendor_label: '腾讯云', region: 'ap-guangzhou', enabled: true,
      status: 'probing', onboarding_status: 'running', onboarding_run_id: 202, ak_masked: 'AK****B',
      priority: 'high', notify_overview: true, notify_inapp: true, notify_push: false, repeat_days: null },
  ];
  try {
    const extra = JSON.parse(localStorage.getItem('qianshu.mock.extraRuns') || '{}');
    for (const [id, run] of Object.entries(extra)) {
      window.__runs[id] = run;
      if (run.account && !window.__accounts.some(x => x.id === run.account_id)) window.__accounts.push(run.account);
    }
  } catch (e) {}
  function emptyRun(account) {
    return {
      id: account.onboarding_run_id, account_id: account.id, status: 'running', stage: 'preparing', attempts: 1,
      account: { id: account.id, name: account.name, vendor: account.vendor, status: 'probing', enabled: true },
      capabilities: ['assets', 'metrics', 'logs', 'cost', 'changes'].map((x) => capability(x, 'running', 'unknown', '')),
      issues: [], items: [], counts: {}, created_at: '2026-11-17T10:10:00', updated_at: '2026-11-17T10:10:00', finished_at: null,
    };
  }
  function workflowResult() {
    if (window.__workflowMode === 'new') {
      return {
        run_id: 56, status: 'done', steps: [{ step: '受保护凭证表单', status: 'ok', ms: 1, detail: '已生成' }],
        artifact: {
          report_md: '## 新账号详细报告\n\n凭证只在受保护表单提交。',
          credential_form: { kind: 'cloud_credential_form', ack_required: true,
            route: { path: '/clouds', query: { new: '1', vendor: 'aliyun', name: 'agent-prod', region: 'cn-hangzhou',
                                               ak: 'ARTIFACT-AK-SECRET', sk: 'ARTIFACT-SK-SECRET' } } },
        }, tickets: [], change_items: [],
      };
    }
    return {
      run_id: 55 + window.__workflowSeq++, status: 'done', steps: [{ step: '接入任务状态', status: 'ok', ms: 1, detail: '同一 Run' }],
      artifact: { report_md: '## 既有账号详细报告\n\n这是默认应折叠的长报告。',
                  onboarding_run: window.__runs[1],
                  task_ref: { kind: 'onboarding_run', account_id: 1, run_id: 101 } },
      tickets: [], change_items: [],
    };
  }
  function apiResponse(method, path, body) {
    window.__mockCalls.push({ method, url: path, body });
    if (/\/bootstrap\/status$/.test(path)) return { initialized: true, has_admin: true, has_credential: true,
                                                     init_done: true, accounts_total: 2, accounts_enabled: 2 };
    if (/\/auth\/me$/.test(path)) return { id: 1, username: 'mock-user', role: window.__role,
                                           must_change_password: false, permission_codes: ['agent.change'] };
    if (/\/ai\/settings$/.test(path)) return [];
    if (/\/clouds\/accounts$/.test(path) && method === 'GET') return { items: window.__accounts, total: window.__accounts.length };
    if (/\/clouds\/accounts$/.test(path) && method === 'POST') {
      const id = window.__nextAccountId++;
      const account = { id, name: body.name, vendor: body.vendor, vendor_label: '阿里云', region: body.region || '',
        enabled: true, status: 'probing', onboarding_status: 'running', onboarding_run_id: id * 101,
        ak_masked: 'AK****C', priority: 'high', notify_overview: true, notify_inapp: true, notify_push: false, repeat_days: null };
      window.__accounts = window.__accounts.filter((x) => x.id !== id).concat([account]);
      window.__runs[id] = emptyRun(account);
      return account;
    }
    if (/\/clouds\/onboarding-runs$/.test(path)) return { items: [window.__runs[2], window.__runs[1]].filter(Boolean), total: 2 };
    const runMatch = path.match(/\/clouds\/accounts\/(\d+)\/onboarding$/);
    if (runMatch) return window.__runs[Number(runMatch[1])] || {};
    const retryMatch = path.match(/\/clouds\/accounts\/(\d+)\/onboarding\/retry$/);
    if (retryMatch && method === 'POST') {
      if (!Array.isArray(body?.scopes) || body.scopes.length > 20) return { detail: 'scope batch exceeds 20' };
      return { onboarding: window.__runs[Number(retryMatch[1])] };
    }
    if (/\/agent\/workflows$/.test(path)) return { items: [{ key: 'access_cloud', name: '接入云账号', domain: '接入域', params_schema: {} }] };
    if (/\/agent\/workflows\/access_cloud\/run$/.test(path) && method === 'POST') return workflowResult();
    if (/\/agent\/system-snapshot$/.test(path)) return {};
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
      this.upload = { addEventListener() {}, removeEventListener() {} }; this._listeners = {};
    }
    open(method, url) { this._method = method; this._url = url; this.readyState = 1; }
    setRequestHeader() {} abort() {} overrideMimeType() {}
    addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); }
    removeEventListener(t, fn) { this._listeners[t] = (this._listeners[t] || []).filter((f) => f !== fn); }
    getAllResponseHeaders() { return 'content-type: application/json\r\n'; }
    getResponseHeader(k) { return String(k).toLowerCase() === 'content-type' ? 'application/json' : null; }
    send(bodyRaw) {
      const path = String(this._url || '').split('?')[0];
      const isTask = /\/clouds\/accounts\/\d+\/onboarding$/.test(path);
      const delay = /\/clouds\/accounts\/1\/onboarding$/.test(path) && window.__delayA ? 900
        : (/\/clouds\/accounts$/.test(path) && String(this._method).toUpperCase() === 'POST' ? 400 : 0);
      setTimeout(() => {
        const loseCreate = window.__loseCreateResponse && /\/clouds\/accounts$/.test(path)
          && String(this._method).toUpperCase() === 'POST';
        if (loseCreate) window.__loseCreateResponse = false;
        let payload = window.__failTasks && isTask ? { detail: 'mock network unavailable' } : handle(this._method, this._url, bodyRaw);
        if (loseCreate) payload = { detail: 'mock response lost after commit' };
        this.status = (window.__failTasks && isTask) || loseCreate ? 503 : 200;
        this.statusText = this.status === 200 ? 'OK' : 'Unavailable'; this.readyState = 4;
        this.responseText = JSON.stringify(payload); this.response = this.responseText;
        if (this.onreadystatechange) this.onreadystatechange();
        if (this.onload) this.onload(); if (this.onloadend) this.onloadend();
        (this._listeners.load || []).forEach((f) => f({ target: this }));
        (this._listeners.loadend || []).forEach((f) => f({ target: this }));
      }, delay);
    }
  }
  window.XMLHttpRequest = FakeXHR;
  window.fetch = (input, init) => {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const method = (init && init.method) || 'GET';
    const payload = handle(method, url, (init && init.body) || null);
    return Promise.resolve(new Response(JSON.stringify(payload), { status: 200, headers: { 'content-type': 'application/json' } }));
  };
})();
"""

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
            page_errors.append((msg["params"].get("exceptionDetails", {}).get("exception", {}).get("description") or "")[:240])
    return {}


def evaljs(expr):
    result = cdp_send("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
    return (result.get("result") or {}).get("value")


def wait_js(expr, want=True, timeout=20, label=""):
    started = time.time()
    while time.time() - started < timeout:
        if evaljs(expr) == want:
            return True
        time.sleep(0.15)
    print(f"    (等待超时: {label} -> {evaljs(expr)!r})")
    return False


def click_js(expr):
    return evaljs(f"(() => {{ const el = {expr}; if (!el) return false; el.click(); return true; }})()")


def set_input_js(expr, value):
    return evaljs(f"""(() => {{
      const inp = {expr}; if (!inp) return false;
      Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(inp, {json.dumps(value)});
      inp.dispatchEvent(new Event('input', {{ bubbles: true }})); return true;
    }})()""")


def navigate(path):
    cdp_send("Page.navigate", {"url": f"{SITE}{path}"})
    time.sleep(0.35)


def task_run_id():
    return evaljs("(document.querySelector('.task-result') || {}).dataset ? document.querySelector('.task-result').dataset.runId : ''")


def main():
    global proc, ws
    try:
        urllib.request.urlopen(f"{SITE}/", timeout=5)
    except Exception as exc:
        print(f"[FAIL] 前端不可达（{SITE}）：{exc}")
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
                targets = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=3).read())
                break
            except Exception:
                time.sleep(1)
        page = next(t for t in targets if t["type"] == "page")
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=60)
        cdp_send("Page.enable")
        cdp_send("Runtime.enable")
        cdp_send("Page.addScriptToEvaluateOnNewDocument", {"source": MOCK_JS})

        print("== 1 云账号页：晚到响应、断网与刷新恢复 ==")
        navigate("/clouds?account_id=1&run_id=101")
        check("1 两账号表格加载", wait_js("document.querySelectorAll('.el-table__body tbody tr').length", 2, 20, "账号表格"))
        click_js("""[...document.querySelectorAll('.el-table__body tbody tr')].find(r => (r.innerText || '').includes('账号 B'))
          ?.querySelector('button')""")
        check("1 快速切到账号 B", wait_js(
            "(document.querySelector('.task-result') || {}).dataset?.runId", '202', 20, "Run 202"))
        time.sleep(1.3)  # 等账号 A 的 900ms 晚到响应返回
        check("1 账号 A 晚到响应不覆盖 B", task_run_id() == '202', str(task_run_id()))
        evaljs("window.__failTasks = true")
        check("1 断网显示可恢复错误", wait_js("document.querySelectorAll('.task-error').length", 1, 8, "任务断网"))
        check("1 断网保留 Run 与账号列表", task_run_id() == '202'
              and evaljs("document.querySelectorAll('.el-table__body tbody tr').length") == 2)
        evaljs("window.__failTasks = false")
        check("1 网络恢复继续同一 Run", wait_js("document.querySelectorAll('.task-error').length", 0, 8, "错误消失")
              and task_run_id() == '202')
        navigate("/clouds?account_id=2&run_id=202")
        check("1 刷新恢复 Run 202", wait_js("(document.querySelector('.task-result') || {}).dataset?.runId", '202', 20, "刷新 Run 202"))

        print("== 2 共享卡：完整失败清单与真实证据深链 ==")
        navigate("/clouds?account_id=1&run_id=101")
        check("2 恢复账号 A", wait_js("(document.querySelector('.task-result') || {}).dataset?.runId", '101', 20, "Run 101"))
        check("2 主卡最多三个数字、三个阻塞、一个主动作",
              evaljs("document.querySelectorAll('.task-numbers > div').length") == 3
              and evaljs("document.querySelectorAll('.task-blockers > div').length") == 3
              and evaljs("document.querySelectorAll('.task-primary button').length") == 1)
        click_js("document.querySelector('.task-evidence .el-collapse-item__header')")
        check("2 折叠区保留五能力和全部四个问题",
              wait_js("document.querySelectorAll('.task-capability').length", 5, 10, "五能力")
              and evaljs("document.querySelectorAll('.task-issues > div').length") == 4)
        asset_text = evaljs("document.querySelector('.task-capability').innerText") or ''
        check("2 成功且无 reason_code 不误报未验证", '执行与数据读取已验证' in asset_text and '尚未验证' not in asset_text, asset_text)
        # 让 B 全部成功，首个可用能力成为主动作；观测截止早于回读时间时窗口仍正序。
        evaljs("""(() => { const r = window.__runs[2]; r.status = 'ready';
          r.capabilities.forEach((c, i) => { c.execution = 'succeeded'; c.data_state = 'available'; c.reason_code = '';
            c.checked_at = '2026-11-17T10:00:0' + i; c.observed_until = '2026-11-17T09:59:5' + i;
            c.evidence_refs = ['evidence:' + c.capability + ':b']; }); r.updated_at = '2026-11-17T10:00:05'; })()""")
        click_js("""[...document.querySelectorAll('.el-table__body tbody tr')].find(r => (r.innerText || '').includes('账号 B'))
          ?.querySelector('button')""")
        check("2 首个成功能力成为主动作", wait_js("(document.querySelector('.task-primary button') || {}).innerText", '查看资产结果', 20, "查看资产"))
        click_js("document.querySelector('.task-primary button')")
        check("2 深链携带账号、Run、对象和证据，不用采集时间冒充观测起点", wait_js("location.pathname", '/ops', 10, "资产页")
              and evaljs("""(() => { const q = new URLSearchParams(location.search); return q.get('account_id') === '2' && q.get('run_id') === '202'
                && q.get('object_type') === 'capability' && q.get('object_id') === 'assets'
                && q.get('evidence') === 'evidence:assets:b' && q.get('from') === null
                && q.get('to') === '2026-11-17T09:59:50'; })()"""))

        print("== 3 viewer 与受保护新建表单 ==")
        evaljs("localStorage.setItem('qianshu.mock.role', 'viewer'); window.__delayA = false")
        navigate("/clouds?account_id=1&run_id=101")
        check("3 viewer 可读取同一 Run", wait_js("(document.querySelector('.task-result') || {}).dataset?.runId", '101', 20, "viewer Run"))
        check("3 viewer 无管理假按钮", evaljs("document.querySelectorAll('.task-primary button').length") == 0
              and '需要管理员执行' in (evaljs("document.querySelector('.task-primary').innerText") or ''))
        check("3 viewer 无账号编辑删除按钮",
              not evaljs("[...document.querySelectorAll('.el-table button')].some(b => ['编辑', '删除', '测试'].some(x => (b.innerText || '').includes(x)))"))
        navigate("/clouds?new=1&vendor=aliyun&name=viewer-forbidden&ak=VIEWER-AK-SECRET&sk=VIEWER-SK-SECRET")
        time.sleep(0.8)
        check("3 viewer 不能通过深链打开新建表单且秘密立即离开 URL", not evaljs("!!document.querySelector('#cloud-account-ak')")
              and 'VIEWER-AK-SECRET' not in (evaljs("location.href") or '')
              and 'VIEWER-SK-SECRET' not in (evaljs("location.href") or ''))
        evaljs("localStorage.setItem('qianshu.mock.role', 'admin')")
        navigate("/clouds?account_id=1&run_id=101&edit=credential&ak=EDIT-AK-SECRET&sk=EDIT-SK-SECRET")
        check("3 编辑深链也立即清理秘密参数", wait_js("document.querySelectorAll('#cloud-account-ak').length", 1, 20, "编辑表单")
              and 'EDIT-AK-SECRET' not in (evaljs("location.href") or '')
              and 'EDIT-SK-SECRET' not in (evaljs("location.href") or ''))
        navigate("/clouds?new=1&vendor=aliyun&name=agent-prod&region=cn-hangzhou&ak=URLAKSECRET&sk=URLSKSECRET")
        check("3 智能体深链打开受保护表单", wait_js("document.querySelectorAll('#cloud-account-ak').length", 1, 20, "新建表单"))
        check("3 仅预填非秘密字段", evaljs("document.querySelector('#cloud-account-name').value") == 'agent-prod'
              and evaljs("document.querySelector('#cloud-account-region').value") == 'cn-hangzhou'
              and evaljs("document.querySelector('#cloud-account-ak').value") == ''
              and evaljs("document.querySelector('#cloud-account-sk').value") == '')
        check("3 URL 中伪造秘密被移除且不持久化", wait_js("!location.search.includes('URLAKSECRET') && !location.search.includes('URLSKSECRET')", True, 10, "清理秘密 URL")
              and 'URLAKSECRET' not in (evaljs("JSON.stringify({local: {...localStorage}, session: {...sessionStorage}})") or ''))
        set_input_js("document.querySelector('#cloud-account-ak')", 'FORM-AK-MOCK')
        set_input_js("document.querySelector('#cloud-account-sk')", 'FORM-SK-MOCK')
        click_js("document.querySelector('.el-dialog__footer button')")
        time.sleep(0.4)
        click_js("document.querySelector('.toolbar .el-button--primary')")
        check("3 关闭再打开已清空明文", wait_js("document.querySelector('#cloud-account-ak')?.value", '', 10, "AK 清空")
              and evaljs("document.querySelector('#cloud-account-sk').value") == '')

        print("== 4 连续创建只受理一次 ==")
        set_input_js("document.querySelector('#cloud-account-name')", 'only-once')
        set_input_js("document.querySelector('#cloud-account-ak')", 'FORM-AK-MOCK')
        set_input_js("document.querySelector('#cloud-account-sk')", 'FORM-SK-MOCK')
        click_js("[...document.querySelectorAll('.el-dialog')].filter(x => x.getClientRects().length).at(-1)?.querySelector('.el-dialog__footer .el-button--primary')")
        check("4 保留安全确认", wait_js("[...document.querySelectorAll('.el-dialog')].filter(x => x.getClientRects().length).at(-1)?.querySelectorAll('input').length", 1, 10, "安全确认"))
        set_input_js("[...document.querySelectorAll('.el-dialog')].filter(x => x.getClientRects().length).at(-1)?.querySelector('input')", '我已知晓')
        wait_js("""(() => { const b = [...document.querySelectorAll('.el-dialog')].filter(x => x.getClientRects().length).at(-1)
          ?.querySelector('.el-dialog__footer .el-button--primary'); return !!b && !b.disabled; })()""", True, 5, "确认可用")
        evaljs("""(() => { const b = [...document.querySelectorAll('.el-dialog')].filter(x => x.getClientRects().length).at(-1)
          ?.querySelector('.el-dialog__footer .el-button--primary'); b.click(); b.click(); })()""")
        check("4 连续点击只创建一个账号", wait_js("window.__mockCalls.filter(c => c.method === 'POST' && /\\/clouds\\/accounts$/.test(c.url)).length", 1, 20, "一次 POST"))
        check("4 返回真实 Run 303", wait_js("(document.querySelector('.task-result') || {}).dataset?.runId", '303', 20, "Run 303"))

        print("== 4b 同 Run attempt 防晚到与创建响应丢失恢复 ==")
        evaljs("window.__runs[1].attempts = 3")
        click_js("""[...document.querySelectorAll('.el-table__body tbody tr')].find(r => (r.innerText || '').includes('账号 A'))?.querySelector('button')""")
        check("4b 当前卡进入第 3 次接入", wait_js("(document.querySelector('.task-id') || {}).innerText", '任务 #101 · 第 3 次接入', 20, "attempt 3"))
        evaljs("window.__runs[1].attempts = 2")
        click_js("""[...document.querySelectorAll('.el-table__body tbody tr')].find(r => (r.innerText || '').includes('账号 A'))?.querySelector('button')""")
        time.sleep(1.2)
        check("4b 同 Run 旧 attempt 不覆盖新结果", (evaljs("document.querySelector('.task-id').innerText") or '').endswith('第 3 次接入'))
        click_js("document.querySelector('.toolbar .el-button--primary')")
        wait_js("document.querySelectorAll('#cloud-account-ak').length", 1, 10, "恢复测试新建表单")
        set_input_js("document.querySelector('#cloud-account-name')", 'lost-response')
        set_input_js("document.querySelector('#cloud-account-ak')", 'FORM-AK-MOCK')
        set_input_js("document.querySelector('#cloud-account-sk')", 'FORM-SK-MOCK')
        click_js("[...document.querySelectorAll('.el-dialog')].filter(x => x.getClientRects().length).at(-1)?.querySelector('.el-dialog__footer .el-button--primary')")
        wait_js("[...document.querySelectorAll('.el-dialog')].filter(x => x.getClientRects().length).at(-1)?.querySelectorAll('input').length", 1, 10, "恢复测试安全确认")
        set_input_js("[...document.querySelectorAll('.el-dialog')].filter(x => x.getClientRects().length).at(-1)?.querySelector('input')", '我已知晓')
        evaljs("window.__loseCreateResponse = true")
        click_js("[...document.querySelectorAll('.el-dialog')].filter(x => x.getClientRects().length).at(-1)?.querySelector('.el-dialog__footer .el-button--primary')")
        check("4b 创建响应丢失后恢复同名账号和真实 Run", wait_js("(document.querySelector('.task-result') || {}).dataset?.runId", '404', 20, "恢复 Run 404"))
        check("4b 恢复过程不重复创建", evaljs("window.__mockCalls.filter(c => c.method === 'POST' && /\\/clouds\\/accounts$/.test(c.url) && c.body?.name === 'lost-response').length") == 1)

        print("== 4c Run 级凭证失败与 31 范围分批重试 ==")
        evaljs("""(() => {
          const account = { id: 6, name: '凭证级失败', vendor: 'aliyun', vendor_label: '阿里云', region: '', enabled: true,
            status: 'error', onboarding_status: 'failed', onboarding_run_id: 606, priority: 'high' };
          window.__accounts.push(account); window.__runs[6] = { id: 606, account_id: 6, status: 'failed', attempts: 1,
            account, capabilities: ['assets', 'metrics', 'logs', 'cost', 'changes'].map(x => window.__capability(x, 'waiting', 'unknown', '')),
            issues: [{ key: 'credential', kind: 'credential', label: '凭证与连通性', status: 'failed', detail: 'AccessKey 无效' }],
            items: [], counts: {}, updated_at: '2026-11-17T11:00:00' };
          localStorage.setItem('qianshu.mock.extraRuns', JSON.stringify({ 6: window.__runs[6] }));
        })()""")
        navigate("/clouds?account_id=6&run_id=606")
        check("4c 跨刷新恢复凭证失败夹具", wait_js("window.__accounts.some(x => x.id === 6) && !!window.__runs[6]", True, 10, "账号6夹具"),
              str(evaljs("JSON.stringify({ accounts: window.__accounts.map(x => x.id), extra: localStorage.getItem('qianshu.mock.extraRuns'), href: location.href })")))
        check("4c Run 级凭证失败不显示为准备中", wait_js("(document.querySelector('.task-result') || {}).dataset?.runId", '606', 20, "Run 606")
              and '1 项能力需要处理' in (evaljs("document.querySelector('.task-conclusion').innerText") or '')
              and '自动验证和准备' not in (evaljs("document.querySelector('.task-conclusion').innerText") or ''))
        check("4c Run 级凭证失败给修改凭证主动作", (evaljs("document.querySelector('.task-primary button')?.innerText") or '') == '修改凭证并继续'
              and 'AccessKey 无效' in (evaljs("document.querySelector('.task-blockers').innerText") or ''))
        evaljs("""(() => {
          const account = { id: 7, name: '大批量重试', vendor: 'aliyun', vendor_label: '阿里云', region: '', enabled: true,
            status: 'attention', onboarding_status: 'attention', onboarding_run_id: 707, priority: 'high' };
          const days = Array.from({ length: 31 }, (_, i) => window.__capability('cost', 'failed', 'unknown', 'collection_failed', {
            retryable: true, scope: { bill_date: `2026-10-${String(i + 1).padStart(2, '0')}` } }));
          window.__accounts.push(account); window.__runs[7] = { id: 707, account_id: 7, status: 'attention', attempts: 1,
            account, capabilities: days, issues: [], items: [], counts: {}, updated_at: '2026-11-17T11:00:00' };
          localStorage.setItem('qianshu.mock.extraRuns', JSON.stringify({ 6: window.__runs[6], 7: window.__runs[7] }));
        })()""")
        navigate("/clouds?account_id=7&run_id=707")
        check("4c 31 个失败范围显示一个重试主动作", wait_js("(document.querySelector('.task-primary button') || {}).innerText", '重试失败范围', 20, "批量重试"))
        click_js("document.querySelector('.task-primary button')")
        check("4c 重试按 20+11 分批且不触发 422", wait_js("window.__mockCalls.filter(c => c.method === 'POST' && /\\/onboarding\\/retry$/.test(c.url)).length", 2, 20, "两批重试")
              and evaljs("JSON.stringify(window.__mockCalls.filter(c => c.method === 'POST' && /\\/onboarding\\/retry$/.test(c.url)).map(c => c.body.scopes.length))") == '[20,11]')

        print("== 5 无模型智能体：同 Run 去重与结构化表单 ==")
        navigate("/agent?account_id=1&run_id=101")
        check("5 无模型仍恢复确定性任务卡", wait_js("(document.querySelector('.agent-onboarding .task-result') || {}).dataset?.runId", '101', 20, "Agent Run 101"))
        navigate("/agent?account_id=1&run_id=101&workflow=access_cloud")
        check("5 工作流参数框打开", wait_js("document.querySelectorAll('.el-dialog__footer .el-button--primary').length", 1, 20, "工作流运行"))
        click_js("document.querySelector('.el-dialog__footer .el-button--primary')")
        check("5 工作流与顶部同 Run 只显示一卡", wait_js("document.querySelectorAll('.wf-card').length", 1, 20, "工作流卡")
              and evaljs("document.querySelectorAll('.task-result').length") == 1)
        check("5 长报告默认折叠", '这是默认应折叠的长报告' not in (evaljs("document.body.innerText") or ''))
        navigate("/agent?account_id=2&run_id=202&workflow=access_cloud")
        wait_js("document.querySelectorAll('.el-dialog__footer .el-button--primary').length", 1, 20, "非顶部 Run 工作流")
        click_js("document.querySelector('.el-dialog__footer .el-button--primary')")
        wait_js("document.querySelectorAll('.wf-card').length", 1, 20, "第一张非顶部工作流卡")
        click_js("document.querySelector('.wf-btn')")
        wait_js("document.querySelectorAll('.wf-run-btn').length", 1, 10, "再次打开工作流面板")
        click_js("document.querySelector('.wf-run-btn')")
        wait_js("document.querySelectorAll('.el-dialog__footer .el-button--primary').length", 1, 10, "再次运行参数")
        click_js("document.querySelector('.el-dialog__footer .el-button--primary')")
        check("5 重复工作流引用同一 Run 只渲染一张实时卡", wait_js("document.querySelectorAll('.wf-card').length", 2, 20, "两份工作流报告")
              and evaljs("document.querySelectorAll('.task-result').length") == 2)
        evaljs("localStorage.setItem('qianshu.mock.workflow', 'new')")
        navigate("/agent?account_id=1&run_id=101&workflow=access_cloud")
        wait_js("document.querySelectorAll('.el-dialog__footer .el-button--primary').length", 1, 20, "新账号工作流")
        click_js("document.querySelector('.el-dialog__footer .el-button--primary')")
        check("5 新账号显示受保护表单主动作", wait_js("document.querySelectorAll('.wf-credential-action button').length", 1, 20, "表单动作"))
        click_js("document.querySelector('.wf-credential-action button')")
        check("5 表单跳转不转发产物中的伪造秘密", wait_js("document.querySelectorAll('#cloud-account-ak').length", 1, 20, "Agent 到表单")
              and 'ARTIFACT-AK-SECRET' not in (evaljs("location.href") or '')
              and 'ARTIFACT-SK-SECRET' not in (evaljs("location.href") or '')
              and evaljs("document.querySelector('#cloud-account-ak').value") == ''
              and evaljs("document.querySelector('#cloud-account-sk').value") == '')

        evaljs("localStorage.setItem('qianshu.mock.role', 'operator')")
        navigate("/agent?account_id=1&run_id=101&workflow=access_cloud")
        wait_js("document.querySelectorAll('.el-dialog__footer .el-button--primary').length", 1, 20, "operator 新账号工作流")
        click_js("document.querySelector('.el-dialog__footer .el-button--primary')")
        check("5 operator 可运行工作流但无管理员表单假按钮", wait_js("document.querySelectorAll('.wf-credential-action').length", 1, 20, "operator 表单提示")
              and evaljs("document.querySelectorAll('.wf-credential-action button').length") == 0
              and '需要管理员' in (evaljs("document.querySelector('.wf-credential-action').innerText") or ''))

        check("页面无未捕获异常", not page_errors, str(page_errors[:4]))
    finally:
        try:
            if ws is not None:
                ws.close()
        except Exception:
            pass
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
    print(f"\n== 结果: {len(PASS)} 通过 / {len(FAIL)} 失败 ==")
    if FAIL:
        print("失败项: " + " | ".join(FAIL))
        raise SystemExit(1)
    raise SystemExit(0)


if __name__ == "__main__":
    main()
