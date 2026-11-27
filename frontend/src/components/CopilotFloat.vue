<script setup>
// 千枢智能体 · 全局浮窗：右下悬浮球，点击展开迷你对话。
// 自动带当前页上下文（route.path + query + params 组成 ctx）；小窗里可跳完整工作台 /agent。
// 挂载在 layout/MainLayout.vue；工作台页（/agent）自身不显示悬浮球，避免双入口打架。
// ⌘K/Ctrl+K 显式唤起不受此限——任意页可唤起，收起后 /agent 仍隐藏。
import MarkdownIt from 'markdown-it'
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { useRoute, useRouter } from 'vue-router'
import { MagicStick } from '@element-plus/icons-vue'

import { abortSession, createSession, listChanges, listMessages, streamAgentMessage } from '@/api/agent'
import { listAiSettings } from '@/api/ai'
import { useAuthStore } from '@/stores/auth'
import { t } from '@/i18n'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const md = new MarkdownIt({ html: false, breaks: true })

const open = ref(false)
const input = ref('')
const inputEl = ref(null)
const sending = ref(false)
const messages = ref([])      // {role, content_md}
const streamBuf = ref('')
const streamHtml = ref('')
const needModel = ref(false)
const errorMsg = ref('')
const boxEl = ref(null)
const sessionId = ref(Number(localStorage.getItem('qianshu.copilot.session') || 0) || null)
const pendingChanges = ref(0)   // 有未完成变更工单时悬浮球小红点（提示去工作台处理）
let abortCtrl = null
let renderTimer = null
let probed = false

// /agent 工作台不自动显示浮窗球（避免双入口），但 ⌘K 显式唤起仍可用
const visible = computed(() => route.path !== '/agent' || open.value)

// 快捷键提示随平台：mac ⌘K / 其他 Ctrl+K（挂浮窗按钮 tooltip）
const shortcutKeys = /Mac|iPhone|iPad|iPod/i.test(navigator.platform || navigator.userAgent || '')
  ? '⌘K' : 'Ctrl+K'

function ctxOf() {
  return {
    page: route.path,
    // meta.title 为 i18n 键：发给智能体的上下文用当前语言标题
    title: route.meta?.title ? t(route.meta.title) : '',
    query: route.query,
    params: route.params,
  }
}

// ⌘K 唤起（及点击悬浮球打开）时把当前页上下文预填成 seed prompt，光标就位可直接续写；
// 输入框已有内容则不覆盖（用户正在输入的优先）
function seedPrompt() {
  if ((input.value || '').trim()) return
  const c = ctxOf()
  const parts = []
  for (const [k, v] of Object.entries({ ...(c.query || {}), ...(c.params || {}) })) {
    if (v !== undefined && v !== null && v !== '') parts.push(`${k}=${Array.isArray(v) ? v.join(',') : v}`)
  }
  const params = parts.join('、')
  const title = c.title || c.page
  input.value = params
    ? t('agent.copilot.seed_params', { title, params })
    : t('agent.copilot.seed', { title })
}

function focusInput() {
  nextTick(() => {
    const inst = inputEl.value
    const el = inst?.textarea || inst?.input || inst?.$el?.querySelector?.('textarea, input')
    if (!el || typeof el.focus !== 'function') return
    el.focus()
    // 光标停在省略号前：续写即接在「请」后
    const val = String(el.value || '')
    const pos = val.endsWith('…') ? val.length - 1 : val.length
    try { el.setSelectionRange(pos, pos) } catch { /* 非文本元素无 selection 概念 */ }
  })
}

/* ---------------- 全局快捷键 ⌘K / Ctrl+K + Esc ----------------
 * 任意页面 ⌘K（mac）/Ctrl+K（win/linux）唤起或收起浮窗；打开即预填当前页 seed prompt；
 * Esc 关闭；快捷键提示挂在浮窗按钮 tooltip。
 *
 * 与未来输入框命令面板不冲突的约定：
 *  1) 本监听只占用「⌘/Ctrl+K」和「Esc」两个键，且挂在 window 冒泡阶段——
 *     输入框内未来的命令面板（"/" 唤起或输入框自身 ⌘K 行内面板）在自己的
 *     keydown 处理里 stopPropagation 即可完全接管，本监听收不到、天然不冲突；
 *  2) 若命令面板也挂 window 级监听又不想触发本浮窗：把事件 target 置于
 *     CMDK_GUARD_SEL 所列弹层内（或加 data-cmdk-guard 属性）即可让本监听让位；
 *  3) 不误伤组件内部按键：el-select 搜索/下拉/对话框等内部的 ⌘K/Escape
 *     按 target 守卫直接放行（普通输入框里 ⌘K 仍唤起——全局键在哪儿都该灵）。
 */
const CMDK_GUARD_SEL = '.el-select, .el-select-dropdown, .el-dropdown, .el-dropdown-menu,'
  + ' .el-popper, .el-dialog, .el-drawer, .el-overlay, [contenteditable="true"], [data-cmdk-guard]'

function onGlobalKey(e) {
  const tgt = e.target
  const guarded = !!(tgt && tgt.closest && tgt.closest(CMDK_GUARD_SEL))
  if (e.key === 'Escape') {
    if (open.value && !guarded) open.value = false
    return
  }
  if ((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey && String(e.key).toLowerCase() === 'k') {
    if (guarded) return
    e.preventDefault()   // 压浏览器默认（Chrome 聚焦地址栏搜索）
    if (open.value) open.value = false   // 再按一次收起
    else onOpen()                        // 展开并预填 seed prompt
  }
}

function scrollBottom() {
  nextTick(() => {
    const el = boxEl.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

watch(streamBuf, () => {
  if (renderTimer) return
  renderTimer = setTimeout(() => {
    renderTimer = null
    streamHtml.value = md.render(streamBuf.value)
    scrollBottom()
  }, 120)
})

function handleStreamEvent({ event, data }) {
  const type = event || 'message'
  if (type === 'token') {
    const t = typeof data === 'string' ? data : (data?.token ?? data?.text ?? data?.content ?? '')
    if (t) streamBuf.value += t
  } else if (type === 'error') {
    const code = data && typeof data === 'object' ? data.code : null
    if (code === 'NEED_MODEL') {
      needModel.value = true
    } else {
      errorMsg.value = (data && typeof data === 'object' ? (data.message || data.detail) : '') || t('agent.copilot.gen_failed')
    }
  }
}

function finalizeStream() {
  const content = streamBuf.value
  if (content) {
    messages.value.push({ role: 'assistant', content_md: content })
    scrollBottom()
  }
  streamBuf.value = ''
  streamHtml.value = ''
  if (renderTimer) { clearTimeout(renderTimer); renderTimer = null }
}

async function send() {
  const text = (input.value || '').trim()
  if (!text || sending.value || needModel.value) return
  sending.value = true
  errorMsg.value = ''
  input.value = ''
  abortCtrl = new AbortController()
  try {
    if (!sessionId.value) {
      const s = await createSession({ title: text.slice(0, 40) })
      sessionId.value = s.id
      localStorage.setItem('qianshu.copilot.session', String(s.id))
    }
    messages.value.push({ role: 'user', content_md: text })
    scrollBottom()
    await streamAgentMessage(sessionId.value, {
      q: text, ctx: ctxOf(), onEvent: handleStreamEvent, signal: abortCtrl.signal,
    })
  } catch (e) {
    if (e?.name === 'AbortError') {
      // 停止：保留已生成内容
    } else if (e?.code === 'NEED_MODEL') {
      needModel.value = true
    } else {
      errorMsg.value = e?.message || t('agent.copilot.send_failed')
    }
  } finally {
    finalizeStream()
    sending.value = false
    abortCtrl = null
  }
}

function stopGen() {
  abortCtrl?.abort()
  if (sessionId.value) abortSession(sessionId.value).catch(() => { /* 拦截器已提示 */ })
}

function newChat() {
  messages.value = []
  streamBuf.value = ''
  streamHtml.value = ''
  errorMsg.value = ''
  sessionId.value = null
  localStorage.removeItem('qianshu.copilot.session')
}

function openWorkbench() {
  open.value = false
  router.push(sessionId.value
    ? { path: '/agent', query: { session: String(sessionId.value) } }
    : { path: '/agent', query: { session: 'new' } })
}

function goSettings() {
  open.value = false
  router.push('/settings?tab=ai')
}

// 未完成变更工单数（悬浮球小红点）——失败静默，不打断浮窗主流程
const TERMINAL_STATUS = ['done', 'completed', 'finished', 'ok', 'success', 'skipped',
  'failed', 'error', 'partial', 'rolled_back', 'dismissed', 'cancelled', 'canceled']
async function refreshPending() {
  try {
    const r = await listChanges()
    pendingChanges.value = (r.items || []).filter((t) => !TERMINAL_STATUS.includes(t.status)).length
  } catch { /* 拿不到就不显红点 */ }
}

onMounted(() => {
  refreshPending()
  window.addEventListener('keydown', onGlobalKey)
})
onUnmounted(() => window.removeEventListener('keydown', onGlobalKey))

// 打开时续上本浮窗的会话历史（跨页面导航会话不丢）；并探测模型配置（只读角色不探测）
async function onOpen() {
  open.value = true
  seedPrompt()
  focusInput()
  refreshPending()
  if (sessionId.value && !messages.value.length) {
    try {
      const r = await listMessages(sessionId.value)
      messages.value = (r.items || []).map((m) => ({ role: m.role, content_md: m.content_md }))
      scrollBottom()
    } catch { /* 历史拿不到就当新对话 */ }
  }
  if (!probed && auth.role !== 'viewer') {
    probed = true
    try {
      const list = await listAiSettings()
      if (Array.isArray(list) && !list.length) needModel.value = true
    } catch { /* 探测失败不拦截 */ }
  }
}
</script>

<template>
  <div v-if="visible" class="copilot-float">
    <!-- 迷你对话小窗 -->
    <div v-if="open" class="cf-panel">
      <div class="cf-head">
        <b>{{ t('agent.copilot.title') }}</b>
        <span class="cf-ctx" :title="t('agent.copilot.ctx_tip', { path: route.path })">
          {{ t('agent.copilot.ctx_page', { title: route.meta?.title ? t(route.meta.title) : route.path }) }}
        </span>
        <el-button link type="primary" size="small" @click="openWorkbench">{{ t('agent.copilot.open_workbench') }}</el-button>
        <el-button link size="small" @click="newChat">{{ t('agent.copilot.new_chat') }}</el-button>
        <el-button link size="small" class="cf-close" @click="open = false">×</el-button>
      </div>
      <div ref="boxEl" class="cf-msgs">
        <div v-if="!messages.length && !streamBuf" class="cf-empty">
          {{ t('agent.copilot.empty', { title: route.meta?.title ? t(route.meta.title) : route.path }) }}
        </div>
        <div v-for="(m, i) in messages" :key="i" class="cf-msg">
          <div v-if="m.role === 'user'" class="cf-user">{{ m.content_md }}</div>
          <!-- eslint-disable-next-line vue/no-v-html -->
          <div v-else class="cf-asst markdown-body" v-html="md.render(m.content_md)" />
        </div>
        <div v-if="sending" class="cf-msg">
          <!-- eslint-disable-next-line vue/no-v-html -->
          <div class="cf-asst markdown-body" v-html="streamHtml || '…'" />
        </div>
        <div v-if="errorMsg" class="cf-err">{{ errorMsg }}</div>
      </div>
      <div v-if="needModel" class="cf-need">
        {{ t('agent.copilot.need_model') }}
        <el-button link type="primary" size="small" @click="goSettings">{{ t('agent.copilot.go_settings') }}</el-button>
      </div>
      <div v-else class="cf-input">
        <el-input
          ref="inputEl"
          v-model="input"
          type="textarea"
          :rows="2"
          resize="none"
          :placeholder="t('agent.copilot.input_ph')"
          @keydown.enter.exact.prevent="send()"
        />
        <div class="cf-foot">
          <span class="cf-hint">{{ t('agent.copilot.ctx_hint') }}</span>
          <el-button v-if="sending" size="small" type="danger" plain @click="stopGen">{{ t('agent.btn.stop') }}</el-button>
          <el-button v-else size="small" type="primary" :disabled="!input.trim()" @click="send()">{{ t('agent.btn.send') }}</el-button>
        </div>
      </div>
    </div>

    <!-- 悬浮球（tooltip 带快捷键提示） -->
    <div class="cf-ball" :title="t('agent.copilot.shortcut_tip', { keys: shortcutKeys })" @click="open ? (open = false) : onOpen()">
      <el-icon :size="22"><MagicStick /></el-icon>
      <span v-if="pendingChanges" class="cf-dot" :title="t('agent.copilot.pending_dot', { n: pendingChanges })" />
    </div>
  </div>
</template>

<style scoped>
.copilot-float {
  position: fixed;
  right: 22px;
  bottom: 22px;
  z-index: 1600;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 12px;
}
.cf-ball {
  width: 52px;
  height: 52px;
  border-radius: 50%;
  background: linear-gradient(135deg, #3b82f6, #2f6fe0);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  box-shadow: 0 6px 18px rgba(59, 130, 246, 0.40);
  transition: transform 0.15s ease;
  position: relative;
}
.cf-dot {
  position: absolute;
  top: 2px;
  right: 2px;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--el-color-danger);
  border: 2px solid var(--el-bg-color-overlay);
}
.cf-ball:hover {
  transform: scale(1.08);
}
.cf-panel {
  width: 380px;
  height: 520px;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 14px;
  background: var(--el-bg-color-overlay);
  box-shadow: var(--el-box-shadow);
  overflow: hidden;
}
.cf-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--el-border-color-lighter);
  font-size: 13px;
}
.cf-ctx {
  font-size: 11.5px;
  color: var(--el-text-color-secondary);
  background: var(--el-fill-color-light);
  border-radius: 999px;
  padding: 2px 8px;
  max-width: 110px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.cf-close {
  font-size: 16px;
  margin-left: auto;
}
.cf-msgs {
  flex: 1;
  overflow-y: auto;
  padding: 12px;
}
.cf-empty {
  color: var(--el-text-color-secondary);
  font-size: 12.5px;
  line-height: 1.8;
  padding: 18px 8px;
}
.cf-msg {
  margin-bottom: 10px;
}
.cf-user {
  margin-left: 48px;
  background: var(--el-color-primary-light-9);
  border-radius: 10px 10px 2px 10px;
  padding: 8px 12px;
  font-size: 12.5px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}
.cf-asst {
  background: var(--el-fill-color-light);
  border-radius: 10px 10px 10px 2px;
  padding: 8px 12px;
  font-size: 12.5px;
}
.cf-err {
  color: var(--el-color-danger);
  font-size: 12px;
  padding: 4px 2px;
}
.cf-need {
  padding: 10px 12px;
  font-size: 12.5px;
  color: var(--el-color-warning);
  border-top: 1px solid var(--el-border-color-lighter);
}
.cf-input {
  border-top: 1px solid var(--el-border-color-lighter);
  padding: 10px 12px;
}
.cf-foot {
  display: flex;
  align-items: center;
  margin-top: 8px;
}
.cf-hint {
  font-size: 11px;
  color: var(--el-text-color-secondary);
}
.cf-foot :deep(.el-button) {
  margin-left: auto;
}

/* 暗色主题柔化（html.dark 分作用域，不写组件级硬浅色） */
html.dark .cf-ball {
  box-shadow: 0 6px 18px rgba(59, 130, 246, 0.25);
}
html.dark .cf-user {
  background: var(--el-color-primary);
  color: #fff;
}
</style>
