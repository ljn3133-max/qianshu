#!/usr/bin/env python3
"""COST-INSIGHTS C2 隔离 Chrome 验收；页面 API 全 mock，不访问真实云或模型。"""
import json
import os
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chrome_run import chrome_popen  # noqa: E402

SITE = os.environ.get("QIANSHU_SITE", "http://127.0.0.1:5273")
PORT = 9357
PROFILE = tempfile.mkdtemp(prefix="qianshu-cost-insights-chrome-")
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


MOCK_JS = r"""
(() => {
  try {
    localStorage.setItem('qianshu.token', 'mock-token');
    if (!localStorage.getItem('qianshu.account')) localStorage.setItem('qianshu.account', '1');
  } catch (e) { /* about:blank 无存储域；真实页面会重新执行本脚本。 */ }
  window.__mockCalls = [];
  window.__delay30 = false;
  window.__scopeMismatch = false;
  window.__failInsights = false;
  window.__failWorkflow = false;
  window.__modelUnavailable = false;
  window.__lastWorkflow = null;
  const paramsOf = (url) => new URL(String(url), location.origin).searchParams;
  const dates = (start, end) => {
    const out = []; const d = new Date(start + 'T00:00:00'); const last = new Date(end + 'T00:00:00');
    while (d <= last) {
      out.push(d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'));
      d.setDate(d.getDate() + 1);
    }
    return out;
  };
  const object = { id: 'ecs', product_code: 'ecs', product_name: '云服务器', instance_id: '', instance_name: '', billing_item: '' };
  const instanceObject = { id: 'i-1', product_code: 'ecs', product_name: '云服务器', instance_id: 'i-1', instance_name: '服务器一', billing_item: '' };
  function accountItem(aid, start, end, allDays) {
    const name = aid === 1 ? '生产账号' : aid === 2 ? '无账单账号' : '缺摊销账号';
    if (aid === 2) return {
      account: { id: 2, name, vendor: 'aliyun', enabled: true, billing_applicable: true },
      range: { start, end, days: allDays.length }, status: 'no_data', reason_code: 'no_billing_data',
      coverage: { requested_days: allDays.length, bill_complete_days: 0, amortized_complete_days: 0,
        complete_days: 0, observed_start: '', observed_end: '', missing_bill_dates: allDays,
        missing_amortized_dates: [], complete: false, total_known: false, collected_at: '' },
      totals: { payg: null, amortized: null, total: null, order: null, refund: null, complete: false },
      days: allDays.map(date => ({ date, has_data: false, payg: null, amortized: null, total: null,
        order: null, refund: null, bill_complete: false, amortized_complete: false, complete: false,
        collected_at: '', evidence_refs: [] })),
      products: [], instances: [], anomalies: [], observations: [], recommendations: [],
      analysis: { anomaly_status: 'not_fully_verified', recommendation_status: 'insufficient_optimization_evidence' },
    };
    const partial = aid === 3;
    const total = allDays.length * (partial ? 4 : 5);
    const payg = allDays.length * (partial ? 4 : 4);
    const amortized = partial ? null : allDays.length;
    const refs = allDays.map(date => `cost_daily:${aid}:${date}`);
    const finding = {
      id: `spike:${aid}:ecs|i-1|cpu`, account_id: aid, kind: 'payg_spike', date: end,
      object: { ...instanceObject, id: 'ecs|i-1|cpu', billing_item: 'cpu' },
      value: 100, baseline: 10, delta: 90, ratio: 10, mad: 1, level: 'warning', n_samples: 28,
      time_range: { from: end, to: end }, history_range: { from: allDays[0], to: end },
      source: 'cost_daily', evidence_refs: refs, notes: [], next_action: 'investigate_cost_growth',
    };
    const recommendation = {
      id: `investigate_cost_growth:${finding.id}`, account_id: aid, action: 'investigate_cost_growth',
      title: '排查云服务器费用增长', reason: '同账号周期基线显示费用增长，先核对用量与变更',
      object: finding.object, impact_amount: 90, estimated_savings: null, risk: 'read_only',
      evidence_sufficient: true, time_range: finding.time_range, history_range: finding.history_range,
      evidence_refs: refs,
    };
    return {
      account: { id: aid, name, vendor: 'aliyun', enabled: true, billing_applicable: true },
      range: { start, end, days: allDays.length }, status: partial ? 'partial' : 'available',
      reason_code: partial ? 'amortized_incomplete' : 'ready',
      coverage: { requested_days: allDays.length, bill_complete_days: allDays.length,
        amortized_complete_days: partial ? 0 : allDays.length, complete_days: partial ? 0 : allDays.length,
        observed_start: start, observed_end: end, missing_bill_dates: [],
        missing_amortized_dates: partial ? allDays : [], complete: !partial, total_known: !partial,
        collected_at: '2026-11-18T02:03:04' },
      totals: { payg, amortized, total, order: 500, refund: -50, complete: !partial },
      days: allDays.map(date => ({ date, has_data: true, payg: 4, amortized: partial ? null : 1,
        total: partial ? 4 : 5, order: 0, refund: 0, bill_complete: true,
        amortized_complete: !partial, complete: !partial, collected_at: '2026-11-18T02:03:04',
        evidence_refs: [`cost_daily:${aid}:${date}`] })),
      products: [{ object, payg, amortized, total, order: 500, refund: -50,
        share_pct: 100, evidence_refs: refs }],
      instances: [{ object: instanceObject, payg, amortized, total, order: 500, refund: -50,
        share_pct: 100, evidence_refs: refs }],
      anomalies: partial ? [] : [finding], observations: [], recommendations: partial ? [] : [recommendation],
      analysis: { anomaly_status: partial ? 'not_fully_verified' : 'anomalies_found',
        recommendation_status: partial ? 'insufficient_optimization_evidence' : 'available' },
    };
  }
  function zeroItem(start, end, allDays) {
    const item = accountItem(4, start, end, allDays);
    item.account.name = '零费用账号';
    item.totals = { payg: 0, amortized: 0, total: 0, order: 0, refund: 0, complete: true };
    item.days = item.days.map(x => ({ ...x, payg: 0, amortized: 0, total: 0, order: 0, refund: 0 }));
    item.products = []; item.instances = []; item.anomalies = [];
    item.recommendations = [{
      id: 'product-only', title: '核实按量产品费用', reason: '先查看该产品的原始账单',
      object: { product_code: 'ecs', product_name: '云服务器', instance_id: '' },
      evidence_refs: ['cost_daily:4:' + end], action: 'investigate_cost_growth',
      impact_amount: null, estimated_savings: null, risk: 'read_only', evidence_sufficient: true,
      time_range: { from: start, to: end }, history_range: { from: '', to: '' },
    }];
    item.analysis = { anomaly_status: 'no_anomaly_in_verified_scope', recommendation_status: 'available' };
    return item;
  }
  function insights(url) {
    const q = paramsOf(url); const requestedId = Number(q.get('account_id') || 0);
    const aid = window.__scopeMismatch ? 2 : requestedId;
    const start = q.get('start'); const end = q.get('end'); const allDays = dates(start, end);
    const accounts = aid ? [aid === 4 ? zeroItem(start, end, allDays) : accountItem(aid, start, end, allDays)]
      : [[1, 2, 3].map(id => accountItem(id, start, end, allDays)), zeroItem(start, end, allDays)].flat();
    const unattributed = aid ? [] : [accountItem(999, start, end, allDays)];
    unattributed.forEach(x => { x.account = { id: 999, name: '', vendor: '', enabled: false,
      billing_applicable: false, unattributed: true }; });
    return { requested: { account_id: aid, start, end }, accounts, unattributed,
      selected_account_ids: aid ? [aid] : [1, 2, 3, 4] };
  }
  function response(method, url, body) {
    const path = String(url).split('?')[0];
    if (/\/bootstrap\/status$/.test(path)) return { initialized: true, has_admin: true, has_credential: true,
      init_done: true, accounts_total: 3, accounts_enabled: 3 };
    if (/\/auth\/me$/.test(path)) return { id: 1, username: 'mock-admin', role: localStorage.getItem('mockRole') || 'admin', must_change_password: false };
    if (/\/ai\/settings$/.test(path)) return window.__modelUnavailable ? { detail: 'model timeout' } : [];
    if (/\/clouds\/accounts$/.test(path)) return { items: [1, 2, 3, 4].map(id => ({ id,
      name: id === 1 ? '生产账号' : id === 2 ? '无账单账号' : id === 3 ? '缺摊销账号' : '零费用账号', vendor: 'aliyun', enabled: true })), total: 4 };
    if (/\/cost\/insights$/.test(path)) return window.__failInsights ? { detail: 'mock insights unavailable' } : insights(url);
    if (/\/cost\/collect\/status$/.test(path)) return { last_finished_at: '2026-11-18T02:03:04' };
    if (/\/cost\/anomalies$/.test(path)) return { items: [], total: 0 };
    if (/\/cost\/packs\/impact$/.test(path)) return { items: [] };
    if (/\/cost\/packs$/.test(path)) return { items: [{ id: 1, name: 'CDN流量包', state: 'ok', remaining_pct: 80 }], exhausted_count: 0, snapshot_date: '2026-11-17' };
    if (/\/cost\/purchase-advice$/.test(path)) return { items: [{ title: '高级资源包补充', detail: '仅高级页保留', level: 'info' }], note: 'mock', snapshot_date: '2026-11-17' };
    if (/\/cost\/drilldown$/.test(path)) return { scenarios: [], instances: [], packs: [], total: 0 };
    if (/\/cost\/daily$/.test(path)) {
      const q = paramsOf(url);
      return { total: 1, items: [{ stat_date: q.get('end'), source: 'bill', product_code: q.get('product'),
        product_name: '云服务器', instance_id: q.get('instance_id'), instance_name: '服务器一',
        billing_item: 'cpu', subscription_type: 'PayAsYouGo', pretax_amount: 100, usage: 1,
        vendor: '阿里云', account_name: '生产账号' }] };
    }
    if (/\/agent\/sessions$/.test(path) && method === 'POST') {
      return { id: 81, title: JSON.parse(body || '{}').title || '成本洞察', message_count: 0 };
    }
    if (/\/agent\/workflows\/cost_checkup\/run$/.test(path) && method === 'POST') {
      if (window.__failWorkflow) return { detail: 'mock workflow unavailable' };
      const request = JSON.parse(body || '{}'); const p = request.params || {};
      const facts = insights('/api/cost/insights?' + new URLSearchParams(p).toString());
      window.__lastWorkflow = { run_id: 901, status: 'done', steps: [], tickets: [],
        artifact: { report_md: '## 成本洞察\n\n日成本 ¥150.00；同账号同窗口证据完整', highlights: [], insights: facts, answers: [] } };
      return window.__lastWorkflow;
    }
    if (/\/agent\/sessions\/81\/messages$/.test(path)) {
      const run = window.__lastWorkflow;
      return { items: run ? [{ id: 'wf-run-901', msg_kind: 'workflow_run', run_id: run.run_id,
        workflow_key: 'cost_checkup', workflow_name: '成本体检', status: run.status,
        artifact: run.artifact, created_at: '2026-11-18T02:03:04' }] : [], total: run ? 1 : 0 };
    }
    if (/\/agent\/chat/.test(path) && method === 'POST') return { detail: 'model timeout' };
    if (/\/agent\/workflows$/.test(path)) return { items: [] };
    if (/\/agent\/sessions/.test(path)) return { items: [], total: 0 };
    if (/\/agent\/system-snapshot$/.test(path)) return {};
    return { items: [], total: 0 };
  }
  class FakeXHR {
    constructor() { this.readyState = 0; this.status = 0; this.responseText = ''; this.response = '';
      this.upload = { addEventListener() {}, removeEventListener() {} }; this._listeners = {}; }
    open(method, url) { this._method = method; this._url = url; this.readyState = 1; }
    setRequestHeader() {} abort() {} overrideMimeType() {}
    addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); }
    removeEventListener(type, fn) { this._listeners[type] = (this._listeners[type] || []).filter(x => x !== fn); }
    getAllResponseHeaders() { return 'content-type: application/json\r\n'; }
    getResponseHeader(key) { return String(key).toLowerCase() === 'content-type' ? 'application/json' : null; }
    send(body) {
      const path = String(this._url).split('?')[0];
      const isInsights = /\/cost\/insights$/.test(path);
      const q = paramsOf(this._url); const days = isInsights ? dates(q.get('start'), q.get('end')).length : 0;
      const delay = isInsights && days === 30 && window.__delay30 ? 900 : 0;
      window.__mockCalls.push({ method: String(this._method).toUpperCase(), url: this._url, body });
      setTimeout(() => {
        const payload = response(this._method, this._url, body);
        this.status = (window.__failInsights && isInsights)
          || (window.__failWorkflow && /\/agent\/workflows\/cost_checkup\/run/.test(path))
          || (window.__modelUnavailable && /\/(ai\/settings|agent\/chat)/.test(path)) ? 503 : 200;
        this.statusText = this.status === 200 ? 'OK' : 'Unavailable'; this.readyState = 4;
        this.responseText = JSON.stringify(payload); this.response = this.responseText;
        if (this.onreadystatechange) this.onreadystatechange();
        if (this.onload) this.onload(); if (this.onloadend) this.onloadend();
        (this._listeners.load || []).forEach(fn => fn({ target: this }));
        (this._listeners.loadend || []).forEach(fn => fn({ target: this }));
      }, delay);
    }
  }
  window.XMLHttpRequest = FakeXHR;
  window.fetch = (input, init) => {
    const url = typeof input === 'string' ? input : input.url;
    const payload = response((init && init.method) || 'GET', url, (init && init.body) || null);
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
            detail = msg["params"].get("exceptionDetails", {})
            page_errors.append((detail.get("exception", {}).get("description") or detail.get("text") or "")[:500])
    return {}


def evaljs(expr):
    result = cdp_send("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
    if "exceptionDetails" in result:
        page_errors.append(str(result["exceptionDetails"])[:500])
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


def click_text(selector, text):
    found = evaljs(f"""(() => {{ const el = [...document.querySelectorAll({json.dumps(selector)})]
      .find(e => (e.innerText || '').trim().includes({json.dumps(text)}) && e.getClientRects().length);
      if (!el) return false; (el.querySelector('input') || el).click(); return true; }})()""")
    if not found:
        print(f"    (未找到可点击文本: {selector} / {text})")
    return found


def navigate(path):
    cdp_send("Page.navigate", {"url": f"{SITE}{path}"})
    time.sleep(0.4)


def main():
    global proc, ws
    try:
        urllib.request.urlopen(f"{SITE}/", timeout=5)
    except Exception as exc:
        print(f"[FAIL] 前端不可达（{SITE}）：{exc}")
        raise SystemExit(1)
    proc = chrome_popen(["google-chrome", "--headless=new", "--disable-gpu",
                         f"--remote-debugging-port={PORT}", "--remote-allow-origins=*",
                         f"--user-data-dir={PROFILE}", "about:blank"], PROFILE)
    try:
        import websocket
        targets = None
        for _ in range(40):
            try:
                targets = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=2).read())
                if targets:
                    break
            except Exception:
                pass
            time.sleep(0.25)
        page = next(x for x in targets if x["type"] == "page")
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=60)
        cdp_send("Page.enable")
        cdp_send("Runtime.enable")
        cdp_send("Page.addScriptToEvaluateOnNewDocument", {"source": MOCK_JS})

        print("== 1 首屏三个答案与唯一事实源 ==")
        navigate("/cost")
        check("1 默认30天首屏只有一个账号卡", wait_js("document.querySelectorAll('[data-insight-account]').length", 1, 20, "成本卡")
              and evaljs("document.querySelector('[data-insight-account]').dataset.insightAccount") == '1')
        check("1 三个答案先于图表且金额来自 C1", evaljs("document.querySelectorAll('[data-answer]').length") == 3
              and '¥150.00' in (evaljs("document.querySelector('[data-answer=spend]').innerText") or '')
              and '云服务器' in (evaljs("document.querySelector('[data-answer=spend]').innerText") or '')
              and '基线' in (evaljs("document.querySelector('[data-answer=anomaly]').innerText") or '')
              and '排查云服务器费用增长' in (evaljs("document.querySelector('[data-answer=action]').innerText") or ''))
        check("1 不再请求旧 summary/instances 双口径", not evaljs("window.__mockCalls.some(c => /\\/cost\\/(summary|instances)(\\?|$)/.test(c.url))"))
        click_js("document.querySelector('.task-evidence .el-collapse-item__header')")
        check("1 异常、建议和证据可展开", wait_js("document.querySelectorAll('.finding-row').length", 2, 10, "证据清单")
              and 'cost_daily:1:' in (evaljs("document.querySelector('.task-evidence').innerText") or ''))

        print("== 2 同账号原始明细与结构化智能体深链 ==")
        click_js("[...document.querySelectorAll('.insight-overview > .el-table')].at(-1)?.querySelector('tbody tr')")
        check("2 明细抽屉仍可打开", wait_js("[...document.querySelectorAll('.el-drawer')].some(x => x.getClientRects().length && x.innerText.includes('费用明细'))", True, 10, "明细抽屉"))
        check("2 明细显式带同账号、产品和实例", evaljs("""(() => {
          const calls = window.__mockCalls.filter(c => c.url.includes('/cost/daily')).map(c => c.url);
          return calls.some(url => { const q = new URL(url, location.origin).searchParams;
            return q.get('account_id') === '1' && q.get('product') === 'ecs' && q.get('instance_id') === 'i-1'; });
        })()"""), str(evaljs("window.__mockCalls.filter(c => c.url.includes('/cost/daily')).map(c => c.url)")))
        click_js("document.querySelector('.el-drawer__close-btn')")
        evaljs("window.__failWorkflow = true")
        click_js("document.querySelector('[data-insight-account] .task-primary button')")
        check("2 工作流失败保留成本卡并允许重试", wait_js("document.body.innerText.includes('mock workflow unavailable')", True, 10, "工作流失败提示")
              and evaljs("location.pathname") == '/cost'
              and evaljs("document.querySelectorAll('[data-insight-account]').length") == 1)
        evaljs("window.__failWorkflow = false")
        click_js("document.querySelector('[data-insight-account] .task-primary button')")
        check("2 智能体直接运行同账号同窗口工作流", wait_js("location.pathname", '/agent', 10, "智能体")
              and evaljs("""(() => {
                const call = window.__mockCalls.find(c => c.method === 'POST' && c.url.includes('/agent/workflows/cost_checkup/run'));
                if (!call) return false;
                const body = JSON.parse(call.body); const ctx = JSON.parse(new URLSearchParams(location.search).get('ctx') || '{}');
                return body.session_id === 81 && body.params.account_id === 1 && body.params.days === 30
                  && window.__mockCalls.filter(c => c.method === 'POST' && /\/agent\/sessions$/.test(c.url)).length === 1
                  && body.params.start === ctx.start && body.params.end === ctx.end
                  && ctx.kind === 'cost_insights' && ctx.status === 'available' && ctx.reason_code === 'ready'
                  && ctx.source === '/api/cost/insights' && ctx.evidence_refs.some(x => x.startsWith('cost_daily:1:'))
                  && !window.__mockCalls.some(c => c.method === 'POST' && c.url.includes('/agent/sessions/81/messages'));
              })()"""), str(evaljs("window.__mockCalls.filter(c => c.url.includes('/agent/')).map(c => ({method:c.method,url:c.url,body:c.body}))")))
        check("2 重开会话恢复工作流结果卡", wait_js("document.querySelector('.wf-row')?.innerText.includes('run #901')", True, 10, "持久化工作流卡"))
        click_js("document.querySelector('.wf-details .el-collapse-item__header')")
        check("2 展开后仍是同一确定性成本报告", wait_js("document.querySelector('.wf-report')?.innerText.includes('成本洞察')", True, 10, "持久化成本报告")
              and '¥150.00' in (evaljs("document.querySelector('.wf-report')?.innerText") or ''),
              str(evaljs("document.querySelector('.wf-row')?.innerText")))

        print("== 3 模型不可用不影响确定性事实 ==")
        evaljs("window.__modelUnavailable = true")
        navigate("/cost")
        check("3 模型超时仍有完整三答案", wait_js("document.querySelectorAll('[data-answer]').length", 3, 20, "无模型成本卡")
              and '¥150.00' in (evaljs("document.querySelector('[data-answer=spend]').innerText") or ''))
        evaljs("window.__modelUnavailable = false")

        print("== 4 无数据、部分覆盖、未归属分账号 ==")
        evaljs("localStorage.setItem('qianshu.account', '2')")
        navigate("/cost")
        check("4 无数据不显示零费用或健康", wait_js("document.querySelector('[data-status=no_data]') !== null", True, 20, "无数据卡")
              and '—' in (evaljs("document.querySelector('[data-answer=spend]').innerText") or '')
              and '¥0' not in (evaljs("document.querySelector('[data-insight-account]').innerText") or '')
              and '不能判断花费或健康状况' in (evaljs("document.querySelector('.task-conclusion').innerText") or ''))
        evaljs("localStorage.setItem('qianshu.account', '3')")
        navigate("/cost")
        check("4 缺摊销明确部分覆盖且总额不补零", wait_js("document.querySelector('[data-status=partial]') !== null", True, 20, "部分覆盖卡")
              and '¥120.00' in (evaljs("document.querySelector('[data-answer=spend]').innerText") or '')
              and '摊销覆盖不完整' in (evaljs("document.querySelector('.insight-coverage').innerText") or '')
              and '不能据此认定总成本或整体正常' in (evaljs("document.querySelector('.task-conclusion').innerText") or ''))
        click_js("document.querySelector('[data-insight-account=\"3\"] .task-primary button')")
        check("4 无异常建议账号进入智能体也携带日账单证据", wait_js("location.pathname", '/agent', 10, "部分覆盖智能体")
              and evaljs("""(() => { const ctx = JSON.parse(new URLSearchParams(location.search).get('ctx') || '{}');
                return ctx.account_id === 3 && ctx.days === 30
                  && ctx.evidence_refs.some(x => x.startsWith('cost_daily:3:')); })()"""))
        evaljs("localStorage.setItem('qianshu.account', '4')")
        navigate("/cost")
        check("4 真实零费用与无数据文案严格区分", wait_js("document.querySelector('[data-status=available]') !== null", True, 20, "零费用卡")
              and '¥0.00' in (evaljs("document.querySelector('[data-answer=spend]').innerText") or '')
              and '真实零费用' in (evaljs("document.querySelector('[data-answer=spend]').innerText") or '')
              and '暂无可核验金额' not in (evaljs("document.querySelector('[data-answer=spend]').innerText") or ''))
        evaljs("window.__mockCalls = []")
        click_js("document.querySelector('[data-insight-account=\"4\"] .task-evidence .el-collapse-item__header')")
        click_js("[...document.querySelectorAll('[data-insight-account=\"4\"] .finding-row')].find(x => x.innerText.includes('核实按量产品费用'))?.querySelector('button')")
        check("4 产品级建议无实例也打开同账号同产品原始账单", wait_js("""(() => {
          const drawer = [...document.querySelectorAll('.el-drawer')].some(x => x.getClientRects().length && x.innerText.includes('费用明细'));
          return drawer && window.__mockCalls.some(c => { if (!c.url.includes('/cost/daily?')) return false;
            const q = new URL(c.url, location.origin).searchParams;
            return q.get('account_id') === '4' && q.get('product') === 'ecs' && !q.get('instance_id'); });
        })()""", True, 10, "产品级原始账单"), str(evaljs("window.__mockCalls.filter(c => c.url.includes('/cost/daily')).map(c => c.url)")))
        click_js("document.querySelector('.el-drawer__body > .el-button')")
        check("4 产品明细导出继承同账号同产品过滤", wait_js("""window.__mockCalls.some(c => {
          if (!c.url.includes('/cost/daily/export')) return false;
          const q = new URL(c.url, location.origin).searchParams;
          return q.get('account_id') === '4' && q.get('product') === 'ecs' && !q.get('instance_id');
        })""", True, 10, "产品明细导出"), str(evaljs("window.__mockCalls.filter(c => c.url.includes('/cost/daily/export')).map(c => c.url)")))
        evaljs("localStorage.setItem('qianshu.account', '0')")
        navigate("/cost")
        check("4 全部账号分账号且未归属单列", wait_js("document.querySelectorAll('[data-insight-account]').length", 5, 20, "多账号卡")
              and evaljs("document.querySelectorAll('[data-unattributed=true]').length") == 1
              and evaljs("[...document.querySelectorAll('[data-insight-account]')].map(x => x.dataset.insightAccount).join(',')") == '1,2,3,4,999')

        print("== 5 切范围、晚到响应与错误撤卡 ==")
        evaljs("localStorage.setItem('qianshu.account', '1'); window.__delay30 = true")
        navigate("/cost")
        wait_js("document.querySelectorAll('.page-toolbar .el-radio-button').length", 4, 10, "范围选择器")
        click_text(".page-toolbar .el-radio-button", "近7天")
        check("5 新区间先到达", wait_js("document.querySelector('[data-answer=spend] .insight-main-number')?.innerText", '¥35.00', 15, "7天金额"))
        time.sleep(1.2)
        check("5 旧30天晚到响应不覆盖7天", evaljs("document.querySelector('[data-answer=spend] .insight-main-number')?.innerText") == '¥35.00'
              and evaljs("window.__mockCalls.filter(c => /\\/cost\\/insights/.test(c.url)).length") >= 2)
        evaljs("window.__delay30 = false; window.__scopeMismatch = true")
        click_text(".page-toolbar .el-radio-button", "近14天")
        check("5 账号/窗口不匹配响应被拒绝并撤下旧卡", wait_js("document.querySelectorAll('[data-insight-account]').length", 0, 10, "撤下错卡")
              and '账号或时间窗与当前选择不一致' in (evaljs("document.querySelector('.el-alert--error')?.innerText") or ''))
        evaljs("window.__scopeMismatch = false; window.__failInsights = true")
        click_text(".page-toolbar .el-radio-button", "近7天")
        check("5 加载失败不保留旧卡冒充当前范围", wait_js("document.querySelectorAll('[data-insight-account]').length", 0, 10, "失败撤卡")
              and 'mock insights unavailable' in (evaljs("document.querySelector('.el-alert--error')?.innerText") or ''))
        evaljs("window.__failInsights = false")
        click_js("document.querySelector('.el-alert--error button')")
        check("5 重试恢复同一事实", wait_js("document.querySelectorAll('[data-insight-account]').length", 1, 10, "重试恢复"))

        print("== 6 原高级页签保留 ==")
        click_text(".el-tabs__item", "资源包")
        check("6 资源包高级页仍可打开", wait_js("document.body.innerText.includes('高级资源包补充')", True, 10, "资源包高级页"))
        click_text(".el-tabs__item", "昨日拆解")
        check("6 昨日拆解深链页签仍可打开", wait_js("document.querySelector('.el-tabs__item.is-active')?.innerText.includes('昨日拆解')", True, 10, "昨日拆解"))
        navigate("/cost?tab=packs")
        check("6 原资源包深链保留", wait_js("document.querySelector('.el-tabs__item.is-active')?.innerText.includes('资源包')", True, 20, "资源包深链"))
        evaljs("localStorage.setItem('qianshu.account', '1'); localStorage.setItem('mockRole', 'viewer')")
        navigate("/cost")
        check("7 viewer保留确定性答案但不显示无权运行按钮", wait_js("document.querySelectorAll('[data-answer]').length", 3, 20, "viewer成本卡")
              and evaljs("document.querySelectorAll('[data-insight-account] .task-primary button').length") == 0)
        evaljs("localStorage.removeItem('mockRole')")
        check("7 无未捕获页面异常", not page_errors, str(page_errors[:3]))
    finally:
        if ws:
            ws.close()
        if proc:
            proc.terminate()
            proc.wait(timeout=10)
    print(f"\n== 结果: {len(PASS)} 通过 / {len(FAIL)} 失败 ==")
    for failure in FAIL:
        print("  FAIL:", failure)
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
