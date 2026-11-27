<script setup>
// 千枢智能体 · 会话工作台：三栏布局
// 左栏会话列表（未完成变更红点）/ 中栏消息流（用户气泡 + 助手 markdown + 工具轨迹卡 +
// 变更确认卡 + 工作流结果卡 + 系统快照条）/ 右栏会话面板（系统快照 + 工具统计 + 变更工单）。
// 右栏默认收起，展开为悬浮抽屉 overlay，不挤压消息流。
// 深链入参：/agent?session=<id> 打开指定会话；/agent?session=new&q=<预填>&ctx=<json> 新会话
// 自动发送；/agent?workflow=<key> 直达工作流运行面板。
import MarkdownIt from 'markdown-it'
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CopyDocument, MagicStick, Refresh } from '@element-plus/icons-vue'
import { useRoute, useRouter } from 'vue-router'

import {
  abortSession, createSession, createSchedule, deleteSession, getAgentSuggestions,
  getSystemSnapshot,
  listChanges, listMessages, listSchedules, listSessions, listWorkflowRuns, listWorkflows,
  normalizeTicket, patchSchedule, patchSession, runWorkflow, streamAgentMessage,
} from '@/api/agent'
import { listAiSettings } from '@/api/ai'
import { getAccountOnboarding, listOnboardingRuns } from '@/api/clouds'
import AgentChangeCard from '@/components/AgentChangeCard.vue'
import TaskResultCard from '@/components/TaskResultCard.vue'
import { useAuthStore } from '@/stores/auth'
import { fmtTime } from '@/utils/format'
import { t } from '@/i18n'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
// 运行/定时权限与后端 _RUN_ROLES/_WRITE_ROLES 对齐，viewer 只读
const canRunWorkflow = computed(() => ['admin', 'operator'].includes(auth.role))

const md = new MarkdownIt({ html: false, breaks: true })

/* ---------------- 左栏：会话列表 ---------------- */
const sessions = ref([])
const search = ref('')
const current = ref(null)   // 当前会话（null=新会话，首条消息发出时才落库，避免空会话垃圾）

async function loadSessions() {
  try {
    const r = await listSessions()
    sessions.value = r.items || []
    // 深链直达的会话在列表回来前用的是占位标题——补真标题
    if (current.value?.id) {
      const s = sessions.value.find((x) => x.id === current.value.id)
      if (s) current.value = s
    }
  } catch { /* 列表失败不打断主区（拦截器已提示） */ }
}

const groupedSessions = computed(() => {
  const kw = search.value.trim()
  let arr = sessions.value.slice()
  if (kw) arr = arr.filter((s) => (s.title || '').includes(kw))
  arr.sort((a, b) => ((b.pinned ? 1 : 0) - (a.pinned ? 1 : 0))
    || (new Date(b.last_active_at || 0) - new Date(a.last_active_at || 0)))
  const dayStart = new Date()
  dayStart.setHours(0, 0, 0, 0)
  const weekStart = new Date(dayStart)
  weekStart.setDate(dayStart.getDate() - ((dayStart.getDay() + 6) % 7))   // 周一
  // 分桶 id（i18n 键后缀）：today / week / older，渲染处 t() 化
  const buckets = { today: [], week: [], older: [] }
  for (const s of arr) {
    const at = new Date(s.last_active_at || 0)
    if (at >= dayStart) buckets.today.push(s)
    else if (at >= weekStart) buckets.week.push(s)
    else buckets.older.push(s)
  }
  return Object.entries(buckets).map(([label, items]) => ({ label, items })).filter((g) => g.items.length)
})

const fmtShort = (iso) => (iso ? fmtTime(iso).slice(5, 16) : '')

function newSession() {
  current.value = null
  messages.value = []
  wfResults.value = []
  resetWorkflowOnboardingTasks()
  streamBuf.value = ''
  streamTools.value = []
  streamActive.value = false
  errorMsg.value = ''
  loadSuggestions()   // 回到工作台刷新"今天建议"（期间可能有新变更/待办）
}

function openSession(s) {
  if (current.value?.id === s.id) return
  current.value = s
  messages.value = []
  wfResults.value = []
  resetWorkflowOnboardingTasks()
  errorMsg.value = ''
  streamBuf.value = ''
  streamTools.value = []
  streamActive.value = false
  loadMessages()
  loadChanges()
}

async function togglePin(s) {
  try {
    await patchSession(s.id, { pinned: !s.pinned })
    s.pinned = !s.pinned
    ElMessage.success(s.pinned ? t('agent.msg.pinned') : t('agent.msg.unpinned'))
  } catch { /* 拦截器已提示 */ }
}

async function renameSession(s) {
  let title
  try {
    const r = await ElMessageBox.prompt(t('agent.session.title_prompt'), t('agent.session.rename_title'), {
      inputValue: s.title || '',
      inputValidator: (v) => !!String(v || '').trim() || t('agent.session.title_required'),
    })
    title = String(r.value).trim()
  } catch { return }
  try {
    await patchSession(s.id, { title })
    s.title = title
    if (current.value?.id === s.id) current.value.title = title
    ElMessage.success(t('agent.msg.renamed'))
  } catch { /* 拦截器已提示 */ }
}

async function removeSession(s) {
  try {
    await ElMessageBox.confirm(
      t('agent.confirm.delete_session', { name: s.title || t('agent.session.new') }),
      t('agent.confirm.delete_session_title'), { type: 'warning' },
    )
  } catch { return }
  try {
    await deleteSession(s.id)
    sessions.value = sessions.value.filter((x) => x.id !== s.id)
    if (current.value?.id === s.id) newSession()
    ElMessage.success(t('agent.msg.deleted'))
  } catch { /* 拦截器已提示 */ }
}

/* ---------------- 中栏：消息流 ---------------- */
const messages = ref([])
const openTool = ref({})       // msgId -> 工具卡展开
const toolItemOpen = ref({})   // `${msgId}-${i}` -> 结果摘要展开
const streamEl = ref(null)

async function loadMessages() {
  if (!current.value?.id) {
    messages.value = []
    return
  }
  try {
    const r = await listMessages(current.value.id)
    messages.value = (r.items || []).map((m) => {
      const o = { ...m, tool_calls: m.tool_calls || [] }
      // 持久化运行结果：msg_kind=workflow_run 的合成消息渲染成工作流结果卡
      // （复用现有卡组件逻辑；id=wf-run-N 与本地卡同 id，displayItems 去重防双卡）
      if (m.msg_kind === 'workflow_run') {
        o.kind = 'workflow'
        o.wfName = m.workflow_name || m.workflow_key || t('agent.wf.generic_name')
        o.runId = m.run_id
      }
      return o
    })
    loadWorkflowOnboardingTasks()
  } catch { /* 保留现有显示 */ }
  scrollToBottom()
}

// 已落库消息的 markdown 只渲染一次缓存（长会话不因流式 token 反复重渲全量历史）
const htmlCache = reactive({})
function renderHtml(m) {
  if (htmlCache[m.id] === undefined) htmlCache[m.id] = md.render(m.content_md || '')
  return htmlCache[m.id]
}

function toggleTool(m) {
  openTool.value[m.id] = !openTool.value[m.id]
}

function toggleToolItem(key) {
  toolItemOpen.value[key] = !toolItemOpen.value[key]
}

const toolTotalSec = (m) => ((m.tool_calls || []).reduce((a, t) => a + (Number(t.ms) || 0), 0) / 1000).toFixed(1)

/* ---------------- 发送 / SSE 流式 ---------------- */
const input = ref('')
const sending = ref(false)
const streamActive = ref(false)
const streamBuf = ref('')
const streamHtml = ref('')
const streamTools = ref([])
const doneInfo = ref(null)
const errorMsg = ref('')
const needModel = ref(false)
const linkCtx = ref(null)      // 深链带来的页面对象上下文
let abortCtrl = null
let renderTimer = null

// 消息流统一渲染源：用户/助手消息 + 变更确认卡 + 工作流结果卡（按时间混排，流式消息恒在末尾）
const displayItems = computed(() => {
  const items = []
  const seen = new Set()
  for (const m of messages.value) {
    const k = String(m.id)
    if (seen.has(k)) continue   // 防重复：合成 workflow_run 卡与本地卡同 id 时只留一条
    seen.add(k)
    items.push({ ...m, kind: m.kind || 'msg' })
  }
  for (const t of sessionTickets.value) {
    const k = 'chg-' + t.id
    if (seen.has(k)) continue
    seen.add(k)
    items.push({ id: k, kind: 'change', ticket: t, created_at: t.created_at })
  }
  for (const w of wfResults.value) {
    const k = String(w.id)
    if (seen.has(k)) continue
    seen.add(k)
    items.push(w)
  }
  items.sort((a, b) => new Date(a.created_at || 0) - new Date(b.created_at || 0))
  if (streamActive.value) {
    items.push({
      id: 'stream', role: 'assistant', kind: 'msg', content_md: streamBuf.value,
      tool_calls: streamTools.value, streaming: true,
    })
  }
  return items
})

watch(streamBuf, () => {
  if (renderTimer) return
  renderTimer = setTimeout(() => {
    renderTimer = null
    streamHtml.value = md.render(streamBuf.value)
  }, 120)
})

function scrollToBottom() {
  nextTick(() => {
    const el = streamEl.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

function buildCtx() {
  return { page: '/agent', ...(linkCtx.value || {}) }
}

function handleStreamEvent({ event, data }) {
  const type = event || 'message'
  if (type === 'snapshot') {
    const snap = (data && typeof data === 'object' && data.snapshot) ? data.snapshot : data
    if (snap && typeof snap === 'object') {
      snapshot.value = snap
      snapState.value = Object.keys(snap).length ? 'ok' : 'empty'
    }
  } else if (type === 'token') {
    const t = typeof data === 'string' ? data : (data?.token ?? data?.text ?? data?.content ?? '')
    if (t) {
      streamBuf.value += t
      scrollToBottom()
    }
  } else if (type === 'tool') {
    const tc = (data && typeof data === 'object' && (data.tool_call || data.tool))
      ? (data.tool_call || data.tool) : data
    if (tc && tc.name) {
      streamTools.value.push({
        name: tc.name,
        params_summary: tc.params_summary ?? (tc.params ? JSON.stringify(tc.params) : ''),
        result_summary: tc.result_summary ?? '',
        ms: tc.ms ?? tc.duration_ms ?? 0,
      })
      scrollToBottom()
    }
  } else if (type === 'done') {
    doneInfo.value = (data && typeof data === 'object') ? data : {}
  } else if (type === 'change') {
    // 变更工单事件：{ticket_id,title,change_list:[{idx,title,impact,rollback,ack_required}]}
    const d = (data && typeof data === 'object') ? (data.ticket || data) : null
    const t = upsertTicket(d, current.value?.id)
    if (t) scrollToBottom()
  } else if (type === 'error') {
    const code = data && typeof data === 'object' ? data.code : null
    if (code === 'NEED_MODEL') {
      needModel.value = true    // 引导卡替代输入框（不做假聊天）
    } else {
      errorMsg.value = (data && typeof data === 'object' ? (data.message || data.detail) : '') || t('agent.msg.gen_failed')
    }
  }
}

// 流结束（正常/停止/异常）：保留已生成内容为一条助手消息
function finalizeStream() {
  const content = streamBuf.value
  const tools = streamTools.value.slice()
  if (streamActive.value && (content || tools.length)) {
    const id = 'a' + Date.now()
    if (content) htmlCache[id] = md.render(content)
    messages.value.push({
      id,
      role: 'assistant',
      content_md: content,
      tool_calls: tools,
      model: doneInfo.value?.model || '',
      tokens: doneInfo.value?.tokens || 0,
      created_at: new Date().toISOString(),
    })
    if (current.value) {
      current.value.message_count = (current.value.message_count || 0) + 2
      current.value.last_active_at = new Date().toISOString()
    }
  }
  streamActive.value = false
  streamBuf.value = ''
  streamTools.value = []
  doneInfo.value = null
  if (renderTimer) { clearTimeout(renderTimer); renderTimer = null }
  streamHtml.value = ''
  scrollToBottom()
}

async function send() {
  const text = (input.value || '').trim()
  if (!text || sending.value || needModel.value) return
  sending.value = true
  errorMsg.value = ''
  streamBuf.value = ''
  streamHtml.value = ''
  streamTools.value = []
  doneInfo.value = null
  input.value = ''
  abortCtrl = new AbortController()
  try {
    if (!current.value) {
      const s = await createSession({ title: text.slice(0, 40) })
      current.value = s
      sessions.value = [{
        ...s,
        message_count: s.message_count || 0,
        last_active_at: s.last_active_at || new Date().toISOString(),
      }, ...sessions.value]
    }
    messages.value.push({
      id: 'u' + Date.now(), role: 'user', content_md: text,
      created_at: new Date().toISOString(),
    })
    streamActive.value = true
    scrollToBottom()
    await streamAgentMessage(current.value.id, {
      q: text, ctx: buildCtx(), onEvent: handleStreamEvent, signal: abortCtrl.signal,
    })
  } catch (e) {
    if (e?.name === 'AbortError') {
      // 停止按钮：中断当前生成，已生成内容由 finalizeStream 保留
    } else if (e?.code === 'NEED_MODEL') {
      needModel.value = true
    } else {
      errorMsg.value = e?.message || t('agent.msg.send_failed')
      // 失败且没产出任何内容：把提问放回输入框，用户改一下就能重试
      if (!streamBuf.value) input.value = text
    }
  } finally {
    finalizeStream()
    sending.value = false
    abortCtrl = null
  }
}

function stopGen() {
  abortCtrl?.abort()
  if (current.value?.id) abortSession(current.value.id).catch(() => { /* 拦截器已提示 */ })
}

/* ---------------- 变更工单（变更确认卡 + 右栏面板 + 会话红点） ---------------- */
const changeTickets = ref([])   // 跨会话全量变更工单（GET /agent/changes）
const wfResults = ref([])       // 本会话内工作流运行结果消息（kind='workflow'，含内嵌变更卡）
const canChange = computed(() => auth.hasCode('agent.change'))

/* ---------------- 共享接入任务：深链 / 最近 Run / 工作流引用 ---------------- */
// 任务恢复不经过模型。顶部只保留一个身份；详情接口返回的 capabilities 是卡片唯一事实源。
const canWriteOnboarding = computed(() => ['admin', 'operator'].includes(auth.role))
const canEditOnboardingCredentials = computed(() => auth.role === 'admin')
const onboardingAccountId = ref(0)
const onboardingRunId = ref(0)
const onboardingRun = ref(null)
const onboardingLoading = ref(false)
const onboardingError = ref('')
const workflowOnboardingTasks = reactive({})  // `${account_id}:${run_id}` -> 同 Run 的独立详情状态
let onboardingRequestSeq = 0
let onboardingTimer = null
let workflowOnboardingGeneration = 0
let workflowOnboardingTimer = null

const hasOnboardingTask = computed(() => !!(onboardingAccountId.value || onboardingRun.value))
const onboardingTaskKey = (accountId, runId) => `${Number(accountId)}:${Number(runId)}`
const sameOnboardingTask = (run, accountId, runId) => !!run
  && Number(run.account_id) === Number(accountId)
  && Number(run.id) === Number(runId)

function isOlderAttempt(incoming, current) {
  return !!current && Number(incoming?.attempts || 1) < Number(current.attempts || 1)
}

function updateOnboardingRun(run) {
  if (!sameOnboardingTask(run, onboardingAccountId.value, onboardingRunId.value)
    || isOlderAttempt(run, onboardingRun.value)) return
  onboardingRun.value = run
  const state = workflowOnboardingTasks[onboardingTaskKey(run.account_id, run.id)]
  if (state && !isOlderAttempt(run, state.run)) state.run = run
}

async function loadOnboardingTask() {
  const accountId = onboardingAccountId.value
  if (!accountId) return
  clearTimeout(onboardingTimer)
  const requestSeq = ++onboardingRequestSeq
  onboardingLoading.value = true
  onboardingError.value = ''
  try {
    const run = await getAccountOnboarding(accountId)
    // 切账号、切 Run 或重新刷新后，晚到响应不得覆盖当前身份。
    if (requestSeq !== onboardingRequestSeq || accountId !== onboardingAccountId.value
      || isOlderAttempt(run, onboardingRun.value)) return
    onboardingRun.value = run
    onboardingRunId.value = Number(run.id)
    const state = workflowOnboardingTasks[onboardingTaskKey(run.account_id, run.id)]
    if (state && !isOlderAttempt(run, state.run)) state.run = run
  } catch {
    // 网络失败保留账号、Run 与已有卡片内容；恢复后继续读取同一任务。
    if (requestSeq === onboardingRequestSeq && accountId === onboardingAccountId.value) {
      onboardingError.value = t('clouds.task.load_failed')
    }
  } finally {
    if (requestSeq === onboardingRequestSeq && accountId === onboardingAccountId.value) {
      onboardingLoading.value = false
      if (onboardingError.value || ['running', 'waiting'].includes(onboardingRun.value?.status)) {
        onboardingTimer = setTimeout(loadOnboardingTask, 2500)
      }
    }
  }
}

function selectOnboardingTask(accountId, runId = 0) {
  const aid = Number(accountId)
  const rid = Number(runId) || 0
  if (!Number.isInteger(aid) || aid <= 0) return
  clearTimeout(onboardingTimer)
  onboardingRequestSeq += 1
  onboardingAccountId.value = aid
  onboardingRunId.value = rid
  if (onboardingRun.value?.account_id !== aid || (rid && onboardingRun.value?.id !== rid)) {
    onboardingRun.value = null
  }
  loadOnboardingTask()
}

async function loadLatestOnboarding() {
  clearTimeout(onboardingTimer)
  const requestSeq = ++onboardingRequestSeq
  onboardingLoading.value = true
  onboardingError.value = ''
  try {
    const result = await listOnboardingRuns()
    if (requestSeq !== onboardingRequestSeq) return
    const latest = (result.items || [])[0]
    if (!latest) {
      onboardingAccountId.value = 0
      onboardingRunId.value = 0
      onboardingRun.value = null
      return
    }
    onboardingAccountId.value = Number(latest.account_id)
    onboardingRunId.value = Number(latest.id)
    if (!sameOnboardingTask(onboardingRun.value, latest.account_id, latest.id)) onboardingRun.value = null
    loadOnboardingTask()   // 最近列表只负责找身份，卡片仍独立读取账号详情。
  } catch {
    if (requestSeq === onboardingRequestSeq) onboardingError.value = t('clouds.task.load_failed')
  } finally {
    if (requestSeq === onboardingRequestSeq) {
      onboardingLoading.value = false
      if (onboardingError.value && onboardingAccountId.value) {
        onboardingTimer = setTimeout(loadOnboardingTask, 2500)
      }
    }
  }
}

function refreshOnboardingTask(ref) {
  const aid = Number(ref?.account_id)
  const rid = Number(ref?.run_id) || onboardingRunId.value
  if (aid > 0 && (aid !== onboardingAccountId.value || rid !== onboardingRunId.value)) {
    selectOnboardingTask(aid, rid)
  } else {
    loadOnboardingTask()
  }
}

function recoverOnboardingFromQuery(query) {
  const accountId = Number(query.account_id)
  if (Number.isInteger(accountId) && accountId > 0) {
    selectOnboardingTask(accountId, Number(query.run_id) || 0)
  } else {
    loadLatestOnboarding()
  }
}

function workflowOnboardingRef(m) {
  const artifact = m?.artifact || {}
  const embedded = artifact.onboarding_run || {}
  const taskRef = artifact.task_ref?.kind === 'onboarding_run'
    ? artifact.task_ref
    : (embedded.task_ref?.kind === 'onboarding_run' ? embedded.task_ref : {})
  const accountId = Number(embedded.account_id ?? taskRef.account_id)
  const runId = Number(embedded.id ?? taskRef.run_id)
  if (!Number.isInteger(accountId) || accountId <= 0 || !Number.isInteger(runId) || runId <= 0) return null
  return { account_id: accountId, run_id: runId, key: onboardingTaskKey(accountId, runId) }
}

function workflowArtifactRun(m) {
  const ref = workflowOnboardingRef(m)
  const run = m?.artifact?.onboarding_run
  // task_ref 只有摘要，不能冒充完整 Run；没有内嵌详情时先独立 GET。
  return ref && sameOnboardingTask(run, ref.account_id, ref.run_id) && Array.isArray(run.capabilities) ? run : null
}

function workflowOnboardingState(m) {
  const ref = workflowOnboardingRef(m)
  return ref ? workflowOnboardingTasks[ref.key] : null
}

function workflowOnboardingRun(m) {
  return workflowOnboardingState(m)?.run || workflowArtifactRun(m)
}

function isTopOnboardingRef(ref) {
  return !!ref && Number(ref.account_id) === Number(onboardingAccountId.value)
    && Number(ref.run_id) === Number(onboardingRunId.value)
}

const duplicateWorkflowOnboardingCards = computed(() => {
  const seen = new Set()
  const duplicates = new Set()
  for (const message of displayItems.value) {
    if (message.kind !== 'workflow') continue
    const ref = workflowOnboardingRef(message)
    if (!ref) continue
    if (seen.has(ref.key)) duplicates.add(String(message.id))
    else seen.add(ref.key)
  }
  return duplicates
})

function workflowCredentialForm(m) {
  const form = m?.artifact?.credential_form
  return form?.kind === 'cloud_credential_form' ? form : null
}

function openWorkflowCredentialForm(m) {
  if (!canEditOnboardingCredentials.value) return
  const query = workflowCredentialForm(m)?.route?.query || {}
  // 只消费结构化非秘密字段；即使旧产物含凭证，也绝不转发到 URL。
  router.push({ path: '/clouds', query: {
    new: '1', vendor: String(query.vendor || 'aliyun'),
    name: String(query.name || ''), region: String(query.region || ''),
  } })
}

function resetWorkflowOnboardingTasks() {
  workflowOnboardingGeneration += 1
  clearTimeout(workflowOnboardingTimer)
  for (const key of Object.keys(workflowOnboardingTasks)) delete workflowOnboardingTasks[key]
}

function currentWorkflowOnboardingRefs() {
  const cards = [...wfResults.value, ...messages.value.filter((m) => m.kind === 'workflow' || m.msg_kind === 'workflow_run')]
  const refs = new Map()
  for (const card of cards) {
    const ref = workflowOnboardingRef(card)
    if (!ref || refs.has(ref.key)) continue
    refs.set(ref.key, ref)
    if (!workflowOnboardingTasks[ref.key]) {
      workflowOnboardingTasks[ref.key] = { run: workflowArtifactRun(card), loading: false, error: '' }
    } else if (!workflowOnboardingTasks[ref.key].run && workflowArtifactRun(card)) {
      workflowOnboardingTasks[ref.key].run = workflowArtifactRun(card)
    }
  }
  return [...refs.values()]
}

async function loadWorkflowOnboardingTasks(onlyKey = '') {
  clearTimeout(workflowOnboardingTimer)
  const refs = currentWorkflowOnboardingRefs().filter((ref) => !onlyKey || ref.key === onlyKey)
  if (!refs.length) return
  const generation = workflowOnboardingGeneration
  await Promise.all(refs.map(async (ref) => {
    const state = workflowOnboardingTasks[ref.key]
    const stateRequestSeq = (state.requestSeq || 0) + 1
    state.requestSeq = stateRequestSeq
    state.loading = true
    state.error = ''
    try {
      const run = await getAccountOnboarding(ref.account_id)
      // 每个 Run 独立防晚到；手动刷新 B 不会取消仍在加载的 A。
      if (workflowOnboardingTasks[ref.key] !== state || state.requestSeq !== stateRequestSeq) return
      if (sameOnboardingTask(run, ref.account_id, ref.run_id) && !isOlderAttempt(run, state.run)) state.run = run
    } catch {
      if (workflowOnboardingTasks[ref.key] === state && state.requestSeq === stateRequestSeq) {
        state.error = t('clouds.task.load_failed')
      }
    } finally {
      if (workflowOnboardingTasks[ref.key] === state && state.requestSeq === stateRequestSeq) state.loading = false
    }
  }))
  if (generation !== workflowOnboardingGeneration) return
  const active = Object.values(workflowOnboardingTasks).some((state) =>
    state.error || ['running', 'waiting'].includes(state.run?.status))
  if (active) {
    clearTimeout(workflowOnboardingTimer)
    workflowOnboardingTimer = setTimeout(() => loadWorkflowOnboardingTasks(), 2500)
  }
}

function updateWorkflowOnboardingRun(ref, run) {
  if (!ref || !sameOnboardingTask(run, ref.account_id, ref.run_id)) return
  const state = workflowOnboardingTasks[ref.key]
  if (state && !isOlderAttempt(run, state.run)) state.run = run
}

function refreshWorkflowOnboardingTask(ref) {
  if (ref) loadWorkflowOnboardingTasks(ref.key)
}

function editOnboardingCredential(run) {
  router.push({ path: '/clouds', query: {
    account_id: String(run.account_id), run_id: String(run.id), edit: 'credential',
  } })
}

function confirmOnboardingLogs(run) {
  router.push({ path: '/bootstrap', hash: '#sls-workspace', query: {
    account_id: String(run.account_id), run_id: String(run.id),
  } })
}

onBeforeUnmount(() => {
  onboardingRequestSeq += 1
  workflowOnboardingGeneration += 1
  clearTimeout(onboardingTimer)
  clearTimeout(workflowOnboardingTimer)
})

const TERMINAL_STATUS = ['done', 'completed', 'finished', 'ok', 'success', 'skipped',
  'failed', 'error', 'partial', 'rolled_back', 'dismissed', 'cancelled', 'canceled']
const isUnfinished = (t) => !TERMINAL_STATUS.includes(t.status)

async function loadChanges() {
  try {
    const r = await listChanges()
    const byId = {}
    for (const t of (r.items || [])) {
      const n = normalizeTicket(t)
      if (n) byId[n.id] = n
    }
    // 本地已知（SSE/工作流刚产生的）但列表还没落库的工单保留，避免闪烁消失
    for (const t of changeTickets.value) if (!byId[t.id]) byId[t.id] = t
    changeTickets.value = Object.values(byId)
    // 工作流消息内嵌的变更卡与列表同源（刷新后按 id 重挂新对象，防止卡内状态变旧）
    // 含 listMessages 拼接的 workflow_run 持久化合成消息
    const wfMsgs = [...wfResults.value, ...messages.value.filter((m) => m.msg_kind === 'workflow_run')]
    for (const w of wfMsgs) {
      if (w.tickets?.length) w.tickets = w.tickets.map((t) => byId[t.id] || t)
    }
  } catch { /* 列表失败不打断主区（拦截器已提示） */ }
}

function upsertTicket(raw, sessionId) {
  const n = normalizeTicket(raw, sessionId)
  if (!n) return null
  const i = changeTickets.value.findIndex((t) => t.id === n.id)
  if (i >= 0) changeTickets.value[i] = { ...changeTickets.value[i], ...n }
  else changeTickets.value.push(n)
  return changeTickets.value[i >= 0 ? i : changeTickets.value.length - 1]
}

// 会话内变更卡（工作流结果卡内嵌的除外，避免重复渲染）
const sessionTickets = computed(() => {
  const sid = current.value?.id
  // 新会话（尚未落库）不挂任何历史工单：loadChanges 返回跨会话全量，
  // session_id 为空/0 的历史工单不能冒充本会话消息。
  if (sid == null) return []
  // 内嵌变更卡来自本地 wfResults 与 listMessages 合成的 workflow_run 卡
  // （其 tickets 由 loadChanges 同源重挂）；两处内嵌工单都不能再重复渲染独立卡。
  // run 已被真消息引用时，后端不会再合成对应消息。
  const wfCards = [...wfResults.value, ...messages.value.filter((m) => m.msg_kind === 'workflow_run')]
  const inWf = new Set(wfCards.flatMap((w) => (w.tickets || []).map((t) => t.id)))
  return changeTickets.value
    .filter((t) => t.session_id === sid && !inWf.has(t.id))
    .sort((a, b) => new Date(a.created_at || 0) - new Date(b.created_at || 0))
})

// 会话列表红点：该会话有未完成变更（pending/执行中等非终态）
const pendingSessionIds = computed(() => {
  const s = new Set()
  for (const t of changeTickets.value) if (t.session_id != null && isUnfinished(t)) s.add(t.session_id)
  return s
})

// 右栏变更工单面板：跨会话待处理（非终态）
const pendingTickets = computed(() => changeTickets.value
  .filter(isUnfinished)
  .sort((a, b) => new Date(b.created_at || 0) - new Date(a.created_at || 0)))

const CHG_STATUS_LABEL = {
  pending: 'agent.status.pending', draft: 'agent.status.pending', confirmed: 'agent.status.confirmed',
  executing: 'agent.status.executing', running: 'agent.status.executing', partial: 'agent.status.partial',
  failed: 'agent.status.failed', error: 'agent.status.failed',
}
const chgStatusLabel = (s) => (CHG_STATUS_LABEL[s] ? t(CHG_STATUS_LABEL[s]) : (s ? String(s) : t('agent.status.pending')))
const chgStatusType = (s) => ({
  pending: 'warning', draft: 'warning', confirmed: 'warning',
  executing: 'primary', running: 'primary', partial: 'warning',
  failed: 'danger', error: 'danger',
}[s] || 'info')

function onTicketChanged() {
  loadChanges()   // 回服务端取真状态（确认门/回滚以服务端为准）
}

function openSessionById(id) {
  const s = sessions.value.find((x) => String(x.id) === String(id))
  if (s) openSession(s)
  else {
    current.value = { id: Number(id), title: t('agent.session.placeholder', { id }) }
    messages.value = []
    wfResults.value = []
    resetWorkflowOnboardingTasks()
    loadMessages()
    loadChanges()
  }
}

/* ---------------- 工作流入口（面板 + 运行参数 + 结果消息） ---------------- */
const wfOpen = ref(false)
const wfLoading = ref(false)
const wfList = ref([])
const wfRuns = ref([])
const wfHighlight = ref('')      // 深链 ?workflow=key 高亮直达
const runOpen = ref(false)
const runWf = ref(null)          // 正在填参数的工作流
const runParams = ref([{ k: '', v: '' }])
const runBusy = ref(false)

async function openWorkflowPanel() {
  wfOpen.value = true
  await loadWorkflows()
}

async function loadWorkflows() {
  wfLoading.value = true
  try {
    const [w, r] = await Promise.all([
      listWorkflows().catch(() => ({ items: [] })),
      listWorkflowRuns().catch(() => ({ items: [] })),
    ])
    wfList.value = w.items || []
    wfRuns.value = (r.items || []).slice(0, 8)
    await loadSchedules()   // 定时列显示上次与下次执行时间
  } finally {
    wfLoading.value = false
  }
}

function openRun(w) {
  runWf.value = w
  runParams.value = [{ k: '', v: '' }]
  runOpen.value = true
}

function addParamRow() { runParams.value.push({ k: '', v: '' }) }
function removeParamRow(i) { runParams.value.splice(i, 1) }

const WF_STATUS_LABEL = {
  pending: 'agent.wfStatus.pending', running: 'agent.wfStatus.running', executing: 'agent.wfStatus.running',
  done: 'agent.wfStatus.done', success: 'agent.wfStatus.success', completed: 'agent.wfStatus.done',
  failed: 'agent.wfStatus.failed', error: 'agent.wfStatus.failed', skipped: 'agent.wfStatus.skipped',
}
const wfStatusLabel = (s) => (WF_STATUS_LABEL[s] ? t(WF_STATUS_LABEL[s]) : (s ? String(s) : '—'))
const wfStatusType = (s) => ({
  running: 'primary', executing: 'primary', pending: 'info',
  done: 'success', success: 'success', completed: 'success',
  failed: 'danger', error: 'danger', skipped: 'info',
}[s] || 'info')

async function doRun() {
  if (!runWf.value || runBusy.value) return
  const params = {}
  for (const row of runParams.value) {
    const k = String(row.k || '').trim()
    if (k) params[k] = row.v ?? ''
  }
  runBusy.value = true
  try {
    // 带 session_id 落库：运行记录挂会话（重开会话后工作流结果卡仍在——listMessages 拼接）
    const r = await runWorkflow(runWf.value.key, params, current.value?.id)
    const msg = {
      id: 'wf-run-' + (r.run_id ?? Date.now()),
      kind: 'workflow',
      role: 'assistant',
      wfName: runWf.value.name || runWf.value.key,
      runId: r.run_id ?? '—',
      status: r.status || 'done',
      steps: r.steps || [],
      artifact: r.artifact || null,
      tickets: [],
      created_at: new Date().toISOString(),
    }
    // 工作流产生的变更工单：内嵌变更卡渲染（与工单列表同源），并入红点/面板统计
    for (const raw of (r.tickets || [])) {
      const t = upsertTicket(raw, current.value?.id)
      if (t) msg.tickets.push(t)
    }
    wfResults.value.push(msg)
    loadWorkflowOnboardingTasks()
    runOpen.value = false
    wfOpen.value = false
    ElMessage.success(r.status === 'failed'
      ? t('agent.wf.run_failed', { name: msg.wfName })
      : t('agent.wf.run_done', { name: msg.wfName }))
    scrollToBottom()
    loadChanges()
    loadWorkflows()   // 刷新最近运行
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail?.message || e?.response?.data?.detail || e?.message || t('agent.wf.run_error'))
  } finally {
    runBusy.value = false
  }
}

function renderWfReport(m) {
  if (m._html === undefined) m._html = md.render(m.artifact?.report_md || '')
  return m._html
}

// 深链 ?workflow=key：面板直达并打开该工作流的运行参数框
async function openWorkflowByKey(key) {
  wfOpen.value = true
  wfHighlight.value = key
  await loadWorkflows()
  const w = wfList.value.find((x) => String(x.key) === String(key))
  if (w) openRun(w)
  else ElMessage.warning(t('agent.wf.not_found', { key }))
}

// 自定义工作流管理入口：admin 到编辑器页编排
function goWorkflowEditor() {
  wfOpen.value = false
  router.push('/workflow-editor')
}

/* ---------------- 工作流定时体检：定时运行、产物落库、有异动才通知 ---------------- */
const wfSchedules = ref({})     // workflow_key -> 定时配置（含上次/下次执行）
const schedOpen = ref(false)    // 定时设置对话框
const schedWf = ref(null)       // 正在配置定时的工作流
const schedForm = reactive({ interval_days: 1, hour: 9 })
const schedBusy = ref(false)

async function loadSchedules() {
  try {
    const r = await listSchedules()
    const map = {}
    for (const s of (r.items || [])) map[s.workflow_key] = s
    wfSchedules.value = map
  } catch { /* 面板容错：定时列表失败不挡工作流面板 */ }
}

const schedOf = (key) => wfSchedules.value[key] || null

const schedText = (key) => {
  const s = schedOf(key)
  if (!s) return t('agent.sched.disabled')
  if (!s.enabled) return t('agent.sched.stopped')
  const last = s.last_run_at ? t('agent.sched.last', { time: fmtShort(s.last_run_at) }) : t('agent.sched.never_run')
  const next = s.next_run_at ? t('agent.sched.next', { time: fmtShort(s.next_run_at) }) : t('agent.sched.pending')
  return `${last} · ${next}`
}

async function toggleSchedule(w, on) {
  const s = schedOf(w.key)
  if (!s && !on) return   // 未建定时却点"关"=无操作
  try {
    if (s) await patchSchedule(s.id, { enabled: !!on })
    else await createSchedule({ workflow_key: w.key, interval_days: 1, hour: 9, enabled: true })
    await loadSchedules()
    ElMessage.success(on
      ? t('agent.sched.enabled_msg', { name: w.name })
      : t('agent.sched.disabled_msg', { name: w.name }))
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail?.message || e?.response?.data?.detail || e?.message || t('agent.sched.config_failed'))
    loadSchedules()
  }
}

function openSched(w) {
  const s = schedOf(w.key)
  schedWf.value = w
  schedForm.interval_days = s?.interval_days ?? 1
  schedForm.hour = s?.hour ?? 9
  schedOpen.value = true
}

async function saveSched() {
  const s = schedOf(schedWf.value?.key)
  if (!s) return
  schedBusy.value = true
  try {
    await patchSchedule(s.id, {
      interval_days: Number(schedForm.interval_days) || 1,
      hour: Number(schedForm.hour) || 0,
    })
    await loadSchedules()
    schedOpen.value = false
    ElMessage.success(t('agent.sched.saved'))
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail?.message || e?.response?.data?.detail || e?.message || t('agent.sched.save_failed'))
  } finally {
    schedBusy.value = false
  }
}

/* ---------------- 系统快照（条 + 右栏） ---------------- */
const snapshot = ref(null)
const snapState = ref('loading')   // loading / ok / empty / error

async function loadSnapshot() {
  snapState.value = 'loading'
  try {
    const r = await getSystemSnapshot()
    const s = (r && typeof r === 'object' && r.snapshot) ? r.snapshot : r
    snapshot.value = s && typeof s === 'object' ? s : null
    snapState.value = snapshot.value && Object.keys(snapshot.value).length ? 'ok' : 'empty'
  } catch {
    snapState.value = 'error'
  }
}

// 快照 dict 字段以契约为准（云账号/数据源/资产/拓扑/最近变更）——键名留别名容错
const SNAP_SECTIONS = [
  { labelKey: 'agent.snap.clouds', keys: ['cloud_accounts', 'clouds', 'accounts', 'cloud_account'] },
  { labelKey: 'agent.snap.datasources', keys: ['data_sources', 'datasources', 'datasource', 'sources'] },
  { labelKey: 'agent.snap.assets', keys: ['assets'] },
  { labelKey: 'agent.snap.topology', keys: ['topology'] },
  { labelKey: 'agent.snap.changes', keys: ['recent_changes', 'changes', 'recent_change'] },
]

const snapshotSections = computed(() => {
  const s = snapshot.value || {}
  const used = new Set()
  const out = []
  for (const sec of SNAP_SECTIONS) {
    const k = sec.keys.find((kk) => s[kk] !== undefined)
    if (k) {
      used.add(k)
      out.push({ label: t(sec.labelKey), value: s[k] })
    }
  }
  return out
})

const snapshotExtras = computed(() => {
  const s = snapshot.value || {}
  return Object.keys(s)
    .filter((k) => !SNAP_SECTIONS.some((sec) => sec.keys.includes(k)))
    .map((k) => ({ key: k, value: s[k] }))
})

function snapCount(v) {
  if (v == null) return '0'
  if (typeof v === 'number') return String(v)
  if (Array.isArray(v)) return String(v.length)
  if (typeof v === 'object') return String(v.count ?? v.total ?? Object.keys(v).length)
  return String(v)
}

function snapDetailLines(v) {
  if (v == null) return []
  if (Array.isArray(v)) {
    return v.slice(0, 8).map((it) => {
      if (it && typeof it === 'object') return String(it.name || it.title || it.label || it.id || it.key || JSON.stringify(it).slice(0, 60))
      return String(it)
    })
  }
  if (typeof v === 'object') {
    return Object.entries(v).slice(0, 8).map(([k, val]) => {
      const s = Array.isArray(val) ? t('agent.snap.items_n', { n: val.length }) : (val && typeof val === 'object' ? '…' : String(val))
      return `${k}: ${s}`
    })
  }
  return [String(v)]
}

const snapshotSummary = computed(() => {
  const parts = snapshotSections.value.map((sec) => `${sec.label} ${snapCount(sec.value)}`)
  for (const e of snapshotExtras.value) parts.push(`${e.key} ${snapCount(e.value)}`)
  return parts.length ? parts.join(' · ') : t('agent.snap.empty')
})

/* ---------------- 工作台空态：今天建议 ---------------- */
// 空白聊天框让用户自己想问题=把"该看什么"的判断丢回给人。这里后端把
// 快照事实 + 待确认变更 + 账号同步异常 + 总览待办聚合成 3~5 张可行动卡片，
// 每条点「问千枢」直接发送（带系统快照回答），点「去处理」直达对应页面。
const suggestions = ref(null)
const sugState = ref('loading')   // loading / ok / error

async function loadSuggestions() {
  sugState.value = 'loading'
  try {
    const r = await getAgentSuggestions()
    suggestions.value = r && typeof r === 'object' ? r : null
    sugState.value = suggestions.value ? 'ok' : 'error'
  } catch {
    sugState.value = 'error'
  }
}

const userName = computed(() => auth.user?.display_name || auth.user?.username || '')
const greeting = computed(() => {
  const h = new Date().getHours()
  const k = h < 12 ? 'morning' : h < 18 ? 'afternoon' : 'evening'
  return t('agent.hero.title_' + k, { name: userName.value })
})

const heroFacts = computed(() => {
  const f = suggestions.value?.facts || {}
  const s = snapshot.value || {}
  return [
    { key: 'assets', label: t('agent.fact.assets'), value: f.assets ?? s.assets?.total ?? '—', tone: 'blue' },
    { key: 'clouds', label: t('agent.fact.clouds'), value: f.cloud_accounts ?? s.cloud_accounts?.total ?? '—', tone: 'violet' },
    { key: 'sources', label: t('agent.fact.datasources'), value: f.datasources ?? s.datasources?.enabled_count ?? '—', tone: 'teal' },
    // 快照失败时 alerts_firing 为 null：显示 '—' 且不能给绿色（会被读成“无告警一切正常”）
    { key: 'alerts', label: t('agent.fact.alerts'), value: f.alerts_firing ?? '—',
      tone: f.alerts_firing == null ? 'blue' : (f.alerts_firing > 0 ? 'red' : 'green') },
  ]
})

const sevTagType = (sev) => ({ critical: 'danger', warning: 'warning', success: 'success' }[sev] || 'info')

function askPrompt(text) {
  const q = String(text || '').trim()
  if (!q) return
  if (needModel.value) {
    ElMessage.warning(t('agent.need.short'))
    return
  }
  input.value = q
  send()
}

function askSuggestion(it) {
  if (!it) return
  askPrompt(it.prompt || it.title)
}

function goSuggestion(it) {
  if (!it) return
  if (it.action === 'tickets') {
    rightOpen.value = true
    loadChanges()
    return
  }
  if (it.link) router.push(it.link)
}

async function copyMsg(m) {
  try {
    await navigator.clipboard.writeText(m?.content_md || '')
    ElMessage.success(t('agent.msg.copied'))
  } catch {
    ElMessage.warning(t('agent.msg.copy_failed'))
  }
}

/* ---------------- 右栏：工具统计 ---------------- */
// 会话面板默认收起；展开为 el-drawer 无遮罩悬浮抽屉，不挤压消息宽度。
// 开关偏好记忆在 localStorage（qianshu.agent.panel_open：'1'开/'0'关；无记录=默认关）
const PANEL_OPEN_KEY = 'qianshu.agent.panel_open'
const rightOpen = ref(localStorage.getItem(PANEL_OPEN_KEY) === '1')
watch(rightOpen, (v) => { localStorage.setItem(PANEL_OPEN_KEY, v ? '1' : '0') })

const toolStats = computed(() => {
  const by = {}
  let total = 0
  let totalMs = 0
  const all = [...messages.value.map((m) => m.tool_calls || []), streamTools.value]
  for (const arr of all) {
    for (const t of arr) {
      total += 1
      totalMs += Number(t.ms) || 0
      const k = t.name || 'unknown'
      by[k] = by[k] || { count: 0, ms: 0 }
      by[k].count += 1
      by[k].ms += Number(t.ms) || 0
    }
  }
  return { total, totalMs, by }
})

/* ---------------- 输入区：场景 chips / 模型指示 ---------------- */
// 场景 chips：label/q 为 i18n 键（预填提问文本也随语言）
const SCENARIOS = [
  { labelKey: 'agent.chip.cost', qKey: 'agent.chip.cost_q' },
  { labelKey: 'agent.chip.diagnose', qKey: 'agent.chip.diagnose_q' },
  { labelKey: 'agent.chip.capacity', qKey: 'agent.chip.capacity_q' },
  { labelKey: 'agent.chip.onboard', qKey: 'agent.chip.onboard_q' },
  { labelKey: 'agent.chip.change_impact', qKey: 'agent.chip.change_impact_q' },
  { labelKey: 'agent.chip.weekly', qKey: 'agent.chip.weekly_q' },
]

function applyScenario(c) {
  input.value = t(c.qKey)
}

// 常用问法：动态建议卡为空或新用户不知道能问什么时，点击即可直接发送。
// 每个 i18n 键都必须同时提供 zh/en 文案。
const COMMON_QUESTIONS = [
  'agent.common.cpu_high', 'agent.common.cost_top5', 'agent.common.rds',
  'agent.common.alerts', 'agent.common.redis_mem',
  'agent.common.bill_compose', 'agent.common.bill_detail',
]

// 工具显示名：未登记的工具回落原始名，不隐藏信息
const TOOL_LABEL_KEYS = {
  query_assets: 'agent.tool.name.query_assets',
  query_metric: 'agent.tool.name.query_metric',
  query_cost: 'agent.tool.name.query_cost',
  query_alert_history: 'agent.tool.name.query_alert_history',
  chat: 'agent.tool.name.chat',
  bill_breakdown: 'agent.tool.name.bill_breakdown',
  bill_config: 'agent.tool.name.bill_config',
  cost_anomaly_list: 'agent.tool.name.cost_anomaly_list',
  pack_pools: 'agent.tool.name.pack_pools',
  object_chain: 'agent.tool.name.object_chain',
}
const toolLabel = (name) => (TOOL_LABEL_KEYS[name] ? t(TOOL_LABEL_KEYS[name]) : name)

const modelLabel = computed(() => {
  for (let i = messages.value.length - 1; i >= 0; i--) {
    const m = messages.value[i]
    if (m.role === 'assistant' && m.model) return m.model
  }
  return t('agent.model.auto')
})

function goSettings() {
  router.push('/settings?tab=ai')
}

// 未配置 AI 模型：引导卡替代输入框。契约里 NEED_MODEL 由 SSE error 事件给出；
// 进页先按现有 /ai/settings 探测一次（无任何模型连接=必未配置），只读角色不探测（配置页不对其可见）。
async function probeModel() {
  if (auth.role === 'viewer') return
  try {
    const list = await listAiSettings()
    if (Array.isArray(list) && !list.length) needModel.value = true
  } catch { /* 探测失败不拦截输入，发问时后端 NEED_MODEL 兜底 */ }
}

/* ---------------- 深链入参 ---------------- */
let handledPath = ''
function handleQuery() {
  const full = route.fullPath
  if (full === handledPath) return
  handledPath = full
  const q = route.query
  recoverOnboardingFromQuery(q)
  let ctx = null
  if (q.ctx) {
    try { ctx = JSON.parse(String(q.ctx)) } catch { ctx = { raw: String(q.ctx) } }
  }
  linkCtx.value = ctx
  const sess = q.session ? String(q.session) : ''
  const prefill = q.q ? String(q.q).slice(0, 2000) : ''
  if (sess && sess !== 'new') {
    const s = sessions.value.find((x) => String(x.id) === sess)
    if (s) openSession(s)
    else {
      // 与 openSessionById 共用 i18n 占位标题，避免英文界面出现硬编码中文
      current.value = { id: Number(sess), title: t('agent.session.placeholder', { id: sess }) }
      resetWorkflowOnboardingTasks()
      loadMessages()
    }
  }
  if (prefill) {
    input.value = prefill
    if (sess === 'new') send()   // 深链自动发送：落到新会话首条
  }
  const wf = q.workflow ? String(q.workflow) : ''
  if (wf) openWorkflowByKey(wf)   // 工作流深链直达：面板 + 运行参数框
}

onMounted(() => {
  loadSessions()
  loadSnapshot()
  loadSuggestions()
  loadChanges()
  probeModel()
  handleQuery()
})
watch(() => route.fullPath, handleQuery)
</script>

<template>
  <div class="agent-page">
    <div class="cols">
      <!-- 左栏：会话列表 -->
      <aside class="left">
        <div class="left-head">
          <el-button type="primary" class="new-btn" @click="newSession">{{ t('agent.btn.new_session') }}</el-button>
        </div>
        <el-input v-model="search" class="left-search" size="small" clearable :placeholder="t('agent.session.search_ph')" />
        <div class="session-list">
          <template v-for="g in groupedSessions" :key="g.label">
            <div class="group-label">{{ t('agent.session.bucket_' + g.label) }}</div>
            <div
              v-for="s in g.items"
              :key="s.id"
              class="session-item"
              :class="{ active: current?.id === s.id }"
              @click="openSession(s)"
            >
              <div class="s-title">
                <span v-if="pendingSessionIds.has(s.id)" class="s-dot" :title="t('agent.session.has_changes')" />
                <el-tag v-if="s.pinned" size="small" type="warning" effect="plain" class="pin-tag">{{ t('agent.session.pinned') }}</el-tag>
                <span class="s-name">{{ s.title || t('agent.session.new') }}</span>
              </div>
              <div class="s-meta">
                <span class="sub">{{ fmtShort(s.last_active_at) }}</span>
                <span v-if="s.message_count" class="sub">{{ t('agent.session.msg_count', { n: s.message_count }) }}</span>
                <span class="s-actions" @click.stop>
                  <el-button size="small" text type="primary" @click="togglePin(s)">{{ s.pinned ? t('agent.session.unpin') : t('agent.session.pin') }}</el-button>
                  <el-button size="small" text type="primary" @click="renameSession(s)">{{ t('agent.session.rename') }}</el-button>
                  <el-button size="small" text type="danger" @click="removeSession(s)">{{ t('agent.btn.delete') }}</el-button>
                </span>
              </div>
            </div>
          </template>
          <div v-if="!groupedSessions.length" class="empty">
            {{ search ? t('agent.session.no_match', { kw: search }) : t('agent.session.empty') }}
          </div>
        </div>
      </aside>

      <!-- 中栏：消息流 -->
      <main class="center">
        <div class="center-head">
          <span class="c-title">{{ current?.title || t('agent.session.new') }}</span>
          <span v-if="current?.id" class="sub">{{ t('agent.session.hash', { id: current.id }) }}</span>
          <div class="head-right">
            <el-button size="small" @click="rightOpen = !rightOpen">{{ rightOpen ? t('agent.panel.collapse') : t('agent.panel.title') }}</el-button>
          </div>
        </div>

        <div ref="streamEl" class="stream">
          <!-- 无模型也恢复确定性接入任务；同一 Run 只更新这张顶部卡。 -->
          <section v-if="hasOnboardingTask" class="agent-onboarding">
            <TaskResultCard
              :run="onboardingRun" :can-write="canWriteOnboarding"
              :can-edit-credentials="canEditOnboardingCredentials"
              :loading="onboardingLoading" :load-error="onboardingError"
              @update:run="updateOnboardingRun" @refresh="refreshOnboardingTask"
              @edit-credential="editOnboardingCredential" @confirm-logs="confirmOnboardingLogs"
            />
          </section>

          <!-- 系统快照条（会话首条；空态由下方 hero 事实卡承担，不重复） -->
          <div v-if="displayItems.length && snapState === 'ok'" class="snap-strip">
            <b>{{ t('agent.snap.title') }}</b>
            <span class="snap-summary">{{ snapshotSummary }}</span>
          </div>
          <div v-else-if="displayItems.length && snapState === 'error'" class="snap-strip snap-bad">
            {{ t('agent.snap.error') }}
          </div>
          <div v-else-if="displayItems.length && snapState === 'empty'" class="snap-strip snap-bad">
            {{ t('agent.snap.empty_state') }}
          </div>

          <!-- 空态 = 工作台首页：快照事实 + 今天建议 + 常用动作 -->
          <div v-if="!displayItems.length && !hasOnboardingTask" class="start">
            <div class="hero">
              <div class="hero-eyebrow">
                <el-icon><MagicStick /></el-icon>
                <span>{{ t('agent.hero.eyebrow') }}</span>
              </div>
              <h1 class="hero-title">{{ greeting }}</h1>
              <p class="hero-sub">{{ t('agent.hero.sub') }}</p>
              <div class="facts">
                <div v-for="f in heroFacts" :key="f.key" class="fact" :class="'fact-' + f.tone">
                  <div class="fact-num">{{ f.value }}</div>
                  <div class="fact-label">{{ f.label }}</div>
                </div>
              </div>
              <div v-if="snapState === 'error'" class="hero-warn">{{ t('agent.snap.error') }}</div>
            </div>

            <div class="sug-head">
              <span class="sug-title">{{ t('agent.sugg.title') }}</span>
              <span class="sub">{{ t('agent.sugg.sub') }}</span>
              <el-button
                text size="small" class="sug-refresh" :loading="sugState === 'loading'"
                @click="loadSuggestions"
              >
                <el-icon><Refresh /></el-icon>
              </el-button>
            </div>

            <div v-if="sugState === 'error'" class="sug-note">
              {{ t('agent.sugg.error') }}
            </div>
            <div v-else-if="sugState === 'loading' && !suggestions" class="sug-note">
              {{ t('agent.sugg.loading') }}
            </div>
            <div v-else-if="!(suggestions?.items || []).length" class="sug-note">
              {{ t('agent.sugg.empty') }}
            </div>
            <div v-else class="sug-list">
              <div
                v-for="it in suggestions.items" :key="it.key"
                class="sug-card" :class="'sev-' + it.sev"
              >
                <span class="sug-dot" />
                <div class="sug-body">
                  <div class="sug-card-head">
                    <el-tag size="small" effect="plain" :type="sevTagType(it.sev)">{{ it.cat }}</el-tag>
                    <b class="sug-card-title">{{ it.title }}</b>
                  </div>
                  <div v-if="it.detail" class="sug-detail">{{ it.detail }}</div>
                  <div class="sug-actions">
                    <el-button size="small" type="primary" plain @click="askSuggestion(it)">
                      {{ t('agent.sugg.ask') }}
                    </el-button>
                    <el-button v-if="it.link || it.action === 'tickets'" size="small" text @click="goSuggestion(it)">
                      {{ it.link_label || t('agent.sugg.go') }} →
                    </el-button>
                  </div>
                </div>
              </div>
            </div>

            <div class="quick">
              <div class="quick-title">{{ t('agent.sugg.quick') }}</div>
              <div class="quick-grid">
                <div
                  v-for="c in SCENARIOS" :key="c.labelKey" class="quick-card"
                  @click="askPrompt(t(c.qKey))"
                >
                  <span class="quick-name">{{ t(c.labelKey) }}</span>
                  <span class="quick-arrow">→</span>
                </div>
              </div>
              <!-- 常用问法：点击直接发送 -->
              <div class="quick-title" style="margin-top: 12px">{{ t('agent.common.title') }}</div>
              <div class="quick-grid common-grid">
                <span
                  v-for="k in COMMON_QUESTIONS" :key="k" class="common-chip"
                  @click="askPrompt(t(k))"
                >{{ t(k) }}</span>
              </div>
            </div>
          </div>

          <template v-for="m in displayItems" :key="m.id">
            <!-- 变更确认卡：清单逐项 + ack 警示 + 确认/勾选/取消 + 一键回滚 -->
            <div v-if="m.kind === 'change'" class="msg-row change-row">
              <AgentChangeCard :ticket="m.ticket" :can-change="canChange" @changed="onTicketChanged" />
            </div>
            <!-- 工作流结果卡：共享接入卡 + 默认折叠的详细报告；变更授权卡始终可见。 -->
            <div v-else-if="m.kind === 'workflow'" class="msg-row wf-row">
              <div class="wf-card">
                <div class="wf-head">
                  <b>{{ t('agent.wf.card_title', { name: m.wfName }) }}</b>
                  <el-tag size="small" :type="wfStatusType(m.status)">{{ wfStatusLabel(m.status) }}</el-tag>
                  <span class="sub">run #{{ m.runId }}</span>
                </div>
                <TaskResultCard
                  v-if="workflowOnboardingRef(m) && !isTopOnboardingRef(workflowOnboardingRef(m))
                    && !duplicateWorkflowOnboardingCards.has(String(m.id))"
                  class="wf-onboarding"
                  :run="workflowOnboardingRun(m)" :can-write="canWriteOnboarding"
                  :can-edit-credentials="canEditOnboardingCredentials"
                  :loading="workflowOnboardingState(m)?.loading || false"
                  :load-error="workflowOnboardingState(m)?.error || ''"
                  @update:run="updateWorkflowOnboardingRun(workflowOnboardingRef(m), $event)"
                  @refresh="refreshWorkflowOnboardingTask(workflowOnboardingRef(m))"
                  @edit-credential="editOnboardingCredential" @confirm-logs="confirmOnboardingLogs"
                />
                <div v-if="workflowCredentialForm(m)" class="wf-credential-action">
                  <el-button v-if="canEditOnboardingCredentials" type="primary" @click="openWorkflowCredentialForm(m)">
                    {{ t('agent.wf.open_credential_form') }}
                  </el-button>
                  <span v-else class="sub">{{ t('clouds.task.credential_admin_required', { action: t('agent.wf.open_credential_form') }) }}</span>
                  <span class="sub">{{ t('agent.wf.credential_form_hint') }}</span>
                </div>
                <el-collapse v-if="(m.steps || []).length || m.artifact" class="wf-details">
                  <el-collapse-item :title="t('agent.wf.detail_report')">
                    <template v-if="(m.steps || []).length">
                      <div class="wf-sec-title">{{ t('agent.wf.steps') }}</div>
                      <div v-for="(st, i) in m.steps" :key="i" class="wf-step">
                        <el-tag size="small" effect="plain" :type="wfStatusType(st.status)">{{ wfStatusLabel(st.status) }}</el-tag>
                        <b class="wf-step-name">{{ st.step }}</b>
                        <span class="wf-step-ms">{{ st.ms ?? 0 }} ms</span>
                        <div v-if="st.detail" class="wf-step-detail">{{ st.detail }}</div>
                      </div>
                    </template>
                    <template v-if="m.artifact">
                      <div class="wf-sec-title">{{ t('agent.wf.artifact') }}</div>
                      <div v-if="(m.artifact.points || []).length" class="wf-points">
                        <div v-for="(p, i) in m.artifact.points" :key="i" class="wf-point">· {{ p }}</div>
                      </div>
                      <!-- eslint-disable-next-line vue/no-v-html -->
                      <div v-if="m.artifact.report_md" class="wf-report markdown-body" v-html="renderWfReport(m)" />
                    </template>
                  </el-collapse-item>
                </el-collapse>
                <template v-if="(m.tickets || []).length">
                  <div class="wf-sec-title">{{ t('agent.wf.tickets') }}</div>
                  <AgentChangeCard
                    v-for="tk in m.tickets" :key="tk.id" :ticket="tk"
                    :can-change="canChange" @changed="onTicketChanged"
                  />
                </template>
              </div>
            </div>
            <!-- 用户气泡 -->
            <div v-else-if="m.role === 'user'" class="msg-row user-row">
              <div class="bubble-user">{{ m.content_md }}</div>
            </div>
            <!-- 助手回复 -->
            <div v-else class="msg-row asst-row">
              <div class="asst-line">
                <div class="avatar">枢</div>
                <div class="asst-main">
              <!-- 工具轨迹卡：默认折叠一行摘要，展开逐条 -->
              <div v-if="m.tool_calls?.length" class="tool-card">
                <div class="tool-summary" @click="toggleTool(m)">
                  <span class="tool-title">{{ t('agent.tool.title') }}</span>
                  <span class="tool-brief">{{ t('agent.tool.brief', { n: m.tool_calls.length, s: toolTotalSec(m) }) }}</span>
                  <span class="tool-caret">{{ openTool[m.id] ? t('agent.btn.collapse') : t('agent.btn.expand') }}</span>
                </div>
                <div v-if="openTool[m.id]" class="tool-list">
                  <div v-for="(tc, i) in m.tool_calls" :key="i" class="tool-item">
                    <div class="tool-head">
                      <b class="tool-name mono" :title="tc.name">{{ toolLabel(tc.name) }}</b>
                      <span class="tool-ms">{{ tc.ms ?? tc.duration_ms ?? 0 }} ms</span>
                    </div>
                    <div v-if="tc.params_summary" class="tool-line">{{ t('agent.tool.params') }}{{ tc.params_summary }}</div>
                    <div
                      v-if="tc.result_summary"
                      class="tool-line"
                      :class="{ expanded: toolItemOpen[m.id + '-' + i] }"
                    >
                      {{ t('agent.tool.result') }}{{ tc.result_summary }}
                      <el-link
                        v-if="(tc.result_summary || '').length > 120"
                        type="primary"
                        class="tool-more"
                        @click="toggleToolItem(m.id + '-' + i)"
                      >{{ toolItemOpen[m.id + '-' + i] ? t('agent.btn.collapse') : t('agent.btn.expand') }}</el-link>
                    </div>
                  </div>
                </div>
              </div>
              <!-- eslint-disable-next-line vue/no-v-html -->
              <div class="bubble-asst markdown-body" v-html="m.streaming ? streamHtml : renderHtml(m)" />
              <div class="msg-meta">
                <template v-if="m.streaming">{{ t('agent.stream.generating') }}</template>
                <template v-else>
                  {{ m.model ? t('agent.stream.model_prefix', { model: m.model }) : '' }}{{ m.tokens ? t('agent.stream.tokens_prefix', { n: m.tokens }) : '' }}{{ fmtTime(m.created_at) }}
                  <!-- 审查徽标：点击查看审查问题清单与断言标注 -->
                  <el-popover v-if="m.review" placement="top" width="380" trigger="click">
                    <template #reference>
                      <el-tag size="small" class="review-tag" effect="plain"
                              :type="m.review.pass ? 'success' : 'warning'">
                        {{ m.review.revised ? t('agent.review.revised') : t('agent.review.passed') }}
                      </el-tag>
                    </template>
                    <div class="review-pop">
                      <div v-for="(is, i) in (m.review.issues || [])" :key="i" class="review-issue">
                        <b>{{ is.kind }}</b>：{{ is.detail }}
                        <div class="sub">{{ is.suggestion }}</div>
                      </div>
                      <div v-if="!(m.review.issues || []).length" class="sub">{{ t('agent.review.no_issues') }}</div>
                    </div>
                  </el-popover>
                  <el-button text size="small" class="copy-btn" @click="copyMsg(m)">
                    <el-icon><CopyDocument /></el-icon>{{ t('agent.msg.copy') }}
                  </el-button>
                </template>
              </div>
                </div>
              </div>
            </div>
          </template>

          <div v-if="sending && !streamActive" class="thinking">{{ t('agent.stream.connecting') }}</div>
          <div v-if="errorMsg" class="err-line">{{ errorMsg }}</div>
        </div>

        <!-- 未配置 AI 模型：引导卡替代输入框（不做假聊天） -->
        <div v-if="needModel" class="need-card">
          <div class="need-title">{{ t('agent.need.title') }}</div>
          <div class="need-desc">
            {{ t('agent.need.desc') }}
          </div>
          <el-button type="primary" @click="goSettings">{{ t('agent.copilot.go_settings') }}</el-button>
        </div>

        <!-- 输入区 -->
        <div v-else class="input-area">
          <div class="chips">
            <el-tag
              v-for="c in SCENARIOS" :key="c.labelKey" class="chip" effect="plain"
              @click="applyScenario(c)"
            >{{ t(c.labelKey) }}</el-tag>
            <el-button size="small" class="wf-btn" @click="openWorkflowPanel">{{ t('agent.wf.panel_btn') }}</el-button>
          </div>
          <el-input
            v-model="input"
            type="textarea"
            :rows="3"
            resize="none"
            class="input-box"
            :placeholder="t('agent.input.ph')"
            @keydown.enter.exact.prevent="send()"
          />
          <div class="input-foot">
            <el-popover placement="top" :width="280" trigger="click">
              <template #reference>
                <span class="model-chip">{{ t('agent.model.chip', { model: modelLabel }) }}</span>
              </template>
              <div class="model-pop">
                <div>{{ t('agent.model.current_label') }}<b>{{ modelLabel }}</b></div>
                <div class="sub">{{ t('agent.model.pop_tip') }}</div>
                <el-button size="small" type="primary" @click="goSettings">{{ t('agent.model.settings_btn') }}</el-button>
              </div>
            </el-popover>
            <!-- 金额口径折叠展示，避免免责说明长期占用输入区版面 -->
            <el-popover placement="top" :width="380" trigger="click">
              <template #reference>
                <span class="note-chip">{{ t('agent.note.chip') }}</span>
              </template>
              <div class="sub">{{ t('agent.note.amount') }}</div>
            </el-popover>
            <div class="foot-right">
              <el-button v-if="sending" type="danger" plain @click="stopGen">{{ t('agent.btn.stop') }}</el-button>
              <el-button v-else type="primary" :disabled="!input.trim()" @click="send()">{{ t('agent.btn.send') }}</el-button>
            </div>
          </div>
        </div>
      </main>

      <!-- 右栏会话面板默认收起，展开为悬浮抽屉 overlay，不挤压消息流宽度 -->
      <el-drawer v-model="rightOpen" :title="t('agent.panel.title')" size="320px" :modal="false">
        <div class="rp-body">
        <div class="rp-sec">
          <div class="rp-title">{{ t('agent.snap.title') }}</div>
          <template v-if="snapState === 'ok'">
            <div class="rp-line snap-summary">{{ snapshotSummary }}</div>
            <div v-for="sec in snapshotSections" :key="sec.label" class="rp-block">
              <div class="rp-sub">{{ sec.label }}（{{ snapCount(sec.value) }}）</div>
              <div v-for="(l, i) in snapDetailLines(sec.value)" :key="i" class="rp-line mono">{{ l }}</div>
            </div>
            <div v-for="extra in snapshotExtras" :key="extra.key" class="rp-block">
              <div class="rp-sub">{{ extra.key }}（{{ snapCount(extra.value) }}）</div>
              <div v-for="(l, i) in snapDetailLines(extra.value)" :key="i" class="rp-line mono">{{ l }}</div>
            </div>
          </template>
          <div v-else class="rp-line sub">
            {{ snapState === 'error' ? t('agent.snap.error_short') : snapState === 'loading' ? t('agent.loading') : t('agent.snap.empty_short') }}
          </div>
        </div>
        <div class="rp-sec">
          <div class="rp-title">{{ t('agent.tool.stats_title') }}</div>
          <div v-if="!toolStats.total" class="rp-line sub">
            {{ t('agent.tool.stats_empty') }}
          </div>
          <template v-else>
            <div class="rp-line">{{ t('agent.tool.stats_total', { n: toolStats.total, s: (toolStats.totalMs / 1000).toFixed(1) }) }}</div>
            <div v-for="(v, k) in toolStats.by" :key="k" class="rp-tool-line">
              <span class="mono">{{ k }}</span>
              <span class="sub">{{ t('agent.tool.stats_row', { n: v.count, s: (v.ms / 1000).toFixed(1) }) }}</span>
            </div>
          </template>
        </div>
        <div class="rp-sec">
          <div class="rp-title">{{ t('agent.panel.tickets_title', { n: pendingTickets.length }) }}</div>
          <div v-if="!pendingTickets.length" class="rp-line sub">
            {{ t('agent.panel.tickets_empty') }}
          </div>
          <div
            v-for="tk in pendingTickets" :key="tk.id" class="rp-chg"
            :class="{ 'rp-chg-click': tk.session_id != null }"
            @click="tk.session_id != null && openSessionById(tk.session_id)"
          >
            <div class="rp-chg-line">
              <el-tag size="small" :type="chgStatusType(tk.status)">{{ chgStatusLabel(tk.status) }}</el-tag>
              <span class="rp-chg-title">{{ tk.title }}</span>
            </div>
            <div class="rp-chg-meta sub">
              {{ t('agent.panel.ticket_meta', { id: tk.session_id ?? '—', time: fmtShort(tk.created_at), n: (tk.change_list || []).length }) }}
            </div>
          </div>
        </div>
        </div>
      </el-drawer>
    </div>

    <!-- 工作流面板：内置工作流清单（名称/域/描述/最近运行）+ 运行记录 -->
    <el-dialog v-model="wfOpen" :title="t('agent.wf.panel_title')" width="720px" class="wf-dialog">
      <div v-if="wfLoading" class="wf-loading">{{ t('agent.loading') }}</div>
      <template v-else>
        <div v-if="!wfList.length" class="wf-empty">{{ t('agent.wf.none') }}</div>
        <div
          v-for="w in wfList" :key="w.key" class="wf-row-item"
          :class="{ 'wf-active': w.key === wfHighlight }"
        >
          <div class="wf-row-main">
            <b class="wf-name">{{ w.name }}</b>
            <el-tag size="small" effect="plain" class="wf-domain">{{ w.domain || t('agent.wf.generic') }}</el-tag>
            <el-tag v-if="w.custom" size="small" type="warning" effect="plain" class="wf-custom-tag">{{ t('agent.wf.custom_tag') }}</el-tag>
            <el-tag v-if="w.custom && w.enabled === false" size="small" type="info" effect="plain" class="wf-custom-tag">{{ t('agent.wf.disabled_tag') }}</el-tag>
          </div>
          <div class="wf-row-desc">{{ w.description || '—' }}</div>
          <div class="wf-row-meta">
            <span class="sub">{{ t('agent.wf.last_run') }}{{ w.last_run ? fmtTime(w.last_run.created_at) : t('agent.wf.never_run') }}</span>
            <span v-if="w.schedulable" class="wf-sched">
              <span class="sub">{{ t('agent.sched.label') }}</span>
              <el-switch
                class="wf-sched-switch"
                :disabled="!canRunWorkflow"
                :model-value="!!schedOf(w.key)?.enabled"
                @change="(v) => toggleSchedule(w, v)"
              />
              <span class="sub">{{ schedText(w.key) }}</span>
              <el-button v-if="schedOf(w.key) && canRunWorkflow" size="small" text type="primary" @click="openSched(w)">{{ t('agent.btn.settings') }}</el-button>
            </span>
            <el-button size="small" type="primary" class="wf-run-btn"
                       :disabled="!canRunWorkflow || (w.custom && w.enabled === false)"
                       @click="openRun(w)">{{ t('agent.btn.run') }}</el-button>
          </div>
        </div>
        <div class="wf-sec-title">{{ t('agent.wf.runs_title') }}</div>
        <div v-if="!wfRuns.length" class="wf-empty">{{ t('agent.wf.no_runs') }}</div>
        <div v-for="(r, i) in wfRuns" :key="i" class="wf-run-row">
          <el-tag size="small" effect="plain" :type="wfStatusType(r.status)">{{ wfStatusLabel(r.status) }}</el-tag>
          <span class="mono">{{ r.workflow_key || r.key || '—' }}</span>
          <span class="sub">{{ fmtTime(r.created_at || r.started_at || '') }}</span>
        </div>
        <div class="wf-manage-row">
          <el-button v-if="auth.user?.role === 'admin'" size="small" text type="primary" @click="goWorkflowEditor">{{ t('agent.wf.manage_btn') }}</el-button>
        </div>
      </template>
    </el-dialog>

    <!-- 工作流运行参数对话框：键值成对填，运行结果落消息流一条（步骤+产物+变更卡） -->
    <el-dialog
      v-model="runOpen" width="560px" :close-on-click-modal="false"
      :title="t('agent.wf.run_title', { name: runWf ? runWf.name : '' })"
    >
      <div class="run-desc">{{ runWf ? runWf.description : '' }}</div>
      <div class="wf-sec-title">{{ t('agent.wf.params_title') }}</div>
      <div v-for="(row, i) in runParams" :key="i" class="param-row">
        <el-input v-model="row.k" class="param-k" size="small" :placeholder="t('agent.wf.param_name')" />
        <el-input v-model="row.v" class="param-v" size="small" :placeholder="t('agent.wf.param_value')" />
        <el-button size="small" text type="danger" class="param-del" @click="removeParamRow(i)">{{ t('agent.btn.delete') }}</el-button>
      </div>
      <el-button size="small" text type="primary" class="param-add" @click="addParamRow">{{ t('agent.wf.add_param') }}</el-button>
      <template #footer>
        <el-button @click="runOpen = false">{{ t('agent.btn.cancel') }}</el-button>
        <el-button type="primary" :loading="runBusy" @click="doRun">{{ t('agent.btn.run') }}</el-button>
      </template>
    </el-dialog>

    <!-- 定时体检设置：间隔天数/执行时刻；定时运行、产物落库、有异动才发站内信 -->
    <el-dialog
      v-model="schedOpen" width="420px" :close-on-click-modal="false"
      :title="t('agent.sched.title', { name: schedWf ? schedWf.name : '' })"
    >
      <div class="run-desc">{{ t('agent.sched.desc_a') }}<b>{{ t('agent.sched.desc_b') }}</b>{{ t('agent.sched.desc_c') }}</div>
      <div class="param-row">
        <span class="sched-label">{{ t('agent.sched.interval') }}</span>
        <el-input-number v-model="schedForm.interval_days" :min="1" :max="365" size="small" />
      </div>
      <div class="param-row">
        <span class="sched-label">{{ t('agent.sched.hour') }}</span>
        <el-input-number v-model="schedForm.hour" :min="0" :max="23" size="small" />
      </div>
      <div class="sched-tip sub">{{ t('agent.sched.tip') }}</div>
      <template #footer>
        <el-button @click="schedOpen = false">{{ t('agent.btn.cancel') }}</el-button>
        <el-button type="primary" :loading="schedBusy" @click="saveSched">{{ t('agent.btn.save') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.agent-page {
  height: 100%;
  box-sizing: border-box;
  padding: 12px 14px;
}
.cols {
  display: flex;
  gap: 12px;
  height: 100%;
  overflow-x: auto;   /* 窄窗口兜底：三栏保最小可用宽，横向滚动而不是挤压消息流 */
}

/* ---- 左栏：会话列表 ---- */
.left {
  width: 240px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 12px;
  background: var(--el-bg-color-overlay);
  padding: 12px 10px;
}
.left-head {
  margin-bottom: 8px;
}
.new-btn {
  width: 100%;
}
.left-search {
  margin-bottom: 8px;
}
.session-list {
  flex: 1;
  overflow-y: auto;
}
.group-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin: 10px 4px 4px;
}
.session-item {
  padding: 8px 10px;
  border-radius: 8px;
  cursor: pointer;
  border: 1px solid transparent;
}
.session-item:hover {
  background: var(--el-fill-color-light);
}
.session-item.active {
  background: var(--el-color-primary-light-9);
  border-color: var(--el-color-primary-light-7);
}
.s-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
}
.s-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
}
.pin-tag {
  flex-shrink: 0;
}
.s-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 3px;
  min-height: 22px;
}
.s-actions {
  margin-left: auto;
  opacity: 0;
  display: flex;
}
.session-item:hover .s-actions {
  opacity: 1;
}
.empty {
  padding: 22px 10px;
  text-align: center;
  color: var(--el-text-color-secondary);
  font-size: 12.5px;
  line-height: 1.8;
}

/* ---- 中栏：消息流 ---- */
.center {
  flex: 1;
  min-width: 480px;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 12px;
  background: var(--el-bg-color-overlay);
}
.center-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 16px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.c-title {
  font-size: 15px;
  font-weight: 600;
}
.head-right {
  margin-left: auto;
}
.stream {
  flex: 1;
  overflow-y: auto;
  padding: 14px 18px;
}
.snap-strip {
  display: flex;
  align-items: baseline;
  gap: 10px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  background: var(--el-fill-color-lighter);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  padding: 8px 12px;
  margin-bottom: 14px;
}
.snap-bad {
  color: var(--el-color-warning);
}
.snap-summary {
  color: var(--el-text-color-regular);
}
.stream-empty {
  margin-top: 40px;
}
.msg-row {
  margin-bottom: 16px;
  display: flex;
  flex-direction: column;
}
.user-row {
  align-items: flex-end;
}
.asst-row {
  align-items: flex-start;
}
.bubble-user {
  max-width: 78%;
  background: var(--el-color-primary-light-9);
  color: var(--el-text-color-primary);
  border-radius: 12px 12px 2px 12px;
  padding: 10px 14px;
  font-size: 13.5px;
  line-height: 1.8;
  white-space: pre-wrap;
  word-break: break-word;
}
.bubble-asst {
  max-width: 92%;
  background: var(--el-fill-color-light);
  border-radius: 12px 12px 12px 2px;
  padding: 12px 16px;
  font-size: 13.5px;
}
.msg-meta {
  font-size: 11.5px;
  color: var(--el-text-color-secondary);
  margin-top: 5px;
}
.review-tag { margin-left: 8px; cursor: pointer; }
.review-pop { max-height: 260px; overflow-y: auto; }
.review-issue { padding: 4px 0; border-bottom: 1px solid var(--el-border-color-lighter); font-size: 12px; }
.review-issue:last-child { border-bottom: none; }
.review-issue .sub { color: var(--el-text-color-secondary); font-size: 11.5px; }
.thinking {
  color: var(--el-text-color-secondary);
  font-size: 13px;
  padding: 6px 2px;
}
.err-line {
  color: var(--el-color-danger);
  font-size: 13px;
  padding: 6px 2px;
}

/* 工具轨迹卡 */
.tool-card {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: var(--el-fill-color-lighter);
  margin-bottom: 8px;
  max-width: 92%;
  width: 560px;
}
.tool-summary {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  cursor: pointer;
  font-size: 12.5px;
}
.tool-title {
  font-weight: 600;
}
.tool-brief {
  color: var(--el-text-color-secondary);
  flex: 1;
}
.tool-caret {
  color: var(--el-color-primary);
  font-size: 12px;
}
.tool-list {
  border-top: 1px solid var(--el-border-color-lighter);
  padding: 4px 0;
}
.tool-item {
  padding: 8px 14px;
  border-bottom: 1px dashed var(--el-border-color-lighter);
}
.tool-item:last-child {
  border-bottom: none;
}
.tool-head {
  display: flex;
  align-items: center;
  gap: 10px;
}
.tool-name {
  font-size: 12.5px;
}
.tool-ms {
  margin-left: auto;
  font-size: 11.5px;
  color: var(--el-text-color-secondary);
}
.tool-line {
  font-size: 12px;
  color: var(--el-text-color-regular);
  margin-top: 3px;
  line-height: 1.7;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.tool-line.expanded {
  display: block;
  -webkit-line-clamp: unset;
  overflow: visible;
}
.tool-more {
  font-size: 12px;
  margin-left: 4px;
}

/* NEED_MODEL 引导卡 */
.need-card {
  border: 1px solid var(--el-color-warning-light-5);
  background: var(--el-color-warning-light-9);
  border-radius: 10px;
  padding: 16px 18px;
  margin: 12px 14px;
}
.need-title {
  font-size: 15px;
  font-weight: 600;
  margin-bottom: 6px;
}
.need-desc {
  font-size: 13px;
  color: var(--el-text-color-regular);
  line-height: 1.8;
  margin-bottom: 12px;
}

/* 输入区 */
.input-area {
  border-top: 1px solid var(--el-border-color-lighter);
  padding: 10px 14px 12px;
}
.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}
.input-box {
  width: 100%;
}
.chip {
  cursor: pointer;
}
.input-foot {
  display: flex;
  align-items: center;
  margin-top: 8px;
}
.model-chip {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  cursor: pointer;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 999px;
  padding: 3px 10px;
}
.model-chip:hover {
  color: var(--el-color-primary);
  border-color: var(--el-color-primary-light-5);
}
.model-pop {
  font-size: 12.5px;
  line-height: 1.8;
}
.foot-right {
  margin-left: auto;
  display: flex;
}

/* ---- 右栏：会话面板（悬浮抽屉内容区） ---- */
.rp-body {
  font-size: 13px;
}
.rp-sec {
  border-top: 1px solid var(--el-border-color-lighter);
  padding: 10px 0;
}
.rp-title {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 6px;
}
.rp-sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin: 8px 0 2px;
}
.rp-line {
  font-size: 12px;
  line-height: 1.8;
  word-break: break-all;
}
.rp-tool-line {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  font-size: 12px;
  padding: 2px 0;
}
.rp-block {
  margin-bottom: 2px;
}

/* ---- 变更卡 / 工作流卡 / 变更工单面板 ---- */
.s-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--el-color-danger);
  flex-shrink: 0;
}
.change-row {
  align-items: flex-start;
}
.wf-row {
  align-items: flex-start;
}
.agent-onboarding {
  max-width: 720px;
  margin-bottom: 16px;
}
.wf-onboarding {
  margin-top: 12px;
}
.wf-details {
  margin-top: 10px;
}
.wf-credential-action {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 12px;
}
.wf-card {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 10px;
  background: var(--el-fill-color-lighter);
  padding: 12px 14px;
  max-width: 92%;
  width: 620px;
  font-size: 13px;
}
.wf-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.wf-sec-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--el-text-color-secondary);
  margin: 10px 0 6px;
}
.wf-step {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 4px 0;
  border-bottom: 1px dashed var(--el-border-color-lighter);
  font-size: 12.5px;
}
.wf-step-name {
  font-size: 12.5px;
}
.wf-step-ms {
  margin-left: auto;
  font-size: 11.5px;
  color: var(--el-text-color-secondary);
}
.wf-step-detail {
  flex-basis: 100%;
  font-size: 12px;
  color: var(--el-text-color-regular);
  line-height: 1.7;
}
.wf-points {
  margin-bottom: 2px;
}
.wf-point {
  font-size: 12.5px;
  line-height: 1.9;
}
.wf-dialog :deep(.el-dialog__body) {
  max-height: 62vh;
  overflow-y: auto;
}
.wf-domain {
  flex-shrink: 0;
}
.param-del {
  flex-shrink: 0;
}
.wf-report {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  padding: 10px 12px;
  background: var(--el-bg-color-overlay);
  font-size: 12.5px;
}
.wf-btn {
  margin-left: 4px;
}
.rp-chg {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  padding: 6px 8px;
  margin-bottom: 6px;
}
.rp-chg-click {
  cursor: pointer;
}
.rp-chg-click:hover {
  border-color: var(--el-color-primary-light-5);
  background: var(--el-fill-color-light);
}
.rp-chg-line {
  display: flex;
  align-items: center;
  gap: 6px;
}
.rp-chg-title {
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rp-chg-meta {
  margin-top: 2px;
  font-size: 11.5px;
}
.wf-loading {
  padding: 20px;
  text-align: center;
  color: var(--el-text-color-secondary);
}
.wf-empty {
  padding: 14px 6px;
  text-align: center;
  color: var(--el-text-color-secondary);
  font-size: 12.5px;
}
.wf-row-item {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 10px;
  padding: 10px 12px;
  margin-bottom: 8px;
}
.wf-row-item.wf-active {
  border-color: var(--el-color-primary-light-5);
  background: var(--el-color-primary-light-9);
}
.wf-row-main {
  display: flex;
  align-items: center;
  gap: 8px;
}
.wf-name {
  font-size: 13.5px;
}
.wf-row-desc {
  font-size: 12.5px;
  color: var(--el-text-color-regular);
  line-height: 1.7;
  margin: 4px 0;
}
.wf-row-meta {
  display: flex;
  align-items: center;
}
.wf-run-btn {
  margin-left: auto;
}
.wf-custom-tag {
  flex-shrink: 0;
}
.wf-manage-row {
  margin-top: 8px;
  display: flex;
  justify-content: flex-end;
}
.wf-run-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 3px 2px;
  font-size: 12px;
}
.run-desc {
  font-size: 12.5px;
  color: var(--el-text-color-regular);
  line-height: 1.7;
}
.param-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.param-k {
  width: 180px;
}
.param-v {
  flex: 1;
}
.param-add {
  margin-top: 2px;
}

/* 定时体检（工作流面板定时列 + 设置对话框） */
.wf-sched {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-left: 12px;
}
.wf-sched-switch {
  margin: 0 2px;
}
.sched-label {
  width: 110px;
  font-size: 12.5px;
  color: var(--el-text-color-regular);
}
.sched-tip {
  margin-top: 8px;
  line-height: 1.7;
}

/* ---- 工作台空态：快照事实 + 今天建议 + 常用动作 ---- */
.start {
  max-width: 880px;
  margin: 0 auto;
  padding: 6px 0 18px;
}
.hero {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 16px;
  padding: 22px 24px 20px;
  background: radial-gradient(120% 150% at 0% 0%, var(--el-color-primary-light-9) 0%, var(--el-bg-color-overlay) 58%);
}
.hero-eyebrow {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11.5px;
  letter-spacing: 0.06em;
  font-weight: 600;
  color: var(--el-color-primary);
}
.hero-title {
  margin: 10px 0 6px;
  font-size: 24px;
  line-height: 1.3;
  font-weight: 650;
  color: var(--el-text-color-primary);
}
.hero-sub {
  margin: 0;
  max-width: 660px;
  font-size: 13px;
  line-height: 1.8;
  color: var(--el-text-color-regular);
}
.hero-warn {
  margin-top: 12px;
  font-size: 12px;
  color: var(--el-color-warning);
}
.facts {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
  margin-top: 18px;
}
.fact {
  position: relative;
  overflow: hidden;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 12px;
  padding: 10px 12px;
  background: var(--el-bg-color-overlay);
}
.fact::before {
  content: '';
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 3px;
  background: var(--el-color-primary);
  opacity: 0.85;
}
.fact-num {
  font-size: 22px;
  font-weight: 650;
  line-height: 1.2;
  font-variant-numeric: tabular-nums;
}
.fact-label {
  margin-top: 2px;
  font-size: 11.5px;
  color: var(--el-text-color-secondary);
}
.fact-blue::before { background: #3b82f6; }
.fact-violet::before { background: #6366f1; }
.fact-teal::before { background: #14b8a6; }
.fact-red::before { background: #ef4444; }
.fact-green::before { background: #22c55e; }

.sug-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin: 20px 2px 10px;
}
.sug-title {
  font-size: 15px;
  font-weight: 650;
}
.sug-head .sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.sug-refresh {
  margin-left: auto;
}
.sug-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.sug-card {
  display: flex;
  gap: 10px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 12px;
  padding: 12px 14px;
  background: var(--el-bg-color-overlay);
  transition: border-color 0.18s, box-shadow 0.18s, transform 0.18s;
}
.sug-card:hover {
  border-color: var(--el-color-primary-light-5);
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.06);
  transform: translateY(-1px);
}
.sug-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-top: 7px;
  flex-shrink: 0;
  background: var(--el-color-info);
}
.sev-critical .sug-dot {
  background: var(--el-color-danger);
  box-shadow: 0 0 0 3px var(--el-color-danger-light-9);
}
.sev-warning .sug-dot {
  background: var(--el-color-warning);
  box-shadow: 0 0 0 3px var(--el-color-warning-light-9);
}
.sev-success .sug-dot { background: var(--el-color-success); }
.sev-info .sug-dot { background: var(--el-color-primary); }
.sug-body {
  flex: 1;
  min-width: 0;
}
.sug-card-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.sug-card-title {
  font-size: 13.5px;
}
.sug-detail {
  margin-top: 5px;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--el-text-color-regular);
}
.sug-actions {
  display: flex;
  gap: 4px;
  margin-top: 8px;
}
.sug-note {
  border: 1px dashed var(--el-border-color);
  border-radius: 10px;
  padding: 14px;
  font-size: 12.5px;
  color: var(--el-text-color-secondary);
  text-align: center;
}
.quick {
  margin-top: 20px;
}
.quick-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-regular);
  margin-bottom: 8px;
}
.quick-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
}
.quick-card {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 10px;
  background: var(--el-bg-color-overlay);
  font-size: 12.5px;
  cursor: pointer;
  transition: border-color 0.18s, background 0.18s, color 0.18s;
}
.quick-card:hover {
  border-color: var(--el-color-primary-light-5);
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
}
.quick-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.quick-arrow {
  margin-left: 8px;
  color: var(--el-text-color-placeholder);
}
.quick-card:hover .quick-arrow {
  color: var(--el-color-primary);
}
/* 常用问法使用紧凑标签行，不抢动态建议卡的注意力 */
.common-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.common-chip {
  padding: 4px 10px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 999px;
  font-size: 12px;
  color: var(--el-text-color-regular);
  cursor: pointer;
  transition: border-color 0.18s, color 0.18s, background 0.18s;
}
.common-chip:hover {
  border-color: var(--el-color-primary-light-5);
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}
.note-chip {
  cursor: pointer;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  border-bottom: 1px dashed var(--el-border-color);
}
.note-chip:hover { color: var(--el-color-primary); }
@media (max-width: 900px) {
  .facts { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .quick-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}

/* ---- 助手头像 / 复制回答 ---- */
.asst-line {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  width: 100%;
}
.avatar {
  width: 30px;
  height: 30px;
  border-radius: 10px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  font-weight: 600;
  color: #fff;
  background: linear-gradient(135deg, var(--el-color-primary) 0%, var(--el-color-primary-light-3) 100%);
  box-shadow: 0 2px 8px rgba(64, 158, 255, 0.25);
}
.asst-main {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  min-width: 0;
  flex: 1;
}
.copy-btn {
  margin-left: 8px;
  padding: 0 4px;
  font-size: 11.5px;
}

/* 暗色主题柔化（html.dark 分作用域，不写组件级硬浅色） */
html.dark .bubble-user {
  background: var(--el-color-primary);
  color: #fff;
}
html.dark .session-item.active {
  background: var(--el-color-primary-light-3);
  border-color: var(--el-color-primary-light-5);
}
html.dark .tool-card {
  background: var(--el-fill-color-light);
}
html.dark .wf-card {
  background: var(--el-fill-color-light);
}
html.dark .wf-report {
  background: var(--el-fill-color-light);
}
html.dark .wf-row-item.wf-active {
  background: var(--el-color-primary-light-3);
}
</style>
