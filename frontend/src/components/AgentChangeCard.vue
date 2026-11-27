<script setup>
// 千枢智能体 · 变更确认卡：消息流内的变更工单体。
// 清单逐项（类型图标 + 改什么 + 影响面 + 回滚点）；ack_required 项带云账号安全警示文案
// +「我已知晓」输入（口径与 CloudAccounts.vue 警示弹框一致）；操作=确认执行 / 逐项勾选执行 /
// 取消；执行中逐项状态 + 结果；完成后一键回滚（二次确认）。服务端确认门 403 的 detail 弹出提示。
// 状态由父组件持有（ticket 对象直接可变），本组件只管交互；变更后 emit('changed') 通知父刷新。
// code_modify 项=文件清单卡（路径 mono + +N/-N 徽标 +
// 可展开 diff 红绿预览，确认前可见）+ summary/impact +「需重启生效」提示；完成后「查看改动快照」
// 弹窗（GET code-snapshots 展示文件清单+diff）；「一键回滚」代码类走快照 rollback API（字节级恢复）；
// 「重启生效」applied 快照可点——二次确认后委托 copilotd 重启平台使改动生效。
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Delete, Document, EditPen, Plus, Refresh, Warning,
} from '@element-plus/icons-vue'

import {
  applyRestartCodeSnapshot, confirmChange, dismissChange, errMsg, listCodeSnapshots,
  rollbackChange, rollbackCodeSnapshot, skipChange,
} from '@/api/agent'
import { fmtTime } from '@/utils/format'
import { locale, t } from '@/i18n'

const props = defineProps({
  ticket: { type: Object, required: true },
  canChange: { type: Boolean, default: true },
})
const emit = defineEmits(['changed'])

const checked = reactive({})    // item.idx -> 是否勾选（逐项勾选执行，默认全选）
const ackTexts = reactive({})   // item.idx -> 「我已知晓」输入（ack_required 项）
const busy = ref(false)
const cardError = ref('')

/* ---- 敏感操作二次确认（凭证/密码/权限/删除类；后端 sensitive 标记，ack_required 也是敏感） ----
   口径：敏感项不走批量——预览「你接下来要做的操作」，逐项「继续」才执行（items=[该 idx]），
   「跳过」=该项不执行（不是 dismiss 整单）；批量确认只跑非敏感项。 */
const isSensitive = (it) => !!(it.sensitive || it.ack_required)
const hasSensitive = computed(() => list.value.some(isSensitive))
const plainList = computed(() => list.value.filter((it) => !isSensitive(it)))
// 跳过态落服务端：POST /agent/changes/{id}/skip {idx}
// 状态机 pending→skipped（不可执行）/ restore 回 pending（可恢复）——刷新/换设备后仍生效
const itSkipped = (it) => it.status === 'skipped'
const itDone = (it) => isItemDone(it)   // 终态（done/skipped 等）：不可再执行
const plainOpen = computed(() => plainList.value.filter((it) => !itDone(it)))

// 「我已知晓」输入校验：中文四字是协议短语（后端 ACK_TEXT 比较契约，固定不随语言变）；
// 英文界面额外接受 "I understand" 兜底——提示语写哪个词用户都不会输不过。confirm 上报 ack 固定传「我已知晓」。
function ackPhraseOk(v) {
  const s = String(v || '').trim()
  return s === '我已知晓' || (locale.value === 'en' && s === 'I understand')
}

// 参数摘要只显示掩码值（password/AK 类后端已掩码；前端再兜底——凭证永不回显明文）
function maskVal(k, v) {
  const key = String(k || '').toLowerCase()
  const s = v == null ? '' : String(v)
  if (/pass|secret|token|key|ak|sk|credential|auth/.test(key)) {
    return s.length > 4 ? `${s.slice(0, 2)}***${s.slice(-2)}` : '***'
  }
  if (s.length >= 20 && /^[A-Za-z0-9+/=_-]+$/.test(s)) return `${s.slice(0, 4)}***${s.slice(-2)}`
  return s
}
function paramLines(it) {
  // code_modify 的 params.plan 走文件清单/diff 预览渲染，不在参数摘要里堆对象
  return Object.entries(it.params || {})
    .filter(([k]) => k !== 'plan')
    .map(([k, v]) => `${k}=${maskVal(k, v)}`)
}

async function doContinue(it, i) {
  const key = itemKey(it, i)
  if (busy.value) return
  // ack_required 敏感项：必须先过「我已知晓」输入校验，没过不发 confirm
  if (it.ack_required && !ackPhraseOk(ackTexts[key])) {
    return ElMessage.warning(t('agent.ack.msg_need_continue'))
  }
  busy.value = true
  cardError.value = ''
  const prevStatus = props.ticket.status
  const prevItemStatus = it.status
  props.ticket.status = 'executing'
  if (!isItemDone(it)) it.status = 'executing'
  try {
    const body = { items: [it.idx] }
    if (it.ack_required) body.ack = '我已知晓'
    const r = await confirmChange(props.ticket.id, body)
    applyResults(r?.results)
    ElMessage.success(t('agent.msg.item_done', { name: it.title || t('agent.msg.this_item') }))
    emit('changed')
  } catch (e) {
    if (it.status === 'executing') it.status = prevItemStatus
    if (props.ticket.status === 'executing') props.ticket.status = prevStatus
    const msg = errMsg(e, t('agent.err.exec_failed'))
    cardError.value = msg
    if (e?.response?.status === 403) {
      try {
        await ElMessageBox.alert(msg, t('agent.err.confirm_rejected'), {
          type: 'warning', confirmButtonText: t('agent.btn.got_it'),
        })
      } catch { /* 用户关闭弹框 */ }
    }
    emit('changed')
  } finally {
    busy.value = false
  }
}

async function doSkip(it, i) {
  // 跳过=该项不执行（不 dismiss 整单）：走服务端 skip 接口落库（pending→skipped），
  // 刷新/换设备不丢；可「恢复」回 pending。emit('changed') 回服务端取真状态。
  if (busy.value) return
  busy.value = true
  cardError.value = ''
  try {
    const r = await skipChange(props.ticket.id, { idx: it.idx })
    it.status = r?.status || 'skipped'
    if (r?.ticket_status) props.ticket.status = r.ticket_status
    else applyResults([])
    ElMessage.success(t('agent.msg.item_skipped', { name: it.title || t('agent.msg.this_item') }))
    emit('changed')
  } catch (e) {
    cardError.value = errMsg(e, t('agent.err.skip_failed'))
  } finally {
    busy.value = false
  }
}

async function doRestore(it) {
  // 恢复：skipped→pending（回到可执行/可确认）；done 工单恢复跳过项会自动重开
  if (busy.value) return
  busy.value = true
  cardError.value = ''
  try {
    const r = await skipChange(props.ticket.id, { idx: it.idx, restore: true })
    it.status = 'pending'
    if (r?.ticket_status) props.ticket.status = r.ticket_status
    else applyResults([])
    ElMessage.success(t('agent.msg.item_restored', { name: it.title || t('agent.msg.this_item') }))
    emit('changed')
  } catch (e) {
    cardError.value = errMsg(e, t('agent.err.restore_failed'))
  } finally {
    busy.value = false
  }
}

const list = computed(() => props.ticket.change_list || [])
const itemKey = (it, i) => (it.idx ?? i)

// 默认全选（逐项勾选出厂即全量可执行，用户取消勾选=跳过该项）；刷新后新增项也默认勾上
function initChecked() {
  for (const [i, it] of list.value.entries()) {
    if (checked[itemKey(it, i)] === undefined) checked[itemKey(it, i)] = true
  }
}
initChecked()
watch(list, initChecked)

/* ---- 状态口径：pending/执行中/done/rolled_back 四态 + 其余兜底 ---- */
const STATUS_LABEL = {
  pending: 'agent.status.pending', draft: 'agent.status.pending', confirmed: 'agent.status.confirmed',
  executing: 'agent.status.executing', running: 'agent.status.executing',
  done: 'agent.status.done', completed: 'agent.status.done', finished: 'agent.status.done',
  ok: 'agent.status.ok', success: 'agent.status.ok', skipped: 'agent.status.skipped',
  failed: 'agent.status.failed', error: 'agent.status.failed', partial: 'agent.status.partial',
  rolled_back: 'agent.status.rolled_back', dismissed: 'agent.status.dismissed',
  cancelled: 'agent.status.dismissed', canceled: 'agent.status.dismissed',
}
const STATUS_TYPE = {
  pending: 'warning', draft: 'warning', confirmed: 'warning',
  executing: 'primary', running: 'primary',
  done: 'success', completed: 'success', finished: 'success', ok: 'success', success: 'success', skipped: 'info',
  failed: 'danger', error: 'danger', partial: 'warning',
  rolled_back: 'info', dismissed: 'info', cancelled: 'info', canceled: 'info',
}
const statusLabel = (s) => (STATUS_LABEL[s] ? t(STATUS_LABEL[s]) : (s ? String(s) : t('agent.status.pending')))
const statusType = (s) => STATUS_TYPE[s] || 'info'

const T_OPEN = ['pending', 'draft', 'confirmed', '', null, undefined]
const isItemDone = (it) => ['done', 'completed', 'finished', 'ok', 'success', 'skipped'].includes(it.status)
const openForConfirm = computed(() => T_OPEN.includes(props.ticket.status))
const rollbackable = computed(() => ['done', 'completed', 'finished', 'partial', 'failed', 'error'].includes(props.ticket.status)
  && list.value.some((it) => ['done', 'completed', 'finished', 'ok', 'success'].includes(it.status)))   // 无已执行项不可回滚
// 部分执行后（partial）仍有未处理项时保持可操作——敏感项逐项继续/跳过、失败项续跑
const anyOpenItem = computed(() => list.value.some((it) => !itDone(it) && it.status !== 'rolled_back'))
const editable = computed(() => props.canChange
  && (openForConfirm.value || busy.value || anyOpenItem.value))

const checkedIdxs = computed(() => list.value
  .map((it, i) => ({ it, i }))
  .filter(({ it, i }) => checked[itemKey(it, i)] && !isSensitive(it) && !itDone(it))   // 敏感项/已处理项不走批量
  .map(({ it }) => it.idx))

/* ---- L3b 平台自改管道（code_modify）：文件清单 + diff 预览 + 快照/回滚（方案 §2） ----
   diff 两源：SSE change 事件 diff_summary（权威 diff_head 首 20 行）优先；缺时从
   params.plan.files 的 edits（old/new）现算。params.plan 可能被 ticket_out 截断——
   预览按到手数据渲染，不依赖完整性。 */
const isCodeItem = (it) => it.tool === 'code_modify'
  || !!(planOf(it).files?.length) || !!(it.diff_summary?.files?.length) || !!it.diff_summary?.path
const hasCodeItem = computed(() => list.value.some(isCodeItem))

function planOf(it) {
  const p = it.params?.plan
  if (!p) return {}
  if (typeof p === 'string') {
    try { return JSON.parse(p) || {} } catch { return {} }   // 截断/序列化形态兜底
  }
  return p
}
function diffLinesOf(head) {
  if (Array.isArray(head)) return head.map((l) => String(l))
  if (typeof head === 'string' && head) return head.split(/\r?\n/)
  return []
}
// 归一 diff 文件清单：{files:[{path,add_lines,del_lines,diff_head}]} / [{path,diff_summary}] / 裸 {path,…}
function normFileDiffs(src) {
  if (!src) return []
  if (Array.isArray(src)) return src.flatMap(normFileDiffs)
  if (Array.isArray(src.files)) return src.files.flatMap(normFileDiffs)
  if (src.path) {
    const inner = (src.diff_summary && typeof src.diff_summary === 'object') ? src.diff_summary : src
    return [{
      path: String(src.path),
      add_lines: Number(inner.add_lines ?? src.add_lines ?? 0) || 0,
      del_lines: Number(inner.del_lines ?? src.del_lines ?? 0) || 0,
      diff_lines: diffLinesOf(inner.diff_head ?? src.diff_head),
    }]
  }
  return []
}
// params.plan.files.edits（old/new）现算 diff（首 20 行，与 diff_head 截断口径一致）
function planFileDiffs(it) {
  return (planOf(it).files || []).map((f) => {
    const lines = []
    let add = 0
    let del = 0
    for (const e of (f.edits || [])) {
      for (const l of (e?.old ? String(e.old).split('\n') : [])) {
        if (lines.length < 20) lines.push(`-${l}`)
        del += 1
      }
      for (const l of (e?.new ? String(e.new).split('\n') : [])) {
        if (lines.length < 20) lines.push(`+${l}`)
        add += 1
      }
    }
    return { path: String(f.path || ''), add_lines: add, del_lines: del, diff_lines: lines }
  })
}
function codeFiles(it) {
  let files = normFileDiffs(it.diff_summary)
  if (!files.length) files = normFileDiffs(props.ticket.diff_summary)
  if (!files.length) files = planFileDiffs(it)
  return files
}
function diffLineCls(ln) {
  const s = String(ln ?? '')
  if (/^@@/.test(s)) return 'diff-hunk'
  if (/^(\+\+\+|---)/.test(s)) return 'diff-meta'
  if (s.startsWith('+')) return 'diff-add'
  if (s.startsWith('-')) return 'diff-del'
  return 'diff-ctx'
}
// 「需重启生效」提示：回执带结构化标记、或回执文案含重启字样才显示
function needRestart(it) {
  return it.restart_required === true || /需重启|重启生效|restart/i.test(String(it.result || ''))
}
const codeSummary = (it) => String(planOf(it).summary || '')
const impactOf = (it) => it.impact || String(planOf(it).impact || '')

// diff 展开态（改动项 / 快照弹窗共用）：key=`${作用域}:${path}`；改动项默认展开——
// 敏感项「确认前」就能看 diff 再点「继续」，可收起
const diffOpen = reactive({})
const isDiffOpen = (k) => !!diffOpen[k]
const toggleDiff = (k) => { diffOpen[k] = !diffOpen[k] }
function initDiffOpen() {
  for (const [i, it] of list.value.entries()) {
    if (!isCodeItem(it)) continue
    for (const f of codeFiles(it)) {
      const k = `it-${itemKey(it, i)}:${f.path}`
      if (diffOpen[k] === undefined) diffOpen[k] = true
    }
  }
}
initDiffOpen()
watch(list, initDiffOpen)

// 改动快照弹窗（L3b）：GET code-snapshots 展示文件清单+diff（字节级回滚的依据）
const snapDialog = ref(false)
const snapItems = ref([])
const snapLoading = ref(false)
const snapError = ref('')
const showSnapLink = computed(() => hasCodeItem.value && (
  list.value.some((it) => isCodeItem(it) && (isItemDone(it) || it.status === 'rolled_back'))
  || ['done', 'completed', 'finished', 'partial', 'rolled_back'].includes(props.ticket.status)
))
const snapFiles = (s) => normFileDiffs(s && s.files)
function snapStatusType(s) {
  if (s.status === 'failed' || s.status === 'error') return 'danger'
  return s.verified ? 'success' : 'info'
}
async function openSnapDialog() {
  snapDialog.value = true
  snapLoading.value = true
  snapError.value = ''
  snapItems.value = []
  try {
    const r = await listCodeSnapshots({ ticket_id: props.ticket.id })
    snapItems.value = ((r && r.items) || [])
      .slice()
      .sort((a, b) => new Date(b.created_at || 0) - new Date(a.created_at || 0))
    // 弹窗默认展开 diff（弹窗目的就是看 diff）；可收起
    for (const s of snapItems.value) {
      for (const f of snapFiles(s)) {
        const k = `snap-${s.id}:${f.path}`
        if (diffOpen[k] === undefined) diffOpen[k] = true
      }
    }
  } catch (e) {
    snapError.value = errMsg(e, t('agent.code.snap_failed'))
  } finally {
    snapLoading.value = false
  }
}

// 「重启生效」：applied 快照 admin 二次确认 → 委托 copilotd 重启平台。
// 重启会短暂中断服务（服务端 POST /code-snapshots/{id}/apply-restart 亦强制 confirm=true）。
const restartingId = ref(0)
async function onApplyRestart(s) {
  try {
    await ElMessageBox.confirm(
      t('agent.confirm.restart_body'),
      t('agent.confirm.restart_title'),
      { type: 'warning', confirmButtonText: t('agent.btn.confirm_restart'), cancelButtonText: t('agent.btn.back') },
    )
  } catch { return }
  restartingId.value = s.id
  try {
    await applyRestartCodeSnapshot(s.id)
    ElMessage.success(t('agent.msg.restart_delegated'))
    snapDialog.value = false
  } catch (e) {
    snapError.value = errMsg(e, t('agent.err.restart_failed'))
  } finally {
    restartingId.value = 0
  }
}

/* ---- 类型图标（按「改什么」的人话关键词归类） ---- */
function typeIcon(title) {
  const t = String(title || '')
  if (/删除|移除|下线|解绑/.test(t)) return { icon: Delete, cls: 'ti-danger' }
  if (/新增|添加|创建|接入|登记|建立/.test(t)) return { icon: Plus, cls: 'ti-success' }
  if (/同步|刷新|重画|采集/.test(t)) return { icon: Refresh, cls: 'ti-primary' }
  if (/修改|更新|调整|配置|编辑|迁移/.test(t)) return { icon: EditPen, cls: 'ti-warning' }
  return { icon: Document, cls: 'ti-plain' }
}

/* ---- 确认执行 / 逐项勾选执行 ---- */
function ackOkFor(items) {
  return items
    .filter((it) => it.ack_required)
    .every((it) => ackPhraseOk(ackTexts[itemKey(it, list.value.indexOf(it))]))
}

function applyResults(results) {
  const byIdx = {}
  for (const r of results || []) byIdx[r.idx] = r
  for (const [i, it] of list.value.entries()) {
    const r = byIdx[it.idx]
    if (r) {
      it.status = r.status || 'done'
      it.result = r.result ?? it.result ?? ''
      if (r.restart_required != null) it.restart_required = r.restart_required === true   // L3b 需重启生效
    }
  }
  // 聚合按有效状态（跳过=服务端 skipped，计入已处理）——"全部敏感项处理完后卡状态按已完成项聚合显示"
  const sts = list.value.map((it) => it.status)
  if (sts.length && sts.every((s) => ['done', 'completed', 'finished', 'ok', 'success', 'skipped'].includes(s))) {
    props.ticket.status = 'done'
    props.ticket.confirmed_at = props.ticket.confirmed_at || new Date().toISOString()
  } else if (sts.some((s) => ['done', 'ok', 'success', 'failed', 'error', 'skipped'].includes(s))) {
    props.ticket.status = 'partial'
    props.ticket.confirmed_at = props.ticket.confirmed_at || new Date().toISOString()
  }
}

async function doConfirm(onlyChecked) {
  if (busy.value) return
  // 敏感二次确认：批量只跑非敏感项（敏感项逐项「继续」才执行，items=[该 idx]）；
  // 已处理项（done/skipped 等）不进执行集——skipped 不可执行（服务端也会拦）
  const items = onlyChecked
    ? list.value.filter((it) => checked[itemKey(it, list.value.indexOf(it))] && !isSensitive(it) && !itDone(it))
    : (hasSensitive.value ? plainOpen.value : list.value.filter((it) => !itDone(it)))
  if (onlyChecked && !items.length) return ElMessage.warning(t('agent.msg.check_items'))
  if (!onlyChecked && hasSensitive.value && !items.length) {
    return ElMessage.warning(t('agent.msg.no_batch_items'))
  }
  if (!ackOkFor(items)) {
    return ElMessage.warning(t('agent.ack.msg_need_confirm'))
  }
  busy.value = true
  cardError.value = ''
  // 执行中：逐项状态先行置「执行中」，结果回来逐项落状态
  const prevStatus = props.ticket.status
  const prevItems = items.map((it) => ({ it, status: it.status }))
  props.ticket.status = 'executing'
  for (const it of items) if (!isItemDone(it)) it.status = 'executing'
  try {
    const body = {}
    // 卡含敏感项时批量必须显式限定非敏感 idx（否则服务端会把敏感项一并执行）
    if (onlyChecked || hasSensitive.value) body.items = items.map((it) => it.idx)
    if (items.some((it) => it.ack_required)) body.ack = '我已知晓'
    const r = await confirmChange(props.ticket.id, body)
    applyResults(r?.results)
    ElMessage.success(t('agent.msg.exec_done'))
    emit('changed')
  } catch (e) {
    // 失败先回退假「执行中」态（防刷新也失败时卡死无按钮），服务端真状态由 loadChanges 兜底
    if (props.ticket.status === 'executing') props.ticket.status = prevStatus
    for (const { it, status } of prevItems) if (it.status === 'executing') it.status = status
    const msg = errMsg(e, t('agent.err.confirm_failed'))
    cardError.value = msg
    // 403=未过确认门/ack 不对：detail 提示弹出来（拦截器另有 toast，这里做强提醒）
    if (e?.response?.status === 403) {
      try {
        await ElMessageBox.alert(msg, t('agent.err.confirm_rejected'), {
          type: 'warning', confirmButtonText: t('agent.btn.got_it'),
        })
      } catch { /* 用户关闭弹框 */ }
    }
    emit('changed')   // 失败也回服务端取真状态，避免前端停在假「执行中」
  } finally {
    busy.value = false
  }
}

/* ---- 取消 / 一键回滚（均二次确认） ---- */
async function onCancel() {
  try {
    await ElMessageBox.confirm(
      t('agent.confirm.cancel_body'),
      t('agent.confirm.cancel_title'), { type: 'warning', confirmButtonText: t('agent.btn.confirm_cancel'), cancelButtonText: t('agent.btn.back') },
    )
  } catch { return }
  busy.value = true
  cardError.value = ''
  try {
    await dismissChange(props.ticket.id)
    props.ticket.status = 'dismissed'
    ElMessage.success(t('agent.msg.ticket_cancelled'))
    emit('changed')
  } catch (e) {
    cardError.value = errMsg(e, t('agent.err.cancel_failed'))
  } finally {
    busy.value = false
  }
}

async function onRollback() {
  // L3b 一键回滚扩展支持 code 类：代码项走 code-snapshots 回滚 API（逐文件字节级恢复，
  // 二次确认文案明示）；配置项沿用 /changes/{id}/rollback。混合工单两条都走，各自独立失败不互吞。
  const codeMode = hasCodeItem.value
  const cfgMode = list.value.some((it) => !isCodeItem(it) && isItemDone(it))
  try {
    await ElMessageBox.confirm(
      codeMode ? t('agent.confirm.rollback_code_body') : t('agent.confirm.rollback_body'),
      t('agent.confirm.rollback_title'), { type: 'warning', confirmButtonText: t('agent.btn.confirm_rollback'), cancelButtonText: t('agent.btn.back') },
    )
  } catch { return }
  busy.value = true
  cardError.value = ''
  const errs = []
  if (cfgMode) {
    try {
      await rollbackChange(props.ticket.id)
    } catch (e) {
      errs.push(errMsg(e, t('agent.err.rollback_failed')))
      if (e?.response?.status === 403) {
        try {
          await ElMessageBox.alert(errMsg(e, t('agent.err.rollback_denied')), t('agent.err.rollback_denied_title'), { type: 'warning', confirmButtonText: t('agent.btn.got_it') })
        } catch { /* 用户关闭弹框 */ }
      }
    }
  }
  if (codeMode) {
    try {
      const r = await listCodeSnapshots({ ticket_id: props.ticket.id })
      const items = ((r && r.items) || [])
        .slice()
        .sort((a, b) => new Date(b.created_at || 0) - new Date(a.created_at || 0))
      if (!items.length) errs.push(t('agent.err.no_code_snapshot'))
      // 逐快照回滚（新→旧）：把该工单全部改动面字节级恢复；同文件多次改动最后落到最早 before
      for (const s of items) await rollbackCodeSnapshot(s.id)
    } catch (e) {
      errs.push(errMsg(e, t('agent.err.rollback_failed')))
    }
  }
  if (errs.length) {
    cardError.value = errs.join('；')
  } else {
    props.ticket.status = 'rolled_back'
    for (const it of list.value) if (isItemDone(it) || it.status === 'failed') it.status = 'rolled_back'
    ElMessage.success(codeMode ? t('agent.msg.code_rolled_back') : t('agent.msg.rolled_back'))
  }
  emit('changed')
  busy.value = false
}
</script>

<template>
  <div class="chg-card">
    <div class="chg-head">
      <el-icon class="chg-warn-ico"><Warning /></el-icon>
      <b>{{ t('agent.card.title') }}</b>
      <span class="chg-count">{{ t('agent.card.count', { n: list.length }) }}</span>
      <el-tag size="small" :type="statusType(ticket.status)">{{ statusLabel(ticket.status) }}</el-tag>
      <span class="chg-time">{{ fmtTime(ticket.created_at) }}</span>
    </div>
    <div class="chg-title">{{ ticket.title }}</div>

    <div
      v-for="(it, i) in list" :key="itemKey(it, i)" class="chg-item"
      :class="{ 'chg-item-run': it.status === 'executing' || it.status === 'running' }"
    >
      <div class="chg-item-head">
        <el-checkbox v-if="editable && !isSensitive(it)" v-model="checked[itemKey(it, i)]" class="chg-check" />
        <span class="chg-ico" :class="typeIcon(it.title).cls">
          <el-icon><component :is="typeIcon(it.title).icon" /></el-icon>
        </span>
        <b class="chg-item-title">{{ i + 1 }}. {{ it.title }}</b>
        <el-tag
          v-if="it.status" size="small" effect="plain"
          :type="statusType(it.status)"
        >
          {{ statusLabel(it.status) }}
        </el-tag>
      </div>
      <div v-if="isCodeItem(it) && codeSummary(it)" class="chg-line">{{ t('agent.code.summary') }}{{ codeSummary(it) }}</div>
      <div class="chg-line">{{ t('agent.card.impact') }}{{ impactOf(it) || '—' }}</div>
      <div class="chg-line">{{ t('agent.card.rollback') }}{{ it.rollback || '—' }}</div>
      <div v-if="it.result" class="chg-line chg-result">{{ t('agent.card.result') }}{{ it.result }}</div>

      <!-- L3b 代码变更：文件清单卡（路径 mono + +N/-N 徽标 + 可展开 diff 红绿预览，确认前可见） -->
      <div v-if="isCodeItem(it) && codeFiles(it).length" class="code-box">
        <div class="code-box-head">
          <span class="code-box-title">{{ t('agent.code.files') }}</span>
          <span class="code-box-count">{{ t('agent.code.files_n', { n: codeFiles(it).length }) }}</span>
        </div>
        <div v-for="(f, fi) in codeFiles(it)" :key="f.path + fi" class="code-file">
          <div class="code-file-head" @click="toggleDiff(`it-${itemKey(it, i)}:${f.path}`)">
            <span class="code-path">{{ f.path }}</span>
            <span class="code-stat"><b class="code-add">+{{ f.add_lines }}</b><b class="code-del">-{{ f.del_lines }}</b></span>
            <span class="code-toggle">{{ isDiffOpen(`it-${itemKey(it, i)}:${f.path}`) ? t('agent.code.collapse_diff') : t('agent.code.expand_diff') }}</span>
          </div>
          <div v-if="isDiffOpen(`it-${itemKey(it, i)}:${f.path}`)" class="code-diff">
            <div v-for="(ln, li) in f.diff_lines" :key="li" class="diff-line" :class="diffLineCls(ln)">{{ ln }}</div>
            <div v-if="!f.diff_lines.length" class="diff-line diff-meta">{{ t('agent.code.diff_empty') }}</div>
          </div>
        </div>
      </div>
      <div v-if="isCodeItem(it) && needRestart(it)" class="code-restart">{{ t('agent.code.need_restart') }}</div>

      <!-- ack_required 项：云账号安全警示 + 「我已知晓」输入（口径同 CloudAccounts.vue） -->
      <div v-if="it.ack_required" class="ack-warn">
        <el-alert type="warning" :closable="false" show-icon class="ack-alert">
          <template #title>{{ t('agent.ack.readonly_a') }}<b>{{ t('agent.ack.readonly_b') }}</b>{{ t('agent.ack.readonly_c') }}</template>
        </el-alert>
        <div class="ack-body">
          <p>{{ t('agent.ack.p1_a') }}<b>{{ t('agent.ack.p1_b') }}</b>{{ t('agent.ack.p1_c') }}</p>
          <p>{{ t('agent.ack.p2_a') }}<b>{{ t('agent.ack.p2_b') }}</b>{{ t('agent.ack.p2_c') }}</p>
          <p>{{ t('agent.ack.p3') }}</p>
          <p>{{ t('agent.ack.p4_a') }}<b>{{ t('agent.ack.p4_b') }}</b>{{ t('agent.ack.p4_c') }}</p>
        </div>
        <el-input
          v-if="editable && !itDone(it)"
          v-model="ackTexts[itemKey(it, i)]"
          class="ack-input"
          :placeholder="t('agent.ack.input_ph')"
        />
      </div>

      <!-- 敏感操作二次确认：预览「你接下来要做的操作」+ 逐项继续/跳过（批量不碰敏感项） -->
      <div v-if="isSensitive(it)" class="sens-preview">
        <div class="sens-title">{{ t('agent.sens.title') }}</div>
        <div class="chg-line">{{ t('agent.card.op') }}{{ it.title || '—' }}</div>
        <div class="chg-line">{{ t('agent.card.impact') }}{{ it.impact || '—' }}</div>
        <div v-if="paramLines(it).length" class="chg-line sens-params">{{ t('agent.card.params') }}{{ paramLines(it).join('，') }}</div>
        <div v-if="canChange && itSkipped(it) && !['rolled_back', 'dismissed'].includes(ticket.status)" class="sens-actions">
          <el-button size="small" :disabled="busy" @click="doRestore(it)">{{ t('agent.btn.restore') }}</el-button>
        </div>
        <div v-else-if="editable && !itDone(it)" class="sens-actions">
          <el-button type="warning" size="small" :loading="busy" @click="doContinue(it, i)">{{ t('agent.btn.continue') }}</el-button>
          <el-button size="small" :disabled="busy" @click="doSkip(it, i)">{{ t('agent.btn.skip') }}</el-button>
        </div>
      </div>
    </div>

    <!-- L3b 完成后「查看改动快照」：拉 code-snapshots 展示文件清单+diff -->
    <div v-if="showSnapLink" class="code-snap-link">
      <el-link type="primary" :underline="false" @click="openSnapDialog">{{ t('agent.code.snap_link') }}</el-link>
    </div>

    <div v-if="cardError" class="chg-error">{{ cardError }}</div>

    <div v-if="editable && (plainOpen.length || (openForConfirm && !hasSensitive))" class="chg-actions">
      <el-button type="primary" :loading="busy" @click="doConfirm(false)">
        {{ hasSensitive ? t('agent.btn.confirm_plain') : t('agent.btn.confirm') }}
      </el-button>
      <el-button :loading="busy" :disabled="!checkedIdxs.length" @click="doConfirm(true)">
        {{ checkedIdxs.length ? t('agent.btn.confirm_checked_n', { a: checkedIdxs.length, b: list.length }) : t('agent.btn.confirm_checked') }}
      </el-button>
      <el-button v-if="openForConfirm" :disabled="busy" @click="onCancel">{{ t('agent.btn.cancel') }}</el-button>
      <el-button v-if="canChange && rollbackable" type="danger" plain :loading="busy" @click="onRollback">{{ t('agent.btn.rollback') }}</el-button>
    </div>
    <div v-else-if="canChange && rollbackable" class="chg-actions">
      <el-button type="danger" plain :loading="busy" @click="onRollback">{{ t('agent.btn.rollback') }}</el-button>
    </div>
    <div v-if="!canChange" class="chg-hint">{{ t('agent.card.readonly_hint') }}</div>

    <!-- L3b 改动快照弹窗：文件清单 + diff 预览（字节级回滚的依据） -->
    <el-dialog v-model="snapDialog" :title="t('agent.code.snap_title')" width="680px" class="code-snap-dialog">
      <div v-if="snapLoading" class="snap-state">{{ t('agent.loading') }}</div>
      <div v-else-if="snapError" class="snap-state snap-err">{{ snapError }}</div>
      <div v-else-if="!snapItems.length" class="snap-state">{{ t('agent.code.snap_empty') }}</div>
      <template v-else>
        <div v-for="s in snapItems" :key="s.id" class="snap-item">
          <div class="snap-item-head">
            <b class="snap-time">{{ fmtTime(s.created_at) }}</b>
            <el-tag v-if="s.status" size="small" effect="plain" :type="snapStatusType(s)">{{ s.status }}</el-tag>
            <el-tag v-if="s.verified != null" size="small" :type="s.verified ? 'success' : 'warning'">
              {{ s.verified ? t('agent.code.snap_verified') : t('agent.code.snap_unverified') }}
            </el-tag>
            <el-button
              v-if="s.status === 'applied' && canChange"
              class="snap-restart" type="warning" size="small" plain
              :loading="restartingId === s.id" @click="onApplyRestart(s)"
            >{{ t('agent.code.restart_btn') }}</el-button>
          </div>
          <div v-for="(f, fi) in snapFiles(s)" :key="f.path + fi" class="code-file">
            <div class="code-file-head" @click="toggleDiff(`snap-${s.id}:${f.path}`)">
              <span class="code-path">{{ f.path }}</span>
              <span class="code-stat"><b class="code-add">+{{ f.add_lines }}</b><b class="code-del">-{{ f.del_lines }}</b></span>
              <span class="code-toggle">{{ isDiffOpen(`snap-${s.id}:${f.path}`) ? t('agent.code.collapse_diff') : t('agent.code.expand_diff') }}</span>
            </div>
            <div v-if="isDiffOpen(`snap-${s.id}:${f.path}`)" class="code-diff">
              <div v-for="(ln, li) in f.diff_lines" :key="li" class="diff-line" :class="diffLineCls(ln)">{{ ln }}</div>
              <div v-if="!f.diff_lines.length" class="diff-line diff-meta">{{ t('agent.code.diff_empty') }}</div>
            </div>
          </div>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.chg-card {
  border: 1px solid var(--el-color-warning-light-5);
  border-radius: 10px;
  background: var(--el-fill-color-lighter);
  padding: 12px 14px;
  max-width: 92%;
  width: 620px;
  font-size: 13px;
}
.chg-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.chg-warn-ico {
  color: var(--el-color-warning);
}
.chg-count {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.chg-time {
  margin-left: auto;
  font-size: 11.5px;
  color: var(--el-text-color-secondary);
}
.chg-title {
  font-size: 13.5px;
  font-weight: 600;
  margin: 6px 0 8px;
}
.chg-item {
  border-top: 1px dashed var(--el-border-color-lighter);
  padding: 8px 0;
}
.chg-item-run {
  background: var(--el-color-primary-light-9);
  border-radius: 6px;
  padding: 8px 6px;
}
.chg-item-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.chg-check {
  margin-right: 2px;
}
.chg-ico {
  display: inline-flex;
  align-items: center;
}
.ti-success { color: var(--el-color-success); }
.ti-danger { color: var(--el-color-danger); }
.ti-warning { color: var(--el-color-warning); }
.ti-primary { color: var(--el-color-primary); }
.ti-plain { color: var(--el-text-color-secondary); }
.chg-item-title {
  font-size: 13px;
}
.chg-line {
  font-size: 12px;
  color: var(--el-text-color-regular);
  line-height: 1.8;
  margin-top: 2px;
}
.chg-result {
  color: var(--el-color-primary);
}
.chg-error {
  color: var(--el-color-danger);
  font-size: 12.5px;
  margin-top: 6px;
}
.chg-actions {
  display: flex;
  gap: 8px;
  margin-top: 10px;
}
.chg-hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-top: 8px;
}

/* ack_required 警示块（口径同 CloudAccounts.vue 云凭证安全警示） */
.ack-warn {
  margin-top: 8px;
  border: 1px solid var(--el-color-warning-light-5);
  border-radius: 8px;
  padding: 10px 12px;
  background: var(--el-color-warning-light-9);
}
.ack-alert {
  margin-bottom: 8px;
}
.ack-body {
  line-height: 1.9;
  font-size: 12.5px;
}
.ack-body p {
  margin: 4px 0;
}
.ack-input {
  margin-top: 8px;
}

/* 敏感操作二次确认：预览块 + 逐项继续/跳过 */
.sens-preview {
  margin-top: 8px;
  border: 1px solid var(--el-color-danger-light-5);
  border-radius: 8px;
  padding: 10px 12px;
  background: var(--el-color-danger-light-9);
}
.sens-title {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--el-color-danger);
  margin-bottom: 4px;
}
.sens-params {
  word-break: break-all;
}
.sens-actions {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}

/* ---- L3b 代码变更：文件清单 + diff 红绿预览（等宽字体） ---- */
.code-box {
  margin-top: 8px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: var(--el-fill-color-blank);
  padding: 8px 10px;
}
.code-box-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
}
.code-box-title {
  font-size: 12.5px;
  font-weight: 600;
}
.code-box-count {
  font-size: 11.5px;
  color: var(--el-text-color-secondary);
}
.code-file {
  border-top: 1px dashed var(--el-border-color-lighter);
  padding: 5px 0;
}
.code-file-head {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
}
.code-path {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  word-break: break-all;
}
.code-stat {
  display: inline-flex;
  gap: 6px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 11.5px;
}
.code-add { color: var(--el-color-success); }
.code-del { color: var(--el-color-danger); }
.code-toggle {
  margin-left: auto;
  font-size: 11.5px;
  color: var(--el-color-primary);
  white-space: nowrap;
}
.code-diff {
  margin-top: 4px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  overflow-x: auto;
  background: var(--el-fill-color-lighter);
  padding: 4px 0;
}
.diff-line {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 11.5px;
  line-height: 1.7;
  padding: 0 8px;
  white-space: pre;
}
/* add 绿底 / del 红底（EP dark 会把 light-9 翻成深色调、dark-2 翻成亮色调，变量自适应） */
.diff-add {
  background: var(--el-color-success-light-9);
  color: var(--el-color-success-dark-2);
}
.diff-del {
  background: var(--el-color-danger-light-9);
  color: var(--el-color-danger-dark-2);
}
.diff-hunk {
  background: var(--el-fill-color);
  color: var(--el-text-color-secondary);
}
.diff-meta { color: var(--el-text-color-secondary); }
.diff-ctx { color: var(--el-text-color-regular); }
.code-restart {
  margin-top: 6px;
  font-size: 12px;
  color: var(--el-color-warning);
}
.code-snap-link {
  margin-top: 8px;
}

/* 快照弹窗（L3b） */
.snap-state {
  font-size: 12.5px;
  color: var(--el-text-color-secondary);
}
.snap-err { color: var(--el-color-danger); }
.snap-item {
  border-top: 1px dashed var(--el-border-color-lighter);
  padding: 8px 0;
}
.snap-item-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 2px;
}
.snap-time {
  font-size: 12.5px;
}
.snap-restart {
  margin-left: auto;
}

/* 暗色主题柔化（html.dark 分作用域，不写组件级硬浅色） */
html.dark .chg-card {
  background: var(--el-fill-color-light);
}
html.dark .chg-item-run {
  background: var(--el-color-primary-light-3);
}
html.dark .code-box {
  background: var(--el-fill-color-light);
}
html.dark .code-diff {
  background: var(--el-bg-color-overlay);
}
html.dark .diff-add {
  background: var(--el-color-success-light-9);
  color: var(--el-color-success-dark-2);
}
html.dark .diff-del {
  background: var(--el-color-danger-light-9);
  color: var(--el-color-danger-dark-2);
}
</style>
