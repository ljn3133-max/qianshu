#!/usr/bin/env python3
"""工作流配置页（WorkflowEditor.vue）两区渲染 mock CDP 测试。

运行：cd /opt/qianshu/backend && python3 tests/test_workflow_editor_cdp_mock.py
形态：真 Chrome（headless，降权 nobody 走 chrome_run）+ CDP（Runtime.evaluate 直查 DOM）
+ Page.addScriptToEvaluateOnNewDocument 注入 /api mock 层（XHR/fetch 全拦，调用记
window.__mockCalls）——**零后端依赖**（不碰 8810/不启服务），页面走 vite dev（5273）拿真实
组件（WorkflowEditor.vue 编译产物热加载）。

验证项：
  1 两区列表：「内置（13）」区 13 张只读卡（名称/域/描述/步骤摘要链/内置徽标/另存为按钮）
    +「自定义」区可编辑卡（既有 CRUD 卡零变化）
  2 点开内置只读详情：步骤序列/工具/失败策略表 + 运行参数表（敏感标记）——无编辑表单
  3 「另存为自定义」：POST /agent/workflows/{key}/save-as（overwrite=false）——
    副本进自定义区（卡片 +1）并在编辑器中打开（key=cost_checkup-copy）
  4 历史版本：入口按钮→版本列表（版本/时间/操作人）+ 当前版标注
    + 非当前版恢复按钮→二次确认→POST .../versions/{v}/restore→历史刷新
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
CDP_PORT = 9348   # 避开 browser_sweep 的 9333 与变更卡测试的 9347，防并行测试互踩
PROFILE = "/tmp/chrome-wf-editor-cdp"

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


# ---------------- /api mock 层（注入页面，XHR/fetch 全拦 + 调用留痕） ----------------

MOCK_JS = r"""
(() => {
  localStorage.setItem('qianshu.token', 'mock-token');
  localStorage.setItem('qianshu.account', '0');
  localStorage.setItem('qianshu.lang', 'zh');

  // 内置 13 个（key/name/domain/description/steps_summary——GET /agent/workflows 契约）
  const builtinKeys = [
    ['access_cloud', '接入云账号', '接入域'], ['access_datasource', '接入数据源', '接入域'],
    ['access_k8s', '接入 K8s 集群', '接入域'], ['access_virt', '接入 ESXi/vCenter/KVM', '接入域'],
    ['cost_checkup', '成本体检', '分析域'], ['fault_diagnosis', '故障诊断', '分析域'],
    ['capacity_checkup', '容量体检', '分析域'], ['change_impact', '变更影响分析', '分析域'],
    ['topology_refine', '拓扑重画', '改进域'], ['alert_tune', '告警调参', '改进域'],
    ['inspection_custom', '巡检定制', '改进域'], ['dashboard_custom', '看板定制', '改进域'],
    ['onboard_init', '引导初始化', '引导域'],
  ];
  const summaries = {
    access_cloud: ['校验参数', '建变更工单', '提示测试连通', '触发同步', '拓扑覆盖检查'],
    cost_checkup: ['账单三口径对账', '产品分布', '资源包覆盖', '异动回顾', '节省建议'],
  };
  const builtins = builtinKeys.map(([key, name, domain]) => ({
    key, name, domain, description: name + '：内置工作流描述（' + domain + '）',
    params_schema: {}, steps_summary: summaries[key] || ['步骤一', '步骤二'],
    last_run: null,
  }));

  const costDetail = {
    key: 'cost_checkup', custom: false, builtin: true, name: '成本体检', domain: '分析域',
    description: '成本体检：账单三口径对账 → 产品分布 → 资源包覆盖 → 异动回顾 → 节省建议',
    params_schema: {
      days: { type: 'integer', required: false, default: 30, description: '体检窗口天数', sensitive: false },
      account_id: { type: 'integer', required: false, description: '云账号ID过滤', sensitive: false },
    },
    params_hint: [], enabled: true, version: null, has_change_steps: false,
    steps: [
      { idx: 1, label: '账单三口径对账', kind: 'internal', tools: [], tool: '', continue_on_fail: true, will_ticket: false, params_template: {} },
      { idx: 2, label: '产品分布', kind: 'read', tools: ['query_cost'], tool: 'query_cost', continue_on_fail: true, will_ticket: false, params_template: {} },
      { idx: 3, label: '资源包覆盖', kind: 'read', tools: ['pack_pools'], tool: 'pack_pools', continue_on_fail: true, will_ticket: false, params_template: {} },
      { idx: 4, label: '异动回顾', kind: 'read', tools: ['cost_anomaly_list'], tool: 'cost_anomaly_list', continue_on_fail: true, will_ticket: false, params_template: {} },
      { idx: 5, label: '节省建议', kind: 'internal', tools: [], tool: '', continue_on_fail: true, will_ticket: false, params_template: {} },
    ],
  };

  const customDefs = [{
    key: 'wf_demo', name: '成本巡检（自定义）', description: '查成本 + 看资源包', domain: '自定义域',
    steps: [
      { tool: 'query_cost', label: '查成本', params_template: { days: '{{days}}' }, continue_on_fail: false },
      { tool: 'pack_pools', label: '看资源包', params_template: {}, continue_on_fail: true },
    ],
    enabled: true, version: 4, created_by: 'admin', created_at: '2026-11-06T10:00:00',
    updated_at: '2026-11-06T10:00:00', custom: true, params_hint: ['days'], has_change_steps: false,
  }];

  window.__mockCalls = [];
  function apiResponse(method, path, body) {
    window.__mockCalls.push({ method: method, url: path, body: body });
    if (/\/agent\/workflows\/custom\/catalog$/.test(path)) {
      return { items: [
        { name: 'query_cost', kind: 'read', desc: '查成本', params: [{ name: 'days', desc: '天数', required: false }] },
        { name: 'pack_pools', kind: 'read', desc: '看资源包', params: [] },
      ] };
    }
    if (/\/agent\/workflows\/[\w-]+\/save-as$/.test(path)) {
      const key = (path.match(/\/agent\/workflows\/([\w-]+)\/save-as/) || [])[1] || '';
      const copyKey = key + '-copy';
      const existing = customDefs.find((x) => x.key === copyKey);
      if (existing && !(body && body.overwrite)) {
        return { detail: '工作流 key「' + copyKey + '」已存在（导入覆盖请带 overwrite=true）' };
      }
      const copy = {
        key: copyKey, name: '成本体检（副本）', description: '另存自内置工作流 ' + key,
        domain: '分析域', custom: true, enabled: true,
        version: existing ? existing.version + 1 : 1, created_by: 'admin',
        steps: [
          { tool: 'query_cost', label: '产品分布', params_template: { days: '{{days}}', account_id: '{{account_id}}' }, continue_on_fail: true },
          { tool: 'pack_pools', label: '资源包覆盖', params_template: { account_id: '{{account_id}}' }, continue_on_fail: true },
          { tool: 'cost_anomaly_list', label: '异动回顾', params_template: {}, continue_on_fail: true },
        ],
        params_hint: ['account_id', 'days'], has_change_steps: false,
      };
      if (existing) Object.assign(existing, copy);
      else customDefs.push(copy);
      return Object.assign({}, copy, {
        source_key: key, copied_steps: 3, omitted_labels: ['账单三口径对账', '节省建议'],
      });
    }
    if (/\/agent\/workflows\/[\w-]+\/detail$/.test(path)) {
      const key = (path.match(/\/agent\/workflows\/([\w-]+)\/detail/) || [])[1] || '';
      const d = customDefs.find((x) => x.key === key);
      if (d) return d;
      return costDetail;   // mock 简化：内置 key 统一回 cost_checkup 详情
    }
    if (/\/agent\/workflows\/custom$/.test(path)) {
      if (method === 'GET') return { items: customDefs.slice() };
      return { key: (body && body.key) || 'new', version: 1, custom: true };
    }
    if (/\/agent\/workflows\/custom\/[\w-]+\/versions\/\d+\/restore$/.test(path)) {
      return { key: 'wf_demo', name: '成本巡检（自定义）', version: 5, custom: true, restored_from: 3 };
    }
    if (/\/agent\/workflows\/custom\/[\w-]+\/versions$/.test(path)) {
      return { items: [
        { version: 4, created_at: '2026-11-06T10:00:00', created_by: 'admin',
          name: '成本巡检（自定义）', steps_n: 2, enabled: true, def: {}, current: true },
        { version: 3, created_at: '2026-11-05T09:00:00', created_by: 'admin',
          name: '成本巡检 v3', steps_n: 1, enabled: true, def: {}, current: false },
      ], current_version: 4 };
    }
    if (/\/agent\/workflows$/.test(path)) {
      return { items: builtins.concat(customDefs.map((d) => Object.assign({}, d, {
        params_schema: {}, steps_summary: (d.steps || []).map((s) => s.label),
        last_run: null }))) };
    }
    if (/\/bootstrap\/status$/.test(path)) return { init_done: true };
    if (/\/auth\/me$/.test(path)) {
      return { id: 1, username: 'admin', role: 'admin', must_change_password: false,
               permission_codes: [] };
    }
    if (/\/ai\/settings$/.test(path)) return [{ id: 1, name: 'mock-model', enabled: true }];
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
  // fetch 拦截（兜底路径也走 mock，避免真发请求）
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
        cdp_send("Page.navigate", {"url": f"{SITE}/workflow-editor"})

        print("== 1 两区列表（内置 13 只读卡 + 自定义可编辑卡） ==")
        ok = wait_js("document.querySelectorAll('.def-sec-builtin .def-card').length", 13, 40, "内置 13 卡")
        check("1 「内置」区 13 张卡渲染", ok)
        n_badge = evaljs("[...document.querySelectorAll('.def-sec-builtin .def-card')]"
                         ".filter(e => e.innerText.indexOf('内置') >= 0).length")
        check("1 每张内置卡带「内置」徽标", n_badge == 13, f"n={n_badge}")
        n_steps_line = evaljs("document.querySelectorAll('.def-sec-builtin .def-steps-line').length")
        sample = evaljs("(document.querySelector('.def-sec-builtin .def-steps-line')||{}).innerText || ''")
        check("1 内置卡出步骤摘要链（步骤：… → …）",
              n_steps_line == 13 and '步骤' in (sample or '') and '→' in (sample or ''),
              f"n={n_steps_line} sample={sample!r}")
        n_custom = evaljs("document.querySelectorAll('.def-sec-custom .def-card').length")
        custom_txt = evaljs("(document.querySelector('.def-sec-custom')||{}).innerText || ''")
        check("1 「自定义」区渲染既有自定义卡（wf_demo）",
              n_custom == 1 and '成本巡检（自定义）' in (custom_txt or ''),
              f"n={n_custom}")
        n_save_btns = evaljs("[...document.querySelectorAll('.def-sec-builtin .def-actions button')]"
                             ".filter(e => e.innerText.indexOf('另存为') >= 0).length")
        check("1 每张内置卡都有「另存为自定义」按钮", n_save_btns == 13, f"n={n_save_btns}")
        sec_titles = evaljs("[...document.querySelectorAll('.def-list-title')].map(e=>e.innerText.trim())")
        check("1 两区标题（内置 N + 自定义 N）",
              len(sec_titles or []) == 2 and '13' in sec_titles[0] and '1' in sec_titles[1],
              str(sec_titles))

        print("== 2 内置只读详情（步骤序列/工具/失败策略 + 参数表） ==")
        evaljs("""(() => {
          const card = [...document.querySelectorAll('.def-sec-builtin .def-card')]
            .find(e => e.innerText.indexOf('成本体检') >= 0);
          if (card) card.querySelector('.def-main').click();
        })()""")
        ok = wait_js("document.querySelectorAll('.detail-table').length", 2, 20, "详情两表")
        check("2 点开内置出只读详情（步骤表 + 参数表）", ok)
        rows = evaljs("document.querySelectorAll('.detail-table')[0].querySelectorAll('.el-table__row').length")
        step_txt = evaljs("(document.querySelectorAll('.detail-table')[0]||{}).innerText || ''")
        check("2 步骤序列 5 步（工具/类型/失败策略列：query_cost/内建步骤/失败继续）",
              rows == 5 and '产品分布' in (step_txt or '') and 'query_cost' in (step_txt or '')
              and '账单三口径对账' in (step_txt or '') and '失败继续' in (step_txt or '')
              and '内建步骤' in (step_txt or '') and '只读' in (step_txt or ''),
              f"rows={rows} txt={(step_txt or '')[:200]}")
        param_txt = evaljs("(document.querySelectorAll('.detail-table')[1]||{}).innerText || ''")
        check("2 参数表（days/account_id + 默认值口径）",
              'days' in (param_txt or '') and 'account_id' in (param_txt or '')
              and '体检窗口天数' in (param_txt or ''), (param_txt or '')[:200])
        n_forms = evaljs("document.querySelectorAll('.wf-edit-pane .form-grid').length")
        check("2 详情为只读（无编辑表单）", n_forms == 0, f"n={n_forms}")
        policy_txt = evaljs("(document.querySelector('.wf-edit-pane')||{}).innerText || ''")
        check("2 失败策略说明（内置=单步失败不中止）",
              '单步失败不中止' in (policy_txt or ''), (policy_txt or '')[:200])

        print("== 3 「另存为自定义」（-copy 副本进自定义区 + 打开编辑） ==")
        evaljs("""(() => {
          const btn = [...document.querySelectorAll('.wf-edit-pane .edit-foot button')]
            .find(e => e.innerText.indexOf('另存为') >= 0);
          if (btn) btn.click();
        })()""")
        ok = wait_js("document.querySelectorAll('.def-sec-custom .def-card').length", 2, 20, "副本进自定义区")
        check("3 副本进自定义区（卡片 +1）", ok)
        call = evaljs("""(() => {
          const c = (window.__mockCalls || []).find(
            (x) => x.method === 'POST' && /save-as$/.test(x.url));
          return c ? JSON.stringify(c) : '';
        })()""")
        check("3 发 POST /agent/workflows/cost_checkup/save-as（overwrite=false）",
              bool(call) and '/agent/workflows/cost_checkup/save-as' in call
              and '"overwrite":false' in (call or '').replace(' ', ''),
              call[:200])
        key_val = evaljs("""(() => {
          const inp = document.querySelector('.wf-edit-pane .el-input.mono input');
          return inp ? inp.value : '';
        })()""")
        check("3 副本在编辑器中打开（key=cost_checkup-copy）",
              key_val == 'cost_checkup-copy', f"key={key_val!r}")
        n_step_cards = evaljs("document.querySelectorAll('.wf-edit-pane .step-card').length")
        first_label = evaljs("document.querySelectorAll('.wf-edit-pane .step-card input').length"
                             " ? document.querySelectorAll('.wf-edit-pane .step-card input')[0].value : ''")
        has_save_btn = evaljs("[...document.querySelectorAll('.wf-edit-pane button')]"
                              ".some(e => e.innerText.trim() === '保存')")
        check("3 编辑器可编辑（3 步卡片 + 步骤名 + 保存按钮）",
              n_step_cards == 3 and first_label == '产品分布' and has_save_btn is True,
              f"cards={n_step_cards} label={first_label!r} save={has_save_btn}")

        print("== 4 历史版本（入口/列表+时间/恢复按钮/当前版标注/一键回滚） ==")
        head_txt = evaljs("(document.querySelector('.head-actions')||{}).innerText || ''")
        check("4 导入导出双格式入口（头部标注 YAML）",
              'YAML' in (head_txt or ''), (head_txt or '')[:120])
        evaljs("""(() => {
          const btn = [...document.querySelectorAll('.def-sec-custom .def-actions button')]
            .find(e => e.innerText.indexOf('历史版本') >= 0);
          if (btn) btn.click();
        })()""")
        ok = wait_js("document.querySelectorAll('.el-dialog .el-table__row').length", 2, 20, "历史两行")
        check("4 历史版本入口打开（版本列表 2 行）", ok)
        dlg_txt = evaljs("""(() => {
          const dlg = [...document.querySelectorAll('.el-dialog')]
            .find(e => e.innerText.indexOf('历史版本') >= 0);
          return dlg ? dlg.innerText : '';
        })()""")
        check("4 列表含版本/时间/操作人（v4/v3 + 时间 + admin）",
              'v4' in (dlg_txt or '') and 'v3' in (dlg_txt or '')
              and '2026-11-05 09:00:00' in (dlg_txt or '') and 'admin' in (dlg_txt or ''),
              (dlg_txt or '')[:300])
        check("4 当前版标注（v4 行带「当前版本」）",
              '当前版本' in (dlg_txt or ''), (dlg_txt or '')[:300])
        n_restore_btns = evaljs("""(() => {
          const dlg = [...document.querySelectorAll('.el-dialog')]
            .find(e => e.innerText.indexOf('历史版本') >= 0);
          return dlg ? [...dlg.querySelectorAll('button')]
            .filter(b => b.innerText.indexOf('恢复此版本') >= 0).length : -1;
        })()""")
        check("4 非当前版出恢复按钮（1 个）", n_restore_btns == 1, f"n={n_restore_btns}")
        evaljs("""(() => {
          const dlg = [...document.querySelectorAll('.el-dialog')]
            .find(e => e.innerText.indexOf('历史版本') >= 0);
          const btn = dlg && [...dlg.querySelectorAll('button')]
            .find(b => b.innerText.indexOf('恢复此版本') >= 0);
          if (btn) btn.click();
        })()""")
        ok = wait_js("document.querySelectorAll('.el-message-box').length", 1, 20, "恢复确认框")
        check("4 恢复前二次确认（确认框弹出）", ok)
        evaljs("""(() => {
          const btn = [...document.querySelectorAll('.el-message-box button')]
            .find(b => b.classList.contains('el-button--primary'));
          if (btn) btn.click();
        })()""")
        call = evaljs("""(() => {
          const c = (window.__mockCalls || []).find(
            (x) => x.method === 'POST' && x.url.indexOf('/versions/') >= 0
              && x.url.indexOf('/restore') >= 0);
          return c ? JSON.stringify(c) : '';
        })()""")
        check("4 发 POST .../versions/3/restore（一键回滚）",
              bool(call) and '/versions/3/restore' in call, call[:200])
        ok = wait_js("(window.__mockCalls || []).filter((x) => x.method === 'GET'"
                     " && x.url.indexOf('/versions') >= 0).length >= 2", True, 20, "恢复后刷新历史")
        check("4 恢复后刷新历史列表", ok)

        check("页面无未捕获 JS 异常", not page_errors, str(page_errors[:3]))
        return True
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


if __name__ == "__main__":
    ok = main()
    print(f"\n== 结果: {len(PASS)} 通过 / {len(FAIL)} 失败 ==")
    if FAIL or not ok:
        print("失败项: " + " | ".join(FAIL))
        raise SystemExit(1)
    raise SystemExit(0)
