<script setup>
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'

import {
  acceptBaseline, ackRecord, autoAnalyzeRules, autoCreateRules, clearRecordAck, createRule,
  deleteRule, dryRunRule, evalStatus, listChannels, listRecords, listRules, resolveRecord,
  triggerEval, updateRule,
} from '@/api/alerts'
import http from '@/api/http'
import { exportCsv } from '@/api/download'
import { metricConfig } from '@/api/monitor'
import { slsLogstores, slsProjects } from '@/api/logs'
import CopyBtn from '@/components/CopyBtn.vue'
import { fmtTime, levelLabel } from '@/utils/format'
import { useAuthStore } from '@/stores/auth'
import { t } from '@/i18n'

const auth = useAuthStore()
const canWrite = computed(() => ['admin', 'operator'].includes(auth.role))
const router = useRouter()
const route = useRoute()

// 认领告警：有人在管了，升级链停止。与下方「确认/忽略」（静默重复提醒）语义不同。
async function ackRow(row) {
  const r = await http.post(`/oncall/ack/${row.id}`, { note: '' })
  // 后端 already=true 说明此前已认领；不能仍报「已认领」让用户以为这次生效
  ElMessage.success(t(r?.already ? 'alerts.msg.acked_already' : 'alerts.msg.acked'))
  row.claimed = true
  if (alertDetail.value?.id === row.id) alertDetail.value.claimed = true
  loadRecords()
}

// 确认/忽略：问题我看到了，按账号优先级静默后续重复提醒；
// 不是认领，不影响值班升级链。取消静默恢复原提醒节奏。
async function recordAck(row, action) {
  let note = ''
  try {
    const r = await ElMessageBox.prompt(
      t(action === 'ignore' ? 'alerts.ack.prompt_ignore' : 'alerts.ack.prompt_ack'),
      t('alerts.ack.prompt_title'),
      { inputPlaceholder: t('alerts.ack.note_ph'), confirmButtonText: t('alerts.ack.confirm_btn'), cancelButtonText: t('alerts.ack.cancel') },
    )
    note = r.value || ''
  } catch { return }
  try {
    await ackRecord(row.id, { action, note })
    ElMessage.success(action === 'ignore' ? t('alerts.ack.msg_ignored') : t('alerts.ack.msg_acknowledged'))
    loadRecords()
  } catch { /* 拦截器提示 */ }
}

async function recordUnack(row) {
  try { await clearRecordAck(row.id) } catch { return }
  ElMessage.success(t('alerts.ack.msg_cleared'))
  loadRecords()
}

const ACK_TAG = { acknowledged: 'warning', ignored: 'info' }
function ackLabel(row) {
  if (!row.ack_status || row.ack_status === 'none') return ''
  const base = row.ack_status === 'ignored' ? t('alerts.ack.status_ignored') : t('alerts.ack.status_acknowledged')
  if (row.next_notify_at) return t('alerts.ack.until', { label: base, at: fmtTime(row.next_notify_at) })
  return t('alerts.ack.forever', { label: base })
}

// 告警联动拓扑：定位到图中节点（按 target/名称搜索高亮）
function toTopology(row) {
  const focus = (row.target_name || row.target || row.title || '').slice(0, 100)
  router.push({ path: '/topology', query: focus ? { focus } : {} })
}

// 一键诊断：把告警拼成问题带去诊断页（Agent 只读取证）
function diagFrom(row) {
  const parts = [
    t('alerts.diag.head', {
      name: row.title || row.rule_name,
      state: row.status === 'firing' ? t('alerts.diag.state_firing') : t('alerts.diag.state_ever'),
    }),
    t('alerts.diag.target', { target: row.target_name || row.target }),
  ]
  if (row.breach_value != null) parts.push(t('alerts.diag.breach', { v: row.breach_value }))
  if (row.content) parts.push(t('alerts.diag.content', { c: row.content.slice(0, 150) }))
  parts.push(t('alerts.diag.tail'))
  router.push({ path: '/diagnose', query: { q: parts.join('').slice(0, 500) } })
}

// 默认落到「告警记录」：运维打开告警页要看的是"现在出了什么事"，
// 规则配置是低频维护项；?tab=rules 仍可直达规则
const tab = ref(route.query.tab === 'rules' ? 'rules' : 'records')

/* ---------------- 评估状态 ---------------- */
const status = ref(null)
const evaluating = ref(false)

async function loadStatus() {
  try {
    status.value = await evalStatus()
  } catch {
    status.value = null
  }
}

async function doEval() {
  evaluating.value = true
  try {
    const r = await triggerEval()
    ElMessage.success(t('alerts.eval.ok', {
      fired: r.fired,
      recovered: r.recovered,
      err_part: r.errors?.length ? t('alerts.eval.err_part', { n: r.errors.length }) : '',
    }))
    loadStatus()
    loadRecords()
  } finally {
    evaluating.value = false
  }
}

/* ---------------- 规则 ---------------- */
const rules = ref([])
const rulesLoading = ref(false)
const ruleDialog = ref(false)
const editingRule = ref(null)
const savingRule = ref(false)
const ruleForm = reactive({
  name: '', source: 'metric', level: 'warning',
  product: 'ecs', metric: '',
  project: '', logstore: '', query_expr: '',
  comparator: '>', threshold: 80, duration: 300,
  channel_ids: [], silences: '', enabled: true,
  account_id: 0,   // 0=全部账号；>0=绑定账号
})

// 账号维度：0=全部账号（评估不过滤），>0=只评估该账号资产。
// account_id 指向 cloud_accounts（资产/自动规则都按云账号），
// /settings/accounts 是数据归属分组（另一套 id 空间），用它会导致编辑时显示错账号。
const ruleAccounts = ref([])
async function loadRuleAccounts() {
  try {
    ruleAccounts.value = (await http.get('/clouds/accounts'))?.items || []
  } catch { ruleAccounts.value = [] }
}

const channels = ref([])
const metricOptions = ref([])
// 产品下拉来自交付可选指标集（含 VMware 原生指标），不在前端硬编码
const productOptions = ref([
  { value: 'ecs', label: 'ECS' }, { value: 'rds', label: 'RDS' },
  { value: 'redis', label: 'Redis' }, { value: 'slb', label: 'SLB' },
])
const PRODUCT_LABELS = {
  vmware_vm: () => t('alerts.product.vmware_vm'),
  vmware_host: () => t('alerts.product.vmware_host'),
}
const productLabel = (p) => {
  const f = PRODUCT_LABELS[p]
  return f ? f() : p.toUpperCase()
}
const projects = ref([])
const logstores = ref([])
const loadingLogstores = ref(false)

const DURATIONS = [
  { value: 60, label: 'alerts.duration.1m' },
  { value: 300, label: 'alerts.duration.5m' },
  { value: 600, label: 'alerts.duration.10m' },
  { value: 1800, label: 'alerts.duration.30m' },
  { value: 3600, label: 'alerts.duration.1h' },
]

const rulesError = ref('')      // 规则加载失败 ≠ 没有规则
const recordsError = ref('')    // 记录加载失败 ≠ 没有告警
const LEVEL_TAGS = { critical: 'danger', warning: 'warning', info: 'info' }
const SOURCE_LABELS = { metric: 'alerts.source.metric', sls: 'alerts.source.sls' }

// 全量指标标签映射（规则条件中文化用，"ecs.CPUUtilization" -> "CPU使用率"）
const METRIC_LABELS = {}

const ruleDefText = (r) => {
  if (r.source !== 'metric') return `${r.project}/${r.logstore} ${r.comparator} ${r.threshold}`
  const m = METRIC_LABELS[`${r.product}.${r.metric}`]
  const unit = m?.unit === '%' ? '%' : ''
  return `${m?.label || r.metric} ${r.comparator} ${r.threshold}${unit}${r.duration ? t('alerts.rule.duration_part', { n: Math.round(r.duration / 60) }) : ''}`
}

async function loadRules() {
  rulesLoading.value = true
  rulesError.value = ''
  try {
    rules.value = await listRules()
  } catch (e) {
    rules.value = []
    rulesError.value = e?.response?.data?.detail || t('alerts.rules.load_fail')
  } finally {
    rulesLoading.value = false
  }
}

async function loadMetricLabels() {
  try {
    const all = await metricConfig('')
    const opts = []
    for (const [product, metrics] of Object.entries(all)) {
      opts.push({ value: product, label: productLabel(product) })
      for (const m of metrics) METRIC_LABELS[`${product}.${m.metric}`] = { label: m.label, unit: m.unit }
    }
    if (opts.length) productOptions.value = opts
  } catch { /* 标签不可用时降级显示原始指标名 */ }
}

async function loadChannels() {
  try {
    channels.value = canWrite.value ? await listChannels() : []
  } catch {
    channels.value = []
  }
}

async function loadMetricOptions() {
  try {
    const data = await metricConfig(ruleForm.product)
    metricOptions.value = (data[ruleForm.product] || []).map((m) => ({ value: m.metric, label: m.label }))
    if (metricOptions.value.length && !metricOptions.value.some((m) => m.value === ruleForm.metric)) {
      ruleForm.metric = metricOptions.value[0].value
    }
  } catch {
    metricOptions.value = []
  }
}

async function loadSlsOptions(loadLogstoresToo) {
  if (!projects.value.length) {
    try {
      const data = await slsProjects()
      projects.value = data.projects || []
      if (!ruleForm.project) {
        // 产品默认态不内置客户 logstore 名，也不猜：只有一个候选时才自动选，
        // 多 project 环境留空由用户明确选择（猜错会把规则建到错误日志库）。
        ruleForm.project = projects.value.length === 1 ? projects.value[0] : ''
      }
    } catch {
      projects.value = []
    }
  }
  if (loadLogstoresToo !== false && ruleForm.project) {
    loadingLogstores.value = true
    try {
      const data = await slsLogstores(ruleForm.project)
      logstores.value = data.logstores || []
      if (!logstores.value.includes(ruleForm.logstore)) {
        ruleForm.logstore = logstores.value.find((l) => l.includes('slb')) || logstores.value[0] || ''
      }
    } finally {
      loadingLogstores.value = false
    }
  }
}

function openRuleDialog(row) {
  editingRule.value = row
  Object.assign(ruleForm, {
    name: row?.name || '',
    source: row?.source || 'metric',
    level: row?.level || 'warning',
    product: row?.product || 'ecs',
    metric: row?.metric || '',
    project: row?.project || '',
    logstore: row?.logstore || '',
    query_expr: row?.query_expr || '',
    comparator: row?.comparator || '>',
    threshold: row?.threshold ?? 80,
    duration: row?.duration || 300,
    channel_ids: row ? [...(row.channel_ids || [])] : [],
    silences: (row?.silences || []).join(', '),
    enabled: row?.enabled ?? true,
    account_id: row?.account_id || 0,
  })
  dryResult.value = null
  if (ruleForm.source === 'metric') loadMetricOptions()
  else { projects.value = []; loadSlsOptions() }
  ruleDialog.value = true
}

// 表单 → 规则载荷（保存与试算共用一份字段拼装，防止两边口径漂移）
function buildRuleData() {
  const data = {
    name: ruleForm.name, source: ruleForm.source, level: ruleForm.level,
    comparator: ruleForm.comparator, threshold: Number(ruleForm.threshold),
    duration: ruleForm.duration,
    silences: ruleForm.silences.split(/[,，\s]+/).filter(Boolean),
    account_id: Number(ruleForm.account_id) || 0,   // 账号维度（试算同口径）
  }
  if (ruleForm.source === 'metric') {
    data.product = ruleForm.product
    data.metric = ruleForm.metric
  } else {
    data.project = ruleForm.project
    data.logstore = ruleForm.logstore
    data.query_expr = ruleForm.query_expr
  }
  return data
}

/* ---------------- 规则试算：保存前历史回放，调阈值不盲拍 ---------------- */
const dryRunning = ref(false)
const dryResult = ref(null)
const dryRunDays = ref(7)

async function doDryRun() {
  if (ruleForm.source === 'metric' && !ruleForm.metric) return ElMessage.warning(t('alerts.dry.select_metric'))
  if (ruleForm.source === 'sls' && (!ruleForm.project || !ruleForm.logstore)) return ElMessage.warning(t('alerts.dry.select_logstore'))
  dryRunning.value = true
  try {
    dryResult.value = await dryRunRule({ rule_draft: buildRuleData(), days: dryRunDays.value })
    if (!dryResult.value.count) ElMessage.info(t('alerts.dry.no_trigger', { n: dryRunDays.value }))
  } catch {
    /* http 层已提示 */
  } finally {
    dryRunning.value = false
  }
}

async function saveRule() {
  if (!ruleForm.name) return ElMessage.warning(t('alerts.rule.name_required'))
  if (ruleForm.source === 'metric' && !ruleForm.metric) return ElMessage.warning(t('alerts.dry.select_metric'))
  savingRule.value = true
  try {
    const data = {
      ...buildRuleData(),
      channel_ids: ruleForm.channel_ids,
      enabled: ruleForm.enabled,
    }
    if (editingRule.value) await updateRule(editingRule.value.id, data)
    else await createRule(data)
    ElMessage.success(t('alerts.msg.saved'))
    ruleDialog.value = false
    loadRules()
  } catch {
    /* http 层已提示 */
  } finally {
    savingRule.value = false
  }
}

async function toggleRule(row) {
  await updateRule(row.id, { enabled: !row.enabled })
  ElMessage.success(!row.enabled ? t('alerts.msg.enabled') : t('alerts.msg.disabled'))
  loadRules()
}

async function removeRule(row) {
  await ElMessageBox.confirm(
    t('alerts.remove.confirm', { name: row.name }),
    t('alerts.remove.title'),
    { type: 'warning', confirmButtonText: t('alerts.remove.confirm_btn'), cancelButtonText: t('alerts.action.cancel') },
  )
  await deleteRule(row.id)
  ElMessage.success(t('alerts.msg.deleted'))
  loadRules()
}

/* ---------------- 自动创建规则 ----------------
 * 平台自己盘点：按账号×产品找"有资产、有指标通道、没有规则"的缺口，再读近 N 天
 * 真实样本算 P95 给阈值建议。分析只读；用户勾选后只传候选 key，服务端重新分析
 * 再建，阈值不来自前端也不来自 LLM。默认勾选"有数据支撑"的候选，一次点击即可。 */
const autoDialog = ref(false)
const autoLoading = ref(false)
const autoCreating = ref(false)
const autoReport = ref(null)
const autoSelected = ref([])
const autoDays = ref(7)
const autoAccountId = ref(0)
const autoTable = ref(null)

async function openAutoDialog() {
  autoDialog.value = true
  autoSelected.value = []
  await runAutoAnalyze()
}

async function runAutoAnalyze() {
  autoLoading.value = true
  autoSelected.value = []
  try {
    autoReport.value = await autoAnalyzeRules({ account_id: autoAccountId.value, days: autoDays.value })
    await nextTick()
    // 默认勾选有真实样本支撑的候选；无样本的只作提示，避免一键建出一批拍脑袋阈值
    for (const c of autoReport.value?.candidates || []) {
      if (c.data_backed) autoTable.value?.toggleRowSelection(c, true)
    }
  } catch {
    autoReport.value = null
  } finally {
    autoLoading.value = false
  }
}

async function createAutoRules() {
  if (!autoSelected.value.length) return ElMessage.warning(t('alerts.auto.none_selected'))
  autoCreating.value = true
  try {
    const r = await autoCreateRules(autoSelected.value.map((c) => c.key))
    if (r.created?.length) ElMessage.success(t('alerts.auto.created', { n: r.created.length }))
    if (r.skipped?.length) ElMessage.warning(t('alerts.auto.skipped', { n: r.skipped.length }))
    autoDialog.value = false
    loadRules()
    loadStatus()
  } catch {
    /* http 层已提示 */
  } finally {
    autoCreating.value = false
  }
}

const autoCond = (c) => t('alerts.auto.cond', {
  comparator: c.comparator, threshold: c.threshold, unit: c.unit === '%' ? '%' : '',
  mins: Math.round((c.duration || 300) / 60),
})

/* ---------------- 记录 ---------------- */
// ---- 告警详情（操作收进详情防误触；记录行点详情看全貌）----
const alertDetail = ref(null)
const alertDetailVisible = ref(false)

function openAlertDetail(row) {
  alertDetail.value = row
  alertDetailVisible.value = true
}

function alertOp(cmd, row) {
  if (cmd === 'ack') return ackRow(row)
  if (cmd === 'acknowledge') return recordAck(row, 'acknowledge')
  if (cmd === 'ignore') return recordAck(row, 'ignore')
  if (cmd === 'unack') return recordUnack(row)
  if (cmd === 'topo') return toTopology(row)
  if (cmd === 'diag') return diagFrom(row)
  if (cmd === 'baseline') return acceptBaselineFor(row)
  if (cmd === 'resolve') return resolveRecordFor(row)
  if (cmd === 'metrics') return viewMetrics(row)
}

const records = ref([])
const recordsTotal = ref(0)
const recordsLoading = ref(false)
const recordFilter = reactive({ status: '', keyword: '', page: 1, page_size: 20 })
const exportingRecords = ref(false)

// 告警记录 CSV：沿用当前状态/关键字筛选，不受分页限制
async function exportRecords() {
  exportingRecords.value = true
  try {
    const params = {}
    if (recordFilter.status) params.status = recordFilter.status
    if (recordFilter.keyword) params.keyword = recordFilter.keyword
    await exportCsv('/alerts/records/export', params, 'alert-records.csv')
  } finally {
    exportingRecords.value = false
  }
}

// 告警 → 监控指标：告警说"CPU 高"，点一下就能看到曲线，
// 不用自己去监控页再按实例名找（原生 instance_id 在记录里本来就有）
const METRIC_PRODUCTS = ['ecs', 'rds', 'redis', 'slb', 'vmware_vm', 'vmware_host']
function canViewMetrics(row) {
  return METRIC_PRODUCTS.includes(row.product) && !!row.target
}
function viewMetrics(row) {
  router.push({ path: '/monitor', query: { product: row.product, instance: row.target } })
}

function clearKeyword() {
  recordFilter.keyword = ''
  recordFilter.page = 1
  loadRecords()
}

async function loadRecords() {
  recordsLoading.value = true
  recordsError.value = ''
  try {
    const params = { page: recordFilter.page, page_size: recordFilter.page_size }
    if (recordFilter.status) params.status = recordFilter.status
    if (recordFilter.keyword) params.keyword = recordFilter.keyword
    const data = await listRecords(params)
    recordsTotal.value = data.total
    records.value = data.items
  } catch (e) {
    // 失败不能落成「暂无告警 + 去接信号源」，那是把查询故障说成没数据
    records.value = []
    recordsTotal.value = 0
    recordsError.value = e?.response?.data?.detail || t('alerts.records.load_fail')
  } finally {
    recordsLoading.value = false
  }
}


// 设为常态："这台机器 95% 是正常的"——记为水位，之后只报显著偏离
// 手动标记已恢复：处置完成后主动收口
async function resolveRecordFor(row) {
  try {
    await resolveRecord(row.id)
    ElMessage.success(t('alerts.msg.resolved'))
    loadRecords()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('alerts.msg.op_fail'))
  }
}

async function acceptBaselineFor(row) {
  try {
    await ElMessageBox.confirm(
      t('alerts.baseline.confirm', { target: row.target_name || row.target, rule: row.rule_name }),
      t('alerts.baseline.title'), {
        type: 'info',
        confirmButtonText: t('alerts.baseline.confirm_btn'),
        cancelButtonText: t('alerts.action.cancel'),
      })
  } catch { return }
  try {
    const r = await acceptBaseline({ rule_id: row.rule_id, target: row.target })
    ElMessage.success(t('alerts.baseline.done', { baseline: r.baseline, threshold: r.threshold_override }))
    loadRecords()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('alerts.baseline.fail'))
  }
}

onMounted(() => {
  if (route.query.keyword) { recordFilter.keyword = String(route.query.keyword) }
  loadStatus()
  loadRules()
  loadChannels()
  loadRuleAccounts()
  loadRecords()
  loadMetricLabels()
})

// 站内信可在告警页已打开时再次带 query 跳转，不能只认首次挂载。
watch(() => route.fullPath, () => {
  tab.value = route.query.tab === 'rules' ? 'rules' : 'records'
  recordFilter.keyword = String(route.query.keyword || '')
  loadRecords()
})
</script>

<template>
  <div class="page">
    <div class="status-bar">
      <template v-if="status">
        <span class="sub">
          <span class="ev-seg">{{ t('alerts.status.last_eval') }}{{ status.last_run_at ? fmtTime(status.last_run_at) : t('alerts.status.never') }}</span>
          <template v-if="status.last_run_at">
            <span class="ev-seg">{{ t('alerts.status.duration', { ms: status.duration_ms }) }}</span>
            <span class="ev-seg">{{ t('alerts.status.rules', { n: status.rules }) }}</span>
            <span class="ev-seg">{{ t('alerts.status.fired_recovered', { fired: status.fired, recovered: status.recovered }) }}</span>
          </template>
        </span>
        <el-tag v-if="status.errors?.length" type="danger" size="small">
          {{ t('alerts.status.eval_errors', { n: status.errors.length }) }}
        </el-tag>
      </template>
      <span v-else class="sub">{{ t('alerts.status.unknown') }}</span>
      <div style="flex: 1" />
      <el-button type="primary" :loading="evaluating" :disabled="!canWrite" @click="doEval">{{ t('alerts.status.eval_now') }}</el-button>
    </div>

    <el-tabs v-model="tab">
      <el-tab-pane :label="t('alerts.tabs.rules')" name="rules">
        <div class="toolbar">
          <el-button type="primary" :disabled="!canWrite" @click="openRuleDialog(null)">{{ t('alerts.rules.new') }}</el-button>
          <el-button type="success" plain @click="openAutoDialog">{{ t('alerts.auto.button') }}</el-button>
          <span class="sub">{{ t('alerts.rules.hint') }}</span>
        </div>
        <el-alert v-if="rulesError" type="error" :closable="false" show-icon class="load-error"
                  :title="rulesError">
          <el-button size="small" @click="loadRules">{{ t('common.retry') }}</el-button>
        </el-alert>
        <el-table stripe :data="rules" v-loading="rulesLoading" border
                  :empty-text="rulesError ? rulesError : t('alerts.rules.empty')">
          <el-table-column :label="t('alerts.rules.col_name')" min-width="200">
            <template #default="{ row }">
              <span>{{ row.name }}</span>
              <!-- 规则来源：auto=接入账号时自动播种；与人工规则同权可编辑 -->
              <el-tag v-if="row.origin === 'auto'" size="small" type="success" effect="plain" class="auto-tag">
                {{ t('alerts.rules.auto_tag') }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column :label="t('alerts.rules.col_account')" width="110">
            <template #default="{ row }">
              <el-tag v-if="row.account_id" size="small" type="info">{{ row.account_name || t('alerts.rules.account_bound') }}</el-tag>
              <span v-else class="sub">{{ t('alerts.rules.account_all') }}</span>
            </template>
          </el-table-column>
          <el-table-column :label="t('alerts.rules.col_source')" width="100">
            <template #default="{ row }">{{ t(SOURCE_LABELS[row.source] || row.source) }}</template>
          </el-table-column>
          <el-table-column :label="t('alerts.rules.col_level')" width="90">
            <template #default="{ row }">
              <el-tag :type="LEVEL_TAGS[row.level]" size="small">{{ levelLabel(row.level) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column :label="t('alerts.rules.col_condition')" min-width="280">
            <template #default="{ row }">
              <span class="mono">{{ ruleDefText(row) }}</span>
              <div v-if="row.source === 'sls' && row.query_expr" class="sub mono">{{ row.query_expr }}</div>
            </template>
          </el-table-column>
          <el-table-column :label="t('alerts.rules.col_channels')" width="150">
            <template #default="{ row }">
              <template v-if="channels.length">
                {{ channels.filter(c => row.channel_ids.includes(c.id)).map(c => c.name).join(t('alerts.list.sep')) || t('alerts.channels.inapp') }}
              </template>
              <span v-else-if="row.channel_ids.length" class="sub">{{ t('alerts.channels.count', { n: row.channel_ids.length }) }}</span>
              <span v-else class="sub">{{ t('alerts.channels.inapp') }}</span>
            </template>
          </el-table-column>
          <el-table-column :label="t('alerts.rules.col_silence')" width="90">
            <template #default="{ row }">
              <el-tag v-if="row.in_silence" type="info" size="small">{{ t('alerts.silence.on') }}</el-tag>
              <span v-else-if="row.silences.length" class="sub">{{ t('alerts.silence.windows', { n: row.silences.length }) }}</span>
              <span v-else class="sub">-</span>
            </template>
          </el-table-column>
          <el-table-column :label="t('alerts.rules.col_enabled')" width="80">
            <template #default="{ row }">
              <el-switch :model-value="row.enabled" :disabled="!canWrite" @change="toggleRule(row)" />
            </template>
          </el-table-column>
          <el-table-column :label="t('alerts.rules.col_actions')" width="140" fixed="right">
            <template #default="{ row }">
              <el-button size="small" :disabled="!canWrite" @click="openRuleDialog(row)">{{ t('alerts.action.edit') }}</el-button>
              <el-button size="small" type="danger" :disabled="!canWrite" @click="removeRule(row)">{{ t('alerts.action.delete') }}</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane :label="t('alerts.tabs.records')" name="records">
        <div class="toolbar">
          <el-radio-group v-model="recordFilter.status" @change="() => { recordFilter.page = 1; loadRecords() }">
            <el-radio-button value="">{{ t('alerts.filter.all') }}</el-radio-button>
            <el-radio-button value="firing">{{ t('alerts.status.firing') }}</el-radio-button>
            <el-radio-button value="recovered">{{ t('alerts.status.recovered') }}</el-radio-button>
          </el-radio-group>
          <!-- 深链 ?keyword= 过滤必须可见可清，避免用户不知道正在查看筛选结果 -->
          <el-tag v-if="recordFilter.keyword" closable type="warning" size="small" @close="clearKeyword">
            {{ t('alerts.filter.keyword_chip', { kw: recordFilter.keyword }) }}
          </el-tag>
          <span class="sub">{{ t('alerts.records.total_n', { n: recordsTotal }) }}</span>
          <div style="flex: 1" />
          <el-button size="small" :loading="exportingRecords" @click="exportRecords">
            {{ t('alerts.records.export') }}
          </el-button>
        </div>
        <el-alert v-if="recordsError" type="error" :closable="false" show-icon class="load-error"
                  :title="recordsError">
          <el-button size="small" @click="loadRecords">{{ t('common.retry') }}</el-button>
        </el-alert>
        <el-table stripe :data="records" v-loading="recordsLoading" border
                  :empty-text="recordsError ? recordsError : t('alerts.records.empty')">
          <el-table-column :label="t('alerts.records.col_status')" width="90">
            <template #default="{ row }">
              <el-tag :type="row.status === 'firing' ? 'danger' : 'success'" size="small">
                {{ row.status === 'firing' ? t('alerts.status.firing') : t('alerts.status.recovered') }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column :label="t('alerts.records.col_level')" width="90">
            <template #default="{ row }">
              <el-tag :type="LEVEL_TAGS[row.level]" size="small">{{ levelLabel(row.level) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="rule_name" :label="t('alerts.records.col_rule')" min-width="180">
            <template #default="{ row }">
              {{ row.rule_name }}
              <el-tooltip :content="row.content" placement="top" :show-after="300">
                <div class="sub clamp2">{{ row.content }}</div>
              </el-tooltip>
            </template>
          </el-table-column>
          <el-table-column :label="t('alerts.records.col_target')" min-width="180">
            <template #default="{ row }">
              <el-tag v-for="b in row.biz" :key="b" type="warning" size="small" effect="dark" style="margin-right: 4px">{{ b }}</el-tag>
              <!-- 归属标识：多账号看得出属于谁 -->
              <el-tag v-if="row.account_name" type="info" size="small" effect="plain" style="margin-right: 4px">{{ row.account_name }}</el-tag>
              {{ row.target_name }}
              <div class="sub mono">{{ row.target }}<CopyBtn :text="row.target" /></div>
            </template>
          </el-table-column>
          <el-table-column :label="t('alerts.records.col_breach')" width="90">
            <template #default="{ row }">{{ row.breach_value != null ? Number(row.breach_value).toLocaleString('zh-CN', { maximumFractionDigits: 2 }) : '-' }}</template>
          </el-table-column>
          <el-table-column :label="t('alerts.records.col_fired_at')" width="160">
            <template #default="{ row }">{{ fmtTime(row.fired_at) }}</template>
          </el-table-column>
          <el-table-column :label="t('alerts.records.col_recovered_at')" width="160">
            <template #default="{ row }">{{ fmtTime(row.recovered_at) }}</template>
          </el-table-column>
          <el-table-column :label="t('alerts.records.col_notified')" width="140">
            <template #default="{ row }">
              <el-tag v-if="row.silenced" type="info" size="small">{{ t('alerts.notify.silenced') }}</el-tag>
              <span v-else>{{ row.notified_channels.join(t('alerts.list.sep')) || t('alerts.channels.inapp') }}</span>
            </template>
          </el-table-column>
          <!-- 确认/忽略：与值班认领不同，静默的是重复提醒，不是升级链 -->
          <el-table-column :label="t('alerts.records.col_ack')" width="150">
            <template #default="{ row }">
              <el-tag v-if="row.ack_status && row.ack_status !== 'none'" size="small"
                      :type="ACK_TAG[row.ack_status] || 'info'">
                {{ ackLabel(row) }}
              </el-tag>
              <span v-else class="sub">—</span>
            </template>
          </el-table-column>
          <el-table-column :label="t('alerts.records.col_actions')" width="215" fixed="right">
            <template #default="{ row }">
              <el-button size="small" text type="primary" @click="openAlertDetail(row)">{{ t('alerts.action.detail') }}</el-button>
              <el-button size="small" text type="primary" @click="diagFrom(row)">{{ t('alerts.action.diagnose') }}</el-button>
              <el-dropdown trigger="click" @command="(cmd) => alertOp(cmd, row)">
                <el-button size="small" text type="primary">{{ t('alerts.action.more') }}<el-icon class="more-ic"><ArrowDown /></el-icon></el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item v-if="canViewMetrics(row)" command="metrics">{{ t('alerts.action.view_metrics') }}</el-dropdown-item>
                    <el-dropdown-item v-if="canWrite && !row.claimed && row.level === 'critical' && row.status === 'firing'" command="ack">{{ t('alerts.action.ack') }}</el-dropdown-item>
                    <el-dropdown-item v-if="canWrite && row.status === 'firing'" command="acknowledge">{{ t('alerts.ack.action_acknowledge') }}</el-dropdown-item>
                    <el-dropdown-item v-if="canWrite && row.status === 'firing'" command="ignore">{{ t('alerts.ack.action_ignore') }}</el-dropdown-item>
                    <el-dropdown-item v-if="canWrite && row.ack_status && row.ack_status !== 'none'" command="unack">{{ t('alerts.ack.action_clear') }}</el-dropdown-item>
                    <el-dropdown-item command="topo">{{ t('alerts.action.topology') }}</el-dropdown-item>
                    <el-dropdown-item v-if="canWrite && row.source === 'metric' && row.status === 'firing'" command="baseline">{{ t('alerts.baseline.title') }}</el-dropdown-item>
                    <el-dropdown-item v-if="canWrite && row.status === 'firing'" command="resolve" class="danger-item">{{ t('alerts.action.mark_recovered') }}</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </template>
          </el-table-column>
        </el-table>
        <el-drawer v-model="alertDetailVisible" size="480px" :title="t('alerts.detail.title', { rule: alertDetail?.rule_name || '' })">
          <div v-if="alertDetail" class="ad-wrap">
            <div class="ad-line"><span class="ad-k">{{ t('alerts.detail.status') }}</span>
              <el-tag size="small" :type="alertDetail.status === 'firing' ? 'danger' : 'success'">
                {{ alertDetail.status === 'firing' ? t('alerts.status.firing') : t('alerts.status.recovered') }}</el-tag>
              <el-tag size="small" :type="LEVEL_TAGS[alertDetail.level]" style="margin-left: 8px">{{ levelLabel(alertDetail.level) }}</el-tag>
            </div>
            <div class="ad-line"><span class="ad-k">{{ t('alerts.detail.target') }}</span>{{ alertDetail.target_name }}
              <div class="sub mono">{{ alertDetail.target }}</div></div>
            <div class="ad-line"><span class="ad-k">{{ t('alerts.detail.breach_value') }}</span>{{ alertDetail.breach_value ?? '-' }}</div>
            <div class="ad-line"><span class="ad-k">{{ t('alerts.detail.fired_recovered') }}</span>{{ fmtTime(alertDetail.fired_at) }} / {{ fmtTime(alertDetail.recovered_at) || '—' }}</div>
            <div class="ad-line"><span class="ad-k">{{ t('alerts.detail.notified') }}</span>{{ alertDetail.silenced ? t('alerts.notify.silenced') : (alertDetail.notified_channels || []).join(t('alerts.list.sep')) || t('alerts.channels.inapp') }}</div>
            <div class="ad-line" v-if="alertDetail.ack_status && alertDetail.ack_status !== 'none'">
              <span class="ad-k">{{ t('alerts.detail.ack') }}</span>{{ ackLabel(alertDetail) }}
              <span v-if="alertDetail.ack_note" class="sub">（{{ alertDetail.ack_note }}）</span>
            </div>
            <div class="ad-line" v-if="alertDetail.claimed">
              <span class="ad-k">{{ t('alerts.detail.claimed') }}</span>{{ t('alerts.detail.claimed_yes') }}
            </div>
            <div class="ad-line" v-if="alertDetail.silence_reason"><span class="ad-k">{{ t('alerts.detail.silence_reason') }}</span>{{ alertDetail.silence_reason }}</div>
            <div class="ad-block">
              <div class="ad-k">{{ t('alerts.detail.content') }}</div>
              <div class="ad-content">{{ alertDetail.content }}</div>
            </div>
            <div class="ad-ops" v-if="canWrite">
              <el-button v-if="!alertDetail.claimed && alertDetail.level === 'critical' && alertDetail.status === 'firing'" size="small" @click="alertOp('ack', alertDetail)">{{ t('alerts.action.claim') }}</el-button>
              <el-button v-if="alertDetail.status === 'firing'" size="small" @click="alertOp('acknowledge', alertDetail)">{{ t('alerts.ack.action_acknowledge') }}</el-button>
              <el-button v-if="alertDetail.status === 'firing'" size="small" @click="alertOp('ignore', alertDetail)">{{ t('alerts.ack.action_ignore') }}</el-button>
              <el-button v-if="alertDetail.ack_status && alertDetail.ack_status !== 'none'" size="small" @click="alertOp('unack', alertDetail)">{{ t('alerts.ack.action_clear') }}</el-button>
              <el-button size="small" @click="alertOp('diag', alertDetail)">{{ t('alerts.action.one_click_diag') }}</el-button>
              <el-button v-if="alertDetail.source === 'metric' && alertDetail.status === 'firing'" size="small" @click="alertOp('baseline', alertDetail)">{{ t('alerts.baseline.title') }}</el-button>
              <el-button v-if="alertDetail.status === 'firing'" size="small" type="danger" plain @click="alertOp('resolve', alertDetail)">{{ t('alerts.action.mark_recovered') }}</el-button>
            </div>
          </div>
        </el-drawer>
        <div class="pager">
          <el-pagination
            v-model:current-page="recordFilter.page"
            :page-size="recordFilter.page_size"
            :total="recordsTotal"
            layout="total, prev, pager, next"
            background
            @current-change="loadRecords"
          />
        </div>
      </el-tab-pane>
    </el-tabs>

    <!-- 自动创建规则：平台分析缺口 + 真实数据建议阈值，勾选后创建 -->
    <el-dialog v-model="autoDialog" :title="t('alerts.auto.title')" width="980px" top="5vh">
      <p class="sub" style="margin: 0 0 12px">{{ t('alerts.auto.intro') }}</p>
      <div class="toolbar" style="gap: 8px">
        <el-select v-model="autoAccountId" style="width: 180px" @change="runAutoAnalyze">
          <el-option :label="t('alerts.rules.account_all')" :value="0" />
          <el-option v-for="a in ruleAccounts" :key="a.id" :label="a.name" :value="a.id" />
        </el-select>
        <el-select v-model="autoDays" style="width: 130px" @change="runAutoAnalyze">
          <el-option v-for="d in [3, 7, 14, 30]" :key="d" :label="t('alerts.auto.days_opt', { d })" :value="d" />
        </el-select>
        <el-button :loading="autoLoading" @click="runAutoAnalyze">{{ t('alerts.auto.recheck') }}</el-button>
        <span v-if="autoReport" class="sub">
          {{ t('alerts.auto.summary', {
            assets: autoReport.summary.assets,
            metric: autoReport.summary.metric_assets,
            covered: autoReport.summary.covered_assets,
            candidates: autoReport.summary.candidates,
            backed: autoReport.summary.data_backed,
          }) }}
        </span>
      </div>

      <el-alert v-for="f in autoReport?.findings || []" :key="f" type="info" :closable="false"
                :title="f" style="margin-bottom: 8px" />

      <el-table ref="autoTable" :data="autoReport?.candidates || []" v-loading="autoLoading"
                row-key="key" border max-height="380" :empty-text="t('alerts.auto.empty')"
                @selection-change="(rows) => (autoSelected = rows)">
        <el-table-column type="selection" width="46" />
        <el-table-column :label="t('alerts.rules.col_account')" width="120">
          <template #default="{ row }">
            <el-tag v-if="row.account_id" size="small" type="info">{{ row.account_name }}</el-tag>
            <span v-else class="sub">{{ t('alerts.rules.account_all') }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('alerts.auto.col_rule')" min-width="190">
          <template #default="{ row }">
            {{ row.product_label }} · {{ row.metric_label }}
            <div class="sub">{{ t('alerts.auto.assets', { n: row.asset_count }) }}</div>
          </template>
        </el-table-column>
        <el-table-column :label="t('alerts.auto.col_cond')" width="190">
          <template #default="{ row }">{{ autoCond(row) }}</template>
        </el-table-column>
        <el-table-column :label="t('alerts.auto.col_basis')" min-width="340">
          <template #default="{ row }">
            <div>{{ row.reason }}</div>
            <el-tag v-if="row.data_backed" size="small" type="success" effect="plain">{{ t('alerts.auto.backed') }}</el-tag>
            <el-tag v-else size="small" type="info" effect="plain">{{ t('alerts.auto.generic') }}</el-tag>
            <el-tag v-if="row.risk === 'high_baseline'" size="small" type="warning" effect="plain" style="margin-left: 6px">
              {{ t('alerts.auto.high_baseline') }}
            </el-tag>
            <el-tag v-else-if="row.risk === 'near_baseline'" size="small" type="warning" effect="plain" style="margin-left: 6px">
              {{ t('alerts.auto.near_baseline') }}
            </el-tag>
          </template>
        </el-table-column>
      </el-table>

      <div class="sub" style="margin-top: 8px">{{ t('alerts.auto.consent') }}</div>
      <template #footer>
        <el-button @click="autoDialog = false">{{ t('alerts.action.cancel') }}</el-button>
        <el-button type="primary" :disabled="!canWrite || !autoSelected.length"
                   :loading="autoCreating" @click="createAutoRules">
          {{ t('alerts.auto.create', { n: autoSelected.length }) }}
        </el-button>
      </template>
    </el-dialog>

    <!-- 规则编辑对话框 -->
    <el-dialog v-model="ruleDialog" :title="editingRule ? t('alerts.dialog.edit_title') : t('alerts.dialog.new_title')" width="640px">
      <el-alert v-if="editingRule?.origin === 'auto'" type="success" :closable="false" show-icon
                :title="t('alerts.form.auto_hint')" style="margin-bottom: 12px" />
      <el-form label-width="100px">
        <el-form-item :label="t('alerts.form.name')" required>
          <el-input v-model="ruleForm.name" :placeholder="t('alerts.form.name_ph')" maxlength="128" />
        </el-form-item>
        <el-form-item :label="t('alerts.form.type')">
          <el-radio-group v-model="ruleForm.source" :disabled="!!editingRule">
            <el-radio-button value="metric">{{ t('alerts.form.type_metric') }}</el-radio-button>
            <el-radio-button value="sls">{{ t('alerts.form.type_sls') }}</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item :label="t('alerts.form.level')">
          <el-select v-model="ruleForm.level" style="width: 180px">
            <el-option :label="t('alerts.form.level_critical')" value="critical" />
            <el-option :label="t('alerts.form.level_warning')" value="warning" />
            <el-option :label="t('alerts.form.level_info')" value="info" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('alerts.form.account')">
          <el-select v-model="ruleForm.account_id" style="width: 220px" clearable>
            <el-option :label="t('alerts.rules.account_all')" :value="0" />
            <el-option v-for="a in ruleAccounts" :key="a.id" :label="a.name" :value="a.id" />
          </el-select>
          <div class="sub" style="width: 100%">{{ t('alerts.form.account_hint') }}</div>
        </el-form-item>

        <template v-if="ruleForm.source === 'metric'">
          <el-form-item :label="t('alerts.form.product_metric')" required>
            <el-select v-model="ruleForm.product" style="width: 160px" @change="loadMetricOptions">
              <el-option v-for="p in productOptions" :key="p.value" :label="p.label" :value="p.value" />
            </el-select>
            <el-select v-model="ruleForm.metric" style="width: 220px; margin-left: 8px">
              <el-option v-for="m in metricOptions" :key="m.value" :label="m.label" :value="m.value" />
            </el-select>
            <div class="sub" style="width: 100%">
              {{ t(ruleForm.product?.startsWith('vmware_') ? 'alerts.form.metric_hint_vmware' : 'alerts.form.metric_hint') }}
            </div>
          </el-form-item>
        </template>
        <template v-else>
          <el-form-item :label="t('alerts.form.logstore')" required>
            <el-select v-model="ruleForm.project" filterable style="width: 200px" @change="() => loadSlsOptions()">
              <el-option v-for="p in projects" :key="p" :label="p" :value="p" />
            </el-select>
            <el-select v-model="ruleForm.logstore" filterable style="width: 200px; margin-left: 8px" :loading="loadingLogstores">
              <el-option v-for="l in logstores" :key="l" :label="l" :value="l" />
            </el-select>
          </el-form-item>
          <el-form-item :label="t('alerts.form.query')">
            <el-input
              v-model="ruleForm.query_expr"
              :placeholder="t('alerts.form.query_ph')"
            />
            <!-- SLS 查询语法示例说明区 -->
            <div class="sls-guide">
              <div class="sls-guide-title">{{ t('alerts.sls_guide.title') }}</div>
              <ul class="sls-guide-list">
                <li><span class="mono">status &gt;= 500 | select count(*) as cnt</span>{{ t('alerts.sls_guide.li_5xx') }}</li>
                <li><span class="mono">level: ERROR | select count(*) as cnt</span>{{ t('alerts.sls_guide.li_error') }}</li>
                <li><span class="mono">content: /timeout|timed out/ | select count(*) as cnt</span>{{ t('alerts.sls_guide.li_regex') }}</li>
                <li>{{ t('alerts.sls_guide.li_empty') }}</li>
              </ul>
            </div>
          </el-form-item>
        </template>

        <el-form-item :label="t('alerts.form.threshold')" required>
          <el-select v-model="ruleForm.comparator" style="width: 90px">
            <el-option v-for="c in ['>', '>=', '<', '<=']" :key="c" :label="c" :value="c" />
          </el-select>
          <el-input-number v-model="ruleForm.threshold" :step="10" style="width: 180px; margin-left: 8px" />
        </el-form-item>
        <el-form-item :label="t('alerts.form.duration')">
          <el-select v-model="ruleForm.duration" style="width: 180px">
            <el-option v-for="d in DURATIONS" :key="d.value" :label="t(d.label)" :value="d.value" />
          </el-select>
          <span class="sub" style="margin-left: 8px">{{ t('alerts.form.duration_hint') }}</span>
        </el-form-item>
        <el-form-item :label="t('alerts.form.channels')">
          <el-select v-model="ruleForm.channel_ids" multiple style="width: 100%" :placeholder="t('alerts.form.channels_ph')">
            <el-option v-for="c in channels" :key="c.id" :label="c.name" :value="c.id" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('alerts.form.silences')">
          <el-input v-model="ruleForm.silences" :placeholder="t('alerts.form.silences_ph')" />
        </el-form-item>
        <el-form-item :label="t('alerts.form.enabled')">
          <el-switch v-model="ruleForm.enabled" />
        </el-form-item>
      </el-form>

      <!-- 试算结果面板：保存前先看"会触发几次/什么时间/样本值" -->
      <div v-if="dryResult" class="dry-wrap">
        <div class="dry-head">
          <span class="dry-title">
            {{ t('alerts.dry.title', { days: dryResult.samples_summary.days }) }}
            <template v-if="dryResult.count">{{ t('alerts.dry.would_fire', { n: dryResult.count }) }}</template>
            <template v-else>{{ t('alerts.dry.no_fire') }}</template>
          </span>
          <span class="sub mono">{{ dryResult.samples_summary.condition }}</span>
        </div>
        <div class="dry-stats sub">
          {{ t('alerts.dry.stats', {
            points: dryResult.samples_summary.data_points ?? dryResult.samples_summary.sampled_windows ?? '-',
            max: dryResult.samples_summary.value_max ?? '-',
            min: dryResult.samples_summary.value_min ?? '-',
            avg: dryResult.samples_summary.value_avg ?? '-',
          }) }}
        </div>
        <el-table
          v-if="dryResult.would_fire.length"
          :data="dryResult.would_fire.slice(0, 50)"
          size="small"
          border
          max-height="220"
        >
          <el-table-column prop="ts" :label="t('alerts.dry.col_time')" width="150" />
          <el-table-column :label="t('alerts.dry.col_target')" min-width="150">
            <template #default="{ row }">
              {{ row.target_name }}
              <div class="sub mono">{{ row.target }}</div>
            </template>
          </el-table-column>
          <el-table-column prop="value" :label="t('alerts.dry.col_value')" width="90" />
          <el-table-column prop="threshold" :label="t('alerts.dry.col_threshold')" width="80" />
          <el-table-column :label="t('alerts.dry.col_note')" width="100">
            <template #default="{ row }">
              <el-tag v-if="row.silenced" type="info" size="small">
                {{ row.silence_reason === 'window' ? t('alerts.dry.silence_window') : t('alerts.dry.suppressed') }}
              </el-tag>
            </template>
          </el-table-column>
        </el-table>
        <div v-if="dryResult.count > 50" class="sub">{{ t('alerts.dry.only_first', { n: dryResult.count }) }}</div>
        <div class="dry-note sub">{{ dryResult.samples_summary.note }}</div>
      </div>

      <template #footer>
        <el-button @click="ruleDialog = false">{{ t('alerts.action.cancel') }}</el-button>
        <el-select v-model="dryRunDays" size="small" style="width: 92px; margin: 0 8px">
          <el-option v-for="d in [1, 3, 7, 14, 30]" :key="d" :label="t('alerts.dry.days_opt', { d })" :value="d" />
        </el-select>
        <el-button :loading="dryRunning" :disabled="savingRule" @click="doDryRun">{{ t('alerts.action.dry_run') }}</el-button>
        <el-button type="primary" :loading="savingRule" :disabled="dryRunning" @click="saveRule">{{ t('alerts.action.save') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.ev-seg { margin-right: 14px; white-space: nowrap; }
.clamp2 {
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden;
}
.more-ic { margin-left: 2px; font-size: 12px; }
.danger-item { color: var(--el-color-danger) !important; }
.ad-wrap { display: flex; flex-direction: column; gap: 12px; }
.ad-line { font-size: 13px; line-height: 1.7; }
.ad-k { display: inline-block; min-width: 76px; color: var(--el-text-color-secondary); font-size: 12.5px; }
.ad-block { border-top: 1px solid var(--el-border-color-lighter); padding-top: 10px; }
.ad-content { margin-top: 6px; font-size: 13px; line-height: 1.8; white-space: pre-wrap; }
.ad-ops { display: flex; gap: 10px; border-top: 1px solid var(--el-border-color-lighter); padding-top: 12px; }
.status-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}
.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}
.auto-tag { margin-left: 6px; transform: scale(0.85); transform-origin: left center; }
.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.mono {
  font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 12px;
}
.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 10px;
}
/* SLS 查询语法示例说明区 */
.sls-guide {
  width: 100%;
  margin-top: 8px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 8px 12px;
  background: var(--el-fill-color-lighter);
}
.sls-guide-title {
  font-size: 12px;
  font-weight: 700;
  margin-bottom: 4px;
}
.sls-guide-list {
  margin: 0;
  padding-left: 16px;
  font-size: 11.5px;
  line-height: 1.9;
  color: var(--el-text-color-regular);
}
/* 试算结果面板 */
.dry-wrap {
  margin-top: 6px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 10px 12px;
  background: var(--el-fill-color-lighter);
}
.dry-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
}
.dry-title {
  font-size: 13px;
  font-weight: 700;
}
.dry-stats {
  margin: 6px 0 8px;
}
.dry-note {
  margin-top: 8px;
  line-height: 1.7;
}

</style>
