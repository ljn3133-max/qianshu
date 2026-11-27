<script setup>
// 安装向导：首登部署向导（多步 el-steps）+ 探测/体检/补数据工具区。
// 向导：①欢迎（是否公有云）——否=完全跳过②③直接进自建与接入；是=②选厂商（多选=混合云）
//       ③逐厂商只读 AK+测试可用性（全部绿勾才放行）④自建资产（可选折叠，仅混合云需要）
//       ⑤完成摘要并保存到云账号表；每步可上一步回改。
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CircleCheck } from '@element-plus/icons-vue'
import { useRoute, useRouter } from 'vue-router'
import MarkdownIt from 'markdown-it'

import http from '@/api/http'
import {
  applySlsConfig, bootstrapAgentCommand, bootstrapAgentScript, bootstrapMonitorSources,
  bootstrapProbe, bootstrapReport, bootstrapSlsCatalog, bootstrapStatus,
} from '@/api/bootstrap'
import { createSession, runWorkflow, streamAgentMessage } from '@/api/agent'
import {
  createCloudAccount, getAccountOnboarding, getSlsDiscovery, listCloudAccounts, listOnboardingRuns,
  skipSlsTargets, startSlsDiscovery, testCloudAccountUnsaved,
} from '@/api/clouds'
import CloudCredentialForm from '@/components/CloudCredentialForm.vue'
import TaskResultCard from '@/components/TaskResultCard.vue'
import DataSourceDialog from '@/components/DataSourceDialog.vue'
import { fmtTime } from '@/utils/format'
import { useAuthStore } from '@/stores/auth'
import { locale, t } from '@/i18n'

const md = new MarkdownIt({ breaks: true })

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()
const canOperate = computed(() => ['admin', 'operator'].includes(auth.role))
const canEditCredentials = computed(() => auth.role === 'admin')

/* ---------------- 整体状态 ---------------- */
// backendNotReady：接口 404 / 网络失败时展示"后端尚未就绪"空态，绝不白屏
const backendNotReady = ref(false)
const statusLoading = ref(true)
const status = ref(null) // {initialized, has_admin, has_credential, init_done, accounts_total}
// 初始化已完成：向导降级为"已初始化"只读摘要，工具区保留
const initDone = computed(() => !!status.value?.init_done)

/* ---------------- 首登部署向导 ---------------- */
const step = ref(0)
const usePublicCloud = ref(true)          // ①：否=仅自建/已有监控，完全跳过选厂商与填 AK
const PUBLIC_VENDORS = [
  { value: 'aliyun', labelKey: 'bootstrap.vendor.aliyun' },
  { value: 'tencent', labelKey: 'bootstrap.vendor.tencent' },
  { value: 'volc', labelKey: 'bootstrap.vendor.volc' },
  { value: 'baidu', labelKey: 'bootstrap.vendor.baidu' },
  { value: 'huawei', labelKey: 'bootstrap.vendor.huawei' },
  { value: 'aws', labelKey: 'bootstrap.vendor.aws' },
  { value: 'azure', labelKey: 'bootstrap.vendor.azure' },
]
const VENDOR_LABELS = Object.fromEntries(PUBLIC_VENDORS.map((v) => [v.value, t(v.labelKey)]))
const selectedVendors = ref(['aliyun'])   // ②：多选=混合云多厂商多账号并存
const vendorForms = reactive({})          // ③：{name, ak, sk, region, testing, testResult}
const ONPREM_KINDS = [
  { value: 'esxi', labelKey: 'bootstrap.onprem.esxi' },
  { value: 'vcenter', labelKey: 'bootstrap.onprem.vcenter' },
  { value: 'physical', labelKey: 'bootstrap.onprem.physical' },
  { value: 'k8s', labelKey: 'bootstrap.onprem.k8s_only' },
]
const onpremKinds = ref([])               // ④：占位选项，细化清单在机器库/K8s 管理
// ESXi 裸机 / vCenter 统一管理：两种形态都在自建资产步骤出现，各自使用对应字段
const ONPREM_FORM_DEFS = [
  { kind: 'esxi', labelKey: 'bootstrap.onprem.esxi', hostLabelKey: 'bootstrap.form.host', dc: false },
  { kind: 'vcenter', labelKey: 'bootstrap.onprem.vcenter', hostLabelKey: 'bootstrap.form.host_addr', dc: true },
]
const onpremForms = reactive({
  esxi: { name: t('bootstrap.onprem.esxi'), host: '', user: '', password: '', sslSkip: true },
  vcenter: { name: 'vCenter', host: '', user: '', password: '', datacenter: '', sslSkip: true },
})
const saving = ref(false)
const saveResult = ref(null)              // ⑤：{ok: [], failed: []}——只表示保存结果，不表示接入就绪

// 向导与智能体、后台同步共享同一 Onboarding Run；网络失败不清空已恢复的任务身份。
const onboardingRuns = ref([])
const acceptedRuns = ref([])
const onboardingLoading = ref(false)
const onboardingError = ref('')
let onboardingTimer = null
let onboardingRequestSeq = 0

function updateOnboardingRun(run) {
  if (!run?.id || !run?.account_id) return
  const index = onboardingRuns.value.findIndex((x) => x.id === run.id && x.account_id === run.account_id)
  const current = index >= 0 ? onboardingRuns.value[index] : null
  if (current && Number(run.attempts || 1) < Number(current.attempts || 1)) return
  if (current?.updated_at && run.updated_at
    && Date.parse(run.updated_at) < Date.parse(current.updated_at)) return
  if (index >= 0) onboardingRuns.value[index] = run
  else onboardingRuns.value.unshift(run)
  acceptedRuns.value = acceptedRuns.value.filter((x) => !(x.id === run.id && x.account_id === run.account_id))
}

async function refreshOnboardingRun(ref) {
  if (!ref?.account_id) return loadOnboardingRuns()
  try {
    const run = await getAccountOnboarding(ref.account_id)
    if (run.account_id === ref.account_id && run.id === ref.run_id) updateOnboardingRun(run)
  } catch {
    onboardingError.value = t('clouds.task.load_failed')
  }
}

async function loadOnboardingRuns() {
  if (onboardingTimer) {
    clearTimeout(onboardingTimer)
    onboardingTimer = null
  }
  const requestSeq = ++onboardingRequestSeq
  onboardingLoading.value = true
  onboardingError.value = ''
  try {
    const result = await listOnboardingRuns()
    if (requestSeq !== onboardingRequestSeq) return
    const current = new Map(onboardingRuns.value.map((x) => [`${x.account_id}:${x.id}`, x]))
    onboardingRuns.value = (result.items || []).map((run) => {
      const old = current.get(`${run.account_id}:${run.id}`)
      return old && Number(run.attempts || 1) < Number(old.attempts || 1) ? old : run
    })
    const identities = new Set(onboardingRuns.value.map((x) => `${x.account_id}:${x.id}`))
    acceptedRuns.value = acceptedRuns.value.filter((x) => !identities.has(`${x.account_id}:${x.id}`))
  } catch {
    if (requestSeq === onboardingRequestSeq) onboardingError.value = t('clouds.task.load_failed')
  } finally {
    if (requestSeq === onboardingRequestSeq) {
      onboardingLoading.value = false
      // 断网恢复与运行中的任务都继续读取同一 Run，不追加新任务或新卡。
      if (onboardingError.value || onboardingRuns.value.some((r) => r.status === 'running' || r.status === 'waiting')) {
        onboardingTimer = setTimeout(loadOnboardingRuns, 2500)
      }
    }
  }
}

function onTaskEditCredential(run) {
  router.push({ path: '/clouds', query: { account_id: run.account_id, run_id: run.id, edit: 'credential' } })
}
function onTaskConfirmLogs(run) {
  if (slsAccountId.value !== run.account_id) slsAccountId.value = run.account_id
  else loadSlsDiscovery(false)
  setTimeout(() => document.getElementById('sls-workspace')?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 0)
}

// 新手主线：授权接入 → 自动准备 → 查看结果；其他环境保留为可跳过的独立步骤。
const wizSteps = computed(() => (usePublicCloud.value
  ? ['bootstrap.step.welcome', 'bootstrap.step.creds', 'bootstrap.step.onprem', 'bootstrap.step.done']
  : ['bootstrap.step.welcome', 'bootstrap.step.onprem_connect', 'bootstrap.step.done']))
const isCreds = computed(() => usePublicCloud.value && step.value === 1)
const isOnprem = computed(() => (usePublicCloud.value ? step.value === 2 : step.value === 1))
const isDone = computed(() => step.value === wizSteps.value.length - 1)

function ensureVendorForm(v) {
  if (!vendorForms[v]) {
    vendorForms[v] = {
      name: t('bootstrap.default.account_name', { vendor: VENDOR_LABELS[v] || v }), ak: '', sk: '', region: '',
      testing: false, testResult: null,
    }
  }
  return vendorForms[v]
}
watch(selectedVendors, (list, previous) => {
  // 普通 v-if 卸载不会触发子组件 onDeactivated；父级必须在真正退出厂商时清秘密。
  for (const vendor of previous || []) {
    if (!list.includes(vendor) && vendorForms[vendor]) {
      vendorForms[vendor].ak = ''
      vendorForms[vendor].sk = ''
      vendorForms[vendor].testResult = null
    }
  }
  list.forEach(ensureVendorForm)
}, { immediate: true })
watch(usePublicCloud, (enabled) => {
  if (enabled) return
  for (const form of Object.values(vendorForms)) {
    form.ak = ''
    form.sk = ''
    form.testResult = null
  }
})

// 行数据用 computed 建壳：模板不直接摸可能未初始化的 vendorForms[v]
const vendorFormRows = computed(() => selectedVendors.value.map((v) => ({ v, f: ensureVendorForm(v) })))

// 只校验必要授权是否填写；连通、发现、指标、账单和变更由提交后的同一 Run 自动验证。
const akReady = computed(() => selectedVendors.value.length > 0 && selectedVendors.value.every((v) => {
  const f = ensureVendorForm(v)
  return !!(f.ak.trim() && f.sk.trim())
}))
const akPending = computed(() => selectedVendors.value.filter((v) => {
  const f = ensureVendorForm(v)
  return !(f.ak.trim() && f.sk.trim())
}).map((v) => VENDOR_LABELS[v] || v))

function goNext() {
  if (isCreds.value && !akReady.value) {
    return ElMessage.warning(selectedVendors.value.length
      ? t('bootstrap.msg.fill_aksk')
      : t('bootstrap.msg.need_vendor'))
  }
  if (step.value < wizSteps.value.length - 1) step.value += 1
}

function goPrev() {
  if (step.value > 0) step.value -= 1
}

async function testVendor(v) {
  const f = ensureVendorForm(v)
  if (!f.ak.trim() || !f.sk.trim()) return ElMessage.warning(t('bootstrap.msg.fill_aksk'))
  f.testing = true
  try {
    f.testResult = await testCloudAccountUnsaved({
      vendor: v, ak: f.ak.trim(), sk: f.sk.trim(), region: f.region.trim(),
    })
  } catch {
    f.testResult = { ok: false, message: t('bootstrap.msg.test_req_failed') }
  } finally {
    f.testing = false
  }
}

// 凭证改了旧测试结果作废（绿勾只对当前 AK/SK 有效）
function onCredInput(v) {
  ensureVendorForm(v).testResult = null
}

const summaryRows = computed(() => {
  const rows = []
  if (usePublicCloud.value) {
    for (const v of selectedVendors.value) {
      const f = ensureVendorForm(v)
      rows.push({
        kind: VENDOR_LABELS[v] || v,
        name: f.name || t('bootstrap.default.account_name', { vendor: VENDOR_LABELS[v] }),
        region: f.region || t('bootstrap.summary.all_regions'),
        detail: t('bootstrap.summary.auto_verify'),
      })
    }
  }
  for (const d of ONPREM_FORM_DEFS) {
    if (!onpremKinds.value.includes(d.kind)) continue
    const f = onpremForms[d.kind]
    const hostLabel = t(d.hostLabelKey)
    rows.push({
      kind: t(d.labelKey),
      name: f.name || t(d.labelKey),
      region: f.datacenter || f.host || '-',
      detail: f.host
        ? t('bootstrap.summary.host_filled', { label: hostLabel, user: f.user ? t('bootstrap.summary.with_user') : t('bootstrap.summary.no_user') })
        : t('bootstrap.summary.host_missing', { label: hostLabel }),
    })
  }
  const rest = ONPREM_KINDS.filter((o) => onpremKinds.value.includes(o.value)
    && !ONPREM_FORM_DEFS.some((d) => d.kind === o.value))
  if (rest.length) {
    rows.push({
      kind: t('bootstrap.summary.onprem'), name: t('bootstrap.onprem.assets'), region: '-',
      detail: rest.map((o) => t(o.labelKey)).join(' / '),
    })
  }
  return rows
})

// 「我已知晓」输入校验与 CloudAccounts.vue 同一确认强度：中文四字是协议短语
// （后端 ACK_TEXT 比较契约，固定不随语言变）；英文界面额外接受 "I understand" 兜底。
// 上报 ack 仍固定传「我已知晓」。
function ackPhraseOk(v) {
  const s = String(v || '').trim()
  return s === '我已知晓' || (locale.value === 'en' && s === 'I understand')
}

async function onSaveAll() {
  if (saving.value) return
  const jobs = []
  const skipped = []
  if (usePublicCloud.value) {
    for (const v of selectedVendors.value) {
      const f = ensureVendorForm(v)
      if (!f.ak.trim() || !f.sk.trim()) {
        // 已勾选却缺凭证：显式报错阻断保存，不能静默丢账号。
        skipped.push(t('bootstrap.summary.missing_credential', { label: VENDOR_LABELS[v] }))
        continue
      }
      jobs.push({
        name: (f.name || t('bootstrap.default.account_name', { vendor: VENDOR_LABELS[v] })).trim(), vendor: v,
        ak: f.ak.trim(), sk: f.sk.trim(), region: f.region.trim(), enabled: true,
      })
    }
  }
  // ESXi 裸机 / vCenter：各自字段入库（extra 存主机/数据中心，账号/密码进加密字段），
  // vSphere 采集适配器排期——登记先行、采集上线后自动生效
  for (const d of ONPREM_FORM_DEFS) {
    if (!onpremKinds.value.includes(d.kind)) continue
    const f = onpremForms[d.kind]
    if (!f.host.trim()) {
      skipped.push(t('bootstrap.summary.skipped', { label: t(d.labelKey), host: t(d.hostLabelKey) }))
      continue
    }
    jobs.push({
      name: (f.name || t(d.labelKey)).trim(), vendor: d.kind,
      ak: f.user.trim(), sk: f.password.trim(), region: '', enabled: true,
      extra: d.dc
        ? { host: f.host.trim(), datacenter: f.datacenter.trim(), ssl_skip: f.sslSkip !== false }
        : { host: f.host.trim(), ssl_skip: f.sslSkip !== false },
    })
  }
  // 裸机 / 纯 K8s 不在云账号里建"自建"占位行：
  // 这是资产/机器管理的事，云账号只承担有凭证的采集源。这里只提示去对应页面登记。
  const manualKinds = onpremKinds.value.filter((k) => k === 'physical' || k === 'k8s')
  if (skipped.length) {
    // 勾选了却没填必填项：明确列出并未保存，避免用户以为已接入。
    return ElMessage.error(t('bootstrap.msg.missing_required', { list: skipped.join('；') }))
  }
  if (!jobs.length) {
    if (manualKinds.length) return ElMessage.info(t('bootstrap.msg.onprem_manual'))
    return ElMessage.warning(t('bootstrap.msg.nothing_to_save'))
  }
  // 云凭证安全警示：后端 _require_ack 服务端强制，向导保存必须
  // 先手输「我已知晓」，审计按账号逐条落 cloud.account.ack，确认强度与云账号管理页一致。
  saving.value = true
  try {
    await ElMessageBox.prompt(t('bootstrap.ack.body'), t('bootstrap.ack.title'), {
      type: 'warning',
      confirmButtonText: t('bootstrap.ack.ok'),
      cancelButtonText: t('bootstrap.ack.cancel'),
      inputPlaceholder: t('bootstrap.ack.input_ph'),
      inputValidator: (v) => (ackPhraseOk(v) ? true : t('bootstrap.ack.wrong')),
    })
  } catch { saving.value = false; return }
  const ok = [...(saveResult.value?.ok || [])]
  const failed = [...skipped]
  function acceptAccount(account) {
    if (!ok.includes(account.name)) ok.push(account.name)
    if (account.onboarding_run_id) {
      acceptedRuns.value = [...acceptedRuns.value.filter((x) => x.account_id !== account.id), {
        id: account.onboarding_run_id, account_id: account.id, name: account.name,
      }]
    }
    // 明文仅在提交返回前保留；任务卡与后续接口均不携带 AK/SK。
    const vendorForm = vendorForms[account.vendor]
    if (vendorForm) { vendorForm.ak = ''; vendorForm.sk = ''; vendorForm.testResult = null }
    const onpremForm = onpremForms[account.vendor]
    if (onpremForm) { onpremForm.user = ''; onpremForm.password = '' }
  }
  for (const body of jobs) {
    if (ok.includes(body.name)) continue  // 部分失败后只补交失败账号，不重复创建已受理任务
    try {
      const account = await createCloudAccount({ ...body, ack: '我已知晓' })   // 协议值固定（后端 ACK_TEXT 比较契约）
      acceptAccount(account)
    } catch (e) {
      try {
        const accounts = await listCloudAccounts()
        const recovered = (accounts.items || []).find((x) => x.name === body.name && x.vendor === body.vendor)
        if (recovered) {
          acceptAccount(recovered)
          continue
        }
      } catch { /* 列表也不可用时保留下方原错误，网络恢复后可再恢复 */ }
      failed.push(t('bootstrap.summary.failed_item', { name: body.name, err: e.response?.data?.detail || t('bootstrap.msg.save_failed') }))
    }
  }
  saving.value = false
  saveResult.value = { ok, failed }
  if (ok.length) {
    ElMessage.success(failed.length
      ? t('bootstrap.msg.saved_with_failed', { n: ok.length, f: failed.length })
      : t('bootstrap.msg.saved', { n: ok.length }))
  }
  if (manualKinds.length) ElMessage.info(t('bootstrap.msg.onprem_manual'))
  loadStatus()
  loadOnboardingRuns()
  loadSlsWorkspace()
  auth.fetchInit(true)   // 初始化状态刷新：菜单隐藏/守卫停跳
}

// 暂不接入任何账号也能完成初始化（否则无账号可存的用户会被守卫永远强跳回向导）
const skipping = ref(false)
async function skipInit() {
  skipping.value = true
  try {
    await http.post('/bootstrap/complete')
    ElMessage.success(t('bootstrap.msg.init_skipped'))
    loadStatus()
    auth.fetchInit(true)
  } catch { /* 拦截器提示 */ } finally {
    skipping.value = false
  }
}

/* ---------------- AI 初始化（onboard_init 工作流：数据/规则/画像/拓扑/链路一键生成） ----------------
   复用工作流执行（POST /agent/workflows/onboard_init/run）：规则只建待确认工单不静默写，
   运行记录自动落 agent_workflow_runs + audit；幂等可重跑。 */
const aiInit = reactive({ running: false, result: null, error: '' })
const aiInitSteps = computed(() => aiInit.result?.steps || [])
const aiInitMd = computed(() => (aiInit.result?.artifact?.report_md
  ? md.render(aiInit.result.artifact.report_md) : ''))
const aiStepType = (s) => (s === 'ok' ? 'success' : (s === 'skipped' ? 'info' : 'danger'))
const aiStepLabel = (s) => (s === 'ok' ? t('workflow.run.status_ok')
  : (s === 'skipped' ? t('workflow.run.status_skipped') : t('workflow.run.status_failed')))

async function runOnboardInit() {
  if (aiInit.running) return
  aiInit.running = true
  aiInit.error = ''
  try {
    aiInit.result = await runWorkflow('onboard_init', {})
    if (aiInit.result?.status === 'done') ElMessage.success(t('bootstrap.ai.init_done'))
    else ElMessage.warning(t('bootstrap.ai.init_partial'))
  } catch (err) {
    if (!toastBackendMessage(err)) aiInit.error = t('bootstrap.ai.init_failed')
  } finally {
    aiInit.running = false
    loadOnboardingRuns()
    loadStatus()
  }
}

/* ---------------- AI 协助填写（copilot 会话轻量调用） ----------------
   红线：AI 只出文本建议、绝不自动填表单；点「采用」= 用户确认后才写入单个字段。 */
const aiSuggest = reactive({})
function suggestBox(scope) {
  if (!aiSuggest[scope]) aiSuggest[scope] = { loading: false, text: '', items: [], error: '' }
  return aiSuggest[scope]
}

// 解析建议：每行「字段=值 | 理由」（字段只认 vendors/name/region）
function parseSuggestItems(text) {
  const items = []
  for (const line of String(text || '').split(/\r?\n/)) {
    const m = line.match(/^\s*(vendors?|name|region)\s*[=:：]\s*([^|｜]+?)(?:\s*[|｜]\s*(.+))?$/i)
    if (m) items.push({ key: m[1].toLowerCase().replace(/s$/, ''), value: m[2].trim(), reason: (m[3] || '').trim() })
  }
  return items
}

function suggestPrompt(scope) {
  const intro = t('bootstrap.ai.suggest_intro')
  const fmt = t('bootstrap.ai.suggest_format')
  if (scope === 'vendors') {
    const sel = selectedVendors.value.map((v) => VENDOR_LABELS[v] || v).join('、') || t('bootstrap.ai.none')
    return `${intro}\n${t('bootstrap.ai.ctx_vendors', { vendors: sel })}\n${fmt}`
  }
  const v = scope.slice('cred:'.length)
  const f = ensureVendorForm(v)
  const detail = `${VENDOR_LABELS[v] || v}: name=${f.name || '-'}, region=${f.region || '-'}`
  return `${intro}\n${t('bootstrap.ai.ctx_creds', { detail })}\n${fmt}`
}

async function askAiSuggest(scope) {
  const s = suggestBox(scope)
  if (s.loading) return
  s.loading = true
  s.error = ''
  s.text = ''
  s.items = []
  try {
    const sess = await createSession({ title: t('bootstrap.ai.session_title') })
    let acc = ''
    await streamAgentMessage(sess.id, {
      q: suggestPrompt(scope),
      ctx: {},
      onEvent: ({ event, data }) => {
        if (event === 'token') {
          acc += typeof data === 'string' ? data : (data?.token ?? data?.text ?? data?.content ?? '')
        } else if (event === 'error') {
          s.error = data?.code === 'NEED_MODEL'
            ? t('bootstrap.ai.no_model') : (data?.message || t('bootstrap.ai.suggest_failed'))
        }
      },
    })
    s.text = acc.trim()
    s.items = parseSuggestItems(acc)
    if (!s.text && !s.error) s.error = t('bootstrap.ai.suggest_failed')
  } catch (err) {
    s.error = err?.code === 'NEED_MODEL'
      ? t('bootstrap.ai.no_model') : (err?.message || t('bootstrap.ai.suggest_failed'))
  } finally {
    s.loading = false
  }
}

// 采用建议 = 用户点按钮确认后写入（AI 从不自动填）；值先过安全字符集校验
function adoptSuggest(scope, item) {
  const val = String(item.value || '').trim()
  if (scope === 'vendors') {
    const vals = val.split(/[,，\s]+/).filter((x) => PUBLIC_VENDORS.some((p) => p.value === x))
    if (!vals.length) return ElMessage.warning(t('bootstrap.ai.adopt_unsafe'))
    selectedVendors.value = [...new Set([...selectedVendors.value, ...vals])]
    return ElMessage.success(t('bootstrap.ai.adopted'))
  }
  const v = scope.slice('cred:'.length)
  const f = ensureVendorForm(v)
  if (item.key === 'region') {
    if (!/^[a-z0-9-]{1,64}$/i.test(val)) return ElMessage.warning(t('bootstrap.ai.adopt_unsafe'))
    f.region = val
  } else {
    if (!val || val.length > 64 || /[<>"'`\\\n]/.test(val)) return ElMessage.warning(t('bootstrap.ai.adopt_unsafe'))
    f.name = val
  }
  return ElMessage.success(t('bootstrap.ai.adopted'))
}

/* ---------------- 探测（阿里云五面，工具区） ---------------- */
const cloudForm = reactive({ ak: '', sk: '' }) // 密钥只进不出：不回显已保存凭证
const probing = ref(false)

const FACE_ORDER = ['assets', 'metrics', 'logs', 'cost', 'changes']
const FACE_LABELS = {
  assets: 'bootstrap.face.assets', metrics: 'bootstrap.face.metrics',
  logs: 'bootstrap.face.logs', cost: 'bootstrap.face.cost', changes: 'bootstrap.face.changes',
}
const STATUS_TAG = { green: 'success', yellow: 'warning', red: 'danger' }
const STATUS_TEXT = {
  green: 'bootstrap.probe.green', yellow: 'bootstrap.probe.yellow', red: 'bootstrap.probe.red',
}

const faces = ref([])
const probedAt = ref('')

const faceRows = computed(() =>
  FACE_ORDER
    .map((key) => {
      const f = faces.value.find((x) => x.key === key)
      return f ? { ...f, label: f.label || (FACE_LABELS[key] ? t(FACE_LABELS[key]) : key) } : null
    })
    .filter(Boolean),
)

/* ---------------- 底座体检报告 ---------------- */
const reportLoading = ref(false)
const capabilities = ref([]) // [{name, ready, missing: []}]
const gaps = ref([])        // [{face, impact, fix, unlock}]
const reportReady = computed(() => capabilities.value.length || gaps.value.length)
// 读取失败不能落成「0 账号 / 无缺口」：向导的完成判断
// 会把失败态当成「已搞定」，用户因此漏接入。
const statusError = ref('')
const reportError = ref('')

/* ---------------- 错误处理 ---------------- */
// http.js 拦截器已对 detail 字符串弹过 ElMessage，这里只兜"message"形态，避免重复弹窗。
// 返回是否弹过提示，调用方据此决定要不要兜底提示。
function toastBackendMessage(err) {
  const d = err?.response?.data
  const msg = typeof d?.detail === 'string' ? null : (typeof d?.message === 'string' ? d.message : null)
  if (msg) {
    ElMessage.error(msg)
    return true
  }
  return typeof d?.detail === 'string'
}

// 后端没起 / 路由不存在：vite 代理在后端不可达时回 500，FastAPI 未实现回 404
function isNotReady(err) {
  const s = err?.response?.status
  return !err?.response || s === 404 || s === 500 || s === 502 || s === 503
}

/* ---------------- 加载 ---------------- */
async function loadStatus() {
  statusLoading.value = true
  statusError.value = ''
  try {
    status.value = await bootstrapStatus()
    if (status.value?.init_done) refreshDsSummary()   // 已初始化摘要里展示数据源概况
  } catch (err) {
    status.value = null
    if (isNotReady(err)) backendNotReady.value = true
    else statusError.value = err?.response?.data?.detail || t('bootstrap.done.status_fail')
    toastBackendMessage(err)
  } finally {
    statusLoading.value = false
  }
}

async function loadReport() {
  reportLoading.value = true
  reportError.value = ''
  try {
    const r = await bootstrapReport()
    faces.value = r.faces || []
    capabilities.value = r.capabilities || []
    gaps.value = r.gaps || []
  } catch (err) {
    // 清空旧数据，避免展示上一次的「无缺口」误导
    capabilities.value = []
    gaps.value = []
    if (isNotReady(err)) backendNotReady.value = true
    else reportError.value = err?.response?.data?.detail || t('bootstrap.report.load_fail')
    toastBackendMessage(err)
  } finally {
    reportLoading.value = false
  }
}

async function onProbe() {
  const hasAk = !!cloudForm.ak.trim()
  const hasSk = !!cloudForm.sk.trim()
  if (hasAk !== hasSk) return ElMessage.warning(t('bootstrap.msg.aksk_pair'))
  probing.value = true
  try {
    const body = hasAk ? { ak: cloudForm.ak.trim(), sk: cloudForm.sk.trim() } : {}
    const r = await bootstrapProbe(body)
    faces.value = r.faces || []
    probedAt.value = r.probed_at || ''
    ElMessage.success(t('bootstrap.msg.probe_done'))
    loadReport() // 探测后能力与缺口可能变化，顺手刷新报告
  } catch (err) {
    if (isNotReady(err)) backendNotReady.value = true
    else if (!toastBackendMessage(err)) {
      ElMessage.error(t('bootstrap.msg.probe_failed'))
    }
  } finally {
    probing.value = false
  }
}

// —— 非云机器/已有监控 ——
const agentDialog = ref(false)
const agentInfo = ref(null)
const monitorDialog = ref(false)
const monitorInfo = ref(null)

async function loadAgentCommand() {
  try {
    agentInfo.value = await bootstrapAgentCommand()
    agentDialog.value = true
  } catch { /* 拦截器提示 */ }
}

// 下载 qianshu-agent.py（后端 GET /bootstrap/agent-script 返回脚本文本，前端 Blob 落盘）
function downloadAgentScript() {
  bootstrapAgentScript().then((text) => {
    const blob = new Blob([text], { type: 'text/x-python;charset=utf-8' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = 'qianshu-agent.py'
    a.click()
    URL.revokeObjectURL(a.href)
    ElMessage.success(t('bootstrap.msg.agent_downloaded'))
  }).catch(() => { /* 拦截器提示 */ })
}

// —— 已有监控：数据源可配置 ——
// 后端 GET/PUT /settings/datasource-config 读写 .env 的 METRIC_SOURCES / LOG_SOURCES。
// builtin-vm 不进用户列表，单独标注"系统内置指标库（千枢自带）"；类型覆盖常见开源监控。
// 测试连通必须取到真实信息（版本/系统信息），结果列直接显示"连通 · 版本 x.y.z"；
// Zabbix 可配 API Token/账号（查询适配器）。编辑复用集成中心 DataSourceDialog，
// headers/verify_ssl 等字段集一致，本页只留清单与连通状态，不维护第二套内联表单。
const dsConfig = ref({ metric_sources: [], log_sources: [] })
const dsBuiltin = ref(null)     // {name, type, url, label}——系统内置指标库
const dsSummary = ref({ metric: 0, log: 0 })   // 已初始化摘要页的数据源概况
const dsSaving = ref(false)

// 已初始化摘要用：只拉配置计数（不弹窗）
async function refreshDsSummary() {
  try {
    const cfg = await http.get('/settings/datasource-config')
    dsBuiltin.value = cfg.builtin_metric_source || null
    dsSummary.value = {
      metric: (cfg.metric_sources || []).length,
      log: (cfg.log_sources || []).length,
    }
  } catch { /* 拦截器提示 */ }
}

// 数据源清单行（配置 + 探测状态按 名称+类型 挂接，与集成中心同一挂法）
const dsRows = computed(() => {
  const probeRows = [...(monitorInfo.value?.metrics || []), ...(monitorInfo.value?.logs || [])]
  const out = []
  for (const kind of ['metric_sources', 'log_sources']) {
    (dsConfig.value[kind] || []).forEach((s, i) => {
      const probe = probeRows.find((r) => r.name === s.name && r.type === s.type)
        || probeRows.find((r) => r.name === s.name) || null
      out.push({ ...s, _kind: kind, _idx: i, probe })
    })
  }
  return out
})

// ---- 新增/编辑数据源：复用集成中心 DataSourceDialog，字段集与教程一致 ----
const dsDialog = ref(false)
const dsPreset = ref('')
const dsEdit = ref(null)

function openDsAdd(kind) {
  dsPreset.value = kind === 'log' ? 'loki' : 'prometheus'
  dsEdit.value = null
  dsDialog.value = true
}

function openDsEdit(row) {
  dsPreset.value = ''
  dsEdit.value = { kind: row._kind, index: row._idx, source: dsConfig.value[row._kind][row._idx] }
  dsDialog.value = true
}

async function onDsSave({ kind, source, editIndex, editKind }) {
  if (editIndex >= 0 && editKind) {
    if (editKind === kind) dsConfig.value[editKind][editIndex] = source
    else {
      dsConfig.value[editKind].splice(editIndex, 1)
      dsConfig.value[kind].push(source)
    }
  } else {
    dsConfig.value[kind].push(source)
  }
  await saveDsConfig()
  reprobeMonitor()   // 保存生效后再复检，连通状态列跟着刷新
}

async function removeDsRow(row) {
  try {
    await ElMessageBox.confirm(
      t('bootstrap.msg.ds_remove_confirm', { name: row.name }),
      t('bootstrap.ds.remove_title'),
      { type: 'warning', confirmButtonText: t('bootstrap.btn.del'), cancelButtonText: t('bootstrap.ack.cancel') },
    )
  } catch { return }
  dsConfig.value[row._kind].splice(row._idx, 1)
  saveDsConfig()
}

async function loadMonitorSources() {
  try {
    const [cfg, probe] = await Promise.all([
      http.get('/settings/datasource-config'),
      bootstrapMonitorSources().catch(() => null),   // 探测失败不挡配置表单
    ])
    dsConfig.value = {
      metric_sources: Array.isArray(cfg.metric_sources) ? cfg.metric_sources : [],
      log_sources: Array.isArray(cfg.log_sources) ? cfg.log_sources : [],
    }
    dsBuiltin.value = cfg.builtin_metric_source || null
    monitorInfo.value = probe
    monitorDialog.value = true
  } catch { /* 拦截器提示 */ }
}

// 重新探测：保存配置后一键刷新连通状态
const reprobeLoading = ref(false)
async function reprobeMonitor() {
  reprobeLoading.value = true
  try {
    monitorInfo.value = await bootstrapMonitorSources()
    ElMessage.success(t('bootstrap.msg.reprobed'))
  } catch { /* 拦截器提示 */ } finally {
    reprobeLoading.value = false
  }
}

async function saveDsConfig() {
  dsSaving.value = true
  try {
    const r = await http.put('/settings/datasource-config', dsConfig.value)
    ElMessage.success(r.persisted ? t('bootstrap.msg.ds_saved_env') : t('bootstrap.msg.ds_saved_nopersist'))
  } finally {
    dsSaving.value = false
  }
}

// —— SLS：完整目录与语义推荐为主，高级手选为兼容出口 ——
const slsCatalog = ref([])
const slsAccounts = ref([])
const slsAccountId = ref(0)
const slsDiscovery = ref(null)
const slsLoading = ref(false)
const applyingSls = ref(false)
const slsRoleFilter = ref('recommended')
const slsPage = ref(1)
const slsPageSize = 12
const slsPrimary = reactive({ application: '', cdn: '' })
const slsSelectedSlb = reactive({})
let slsTimer = null
let slsRequestSeq = 0
const slsForm = reactive({
  outerProject: '', outerStore: '', innerProject: '', innerStore: '',
  appProject: '', appStore: '', cdnProject: '', cdnStore: '',
})
const slsManualRows = [
  { kind: 'outer', labelKey: 'bootstrap.sls.outer', noteKey: 'bootstrap.sls.outer_note' },
  { kind: 'inner', labelKey: 'bootstrap.sls.inner', noteKey: 'bootstrap.sls.inner_note' },
  { kind: 'app', labelKey: 'bootstrap.sls.app', noteKey: 'bootstrap.sls.app_note' },
  { kind: 'cdn', labelKey: 'bootstrap.sls.cdn', noteKey: 'bootstrap.sls.cdn_note' },
]
const slsCoverage = computed(() => slsDiscovery.value?.coverage || {})
const slsLogstores = computed(() => slsDiscovery.value?.logstores || [])
const slsTargetItem = computed(() => {
  const run = onboardingRuns.value.find((x) => x.account_id === slsAccountId.value)
  return run?.items?.find((x) => x.key === 'sls_targets') || null
})
const slsRowKey = (row) => `${row.project}/${row.logstore}`
const slsRowByKey = computed(() => Object.fromEntries(slsLogstores.value.map((x) => [slsRowKey(x), x])))
const slsRoleLabel = (role) => t(`bootstrap.sls.role_${role || 'unknown'}`)
const slsFilteredRows = computed(() => {
  const filter = slsRoleFilter.value
  if (filter === 'all') return slsLogstores.value
  if (filter === 'recommended') return slsLogstores.value.filter((x) => x.adoptable)
  if (filter === 'failed') return slsLogstores.value.filter((x) => x.query_status === 'failed')
  if (filter === 'no_recent_data') return slsLogstores.value.filter((x) => x.data_status === 'no_recent_data')
  return slsLogstores.value.filter((x) => x.role === filter)
})
const slsPageRows = computed(() => slsFilteredRows.value.slice(
  (slsPage.value - 1) * slsPageSize, slsPage.value * slsPageSize))
const slsSelectedCount = computed(() => Object.keys(slsSelectedSlb).length
  + Number(!!slsPrimary.application) + Number(!!slsPrimary.cdn))
watch(slsRoleFilter, () => { slsPage.value = 1 })
watch(slsAccountId, () => {
  slsPage.value = 1
  slsPrimary.application = ''
  slsPrimary.cdn = ''
  Object.keys(slsSelectedSlb).forEach((key) => delete slsSelectedSlb[key])
  slsDiscovery.value = null
  slsCatalog.value = []
  Object.keys(slsForm).forEach((key) => { slsForm[key] = '' })
  loadSlsDiscovery()
})

function slsDataLabel(row) {
  if (row.error_code === 'index_missing') return t('bootstrap.sls.data_index_missing')
  if (row.query_status === 'failed') return t('bootstrap.sls.data_query_failed')
  if (row.data_status === 'no_recent_data') return t('bootstrap.sls.data_no_recent')
  return t('bootstrap.sls.data_available')
}

function autoSelectRecommendations() {
  const rows = slsLogstores.value
  const plan = slsDiscovery.value?.confirmation
  if (plan?.groups) {
    // 预选只消费服务端集中确认计划，前端不再按名称、置信度或字段另猜一套。
    for (const role of ['application', 'cdn']) {
      if (slsPrimary[role]) continue
      const target = plan.groups.find((x) => x.role === role)?.preselected?.[0]
      if (target) {
        const row = rows.find((x) => x.project === target.project && x.logstore === target.logstore)
        if (row) slsPrimary[role] = slsRowKey(row)
      }
    }
    return
  }
  // 兼容旧目录响应；新响应必须带 confirmation，不会进入此分支。
  const unique = (roles) => {
    const candidates = rows.filter((x) => roles.includes(x.role) && x.adoptable && x.confidence >= 0.8)
    return candidates.length === 1 ? candidates[0] : null
  }
  if (!slsPrimary.application) {
    const row = unique(['application_json', 'application_text'])
    if (row) slsPrimary.application = slsRowKey(row)
  }
  if (!slsPrimary.cdn) {
    const row = unique(['cdn'])
    if (row) slsPrimary.cdn = slsRowKey(row)
  }
}

async function loadSlsWorkspace() {
  try {
    const result = await listCloudAccounts()
    slsAccounts.value = (result.items || []).filter((x) => x.vendor === 'aliyun' && x.enabled)
    if (!slsAccounts.value.some((x) => x.id === slsAccountId.value)) {
      slsAccountId.value = slsAccounts.value[0]?.id || 0
    } else if (slsAccountId.value) {
      loadSlsDiscovery(false)
    }
  } catch { /* 列表错误由统一拦截器提示 */ }
}

async function loadSlsDiscovery(showError = true) {
  if (slsTimer) {
    clearTimeout(slsTimer)
    slsTimer = null
  }
  const accountId = slsAccountId.value
  const requestSeq = ++slsRequestSeq
  if (!accountId) {
    slsDiscovery.value = null
    return
  }
  slsLoading.value = true
  try {
    const result = await getSlsDiscovery(accountId)
    // 慢响应不得跨账号覆盖当前目录，也不能把旧账号推荐带进新账号选择。
    if (requestSeq !== slsRequestSeq || accountId !== slsAccountId.value) return
    slsDiscovery.value = result
    autoSelectRecommendations()
    if (slsDiscovery.value.status === 'running') {
      slsTimer = setTimeout(() => loadSlsDiscovery(false), 3000)
    }
  } catch (err) {
    if (requestSeq === slsRequestSeq && showError && !toastBackendMessage(err)) {
      ElMessage.error(t('bootstrap.msg.sls_load_failed'))
    }
  } finally {
    if (requestSeq === slsRequestSeq) slsLoading.value = false
  }
}

async function onStartSlsDiscovery() {
  const accountId = slsAccountId.value
  if (!accountId) return
  slsLoading.value = true
  try {
    const result = await startSlsDiscovery(accountId)
    if (accountId !== slsAccountId.value) return
    slsDiscovery.value = result
    loadOnboardingRuns()
    loadSlsDiscovery(false)
  } catch (err) {
    if (!toastBackendMessage(err)) ElMessage.error(t('bootstrap.msg.sls_load_failed'))
  } finally {
    slsLoading.value = false
  }
}

function toggleSlb(row, checked) {
  const key = slsRowKey(row)
  if (checked) slsSelectedSlb[key] = slsSelectedSlb[key] || ''
  else delete slsSelectedSlb[key]
}

async function onApplySlsRecommendations() {
  const slb_targets = Object.entries(slsSelectedSlb).map(([key, chain]) => {
    const row = slsRowByKey.value[key]
    return row ? { project: row.project, logstore: row.logstore, chain } : null
  }).filter(Boolean)
  if (slb_targets.some((x) => !x.chain)) {
    return ElMessage.warning(t('bootstrap.sls.need_chain'))
  }
  const app = slsRowByKey.value[slsPrimary.application]
  const cdn = slsRowByKey.value[slsPrimary.cdn]
  const app_log = app ? [app.project, app.logstore] : []
  const cdn_target = cdn ? [cdn.project, cdn.logstore] : []
  if (!slb_targets.length && !app_log.length && !cdn_target.length) {
    return ElMessage.warning(t('bootstrap.msg.need_log_target'))
  }
  applyingSls.value = true
  try {
    const result = await applySlsConfig({
      account_id: slsAccountId.value, slb_targets, app_log, cdn_target,
    })
    ElMessage.success(result.message || t('bootstrap.msg.sls_applied'))
    await Promise.all([loadOnboardingRuns(), loadSlsDiscovery(false), loadReport()])
  } catch (err) {
    if (!toastBackendMessage(err)) ElMessage.error(t('bootstrap.msg.apply_failed'))
  } finally {
    applyingSls.value = false
  }
}

async function onSkipSls() {
  try {
    await ElMessageBox.confirm(t('bootstrap.sls.skip_confirm'), t('bootstrap.sls.skip_title'), {
      type: 'warning', confirmButtonText: t('bootstrap.sls.skip'), cancelButtonText: t('bootstrap.ack.cancel'),
    })
  } catch { return }
  try {
    await skipSlsTargets(slsAccountId.value)
    await Promise.all([loadOnboardingRuns(), loadSlsDiscovery(false)])
  } catch { /* 请求失败由统一拦截器提示，不改变本地选择 */ }
}

function storeOptions(project) {
  const p = slsCatalog.value.find((x) => x.project === project)
  return p ? p.logstores : []
}

function onProjectChange(kind) {
  const opts = storeOptions(slsForm[kind + 'Project'])
  slsForm[kind + 'Store'] = opts.includes(slsForm[kind + 'Store']) ? slsForm[kind + 'Store'] : (opts[0] || '')
}

async function loadCatalog() {
  slsLoading.value = true
  try {
    const r = await bootstrapSlsCatalog(slsAccountId.value)
    slsCatalog.value = r.projects || []
    if (!slsCatalog.value.length) ElMessage.info(t('bootstrap.msg.sls_empty'))
  } catch (err) {
    if (isNotReady(err)) backendNotReady.value = true
    else if (!toastBackendMessage(err)) ElMessage.error(t('bootstrap.msg.sls_load_failed'))
  } finally {
    slsLoading.value = false
  }
}

async function onApplySls() {
  const slb_targets = []
  if (slsForm.outerProject && slsForm.outerStore) {
    slb_targets.push({ project: slsForm.outerProject, logstore: slsForm.outerStore, chain: 'outer' })
  }
  if (slsForm.innerProject && slsForm.innerStore) {
    slb_targets.push({ project: slsForm.innerProject, logstore: slsForm.innerStore, chain: 'inner' })
  }
  const app_log = slsForm.appProject && slsForm.appStore ? [slsForm.appProject, slsForm.appStore] : []
  const cdn_target = slsForm.cdnProject && slsForm.cdnStore ? [slsForm.cdnProject, slsForm.cdnStore] : []
  if (!slb_targets.length && !app_log.length && !cdn_target.length) {
    return ElMessage.warning(t('bootstrap.msg.need_log_target'))
  }
  applyingSls.value = true
  try {
    const r = await applySlsConfig({ account_id: slsAccountId.value, slb_targets, app_log, cdn_target })
    ElMessage.success(r.message || t('bootstrap.msg.sls_applied'))
    await Promise.all([loadOnboardingRuns(), loadSlsDiscovery(false), loadReport()])
  } catch (err) {
    if (!toastBackendMessage(err)) ElMessage.error(t('bootstrap.msg.apply_failed'))
  } finally {
    applyingSls.value = false
  }
}

onMounted(() => {
  slsAccountId.value = Number(route.query.account_id || 0)
  loadStatus()
  loadReport()
  loadOnboardingRuns()
  loadSlsWorkspace()
  if (route.hash === '#sls-workspace') {
    setTimeout(() => document.getElementById('sls-workspace')?.scrollIntoView({ block: 'start' }), 300)
  }
})

onBeforeUnmount(() => {
  if (onboardingTimer) clearTimeout(onboardingTimer)
  if (slsTimer) clearTimeout(slsTimer)
  Object.values(vendorForms).forEach((f) => { f.ak = ''; f.sk = ''; f.testResult = null })
  Object.values(onpremForms).forEach((f) => { f.user = ''; f.password = '' })
  cloudForm.ak = ''; cloudForm.sk = ''
})
</script>

<template>
  <div class="page">
    <!-- 后端未就绪空态 -->
    <el-empty v-if="backendNotReady" :description="t('bootstrap.empty.not_ready')">
      <template #image>
        <el-icon :size="64" color="var(--el-text-color-secondary)"><i class="el-icon-warning" /></el-icon>
      </template>
      <p class="tip">{{ t('bootstrap.empty.not_ready_tip') }}</p>
    </el-empty>

    <template v-else>
      <!-- 初始化状态提示 -->
      <el-alert
        v-if="status && !status.initialized"
        type="info"
        :closable="false"
        show-icon
        :title="t('bootstrap.alert.not_init_title')"
        class="init-alert"
      >
        <template #default>
          {{ t('bootstrap.alert.not_init_body') }}
          <span v-if="status.has_admin && !status.has_credential">{{ t('bootstrap.alert.missing_creds') }}</span>
          <span v-else-if="!status.has_admin && status.has_credential">{{ t('bootstrap.alert.missing_admin') }}</span>
        </template>
      </el-alert>

      <!-- 已初始化：向导降级只读摘要；完成后菜单隐藏，直连 /bootstrap 只看本摘要 -->
      <el-card v-if="initDone" shadow="never" class="section-card">
        <template #header>
          <div class="card-head">
            <b>{{ t('bootstrap.done.title') }}</b>
            <span class="tip">{{ t('bootstrap.done.tip') }}</span>
          </div>
        </template>
        <el-descriptions :column="2" border size="default">
          <el-descriptions-item :label="t('bootstrap.done.init_status')">
            <el-tag type="success" size="small">{{ t('bootstrap.done.inited') }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item :label="t('bootstrap.done.cloud_accounts')">
            <el-alert v-if="statusError" type="error" :closable="false" show-icon :title="statusError">
              <el-button size="small" @click="loadStatus">{{ t('common.retry') }}</el-button>
            </el-alert>
            <span v-else>{{ t('bootstrap.done.accounts_enabled', { n: status?.accounts_total ?? '—', m: status?.accounts_enabled ?? '—' }) }}</span>
          </el-descriptions-item>
          <el-descriptions-item :label="t('bootstrap.onboarding.summary_title')">
            <el-tag :type="status?.onboarding_ready ? 'success' : 'warning'" size="small">
              {{ status?.onboarding_ready ? t('bootstrap.onboarding.status_ready') : t('bootstrap.onboarding.not_ready') }}
            </el-tag>
            <span class="onboarding-summary">
              {{ t('bootstrap.onboarding.summary', { ready: status?.accounts_ready ?? 0, running: status?.accounts_running ?? 0, gaps: status?.accounts_with_gaps ?? 0 }) }}
            </span>
          </el-descriptions-item>
          <el-descriptions-item :label="t('bootstrap.done.metric_sources')">
            {{ t('bootstrap.done.metric_summary', { n: dsSummary.metric }) }}
          </el-descriptions-item>
          <el-descriptions-item :label="t('bootstrap.done.log_sources')">{{ t('bootstrap.done.log_summary', { n: dsSummary.log }) }}</el-descriptions-item>
        </el-descriptions>
        <p class="tip" style="margin-top: 8px">
          {{ t('bootstrap.done.next_tip') }}
        </p>
      </el-card>

      <!-- 首登部署向导（多步，可回改）——仅未初始化时出现 -->
      <el-card v-else shadow="never" class="section-card">
        <template #header>
          <div class="card-head">
            <b>{{ t('bootstrap.wizard.title') }}</b>
            <span class="tip">{{ t('bootstrap.wizard.tip') }}</span>
          </div>
        </template>

        <el-steps :active="step" finish-status="success" class="wiz-steps">
          <el-step v-for="s in wizSteps" :key="s" :title="t(s)" />
        </el-steps>

        <!-- ① 欢迎：是否使用公有云 -->
        <div v-if="step === 0" class="wiz-body">
          <h3 class="wiz-title">{{ t('bootstrap.welcome.title') }}</h3>
          <p class="tip">
            {{ t('bootstrap.welcome.tip') }}
          </p>
          <div class="wiz-q">{{ t('bootstrap.welcome.question') }}</div>
          <el-radio-group v-model="usePublicCloud">
            <el-radio :value="true">{{ t('bootstrap.welcome.yes') }}</el-radio>
            <el-radio :value="false">{{ t('bootstrap.welcome.no') }}</el-radio>
          </el-radio-group>
          <p v-if="!usePublicCloud" class="tip">{{ t('bootstrap.welcome.no_tip') }}</p>
        </div>

        <!-- ② 必要授权：厂商与凭证一次提交，云端验证由后台自动执行 -->
        <div v-else-if="isCreds" class="wiz-body">
          <div class="wiz-q">{{ t('bootstrap.vendors.question') }}</div>
          <el-checkbox-group v-model="selectedVendors">
            <el-checkbox v-for="v in PUBLIC_VENDORS" :key="v.value" :value="v.value" class="wiz-check">
              {{ t(v.labelKey) }}
            </el-checkbox>
          </el-checkbox-group>
          <p class="tip">{{ t('bootstrap.vendors.tip') }}</p>
          <el-collapse class="advanced-inline">
            <el-collapse-item :title="t('bootstrap.advanced.name_region_ai')">
              <div class="wiz-inline">
                <el-button size="small" :loading="suggestBox('vendors').loading" @click="askAiSuggest('vendors')">
                  {{ t('bootstrap.ai.suggest_btn') }}
                </el-button>
                <span class="tip">{{ t('bootstrap.ai.suggest_tip') }}</span>
              </div>
              <div v-if="suggestBox('vendors').error || suggestBox('vendors').text" class="ai-suggest">
                <div class="ai-suggest-head">{{ t('bootstrap.ai.suggest_result') }}</div>
                <div v-if="suggestBox('vendors').error" class="tip ai-error">{{ suggestBox('vendors').error }}</div>
                <template v-for="(it, i) in suggestBox('vendors').items" :key="i">
                  <div class="ai-suggest-item">
                    <b class="ai-suggest-val">{{ it.key }} = {{ it.value }}</b>
                    <span v-if="it.reason" class="tip">{{ it.reason }}</span>
                    <el-button size="small" type="primary" plain @click="adoptSuggest('vendors', it)">
                      {{ t('bootstrap.ai.adopt') }}
                    </el-button>
                  </div>
                </template>
              </div>
            </el-collapse-item>
          </el-collapse>
          <div v-for="{ v, f } in vendorFormRows" :key="v" class="wiz-vendor-card">
            <div class="wiz-vendor-title">
              {{ VENDOR_LABELS[v] || v }}
              <el-icon v-if="f.testResult?.ok" class="wiz-ok"><CircleCheck /></el-icon>
            </div>
            <el-form :model="f" label-width="130px" class="wiz-form">
              <CloudCredentialForm
                :id-prefix="`bootstrap-${v}`" :name="f.name" :ak="f.ak" :sk="f.sk" :region="f.region" :vendor="v"
                @update:name="f.name = $event" @update:ak="f.ak = $event" @update:sk="f.sk = $event"
                @update:region="f.region = $event" @change="onCredInput(v)"
              />
              <el-collapse class="advanced-inline">
                <el-collapse-item :title="t('bootstrap.advanced.preflight')">
                  <el-form-item :label="t('bootstrap.form.conn_test')">
                    <el-button size="small" :loading="f.testing" @click="testVendor(v)">{{ t('bootstrap.btn.test_avail') }}</el-button>
                    <el-tag v-if="f.testResult" class="wiz-test-tag" size="small"
                            :type="f.testResult.ok ? 'success' : 'danger'">
                      {{ f.testResult.ok ? t('bootstrap.avail') : t('bootstrap.unavail') }}
                    </el-tag>
                    <span v-if="f.testResult" class="tip">{{ f.testResult.message }}</span>
                    <span v-else class="tip">{{ t('bootstrap.form.test_optional') }}</span>
                  </el-form-item>
                  <el-button size="small" class="ai-btn" :loading="suggestBox('cred:' + v).loading"
                             @click="askAiSuggest('cred:' + v)">
                    {{ t('bootstrap.ai.suggest_btn') }}
                  </el-button>
                </el-collapse-item>
              </el-collapse>
            </el-form>
            <div v-if="suggestBox('cred:' + v).error || suggestBox('cred:' + v).text" class="ai-suggest">
              <div class="ai-suggest-head">{{ t('bootstrap.ai.suggest_result') }}</div>
              <div v-if="suggestBox('cred:' + v).error" class="tip ai-error">{{ suggestBox('cred:' + v).error }}</div>
              <template v-for="(it, i) in suggestBox('cred:' + v).items" :key="i">
                <div class="ai-suggest-item">
                  <b class="ai-suggest-val">{{ it.key }} = {{ it.value }}</b>
                  <span v-if="it.reason" class="tip">{{ it.reason }}</span>
                  <el-button size="small" type="primary" plain @click="adoptSuggest('cred:' + v, it)">
                    {{ t('bootstrap.ai.adopt') }}
                  </el-button>
                </div>
              </template>
              <div v-if="!suggestBox('cred:' + v).items.length && !suggestBox('cred:' + v).error" class="tip">
                {{ suggestBox('cred:' + v).text }}
              </div>
            </div>
          </div>
          <p class="tip">{{ t('bootstrap.form.aksk_note') }}</p>
          <el-alert
            v-if="!akReady"
            type="warning"
            :closable="false"
            show-icon
            class="wiz-ak-hint"
            :title="t('bootstrap.msg.credentials_required', { list: akPending.join('、') })"
          />
        </div>

        <!-- ④ 自建资产 + 非云机器接入工具（可选：纯公有云可直接下一步） -->
        <div v-else-if="isOnprem" class="wiz-body">
          <div class="wiz-q">{{ t('bootstrap.onprem.question', { mode: usePublicCloud ? t('bootstrap.onprem.hybrid_only') : t('bootstrap.onprem.as_needed') }) }}</div>
          <el-checkbox-group v-model="onpremKinds">
            <el-checkbox v-for="o in ONPREM_KINDS" :key="o.value" :value="o.value" class="wiz-check">
              {{ t(o.labelKey) }}
            </el-checkbox>
          </el-checkbox-group>

          <!-- ESXi 裸机 / vCenter 统一管理：两种形态都在本步出现，各自使用对应字段 -->
          <template v-for="d in ONPREM_FORM_DEFS" :key="d.kind">
            <div v-if="onpremKinds.includes(d.kind)" class="wiz-vendor-card">
              <div class="wiz-vendor-title">{{ t(d.labelKey) }}</div>
              <el-form :model="onpremForms[d.kind]" label-width="110px" class="wiz-form">
                <el-form-item :label="t('bootstrap.form.name')">
                  <el-input v-model="onpremForms[d.kind].name" maxlength="64" />
                </el-form-item>
                <el-form-item :label="t(d.hostLabelKey)">
                  <el-input v-model="onpremForms[d.kind].host"
                            :placeholder="d.kind === 'esxi' ? t('bootstrap.form.host_ph_esxi') : t('bootstrap.form.host_ph_vc')"
                            maxlength="128" />
                </el-form-item>
                <el-form-item :label="t('bootstrap.form.account')">
                  <el-input v-model="onpremForms[d.kind].user" :placeholder="t('bootstrap.form.user_ph')" maxlength="64" />
                </el-form-item>
                <el-form-item :label="t('bootstrap.form.password')">
                  <el-input v-model="onpremForms[d.kind].password" type="password" show-password
                            :placeholder="t('bootstrap.form.password_ph')" maxlength="128" />
                </el-form-item>
                <el-form-item v-if="d.dc" :label="t('bootstrap.form.datacenter')">
                  <el-input v-model="onpremForms[d.kind].datacenter" :placeholder="t('bootstrap.form.datacenter_ph')" maxlength="64" />
                </el-form-item>
                <el-form-item :label="t('bootstrap.form.ssl_skip')">
                  <el-switch v-model="onpremForms[d.kind].sslSkip" />
                  <span class="tip" style="margin-left: 8px">{{ onpremForms[d.kind].sslSkip ? t('bootstrap.form.ssl_skip_on') : t('bootstrap.form.ssl_strict') }}</span>
                </el-form-item>
              </el-form>
              <p class="tip">{{ t('bootstrap.onprem.def_tip') }}</p>
            </div>
          </template>
          <p class="tip">{{ t('bootstrap.onprem.others_tip') }}</p>

          <div class="wiz-q">{{ t('bootstrap.tools.title') }}</div>
          <div class="wiz-inline">
            <el-button @click="loadAgentCommand">{{ t('bootstrap.btn.gen_agent') }}</el-button>
            <el-button @click="loadMonitorSources">{{ t('bootstrap.btn.monitor_ds') }}</el-button>
          </div>
          <p class="tip">{{ t('bootstrap.tools.tip') }}</p>
        </div>

        <!-- ⑤ 完成摘要 -->
        <div v-else class="wiz-body">
          <el-table :data="summaryRows" border size="small" class="wiz-summary">
            <el-table-column prop="kind" :label="t('bootstrap.col.kind')" width="120" />
            <el-table-column prop="name" :label="t('bootstrap.col.account_name')" min-width="140" />
            <el-table-column prop="region" :label="t('bootstrap.col.region')" width="140" />
            <el-table-column prop="detail" :label="t('bootstrap.col.detail')" min-width="200" />
          </el-table>
          <div v-if="saveResult" class="wiz-save-result">
            <div v-if="saveResult.ok.length" class="tip">{{ t('bootstrap.summary.saved_list') }}{{ saveResult.ok.join('、') }}</div>
            <div v-if="saveResult.failed.length" class="tip">{{ t('bootstrap.summary.failed_list') }}{{ saveResult.failed.join('；') }}</div>
          </div>
          <p class="tip">{{ t('bootstrap.summary.save_tip') }}</p>
        </div>

        <div class="wiz-actions">
          <el-button v-if="step > 0" @click="goPrev">{{ t('bootstrap.btn.prev') }}</el-button>
          <el-button v-if="!isDone" type="primary" :disabled="isCreds && !akReady" @click="goNext">{{ t('bootstrap.btn.next') }}</el-button>
          <template v-else>
            <el-button text :loading="skipping" @click="skipInit">{{ t('bootstrap.btn.skip_init') }}</el-button>
            <el-button type="primary" :loading="saving" @click="onSaveAll">{{ t('bootstrap.btn.save_finish') }}</el-button>
          </template>
          <div v-if="isCreds" class="tip wiz-next-tip">
            {{ t('bootstrap.msg.auto_after_submit') }}
          </div>
        </div>
      </el-card>

      <!-- 同一 Onboarding Run：先显示服务端受理身份，再用真实能力证据更新原卡。 -->
      <section v-if="acceptedRuns.length || onboardingRuns.length || onboardingError" class="section-card onboarding-card">
        <div class="card-head">
          <b>{{ t('bootstrap.onboarding.title') }}</b>
          <el-button size="small" :loading="onboardingLoading" @click="loadOnboardingRuns">
            {{ t('bootstrap.onboarding.refresh') }}
          </el-button>
        </div>
        <el-alert
          v-for="accepted in acceptedRuns" :key="`accepted-${accepted.account_id}-${accepted.id}`"
          type="success" :closable="false" show-icon class="accepted-run"
          :title="t('clouds.task.accepted', { name: accepted.name, id: accepted.id })"
          :data-accepted-run-id="accepted.id"
        />
        <el-alert v-if="onboardingError" type="error" :closable="false" show-icon :title="onboardingError" />
        <TaskResultCard
          v-for="run in onboardingRuns" :key="`${run.account_id}:${run.id}`" :run="run" :can-write="canOperate"
          :can-edit-credentials="canEditCredentials"
          :load-error="onboardingError" @update:run="updateOnboardingRun" @refresh="refreshOnboardingRun"
          @edit-credential="onTaskEditCredential" @confirm-logs="onTaskConfirmLogs"
        />
      </section>

      <!-- AI 初始化保留为高级入口，不作为首次接入前置条件。 -->
      <el-collapse v-if="!initDone" class="advanced-tools">
        <el-collapse-item :title="t('bootstrap.advanced.ai_init')">
      <el-card shadow="never" class="section-card" v-loading="aiInit.running">
        <template #header>
          <div class="card-head">
            <b>{{ t('bootstrap.ai.title') }}</b>
            <span class="tip">{{ t('bootstrap.ai.subtitle') }}</span>
          </div>
        </template>
        <div class="wiz-inline">
          <el-button type="primary" :loading="aiInit.running" @click="runOnboardInit">
            {{ t('bootstrap.ai.init_btn') }}
          </el-button>
          <span class="tip">{{ t('bootstrap.ai.init_tip') }}</span>
        </div>
        <div v-if="aiInit.error" class="tip ai-error">{{ aiInit.error }}</div>
        <template v-if="aiInit.result">
          <div class="ai-steps">
            <div v-for="(st, i) in aiInitSteps" :key="i" class="ai-step-row">
              <el-tag size="small" effect="plain" :type="aiStepType(st.status)">{{ aiStepLabel(st.status) }}</el-tag>
              <b class="ai-step-name">{{ st.step }}</b>
              <span class="ai-step-ms">{{ st.ms ?? 0 }} ms</span>
              <div v-if="st.detail" class="ai-step-detail">{{ st.detail }}</div>
            </div>
          </div>
          <div v-if="(aiInit.result.artifact?.highlights || []).length" class="ai-highlights">
            <div v-for="(h, i) in aiInit.result.artifact.highlights" :key="i" class="ai-highlight">· {{ h }}</div>
          </div>
          <div v-if="(aiInit.result.tickets || []).length" class="tip ai-tickets">
            {{ t('bootstrap.ai.tickets_tip', { n: aiInit.result.tickets.length }) }}
          </div>
          <!-- eslint-disable-next-line vue/no-v-html -->
          <div v-if="aiInitMd" class="ai-report markdown-body" v-html="aiInitMd" />
        </template>
      </el-card>
        </el-collapse-item>
      </el-collapse>

      <!-- 工具区：阿里云探测结果 -->
      <el-card shadow="never" class="section-card">
        <template #header>
          <div class="card-head">
            <b>{{ t('bootstrap.probe.title') }}</b>
            <span v-if="probedAt" class="tip">{{ t('bootstrap.probe.at') }}{{ fmtTime(probedAt) }}</span>
            <span v-else-if="faces.length" class="tip">{{ t('bootstrap.probe.from_report') }}</span>
          </div>
        </template>
        <el-form label-position="top" class="probe-form" @submit.prevent="onProbe">
          <CloudCredentialForm
            id-prefix="bootstrap-probe" :show-identity="false" :ak="cloudForm.ak" :sk="cloudForm.sk"
            @update:ak="cloudForm.ak = $event" @update:sk="cloudForm.sk = $event"
          />
        </el-form>
        <el-button type="primary" :loading="probing" @click="onProbe">{{ t('bootstrap.btn.start_probe') }}</el-button>
        <el-empty v-if="!faces.length" :description="t('bootstrap.probe.empty')" :image-size="60" />
        <div v-else class="face-list">
          <div v-for="f in faceRows" :key="f.key" class="face-row">
            <el-tag :type="STATUS_TAG[f.status] || 'info'" effect="dark" class="face-tag">
              {{ STATUS_TEXT[f.status] ? t(STATUS_TEXT[f.status]) : f.status }}
            </el-tag>
            <div class="face-body">
              <div class="face-label">{{ f.label }}</div>
              <div class="face-detail">{{ f.detail }}</div>
              <div v-if="f.hint" class="face-hint">{{ t('bootstrap.probe.suggest') }}{{ f.hint }}</div>
            </div>
          </div>
        </div>
      </el-card>

      <!-- 工具区：底座体检报告 -->
      <el-card shadow="never" class="section-card" v-loading="reportLoading">
        <template #header><b>{{ t('bootstrap.report.title') }}</b></template>

        <el-alert v-if="reportError" type="error" :closable="false" show-icon style="margin-bottom: 10px"
                  :title="reportError">
          <el-button size="small" @click="loadReport">{{ t('common.retry') }}</el-button>
        </el-alert>
        <div v-else-if="!reportReady" class="tip report-empty">
          {{ t('bootstrap.report.empty') }}
        </div>
        <template v-else>
          <div class="report-title">{{ t('bootstrap.report.cap_table') }}</div>
          <el-table :data="capabilities" border stripe size="default">
            <el-table-column prop="name" :label="t('bootstrap.report.col_cap')" min-width="160" />
            <el-table-column :label="t('bootstrap.report.col_ready')" width="110">
              <template #default="{ row }">
                <el-tag :type="row.ready ? 'success' : 'warning'" size="small">
                  {{ row.ready ? t('bootstrap.report.ready') : t('bootstrap.report.not_ready') }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column :label="t('bootstrap.report.col_missing')" min-width="260">
              <template #default="{ row }">
                <span v-if="row.missing?.length">{{ row.missing.join('、') }}</span>
                <span v-else class="tip">—</span>
              </template>
            </el-table-column>
          </el-table>

          <div class="report-title">{{ t('bootstrap.report.gap_title') }}</div>
          <el-empty
            v-if="!gaps.length"
            :description="t('bootstrap.report.no_gaps')"
            :image-size="60"
          />
          <div v-else class="gap-list">
            <div v-for="(g, i) in gaps" :key="i" class="gap-item">
              <div class="gap-head">
                <el-tag size="small" type="warning" effect="plain">{{ FACE_LABELS[g.face] ? t(FACE_LABELS[g.face]) : g.face }}</el-tag>
                <b>{{ t('bootstrap.report.gap_what') }}</b>{{ FACE_LABELS[g.face] ? t(FACE_LABELS[g.face]) : g.face }}{{ t('bootstrap.report.gap_suffix') }}
              </div>
              <div class="gap-line"><b>{{ t('bootstrap.report.gap_impact') }}</b>{{ g.impact }}</div>
              <div class="gap-line"><b>{{ t('bootstrap.report.gap_fix') }}</b>{{ g.fix }}</div>
              <div class="gap-line"><b>{{ t('bootstrap.report.gap_unlock') }}</b>{{ g.unlock }}</div>
            </div>
          </div>
        </template>
      </el-card>

      <!-- 新手主路径：账号级完整发现与集中确认；底层配置键收进高级手选。 -->
      <el-card id="sls-workspace" shadow="never" class="section-card" v-loading="applyingSls">
        <template #header>
          <div class="card-head sls-head">
            <div>
              <b>{{ t('bootstrap.sls.title') }}</b>
              <div class="tip">{{ t('bootstrap.sls.tip') }}</div>
            </div>
            <div class="sls-account-tools" v-if="slsAccounts.length">
              <el-select v-model="slsAccountId" :aria-label="t('bootstrap.sls.account')" style="width: 210px">
                <el-option v-for="account in slsAccounts" :key="account.id" :label="account.name" :value="account.id" />
              </el-select>
              <el-button :loading="slsLoading" @click="onStartSlsDiscovery">
                {{ slsDiscovery?.status === 'running' ? t('bootstrap.sls.discovering') : t('bootstrap.sls.rediscover') }}
              </el-button>
            </div>
          </div>
        </template>

        <el-empty v-if="!slsAccounts.length" :description="t('bootstrap.sls.no_account')" :image-size="60" />
        <template v-else>
          <div v-loading="slsLoading && !slsDiscovery" class="sls-discovery">
            <el-alert
              v-if="slsTargetItem"
              :type="slsTargetItem.status === 'ready' ? 'success' : (slsTargetItem.status === 'skipped' ? 'info' : 'warning')"
              :title="slsTargetItem.detail"
              :closable="false"
              show-icon
              class="sls-alert"
            />
            <el-alert
              v-if="slsDiscovery?.status === 'pending'"
              type="info" :title="t('bootstrap.sls.pending')" :closable="false" show-icon class="sls-alert"
            />
            <el-alert
              v-else-if="slsDiscovery?.status === 'running'"
              type="info" :title="t('bootstrap.sls.running')" :closable="false" show-icon class="sls-alert"
            />
            <template v-if="slsDiscovery && slsDiscovery.status !== 'pending' && slsDiscovery.status !== 'running'">
              <div class="sls-metrics">
                <div><b>{{ slsCoverage.projects || 0 }}</b><span>Project</span></div>
                <div><b>{{ slsCoverage.logstores || 0 }}</b><span>Logstore</span></div>
                <div><b>{{ slsCoverage.sampled || 0 }}</b><span>{{ t('bootstrap.sls.sampled') }}</span></div>
                <div><b>{{ slsCoverage.recommended || 0 }}</b><span>{{ t('bootstrap.sls.recommended') }}</span></div>
                <div><b>{{ slsCoverage.no_recent_data || 0 }}</b><span>{{ t('bootstrap.sls.no_recent') }}</span></div>
              </div>
              <el-alert
                v-if="!slsCoverage.complete"
                type="warning"
                :title="t('bootstrap.sls.partial', { projects: slsCoverage.project_failures || 0, logs: slsCoverage.query_failures || 0 })"
                :description="slsDiscovery.detail || slsCoverage.project_listing_error"
                :closable="false" show-icon class="sls-alert"
              />
              <div v-if="slsDiscovery.projects?.some((p) => p.status === 'failed')" class="sls-project-errors">
                <div v-for="project in slsDiscovery.projects.filter((p) => p.status === 'failed')" :key="project.project">
                  <b>{{ project.project }}</b>：{{ project.error }}
                </div>
              </div>

              <el-empty v-if="!slsLogstores.length && slsCoverage.complete"
                        :description="t('bootstrap.sls.empty_catalog')" :image-size="60" />
              <template v-if="slsLogstores.length">
                <div class="sls-filter-row">
                  <el-radio-group v-model="slsRoleFilter" size="small">
                    <el-radio-button value="recommended">{{ t('bootstrap.sls.filter_recommended') }} {{ slsCoverage.recommended || 0 }}</el-radio-button>
                    <el-radio-button value="all">{{ t('bootstrap.sls.filter_all') }} {{ slsLogstores.length }}</el-radio-button>
                    <el-radio-button value="slb">SLB {{ slsCoverage.role_counts?.slb || 0 }}</el-radio-button>
                    <el-radio-button value="cdn">CDN {{ slsCoverage.role_counts?.cdn || 0 }}</el-radio-button>
                    <el-radio-button value="application_json">{{ t('bootstrap.sls.role_application_json') }} {{ slsCoverage.role_counts?.application_json || 0 }}</el-radio-button>
                    <el-radio-button value="application_text">{{ t('bootstrap.sls.role_application_text') }} {{ slsCoverage.role_counts?.application_text || 0 }}</el-radio-button>
                    <el-radio-button value="unknown">{{ t('bootstrap.sls.role_unknown') }} {{ slsCoverage.role_counts?.unknown || 0 }}</el-radio-button>
                    <el-radio-button value="no_recent_data">{{ t('bootstrap.sls.no_recent') }} {{ slsCoverage.no_recent_data || 0 }}</el-radio-button>
                    <el-radio-button value="failed">{{ t('bootstrap.sls.query_failed') }} {{ slsCoverage.query_failures || 0 }}</el-radio-button>
                  </el-radio-group>
                </div>
                <p class="tip sls-select-tip">{{ t('bootstrap.sls.selection_tip') }}</p>
                <el-table :data="slsPageRows" :row-key="slsRowKey" border size="small" class="sls-table">
                  <el-table-column type="expand" width="36">
                    <template #default="{ row }">
                      <div class="sls-evidence">
                        <div v-for="item in row.evidence || []" :key="item.code">{{ item.detail }}</div>
                        <div v-if="row.error" class="sls-error">{{ row.error }}</div>
                        <div class="sls-fields">
                          <span>{{ t('bootstrap.sls.fields', { n: row.field_count || 0 }) }}</span>
                          <el-tag v-for="field in row.fields || []" :key="field" size="small" effect="plain">{{ field }}</el-tag>
                        </div>
                      </div>
                    </template>
                  </el-table-column>
                  <el-table-column :label="t('bootstrap.sls.col_target')" min-width="210">
                    <template #default="{ row }">
                      <div class="sls-target-name">{{ row.project }}</div>
                      <div class="sls-target-store">{{ row.logstore }}</div>
                    </template>
                  </el-table-column>
                  <el-table-column :label="t('bootstrap.sls.col_role')" width="150">
                    <template #default="{ row }">
                      <el-tag :type="row.adoptable ? 'primary' : 'info'" size="small" effect="plain">{{ slsRoleLabel(row.role) }}</el-tag>
                      <div v-if="row.confidence" class="tip">{{ t('bootstrap.sls.confidence', { n: Math.round(row.confidence * 100) }) }}</div>
                    </template>
                  </el-table-column>
                  <el-table-column :label="t('bootstrap.sls.col_data')" width="150">
                    <template #default="{ row }">
                      <el-tag :type="row.query_status === 'failed' ? 'danger' : (row.data_status === 'no_recent_data' ? 'info' : 'success')" size="small">
                        {{ slsDataLabel(row) }}
                      </el-tag>
                      <div v-if="row.latest_at" class="tip">{{ fmtTime(new Date(row.latest_at * 1000).toISOString()) }}</div>
                    </template>
                  </el-table-column>
                  <el-table-column :label="t('bootstrap.sls.col_adopt')" min-width="210">
                    <template #default="{ row }">
                      <template v-if="row.role === 'slb' && row.adoptable">
                        <el-checkbox :model-value="slsSelectedSlb[slsRowKey(row)] !== undefined"
                                     @change="toggleSlb(row, $event)">{{ t('bootstrap.sls.select_slb') }}</el-checkbox>
                        <el-select v-if="slsSelectedSlb[slsRowKey(row)] !== undefined"
                                   v-model="slsSelectedSlb[slsRowKey(row)]" :placeholder="t('bootstrap.sls.chain_ph')"
                                   size="small" style="width: 130px">
                          <el-option :label="t('bootstrap.sls.outer')" value="outer" />
                          <el-option :label="t('bootstrap.sls.inner')" value="inner" />
                        </el-select>
                      </template>
                      <el-radio-group v-else-if="['application_json', 'application_text'].includes(row.role) && row.adoptable"
                                      v-model="slsPrimary.application">
                        <el-radio :value="slsRowKey(row)">{{ t('bootstrap.sls.select_app') }}</el-radio>
                      </el-radio-group>
                      <el-radio-group v-else-if="row.role === 'cdn' && row.adoptable" v-model="slsPrimary.cdn">
                        <el-radio :value="slsRowKey(row)">{{ t('bootstrap.sls.select_cdn') }}</el-radio>
                      </el-radio-group>
                      <span v-else class="tip">{{ t('bootstrap.sls.not_adoptable') }}</span>
                    </template>
                  </el-table-column>
                </el-table>
                <div class="sls-pagination">
                  <span class="tip">{{ t('bootstrap.sls.catalog_note') }}</span>
                  <el-pagination v-model:current-page="slsPage" :page-size="slsPageSize" :total="slsFilteredRows.length"
                                 layout="prev, pager, next, total" background small />
                </div>
                <div class="sls-actions">
                  <el-button type="primary" :loading="applyingSls" :disabled="!slsSelectedCount"
                             @click="onApplySlsRecommendations">
                    {{ t('bootstrap.sls.apply', { n: slsSelectedCount }) }}
                  </el-button>
                  <el-button :disabled="applyingSls" @click="onSkipSls">{{ t('bootstrap.sls.skip') }}</el-button>
                  <span class="tip">{{ t('bootstrap.sls.apply_tip') }}</span>
                </div>
              </template>
            </template>
          </div>
        </template>

        <el-collapse class="sls-manual">
          <el-collapse-item :title="t('bootstrap.sls.advanced')" name="manual">
            <p class="tip">{{ t('bootstrap.sls.advanced_tip') }}</p>
            <el-button size="small" :loading="slsLoading" @click="loadCatalog">{{ t('bootstrap.btn.load_catalog') }}</el-button>
            <div v-if="slsCatalog.some((p) => p.status === 'failed')" class="sls-project-errors">
              <div v-for="project in slsCatalog.filter((p) => p.status === 'failed')" :key="project.project">
                <b>{{ project.project }}</b>：{{ project.error }}
              </div>
            </div>
            <el-table v-if="slsCatalog.length" :data="slsManualRows" border size="small" class="sls-table">
              <el-table-column :label="t('bootstrap.sls.col_use')" width="110">
                <template #default="{ row }">{{ t(row.labelKey) }}</template>
              </el-table-column>
              <el-table-column label="Project" min-width="180">
                <template #default="{ row }">
                  <el-select v-model="slsForm[row.kind + 'Project']" filterable clearable
                             :placeholder="t('bootstrap.sls.ph_project')" style="width: 100%"
                             @change="onProjectChange(row.kind)">
                    <el-option v-for="p in slsCatalog" :key="p.project" :label="p.project" :value="p.project" />
                  </el-select>
                </template>
              </el-table-column>
              <el-table-column label="Logstore" min-width="180">
                <template #default="{ row }">
                  <el-select v-model="slsForm[row.kind + 'Store']" filterable clearable
                             :placeholder="t('bootstrap.sls.ph_logstore')" style="width: 100%">
                    <el-option v-for="ls in storeOptions(slsForm[row.kind + 'Project'])"
                               :key="ls" :label="ls" :value="ls" />
                  </el-select>
                </template>
              </el-table-column>
              <el-table-column :label="t('bootstrap.col.detail')" min-width="160">
                <template #default="{ row }"><span class="tip">{{ t(row.noteKey) }}</span></template>
              </el-table-column>
            </el-table>
            <el-button v-if="slsCatalog.length" type="primary" class="apply-btn"
                       :loading="applyingSls" @click="onApplySls">
              {{ t('bootstrap.btn.apply') }}
            </el-button>
          </el-collapse-item>
        </el-collapse>
      </el-card>
    </template>

    <!-- 非云机器：Agent 安装命令 + 脚本下载 -->
    <el-dialog v-model="agentDialog" :title="t('bootstrap.agent.title')" width="620px">
      <template v-if="agentInfo">
        <el-alert v-if="!agentInfo.token_configured" type="warning" :closable="false"
                  :title="t('bootstrap.agent.token_warn')" style="margin-bottom: 12px" />
        <div class="tip">{{ t('bootstrap.agent.hosts', { n: agentInfo.hosts_online }) }}</div>
        <div class="wiz-inline">
          <el-button type="primary" plain @click="downloadAgentScript">{{ t('bootstrap.btn.download_agent') }}</el-button>
        </div>
        <pre class="cmd-box">{{ agentInfo.command }}</pre>
        <div class="tip">{{ agentInfo.hint }}</div>
      </template>
    </el-dialog>

    <!-- 已有监控：数据源清单复用 DataSourceDialog 编辑，并展示连通状态 -->
    <el-dialog v-model="monitorDialog" :title="t('bootstrap.monitor.title')" width="820px">
      <div v-if="dsBuiltin" class="ds-builtin">
        <el-tag size="small" type="success" effect="plain">{{ t('bootstrap.monitor.builtin') }}</el-tag>
        <span>{{ dsBuiltin.label }}：{{ dsBuiltin.name }}{{ t('bootstrap.monitor.builtin_note') }}</span>
      </div>

      <el-table :data="dsRows" size="small" border :empty-text="t('bootstrap.monitor.ds_empty')">
        <el-table-column prop="name" :label="t('bootstrap.col.name')" width="150" show-overflow-tooltip />
        <el-table-column prop="type" :label="t('bootstrap.col.type')" width="130" />
        <el-table-column prop="url" :label="t('bootstrap.col.url')" min-width="200" show-overflow-tooltip />
        <el-table-column :label="t('bootstrap.ds.col_status')" width="120">
          <template #default="{ row }">
            <template v-if="row.probe">
              <el-tag :type="row.probe.ok ? 'success' : 'danger'" size="small">
                {{ row.probe.ok ? t('bootstrap.monitor.up') : t('bootstrap.monitor.down') }}
              </el-tag>
            </template>
            <span v-else class="sub">—</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('bootstrap.col.actions')" width="110">
          <template #default="{ row }">
            <el-button size="small" text type="primary" @click="openDsEdit(row)">{{ t('bootstrap.btn.edit_ds') }}</el-button>
            <el-button size="small" text type="danger" @click="removeDsRow(row)">{{ t('bootstrap.btn.del') }}</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div class="ds-save-row">
        <el-button type="primary" plain @click="openDsAdd('metric')">{{ t('bootstrap.btn.add_metric') }}</el-button>
        <el-button plain @click="openDsAdd('log')">{{ t('bootstrap.btn.add_log') }}</el-button>
        <el-button :loading="reprobeLoading" @click="reprobeMonitor">{{ t('bootstrap.btn.reprobe') }}</el-button>
        <span class="tip">{{ t('bootstrap.monitor.save_tip') }}</span>
      </div>
      <p class="tip">{{ t('bootstrap.monitor.test_note') }}</p>

      <template v-if="monitorInfo">
        <div class="ds-title">{{ t('bootstrap.monitor.conn_status') }}</div>
        <el-table :data="[...(monitorInfo.metrics || []), ...(monitorInfo.logs || [])]" size="small" border>
          <el-table-column :label="t('bootstrap.monitor.col_test')" width="280">
            <template #default="{ row }">
              <template v-if="row.pending">
                <el-tag type="info" size="small">{{ t('bootstrap.monitor.registered_tested') }}</el-tag>
              </template>
              <template v-else>
                <el-tag :type="row.ok ? 'success' : 'danger'" size="small">{{ row.ok ? t('bootstrap.monitor.up') : t('bootstrap.monitor.down') }}</el-tag>
                <span class="ds-detail" :title="row.detail">{{ row.detail ? ' · ' + row.detail : '' }}</span>
              </template>
            </template>
          </el-table-column>
          <el-table-column prop="name" :label="t('bootstrap.col.name')" width="140" />
          <el-table-column prop="type" :label="t('bootstrap.col.type')" width="100" />
          <el-table-column prop="url" :label="t('bootstrap.col.url')" min-width="180" show-overflow-tooltip />
        </el-table>
      </template>
    </el-dialog>

    <!-- 新增/编辑数据源复用集成中心同一弹窗组件，统一 headers/verify_ssl、
         Zabbix 凭证和分步教程；保存走同一 PUT /settings/datasource-config -->
    <DataSourceDialog v-model="dsDialog" :preset-type="dsPreset" :edit-source="dsEdit" @save="onDsSave" />
  </div>
</template>

<style scoped>
.page {
  max-width: 1080px;
}
.init-alert {
  margin-bottom: 12px;
}
.section-card {
  margin-bottom: 14px;
}
.card-head {
  display: flex;
  align-items: baseline;
  gap: 12px;
}
.tip {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.cmd-box {
  background: var(--el-fill-color-light);
  padding: 12px 14px;
  border-radius: 8px;
  font-size: 12.5px;
  white-space: pre-wrap;
  word-break: break-all;
}
.report-empty {
  padding: 12px 0;
}
.sls-title {
  font-weight: 600;
  margin-bottom: 8px;
}
.sls-table {
  margin: 12px 0;
}
.apply-btn {
  margin-top: 4px;
}
.sls-head {
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
}
.sls-account-tools,
.sls-actions,
.sls-pagination {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.sls-discovery {
  min-height: 52px;
}
.sls-alert {
  margin-bottom: 12px;
}
.sls-metrics {
  display: flex;
  gap: 12px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.sls-metrics > div {
  flex: 1;
  min-width: 110px;
  padding: 10px 14px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  background: var(--el-fill-color-lighter);
}
.sls-metrics b,
.sls-metrics span {
  display: block;
}
.sls-metrics b {
  font-size: 22px;
  line-height: 1.4;
}
.sls-metrics span {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.sls-project-errors {
  margin-bottom: 12px;
  color: var(--el-color-danger);
  font-size: 12px;
  line-height: 1.7;
  overflow-wrap: anywhere;
}
.sls-filter-row {
  overflow-x: auto;
  padding-bottom: 4px;
}
.sls-select-tip {
  line-height: 1.6;
}
.sls-target-name,
.sls-target-store {
  overflow-wrap: anywhere;
}
.sls-target-store {
  color: var(--el-text-color-regular);
  font-family: var(--el-font-family-monospace, monospace);
}
.sls-evidence {
  padding: 8px 24px;
  line-height: 1.7;
  font-size: 12px;
}
.sls-fields {
  display: flex;
  align-items: center;
  gap: 5px;
  flex-wrap: wrap;
  margin-top: 6px;
}
.sls-error {
  color: var(--el-color-danger);
}
.sls-pagination {
  justify-content: space-between;
  margin: 10px 0 14px;
}
.sls-manual {
  margin-top: 18px;
  border-bottom: 0;
}

/* 首登部署向导 */
.wiz-steps {
  margin: 4px 0 20px;
}
.wiz-body {
  min-height: 180px;
}
.wiz-title {
  margin: 0 0 8px;
  font-size: 16px;
}
.wiz-q {
  font-weight: 600;
  margin: 10px 0 8px;
}
.wiz-check {
  margin-right: 18px;
}
.wiz-vendor-card {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  padding: 12px 16px 4px;
  margin-bottom: 12px;
}
.wiz-vendor-title {
  font-weight: 600;
  margin-bottom: 4px;
}
.wiz-form {
  margin-top: 4px;
}
.wiz-test-tag {
  margin-left: 8px;
}
.wiz-inline {
  display: flex;
  gap: 8px;
  margin: 10px 0;
  flex-wrap: wrap;
}
.advanced-inline {
  margin: 8px 0;
}
.wiz-summary {
  margin: 6px 0 10px;
}
.wiz-save-result {
  margin: 4px 0;
}
.wiz-actions {
  display: flex;
  gap: 8px;
  margin-top: 16px;
  justify-content: flex-end;
  align-items: center;
  flex-wrap: wrap;
}
.wiz-ok {
  color: var(--el-color-success);
  margin-left: 6px;
  vertical-align: -2px;
}
.wiz-collapse {
  margin-bottom: 8px;
}
.wiz-ak-hint {
  margin-top: 8px;
}
.wiz-next-tip {
  margin-left: 4px;
}

.onboarding-summary {
  margin-left: 8px;
}
.onboarding-card .card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.accepted-run {
  margin-bottom: 10px;
}
.onboarding-issue {
  color: var(--el-color-danger);
  line-height: 1.6;
}

/* AI 初始化 + AI 协助填写 */
.advanced-tools {
  margin-bottom: 14px;
}
.wiz-ai-init {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 12px;
  flex-wrap: wrap;
}
.ai-btn {
  margin-left: 8px;
}
.ai-suggest {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  padding: 8px 12px;
  margin: 4px 0 10px;
  background: var(--el-fill-color-lighter);
}
.ai-suggest-head {
  font-weight: 600;
  font-size: 13px;
  margin-bottom: 4px;
}
.ai-suggest-item {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding: 3px 0;
  font-size: 13px;
}
.ai-suggest-val {
  font-weight: 600;
}
.ai-error {
  color: var(--el-color-danger);
}
.ai-steps {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 8px 0;
}
.ai-step-row {
  border: 1px solid var(--el-border-color-extra-light);
  border-radius: 6px;
  padding: 8px 12px;
  font-size: 13px;
}
.ai-step-name {
  margin-left: 8px;
}
.ai-step-ms {
  margin-left: 8px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.ai-step-detail {
  margin-top: 4px;
  line-height: 1.7;
}
.ai-highlights {
  margin: 6px 0;
}
.ai-highlight {
  font-size: 13px;
  line-height: 1.8;
}
.ai-tickets {
  margin: 6px 0;
}
.ai-report {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px dashed var(--el-border-color-lighter);
  font-size: 13px;
  line-height: 1.8;
}

/* 已有监控：数据源配置表单 */
.ds-title {
  font-weight: 600;
  margin: 12px 0 8px;
}
.ds-builtin {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  margin-bottom: 8px;
  border: 1px solid var(--el-color-success-light-7);
  border-radius: 6px;
  background: var(--el-color-success-light-9);
  font-size: 12.5px;
}
.ds-row {
  display: flex;
  gap: 8px;
  margin-bottom: 8px;
  align-items: center;
}
.ds-auth-row {
  padding-left: 8px;
  opacity: 0.95;
}
.ds-detail {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-left: 2px;
}
.ds-save-row {
  display: flex;
  gap: 10px;
  align-items: center;
  margin: 14px 0 4px;
  flex-wrap: wrap;
}
.probe-form {
  max-width: 420px;
  margin-bottom: 4px;
}

/* 五面探测结果 */
.face-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.face-row {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 8px 12px;
  border: 1px solid var(--el-border-color-extra-light);
  border-radius: 6px;
}
.face-tag {
  width: 76px;
  text-align: center;
  flex-shrink: 0;
  margin-top: 2px;
}
.face-label {
  font-weight: 600;
  font-size: 14px;
}
.face-detail {
  font-size: 13px;
  margin-top: 2px;
}
.face-hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-top: 2px;
}

/* 体检报告 */
.report-title {
  font-size: 14px;
  font-weight: 600;
  margin: 16px 0 10px;
}
.gap-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.gap-item {
  padding: 10px 14px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: var(--el-bg-color-overlay);
  font-size: 13px;
  line-height: 1.7;
}
.gap-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}
.gap-line {
  margin-top: 2px;
}
.gap-line b,
.gap-head b {
  font-weight: 600;
}
</style>
