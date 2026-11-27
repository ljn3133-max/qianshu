// 千枢智能体（copilot）会话 API
// CRUD 走 axios（@/api/http 拦截器带 token）；SSE 流式发问不能用 axios——
// 用 fetch + ReadableStream 解析 SSE，Authorization 头手带 localStorage 的 qianshu.token。
import http from '@/api/http'

export const getSystemSnapshot = () => http.get('/agent/system-snapshot')
/** 工作台空态：{generated_at, facts:{...}, items:[{key,sev,cat,title,detail,prompt,link,link_label,action}]} */
export const getAgentSuggestions = () => http.get('/agent/suggestions')
export const listSessions = (params) => http.get('/agent/sessions', { params })
export const createSession = (data) => http.post('/agent/sessions', data || {})
export const patchSession = (id, data) => http.patch(`/agent/sessions/${id}`, data)
export const deleteSession = (id) => http.delete(`/agent/sessions/${id}`)
export const listMessages = (id) => http.get(`/agent/sessions/${id}/messages`)
export const abortSession = (id) => http.post(`/agent/sessions/${id}/abort`, {})

/** 页面深链统一格式：/agent?session=new&q=<预填>&ctx=<页面上下文 JSON> */
export function agentAskUrl(q, ctx) {
  const params = new URLSearchParams()
  params.set('session', 'new')
  if (q) params.set('q', q)
  if (ctx) params.set('ctx', JSON.stringify(ctx))
  return `/agent?${params.toString()}`
}

/** 解析单个 SSE 块：`event:` 行定事件名，`data:` 行拼 JSON（非 JSON 时包成 {text}） */
function parseSseBlock(block) {
  let event = 'message'
  const dataLines = []
  for (const line of block.split(/\r?\n/)) {
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).replace(/^ /, ''))
  }
  if (!dataLines.length) return null
  const raw = dataLines.join('\n')
  let data
  try { data = JSON.parse(raw) } catch { data = { text: raw } }
  // 部分实现把事件名放 data.type / data.event 里——两处都认
  if (event === 'message' && data && typeof data === 'object') {
    event = data.type || data.event || event
  }
  return { event, data }
}

/**
 * SSE 发问：POST /agent/sessions/{id}/messages，body {q, ctx}。
 * 事件（契约）：snapshot / token / tool / change / done / error（error 带 code，NEED_MODEL=未配模型）。
 * change=变更工单事件：{ticket_id,title,change_list:[{idx,title,impact,rollback,ack_required}]}；
 * L3b 补 diff_summary={files:[{path,add_lines,del_lines,diff_head(首 20 行)}]}（代码变更 diff 预览）。
 * onEvent 收 {event, data}；resolve=流正常结束；reject 带 err.code（HTTP 错误时）。
 */
export function streamAgentMessage(id, { q, ctx, onEvent, signal }) {
  const token = localStorage.getItem('qianshu.token')
  return fetch(`/api/agent/sessions/${id}/messages`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({ q, ctx: ctx || {} }),
    signal,
  }).then(async (resp) => {
    if (!resp.ok) {
      let detail = null
      try { detail = (await resp.json()).detail } catch { /* 非 JSON 错误体 */ }
      const err = new Error(typeof detail === 'string' ? detail : (detail?.message || `请求失败 (${resp.status})`))
      err.code = detail?.code
      err.status = resp.status
      throw err
    }
    const reader = resp.body.getReader()
    const decoder = new TextDecoder('utf-8')
    let buf = ''
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      // 事件分隔兼容 \n\n 与 \r\n\r\n（SSE 规范两者皆可）
      for (;;) {
        const m = buf.match(/\r?\n\r?\n/)
        if (!m) break
        const evt = parseSseBlock(buf.slice(0, m.index))
        buf = buf.slice(m.index + m[0].length)
        if (evt && onEvent) onEvent(evt)
      }
    }
    const tail = parseSseBlock(buf)
    if (tail && onEvent) onEvent(tail)
  })
}

/* ================= 变更工单 + 工作流（功能设计 §三/五契约） ================= */

/** 变更工单列表（跨会话）：status 过滤可选（pending/done/…，不传=全部） */
export const listChanges = (params) => http.get('/agent/changes', { params })
/** 确认执行：body {items?: [idx...], ack?: "我已知晓"}；403=未过确认门/ack 不对（detail 有提示） */
export const confirmChange = (id, body) => http.post(`/agent/changes/${id}/confirm`, body || {})
/** 一键回滚（按快照逆向写回，回滚本身留痕） */
export const rollbackChange = (id) => http.post(`/agent/changes/${id}/rollback`, {})
/** 取消变更工单 */
export const dismissChange = (id) => http.post(`/agent/changes/${id}/dismiss`, {})
/** 跳过/恢复变更项，状态落服务端：body {idx, restore?}。
 *  状态机 pending→skipped（跳过，不可执行）；restore=true 恢复 skipped→pending。 */
export const skipChange = (id, body) => http.post(`/agent/changes/${id}/skip`, body || {})

/* ================= 平台自改管道：代码级快照/回滚 ================= */

/** 代码改动快照列表：GET /agent/code-snapshots?ticket_id= ->
 *  {items:[{id, files:[{path,diff_summary}], created_at, status, verified}]} */
export const listCodeSnapshots = (params) => http.get('/agent/code-snapshots', { params })
/** 一键回滚代码快照（逐文件字节级恢复改动前内容，回滚本身留痕）。
 *  跨快照冲突（文件应用后被第三方改动）默认拒——body {force:true} 才强制还原。 */
export const rollbackCodeSnapshot = (id, force) => http.post(`/agent/code-snapshots/${id}/rollback`, force ? { force: true } : {})
/** 「重启生效」：admin 二次确认后委托 qianshu-copilotd 重启平台使代码改动生效。
 *  body {confirm:true}（服务端强制二次确认）；重启会短暂中断服务，恢复后站内信有重启回执。 */
export const applyRestartCodeSnapshot = (id) => http.post(`/agent/code-snapshots/${id}/apply-restart`, { confirm: true })

/** 工作流清单（key/name/domain/description/steps_summary/last_run） */
export const listWorkflows = () => http.get('/agent/workflows')
/** 工作流结构化详情（内置/自定义统一出步骤序列/工具/失败策略；只读，敏感参数掩码） */
export const getWorkflowDetail = (key) => http.get(`/agent/workflows/${key}/detail`)
/** 运行工作流：body {params, session_id?} -> {run_id,status,steps,artifact,tickets}。
 *  带 session_id 则运行记录挂会话（重开会话后工作流结果卡仍在，listMessages 拼接）。 */
export const runWorkflow = (key, params, sessionId) => http.post(`/agent/workflows/${key}/run`, {
  params: params || {}, session_id: sessionId ?? null,
})
/** 工作流运行记录 */
export const listWorkflowRuns = () => http.get('/agent/workflows/runs')

/* ================= 自定义工作流（admin 管理） ================= */

/** 自定义定义列表（key/name/description/domain/steps/enabled/version/params_hint） */
export const listCustomWorkflows = () => http.get('/agent/workflows/custom')
/** 创建（body 带 overwrite=true 则同 key 幂等覆盖导入，version+1） */
export const createCustomWorkflow = (data) => http.post('/agent/workflows/custom', data || {})
/** 修改（version+1；steps 整表替换） */
export const updateCustomWorkflow = (key, data) => http.put(`/agent/workflows/custom/${key}`, data || {})
/** 删除定义（运行记录保留） */
export const deleteCustomWorkflow = (key) => http.delete(`/agent/workflows/custom/${key}`)
/** 预览执行（零副作用）：body {key?, steps?, params?} -> 逐步展开计划（敏感参数掩码） */
export const previewCustomWorkflow = (data) => http.post('/agent/workflows/custom/preview', data || {})
/** 工具目录（只读工具 + 变更工具两组，含参数名/必填/枚举/敏感标记） */
export const workflowToolCatalog = () => http.get('/agent/workflows/custom/catalog')
/** 内置工作流「另存为自定义」（key 加 -copy 后缀进自定义区；overwrite=true 幂等覆盖副本） */
export const saveAsWorkflow = (key, overwrite) => http.post(`/agent/workflows/${key}/save-as`, {
  overwrite: !!overwrite,
})

/* ============ 版本历史 + 一键回滚 + YAML/JSON 导入导出（admin） ============ */

/** 版本历史（新在前）：{items:[{version,created_at,created_by,name,steps_n,enabled,def,current}],current_version} */
export const listWorkflowVersions = (key) => http.get(`/agent/workflows/custom/${key}/versions`)
/** 一键回滚：恢复为该版本内容 → 当前 version+1 落新行（历史只增）；返回定义 + restored_from */
export const restoreWorkflowVersion = (key, version) => http.post(`/agent/workflows/custom/${key}/versions/${version}/restore`)
/** 导出定义（format=yaml|json 两格式都提供）：返回导出包文本（含步骤/参数模板/失败策略） */
export const exportWorkflowDef = (key, format) => http.get(`/agent/workflows/custom/${key}/export`, {
  params: { format: format || 'yaml' }, responseType: 'text',
})
/** 导入 YAML/JSON 文本（schema 同创建校验；同 key 冲突 409，overwrite=true 覆盖更新） */
export const importWorkflowDef = (content, overwrite) => http.post('/agent/workflows/custom/import', {
  content, overwrite: !!overwrite,
})

/* ================= 工作流定时体检（例行化，有异动才推人） ================= */

/** 定时清单（含上次/下次执行；下次=待触发时为 null） */
export const listSchedules = () => http.get('/agent/schedules')
/** 新增定时（仅分析域；同 key 已有定时 409）：{workflow_key, interval_days?, hour?, enabled?} */
export const createSchedule = (data) => http.post('/agent/schedules', data || {})
/** 修改定时（间隔/时刻/启用；enabled=false=停用） */
export const patchSchedule = (id, data) => http.patch(`/agent/schedules/${id}`, data || {})

/** 错误 detail 提示文案：string / {code,message} / Error 三种形状都认（403 确认门提示用） */
export function errMsg(e, fallback = '操作失败') {
  const d = e?.response?.data?.detail ?? e?.detail
  if (typeof d === 'string' && d) return d
  if (d && typeof d === 'object') return d.message || d.msg || fallback
  return e?.message || fallback
}

/** SSE change 事件 / 工作流 run 返回的 ticket 归一成前端变更卡形状（键名容错） */
export function normalizeTicket(t, sessionId) {
  if (!t || typeof t !== 'object') return null
  const id = t.id ?? t.ticket_id
  if (id == null) return null
  return {
    id,
    session_id: t.session_id ?? sessionId ?? null,
    title: t.title || '变更工单',
    status: t.status || 'pending',
    diff_summary: t.diff_summary || null,               // L3b：change 事件级 diff（代码变更预览）
    change_list: (t.change_list || []).map((it, i) => ({
      idx: it.idx ?? i,
      tool: it.tool || '',                              // L3b：code_modify=平台自改代码变更项
      title: it.title || '',
      impact: it.impact || '',
      rollback: it.rollback || '',
      ack_required: !!it.ack_required,
      sensitive: !!it.sensitive || !!it.ack_required,   // 敏感二次确认：ack_required 也是敏感
      params: it.params || {},                          // 参数摘要（展示走掩码口径）
      diff_summary: it.diff_summary || null,            // L3b：项级 diff（缺省回事件级）
      restart_required: it.restart_required === true,   // L3b：回执带「需重启生效」标记
      status: it.status || '',
      result: it.result ?? '',
    })),
    created_at: t.created_at || new Date().toISOString(),
    confirmed_at: t.confirmed_at || null,
    result: t.result ?? null,
  }
}
