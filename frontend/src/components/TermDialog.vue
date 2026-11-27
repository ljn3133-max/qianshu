<script setup>
// 通用 WebShell 终端对话框：xterm + fit + resize 帧。
// 后端两个通道复用同一组件：机器 SSH / Pod exec（wsUrl 区分）。
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'

import { t } from '@/i18n'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  title: { type: String, default: () => t('k8s.term.title') },
  wsUrl: { type: String, default: '' },
})
const emit = defineEmits(['update:modelValue'])

const boxEl = ref(null)
let term = null
let fit = null
let ws = null

function connect() {
  teardown()
  nextTick(() => {
    if (!boxEl.value) return
    term = new Terminal({
      fontSize: 13, fontFamily: 'Consolas, Menlo, monospace',
      theme: { background: '#1e1e2e', foreground: '#d9e0ee', cursor: '#f5e9dc' },
      cursorBlink: true, scrollback: 2000,
    })
    fit = new FitAddon()
    term.loadAddon(fit)
    term.open(boxEl.value)
    fit.fit()
    term.focus()

    ws = new WebSocket(props.wsUrl)
    ws.onopen = () => {
      ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }))
    }
    ws.onmessage = (e) => {
      if (typeof e.data === 'string') term.write(e.data)
    }
    ws.onclose = () => term.write(`\r\n\x1b[33m${t('k8s.term.disconnected')}\x1b[0m\r\n`)
    ws.onerror = () => term.write(`\r\n\x1b[31m${t('k8s.term.connError')}\x1b[0m\r\n`)

    term.onData((d) => {
      if (ws && ws.readyState === 1) ws.send(d)
    })
    term.onResize(({ cols, rows }) => {
      if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: 'resize', cols, rows }))
    })
    window.addEventListener('resize', onWinResize)
  })
}

function onWinResize() {
  if (fit) fit.fit()
}

function teardown() {
  window.removeEventListener('resize', onWinResize)
  if (ws) { try { ws.close() } catch { /* 已关 */ } ws = null }
  if (term) { try { term.dispose() } catch { /* 已释放 */ } term = null }
  fit = null
}

watch(() => props.modelValue, (v) => {
  if (v) connect()
  else teardown()
})
onBeforeUnmount(teardown)

function close() {
  emit('update:modelValue', false)
}
</script>

<template>
  <el-dialog :model-value="modelValue" :title="title" width="860px" top="6vh"
             class="term-dialog" @close="close" :close-on-click-modal="false">
    <div ref="boxEl" class="term-box" />
    <template #footer>
      <span class="tip">{{ t('k8s.term.auditTip') }}</span>
      <el-button @click="close">{{ t('k8s.actions.close') }}</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.term-box {
  height: 460px; background: #1e1e2e; border-radius: 10px;
  padding: 8px 10px; overflow: hidden;
}
.tip { font-size: 12px; color: var(--el-text-color-secondary); margin-right: 12px; }
.term-dialog :deep(.el-dialog__body) { padding: 12px 16px; }
</style>
