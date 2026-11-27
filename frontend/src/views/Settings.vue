<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useRoute, useRouter } from 'vue-router'

import http from '@/api/http'

import {
  createChannel, deleteChannel, listChannels, testChannel, updateChannel,
} from '@/api/alerts'
import {
  aiUsageLogs, aiUsageSummary, createAiSetting, deleteAiSetting,
  listAiScenarios, listAiSettings, testAiSetting, updateAiScenario, updateAiSetting,
} from '@/api/ai'
import { listAuditLogs } from '@/api/audit'
import { createToken, deleteToken, listTokens } from '@/api/tokens'
import { fmtTime } from '@/utils/format'
import { useAuthStore } from '@/stores/auth'
import { t } from '@/i18n'
import SettingsPolicy from '@/views/settings/SettingsPolicy.vue'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

// SDK 扫描状态：新记录带 status_code；旧记录回退按中文文案映射（重扫后自然升级）
const SDK_STATUS_TAG = { ok: 'success', outdated: 'warning', deprecated: 'danger', not_installed: 'info', error: 'info', unknown: 'info' }
const SDK_STATUS_FALLBACK = { '最新': 'ok', '可升级': 'outdated', '已废弃': 'deprecated', '未安装': 'not_installed', '检查失败': 'error' }
function sdkStatusOf(row) {
  return row?.status_code || SDK_STATUS_FALLBACK[row?.status] || 'unknown'
}
// ---- 节假日日历（基线分析依据可视化）----
const holiday = ref({ items: [], holidays: 0, adjusted_workdays: 0, years: [], last_sync: '', source: '', usage: '' })
const holidayYear = ref('')
const holidaySyncing = ref(false)

async function loadHoliday() {
  try {
    holiday.value = await http.get('/settings/holiday', { params: { year: holidayYear.value || '' } })
  } catch { /* 拦截器 */ }
}

async function syncHoliday() {
  holidaySyncing.value = true
  try {
    await http.post('/settings/holiday/sync')
    ElMessage.success(t('settings.msg.holiday_synced'))
    loadHoliday()
  } catch { /* 拦截器 */ } finally { holidaySyncing.value = false }
}

// ---- 访问域名（保存即动态生效：vite 白名单热重载）----
const allowedHosts = ref([])
const allowedHostsMode = ref('open')
const newHost = ref('')
const hostsSaving = ref(false)

async function loadAllowedHosts() {
  try {
    const r = await http.get('/settings/allowed-hosts')
    allowedHosts.value = r.hosts || []
    allowedHostsMode.value = r.mode || 'open'
  } catch { /* 拦截器 */ }
}

function addHost() {
  const h = (newHost.value || '').trim().toLowerCase()
  if (!h) return
  if (allowedHosts.value.includes(h)) return ElMessage.warning(t('settings.msg.host_dup'))
  allowedHosts.value.push(h)
  newHost.value = ''
}

async function saveAllowedHosts() {
  hostsSaving.value = true
  try {
    const r = await http.put('/settings/allowed-hosts', { hosts: allowedHosts.value })
    allowedHosts.value = r.hosts || []
    allowedHostsMode.value = r.hosts.length ? 'whitelist' : 'open'
    ElMessage.success(t('settings.msg.hosts_saved'))
  } catch { /* 拦截器 */ } finally { hostsSaving.value = false }
}

// ---- SDK 治理：锁版本 + 每周扫描 + 人工确认升级，绝不自动升级 ----
const sdkReport = ref({ scanned_at: '', trigger: '', items: [], total: 0, up_to_date: 0, outdated: 0, deprecated: 0, not_installed: 0, errors: 0, hint: '' })
const sdkLoading = ref(false)
const sdkScanning = ref(false)

async function loadSdkReport() {
  sdkLoading.value = true
  try {
    sdkReport.value = await http.get('/settings/sdk-report')
  } catch { /* 拦截器 */ } finally { sdkLoading.value = false }
}

async function scanSdkNow() {
  sdkScanning.value = true
  try {
    // 扫描要逐包拉 PyPI（10s/个容错），放宽超时到 2 分钟
    const r = await http.post('/settings/sdk-report/scan', {}, { timeout: 120000 })
    sdkReport.value = r
    ElMessage.success(t('settings.msg.sdk_scanned', { total: r.total, outdated: r.outdated, deprecated: r.deprecated }))
  } catch { /* 拦截器 */ } finally { sdkScanning.value = false }
}

const canManage = computed(() => ['admin', 'operator'].includes(auth.role))
const isAdmin = computed(() => auth.role === 'admin')

// 顶层页签为 policy/notify/models/org/platform，旧页签保留为二级分区
// （name 不变），深链通过别名表兼容。
// 默认落「监控与提醒」（全员可读；写操作按角色置灰）。
const tab = ref('policy')
const tabSub = reactive({
  notify: 'channels',
  models: canManage.value ? 'ai' : 'sdk',
  org: 'tokens',
  platform: 'sysparams',
})

/* ---------------- 审计日志（admin 只读） ---------------- */
const auditRows = ref([])
const auditLoading = ref(false)
const auditTotal = ref(0)
const auditQuery = reactive({ page: 1, page_size: 50, action: '' })

async function loadAudit() {
  auditLoading.value = true
  try {
    const params = { page: auditQuery.page, page_size: auditQuery.page_size }
    if (auditQuery.action) params.action = auditQuery.action
    const r = await listAuditLogs(params)
    auditRows.value = r.items || []
    auditTotal.value = r.total || 0
  } finally {
    auditLoading.value = false
  }
}


/* ---------------- 个人 API Token：明文只在创建时回显一次 ---------------- */
const tokens = ref([])
const tokenLoading = ref(false)
const tokenDialog = ref(false)
const tokenForm = reactive({ name: '', expires_days: null })
const createdToken = ref('')

async function loadTokens() {
  tokenLoading.value = true
  try {
    tokens.value = await listTokens()
  } catch { /* 拦截器 */ } finally { tokenLoading.value = false }
}

function openTokenDialog() {
  tokenForm.name = ''
  tokenForm.expires_days = null
  createdToken.value = ''
  tokenDialog.value = true
}

async function saveToken() {
  if (!tokenForm.name) return ElMessage.warning(t('settings.msg.need_token_name'))
  const r = await createToken({ name: tokenForm.name, expires_days: tokenForm.expires_days })
  createdToken.value = r.token
  ElMessage.success(t('settings.msg.token_created'))
  loadTokens()
}

function copyToken() {
  navigator.clipboard.writeText(createdToken.value)
  ElMessage.success(t('settings.msg.copied'))
}

async function revokeToken(row) {
  await ElMessageBox.confirm(
    t('settings.confirm.revoke_token', { name: row.name, prefix: row.token_prefix }),
    t('settings.confirm.revoke_title'),
    { type: 'warning', confirmButtonText: t('settings.btn.revoke'), cancelButtonText: t('settings.btn.cancel') },
  )
  await deleteToken(row.id)
  ElMessage.success(t('settings.msg.revoked'))
  loadTokens()
}

/* ---------------- 通知渠道（唯一管理面） ---------------- */
const channels = ref([])
const channelLoading = ref(false)
const channelDialog = ref(false)
const editingChannel = ref(null) // null=新建
const channelForm = reactive({
  name: '', type: 'wecom_bot', webhook_url: '', at_mobiles: '', enabled: true,
  verify_ssl: true,   // 自签证书可关闭校验，默认开启
})
const testingChannelId = ref(0)

// 不提供邮件渠道：收件人绑定用户会引入额外的用户管理复杂度
const TYPE_LABELS = {
  wecom_bot: 'settings.channel.wecom', dingtalk_bot: 'settings.channel.dingtalk',
  webhook: 'settings.channel.webhook',
}
const channelTypeLabel = (x) => (TYPE_LABELS[x] ? t(TYPE_LABELS[x]) : x)

async function loadChannels() {
  channelLoading.value = true
  try {
    channels.value = await listChannels()
  } finally {
    channelLoading.value = false
  }
}

function openChannelDialog(row) {
  editingChannel.value = row
  Object.assign(channelForm, {
    name: row?.name || '',
    type: row?.type || 'wecom_bot',
    webhook_url: '', // 密文不回显，不修改则留空
    at_mobiles: (row?.at_mobiles || []).join(', '),
    enabled: row?.enabled ?? true,
    verify_ssl: row?.verify_ssl ?? true,
  })
  channelDialog.value = true
}

async function saveChannel() {
  if (!channelForm.name) return ElMessage.warning(t('settings.msg.need_channel_name'))
  if (!channelForm.webhook_url && !editingChannel.value) {
    // 新建必须给 webhook 地址（编辑留空=不修改）
    return ElMessage.warning(t('settings.msg.need_webhook'))
  }
  const atMobiles = channelForm.at_mobiles
    .split(/[,，\s]+/).filter(Boolean).map((m) => m.trim())
  const data = {
    name: channelForm.name,
    type: channelForm.type,
    at_mobiles: atMobiles,
    enabled: channelForm.enabled,
    verify_ssl: channelForm.verify_ssl,
  }
  if (channelForm.webhook_url) data.webhook_url = channelForm.webhook_url
  if (editingChannel.value) {
    await updateChannel(editingChannel.value.id, data)
  } else {
    await createChannel(data)
  }
  ElMessage.success(t('settings.msg.saved'))
  channelDialog.value = false
  loadChannels()
}

async function removeChannel(row) {
  await ElMessageBox.confirm(
    t('settings.confirm.delete_channel', { name: row.name }),
    t('settings.confirm.delete_title'),
    { type: 'warning', confirmButtonText: t('settings.btn.delete'), cancelButtonText: t('settings.btn.cancel') },
  )
  await deleteChannel(row.id)
  ElMessage.success(t('settings.msg.deleted'))
  loadChannels()
}

async function doTestChannel(row) {
  // 发送测试消息是唯一真实推送入口：必须由用户主动点击并二次确认，遵守对外发消息红线
  try {
    await ElMessageBox.confirm(
      t('settings.confirm.send_test', { name: row.name }),
      t('settings.confirm.send_test_title'),
      { type: 'warning', confirmButtonText: t('settings.btn.send_test'), cancelButtonText: t('settings.btn.cancel') },
    )
  } catch { return }
  testingChannelId.value = row.id
  try {
    const r = await testChannel(row.id)
    r.ok ? ElMessage.success(r.message) : ElMessage.error(r.message)
  } finally {
    testingChannelId.value = 0
  }
}

/* ---------------- AI 模型配置 ---------------- */
const aiSettings = ref([])
const aiLoading = ref(false)
const aiDialog = ref(false)
const editingAi = ref(null) // null=新建
const aiForm = reactive({
  provider_label: '', base_url: '', api_key: '', model: '',
  temperature: 0.2, max_tokens: 2048, remark: '', enabled: false,
  verify_ssl: true,   // 自签证书可关闭校验，默认开启
})
const testingAiId = ref(0)
const testingAiForm = ref(false)   // 弹窗内「测试连通」的加载状态
const aiTestResult = ref(null) // {ok, reply, model, duration_ms, usage, error}
const aiTestDialog = ref(false)
const switchingAiId = ref(0)

/* 调用统计 */
const aiUsage = ref(null)
const aiUsageLoading = ref(false)
const aiLogs = ref([])
const aiLogsTotal = ref(0)
const aiLogsLoading = ref(false)
const aiLogsQuery = reactive({ page: 1, page_size: 10 })

const PURPOSE_LABELS = {
  test: 'settings.purpose.test', nl_query: 'settings.purpose.nl_query',
  inspection: 'settings.purpose.inspection', diagnosis: 'settings.purpose.diagnosis',
  // copilot 等用途也会落库，必须映射为显示标签
  copilot: 'settings.purpose.copilot', copilot_review: 'settings.purpose.copilot_review',
  diagnosis_route: 'settings.purpose.diagnosis_route',
}
const purposeLabel = (p) => (PURPOSE_LABELS[p] ? t(PURPOSE_LABELS[p]) : p)

async function loadAiSettings() {
  aiLoading.value = true
  try {
    aiSettings.value = await listAiSettings()
  } finally {
    aiLoading.value = false
  }
}

async function loadAiUsage() {
  aiUsageLoading.value = true
  aiLogsLoading.value = true
  try {
    aiUsage.value = await aiUsageSummary({ days: 30 })
    const r = await aiUsageLogs(aiLogsQuery)
    aiLogsTotal.value = r.total
    aiLogs.value = r.items
  } finally {
    aiUsageLoading.value = false
    aiLogsLoading.value = false
  }
}

function openAiDialog(row) {
  editingAi.value = row
  Object.assign(aiForm, {
    provider_label: row?.provider_label || '',
    base_url: row?.base_url || '',
    api_key: '', // 密文不回显
    model: row?.model || '',
    temperature: row?.temperature ?? 0.2,
    max_tokens: row?.max_tokens ?? 2048,
    remark: row?.remark || '',
    enabled: row?.enabled || false,
    verify_ssl: row?.verify_ssl ?? true,
  })
  aiDialog.value = true
}

// 弹窗内「测试连通」按表单当前值测试，无需先保存；
// 后端 POST /ai/test，编辑时 Key 留空则使用已保存的 Key。
async function testAiForm() {
  if (!aiForm.base_url || !aiForm.model) return ElMessage.warning(t('settings.msg.need_base_model'))
  if (!aiForm.api_key && !editingAi.value) return ElMessage.warning(t('settings.msg.need_api_key'))
  testingAiForm.value = true
  try {
    const r = await http.post('/ai/test', {
      base_url: aiForm.base_url,
      model: aiForm.model,
      api_key: aiForm.api_key || '',
      temperature: aiForm.temperature,
      max_tokens: aiForm.max_tokens,
      verify_ssl: aiForm.verify_ssl,
      editing_id: editingAi.value?.id || null,
    }, { timeout: 180000 })
    aiTestResult.value = r
    aiTestDialog.value = true
  } catch { /* 拦截器提示 */ } finally { testingAiForm.value = false }
}

async function saveAi() {
  if (!aiForm.provider_label || !aiForm.base_url || !aiForm.model) {
    return ElMessage.warning(t('settings.msg.need_name_base_model'))
  }
  if (!editingAi.value && !aiForm.api_key) {
    return ElMessage.warning(t('settings.msg.need_api_key_new'))
  }
  const data = {
    provider_label: aiForm.provider_label,
    base_url: aiForm.base_url,
    model: aiForm.model,
    temperature: aiForm.temperature,
    max_tokens: aiForm.max_tokens,
    remark: aiForm.remark,
    enabled: aiForm.enabled,
    verify_ssl: aiForm.verify_ssl,
  }
  if (aiForm.api_key) data.api_key = aiForm.api_key
  if (editingAi.value) {
    await updateAiSetting(editingAi.value.id, data)
  } else {
    await createAiSetting(data)
  }
  ElMessage.success(t('settings.msg.saved'))
  aiDialog.value = false
  loadAiSettings()
}

async function removeAi(row) {
  await ElMessageBox.confirm(
    t('settings.confirm.delete_ai', { name: row.provider_label }),
    t('settings.confirm.delete_title'),
    { type: 'warning', confirmButtonText: t('settings.btn.delete'), cancelButtonText: t('settings.btn.cancel') },
  )
  await deleteAiSetting(row.id)
  ElMessage.success(t('settings.msg.deleted'))
  loadAiSettings()
  loadAiScenarios()   // 场景矩阵的绑定可能指向被删连接，刷新回兜底显示
}

async function doTestAi(row) {
  testingAiId.value = row.id
  try {
    const r = await testAiSetting(row.id)
    aiTestResult.value = r
    aiTestDialog.value = true
    loadAiUsage() // 测试也消耗 token，刷新统计
  } finally {
    testingAiId.value = 0
  }
}

async function toggleAiEnabled(row) {
  switchingAiId.value = row.id
  try {
    await updateAiSetting(row.id, { enabled: !row.enabled })
    ElMessage.success(!row.enabled ? t('settings.msg.ai_enabled') : t('settings.msg.ai_disabled'))
    loadAiSettings()
  } finally {
    switchingAiId.value = 0
  }
}

/* ---------------- 场景矩阵：场景 × 模型连接 ---------------- */
const aiScenarios = ref([])
const aiScenariosLoading = ref(false)
const savingScenario = ref('')
const SCENARIO_DESC = {
  ask: 'settings.scenario.ask',
  inspection: 'settings.scenario.inspection',
  diagnosis: 'settings.scenario.diagnosis',
  copilot: 'settings.scenario.copilot',
}

async function loadAiScenarios() {
  aiScenariosLoading.value = true
  try {
    aiScenarios.value = (await listAiScenarios()).map((r) => ({ ...r, desc: SCENARIO_DESC[r.scenario] ? t(SCENARIO_DESC[r.scenario]) : '' }))
  } catch { /* 拦截器 */ } finally {
    aiScenariosLoading.value = false
  }
}

async function saveAiScenario(row) {
  savingScenario.value = row.scenario
  try {
    await updateAiScenario(row.scenario, { setting_id: row.setting_id, enabled: row.enabled })
    ElMessage.success(t('settings.msg.scenario_saved', { name: row.label }))
    loadAiScenarios()
  } catch { /* 拦截器 */ } finally {
    savingScenario.value = ''
  }
}

const accounts = ref([])
/* ---------------- 数据保留 · 系统参数 ----------------
   7 项保留天数（0=永久）一个全局保存；保存即按新规则清存量（与 K8s 页既有口径一致）。
   VM/VL 实时生效（查询口径过滤 + 后台渐进清理，无需重建容器/重启存储）；
   引擎上限（caps，部署时定的 -retentionPeriod）内的调整都即时生效，调大超上限才需重建。 */
const retention = reactive({
  vm_days: 180, vl_days: 30, k8s_days: 7,
  llm_days: 90, audit_days: 180, msg_days: 90, event_days: 180,
})
const retentionCaps = reactive({ vm: 3650, vl: 3650 })
const retentionDirty = ref(false)
const retentionSaved = ref(false)
const retentionRows = [
  { key: 'vm_days', label: 'settings.retention.vm', tip: 'settings.retention.engine_tip', cap: 'vm' },
  { key: 'vl_days', label: 'settings.retention.vl', tip: 'settings.retention.engine_tip', cap: 'vl' },
  { key: 'k8s_days', label: 'settings.retention.k8s', tip: 'settings.retention.k8s_tip' },
  { key: 'llm_days', label: 'settings.retention.llm', tip: 'settings.retention.biz_tip' },
  { key: 'audit_days', label: 'settings.retention.audit', tip: 'settings.retention.biz_tip' },
  { key: 'msg_days', label: 'settings.retention.msg', tip: 'settings.retention.biz_tip' },
  { key: 'event_days', label: 'settings.retention.event', tip: 'settings.retention.event_tip' },
]

function rowMax(row) {
  // 上限层：VM/VL 输入上限=引擎上限（部署时定）；后端同步校验（超上限 400）
  return row.cap && retentionCaps[row.cap] > 0 ? retentionCaps[row.cap] : 3650
}

async function loadRetention() {
  try {
    const r = await http.get('/settings/retention')
    for (const k of Object.keys(retention)) {
      if (r[k] !== undefined) retention[k] = r[k]
    }
    if (r.caps) {
      retentionCaps.vm = r.caps.vm || 3650
      retentionCaps.vl = r.caps.vl || 3650
    }
    retentionDirty.value = false
  } catch { /* 拦截器提示 */ }
}

async function saveRetention() {
  // 防误触：确认弹窗（保存即立即按新规则清存量，误设小值会删数据）；正向反馈：已保存状态
  try {
    await ElMessageBox.confirm(
      t('settings.confirm.retention_body'),
      t('settings.confirm.retention_title'), { type: 'warning', confirmButtonText: t('settings.btn.confirm_save'), cancelButtonText: t('settings.btn.think_again') })
  } catch {
    return   // 取消
  }
  const r = await http.put('/settings/retention', { ...retention })
  retentionDirty.value = false
  retentionSaved.value = true
  const rows = Object.values(r.cleaned || {}).reduce((a, b) => a + (b || 0), 0)
  ElMessage.success(t('settings.msg.retention_saved', { rows }))
  setTimeout(() => { retentionSaved.value = false }, 3000)
}
const accDialog = ref(false)
const accForm = reactive({ name: '', kind: 'cloud', remark: '' })

async function loadAccounts() {
  try {
    accounts.value = await http.get('/settings/accounts')
  } catch { /* 拦截器提示 */ }
}

async function createAccount() {
  if (!accForm.name) return ElMessage.warning(t('settings.msg.need_account_name'))
  await http.post('/settings/accounts', { ...accForm })
  ElMessage.success(t('settings.msg.account_created'))
  accDialog.value = false
  loadAccounts()
}

// ---- 归属配置：实体→厂商/账号/业务人工登记 ----
// 未配置的实体在拓扑等处如实显示"未归属"（不猜）；登记后回流各展示面
const ownRules = ref([])
const ownUnattributed = ref([])
const ownAccounts = ref([])
const ownOptions = ref({ key_types: [], match_types: [], vendors: [] })
const ownDialog = ref(false)
const ownEditingId = ref(0)
const ownForm = reactive({
  key_type: 'ip', key_value: '', match_type: 'exact',
  vendor: '', account_name: '', account_id: 0, business: '', note: '',
})
const ownPreview = ref(null)
const ownHints = ref([])

async function loadOwnership() {
  try {
    ownRules.value = await http.get('/ownership/rules')
    ownUnattributed.value = await http.get('/ownership/unattributed')
    if (!ownOptions.value.key_types.length) {
      ownOptions.value = await http.get('/ownership/options')
      const r = await http.get('/clouds/accounts')
      ownAccounts.value = r.items || []
    }
  } catch { /* 拦截器 */ }
}

function openOwnDialog(row) {
  ownEditingId.value = row?.id || 0
  Object.assign(ownForm, row
    ? {
        key_type: row.key_type, key_value: row.key_value, match_type: row.match_type,
        vendor: row.vendor, account_name: row.account_name, account_id: row.account_id || 0,
        business: row.business, note: row.note,
      }
    : {
        key_type: 'ip', key_value: '', match_type: 'exact',
        vendor: '', account_name: '', account_id: 0, business: '', note: '',
      })
  ownPreview.value = null
  ownHints.value = []
  ownDialog.value = true
}

function ownFromUnattributed(u) {
  openOwnDialog(null)
  ownForm.key_type = u.key_type
  ownForm.key_value = u.key_value
  if (u.business) ownForm.business = u.business
  runOwnPreview()
}

async function runOwnPreview() {
  if (!ownForm.key_value) return
  try {
    const r = await http.get('/ownership/preview', {
      params: { key_type: ownForm.key_type, key_value: ownForm.key_value },
    })
    ownPreview.value = r.hit
    ownHints.value = r.hints || []
  } catch { /* 拦截器 */ }
}

function adoptHint(h) {
  // 识别线索一键采纳：线索只是建议，人工点确认才进表单（不落库，不猜）
  if (h.suggest?.vendor) ownForm.vendor = h.suggest.vendor
  if (h.suggest?.account_name) ownForm.account_name = h.suggest.account_name
}

async function saveOwnRule() {
  if (!ownForm.key_value) return ElMessage.warning(t('settings.msg.need_key_value'))
  const body = {
    key_type: ownForm.key_type, key_value: ownForm.key_value, match_type: ownForm.match_type,
    vendor: ownForm.vendor, account_name: ownForm.account_name,
    account_id: Number(ownForm.account_id) || 0,
    business: ownForm.business, note: ownForm.note,
  }
  try {
    if (ownEditingId.value) await http.put(`/ownership/rules/${ownEditingId.value}`, body)
    else await http.post('/ownership/rules', body)
    ElMessage.success(t('settings.msg.own_saved'))
    ownDialog.value = false
    loadOwnership()
  } catch { /* 拦截器 */ }
}

async function deleteOwnRule(row) {
  try {
    await ElMessageBox.confirm(t('settings.confirm.delete_own', { kt: row.key_type, kv: row.key_value }), t('settings.confirm.delete_title'), { type: 'warning' })
    await http.delete(`/ownership/rules/${row.id}`)
    ElMessage.success(t('settings.msg.deleted'))
    loadOwnership()
  } catch { /* 取消或拦截器 */ }
}


onMounted(() => { loadAllowedHosts(); loadHoliday(); loadSdkReport() })
onMounted(() => {
  loadAccounts()
  loadRetention()
  loadOwnership()
  if (isAdmin.value) loadAudit()
  loadTokens()
  if (canManage.value) loadChannels()
  if (canManage.value) {
    loadAiSettings()
    loadAiScenarios()
    loadAiUsage()
  }
})

// 同一 Settings 实例内再次跳转也要消费 query，不能只认首次挂载。
function applySettingsDeepLink() {
  // 页面深链 ?tab= 统一解析，覆盖归属、AI 配置等入口。
  const qtab = typeof route.query.tab === 'string' ? route.query.tab : ''
  // 旧深链兼容：旧页签名 -> [顶层页签, 二级分区]。
  // 旧名不是外部契约的一部分，但拓扑「去归属」、AI 未配置引导等处都在用，必须继续可达。
  const TAB_ALIAS = {
    policy: ['policy', ''],
    notify: ['notify', 'channels'], channels: ['notify', 'channels'],
    models: ['models', 'ai'], ai: ['models', 'ai'], sdk: ['models', 'sdk'],
    // 用户/组织架构不再提供独立页签；旧深链统一落到「访问令牌」，避免出现无落点页签
    org: ['org', 'tokens'], users: ['org', 'tokens'], ownership: ['org', 'ownership'],
    tokens: ['org', 'tokens'], audit: ['org', 'audit'],
    platform: ['platform', 'sysparams'], sysparams: ['platform', 'sysparams'],
    hosts: ['platform', 'hosts'], holiday: ['platform', 'holiday'],
    accounts: ['platform', 'accounts'],
  }
  // 二级分区角色门槛必须与各 pane 的 v-if 一致，避免深链进入无权限或空白分区
  const SECTION_ROLE = {
    channels: 'manage', ai: 'manage', hosts: 'admin', audit: 'admin',
  }
  function sectionAllowed(sub) {
    const need = SECTION_ROLE[sub]
    if (need === 'manage') return canManage.value
    if (need === 'admin') return isAdmin.value
    return true
  }
  const hit = TAB_ALIAS[qtab]
  if (hit) {
    const [top, sub] = hit
    if (!sub) {
      tab.value = top
    } else if (sectionAllowed(sub)) {
      tab.value = top
      tabSub[top] = sub
    }
  }
  // 拓扑详情「去归属」深链：/settings?tab=ownership&key_type=&key_value=（预填登记对话框）
  if (qtab === 'ownership' && route.query.key_value) {
    openOwnDialog(null)
    ownForm.key_type = String(route.query.key_type || 'name')
    ownForm.key_value = String(route.query.key_value)
    runOwnPreview()
  }
}
watch(() => route.fullPath, applySettingsDeepLink, { immediate: true })
</script>

<template>
  <div class="page">
    <el-tabs v-model="tab">
      <el-tab-pane :label="t('settings.tab.policy')" name="policy">
        <SettingsPolicy />
      </el-tab-pane>

      <el-tab-pane v-if="canManage" :label="t('settings.tab.notify')" name="notify">
        <el-tabs v-model="tabSub.notify" class="sub-tabs">
          <el-tab-pane v-if="canManage" :label="t('settings.tab.channels')" name="channels">
            <div class="toolbar">
              <el-button type="primary" @click="openChannelDialog(null)">{{ t('settings.btn.add_channel') }}</el-button>
              <span class="tip">{{ t('settings.channel.tip') }}</span>
            </div>
            <el-table stripe :data="channels" v-loading="channelLoading" border>
              <el-table-column prop="name" :label="t('settings.col.name')" width="160" />
              <el-table-column :label="t('settings.col.type')" width="140">
                <template #default="{ row }">
                  <el-tag size="small" :type="row.type === 'wecom_bot' ? 'success' : row.type === 'dingtalk_bot' ? 'primary' : 'info'">
                    {{ channelTypeLabel(row.type) }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column :label="t('settings.col.endpoint')" min-width="260" show-overflow-tooltip>
                <template #default="{ row }">
                  <span>{{ row.webhook_masked }}</span>
                </template>
              </el-table-column>
              <el-table-column :label="t('settings.col.at_mobiles')" width="180">
                <template #default="{ row }">
                  {{ (row.at_mobiles || []).join(', ') || '—' }}
                </template>
              </el-table-column>
              <el-table-column :label="t('settings.col.status')" width="80">
                <template #default="{ row }">
                  <el-tag :type="row.enabled ? 'success' : 'info'" size="small">
                    {{ row.enabled ? t('settings.enabled') : t('settings.disabled') }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column :label="t('settings.col.actions')" width="220" fixed="right">
                <template #default="{ row }">
                  <el-button size="small" :loading="testingChannelId === row.id" @click="doTestChannel(row)">{{ t('settings.btn.send_test') }}</el-button>
                  <el-button size="small" @click="openChannelDialog(row)">{{ t('settings.btn.edit') }}</el-button>
                  <el-button size="small" type="danger" @click="removeChannel(row)">{{ t('settings.btn.delete') }}</el-button>
                </template>
              </el-table-column>
            </el-table>
          </el-tab-pane>
        </el-tabs>
      </el-tab-pane>

      <el-tab-pane :label="t('settings.tab.models')" name="models">
        <el-tabs v-model="tabSub.models" class="sub-tabs">
          <el-tab-pane v-if="canManage" :label="t('settings.tab.ai')" name="ai">
            <div class="toolbar">
              <el-button type="primary" @click="openAiDialog(null)">{{ t('settings.btn.add_ai') }}</el-button>
              <span class="tip">{{ t('settings.ai.tip') }}</span>
              <!-- operator 可见页签但绑定/增删改禁用，需解释禁用原因 -->
              <span v-if="!isAdmin" class="tip">{{ t('settings.ai.admin_only_tip') }}</span>
            </div>
            <el-table stripe :data="aiSettings" v-loading="aiLoading" border>
              <el-table-column prop="provider_label" :label="t('settings.col.name')" width="150" />
              <el-table-column prop="base_url" label="Base URL" min-width="220" show-overflow-tooltip />
              <el-table-column prop="model" :label="t('settings.col.model')" width="160" />
              <el-table-column :label="t('settings.col.params')" width="150">
                <template #default="{ row }">temp {{ row.temperature }} / {{ row.max_tokens }}t</template>
              </el-table-column>
              <el-table-column :label="t('settings.col.status')" width="90">
                <template #default="{ row }">
                  <el-switch
                    :model-value="row.enabled"
                    :loading="switchingAiId === row.id"
                    :disabled="!isAdmin"
                    @change="toggleAiEnabled(row)"
                  />
                </template>
              </el-table-column>
              <el-table-column prop="updated_at" :label="t('settings.col.updated_at')" width="170" />
              <el-table-column :label="t('settings.col.actions')" width="200" fixed="right">
                <template #default="{ row }">
                  <el-button size="small" :loading="testingAiId === row.id" @click="doTestAi(row)">{{ t('settings.btn.test') }}</el-button>
                  <el-button size="small" :disabled="!isAdmin" @click="openAiDialog(row)">{{ t('settings.btn.edit') }}</el-button>
                  <el-button size="small" type="danger" :disabled="!isAdmin" @click="removeAi(row)">{{ t('settings.btn.delete') }}</el-button>
                </template>
              </el-table-column>
            </el-table>

            <!-- 场景矩阵：各场景独立绑定模型连接 -->
            <div class="usage-title">{{ t('settings.ai.scenario_title') }}</div>
            <el-table stripe :data="aiScenarios" v-loading="aiScenariosLoading" border>
              <el-table-column prop="label" :label="t('settings.col.scenario')" width="130" />
              <el-table-column :label="t('settings.col.bind_model')" min-width="240">
                <template #default="{ row }">
                  <el-select v-model="row.setting_id" :disabled="!isAdmin" style="width: 100%">
                    <el-option :label="t('settings.ai.fallback')" :value="0" />
                    <el-option
                      v-for="s in aiSettings"
                      :key="s.id"
                      :label="`${s.provider_label} · ${s.model}`"
                      :value="s.id"
                    />
                  </el-select>
                </template>
              </el-table-column>
              <el-table-column :label="t('settings.col.scenario_switch')" width="100">
                <template #default="{ row }">
                  <el-switch v-model="row.enabled" :disabled="!isAdmin" />
                </template>
              </el-table-column>
              <el-table-column prop="desc" :label="t('settings.col.detail')" min-width="180" />
              <el-table-column :label="t('settings.col.actions')" width="90" fixed="right">
                <template #default="{ row }">
                  <el-button
                    size="small" type="primary"
                    :loading="savingScenario === row.scenario"
                    :disabled="!isAdmin"
                    @click="saveAiScenario(row)"
                  >{{ t('settings.btn.save') }}</el-button>
                </template>
              </el-table-column>
            </el-table>
            <div class="tip" style="margin-top: 8px; line-height: 1.8">
              {{ t('settings.ai.scenario_tip') }}
              <template v-if="!aiSettings.length">{{ t('settings.ai.no_conn') }}</template>
            </div>

            <!-- 调用统计 -->
            <div class="usage-title">{{ t('settings.ai.usage_title') }}</div>
            <div v-loading="aiUsageLoading" class="usage-cards">
              <div class="usage-card">
                <div class="usage-label">{{ t('settings.ai.total_calls') }}</div>
                <div class="usage-value">{{ aiUsage?.total_calls ?? 0 }}</div>
              </div>
              <div class="usage-card">
                <div class="usage-label">{{ t('settings.ai.failed_calls') }}</div>
                <div class="usage-value" :class="{ danger: aiUsage?.error_calls > 0 }">{{ aiUsage?.error_calls ?? 0 }}</div>
              </div>
              <div class="usage-card">
                <div class="usage-label">{{ t('settings.ai.total_tokens') }}</div>
                <div class="usage-value">{{ (aiUsage?.total_tokens ?? 0).toLocaleString() }}</div>
              </div>
              <div class="usage-card">
                <div class="usage-label">{{ t('settings.ai.avg_duration') }}</div>
                <!-- 没有调用时均值是"未测到"而不是 0ms -->
                <div class="usage-value">{{ aiUsage?.total_calls ? `${aiUsage.avg_duration_ms ?? 0} ms` : '—' }}</div>
              </div>
              <div class="usage-card wide">
                <div class="usage-label">{{ t('settings.ai.by_purpose') }}</div>
                <div class="usage-value" style="font-size: 13px">
                  <template v-if="aiUsage?.by_purpose?.length">
                    <span v-for="p in aiUsage.by_purpose" :key="p.purpose" class="purpose-chip">
                      {{ purposeLabel(p.purpose) }}: {{ t('settings.ai.calls_n', { n: p.calls }) }} / {{ p.total_tokens.toLocaleString() }}t
                    </span>
                  </template>
                  <span v-else class="tip">{{ aiUsage ? t('settings.ai.no_calls') : t('settings.ai.loading') }}</span>
                </div>
              </div>
            </div>

            <el-table stripe :data="aiLogs" v-loading="aiLogsLoading" size="small" border>
              <el-table-column type="expand">
                <template #default="{ row }">
                  <div class="log-expand">
                    <div v-if="row.error">{{ t('settings.ai.error_prefix') }}{{ row.error }}</div>
                    <div>{{ t('settings.ai.config_prefix', { id: row.setting_id, name: row.provider_label, model: row.model }) }}</div>
                  </div>
                </template>
              </el-table-column>
              <el-table-column prop="created_at" :label="t('settings.col.time')" width="180">
                <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
              </el-table-column>
              <el-table-column :label="t('settings.col.purpose')" width="110">
                <template #default="{ row }">{{ purposeLabel(row.purpose) }}</template>
              </el-table-column>
              <el-table-column prop="model" :label="t('settings.col.model')" width="150" />
              <el-table-column label="Tokens" width="160" align="right">
                <template #default="{ row }">
                  {{ row.prompt_tokens }} + {{ row.completion_tokens }} = {{ row.total_tokens }}
                </template>
              </el-table-column>
              <el-table-column :label="t('settings.col.duration')" width="90" align="right">
                <template #default="{ row }">{{ row.duration_ms }} ms</template>
              </el-table-column>
              <el-table-column :label="t('settings.col.result')" width="80">
                <template #default="{ row }">
                  <el-tag :type="row.status === 'ok' ? 'success' : 'danger'" size="small">
                    {{ row.status === 'ok' ? t('settings.ok') : t('settings.failed') }}
                  </el-tag>
                </template>
              </el-table-column>
            </el-table>
            <div style="display: flex; justify-content: flex-end; margin-top: 8px">
              <el-pagination
                v-model:current-page="aiLogsQuery.page"
                :page-size="aiLogsQuery.page_size"
                :total="aiLogsTotal"
                layout="total, prev, pager, next"
                small background
                @current-change="loadAiUsage"
              />
            </div>
          </el-tab-pane>
          <el-tab-pane :label="t('settings.tab.sdk')" name="sdk">
            <el-card shadow="never">
              <template #header>
                <b>{{ t('settings.sdk.title') }}</b>
                <span class="tip" style="margin-left: 10px">
                  {{ t('settings.sdk.tip') }}
                </span>
              </template>
              <div class="hosts-row">
                <el-button type="primary" :loading="sdkScanning" @click="scanSdkNow">{{ t('settings.btn.scan_now') }}</el-button>
                <el-tag size="small" type="success">{{ t('settings.sdk.up_to_date', { n: sdkReport.up_to_date }) }}</el-tag>
                <el-tag size="small" type="warning">{{ t('settings.sdk.outdated', { n: sdkReport.outdated }) }}</el-tag>
                <el-tag size="small" type="danger">{{ t('settings.sdk.deprecated', { n: sdkReport.deprecated }) }}</el-tag>
                <el-tag size="small" type="info">{{ t('settings.sdk.not_installed', { n: sdkReport.not_installed }) }}</el-tag>
                <span class="tip">
                  {{ t('settings.sdk.last_scan') }}{{ sdkReport.scanned_at || t('settings.sdk.never_scanned') }}
                  <template v-if="sdkReport.scanned_at">
                   （{{ { manual: t('settings.sdk.trigger_manual'), timer: t('settings.sdk.trigger_timer') }[sdkReport.trigger] || sdkReport.trigger }}）
                  </template>
                  <template v-if="sdkReport.hint">· {{ sdkReport.hint }}</template>
                </span>
              </div>
              <el-table :data="sdkReport.items" size="small" border max-height="460" v-loading="sdkLoading"
                        :empty-text="t('settings.sdk.empty')">
                <el-table-column prop="pkg" :label="t('settings.col.pkg')" min-width="220" show-overflow-tooltip />
                <el-table-column prop="vendor" :label="t('settings.col.vendor')" width="80" />
                <el-table-column :label="t('settings.col.current_ver')" width="110">
                  <template #default="{ row }"><span class="mono">{{ row.current || t('settings.sdk.not_installed_short') }}</span></template>
                </el-table-column>
                <el-table-column :label="t('settings.col.latest_ver')" width="110">
                  <template #default="{ row }"><span class="mono">{{ row.latest || '—' }}</span></template>
                </el-table-column>
                <el-table-column :label="t('settings.col.status')" width="110">
                  <template #default="{ row }">
                    <!-- 后端返回 status_code，前端按当前语言本地化；旧记录由 sdkStatusOf 兼容中文状态 -->
                    <el-tag size="small" :type="SDK_STATUS_TAG[sdkStatusOf(row)] || 'info'">
                      {{ t(`settings.sdk.status.${sdkStatusOf(row)}`) }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column :label="t('settings.col.deprecated_hint')" min-width="280" show-overflow-tooltip>
                  <template #default="{ row }">{{ row.deprecated_hint || '—' }}</template>
                </el-table-column>
              </el-table>
              <div class="tip sdk-note">
                {{ t('settings.sdk.note') }}
              </div>
            </el-card>
          </el-tab-pane>
        </el-tabs>
      </el-tab-pane>

      <!-- 访问层不提供多用户/组织架构管理，仅保留访问令牌、归属规则和审计日志；
           登录鉴权仍由后端安全底座负责 -->
      <el-tab-pane :label="t('settings.tab.access')" name="org">
        <el-tabs v-model="tabSub.org" class="sub-tabs">
          <el-tab-pane :label="t('settings.tab.tokens')" name="tokens">
            <div class="toolbar">
              <el-button type="primary" @click="openTokenDialog">{{ t('settings.btn.new_token') }}</el-button>
              <span class="tip">{{ t('settings.token.tip') }}</span>
            </div>
            <el-table stripe :data="tokens" v-loading="tokenLoading" border>
              <el-table-column prop="name" :label="t('settings.col.name')" width="180" />
              <el-table-column label="Token" width="160">
                <template #default="{ row }">
                  <span class="mono">{{ row.token_prefix }}…</span>
                </template>
              </el-table-column>
              <el-table-column :label="t('settings.col.expires_at')" width="170">
                <template #default="{ row }">{{ row.expires_at || t('settings.token.never_expire') }}</template>
              </el-table-column>
              <el-table-column :label="t('settings.col.last_used')" width="170">
                <template #default="{ row }">{{ row.last_used_at || t('settings.token.never_used') }}</template>
              </el-table-column>
              <el-table-column :label="t('settings.col.created_at')" width="170">
                <template #default="{ row }">{{ row.created_at }}</template>
              </el-table-column>
              <el-table-column :label="t('settings.col.actions')" width="120" fixed="right">
                <template #default="{ row }">
                  <el-button size="small" type="danger" @click="revokeToken(row)">{{ t('settings.btn.revoke') }}</el-button>
                </template>
              </el-table-column>
            </el-table>

            <el-dialog v-model="tokenDialog" :title="createdToken ? t('settings.token.created_title') : t('settings.token.new_title')" width="520px">
              <template v-if="!createdToken">
                <el-form label-width="110px">
                  <el-form-item :label="t('settings.col.name')" required>
                    <el-input v-model="tokenForm.name" :placeholder="t('settings.token.name_ph')" maxlength="64" />
                  </el-form-item>
                  <el-form-item :label="t('settings.token.expires')">
                    <el-select v-model="tokenForm.expires_days" style="width: 100%">
                      <el-option :label="t('settings.token.never_expire')" :value="null" />
                      <el-option :label="t('settings.token.days30')" :value="30" />
                      <el-option :label="t('settings.token.days90')" :value="90" />
                      <el-option :label="t('settings.token.days365')" :value="365" />
                    </el-select>
                  </el-form-item>
                </el-form>
                <div class="dlg-guide">
                  <div class="dlg-guide-title">{{ t('settings.token.usage') }}</div>
                  <ul class="dlg-guide-list">
                    <li>{{ t('settings.token.usage1_a') }}<span class="mono">Authorization: Bearer qs_xxx…</span>{{ t('settings.token.usage1_b') }}</li>
                    <li>{{ t('settings.token.usage2') }}</li>
                  </ul>
                </div>
              </template>
              <template v-else>
                <div class="token-warn">{{ t('settings.token.warn') }}</div>
                <div class="token-plain mono">{{ createdToken }}</div>
              </template>
              <template #footer>
                <el-button v-if="!createdToken" @click="tokenDialog = false">{{ t('settings.btn.cancel') }}</el-button>
                <el-button v-if="!createdToken" type="primary" @click="saveToken">{{ t('settings.btn.create') }}</el-button>
                <el-button v-if="createdToken" type="primary" @click="copyToken">{{ t('settings.btn.copy') }}</el-button>
                <el-button v-if="createdToken" @click="tokenDialog = false">{{ t('settings.btn.close') }}</el-button>
              </template>
            </el-dialog>
          </el-tab-pane>
          <el-tab-pane :label="t('settings.tab.ownership')" name="ownership">
            <el-card shadow="never">
              <template #header>
                <div style="display: flex; align-items: center; gap: 12px">
                  <b>{{ t('settings.own.title') }}</b>
                  <span class="tip">{{ t('settings.own.tip') }}</span>
                  <el-button v-if="canManage" size="small" type="primary" @click="openOwnDialog(null)">{{ t('settings.btn.new_rule') }}</el-button>
                </div>
              </template>
              <el-table :data="ownRules" border size="small">
                <el-table-column prop="key_type" :label="t('settings.col.key_type')" width="90" />
                <el-table-column prop="key_value" :label="t('settings.col.key_value')" min-width="150" />
                <el-table-column :label="t('settings.col.match')" width="80">
                  <template #default="{ row }">{{ row.match_type === 'pattern' ? t('settings.own.pattern') : t('settings.own.exact') }}</template>
                </el-table-column>
                <el-table-column :label="t('settings.col.vendor_account')" min-width="150">
                  <template #default="{ row }">{{ [row.vendor, row.account_name].filter(Boolean).join(' · ') || '—' }}</template>
                </el-table-column>
                <el-table-column :label="t('settings.col.business')" width="110">
                  <template #default="{ row }">{{ row.business || '—' }}</template>
                </el-table-column>
                <el-table-column prop="note" :label="t('settings.col.remark')" min-width="120" />
                <el-table-column v-if="canManage" :label="t('settings.col.actions')" width="130">
                  <template #default="{ row }">
                    <el-button size="small" text type="primary" @click="openOwnDialog(row)">{{ t('settings.btn.edit') }}</el-button>
                    <el-button size="small" text type="danger" @click="deleteOwnRule(row)">{{ t('settings.btn.delete') }}</el-button>
                  </template>
                </el-table-column>
              </el-table>
            </el-card>

            <el-card shadow="never" style="margin-top: 12px">
              <template #header>
                <b>{{ t('settings.own.unattributed_title') }}</b>
                <span class="tip">{{ t('settings.own.unattributed_tip') }}</span>
              </template>
              <el-table :data="ownUnattributed" border size="small" max-height="280">
                <el-table-column prop="label" :label="t('settings.col.entity')" min-width="150" />
                <el-table-column prop="type" :label="t('settings.col.shape')" width="90" />
                <el-table-column :label="t('settings.col.suggested_key')" width="180">
                  <template #default="{ row }">{{ row.key_type }} = {{ row.key_value }}</template>
                </el-table-column>
                <el-table-column v-if="canManage" :label="t('settings.col.actions')" width="100">
                  <template #default="{ row }">
                    <el-button size="small" text type="primary" @click="ownFromUnattributed(row)">{{ t('settings.own.assign') }}</el-button>
                  </template>
                </el-table-column>
              </el-table>
            </el-card>

            <el-dialog v-model="ownDialog" :title="ownEditingId ? t('settings.own.edit_title') : t('settings.own.new_title')" width="560px">
              <el-form :model="ownForm" label-width="90px">
                <el-form-item :label="t('settings.own.entity_key')">
                  <el-select v-model="ownForm.key_type" style="width: 110px">
                    <el-option v-for="k in ownOptions.key_types" :key="k.code" :label="k.label" :value="k.code" />
                  </el-select>
                  <el-input v-model="ownForm.key_value" style="width: 290px"
                            :placeholder="t('settings.own.key_ph')" @change="runOwnPreview()" />
                </el-form-item>
                <el-form-item :label="t('settings.own.match')">
                  <el-radio-group v-model="ownForm.match_type">
                    <el-radio-button value="exact">{{ t('settings.own.exact') }}</el-radio-button>
                    <el-radio-button value="pattern">{{ t('settings.own.pattern') }}</el-radio-button>
                  </el-radio-group>
                  <span class="tip" style="margin-left: 10px">{{ t('settings.own.pattern_tip') }}</span>
                </el-form-item>
                <el-form-item :label="t('settings.own.vendor')">
                  <el-select v-model="ownForm.vendor" clearable filterable allow-create style="width: 200px" :placeholder="t('settings.own.optional')">
                    <el-option v-for="v in ownOptions.vendors" :key="v.code" :label="v.label" :value="v.code" />
                  </el-select>
                </el-form-item>
                <el-form-item :label="t('settings.own.account')">
                  <el-input v-model="ownForm.account_name" style="width: 200px" :placeholder="t('settings.own.account_ph')" />
                </el-form-item>
                <el-form-item :label="t('settings.own.bind_ledger')">
                  <el-select v-model="ownForm.account_id" clearable style="width: 200px"
                             :placeholder="t('settings.own.bind_ph')">
                    <el-option v-for="a in ownAccounts" :key="a.id" :label="a.name" :value="a.id" />
                  </el-select>
                </el-form-item>
                <el-form-item :label="t('settings.own.business')">
                  <el-input v-model="ownForm.business" style="width: 200px" :placeholder="t('settings.own.business_ph')" />
                </el-form-item>
                <el-form-item :label="t('settings.col.remark')">
                  <el-input v-model="ownForm.note" type="textarea" :rows="2" />
                </el-form-item>
                <el-form-item :label="t('settings.own.hints')">
                  <div>
                    <div v-if="ownPreview" class="tip">{{ t('settings.own.preview_hit', { id: ownPreview.rule_id }) }}</div>
                    <div v-for="(h, i) in ownHints" :key="i" style="margin: 2px 0">
                      {{ h.ref }}
                      <span v-if="h.account" class="tip">（{{ h.account }}）</span>
                      <el-button v-if="h.suggest && h.suggest.account_name" size="small" text type="primary"
                                 @click="adoptHint(h)">{{ t('settings.own.adopt') }}</el-button>
                    </div>
                    <div v-if="!ownPreview && !ownHints.length" class="tip">{{ t('settings.own.no_hints') }}</div>
                  </div>
                </el-form-item>
              </el-form>
              <template #footer>
                <el-button @click="ownDialog = false">{{ t('settings.btn.cancel') }}</el-button>
                <el-button type="primary" @click="saveOwnRule()">{{ t('settings.btn.save') }}</el-button>
              </template>
            </el-dialog>
          </el-tab-pane>
          <el-tab-pane v-if="isAdmin" :label="t('settings.tab.audit')" name="audit">
            <el-card shadow="never">
              <template #header>
                <div style="display: flex; align-items: center; gap: 12px">
                  <span>{{ t('settings.audit.title') }}</span>
                  <el-input v-model="auditQuery.action" :placeholder="t('settings.audit.filter_ph')"
                            clearable style="width: 280px"
                            @keyup.enter="() => { auditQuery.page = 1; loadAudit() }"
                            @clear="() => { auditQuery.page = 1; loadAudit() }" />
                  <el-button size="small" @click="() => { auditQuery.page = 1; loadAudit() }">{{ t('settings.btn.query') }}</el-button>
                </div>
              </template>
              <el-table stripe v-loading="auditLoading" :data="auditRows" size="small" border>
                <el-table-column prop="created_at" :label="t('settings.col.time')" width="160">
                  <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
                </el-table-column>
                <el-table-column prop="username" :label="t('settings.col.operator')" width="100" />
                <el-table-column :label="t('settings.col.what')" min-width="320" show-overflow-tooltip>
                  <template #default="{ row }">
                    <b>{{ row.action_label || row.action }}</b>
                    <!-- data-raw-ok：动作码是刻意保留的技术标识（排查用），走查质量契约不当作漏翻 -->
                    <span class="sub mono" data-raw-ok>{{ row.action }}</span>
                    <div class="sub">{{ row.summary }}</div>
                  </template>
                </el-table-column>
                <el-table-column :label="t('settings.col.source_ip')" width="130">
                  <template #default="{ row }">
                    <!-- 智能体代操作使用 copilot 占位 client_ip，审计中必须显式标明 -->
                    <el-tag v-if="row.client_ip === 'copilot'" size="small" type="warning">{{ t('settings.audit.agent_via') }}</el-tag>
                    <span v-else>{{ row.client_ip }}</span>
                  </template>
                </el-table-column>
                <el-table-column :label="t('settings.col.result')" width="80">
                  <template #default="{ row }">
                    <el-tag :type="row.result === 'ok' ? 'success' : 'danger'" size="small">
                      {{ row.result === 'ok' ? t('settings.ok') : row.result }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column :label="t('settings.col.raw_detail')" min-width="160" show-overflow-tooltip>
                  <!-- 原始 detail 是排查用的技术 JSON（含 via 等内部标识），不算漏翻 -->
                  <template #default="{ row }"><span class="mono" data-raw-ok>{{ row.detail || '-' }}</span></template>
                </el-table-column>
              </el-table>
              <div style="display: flex; justify-content: flex-end; margin-top: 10px">
                <el-pagination background layout="total, prev, pager, next" :total="auditTotal"
                  :page-size="auditQuery.page_size" :current-page="auditQuery.page"
                  @current-change="(p) => { auditQuery.page = p; loadAudit() }" />
              </div>
            </el-card>
          </el-tab-pane>
        </el-tabs>
      </el-tab-pane>

      <el-tab-pane :label="t('settings.tab.platform')" name="platform">
        <el-tabs v-model="tabSub.platform" class="sub-tabs">
          <el-tab-pane :label="t('settings.tab.sysparams')" name="sysparams">
            <el-card shadow="never">
              <template #header>
                <div style="display: flex; align-items: center; gap: 12px">
                  <b>{{ t('settings.sysparams.title') }}</b>
                  <el-tag v-if="retentionDirty" type="warning" size="small">{{ t('settings.sysparams.unsaved') }}</el-tag>
                  <el-tag v-else-if="retentionSaved" type="success" size="small">{{ t('settings.sysparams.saved_ok') }}</el-tag>
                </div>
              </template>
              <div class="tip" style="margin-bottom: 10px">
                {{ t('settings.sysparams.tip') }}
              </div>
              <div class="retention-grid">
                <div v-for="row in retentionRows" :key="row.key" class="retention-row">
                  <span class="retention-label">{{ t(row.label) }}</span>
                  <el-input-number v-model="retention[row.key]" :min="0" :max="rowMax(row)" size="small"
                                   @change="retentionDirty = true" />
                  <span>{{ t('settings.sysparams.days') }}</span>
                  <span v-if="row.tip" class="tip">{{ t(row.tip) }}</span>
                </div>
              </div>
              <div class="tip" style="margin-top: 10px">
                {{ t('settings.retention.cap_note', { vm: retentionCaps.vm, vl: retentionCaps.vl }) }}
              </div>
              <div style="display: flex; justify-content: flex-end; margin-top: 12px">
                <!-- 保存=立即按新规则清存量；权限与后端一致（admin） -->
                <el-button v-if="isAdmin" type="primary" size="small"
                           :disabled="!retentionDirty && !retentionSaved"
                           @click="saveRetention">
                  {{ retentionDirty ? t('settings.sysparams.save_now') : t('settings.btn.save') }}
                </el-button>
              </div>
            </el-card>
          </el-tab-pane>
          <el-tab-pane :label="t('settings.tab.accounts')" name="accounts">
            <el-card shadow="never">
              <template #header>
                <div style="display: flex; align-items: center; gap: 12px">
                  <b>{{ t('settings.accounts.title') }}</b>
                  <!-- 此处是数据归属分组，不是云账号管理中的云厂商 AK -->
                  <span class="tip">{{ t('settings.accounts.tip') }}</span>
                  <el-button v-if="canManage" size="small" type="primary" @click="accDialog = true">{{ t('settings.btn.new_group') }}</el-button>
                </div>
              </template>
              <el-table :data="accounts" border size="small">
                <el-table-column prop="id" label="ID" width="60" />
                <el-table-column prop="name" :label="t('settings.col.name')" width="160" />
                <el-table-column prop="kind" :label="t('settings.col.type')" width="90" />
                <el-table-column prop="remark" :label="t('settings.col.remark')" min-width="180" />
              </el-table>
              <div class="tip" style="margin-top: 8px">
                {{ t('settings.accounts.foot') }}
              </div>
            </el-card>
            <el-dialog v-model="accDialog" :title="t('settings.accounts.new_title')" width="420px">
              <el-form :model="accForm" label-width="80px">
                <el-form-item :label="t('settings.col.name')"><el-input v-model="accForm.name" /></el-form-item>
                <el-form-item :label="t('settings.col.type')">
                  <el-radio-group v-model="accForm.kind">
                    <el-radio-button value="cloud">{{ t('settings.accounts.kind_cloud') }}</el-radio-button>
                    <el-radio-button value="idc">IDC</el-radio-button>
                  </el-radio-group>
                </el-form-item>
                <el-form-item :label="t('settings.col.remark')"><el-input v-model="accForm.remark" /></el-form-item>
              </el-form>
              <template #footer>
                <el-button @click="accDialog = false">{{ t('settings.btn.cancel') }}</el-button>
                <el-button type="primary" @click="createAccount">{{ t('settings.btn.create') }}</el-button>
              </template>
            </el-dialog>
          </el-tab-pane>
          <el-tab-pane :label="t('settings.tab.holiday')" name="holiday">
            <el-card shadow="never">
              <template #header>
                <b>{{ t('settings.holiday.title') }}</b>
                <span class="tip" style="margin-left: 10px">{{ holiday.usage }}</span>
              </template>
              <div class="hosts-row">
                <el-select v-model="holidayYear" :placeholder="t('settings.holiday.all_years')" clearable style="width: 130px" @change="loadHoliday">
                  <el-option v-for="y in holiday.years" :key="y" :label="t('settings.holiday.year_opt', { y })" :value="y" />
                </el-select>
                <el-button type="primary" :loading="holidaySyncing" @click="syncHoliday">{{ t('settings.btn.sync_now') }}</el-button>
                <el-tag size="small" type="success">{{ t('settings.holiday.holidays', { n: holiday.holidays }) }}</el-tag>
                <el-tag size="small" type="warning">{{ t('settings.holiday.workdays', { n: holiday.adjusted_workdays }) }}</el-tag>
                <span class="tip">{{ t('settings.holiday.source_label') }}{{ holiday.source }} · {{ t('settings.holiday.last_sync') }}{{ holiday.last_sync || '—' }}</span>
              </div>
              <el-table :data="holiday.items" size="small" border max-height="420"
                        :empty-text="t('settings.holiday.empty')">
                <el-table-column prop="day" :label="t('settings.col.day')" width="130" />
                <el-table-column :label="t('settings.col.type')" width="110">
                  <template #default="{ row }">
                    <el-tag size="small" :type="row.kind === 'holiday' ? 'danger' : (row.kind === 'workday' ? 'warning' : 'info')">
                      {{ { holiday: t('settings.holiday.kind_holiday'), workday: t('settings.holiday.kind_workday'), rest: t('settings.holiday.kind_rest') }[row.kind] || row.kind }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column prop="name" :label="t('settings.col.holiday_name')" min-width="140" />
                <el-table-column prop="source" :label="t('settings.col.source')" width="90" />
              </el-table>
            </el-card>
          </el-tab-pane>
          <el-tab-pane v-if="isAdmin" :label="t('settings.tab.hosts')" name="hosts">
            <el-card shadow="never">
              <template #header>
                <b>{{ t('settings.hosts.title') }}</b>
                <span class="tip" style="margin-left: 10px">
                  {{ t('settings.hosts.tip') }}
                </span>
              </template>
              <div class="hosts-row">
                <el-input v-model="newHost" :placeholder="t('settings.hosts.ph')" style="width: 320px"
                          @keyup.enter="addHost" />
                <el-button type="primary" plain @click="addHost">{{ t('settings.btn.add') }}</el-button>
                <el-button type="primary" :loading="hostsSaving" @click="saveAllowedHosts">{{ t('settings.btn.save_apply') }}</el-button>
                <el-tag size="small" :type="allowedHostsMode === 'open' ? 'info' : 'success'">
                  {{ allowedHostsMode === 'open' ? t('settings.hosts.mode_open') : t('settings.hosts.mode_whitelist') }}
                </el-tag>
              </div>
              <div class="hosts-list">
                <el-tag v-for="h in allowedHosts" :key="h" closable size="large"
                        @close="allowedHosts = allowedHosts.filter((x) => x !== h)">
                  {{ h }}
                </el-tag>
                <span v-if="!allowedHosts.length" class="tip">{{ t('settings.hosts.empty') }}</span>
              </div>
            </el-card>
          </el-tab-pane>
        </el-tabs>
      </el-tab-pane>
    </el-tabs>

    <!-- 通知渠道编辑对话框 -->
    <el-dialog v-model="channelDialog" :title="editingChannel ? t('settings.channel.edit_title') : t('settings.channel.add_title')" width="520px">
      <el-form label-width="110px">
        <el-form-item :label="t('settings.col.name')" required>
          <el-input v-model="channelForm.name" :placeholder="t('settings.channel.name_ph')" maxlength="64" />
        </el-form-item>
        <el-form-item :label="t('settings.col.type')" required>
          <el-select v-model="channelForm.type" style="width: 100%">
            <el-option :label="t('settings.channel.wecom')" value="wecom_bot" />
            <el-option :label="t('settings.channel.dingtalk')" value="dingtalk_bot" />
            <el-option :label="t('settings.channel.webhook')" value="webhook" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="channelForm.type === 'webhook'" :label="t('settings.channel.body')">
          <!-- 通用 webhook 显示请求体示例 -->
          <div class="dlg-code">{{ t('settings.channel.body_example') }}</div>
        </el-form-item>
        <el-form-item label="Webhook" :required="!editingChannel">
          <el-input
            v-model="channelForm.webhook_url"
            type="password"
            show-password
            :placeholder="editingChannel ? t('settings.channel.keep_blank') : (channelForm.type === 'dingtalk_bot' ? 'https://oapi.dingtalk.com/robot/send?access_token=...' : 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...')"
          />
        </el-form-item>
        <el-form-item v-if="channelForm.type === 'dingtalk_bot'" :label="t('settings.channel.at_label')">
          <el-input v-model="channelForm.at_mobiles" :placeholder="t('settings.channel.at_ph')" />
        </el-form-item>
        <el-form-item :label="t('settings.channel.enabled')">
          <el-switch v-model="channelForm.enabled" />
        </el-form-item>
        <el-form-item :label="t('settings.channel.tls')">
          <el-switch v-model="channelForm.verify_ssl" />
          <span class="tip" style="margin-left: 8px">{{ t('settings.channel.tls_tip') }}</span>
        </el-form-item>
      </el-form>
      <!-- 教程与注意事项 -->
      <div class="dlg-guide">
        <div class="dlg-guide-title">{{ t('settings.channel.guide_title') }}</div>
        <ul class="dlg-guide-list">
          <li>{{ t('settings.channel.guide1') }}</li>
          <li>{{ t('settings.channel.guide2') }}</li>
        </ul>
        <div class="dlg-guide-title">{{ t('settings.guide.notes') }}</div>
        <ul class="dlg-guide-list">
          <li>{{ t('settings.channel.note1') }}</li>
          <li>{{ t('settings.channel.note2') }}</li>
          <li>{{ t('settings.channel.note3') }}</li>
        </ul>
      </div>
      <template #footer>
        <el-button @click="channelDialog = false">{{ t('settings.btn.cancel') }}</el-button>
        <el-button type="primary" @click="saveChannel">{{ t('settings.btn.save') }}</el-button>
      </template>
    </el-dialog>

    <!-- AI 模型编辑对话框 -->
    <el-dialog v-model="aiDialog" :title="editingAi ? t('settings.ai.edit_title') : t('settings.ai.add_title')" width="520px">
      <el-form label-width="110px">
        <el-form-item :label="t('settings.col.name')" required>
          <el-input v-model="aiForm.provider_label" :placeholder="t('settings.ai.name_ph')" maxlength="64" />
        </el-form-item>
        <el-form-item label="Base URL" required>
          <el-input v-model="aiForm.base_url" :placeholder="t('settings.ai.base_ph')" />
        </el-form-item>
        <el-form-item label="API Key" :required="!editingAi">
          <el-input v-model="aiForm.api_key" type="password" show-password
                    :placeholder="editingAi ? t('settings.ai.keep_blank') : t('settings.ai.key_ph')" />
        </el-form-item>
        <el-form-item :label="t('settings.ai.model_name')" required>
          <el-input v-model="aiForm.model" :placeholder="t('settings.ai.model_ph')" />
        </el-form-item>
        <el-form-item label="Temperature">
          <el-input-number v-model="aiForm.temperature" :min="0" :max="2" :step="0.1" />
        </el-form-item>
        <el-form-item label="Max Tokens">
          <el-input-number v-model="aiForm.max_tokens" :min="1" :max="393216" :step="256" />
        </el-form-item>
        <el-form-item :label="t('settings.col.remark')">
          <el-input v-model="aiForm.remark" maxlength="255" />
        </el-form-item>
        <el-form-item :label="t('settings.ai.enabled_label')">
          <el-switch v-model="aiForm.enabled" />
          <span class="tip" style="margin-left: 8px">{{ t('settings.ai.enabled_tip') }}</span>
        </el-form-item>
        <el-form-item :label="t('settings.channel.tls')">
          <el-switch v-model="aiForm.verify_ssl" />
          <span class="tip" style="margin-left: 8px">{{ t('settings.ai.tls_tip') }}</span>
        </el-form-item>
      </el-form>
      <!-- 教程与注意事项 -->
      <div class="dlg-guide">
        <div class="dlg-guide-title">{{ t('settings.ai.guide_title') }}</div>
        <ul class="dlg-guide-list">
          <li>{{ t('settings.ai.guide1') }}</li>
          <li>{{ t('settings.ai.guide2') }}</li>
          <li>{{ t('settings.ai.guide3') }}</li>
        </ul>
        <div class="dlg-guide-title">{{ t('settings.guide.notes') }}</div>
        <ul class="dlg-guide-list">
          <li>{{ t('settings.ai.note1') }}</li>
          <li>{{ t('settings.ai.note2') }}</li>
          <li>{{ t('settings.ai.note3') }}</li>
        </ul>
      </div>
      <template #footer>
        <el-button @click="aiDialog = false">{{ t('settings.btn.cancel') }}</el-button>
        <el-button :loading="testingAiForm" @click="testAiForm">{{ t('settings.btn.test_conn') }}</el-button>
        <el-button type="primary" @click="saveAi">{{ t('settings.btn.save') }}</el-button>
      </template>
    </el-dialog>

    <!-- 连通性测试结果 -->
    <el-dialog v-model="aiTestDialog" :title="t('settings.ai.test_result_title')" width="480px">
      <template v-if="aiTestResult">
        <el-result
          :icon="aiTestResult.ok ? 'success' : 'error'"
          :title="aiTestResult.ok ? t('settings.ai.test_ok_title', { model: aiTestResult.model }) : t('settings.ai.test_fail_title')"
        >
          <template #sub-title>
            <div v-if="aiTestResult.ok">
              <p>{{ t('settings.ai.test_meta', {
                ms: aiTestResult.duration_ms,
                pt: aiTestResult.usage?.prompt_tokens ?? 0,
                ct: aiTestResult.usage?.completion_tokens ?? 0,
              }) }}</p>
            </div>
            <p v-else class="danger-text">{{ aiTestResult.error }}</p>
          </template>
          <template #extra>
            <div v-if="aiTestResult.reply" class="test-reply">「{{ aiTestResult.reply }}」</div>
          </template>
        </el-result>
      </template>
      <template #footer>
        <el-button type="primary" @click="aiTestDialog = false">{{ t('settings.btn.got_it') }}</el-button>
      </template>
    </el-dialog>

  </div>
</template>

<style scoped>
.hosts-row { display: flex; gap: 10px; align-items: center; margin-bottom: 14px; flex-wrap: wrap; }
.sub-tabs { margin-top: 2px; }
.retention-grid { display: flex; flex-direction: column; gap: 10px; }
.retention-row { display: flex; align-items: center; gap: 10px; }
.retention-label { width: 210px; }
.hosts-list { display: flex; gap: 8px; flex-wrap: wrap; }
.sdk-note { margin-top: 12px; line-height: 1.8; }
.mono {
  font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 12px;
  word-break: break-all;
}
.token-warn {
  margin-bottom: 10px;
  font-size: 13px;
  color: var(--el-color-warning);
}
.token-plain {
  padding: 12px;
  border-radius: 8px;
  background: var(--el-fill-color-light);
  font-size: 13px;
  user-select: all;
}
.toolbar {
  margin-bottom: 12px;
  display: flex;
  align-items: center;
  gap: 12px;
}
.tip {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.usage-title {
  font-size: 14px;
  font-weight: 600;
  margin: 20px 0 10px;
}
.usage-cards {
  display: grid;
  grid-template-columns: repeat(4, 1fr) 2fr;
  gap: 10px;
  margin-bottom: 12px;
}
.usage-card {
  padding: 10px 14px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: var(--el-bg-color-overlay);
}
.usage-card.wide {
  grid-column: span 1;
}
.usage-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.usage-value {
  font-size: 20px;
  font-weight: 600;
  margin-top: 4px;
}
.usage-value.danger {
  color: var(--el-color-danger);
}
.purpose-chip {
  display: inline-block;
  margin: 2px 8px 2px 0;
  white-space: nowrap;
}
.log-expand {
  padding: 4px 12px;
  font-size: 12px;
  line-height: 1.8;
  color: var(--el-text-color-secondary);
}
.danger-text {
  color: var(--el-color-danger);
  font-size: 12px;
  word-break: break-all;
  white-space: pre-wrap;
}
.test-reply {
  font-size: 14px;
  padding: 8px 12px;
  border-radius: 6px;
  background: var(--el-fill-color-light);
  max-width: 380px;
  word-break: break-all;
}
/* 教程与注意事项 */
.dlg-guide {
  margin-top: 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  padding: 10px 14px;
  background: var(--el-fill-color-lighter);
}
.dlg-guide-title {
  font-size: 12.5px;
  font-weight: 700;
  margin: 6px 0 4px;
  padding-left: 8px;
  border-left: 3px solid var(--el-color-primary);
}
.dlg-guide-title:first-child { margin-top: 0; }
.dlg-guide-list {
  margin: 0;
  padding-left: 18px;
  font-size: 12px;
  line-height: 1.9;
  color: var(--el-text-color-regular);
}
.dlg-code {
  font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 11.5px;
  padding: 6px 10px;
  border-radius: 6px;
  background: var(--el-fill-color-light);
  word-break: break-all;
}
</style>
