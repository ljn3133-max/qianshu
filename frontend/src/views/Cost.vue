<script setup>
import CostDrill from './cost/CostDrill.vue'
import CostOverview from './cost/CostOverview.vue'
import CostPacks from './cost/CostPacks.vue'
import CostCdn from './cost/CostCdn.vue'
import CostAnomalies from './cost/CostAnomalies.vue'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { t } from '@/i18n'
import { ElMessage } from 'element-plus'

import { ackAnomalies, cdnDomainTraffic, cdnUrls, collectStatus, costAnomalies, costInsights, listCostDaily, packsImpact, paygDrilldown, purchaseAdvice, resourcePacks, triggerCollect, triggerDetect } from '@/api/cost'
import { exportCsv } from '@/api/download'
import { createSession, runWorkflow } from '@/api/agent'
import { fmtTime } from '@/utils/format'
import { useAuthStore } from '@/stores/auth'
import { useRoute, useRouter } from 'vue-router'

const auth = useAuthStore()
const canWrite = computed(() => ['admin', 'operator'].includes(auth.role))

const activeTab = ref('overview')

// 深链支持：/cost?tab=packs 直达页签（首页待办"去看"要落到相关内容上）
const route = useRoute()
const router = useRouter()

// 账单答疑：数据点上的「为什么」→ 跳千枢智能体自动分析
// session=new 触发深链自动发送，进入新会话即问
function why(q) {
  router.push({ path: '/agent', query: { session: 'new', q } })
}
if (route.query.tab) activeTab.value = String(route.query.tab)

/* ---------------- 时间范围：各成本组件共用同一区间 ---------------- */
const RANGES = [
  { value: 7, labelKey: 'cost.range.7' },
  { value: 14, labelKey: 'cost.range.14' },
  { value: 30, labelKey: 'cost.range.30' },
  { value: 90, labelKey: 'cost.range.90' },
]
const rangeDays = ref(30)

// 换区间=换统计口径：汇总/实例/异动/拆解一起重算，避免同一页面两套时间窗
function changeRange() {
  detailVisible.value = false
  detailRequest += 1
  detailRows.value = []
  loadAll()
  loadDrill()
  loadAdvice()
}

function rangeParams() {
  // end=T-1；用本地时区格式化（toISOString 是 UTC，凌晨访问会错位一天）
  const fmt = (d) => {
    const y = d.getFullYear()
    const m = String(d.getMonth() + 1).padStart(2, '0')
    const day = String(d.getDate()).padStart(2, '0')
    return `${y}-${m}-${day}`
  }
  const end = new Date()
  end.setDate(end.getDate() - 1)
  const start = new Date(end)
  start.setDate(start.getDate() - (rangeDays.value - 1))
  return { start: fmt(start), end: fmt(end) }
}

// CSV 导出：与当前区间/实例/账号筛选同口径，绕开列表 page_size 上限
const exportingDetail = ref(false)
async function exportDaily(instanceId = '', accountId = selectedAccountId(), product = '') {
  exportingDetail.value = true
  try {
    const params = { ...rangeParams(), account_id: accountId }
    if (instanceId) params.instance_id = instanceId
    if (product) params.product = product
    await exportCsv('/cost/daily/export', params, 'cost-daily.csv')
  } finally {
    exportingDetail.value = false
  }
}

/* ---------------- 采集状态 ---------------- */
const status = ref(null)
const collecting = ref(false)
const statusError = ref('')

async function loadStatus() {
  try {
    status.value = await collectStatus()
    statusError.value = ''
  } catch (e) {
    status.value = null
    statusError.value = e?.response?.data?.detail || t('cost.load.status_fail')
  }
}

async function doCollect() {
  collecting.value = true
  try {
    const r = await triggerCollect({})
    ElMessage.success(t('cost.msg.collect_done', { rows: r.rows })
      + (r.errors?.length ? t('cost.msg.collect_done_err', { n: r.errors.length }) : '')
      + (r.anomalies ? t('cost.msg.collect_done_anom', { n: r.anomalies }) : '')
      + t('cost.msg.collect_done_tail'))
    loadStatus()
    loadDrill()    // 采集后刷新拆解，避免继续展示旧空态
    loadAdvice()
    loadAll()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || t('cost.load.collect_fail'))
  } finally {
    collecting.value = false
  }
}

/* ---------------- 唯一成本事实 ---------------- */
const insights = ref(null)
const insightsLoading = ref(false)
const insightsError = ref('')
let insightsRequest = 0

function selectedAccountId() {
  const value = Number(localStorage.getItem('qianshu.account') || 0)
  return Number.isSafeInteger(value) && value >= 0 ? value : 0
}

async function loadInsights() {
  const request = ++insightsRequest
  const params = { ...rangeParams(), account_id: selectedAccountId() }
  // 新范围发出前撤下旧卡；请求序号防止晚到的旧账号/旧区间覆盖当前结果。
  insights.value = null
  insightsLoading.value = true
  insightsError.value = ''
  try {
    const data = await costInsights(params)
    if (request !== insightsRequest) return
    const sameScope = data?.requested?.account_id === params.account_id
      && data?.requested?.start === params.start && data?.requested?.end === params.end
    if (!sameScope) throw new Error(t('cost.insight.scope_mismatch'))
    insights.value = data
  } catch (e) {
    if (request !== insightsRequest) return
    insights.value = null
    insightsError.value = e?.response?.data?.detail || e?.message || t('cost.load.insights_fail')
  } finally {
    if (request === insightsRequest) insightsLoading.value = false
  }
}

const agentStarting = ref(false)
let pendingAgentSession = null

async function askCostAgent(item) {
  if (agentStarting.value) return
  const findings = [...(item.days || []), ...(item.anomalies || []),
    ...(item.observations || []), ...(item.recommendations || [])]
  const ctx = {
    kind: 'cost_insights', source: '/api/cost/insights',
    account_id: item.account.id, account_name: item.account.name,
    start: item.range.start, end: item.range.end, days: item.range.days,
    status: item.status, reason_code: item.reason_code,
    evidence_refs: [...new Set(findings.flatMap((x) => x.evidence_refs || []))],
  }
  agentStarting.value = true
  try {
    // 工作流分发不消费聊天 ctx；直接提交结构化窗口，避免自然语言漏掉账号或日期。
    const scopeKey = `${item.account.id}:${item.range.start}:${item.range.end}`
    let session = pendingAgentSession?.key === scopeKey ? pendingAgentSession.session : null
    if (!session) {
      session = await createSession({
        title: t('cost.insight.agent_session', {
          name: item.account.name, start: item.range.start, end: item.range.end,
        }),
      })
      pendingAgentSession = { key: scopeKey, session }
    }
    await runWorkflow('cost_checkup', {
      account_id: item.account.id, start: item.range.start,
      end: item.range.end, days: item.range.days,
    }, session.id)
    pendingAgentSession = null
    router.push({
      path: '/agent',
      query: { session: String(session.id), ctx: JSON.stringify(ctx) },
    })
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || e?.message || t('cost.insight.agent_start_failed'))
  } finally {
    agentStarting.value = false
  }
}

/* ---------------- 异动检测 ---------------- */
const ANOMALY_TYPES = {
  payg_spike: { label: '按量突增', tag: 'danger' },
  payg_new: { label: '新增费用项', tag: 'warning' },
  pack_spike: { label: '消耗突增', tag: 'danger' },
  pack_exhausting: { label: '池将耗尽', tag: 'warning' },
  pack_exhausted: { label: '已耗尽', tag: 'danger' },
  pack_expiry: { label: '即将到期', tag: 'warning' },
  profile_drift: { label: '构成漂移', tag: 'danger' },
  host_down: { label: '服务器关机', tag: 'danger' },
  aux_spillover: { label: '辅助机器5xx外溢', tag: 'danger' },
  traffic_spike: { label: '流量突增', tag: 'danger' },
  growth_accel: { label: '增速突变', tag: 'danger' },
}
// 成本页只看成本类；监控类/画像类归各自域，判定见后端 ANOMALY_TYPE_DOMAINS
const COST_ANOMALY_TYPES = {
  payg_spike: ANOMALY_TYPES.payg_spike,
  payg_new: ANOMALY_TYPES.payg_new,
  pack_spike: ANOMALY_TYPES.pack_spike,
  pack_exhausting: ANOMALY_TYPES.pack_exhausting,
  pack_exhausted: ANOMALY_TYPES.pack_exhausted,
  pack_expiry: ANOMALY_TYPES.pack_expiry,
}
const typeLabel = (t) => ANOMALY_TYPES[t]?.label || t
const typeTag = (t) => ANOMALY_TYPES[t]?.tag || 'info'

const anomalies = ref([])
const anomaliesTotal = ref(0)
const anomaliesLoading = ref(false)
const anomaliesError = ref('')
const anomalyQuery = reactive({ page: 1, page_size: 20 })
const anomalyTypeFilter = ref('')
const anomalyLevelFilter = ref('')
const anomalyStatusFilter = ref('open')   // 默认只看待处理
const anomalySelection = ref([])

const ACK_STATUS = {
  open: { label: '待处理', tag: 'danger' },
  acknowledged: { label: '已确认', tag: 'success' },
  ignored: { label: '已忽略', tag: 'info' },
  resolved: { label: '已关闭', tag: 'info' },
}
const detecting = ref(false)

async function loadAnomalies() {
  anomaliesLoading.value = true
  anomaliesError.value = ''
  try {
    // 资源包异动的 detect_date 可能是今天（当日快照），end 放宽到今天
    const now = new Date()
    const todayStr = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`
    // domain=cost：成本页只查成本类异动，监控/画像类归各自域
    const params = { ...rangeParams(), end: todayStr, domain: 'cost', ...anomalyQuery }
    if (anomalyTypeFilter.value) params.type = anomalyTypeFilter.value
    if (anomalyLevelFilter.value) params.level = anomalyLevelFilter.value
    if (anomalyStatusFilter.value) params.status = anomalyStatusFilter.value
    const data = await costAnomalies(params)
    anomaliesTotal.value = data.total
    anomalies.value = data.items
  } catch (e) {
    anomaliesTotal.value = 0
    anomalies.value = []
    anomaliesError.value = e?.response?.data?.detail || t('cost.load.anomalies_fail')
  } finally {
    anomaliesLoading.value = false
  }
}

/* 生命周期操作：批量确认/忽略/重开 */
async function ackAnomaliesBatch(status, note) {
  if (!anomalySelection.value.length) {
    ElMessage.warning(t('cost.msg.select_first'))
    return
  }
  try {
    await ackAnomalies({ ids: anomalySelection.value.map((r) => r.id), status, note })
    ElMessage.success(t('cost.msg.batch_done', {
      action: status === 'open' ? t('cost.action.reopen') : status === 'acknowledged' ? t('cost.action.ack') : t('cost.action.ignore'),
      n: anomalySelection.value.length,
    }))
    // 已处理的行可能因筛选（默认只看待处理）从列表消失：清空勾选，避免下次
    // 批量操作把上一轮已看不到的 id 再次提交
    anomalySelection.value = []
    anomalyQuery.page = 1
    loadAnomalies()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || t('cost.msg.op_failed'))
  }
}

async function doDetect() {
  detecting.value = true
  try {
    const r = await triggerDetect({ packs: true })
    ElMessage.success(t('cost.msg.detect_done', { n: r.new_anomalies }))
    loadAnomalies()
    loadPacks()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || t('cost.msg.op_failed'))
  } finally {
    detecting.value = false
  }
}

/* ---------------- 昨日拆解：钱花在哪、量谁在烧 ---------------- */
const drill = ref(null)
const drillLoading = ref(false)
const drillError = ref('')
const drillProduct = ref('')
const drillScenarios = computed(() => {
  const arr = drill.value?.scenarios || []
  return drillProduct.value ? arr.filter((s) => s.product === drillProduct.value) : arr
})
const drillInstances = ref([])

async function loadDrill() {
  drillLoading.value = true
  drillError.value = ''
  try {
    drill.value = await paygDrilldown({ days: 7 })
    drillProduct.value = ''
    drillInstances.value = drill.value?.instances || []
  } catch (e) {
    drill.value = null
    drillInstances.value = []
    drillError.value = e?.response?.data?.detail || t('cost.load.drill_fail')
  } finally {
    drillLoading.value = false
  }
}

async function drillProductClick(row) {
  drillProduct.value = row.product
  try {
    const r = await paygDrilldown({ days: 7, product: row.product })
    drillInstances.value = r.instances || []
  } catch { /* 失败保留全量明细 */ }
}

const fmtYuan2 = (v) => `¥${Number(v || 0).toLocaleString('zh-CN', { maximumFractionDigits: 2 })}`

/* ---------------- 采购建议：给结论不给数据点 ---------------- */
const advice = ref(null)
const adviceLoading = ref(false)
const adviceError = ref('')

async function loadAdvice() {
  adviceLoading.value = true
  adviceError.value = ''
  try {
    advice.value = await purchaseAdvice()
  } catch (e) {
    advice.value = null
    adviceError.value = e?.response?.data?.detail || t('cost.load.advice_fail')
  } finally {
    adviceLoading.value = false
  }
}

const ADVISE_TAGS = { urgent: 'danger', warning: 'warning', info: 'info' }

/* ---------------- 资源包监控 ---------------- */
const packs = ref([])
const packsLoading = ref(false)
const packsError = ref('')
const packsSnapshot = ref(null)
const packStateFilter = ref('')

const PACK_STATES = [
  { value: '', label: '全部' },
  { value: 'exhausting', label: '即将耗尽' },
  { value: 'expiring', label: '即将到期' },
  { value: 'ok', label: '正常' },
]
const packStateText = {
  exhausted: '已耗尽', exhausting: '即将耗尽', expiring: '即将到期',
  rotating: '池内轮转', ok: '正常',
}
const packStateTag = {
  exhausted: 'danger', exhausting: 'danger', expiring: 'warning',
  rotating: 'info', ok: 'success',
}

const packItems = computed(() =>
  packStateFilter.value ? packs.value.filter((p) => p.state === packStateFilter.value) : packs.value
)

const packsExhausted = ref(0) // 已耗尽的包不进列表，只保留汇总数

const packSummary = computed(() => {
  const items = packs.value
  return {
    total: items.length,
    exhausted: packsExhausted.value,
    exhausting: items.filter((p) => p.state === 'exhausting').length,
    expiring: items.filter((p) => p.state === 'expiring').length,
  }
})

async function loadPacks() {
  packsLoading.value = true
  packsError.value = ''
  try {
    const data = await resourcePacks()
    packs.value = data.items || []
    packsExhausted.value = data.exhausted_count || 0
    packsSnapshot.value = data.snapshot_date
  } catch (e) {
    packs.value = []
    packsExhausted.value = 0
    packsSnapshot.value = null
    packsError.value = e?.response?.data?.detail || t('cost.load.packs_fail')
  } finally {
    packsLoading.value = false
  }
}

/* ---------------- 耗尽包费用影响（已耗尽的包不进列表，只取汇总数） ---------------- */
const packImpacts = ref([])

const impactTotalDaily = computed(() =>
  packImpacts.value.reduce((s, it) => s + (it.extra_daily > 0 ? it.extra_daily : 0), 0)
)

async function loadPackImpacts() {
  try {
    const data = await packsImpact()
    packImpacts.value = (data.items || []).filter(it => it.available)
  } catch { /* 影响分析仅辅助展示，失败不打断列表 */ }
}

/* ---------------- CDN 域名消耗 Top：流量包被谁烧掉 ---------------- */
const cdnData = ref(null)
const cdnLoading = ref(false)
const cdnError = ref('')
const cdnDays = ref(14)
const CDN_DAYS_OPTIONS = [7, 14, 30]

const cdnSeries = computed(() => {
  const domains = cdnData.value?.domains || []
  return domains.slice(0, 3).map((d) => ({
    name: d.domain,
    points: Object.entries(d.daily_gb || {}).sort().map(([date, gb]) => [date, gb]),
  }))
})

async function loadCdnDomains() {
  cdnLoading.value = true
  cdnError.value = ''
  try {
    cdnData.value = await cdnDomainTraffic({ days: cdnDays.value, top: 10 })
    // URL 分析（突增归因）：优先使用深链 ?domain=，否则默认消耗大头
    if (!urlDomain.value) {
      const deep = route.query.domain ? String(route.query.domain) : ''
      const domains = cdnData.value?.domains || []
      urlDomain.value = domains.find((d) => d.domain === deep)?.domain
        || (deep || domains[0]?.domain || '')
    }
    loadCdnUrls()
  } catch (e) {
    cdnData.value = null
    cdnError.value = e?.response?.data?.detail || t('cost.load.cdn_fail')
  } finally {
    cdnLoading.value = false
  }
}

/* ---------------- CDN URL/文件级下钻（突增归因） ---------------- */
const urlDomain = ref('')
const urlData = ref(null)
const urlLoading = ref(false)

async function loadCdnUrls() {
  if (!urlDomain.value) return
  urlLoading.value = true
  try {
    urlData.value = await cdnUrls({ domain: urlDomain.value, days: Math.min(7, cdnDays.value || 7), top: 10 })
  } catch (e) {
    urlData.value = { urls: [], note: e.response?.data?.detail || '查询失败' }
  } finally {
    urlLoading.value = false
  }
}

/* ---------------- 待办收口任务条 ---------------- */
const taskAnomalyId = ref(route.query.anomaly ? Number(route.query.anomaly) : 0)
const taskState = ref('pending')   // pending / done
const taskRecord = ref(null)

// 深链定位不依赖列表筛选：待办可能已被确认/已超出默认时间窗，列表里查不到时
// 按 anomaly_id 精确取单条（后端此参数跳过 30 天窗口），否则任务条只剩一个 id。
const taskAnomaly = computed(() =>
  taskRecord.value || anomalies.value.find((a) => a.id === taskAnomalyId.value) || null)

async function loadTaskAnomaly() {
  if (!taskAnomalyId.value) return
  try {
    const data = await costAnomalies({ anomaly_id: taskAnomalyId.value })
    taskRecord.value = (data.items || [])[0] || null
  } catch { /* 取不到时任务条降级显示 #id，不阻断页面 */ }
}

async function taskResolve(status) {
  try {
    await ackAnomalies({ ids: [taskAnomalyId.value], status, note: status === 'acknowledged' ? '落地页评估后确认' : '落地页评估后忽略' })
    taskState.value = 'done'
    ElMessage.success(status === 'acknowledged' ? t('cost.msg.resolve_ack') : t('cost.msg.resolve_ignore'))
    loadAnomalies()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('cost.msg.op_failed'))
  }
}

function onTabChange(name) {
  // 切到即刷新，避免采集后切回仍看到旧空态
  if (name === 'cdn' && !cdnData.value) loadCdnDomains()
  if (name === 'drill') loadDrill()
  if (name === 'packs') loadAdvice()
  if (name === 'anomalies' && !anomalies.value.length) loadAnomalies()
}

// ?domain= 不带 tab 时也直达域名页签，避免落在概览页签时参数静默失效
if (route.query.domain && !route.query.tab) activeTab.value = 'cdn'

// 站内信/异动页可在 Cost 已打开时再次跳转；同路由 query 变化也必须更新页签和任务条。
watch(() => route.fullPath, () => {
  const nextTab = route.query.tab ? String(route.query.tab) : (route.query.domain ? 'cdn' : '')
  if (nextTab) {
    activeTab.value = nextTab
    onTabChange(nextTab)
  }
  const nextId = route.query.anomaly ? Number(route.query.anomaly) : 0
  if (nextId !== taskAnomalyId.value) {
    taskAnomalyId.value = nextId
    taskRecord.value = null
    taskState.value = 'pending'
    loadTaskAnomaly()
  }
})

function fmtYuan(v) {
  return `¥${Number(v).toLocaleString('zh-CN', { maximumFractionDigits: v >= 100 ? 0 : 1 })}`
}

/* ---------------- 明细 ---------------- */
const detailVisible = ref(false)
const detailInstance = ref(null)
const detailRows = ref([])
const detailTotal = ref(0)
const detailLoading = ref(false)
const detailError = ref('')
const detailQuery = reactive({ page: 1, page_size: 50 })
let detailRequest = 0

async function openDetail(row, accountId = selectedAccountId()) {
  detailInstance.value = { ...row, account_id: accountId }
  detailVisible.value = true
  detailQuery.page = 1
  // 新对象返回前不得把上一账号/产品的行显示在新标题下。
  detailRows.value = []
  detailTotal.value = 0
  detailError.value = ''
  await loadDetail()
}

async function loadDetail() {
  if (!detailInstance.value) return
  const request = ++detailRequest
  const target = { ...detailInstance.value }
  const params = { ...rangeParams(), ...detailQuery }
  detailLoading.value = true
  detailError.value = ''
  try {
    const data = await listCostDaily({
      ...params, account_id: target.account_id,
      instance_id: target.instance_id, product: target.product_code || '',
    })
    if (request !== detailRequest) return
    detailRows.value = data.items
    detailTotal.value = data.total
  } catch (e) {
    if (request !== detailRequest) return
    detailRows.value = []
    detailTotal.value = 0
    detailError.value = e?.response?.data?.detail || t('cost.load.detail_fail')
  } finally {
    if (request === detailRequest) detailLoading.value = false
  }
}

const fmtMoney = (v) => (v == null ? '-' : `¥${Number(v).toFixed(2)}`)
const fmtDate = (s) => (s ? s.slice(0, 10) : '-')
const fmtQty = (v) => {
  if (v == null) return '-'
  const n = Number(v)
  if (n === 0) return '0'
  return Math.abs(n) >= 100 ? Math.round(n).toString() : String(Math.round(n * 100) / 100)
}

function loadAll() {
  loadInsights()
  loadAnomalies()
}

onMounted(() => {
  // 深链进来（?tab=cdn&domain=xxx）初始页签不触发 tab-change，懒加载页必须显式跑一次
  // 否则深链落在懒加载页签时不会发起请求。空态文案也不许把"没加载"说成"没数据"。
  onTabChange(activeTab.value)
  loadTaskAnomaly()
  loadStatus()
  loadAll()
  loadPacks()
  loadPackImpacts()
})
</script>
<template>
  <div class="page">
    <!-- 待办收口任务条：?anomaly=<id> 深链独立加载该条，不受列表筛选和时间窗限制 -->
    <div v-if="taskAnomalyId" class="task-bar" :class="{ done: taskState === 'done' }">
      <template v-if="taskState === 'pending'">
        <span class="task-text">
          待办收口：{{ taskAnomaly?.title || `异动 #${taskAnomalyId}` }}
          <span v-if="!taskAnomaly" class="sub">{{ t('cost.task.not_in_list') }}</span>
        </span>
        <el-button v-if="canWrite" size="small" type="success" plain @click="taskResolve('acknowledged')">{{ t('cost.btn.ack_done') }}</el-button>
        <el-button v-if="canWrite" size="small" type="info" plain @click="taskResolve('ignored')">{{ t('cost.btn.ignore_fp') }}</el-button>
      </template>
      <template v-else>
        <span class="task-text">{{ t('cost.task.resolved') }}</span>
      </template>
    </div>
    <!-- 统计区间 + 采集入口（审评修正：区间状态存在但没有选择器，采集函数也没有按钮） -->
    <div class="page-toolbar">
      <span class="sub">{{ t('cost.range.label') }}</span>
      <el-radio-group v-model="rangeDays" size="small" @change="changeRange">
        <el-radio-button v-for="r in RANGES" :key="r.value" :value="r.value">{{ t(r.labelKey) }}</el-radio-button>
      </el-radio-group>
      <span class="sub">{{ t('cost.range.note') }}</span>
      <div style="flex: 1" />
      <span class="sub">
        {{ t('cost.status.last_run', { v: status?.last_finished_at ? fmtTime(status.last_finished_at) : t('cost.status.never') }) }}
        <el-tooltip v-if="statusError" :content="statusError" placement="top">
          <el-tag type="warning" size="small" effect="plain">{{ t('cost.status.unknown') }}</el-tag>
        </el-tooltip>
      </span>
      <el-button size="small" :loading="exportingDetail" @click="exportDaily()">
        {{ t('cost.btn.export_detail') }}
      </el-button>
      <el-button v-if="canWrite" size="small" type="primary" :loading="collecting" @click="doCollect">
        {{ t('cost.btn.collect') }}
      </el-button>
    </div>
    <el-tabs v-model="activeTab" @tab-change="onTabChange">
      <el-tab-pane :label="t('cost.tab.overview')" name="overview">
        <el-alert v-if="insightsError" type="error" :closable="false" show-icon :title="insightsError" style="margin-bottom: 10px">
          <el-button size="small" @click="loadInsights">{{ t('common.retry') }}</el-button>
        </el-alert>
        <CostOverview v-bind="{insights, insightsLoading, detailError, detailInstance, detailLoading, detailQuery, detailRows, detailTotal, exportDetail: exportDaily, exportingDetail, fmtDate, fmtMoney, fmtQty, openDetail, askAgent: askCostAgent, agentStarting, canAskAgent: canWrite}"
                      v-model:detailVisible="detailVisible"
                      @update:detailPage="v => { detailQuery.page = v; loadDetail() }" />
      </el-tab-pane>
      <el-tab-pane :label="t('cost.tab.drill')" name="drill">
        <el-alert v-if="drillError" type="error" :closable="false" show-icon :title="drillError" style="margin-bottom: 10px">
          <el-button size="small" @click="loadDrill">{{ t('common.retry') }}</el-button>
        </el-alert>
        <CostDrill v-bind="{amount, available, billing_items, consumers, date, days_left, delta_pct, drill, drillLoading, drillProductClick, drillScenarios, fmtYuan2, instance_id, instances, name, note, packs, pct, pool_pct, prev_total, products, scenario_note, total, unit, why}"
                   v-model:drillProduct="drillProduct" v-model:drillInstances="drillInstances" />
      </el-tab-pane>
      <el-tab-pane :label="`${t('cost.tab.packs')} (${packSummary.total})`" name="packs">
        <el-alert v-if="packsError || adviceError" type="error" :closable="false" show-icon
                  :title="packsError || adviceError" style="margin-bottom: 10px">
          <el-button size="small" @click="loadPacks(); loadAdvice()">{{ t('common.retry') }}</el-button>
        </el-alert>
        <CostPacks v-bind="{ADVISE_TAGS, PACK_STATES, advice, adviceLoading, applicable_products, days_left, deduct_type, detail, effective_time, exhausted, exhausting, expiring, expiry_time, fmtDate, fmtQty, impactTotalDaily, fmtYuan, instance_id, items, level, money, note, packItems, packStateTag, packStateText, packSummary, package_type, packsLoading, packsSnapshot, pool_days_left, pool_packs, pool_remaining_pct, products_label, rate_from_history, rate_pct_per_day, remaining_amount, remaining_pct, remaining_unit, round, scope_label, snapshot_date, state, status, title, total, total_amount, total_unit, type_label}" v-model:packStateFilter="packStateFilter" />
      </el-tab-pane>
      <el-tab-pane name="cdn">
        <template #label>{{ t('cost.tab.cdn') }}<span v-if="cdnData?.domains?.length" class="sub"> ({{ cdnData.domains.length }})</span></template>
        <el-alert v-if="cdnError" type="error" :closable="false" show-icon :title="cdnError" style="margin-bottom: 10px">
          <el-button size="small" @click="loadCdnDomains">{{ t('common.retry') }}</el-button>
        </el-alert>
        <CostCdn v-bind="{CDN_DAYS_OPTIONS, avg_daily_gb, cdnData, cdnLoading, cdnSeries, daily_total, delta_gb, domain, domains, fontWeight, grand_total_gb, last_daily_gb, loadCdnDomains, loadCdnUrls, marginRight, note, peak_day, peak_day_gb, product, requests, share_pct, total_gb, urlData, urlLoading, urls, window_days}"
                 v-model:cdnDays="cdnDays" v-model:urlDomain="urlDomain" />
      </el-tab-pane>
      <el-tab-pane :label="`${t('cost.tab.anomalies')} (${anomaliesTotal})`" name="anomalies">
        <el-alert v-if="anomaliesError" type="error" :closable="false" show-icon :title="anomaliesError" style="margin-bottom: 10px">
          <el-button size="small" @click="loadAnomalies">{{ t('common.retry') }}</el-button>
        </el-alert>
        <CostAnomalies v-bind="{ACK_STATUS, ANOMALY_TYPES: COST_ANOMALY_TYPES, ackAnomaliesBatch, ack_note, ack_status, anomalies, anomaliesError, anomaliesLoading, anomaliesTotal, anomalyQuery, baseline, biz, canWrite, content, detect_date, detecting, doDetect, fmtMoney, instance_id, last_seen, level, loadAnomalies, page, page_size, ratio, related_packs, rows, tag, target_key, title, typeLabel, typeTag, type_label}"
                       v-model:anomalyLevelFilter="anomalyLevelFilter" v-model:anomalyStatusFilter="anomalyStatusFilter" v-model:anomalyTypeFilter="anomalyTypeFilter" v-model:anomalySelection="anomalySelection"
                       @update:anomalyPage="v => { anomalyQuery.page = v }" />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.task-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  margin-bottom: 12px;
  border: 1px solid var(--el-color-warning-light-5);
  border-left: 3px solid var(--el-color-warning);
  border-radius: 6px;
  background: var(--el-color-warning-light-9);
}
.task-bar.done {
  border-color: var(--el-color-success-light-5);
  border-left-color: var(--el-color-success);
  background: var(--el-color-success-light-9);
}
.task-text { flex: 1; font-size: 13px; }
.page-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 8px;
}

.advice-item {
  border: 1px solid var(--el-border-color-lighter);
  border-left: 3px solid var(--el-color-warning);
  border-radius: 6px;
  padding: 8px 12px;
  margin-bottom: 6px;
}

.status-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 14px;
}
.stat-cards {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
  margin-bottom: 14px;
}
.stat-card {
  padding: 14px 16px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: var(--el-bg-color-overlay);
}
.stat-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.stat-value {
  font-size: 24px;
  font-weight: 600;
  margin: 6px 0 2px;
}
.stat-value.danger {
  color: var(--el-color-danger);
}
.stat-value.warn {
  color: var(--el-color-warning);
}
.stat-sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.charts-row {
  display: grid;
  grid-template-columns: 2fr 1fr;
  gap: 12px;
  margin-bottom: 14px;
}
.chart-card {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  padding: 8px;
  background: var(--el-bg-color-overlay);
}
.marks-legend {
  padding: 0 8px 4px;
}
.pie-title {
  font-size: 13px;
  font-weight: 500;
  padding: 6px 8px 0;
}
.empty-chart {
  height: 240px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.product-table {
  margin-bottom: 18px;
}
.section-title {
  font-size: 14px;
  font-weight: 600;
  margin: 6px 0 10px;
}
.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 10px;
}
.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.mono {
  font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 12px;
}
.neg {
  color: var(--el-color-success);
}
.danger {
  color: var(--el-color-danger);
}
.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 10px;
}
.drawer-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
}
.pack-expand {
  padding: 4px 12px;
  font-size: 12px;
  line-height: 1.9;
  color: var(--el-text-color-secondary);
}
.anomaly-content {
  padding: 4px 12px;
  font-size: 12px;
  white-space: pre-wrap;
  line-height: 1.8;
}
</style>
