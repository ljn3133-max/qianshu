<script setup>
// 平台自愈问题收件箱：AI 先定位，R0/R1 可自动执行；R2/R3 必须管理员按完整计划哈希授权。
// 页面只展示固定 recipe 的结构化计划，不提供任何自由命令/代码输入入口。
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import MarkdownIt from 'markdown-it'

import {
  approvePlan, assistIssue, diagnoseIssue, ignoreIssue, issueDetail, issueMeta,
  listIssues, rejectPlan, resetBreaker, retryIssue, rollbackPlan,
} from '@/api/issues'
import { useAuthStore } from '@/stores/auth'
import { fmtTime } from '@/utils/format'
import { t } from '@/i18n'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const md = new MarkdownIt({ html: false, breaks: true })

const ACTIVE = 'open,diagnosing,awaiting_approval,awaiting_user,repairing,verifying,recurring,manual_required'
const BUSY = ['diagnosing', 'repairing', 'verifying']
const STATUS_TAG = {
  open: 'info', diagnosing: 'primary', awaiting_approval: 'warning',
  awaiting_user: 'warning', repairing: 'primary', verifying: 'primary',
  recovered: 'success', recurring: 'danger', manual_required: 'danger', ignored: 'info',
}
const SEVERITY_TAG = { info: 'info', warning: 'warning', critical: 'danger' }
const PLAN_STATUS_TAG = {
  proposed: 'warning', approved: 'primary', executing: 'primary', succeeded: 'success',
  failed: 'danger', expired: 'info', rejected: 'info', rolled_back: 'info',
}
const RISK_TAG = { R0: 'info', R1: 'success', R2: 'warning', R3: 'danger', R4: 'danger' }
const SOURCE_LABEL = {
  onboarding: 'issues.source.onboarding', cloud: 'issues.source.cloud',
  cloud_sync: 'issues.source.cloud_sync',
  alert: 'issues.source.alert', api_5xx: 'issues.source.api_5xx',
  copilotd: 'issues.source.copilotd', platform: 'issues.source.platform',
  job: 'issues.source.job', agent: 'issues.source.agent',
  code: 'issues.source.code', platform_health: 'issues.source.platform_health',
}
const EVENT_LABEL = {
  detected: 'issues.event.detected', deduped: 'issues.event.deduped',
  recurred: 'issues.event.recurred', plan_created: 'issues.event.plan_created',
  plan_rejected: 'issues.event.plan_rejected', approved: 'issues.event.approved',
  rejected: 'issues.event.rejected', repair_started: 'issues.event.repair_started',
  step_ok: 'issues.event.step_ok', step_failed: 'issues.event.step_failed',
  verify_done: 'issues.event.verify_done', recovered: 'issues.event.recovered',
  escalated: 'issues.event.escalated', assist_resumed: 'issues.event.assist_resumed',
  ignored: 'issues.event.ignored', rolled_back: 'issues.event.rolled_back',
  diagnosis_started: 'issues.event.diagnosis_started',
  diagnosis_done: 'issues.event.diagnosis_done',
  plan_approved: 'issues.event.plan_approved',
  plan_expired: 'issues.event.plan_expired',
  step_started: 'issues.event.step_started',
  step_done: 'issues.event.step_done',
  restart_delegated: 'issues.event.restart_delegated',
  rollback_failed: 'issues.event.rollback_failed',
}
const MODE_LABEL = {
  observe: 'issues.mode.observe', assist: 'issues.mode.assist',
  auto_r0: 'issues.mode.auto_r0', approved_r2: 'issues.mode.approved_r2',
  approved_r3: 'issues.mode.approved_r3',
}

const meta = ref(null)
const items = ref([])
const total = ref(0)
const stats = ref({})
const listLoading = ref(false)
const detailLoading = ref(false)
const detail = ref(null)
const selectedId = ref(null)
const page = reactive({ page: 1, pageSize: 30 })
const filter = reactive({
  status: 'active', severity: '', source: '',
  // 收件箱是平台级视图；默认 0，避免顶栏账号切换器给 GET 自动注入单账号过滤。
  account_id: Number(route.query.account_id || 0),
  run_id: Number(route.query.run_id || 0),
  hours: 0,
})
let timer = null

const role = computed(() => auth.role || meta.value?.role || '')
const isAdmin = computed(() => role.value === 'admin')
const currentPlan = computed(() => detail.value?.current_plan || null)
const planBody = computed(() => currentPlan.value?.plan || {})
const allowed = computed(() => detail.value?.allowed || {})
const hasBreaker = computed(() => !!meta.value?.breaker?.open)

function safeT(key, fallback = '') {
  const v = t(key)
  return v === key ? (fallback || key) : v
}
const statusLabel = (s) => safeT(`issues.status.${s}`, s)
const planStatusLabel = (s) => safeT(`issues.plan.status.${s}`, s)
const severityLabel = (s) => safeT(`issues.severity.${s}`, s)
const riskLabel = (r) => safeT(`issues.risk.${r}`, r)
const sourceLabel = (s) => safeT(SOURCE_LABEL[s] || '', s)
const createdByLabel = (v) => safeT(`issues.plan.created_by.${v}`, v)
const eventLabel = (k) => safeT(EVENT_LABEL[k] || '', k)
const modeLabel = (m) => safeT(MODE_LABEL[m] || '', m || '-')

const stepList = computed(() => (Array.isArray(planBody.value.steps) ? planBody.value.steps : []))
const assistSteps = computed(() => (Array.isArray(planBody.value.assist_steps) ? planBody.value.assist_steps : []))
const questions = computed(() => (Array.isArray(planBody.value.questions) ? planBody.value.questions : []))
const patchStep = computed(() => stepList.value.find((s) => s.recipe === 'patch_platform_code') || null)
const patchFiles = computed(() => patchStep.value?.params?.plan?.files || [])
const confidencePct = computed(() => {
  const c = Number(planBody.value.confidence)
  return Number.isFinite(c) ? Math.round(c * 100) : null
})

function tagType(map, value, fallback = 'info') {
  return map[value] || fallback
}
function pretty(obj) {
  try { return JSON.stringify(obj, null, 2) } catch { return String(obj) }
}
function renderMarkdown(text) {
  return text ? md.render(String(text)) : ''
}
function patchFileKey(file, idx) {
  return `${file?.path || 'file'}:${idx}`
}

async function loadMeta() {
  try { meta.value = await issueMeta() } catch { meta.value = null }
}

async function loadList(silent = false) {
  if (!silent) listLoading.value = true
  try {
    const status = filter.status === 'active' ? ACTIVE : filter.status
    const r = await listIssues({
      status, severity: filter.severity, source: filter.source,
      account_id: filter.account_id || 0,
      run_id: filter.run_id || 0,
      hours: filter.hours || 0,
      page: page.page, page_size: page.pageSize,
    })
    items.value = r.items || []
    total.value = r.total || 0
    stats.value = r.stats || {}
    if (meta.value && r.mode) meta.value.mode = r.mode
  } finally {
    if (!silent) listLoading.value = false
  }
}

async function openIssue(id, silent = false) {
  if (!id) return
  if (!silent) detailLoading.value = true
  try {
    detail.value = await issueDetail(id)
    selectedId.value = Number(id)
    if (String(route.query.id || '') !== String(id)) {
      router.replace({ path: route.path, query: { ...route.query, id: String(id) } })
    }
  } catch {
    if (!silent) detail.value = null
  } finally {
    if (!silent) detailLoading.value = false
  }
}

async function refreshAll() {
  await Promise.all([loadMeta(), loadList(true)])
  if (selectedId.value) await openIssue(selectedId.value, true)
}

function chooseStatus(value) {
  filter.status = value
  page.page = 1
  loadList()
}
function chooseFilter() {
  page.page = 1
  loadList()
}

function afterAction(message) {
  ElMessage.success(message)
  setTimeout(() => refreshAll(), 700)
}

async function doDiagnose() {
  try {
    await diagnoseIssue(selectedId.value)
    afterAction(t('issues.msg.diagnose_started'))
  } catch { /* http 拦截器已提示 */ }
}

async function doRetry() {
  try {
    await retryIssue(selectedId.value)
    afterAction(t('issues.msg.retry_started'))
  } catch { /* ignore */ }
}

async function doIgnore() {
  try {
    await ElMessageBox.confirm(t('issues.msg.ignore_confirm'), t('issues.actions.ignore'), {
      type: 'warning', confirmButtonText: t('issues.actions.ignore'), cancelButtonText: t('issues.actions.close'),
    })
  } catch { return }
  try {
    await ignoreIssue(selectedId.value)
    afterAction(t('issues.msg.ignored'))
  } catch { /* ignore */ }
}

async function doRollback() {
  try {
    await ElMessageBox.confirm(t('issues.actions.rollback_confirm'), t('issues.actions.rollback'), {
      type: 'warning', confirmButtonText: t('issues.actions.rollback'), cancelButtonText: t('issues.actions.close'),
    })
  } catch { return }
  try {
    await rollbackPlan(selectedId.value, currentPlan.value.id)
    afterAction(t('issues.msg.rolled_back'))
  } catch { /* ignore */ }
}

async function doResetBreaker() {
  try {
    await ElMessageBox.confirm(t('issues.breaker.reset_confirm'), t('issues.breaker.reset'), {
      type: 'warning', confirmButtonText: t('issues.breaker.reset'), cancelButtonText: t('issues.actions.close'),
    })
  } catch { return }
  try {
    await resetBreaker()
    afterAction(t('issues.msg.breaker_reset'))
  } catch { /* ignore */ }
}

/* ---------------- 授权 / 拒绝 / 协助 ---------------- */
const approveDialog = reactive({ visible: false, ack: false, submitting: false })
const rejectDialog = reactive({ visible: false, reason: '', submitting: false })
const assistDialog = reactive({ visible: false, note: '', submitting: false })

function openApprove() {
  approveDialog.ack = false
  approveDialog.visible = true
}
async function submitApprove() {
  if (!approveDialog.ack) {
    ElMessage.warning(t('issues.approve.ack_required'))
    return
  }
  approveDialog.submitting = true
  try {
    await approvePlan(selectedId.value, currentPlan.value.id, {
      plan_hash: currentPlan.value.plan_hash, ack: true,
    })
    approveDialog.visible = false
    afterAction(t('issues.msg.approved'))
  } catch { /* ignore */ } finally {
    approveDialog.submitting = false
  }
}

function openReject() {
  rejectDialog.reason = ''
  rejectDialog.visible = true
}
async function submitReject() {
  rejectDialog.submitting = true
  try {
    await rejectPlan(selectedId.value, currentPlan.value.id, { reason: rejectDialog.reason })
    rejectDialog.visible = false
    afterAction(t('issues.msg.rejected'))
  } catch { /* ignore */ } finally {
    rejectDialog.submitting = false
  }
}

function openAssist() {
  assistDialog.note = ''
  assistDialog.visible = true
}
async function submitAssist() {
  assistDialog.submitting = true
  try {
    await assistIssue(selectedId.value, { note: assistDialog.note })
    assistDialog.visible = false
    afterAction(t('issues.msg.assist_submitted'))
  } catch { /* ignore */ } finally {
    assistDialog.submitting = false
  }
}

/* ---------------- 轮询：只在处理中状态刷新，避免无意义请求 ---------------- */
function schedulePoll() {
  timer = setInterval(async () => {
    const s = detail.value?.status
    if (selectedId.value && BUSY.includes(s)) await openIssue(selectedId.value, true)
    if (items.value.some((i) => BUSY.includes(i.status))) await loadList(true)
  }, 5000)
}

onMounted(async () => {
  await loadMeta()
  await loadList()
  const qid = Number(route.query.id || 0)
  if (qid) {
    await openIssue(qid)
  } else if (items.value.length) {
    await openIssue(items.value[0].id)
  }
  schedulePoll()
})
// 任务结果卡等入口带 account_id 深链：切到同一页面时也要重新按账号收窄。
watch(() => [route.query.account_id, route.query.run_id], ([acc, run]) => {
  filter.account_id = Number(acc || 0)
  filter.run_id = Number(run || 0)
  page.page = 1
  loadList()
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div class="page">
    <el-card shadow="never" class="issue-hero">
      <template #header>
        <div class="hero-head">
          <div>
            <div class="hero-title">{{ t('issues.title') }}</div>
            <div class="sub">{{ t('issues.subtitle') }}</div>
          </div>
          <div class="hero-tags">
            <el-tag v-if="meta" size="small" effect="plain">{{ t('issues.mode.label') }}：{{ modeLabel(meta.mode) }}</el-tag>
            <el-tag v-if="meta" size="small" :type="meta.llm_enabled ? 'success' : 'info'" effect="plain">
              {{ meta.llm_enabled ? t('issues.llm.on') : t('issues.llm.off') }}
            </el-tag>
            <el-button v-if="isAdmin && hasBreaker" size="small" type="danger" plain @click="doResetBreaker">
              {{ t('issues.breaker.reset') }}
            </el-button>
          </div>
        </div>
      </template>
      <el-alert v-if="hasBreaker" type="error" :closable="false" show-icon :title="t('issues.breaker.open')" />
      <div v-if="stats.total !== undefined" class="stats-row">
        <span><b>{{ stats.active || 0 }}</b> {{ t('issues.stats.active') }}</span>
        <span><b>{{ stats.recovered || 0 }}</b> {{ t('issues.stats.recovered') }}</span>
        <span><b>{{ stats.awaiting_approval || 0 }}</b> {{ t('issues.stats.awaiting_approval') }}</span>
        <span><b>{{ stats.manual_required || 0 }}</b> {{ t('issues.stats.manual_required') }}</span>
        <span><b>{{ stats.recurring || 0 }}</b> {{ t('issues.stats.recurring') }}</span>
      </div>
    </el-card>

    <el-row :gutter="16" class="issue-body">
      <!-- 左：问题列表 -->
      <el-col :xs="24" :md="9" :lg="8">
        <el-card shadow="never" class="issue-list-card" v-loading="listLoading">
          <template #header>
            <div class="list-head">
              <span class="panel-title">{{ t('issues.filter.status') }}</span>
              <el-button link type="primary" size="small" @click="refreshAll">{{ t('issues.filter.refresh') }}</el-button>
            </div>
          </template>
          <div class="filters">
            <el-radio-group v-model="filter.status" size="small" @change="chooseStatus">
              <el-radio-button value="active">{{ t('issues.filter.active') }}</el-radio-button>
              <el-radio-button value="">{{ t('issues.filter.all') }}</el-radio-button>
              <el-radio-button value="recovered">{{ t('issues.filter.recovered') }}</el-radio-button>
              <el-radio-button value="ignored">{{ t('issues.filter.ignored') }}</el-radio-button>
            </el-radio-group>
            <div class="filter-row">
              <el-select v-model="filter.severity" size="small" clearable :placeholder="t('issues.filter.severity')" @change="chooseFilter">
                <el-option :label="t('issues.severity.critical')" value="critical" />
                <el-option :label="t('issues.severity.warning')" value="warning" />
                <el-option :label="t('issues.severity.info')" value="info" />
              </el-select>
              <el-select v-model="filter.source" size="small" clearable :placeholder="t('issues.filter.source')" @change="chooseFilter">
                <el-option :label="t('issues.source.onboarding')" value="onboarding" />
                <el-option :label="t('issues.source.cloud')" value="cloud" />
                <el-option :label="t('issues.source.cloud_sync')" value="cloud_sync" />
                <el-option :label="t('issues.source.alert')" value="alert" />
                <el-option :label="t('issues.source.api_5xx')" value="api_5xx" />
                <el-option :label="t('issues.source.copilotd')" value="copilotd" />
                <el-option :label="t('issues.source.job')" value="job" />
                <el-option :label="t('issues.source.agent')" value="agent" />
                <el-option :label="t('issues.source.code')" value="code" />
                <el-option :label="t('issues.source.platform_health')" value="platform_health" />
              </el-select>
            </div>
            <div class="filter-row">
              <el-input-number v-model="filter.account_id" size="small" :min="0" :controls="false"
                               :placeholder="t('issues.filter.account')" @change="chooseFilter" />
              <el-input-number v-model="filter.run_id" size="small" :min="0" :controls="false"
                               :placeholder="t('issues.filter.run')" @change="chooseFilter" />
              <el-select v-model="filter.hours" size="small" @change="chooseFilter">
                <el-option :label="t('issues.filter.time_all')" :value="0" />
                <el-option :label="t('issues.filter.time_1h')" :value="1" />
                <el-option :label="t('issues.filter.time_24h')" :value="24" />
                <el-option :label="t('issues.filter.time_7d')" :value="168" />
              </el-select>
            </div>
          </div>

          <div v-if="!items.length" class="empty">{{ t('issues.list.empty') }}</div>
          <div v-else class="issue-items">
            <div v-for="row in items" :key="row.id" class="issue-item"
                 :class="{ active: String(row.id) === String(selectedId) }"
                 :data-issue-id="row.id" @click="openIssue(row.id)">
              <div class="item-top">
                <el-tag size="small" :type="tagType(SEVERITY_TAG, row.severity)" effect="dark">
                  {{ severityLabel(row.severity) }}
                </el-tag>
                <el-tag size="small" :type="tagType(STATUS_TAG, row.status)" effect="plain">
                  {{ statusLabel(row.status) }}
                </el-tag>
                <span class="item-id">#{{ row.id }}</span>
              </div>
              <div class="item-title">{{ row.title }}</div>
              <div class="item-meta">
                <span>{{ sourceLabel(row.source) }}</span>
                <span v-if="row.capability">· {{ row.capability }}</span>
                <span v-if="row.account_id">· {{ t('issues.detail.account') }} {{ row.account_id }}</span>
              </div>
              <div class="item-foot">
                <span>{{ t('issues.list.occurrences', { n: row.occurrences }) }}</span>
                <span v-if="row.last_seen">· {{ t('issues.list.last_seen', { time: fmtTime(row.last_seen) }) }}</span>
              </div>
            </div>
          </div>
          <div v-if="total > page.pageSize" class="pager">
            <el-pagination v-model:current-page="page.page" :page-size="page.pageSize" :total="total"
                           layout="prev, pager, next" small @current-change="loadList" />
          </div>
        </el-card>
      </el-col>

      <!-- 右：问题详情 -->
      <el-col :xs="24" :md="15" :lg="16">
        <div v-if="!detail" class="detail-empty">{{ t('issues.detail.empty') }}</div>
        <div v-else v-loading="detailLoading">
          <el-card shadow="never" class="detail-card">
            <template #header>
              <div class="detail-head">
                <div class="detail-title">
                  <el-tag :type="tagType(SEVERITY_TAG, detail.severity)" effect="dark">{{ severityLabel(detail.severity) }}</el-tag>
                  <el-tag :type="tagType(STATUS_TAG, detail.status)" effect="plain">{{ statusLabel(detail.status) }}</el-tag>
                  <b>#{{ detail.id }} {{ detail.title }}</b>
                </div>
                <div class="detail-actions">
                  <el-button v-if="allowed.diagnose" size="small" :loading="detail.status === 'diagnosing'" @click="doDiagnose">
                    {{ detail.status === 'diagnosing' ? t('issues.actions.diagnosing') : t('issues.actions.diagnose') }}
                  </el-button>
                  <el-button v-if="allowed.retry" size="small" type="primary" plain @click="doRetry">
                    {{ t('issues.actions.retry') }}
                  </el-button>
                  <el-button v-if="allowed.assist" size="small" type="success" @click="openAssist">
                    {{ t('issues.actions.assist') }}
                  </el-button>
                  <el-button v-if="allowed.ignore" size="small" type="danger" text @click="doIgnore">
                    {{ t('issues.actions.ignore') }}
                  </el-button>
                </div>
              </div>
            </template>

            <div class="meta-grid">
              <div><span>{{ t('issues.detail.reason_code') }}</span><b>{{ detail.reason_code || '-' }}</b></div>
              <div><span>{{ t('issues.detail.error_class') }}</span><b>{{ detail.error_class || '-' }}</b></div>
              <div><span>{{ t('issues.detail.account') }}</span><b>{{ detail.account_id || '-' }}</b></div>
              <div><span>{{ t('issues.detail.capability') }}</span><b>{{ detail.capability || '-' }}</b></div>
              <div><span>{{ t('issues.detail.attempts') }}</span><b>{{ detail.attempts }} / {{ detail.max_attempts }}</b></div>
              <div><span>{{ t('issues.detail.next_action') }}</span><b>{{ detail.next_action || '-' }}</b></div>
              <div><span>{{ t('issues.detail.first_seen') }}</span><b>{{ fmtTime(detail.first_seen) }}</b></div>
              <div><span>{{ t('issues.detail.last_seen') }}</span><b>{{ fmtTime(detail.last_seen) }}</b></div>
            </div>

            <el-alert v-if="detail.recurrence_of_id" type="error" :closable="false" show-icon class="recurrence"
                      :title="t('issues.detail.recurrence', { id: detail.recurrence_of_id })" />
            <div v-if="detail.resolution_md" class="resolution">
              <b>{{ t('issues.detail.resolution') }}</b>
              <div class="md" v-html="renderMarkdown(detail.resolution_md)"></div>
            </div>
            <div v-if="detail.summary_md" class="md summary" v-html="renderMarkdown(detail.summary_md)"></div>
          </el-card>

          <!-- 计划 -->
          <el-card shadow="never" class="detail-card plan-card">
            <template #header>
              <div class="plan-head">
                <span class="panel-title">{{ t('issues.plan.title') }}</span>
                <template v-if="currentPlan">
                  <el-tag size="small" effect="plain">{{ t('issues.plan.version', { n: currentPlan.version }) }}</el-tag>
                  <el-tag size="small" :type="tagType(RISK_TAG, currentPlan.risk)">{{ riskLabel(currentPlan.risk) }}</el-tag>
                  <el-tag size="small" :type="tagType(PLAN_STATUS_TAG, currentPlan.status)">
                    {{ planStatusLabel(currentPlan.status) }}
                  </el-tag>
                </template>
                <div class="plan-head-actions">
                  <el-button v-if="allowed.approve" size="small" type="warning" @click="openApprove">
                    {{ t('issues.actions.approve') }}
                  </el-button>
                  <el-tag v-if="allowed.approve_blocked" size="small" type="info" effect="plain">
                    {{ t('issues.plan.mode_blocked') }}
                  </el-tag>
                  <el-button v-if="allowed.reject" size="small" @click="openReject">{{ t('issues.actions.reject') }}</el-button>
                  <el-button v-if="allowed.rollback" size="small" type="danger" plain @click="doRollback">
                    {{ t('issues.actions.rollback') }}
                  </el-button>
                </div>
              </div>
            </template>

            <div v-if="!currentPlan" class="empty">{{ t('issues.plan.none') }}</div>
            <template v-else>
              <div class="plan-meta">
                <div><span>{{ t('issues.plan.created_by') }}</span><b>{{ createdByLabel(currentPlan.created_by) }}</b></div>
                <div v-if="currentPlan.model"><span>{{ t('issues.plan.model') }}</span><b>{{ currentPlan.model }}</b></div>
                <div><span>{{ t('issues.plan.expires') }}</span><b>{{ fmtTime(currentPlan.expires_at) }}</b></div>
                <div v-if="currentPlan.approved_by"><span>{{ t('issues.plan.approved_by') }}</span><b>{{ currentPlan.approved_by }}</b></div>
                <div v-if="confidencePct !== null"><span>{{ t('issues.plan.confidence') }}</span><b>{{ confidencePct }}%</b></div>
              </div>
              <div class="hash-line">
                <span>{{ t('issues.plan.hash') }}</span>
                <code>{{ currentPlan.plan_hash }}</code>
                <div class="sub">{{ t('issues.plan.hash_hint') }}</div>
              </div>
              <div class="sub hint-line">{{ t('issues.plan.auto_hint') }}</div>

              <div v-if="planBody.summary" class="plan-block">
                <b>{{ t('issues.plan.summary') }}</b>
                <div class="md" v-html="renderMarkdown(planBody.summary)"></div>
              </div>
              <div v-if="planBody.root_cause || detail.root_cause_md" class="plan-block">
                <b>{{ t('issues.plan.root_cause') }}</b>
                <div class="md" v-html="renderMarkdown(planBody.root_cause || detail.root_cause_md)"></div>
              </div>
              <div v-if="planBody.impact || detail.impact_md" class="plan-block">
                <b>{{ t('issues.plan.impact') }}</b>
                <div class="md" v-html="renderMarkdown(planBody.impact || detail.impact_md)"></div>
              </div>
              <div v-if="planBody.verify" class="plan-block">
                <b>{{ t('issues.plan.verify') }}</b>
                <div class="md" v-html="renderMarkdown(planBody.verify)"></div>
              </div>

              <div v-if="stepList.length" class="plan-block">
                <b>{{ t('issues.plan.steps') }}</b>
                <div v-for="(step, i) in stepList" :key="`${step.recipe}:${i}`" class="step">
                  <div class="step-head">
                    <span class="step-no">{{ t('issues.plan.step', { n: i + 1 }) }}</span>
                    <el-tag size="small" :type="tagType(RISK_TAG, step.risk)">{{ riskLabel(step.risk) }}</el-tag>
                    <code>{{ step.recipe }}</code>
                    <span class="sub">{{ t('issues.plan.timeout', { s: step.timeout_s }) }}</span>
                  </div>
                  <div class="step-line"><span>{{ t('issues.plan.reason') }}</span>{{ step.reason }}</div>
                  <div class="step-line"><span>{{ t('issues.plan.expected') }}</span>{{ step.expected_effect }}</div>
                  <div class="step-line"><span>{{ t('issues.plan.verify_expect') }}</span>
                    {{ step.verify?.kind }} · {{ step.verify?.expect }}
                  </div>
                  <el-collapse v-if="step.params && Object.keys(step.params).length">
                    <el-collapse-item :title="t('issues.plan.params')">
                      <pre class="code-block">{{ pretty(step.params) }}</pre>
                    </el-collapse-item>
                  </el-collapse>
                </div>
              </div>

              <!-- R3 代码补丁：管理员授权前必须能看到真实改动 -->
              <div v-if="patchFiles.length" class="plan-block patch-block">
                <b>{{ t('issues.plan.patch_files') }}（{{ patchFiles.length }}）</b>
                <div v-for="(file, fi) in patchFiles" :key="patchFileKey(file, fi)" class="patch-file">
                  <div class="patch-path"><code>{{ file.path }}</code></div>
                  <div v-if="file.new" class="patch-part">
                    <span>{{ t('issues.plan.patch_new') }}</span>
                    <pre class="code-block patch-pre">{{ file.new }}</pre>
                  </div>
                  <div v-for="(edit, ei) in (file.edits || [])" :key="ei" class="patch-part">
                    <span>{{ t('issues.plan.patch_edits') }} #{{ ei + 1 }}</span>
                    <div class="sub">{{ t('issues.plan.patch_old') }}</div>
                    <pre class="code-block patch-pre patch-old">{{ edit.old }}</pre>
                    <div class="sub">{{ t('issues.plan.patch_after') }}</div>
                    <pre class="code-block patch-pre patch-new">{{ edit.new }}</pre>
                  </div>
                </div>
              </div>

              <div v-if="assistSteps.length" class="plan-block assist-block">
                <b>{{ t('issues.plan.assist') }}</b>
                <ol class="assist-list">
                  <li v-for="(s, i) in assistSteps" :key="i">{{ s }}</li>
                </ol>
                <template v-if="questions.length">
                  <b>{{ t('issues.plan.questions') }}</b>
                  <ul class="assist-list">
                    <li v-for="(q, i) in questions" :key="i">{{ q }}</li>
                  </ul>
                </template>
                <div class="sub">{{ t('issues.plan.awaiting_user_hint') }}</div>
                <el-button v-if="allowed.assist" size="small" type="success" @click="openAssist">
                  {{ t('issues.actions.assist') }}
                </el-button>
              </div>

              <div v-if="currentPlan.result && Object.keys(currentPlan.result).length" class="plan-block">
                <b>{{ t('issues.plan.result') }}</b>
                <pre class="code-block">{{ pretty(currentPlan.result) }}</pre>
              </div>
            </template>
          </el-card>

          <!-- 证据 -->
          <el-card shadow="never" class="detail-card">
            <template #header><span class="panel-title">{{ t('issues.evidence.title') }}</span></template>
            <div v-if="!detail.evidence || !Object.keys(detail.evidence).length" class="empty">{{ t('issues.evidence.empty') }}</div>
            <el-collapse v-else>
              <el-collapse-item :title="t('issues.evidence.expand')">
                <pre class="code-block evidence-block">{{ pretty(detail.evidence) }}</pre>
              </el-collapse-item>
            </el-collapse>
          </el-card>

          <!-- 时间线 -->
          <el-card shadow="never" class="detail-card">
            <template #header><span class="panel-title">{{ t('issues.timeline.title') }}</span></template>
            <div v-if="!detail.events?.length" class="empty">{{ t('issues.timeline.empty') }}</div>
            <el-timeline v-else class="timeline">
              <el-timeline-item v-for="ev in detail.events" :key="ev.id"
                                :timestamp="fmtTime(ev.created_at)" placement="top">
                <div class="event-head">
                  <b>{{ eventLabel(ev.kind) }}</b>
                  <span class="sub">· {{ ev.actor }}</span>
                </div>
                <div v-if="ev.payload && Object.keys(ev.payload).length" class="event-payload">
                  <code>{{ pretty(ev.payload) }}</code>
                </div>
              </el-timeline-item>
            </el-timeline>
          </el-card>
        </div>
      </el-col>
    </el-row>

    <!-- 授权弹窗：展示完整哈希 + 风险 + 步骤，勾选后才提交 -->
    <el-dialog v-model="approveDialog.visible" :title="t('issues.approve.title')" width="720px">
      <el-alert type="warning" :closable="false" show-icon :title="t('issues.approve.alert')" />
      <div class="approve-head">
        <span>{{ t('issues.approve.risk') }}</span>
        <el-tag :type="tagType(RISK_TAG, currentPlan?.risk)">{{ riskLabel(currentPlan?.risk) }}</el-tag>
        <span>{{ t('issues.plan.version', { n: currentPlan?.version }) }}</span>
      </div>
      <div class="approve-hash">
        <div>{{ t('issues.approve.hash_label') }}</div>
        <code>{{ currentPlan?.plan_hash }}</code>
      </div>
      <div v-if="stepList.length" class="approve-steps">
        <div v-for="(step, i) in stepList" :key="`a:${step.recipe}:${i}`" class="approve-step">
          <span class="step-no">{{ t('issues.plan.step', { n: i + 1 }) }}</span>
          <el-tag size="small" :type="tagType(RISK_TAG, step.risk)">{{ riskLabel(step.risk) }}</el-tag>
          <code>{{ step.recipe }}</code>
          <span>{{ step.reason }}</span>
        </div>
      </div>
      <el-checkbox v-model="approveDialog.ack" class="approve-ack">{{ t('issues.approve.ack') }}</el-checkbox>
      <template #footer>
        <el-button @click="approveDialog.visible = false">{{ t('issues.approve.cancel') }}</el-button>
        <el-button type="warning" :disabled="!approveDialog.ack" :loading="approveDialog.submitting"
                   @click="submitApprove">{{ t('issues.approve.submit') }}</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="rejectDialog.visible" :title="t('issues.reject.title')" width="480px">
      <el-input v-model="rejectDialog.reason" type="textarea" :rows="3" maxlength="500" show-word-limit
                :placeholder="t('issues.reject.reason')" />
      <template #footer>
        <el-button @click="rejectDialog.visible = false">{{ t('issues.actions.close') }}</el-button>
        <el-button type="danger" :loading="rejectDialog.submitting" @click="submitReject">
          {{ t('issues.reject.submit') }}
        </el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="assistDialog.visible" :title="t('issues.assist.title')" width="560px">
      <el-input v-model="assistDialog.note" type="textarea" :rows="5" maxlength="2000" show-word-limit
                :placeholder="t('issues.assist.placeholder')" />
      <template #footer>
        <el-button @click="assistDialog.visible = false">{{ t('issues.actions.close') }}</el-button>
        <el-button type="success" :loading="assistDialog.submitting" @click="submitAssist">
          {{ t('issues.assist.submit') }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.issue-hero { margin-bottom: 16px; }
.hero-head { display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; align-items: flex-start; }
.hero-title { font-size: 18px; font-weight: 600; }
.hero-tags { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.stats-row { display: flex; gap: 18px; flex-wrap: wrap; margin-top: 12px; color: var(--el-text-color-secondary); font-size: 13px; }
.stats-row b { color: var(--el-text-color-primary); margin-right: 2px; }
.issue-body { align-items: flex-start; }
.issue-list-card { position: sticky; top: 12px; }
.panel-title { font-weight: 600; }
.list-head, .plan-head, .detail-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.list-head, .plan-head { justify-content: space-between; }
.filters { display: flex; flex-direction: column; gap: 10px; margin-bottom: 12px; }
.filter-row { display: flex; gap: 8px; }
.filter-row :deep(.el-select), .filter-row :deep(.el-input-number) { flex: 1; min-width: 0; }
.issue-items { display: flex; flex-direction: column; gap: 8px; max-height: calc(100vh - 330px); overflow-y: auto; padding-right: 4px; }
.issue-item { border: 1px solid var(--el-border-color-light); border-radius: 8px; padding: 10px; cursor: pointer; transition: border-color .15s, background .15s; }
.issue-item:hover { border-color: var(--el-color-primary); }
.issue-item.active { border-color: var(--el-color-primary); background: var(--el-color-primary-light-9); }
.item-top { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.item-id { margin-left: auto; color: var(--el-text-color-secondary); font-size: 12px; }
.item-title { font-weight: 600; margin: 6px 0 4px; word-break: break-all; }
.item-meta, .item-foot { color: var(--el-text-color-secondary); font-size: 12px; display: flex; gap: 4px; flex-wrap: wrap; }
.item-foot { margin-top: 4px; }
.pager { display: flex; justify-content: center; margin-top: 10px; }
.detail-empty { color: var(--el-text-color-secondary); text-align: center; padding: 60px 0; border: 1px dashed var(--el-border-color); border-radius: 8px; }
.detail-card { margin-bottom: 16px; }
.plan-card { border-left: 3px solid var(--el-color-warning-light-5); }
.plan-head-actions { display: flex; align-items: center; gap: 6px; margin-left: auto; }
.approve-steps { display: flex; flex-direction: column; gap: 8px; margin: 12px 0; max-height: 260px; overflow-y: auto; }
.detail-title { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; flex: 1; min-width: 0; }
.detail-actions { display: flex; gap: 6px; flex-wrap: wrap; }
.meta-grid, .plan-meta { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 8px 16px; }
.meta-grid > div, .plan-meta > div { display: flex; justify-content: space-between; gap: 8px; font-size: 13px; border-bottom: 1px dashed var(--el-border-color-lighter); padding-bottom: 4px; }
.meta-grid span, .plan-meta span { color: var(--el-text-color-secondary); }
.recurrence { margin: 12px 0; }
.resolution, .summary { margin-top: 12px; }
.md :deep(p) { margin: 6px 0; }
.md :deep(pre) { background: var(--el-fill-color-light); padding: 8px; border-radius: 6px; overflow-x: auto; }
.hash-line { margin: 12px 0; display: flex; flex-direction: column; gap: 4px; }
.hash-line code, .approve-hash code { word-break: break-all; background: var(--el-fill-color-light); padding: 4px 6px; border-radius: 4px; font-size: 12px; }
.hint-line { margin-bottom: 8px; }
.plan-block { margin-top: 14px; }
.plan-block > b { display: block; margin-bottom: 6px; }
.plan-block .md { font-size: 13px; }
.step { border: 1px solid var(--el-border-color-light); border-radius: 8px; padding: 10px; margin-bottom: 8px; }
.step-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 6px; }
.step-no { font-weight: 600; }
.step-line { font-size: 13px; margin: 4px 0; display: flex; gap: 8px; }
.step-line > span:first-child { color: var(--el-text-color-secondary); min-width: 76px; flex: none; }
.code-block { background: var(--el-fill-color-light); padding: 8px; border-radius: 6px; overflow: auto; font-size: 12px; max-height: 360px; white-space: pre-wrap; word-break: break-all; }
.patch-block { border-top: 1px solid var(--el-border-color-lighter); padding-top: 12px; }
.patch-file { border: 1px solid var(--el-border-color-light); border-radius: 8px; padding: 10px; margin-bottom: 10px; }
.patch-path { margin-bottom: 6px; }
.patch-part { margin-top: 8px; font-size: 13px; }
.patch-pre { max-height: 320px; }
.patch-old { border-left: 3px solid var(--el-color-danger); }
.patch-new { border-left: 3px solid var(--el-color-success); }
.assist-block { background: var(--el-color-warning-light-9); border-radius: 8px; padding: 12px; }
.assist-list { margin: 6px 0 10px; padding-left: 20px; font-size: 13px; }
.evidence-block { max-height: 460px; }
.timeline { padding-left: 4px; }
.event-head { font-size: 13px; }
.event-payload code { font-size: 11px; color: var(--el-text-color-secondary); word-break: break-all; white-space: pre-wrap; }
.empty { color: var(--el-text-color-secondary); padding: 18px 0; text-align: center; }
.approve-head { display: flex; align-items: center; gap: 10px; margin: 14px 0 8px; }
.approve-hash { margin-bottom: 12px; }
.approve-hash > div { margin-bottom: 4px; font-size: 13px; color: var(--el-text-color-secondary); }
.approve-step { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 13px; padding: 6px 0; border-bottom: 1px dashed var(--el-border-color-lighter); }
.approve-ack { display: block; margin-top: 16px; }
@media (max-width: 992px) {
  .issue-list-card { position: static; margin-bottom: 16px; }
  .issue-items { max-height: 420px; }
}
</style>
