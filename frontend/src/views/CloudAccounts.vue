<script setup>
// 云账号管理：
// 厂商徽标/区域/状态/最近测试 + 新增编辑对话框（AK/SK 加密入库只进不出）+ 连通测试 + 删除。
// 混合云：多厂商多账号并存；腾讯云 CVM/CDB/Redis/CLB/COS 清单同步已接；其余厂商本期支持登记与连通测试。
// vCenter/ESXi/KVM：四步接入向导（①地址与凭证 ②SSL 校验 ③测试连通取版本 ④完成），
// SSL 跳过开关默认开（vCenter 自签证书；仅内网可信环境使用），探活取到版本才算通。
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useRoute, useRouter } from 'vue-router'

import {
  createCloudAccount, deleteCloudAccount, getAccountOnboarding, listCloudAccounts, listVendors,
  testCloudAccount, testCloudAccountUnsaved, updateCloudAccount,
} from '@/api/clouds'
import CloudCredentialForm from '@/components/CloudCredentialForm.vue'
import TaskResultCard from '@/components/TaskResultCard.vue'
import { fmtTime } from '@/utils/format'
import { listChangeSources } from '@/api/changes'
import { useAuthStore } from '@/stores/auth'
import { locale, t } from '@/i18n'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const isAdmin = computed(() => auth.role === 'admin')
const canWrite = computed(() => ['admin', 'operator'].includes(auth.role))

// 厂商选项兜底（仅后端 /clouds/vendors 不可用时使用；禁止继续扩充——
// 能力位/档位以服务端 provider.py 为唯一事实源）
const VENDORS = [
  { value: 'aliyun', labelKey: 'clouds.vendor.aliyun', tag: 'danger' },
  { value: 'tencent', labelKey: 'clouds.vendor.tencent', tag: 'primary' },
  { value: 'volc', labelKey: 'clouds.vendor.volc', tag: 'warning' },
  { value: 'baidu', labelKey: 'clouds.vendor.baidu', tag: 'info' },
  { value: 'huawei', labelKey: 'clouds.vendor.huawei', tag: 'warning' },
  { value: 'aws', labelKey: 'clouds.vendor.aws', tag: 'success' },
  { value: 'azure', labelKey: 'clouds.vendor.azure', tag: 'primary' },
  { value: 'esxi', labelKey: 'clouds.vendor.esxi', tag: 'success' },
  { value: 'vcenter', labelKey: 'clouds.vendor.vcenter', tag: 'warning' },
  { value: 'kvm', labelKey: 'clouds.vendor.kvm', tag: 'success' },
]
const VENDOR_MAP = Object.fromEntries(VENDORS.map((v) => [v.value, v]))
const vendorLabel = (v) => (VENDOR_MAP[v]?.labelKey ? t(VENDOR_MAP[v].labelKey) : (v || ''))

// 能力位兜底（与后端矩阵一致；正常路径由 /clouds/vendors 覆盖）
const FALLBACK_CAP = {
  aliyun: { tier: 'asset_billing', register: true, probe: true, sync: true, billing: true, power: true, metrics: true, agent: false, needs_ak_sk: true },
  tencent: { tier: 'asset_billing', register: true, probe: true, sync: true, billing: true, power: false, metrics: false, agent: false, needs_ak_sk: true },
  volc: { tier: 'register_only', probe: false, sync: false, billing: false, metrics: false, agent: false, needs_ak_sk: true },
  baidu: { tier: 'register_only', probe: false, sync: false, billing: false, metrics: false, agent: false, needs_ak_sk: true },
  huawei: { tier: 'register_only', probe: false, sync: false, billing: false, metrics: false, agent: false, needs_ak_sk: true },
  aws: { tier: 'register_only', probe: false, sync: false, billing: false, metrics: false, agent: false, needs_ak_sk: true },
  azure: { tier: 'register_only', probe: false, sync: false, billing: false, metrics: false, agent: false, needs_ak_sk: true },
  esxi: { tier: 'asset_sync', probe: true, sync: true, billing: false, metrics: false, agent: false, needs_ak_sk: false },
  vcenter: { tier: 'asset_sync', probe: true, sync: true, billing: false, metrics: false, agent: false, needs_ak_sk: false },
  kvm: { tier: 'probe_only', probe: true, sync: false, billing: false, metrics: false, agent: false, needs_ak_sk: false },
}
const vendorCaps = ref({ ...FALLBACK_CAP })
const vendors = ref([...VENDORS])
const changeSources = ref([])
const changeSourceDefaults = ref({})
async function loadChangeSources() {
  try {
    const r = await listChangeSources()
    changeSources.value = r.items || []
    changeSourceDefaults.value = r.defaults || {}
  } catch { /* 后端不可用时保留自动选择；保存会由后端做真实校验 */ }
}
function automaticChangeSourceLabel() {
  const key = changeSourceDefaults.value[form.vendor]
    || changeSourceDefaults.value[editing.value?.vendor]
  const source = changeSources.value.find((x) => x.key === key)
  return t('clouds.form.change_source_auto', { source: source?.label || t('clouds.form.change_source_none') })
}
const availableChangeSources = computed(() =>
  changeSources.value.filter((s) => !s.vendor || s.vendor === form.vendor))
const capOf = (v) => vendorCaps.value[v] || { tier: 'register_only', probe: false, sync: false, billing: false, metrics: false, agent: false, needs_ak_sk: true }
const tierLabel = (tier) => t(`clouds.cap.tier.${tier}`)
// Agent 上报的裸机不作为云账号厂商：裸机去「机器管理」登记，
// 这里只保留有凭证的采集源与仅登记厂商；空分组不渲染。
const vendorGroups = computed(() => [
  { key: 'collect', label: t('clouds.cap.group_collect'), items: vendors.value.filter((v) => capOf(v.value).sync) },
  { key: 'register', label: t('clouds.cap.group_register'), items: vendors.value.filter((v) => !capOf(v.value).sync) },
].filter((g) => g.items.length))
function capTip(v) {
  const c = capOf(v)
  if (c.agent) return t('clouds.cap.tip.agent')
  if (!c.probe) return t('clouds.cap.tip.register_only', { vendor: vendorLabel(v) })
  if (!c.sync) return t('clouds.cap.tip.probe_only', { vendor: vendorLabel(v) })
  return ''
}

async function loadVendors() {
  try {
    const r = await listVendors()
    const items = r.items || []
    if (!items.length) return
    vendors.value = items.map((x) => ({
      value: x.vendor,
      labelKey: x.label_key || `clouds.vendor.${x.vendor}`,
      tag: VENDOR_MAP[x.vendor]?.tag || 'info',
      tier: x.tier,
    }))
    vendorCaps.value = Object.fromEntries(items.map((x) => [x.vendor, x.capabilities]))
  } catch { /* 后端不可用：静默回退内置兜底，不阻断账号列表 */ }
}

// 账号状态：probing/syncing/seeding 均为接入非终态；终态以共享 Run 为准。
const STATUS_MAP = {
  untested: { labelKey: 'clouds.status.untested', type: 'info' },
  probing: { labelKey: 'clouds.status.probing', type: 'warning' },
  syncing: { labelKey: 'clouds.status.syncing', type: 'warning' },
  seeding: { labelKey: 'clouds.status.seeding', type: 'warning' },
  ok: { labelKey: 'clouds.status.ok', type: 'success' },
  error: { labelKey: 'clouds.status.error', type: 'danger' },
  unsupported: { labelKey: 'clouds.status.unsupported', type: 'warning' },
  attention: { labelKey: 'clouds.status.attention', type: 'warning' },
  failed: { labelKey: 'clouds.status.failed', type: 'danger' },
}
const ONBOARDING_STATUSES = ['probing', 'syncing', 'seeding']
const RUN_DISPLAY_STATUS = { ready: 'ok', attention: 'attention', failed: 'failed', waiting: 'untested' }
const displayStatus = (row) => RUN_DISPLAY_STATUS[row.onboarding_status] || row.status

const items = ref([])
const loading = ref(false)
const keyword = ref('')
const testingId = ref(0)

// 当前账号只显示一个共享 Run；切账号使旧请求失效，轮询只更新原卡。
const selectedAccountId = ref(0)
const selectedRun = ref(null)
const acceptedRun = ref(null)
const taskLoading = ref(false)
const taskError = ref('')
let taskRequestSeq = 0
let taskPollTimer = null
const selectedAccount = computed(() => items.value.find((x) => x.id === selectedAccountId.value) || null)

const SECRET_QUERY_KEYS = ['ak', 'sk', 'secret', 'token', 'password']
const FORM_QUERY_KEYS = ['new', 'edit', 'vendor', 'name', 'region']
function sanitizedQuery(source, dropForm = false) {
  const query = { ...source }
  SECRET_QUERY_KEYS.forEach((key) => delete query[key])
  if (dropForm) FORM_QUERY_KEYS.forEach((key) => delete query[key])
  return query
}
function scrubRoute(dropForm = false) {
  const query = sanitizedQuery(route.query, dropForm || !isAdmin.value)
  const changed = SECRET_QUERY_KEYS.some((key) => key in route.query)
    || ((dropForm || !isAdmin.value) && FORM_QUERY_KEYS.some((key) => key in route.query))
  if (changed) router.replace({ query })
}

function taskRoute(accountId, runId = 0) {
  const query = { ...sanitizedQuery(route.query, true),
    account_id: String(accountId), ...(runId ? { run_id: String(runId) } : {}) }
  router.replace({ query })
}
function updateSelectedRun(run) {
  if (!run || run.account_id !== selectedAccountId.value) return
  if (selectedRun.value && run.id !== selectedRun.value.id) return
  if (Number(run.attempts || 1) < Number(selectedRun.value?.attempts || 1)) return
  selectedRun.value = run
  acceptedRun.value = null
}
async function loadSelectedTask() {
  const accountId = selectedAccountId.value
  if (!accountId) { selectedRun.value = null; return }
  clearTimeout(taskPollTimer)
  const requestSeq = ++taskRequestSeq
  taskLoading.value = true
  taskError.value = ''
  try {
    const run = await getAccountOnboarding(accountId)
    if (requestSeq !== taskRequestSeq || accountId !== selectedAccountId.value) return
    updateSelectedRun(run)
  } catch {
    if (requestSeq === taskRequestSeq && accountId === selectedAccountId.value) {
      taskError.value = t('clouds.task.load_failed')
    }
  } finally {
    if (requestSeq === taskRequestSeq && accountId === selectedAccountId.value) {
      taskLoading.value = false
      if (taskError.value || ['running', 'waiting'].includes(selectedRun.value?.status)) {
        taskPollTimer = setTimeout(loadSelectedTask, 2500)
      }
    }
  }
}
function selectTask(row, updateRoute = true) {
  if (!row?.id) return
  const runId = row.onboarding_run_id || 0
  selectedAccountId.value = row.id
  if (selectedRun.value?.account_id !== row.id || (runId && selectedRun.value?.id !== runId)) {
    selectedRun.value = null
  }
  acceptedRun.value = acceptedRun.value?.account_id === row.id ? acceptedRun.value : null
  taskRequestSeq += 1
  if (updateRoute) taskRoute(row.id, runId)
  loadSelectedTask()
}
function editTaskCredential(run) {
  const row = items.value.find((x) => x.id === run.account_id)
  if (row && isAdmin.value) openEdit(row)
}
function confirmTaskLogs(run) {
  router.push({ path: '/bootstrap', hash: '#sls-workspace',
    query: { account_id: run.account_id, run_id: run.id } })
}

const filtered = computed(() => {
  const k = keyword.value.trim().toLowerCase()
  if (!k) return items.value
  return items.value.filter((r) =>
    (r.name + r.vendor_label + r.region).toLowerCase().includes(k))
})

let syncPollTimer = null
let syncPollLeft = 0

const loadError = ref('')

async function load() {
  // 秘密 query 永不参与预填、轮询或刷新；viewer/operator 的管理动作参数也立即消费。
  scrubRoute(false)
  loading.value = true
  loadError.value = ''
  try {
    const r = await listCloudAccounts()
    items.value = r.items || []
    const requestedId = Number(route.query.account_id || 0)
    const target = items.value.find((x) => x.id === requestedId)
      || items.value.find((x) => x.id === selectedAccountId.value) || items.value[0]
    if (target && (target.id !== selectedAccountId.value || !selectedRun.value)) selectTask(target, false)
    if (route.query.new === '1' && isAdmin.value && !dialogVisible.value) openCreate()
    else if (route.query.edit === 'credential' && target && isAdmin.value && !dialogVisible.value) openEdit(target)
    // 探活、资产同步、规则初始化都是非终态，任一阶段存在就继续轮询
    if (items.value.some((x) => ONBOARDING_STATUSES.includes(x.status)) && syncPollLeft > 0) {
      syncPollLeft -= 1
      clearTimeout(syncPollTimer)
      syncPollTimer = setTimeout(load, 3000)
    } else if (!items.value.some((x) => ONBOARDING_STATUSES.includes(x.status))) {
      syncPollLeft = 0
    }
  } catch (e) {
    // 网络失败保留账号与 Run，恢复后继续；不得把读取失败显示成空账号。
    loadError.value = e?.response?.data?.detail || t('clouds.load_fail')
  } finally { loading.value = false }
}

// ---- 新增/编辑对话框（密钥只进不出：不回显已保存 AK/SK） ----
const dialogVisible = ref(false)
const saving = ref(false)
const editing = ref(null)          // null=新增；否则为行数据
const testingUnsaved = ref(false)
const unsavedTestResult = ref(null)
const wizStep = ref(0)             // 虚拟化向导步（0-3）
function clearCredentialSecrets() {
  form.ak = ''
  form.sk = ''
  unsavedTestResult.value = null
  ackText.value = ''
}
watch(dialogVisible, (visible) => {
  if (!visible) {
    clearCredentialSecrets()
    scrubRoute(true)
  }
})

const form = reactive({
  name: '', vendor: 'aliyun', ak: '', sk: '', region: '', enabled: true,
  host: '', datacenter: '', sslSkip: true, changeSource: '',
  // 数据源分级监控：新建默认高优先级、三去向全开、重申间隔跟随优先级
  priority: 'high', notifyOverview: true, notifyInapp: true, notifyPush: true, repeatSel: 'inherit',
})
const PRIORITY_TAGS = { high: 'danger', medium: 'warning', low: 'info' }
const priorityLabel = (p) => t(`settings.policy.priority.${p}`)
const repeatLabel = (row) => {
  if (row.repeat_days == null) return t('settings.policy.repeat.inherit')
  if (Number(row.repeat_days) <= 0) return t('settings.policy.repeat.never')
  return t('settings.policy.repeat.days', { n: row.repeat_days })
}
// 列表摘要：允许哪些提醒去向（模块开关仍会再过滤一次，这里只表达账号侧配置）
function destSummary(row) {
  const keys = []
  if (row.notify_overview) keys.push(t('settings.policy.dest.overview'))
  if (row.notify_inapp) keys.push(t('settings.policy.dest.inapp'))
  if (row.notify_push) keys.push(t('settings.policy.dest.push'))
  return keys.join(' · ') || t('clouds.policy.dest_none')
}

// 表单修改即作废测试结果：
// 可用性结果只对当前参数有效，改了任何字段都要重测
watch(form, () => {
  if (!testingUnsaved.value) unsavedTestResult.value = null
})
watch(() => form.vendor, () => {
  if (!editing.value) form.changeSource = ''
})

function openCreate() {
  if (!isAdmin.value) return
  const prefill = route.query.new === '1' ? {
    vendor: String(route.query.vendor || ''),
    name: String(route.query.name || '').slice(0, 64),
    region: String(route.query.region || '').slice(0, 64),
  } : null
  editing.value = null
  wizStep.value = 0
  Object.assign(form, {
    name: prefill?.name || '', vendor: VENDOR_MAP[prefill?.vendor] ? prefill.vendor : 'aliyun',
    ak: '', sk: '', region: prefill?.region || '', enabled: true,
    host: '', datacenter: '', sslSkip: true, changeSource: '',
    priority: 'high', notifyOverview: true, notifyInapp: true, notifyPush: true, repeatSel: 'inherit',
  })
  unsavedTestResult.value = null
  dialogVisible.value = true
  if (prefill) {
    // 结构化引用只携带非秘密预填项；打开即消费，避免轮询或刷新重复打开。
    const query = { ...route.query }
    ;['new', 'vendor', 'name', 'region', 'ak', 'sk', 'secret', 'token', 'password']
      .forEach((key) => delete query[key])
    router.replace({ query })
  }
}

function openEdit(row) {
  editing.value = row
  wizStep.value = 0
  Object.assign(form, {
    name: row.name, vendor: row.vendor, ak: '', sk: '', region: row.region,
    enabled: row.enabled,
    host: (row.extra && row.extra.host) || '',
    datacenter: (row.extra && row.extra.datacenter) || '',
    // 旧数据未存该键=vCenter 自签默认跳过校验（与后端 extra.ssl_skip 默认一致）
    sslSkip: !row.extra || row.extra.ssl_skip !== false,
    changeSource: (row.extra && row.extra.change_source) || '',
    priority: row.priority || 'high',
    notifyOverview: row.notify_overview !== false,
    notifyInapp: row.notify_inapp !== false,
    notifyPush: row.notify_push !== false,
    repeatSel: row.repeat_days == null ? 'inherit' : (Number(row.repeat_days) <= 0 ? 'never' : Number(row.repeat_days)),
  })
  unsavedTestResult.value = null
  dialogVisible.value = true
}

// 无云 AK/SK 语义由能力位决定（自建 / ESXi / vCenter / KVM）
function isNoCreds() {
  return !capOf(form.vendor).needs_ak_sk
}

// 虚拟化家族：ESXi / vCenter / KVM——走四步接入向导
function isVirt() {
  return ['esxi', 'vcenter', 'kvm'].includes(form.vendor)
}

function isVSphere() {
  return ['esxi', 'vcenter'].includes(form.vendor)
}

function isKvm() {
  return form.vendor === 'kvm'
}

// 主机地址字段语义随厂商（KVM 填 libvirt URI）
function hostLabel() {
  return isKvm() ? 'libvirt URI' : t('clouds.form.host')
}
function hostPlaceholder() {
  if (isKvm()) return t('clouds.form.host_ph_kvm')
  return form.vendor === 'esxi' ? t('clouds.form.host_ph_esxi') : t('clouds.form.host_ph_vcenter')
}

// 入库 extra：以原对象为底保留所有未知/其他模块键，只改本表单拥有的字段。
function buildExtra() {
  const extra = editing.value?.extra
    ? JSON.parse(JSON.stringify(editing.value.extra)) : {}
  if (isKvm()) {
    if (form.host.trim()) extra.host = form.host.trim()
    else delete extra.host
  } else if (isVSphere()) {
    if (form.host.trim()) extra.host = form.host.trim()
    else delete extra.host
    if (form.vendor === 'vcenter' && form.datacenter.trim()) extra.datacenter = form.datacenter.trim()
    else delete extra.datacenter
    extra.ssl_skip = !!form.sslSkip
  }
  if (!isVirt() && form.changeSource) extra.change_source = form.changeSource
  else if (!form.changeSource) delete extra.change_source
  return extra
}

async function onTestUnsaved() {
  if (!isNoCreds() && (!form.ak.trim() || !form.sk.trim())) {
    return ElMessage.warning(t('clouds.msg.need_aksk_test'))
  }
  if (isVirt() && !form.host.trim()) {
    return ElMessage.warning(isKvm()
      ? t('clouds.msg.need_uri_kvm')
      : t('clouds.msg.need_host_vsphere'))
  }
  testingUnsaved.value = true
  try {
    unsavedTestResult.value = await testCloudAccountUnsaved({
      vendor: form.vendor, ak: form.ak.trim(), sk: form.sk.trim(), region: form.region.trim(),
      extra: buildExtra(),
    })
  } catch { /* 拦截器提示 */ } finally { testingUnsaved.value = false }
}

// ---- 向导导航（虚拟化家族四步；非虚拟化单表单） ----
// 注意：导航不得清空测试结果，否则测通后翻到汇总会显示"未测试"。
// 结果作废只由 form 字段变化触发（见上方 watch），导航本身不改参数，结果应保持。
function wizNext() {
  if (wizStep.value === 0) {
    if (!form.name.trim()) return ElMessage.warning(t('clouds.msg.need_name'))
    if (!form.host.trim()) return ElMessage.warning(t('clouds.msg.need_host', { label: hostLabel() }))
  }
  wizStep.value = Math.min(3, wizStep.value + 1)
}

function wizPrev() {
  wizStep.value = Math.max(0, wizStep.value - 1)
}

// ---- 云凭证安全警示：保存前二次弹框+输入「我已知晓」，
// 确认入审计（cloud.account.ack）——追责保护：平台已警示、用户已知悉用途与只读建议 ----
const ackVisible = ref(false)
const ackText = ref('')

function onSave() {
  if (saving.value) return
  if (!form.name.trim()) return ElMessage.warning(t('clouds.msg.need_name'))
  if (isVirt() && !form.host.trim()) return ElMessage.warning(t('clouds.msg.need_host', { label: hostLabel() }))
  if (!isNoCreds() && (!form.ak.trim() || !form.sk.trim()) && !editing.value) {
    return ElMessage.warning(t('clouds.msg.need_aksk'))
  }
  // 新建必警示；编辑时改凭证（AK/SK 非空）重新警示——与后端强制口径一致
  const needAck = !editing.value || !!(form.ak.trim() || form.sk.trim())
  if (needAck) {
    ackText.value = ''
    ackVisible.value = true
    return
  }
  doSave('')
}

function finishSave(saved, wasEditing) {
  selectedAccountId.value = saved.id
  selectedRun.value = null
  if (saved.onboarding_run_id) {
    acceptedRun.value = { account_id: saved.id, id: saved.onboarding_run_id, name: saved.name }
  }
  taskRoute(saved.id, saved.onboarding_run_id || 0)
  ElMessage.success(t(wasEditing ? 'clouds.msg.updated' : 'clouds.msg.added_auto'))
  // 新增与修改都可能触发后台重测+同步（改凭证/地址/重新启用）；仅当行处于
  // probing/syncing/seeding 时轮询才会继续，纯改名改阈值不会空转
  syncPollLeft = 60
  dialogVisible.value = false
  ackVisible.value = false
  clearCredentialSecrets()
  load()
}

async function recoverCreatedAccount(body) {
  // POST 已提交但响应丢失时，唯一名称+厂商是恢复键；找不到才保留原错误，绝不直接重建。
  const result = await listCloudAccounts()
  return (result.items || []).find((x) => x.name === body.name && x.vendor === body.vendor) || null
}

async function doSave(ack) {
  if (saving.value) return
  saving.value = true
  // 失败态二次确认：连通测试失败仍要保存须人工确认，防误存坏配置
  if (unsavedTestResult.value && !unsavedTestResult.value.ok) {
    try {
      await ElMessageBox.confirm(
        t('clouds.confirm.save_failed_body'),
        t('clouds.confirm.save_failed_title'),
        { type: 'warning', confirmButtonText: t('clouds.btn.save_anyway'), cancelButtonText: t('clouds.btn.back_edit') },
      )
    } catch { saving.value = false; return }
  }
  const body = {
    name: form.name.trim(), vendor: form.vendor,
    ak: form.ak.trim(), sk: form.sk.trim(), region: form.region.trim(),
    extra: buildExtra(), enabled: form.enabled, ack,
    // 监控策略列：只改这些不触发重新同步（后端 update 仅在凭证/地址变化时 resync）
    priority: form.priority,
    notify_overview: form.notifyOverview,
    notify_inapp: form.notifyInapp,
    notify_push: form.notifyPush,
    repeat_days: form.repeatSel === 'inherit' ? null
      : form.repeatSel === 'never' ? 0 : Number(form.repeatSel),
  }
  const wasEditing = !!editing.value
  try {
    const saved = wasEditing
      ? await updateCloudAccount(editing.value.id, body)
      : await createCloudAccount(body)
    finishSave(saved, wasEditing)
  } catch {
    if (!wasEditing) {
      try {
        const recovered = await recoverCreatedAccount(body)
        if (recovered) finishSave(recovered, false)
      } catch { /* 列表也不可用：保留表单，网络恢复后再从同名账号恢复 */ }
    }
  } finally { saving.value = false }
}

// 「我已知晓」输入校验：中文四字是协议短语（后端 ACK_TEXT 比较契约，固定不随语言变）；
// 英文界面额外接受 "I understand" 兜底——提示语写哪个词用户都不会输不过。上报 ack 固定传「我已知晓」。
function ackPhraseOk(v) {
  const s = String(v || '').trim()
  return s === '我已知晓' || (locale.value === 'en' && s === 'I understand')
}

function onAckConfirm() {
  if (!ackPhraseOk(ackText.value)) {
    return ElMessage.warning(t('clouds.ack.msg_need'))
  }
  ackVisible.value = false
  doSave('我已知晓')
}

async function onTestRow(row) {
  testingId.value = row.id
  try {
    const r = await testCloudAccount(row.id)
    ElMessage[r.ok ? 'success' : 'error'](r.message || (r.ok ? t('clouds.msg.test_ok') : t('clouds.msg.test_fail')))
    load()
  } catch { /* 拦截器提示 */ } finally { testingId.value = 0 }
}

async function onDelete(row) {
  try {
    await ElMessageBox.confirm(
      t('clouds.confirm.delete_body', { name: row.name, vendor: row.vendor_label }),
      t('clouds.confirm.delete_title'), { type: 'warning', confirmButtonText: t('clouds.btn.delete'), cancelButtonText: t('clouds.btn.cancel') },
    )
  } catch { return }
  try {
    await deleteCloudAccount(row.id)
    ElMessage.success(t('clouds.msg.deleted'))
    load()
  } catch { /* 拦截器提示 */ }
}

watch(() => route.query.account_id, (value) => {
  const row = items.value.find((x) => x.id === Number(value))
  if (row && row.id !== selectedAccountId.value) selectTask(row, false)
})
onMounted(() => { loadVendors(); loadChangeSources(); load() })
onBeforeUnmount(() => {
  clearTimeout(syncPollTimer)
  clearTimeout(taskPollTimer)
  taskRequestSeq += 1
  clearCredentialSecrets()
})
</script>

<template>
  <div class="page">
    <el-alert type="info" :closable="false" show-icon class="cloud-tip"
      :title="t('clouds.top_note')" />
    <!-- SSH 凭证语义不同，此处注明引导到对应管理入口 -->
    <div class="sub ssh-note">{{ t('clouds.ssh_note') }}</div>

    <div class="toolbar">
      <el-input v-model="keyword" :placeholder="t('clouds.search_ph')" clearable style="width: 220px" />
      <div style="flex: 1" />
      <el-button v-if="isAdmin" type="primary" @click="openCreate">{{ t('clouds.btn.add') }}</el-button>
    </div>

    <section v-if="selectedAccountId || acceptedRun" class="current-task">
      <h3>{{ t('clouds.task.current') }}</h3>
      <el-alert
        v-if="acceptedRun" type="success" :closable="false" show-icon class="accepted-run"
        :title="t('clouds.task.accepted', { name: acceptedRun.name, id: acceptedRun.id })"
        :data-accepted-run-id="acceptedRun.id"
      />
      <TaskResultCard
        :run="selectedRun" :can-write="canWrite" :can-edit-credentials="isAdmin"
        :loading="taskLoading" :load-error="taskError"
        @update:run="updateSelectedRun" @refresh="loadSelectedTask"
        @edit-credential="editTaskCredential" @confirm-logs="confirmTaskLogs"
      />
    </section>

    <el-alert v-if="loadError" type="error" :closable="false" show-icon style="margin-bottom: 10px"
              :title="loadError">
      <el-button size="small" @click="load">{{ t('common.retry') }}</el-button>
    </el-alert>
    <el-table stripe :data="filtered" v-loading="loading" border
              :empty-text="loadError ? loadError : t('clouds.empty')">
      <el-table-column :label="t('clouds.col.vendor')" width="150">
        <template #default="{ row }">
          <el-tag :type="VENDOR_MAP[row.vendor]?.tag || 'info'" size="small" effect="plain">
            {{ row.vendor_label }}
          </el-tag>
          <el-tag v-if="row.capability?.tier" class="cap-tag" size="small" type="info" effect="plain">
            {{ tierLabel(row.capability.tier) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="name" :label="t('clouds.col.name')" min-width="150" show-overflow-tooltip>
        <template #default="{ row }">
          {{ row.name }}
          <el-tag v-if="row.bridge_group" size="small" type="info" effect="plain">{{ t('clouds.col.bridge') }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="ak_masked" label="AccessKey" width="150">
        <template #default="{ row }">
          <span class="mono">{{ row.ak_masked || t('clouds.col.no_creds') }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="region" :label="t('clouds.col.region')" width="130">
        <template #default="{ row }">{{ row.region || t('clouds.col.all_regions') }}</template>
      </el-table-column>
      <el-table-column :label="t('clouds.col.status')" width="120">
        <template #default="{ row }">
          <el-tag :type="STATUS_MAP[displayStatus(row)]?.type || 'info'" size="small">
            {{ STATUS_MAP[displayStatus(row)]?.labelKey ? t(STATUS_MAP[displayStatus(row)].labelKey) : displayStatus(row) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('clouds.col.enabled')" width="80">
        <template #default="{ row }">
          <el-tag :type="row.enabled ? 'success' : 'info'" size="small">
            {{ row.enabled ? t('clouds.enabled') : t('clouds.disabled') }}
          </el-tag>
        </template>
      </el-table-column>
      <!-- 数据源分级监控：优先级 + 允许的去向；详细策略在「系统设置→监控与提醒」 -->
      <el-table-column :label="t('clouds.col.priority')" width="100">
        <template #default="{ row }">
          <el-tag :type="PRIORITY_TAGS[row.priority] || 'info'" size="small">
            {{ priorityLabel(row.priority || 'high') }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('clouds.col.destinations')" min-width="180">
        <template #default="{ row }">
          <span class="sub">{{ destSummary(row) }}</span>
          <div class="sub">{{ repeatLabel(row) }}</div>
        </template>
      </el-table-column>
      <el-table-column :label="t('clouds.col.last_test')" width="170">
        <template #default="{ row }">
          <span class="sub">{{ row.last_test_at ? fmtTime(row.last_test_at) : t('clouds.never_tested') }}</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('clouds.col.last_sync')" width="170">
        <template #default="{ row }">
          <span class="sub">{{ row.last_sync_at ? fmtTime(row.last_sync_at) : t('clouds.never_synced') }}</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('clouds.col.actions')" width="280" fixed="right">
        <template #default="{ row }">
          <el-button size="small" link type="primary" :disabled="!row.onboarding_run_id"
                     @click="selectTask(row)">{{ t('clouds.task.view') }}</el-button>
          <el-button v-if="canWrite" size="small" link type="primary"
                     :disabled="!capOf(row.vendor).probe"
                     :title="capOf(row.vendor).probe ? '' : t('clouds.cap.test_disabled')"
                     :loading="testingId === row.id" @click="onTestRow(row)">{{ t('clouds.btn.test') }}</el-button>
          <el-button v-if="isAdmin" size="small" link type="primary" @click="openEdit(row)">{{ t('clouds.btn.edit') }}</el-button>
          <el-button v-if="isAdmin" size="small" link type="danger" @click="onDelete(row)">{{ t('clouds.btn.delete') }}</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 新增/编辑对话框：虚拟化家族（ESXi/vCenter/KVM）走四步向导，其余厂商单表单 -->
    <el-dialog v-model="dialogVisible" width="620px" :close-on-click-modal="false"
               :title="editing ? t('clouds.dlg.edit_title') : (isVirt() ? t('clouds.dlg.wizard_title', { vendor: vendorLabel(form.vendor) }) : t('clouds.dlg.add_title'))">
      <el-steps v-if="isVirt()" :active="wizStep" align-center finish-status="success" class="wz-steps">
        <el-step :title="t('clouds.wiz.step1')" /><el-step :title="t('clouds.wiz.step2')" />
        <el-step :title="t('clouds.wiz.step3')" /><el-step :title="t('clouds.wiz.step4')" />
      </el-steps>

      <!-- ===== 虚拟化向导 ===== -->
      <template v-if="isVirt()">
        <el-form v-if="wizStep === 0" :model="form" label-width="110px" class="wz-form">
          <el-form-item :label="t('clouds.form.vendor')" required>
            <el-select v-model="form.vendor" style="width: 100%" :disabled="!!editing">
              <el-option-group v-for="g in vendorGroups" :key="g.key" :label="g.label">
                <el-option v-for="v in g.items" :key="v.value" :label="t(v.labelKey)" :value="v.value" />
              </el-option-group>
            </el-select>
            <div v-if="editing" class="sub form-hint">{{ t('clouds.form.vendor_locked') }}</div>
          </el-form-item>
          <el-form-item :label="t('clouds.form.account_name')" required>
            <el-input v-model="form.name" :placeholder="t('clouds.form.name_ph_virt')" maxlength="64" />
          </el-form-item>
          <el-form-item :label="hostLabel()" required>
            <el-input v-model="form.host" :placeholder="hostPlaceholder()" maxlength="128" />
            <div v-if="isKvm()" class="sub form-hint">
              {{ t('clouds.form.kvm_hint') }}
            </div>
          </el-form-item>
          <el-form-item v-if="form.vendor === 'vcenter'" :label="t('clouds.form.datacenter')">
            <el-input v-model="form.datacenter" :placeholder="t('clouds.form.datacenter_ph')" maxlength="64" />
          </el-form-item>
          <el-form-item :label="t('clouds.form.account')">
            <el-input v-model="form.ak" type="password" show-password
                      :placeholder="editing ? t('clouds.form.keep_blank') : (isKvm() ? t('clouds.form.user_ph_kvm') : t('clouds.form.user_ph'))"
                      maxlength="256" />
          </el-form-item>
          <el-form-item :label="t('clouds.form.password')">
            <el-input v-model="form.sk" type="password" show-password
                      :placeholder="editing ? t('clouds.form.keep_blank') : t('clouds.form.password_ph')" maxlength="256" />
          </el-form-item>
          <el-form-item :label="t('clouds.form.enabled')">
            <el-switch v-model="form.enabled" />
          </el-form-item>
        </el-form>

        <div v-if="wizStep === 1" class="wz-form">
          <template v-if="isVSphere()">
            <div class="wz-q">{{ t('clouds.wiz.ssl_q') }}</div>
            <el-form :model="form" label-width="150px">
              <el-form-item :label="t('clouds.wiz.ssl_skip')">
                <el-switch v-model="form.sslSkip" />
                <span class="sub ssl-state">{{ form.sslSkip ? t('clouds.wiz.ssl_skip_on') : t('clouds.wiz.ssl_strict') }}</span>
              </el-form-item>
            </el-form>
            <el-alert type="warning" :closable="false" show-icon class="ssl-alert"
              :title="t('clouds.wiz.ssl_warn')" />
            <div class="sub ssl-explain">
              {{ t('clouds.wiz.ssl_explain1') }}<br>
              {{ t('clouds.wiz.ssl_explain2') }}<br>
              {{ t('clouds.wiz.ssl_explain3') }}
            </div>
          </template>
          <template v-else>
            <div class="wz-q">{{ t('clouds.wiz.kvm_q') }}</div>
            <div class="sub ssl-explain">
              {{ t('clouds.wiz.kvm_explain1') }}<br>
              {{ t('clouds.wiz.kvm_explain2') }}<br>
              {{ t('clouds.wiz.kvm_explain3') }}<br>
              {{ t('clouds.wiz.kvm_explain4') }}
            </div>
          </template>
        </div>

        <div v-if="wizStep === 2" class="wz-form">
          <div class="wz-q">{{ t('clouds.wiz.test_q', { v: isKvm() ? ' libvirt/hypervisor ' : ' ' }) }}</div>
          <div class="test-row">
            <el-button size="small" type="primary" plain :loading="testingUnsaved" @click="onTestUnsaved">{{ t('clouds.btn.test_avail') }}</el-button>
            <el-tag v-if="unsavedTestResult" class="test-tag" size="small"
                    :type="unsavedTestResult.ok ? 'success' : 'danger'">
              {{ unsavedTestResult.ok ? t('clouds.avail') : t('clouds.unavail') }}
            </el-tag>
          </div>
          <div v-if="unsavedTestResult" class="sub test-msg-big">{{ unsavedTestResult.message }}</div>
          <div v-else class="sub ssl-explain">
            {{ isKvm() ? t('clouds.wiz.test_hint_kvm') : t('clouds.wiz.test_hint_vsphere') }}
          </div>
        </div>

        <div v-if="wizStep === 3" class="wz-form">
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item :label="t('clouds.form.vendor')">{{ vendorLabel(form.vendor) }}</el-descriptions-item>
            <el-descriptions-item :label="t('clouds.form.account_name')">{{ form.name || t('clouds.sum.empty') }}</el-descriptions-item>
            <el-descriptions-item :label="hostLabel()">{{ form.host || t('clouds.sum.empty') }}</el-descriptions-item>
            <el-descriptions-item v-if="form.vendor === 'vcenter'" :label="t('clouds.form.datacenter')">{{ form.datacenter || t('clouds.sum.empty_optional') }}</el-descriptions-item>
            <el-descriptions-item v-if="isVSphere()" :label="t('clouds.wiz.ssl_label')">{{ form.sslSkip ? t('clouds.sum.ssl_skip') : t('clouds.wiz.ssl_strict') }}</el-descriptions-item>
            <el-descriptions-item :label="t('clouds.sum.creds')">{{ form.ak ? t('clouds.sum.creds_filled') : t('clouds.sum.creds_empty') }}</el-descriptions-item>
            <el-descriptions-item :label="t('clouds.wiz.step3')">
              <el-tag v-if="unsavedTestResult" size="small" :type="unsavedTestResult.ok ? 'success' : 'danger'">
                {{ unsavedTestResult.ok ? t('clouds.avail') : t('clouds.unavail') }}
              </el-tag>
              <span v-if="unsavedTestResult" class="sub">{{ unsavedTestResult.message }}</span>
              <span v-else class="sub">{{ t('clouds.sum.untested') }}</span>
            </el-descriptions-item>
          </el-descriptions>
          <div class="sub ssl-explain">
            {{ t('clouds.sum.footer', { v: isKvm() ? t('clouds.sum.adapter_libvirt') : t('clouds.sum.adapter_vsphere') }) }}
          </div>
        </div>
      </template>

      <!-- ===== 云厂商单表单 ===== -->
      <el-form v-else :model="form" label-width="110px">
        <el-form-item :label="t('clouds.form.vendor')" required>
          <el-select v-model="form.vendor" style="width: 100%" :disabled="!!editing">
            <el-option-group v-for="g in vendorGroups" :key="g.key" :label="g.label">
              <el-option v-for="v in g.items" :key="v.value" :label="t(v.labelKey)" :value="v.value" />
            </el-option-group>
          </el-select>
          <div v-if="capTip(form.vendor)" class="sub form-hint">{{ capTip(form.vendor) }}</div>
          <div v-if="editing" class="sub form-hint">{{ t('clouds.form.vendor_locked') }}</div>
        </el-form-item>
        <CloudCredentialForm
          id-prefix="cloud-account" :name="form.name" :ak="form.ak" :sk="form.sk" :region="form.region"
          :editing="!!editing" :vendor="form.vendor" @update:name="form.name = $event" @update:ak="form.ak = $event"
          @update:sk="form.sk = $event" @update:region="form.region = $event"
        />
        <el-form-item :label="t('clouds.form.change_source')">
          <el-select v-model="form.changeSource" style="width: 100%">
            <el-option value="" :label="automaticChangeSourceLabel()" />
            <el-option v-for="s in availableChangeSources" :key="s.key" :value="s.key" :label="s.label" />
          </el-select>
          <div class="sub form-hint">{{ t('clouds.form.change_source_hint') }}</div>
        </el-form-item>
        <el-form-item :label="t('clouds.form.enabled')">
          <el-switch v-model="form.enabled" />
        </el-form-item>
        <el-form-item v-if="capOf(form.vendor).probe" :label="t('clouds.form.conn_test')">
          <el-button size="small" :loading="testingUnsaved" @click="onTestUnsaved">{{ t('clouds.btn.test_avail') }}</el-button>
          <el-tag v-if="unsavedTestResult" class="test-tag" size="small"
                  :type="unsavedTestResult.ok ? 'success' : 'danger'">
            {{ unsavedTestResult.ok ? t('clouds.avail') : t('clouds.unavail') }}
          </el-tag>
          <span v-if="unsavedTestResult" class="sub test-msg">{{ unsavedTestResult.message }}</span>
        </el-form-item>
      </el-form>

      <!-- 监控策略：账号级优先级/去向/重申间隔；只改这些字段不会触发重新同步 -->
      <template v-if="!isVirt() || wizStep === 3">
        <el-divider content-position="left">{{ t('clouds.policy.title') }}</el-divider>
        <el-form :model="form" label-width="110px" class="policy-form">
          <el-form-item :label="t('clouds.policy.priority')">
            <el-radio-group v-model="form.priority" :disabled="!isAdmin">
              <el-radio-button value="high">{{ t('settings.policy.priority.high') }}</el-radio-button>
              <el-radio-button value="medium">{{ t('settings.policy.priority.medium') }}</el-radio-button>
              <el-radio-button value="low">{{ t('settings.policy.priority.low') }}</el-radio-button>
            </el-radio-group>
            <div class="sub form-hint">{{ t('clouds.policy.priority_hint') }}</div>
          </el-form-item>
          <el-form-item :label="t('clouds.policy.dest')">
            <el-checkbox v-model="form.notifyOverview">{{ t('settings.policy.dest.overview') }}</el-checkbox>
            <el-checkbox v-model="form.notifyInapp">{{ t('settings.policy.dest.inapp') }}</el-checkbox>
            <el-checkbox v-model="form.notifyPush">{{ t('settings.policy.dest.push') }}</el-checkbox>
            <div class="sub form-hint">{{ t('clouds.policy.dest_hint') }}</div>
          </el-form-item>
          <el-form-item :label="t('clouds.policy.repeat')">
            <el-select v-model="form.repeatSel" style="width: 220px" :disabled="!isAdmin">
              <el-option value="inherit" :label="t('settings.policy.repeat.inherit')" />
              <el-option :value="7" :label="t('settings.policy.repeat.days', { n: 7 })" />
              <el-option :value="30" :label="t('settings.policy.repeat.days', { n: 30 })" />
              <el-option :value="90" :label="t('settings.policy.repeat.days', { n: 90 })" />
              <el-option
                v-if="typeof form.repeatSel === 'number' && ![7, 30, 90].includes(form.repeatSel)"
                :value="form.repeatSel" :label="t('settings.policy.repeat.days', { n: form.repeatSel })"
              />
              <el-option value="never" :label="t('settings.policy.repeat.never')" />
            </el-select>
            <div class="sub form-hint">{{ t('clouds.policy.repeat_hint') }}</div>
          </el-form-item>
        </el-form>
      </template>

      <template #footer>
        <el-button @click="dialogVisible = false">{{ t('clouds.btn.cancel') }}</el-button>
        <template v-if="isVirt()">
          <el-button v-if="wizStep > 0" @click="wizPrev">{{ t('clouds.btn.prev') }}</el-button>
          <el-button v-if="wizStep < 3" type="primary" @click="wizNext">{{ t('clouds.btn.next') }}</el-button>
          <el-button v-else type="primary" @click="onSave">{{ editing ? t('clouds.btn.save') : t('clouds.btn.finish_add') }}</el-button>
        </template>
        <el-button v-else type="primary" :loading="saving" :disabled="saving" @click="onSave">{{ editing ? t('clouds.btn.save') : t('clouds.btn.add_short') }}</el-button>
      </template>
    </el-dialog>

    <!-- 云凭证安全警示（二次弹框）：用途说清、只读警示、我已知晓入审计 -->
    <el-dialog v-model="ackVisible" width="560px" :close-on-click-modal="false" :title="t('clouds.ack.title')">
      <el-alert type="warning" :closable="false" show-icon style="margin-bottom: 12px">
        <template #title>{{ t('clouds.ack.readonly_a') }}<b>{{ t('clouds.ack.readonly_b') }}</b>{{ t('clouds.ack.readonly_c') }}</template>
      </el-alert>
      <div class="ack-body">
        <p>{{ t('clouds.ack.p1_a') }}<b>{{ t('clouds.ack.p1_b') }}</b>{{ t('clouds.ack.p1_c') }}</p>
        <p>{{ t('clouds.ack.p2_a') }}<b>{{ t('clouds.ack.p2_b') }}</b>{{ t('clouds.ack.p2_c') }}</p>
        <p>{{ t('clouds.ack.p3') }}</p>
        <p>{{ t('clouds.ack.p4_a') }}<b>{{ t('clouds.ack.p4_b') }}</b>{{ t('clouds.ack.p4_c') }}</p>
      </div>
      <el-input v-model="ackText" :placeholder="t('clouds.ack.input_ph')" style="margin-top: 8px"
                @keyup.enter="onAckConfirm" />
      <template #footer>
        <el-button @click="ackVisible = false; ackText = ''">{{ t('clouds.btn.cancel') }}</el-button>
        <el-button type="primary" :loading="saving" :disabled="saving || !ackPhraseOk(ackText)" @click="onAckConfirm">
          {{ t('clouds.ack.confirm_btn') }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.current-task { margin: 0 0 16px; }
.current-task h3 { margin: 0 0 10px; font-size: 15px; }
.accepted-run { margin-bottom: 10px; }
.cap-tag { margin-left: 4px; }
.policy-form { margin-top: 8px; }
.ack-body { line-height: 1.9; font-size: 13px; }
.ack-body p { margin: 6px 0; }
.cloud-tip {
  margin-bottom: 12px;
}
.ssh-note {
  margin: -6px 0 12px;
}
.toolbar {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
  align-items: center;
}
.form-hint {
  margin-top: 2px;
}
.test-tag {
  margin-left: 8px;
}
.test-msg {
  margin-left: 8px;
}
.wz-q {
  font-weight: 600;
  margin-bottom: 10px;
}
.ssl-state {
  margin-left: 10px;
}
.ssl-alert {
  margin: 4px 0 12px;
}
.ssl-explain {
  line-height: 1.9;
}
.test-row {
  display: flex;
  align-items: center;
  gap: 4px;
}
.test-msg-big {
  margin-top: 10px;
  line-height: 1.8;
  word-break: break-all;
}
</style>
