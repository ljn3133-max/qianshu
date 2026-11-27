<script setup>
// 接入卡唯一事实源是 OnboardingRun.capabilities；不根据账号状态或列表数量另算就绪。
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { retryOnboardingCapabilities } from '@/api/clouds'
import { fmtTime } from '@/utils/format'
import { t } from '@/i18n'

const props = defineProps({
  run: { type: Object, default: null },
  canWrite: { type: Boolean, default: false },
  canEditCredentials: { type: Boolean, default: false },
  loading: { type: Boolean, default: false },
  loadError: { type: String, default: '' },
})
const emit = defineEmits(['update:run', 'refresh', 'edit-credential', 'confirm-logs'])
const router = useRouter()
const retrying = ref(false)
const actionError = ref('')

const capabilities = computed(() => Array.isArray(props.run?.capabilities) ? props.run.capabilities : [])
const ready = computed(() => capabilities.value.filter((x) => x.execution === 'succeeded'))
const running = computed(() => capabilities.value.filter((x) => x.execution === 'running'))
const failed = computed(() => capabilities.value.filter((x) => x.execution === 'failed'))
const confirmations = computed(() => capabilities.value.filter((x) =>
  x.execution === 'waiting' && (x.reason_code === 'confirmation_required'
    || ['confirm_sls_targets', 'confirm_logs'].includes(x.next_action))))
const waiting = computed(() => capabilities.value.filter((x) => x.execution === 'waiting'))
const issueCapability = (issue) => ({
  assets: 'assets', sls_discovery: 'logs', sls_targets: 'logs',
}[issue.kind] || issue.kind || 'unknown')
// 凭证、规则种子和调度失败只存在于 Run.items/issues，不能因五个默认能力仍 waiting 就显示“准备中”。
const issueFailures = computed(() => (props.run?.issues || []).filter((x) => x.status === 'failed').map((x) => ({
  ...x, capability: issueCapability(x), execution: 'failed', _issue: true,
})))
const uncoveredIssueFailures = computed(() => issueFailures.value.filter((issue) =>
  !failed.value.some((x) => x.capability === issue.capability)))
const hardFailures = computed(() => [...failed.value, ...uncoveredIssueFailures.value])
const blockers = computed(() => [...hardFailures.value, ...confirmations.value])
const scopeKey = (x) => x._issue ? `issue:${x.key}` : [x.capability, x.scope?.region, x.scope?.source_id,
  ...(x.scope?.resource_ids || []), x.scope?.bill_date].join(':')

const conclusion = computed(() => {
  if (props.loadError && !props.run) return t('clouds.task.conclusion.unavailable')
  if (!props.run) return t('clouds.task.conclusion.loading')
  if (hardFailures.value.length) return t('clouds.task.conclusion.failed', { n: hardFailures.value.length })
  if (confirmations.value.length) return t('clouds.task.conclusion.confirm')
  if (running.value.length || waiting.value.length) {
    return ready.value.length
      ? t('clouds.task.conclusion.partial', { n: ready.value.length })
      : t('clouds.task.conclusion.running')
  }
  if (ready.value.length) return t('clouds.task.conclusion.ready', { n: ready.value.length })
  return t('clouds.task.conclusion.unverified')
})
const statusType = computed(() => {
  if (hardFailures.value.length || props.run?.status === 'failed') return 'danger'
  if (confirmations.value.length || props.run?.status === 'attention') return 'warning'
  if (running.value.length || waiting.value.length || props.run?.status === 'running') return 'primary'
  if (ready.value.length) return 'success'
  return 'info'
})
const statusLabel = computed(() => t(`clouds.task.status.${props.run?.status || 'waiting'}`))
const capabilityLabel = (name) => t(`clouds.task.capability.${name || 'unknown'}`)
const blockerLabel = (x) => x._issue ? (x.label || x.key) : capabilityLabel(x.capability)
const executionLabel = (x) => t(`clouds.task.execution.${x.execution || 'waiting'}`)
const dataLabel = (x) => t(`clouds.task.data.${x.data_state || 'unknown'}`)
const reasonLabel = (x) => {
  if (x._issue) return x.detail || x.label || x.key
  const fallback = x.execution === 'succeeded'
    ? (x.data_state === 'no_data' ? 'no_data' : 'verified')
    : (x.reason_code || 'not_verified')
  const key = `clouds.task.reason.${x.reason_code || fallback}`
  const value = t(key)
  return value === key ? (x.reason_code || t('clouds.task.reason.not_verified')) : value
}

const action = computed(() => {
  if (!props.run) return null
  const credential = hardFailures.value.find((x) => x.key === 'credential'
    || /credential|auth|access_key/i.test(x.reason_code || ''))
  if (credential) return { type: 'edit-credential', label: t('clouds.task.action.edit_credential'), admin: true }
  if (confirmations.value.length) return { type: 'confirm-logs', label: t('clouds.task.action.confirm_logs'), admin: true }
  if (failed.value.some((x) => x.retryable)) return { type: 'retry', label: t('clouds.task.action.retry'), admin: true }
  const first = ready.value[0]
  if (first) return { type: 'view', label: t('clouds.task.action.view', { capability: capabilityLabel(first.capability) }), capability: first }
  return { type: 'refresh', label: t('clouds.task.action.refresh') }
})

function evidenceWindow(capability) {
  // checked_at 是平台采集/回读时间，不是云端观测起点；没有真实 observed_from 时不得缩窄目标页窗口。
  if (capability.scope?.bill_date) {
    return { from: `${capability.scope.bill_date}T00:00:00`,
      to: capability.observed_until || `${capability.scope.bill_date}T23:59:59` }
  }
  return { from: capability.observed_from || '', to: capability.observed_until || '' }
}
function viewResult(capability) {
  const paths = { assets: '/ops', metrics: '/monitor', logs: '/logs', cost: '/cost', changes: '/changes' }
  const scope = capability.scope || {}
  const evidence = (capability.evidence_refs || []).filter(Boolean)
  const window = evidenceWindow(capability)
  router.push({
    path: paths[capability.capability] || '/clouds',
    query: {
      account_id: props.run.account_id, run_id: props.run.id,
      object_type: 'capability', object_id: capability.capability,
      ...(scope.region ? { region: scope.region } : {}),
      ...(scope.source_id ? { source_id: scope.source_id } : {}),
      ...(scope.bill_date ? { bill_date: scope.bill_date } : {}),
      ...(scope.resource_ids?.length ? { resource_id: scope.resource_ids } : {}),
      ...(window.from ? { from: window.from } : {}), ...(window.to ? { to: window.to } : {}),
      ...(evidence.length ? { evidence } : {}),
    },
  })
}
async function retry() {
  if (retrying.value || !props.canWrite) return
  const accountId = props.run.account_id
  const runId = props.run.id
  const scopes = failed.value.filter((x) => x.retryable).map((x) => ({
    capability: x.capability, region: x.scope?.region || '', source_id: x.scope?.source_id || '',
    resource_ids: x.scope?.resource_ids || [], bill_date: x.scope?.bill_date || '',
  }))
  if (!scopes.length) return
  retrying.value = true
  actionError.value = ''
  try {
    let latest = null
    // 后端每次最多 20 个 scope；30 天账单加汇总会有 31 项，必须分批而不是制造 422。
    for (let offset = 0; offset < scopes.length; offset += 20) {
      const result = await retryOnboardingCapabilities(accountId, scopes.slice(offset, offset + 20))
      latest = result.onboarding || latest
      // 用户切换账号/任务后，晚到响应只被丢弃，不覆盖当前卡。
      if (props.run?.account_id !== accountId || props.run?.id !== runId) return
    }
    if (latest && Number(latest.attempts || 1) >= Number(props.run?.attempts || 1)) {
      emit('update:run', latest)
    }
    emit('refresh', { account_id: accountId, run_id: runId })
  } catch {
    if (props.run?.account_id === accountId && props.run?.id === runId) {
      actionError.value = t('clouds.task.action_failed')
      emit('refresh', { account_id: accountId, run_id: runId })
    }
  } finally {
    retrying.value = false
  }
}
const actionAllowed = computed(() => {
  if (!action.value?.admin) return true
  return action.value.type === 'edit-credential' ? props.canEditCredentials : props.canWrite
})

function openIssueInbox() {
  // 接入失败统一交给平台自愈收件箱：不在接入卡上再造一套修复按钮。
  router.push({ path: '/agent/issues', query: props.run?.account_id ? { account_id: props.run.account_id } : {} })
}

function performAction() {
  const current = action.value
  if (!current) return
  actionError.value = ''
  if (current.type === 'retry') retry()
  else if (current.type === 'view') viewResult(current.capability)
  else if (current.type === 'refresh') emit('refresh', { account_id: props.run.account_id, run_id: props.run.id })
  else emit(current.type, props.run)
}
</script>

<template>
  <el-card shadow="never" class="task-result" :data-run-id="run?.id || ''" :data-account-id="run?.account_id || ''">
    <template #header>
      <div class="task-head">
        <div>
          <b>{{ run?.account?.name || t('clouds.task.account', { id: run?.account_id || '—' }) }}</b>
          <span v-if="run?.id" class="task-id">{{ t('clouds.task.identity', { id: run.id, attempt: run.attempts || 1 }) }}</span>
        </div>
        <el-tag :type="statusType" size="small">{{ statusLabel }}</el-tag>
      </div>
    </template>
    <div v-loading="loading && !run" class="task-body">
      <el-alert v-if="loadError" type="error" :closable="false" show-icon :title="loadError" class="task-error" />
      <div class="task-conclusion">{{ conclusion }}</div>
      <div v-if="hardFailures.length" class="task-self-heal">
        <span>{{ t('clouds.task.self_heal_hint') }}</span>
        <el-button link type="primary" size="small" @click="openIssueInbox">
          {{ t('clouds.task.self_heal_link') }}
        </el-button>
      </div>
      <div v-if="run" class="task-numbers">
        <div><b>{{ ready.length }}</b><span>{{ t('clouds.task.number.ready') }}</span></div>
        <div><b>{{ running.length + waiting.length }}</b><span>{{ t('clouds.task.number.preparing') }}</span></div>
        <div><b>{{ blockers.length }}</b><span>{{ t('clouds.task.number.blocked') }}</span></div>
      </div>
      <div v-if="blockers.length" class="task-blockers">
        <div v-for="item in blockers.slice(0, 3)" :key="scopeKey(item)">
          <b>{{ blockerLabel(item) }}</b> · {{ reasonLabel(item) }}
        </div>
      </div>
      <div v-if="actionError" class="task-action-error">{{ actionError }}</div>
      <div v-if="action" class="task-primary">
        <el-button
          v-if="actionAllowed" type="primary" :loading="retrying"
          :disabled="loading" @click="performAction"
        >{{ action.label }}</el-button>
        <span v-else class="task-admin-required">{{ t(action.type === 'edit-credential'
          ? 'clouds.task.credential_admin_required' : 'clouds.task.admin_required', { action: action.label }) }}</span>
      </div>
      <el-collapse v-if="run" class="task-evidence">
        <el-collapse-item :title="t('clouds.task.full_evidence', { n: capabilities.length + (run.issues || []).length })">
          <div class="task-capabilities">
            <div v-for="item in capabilities" :key="scopeKey(item)" class="task-capability">
              <div class="task-cap-head">
                <b>{{ capabilityLabel(item.capability) }}</b>
                <el-tag size="small" :type="item.execution === 'failed' ? 'danger' : (item.execution === 'succeeded' ? 'success' : 'info')">
                  {{ executionLabel(item) }} · {{ dataLabel(item) }}
                </el-tag>
              </div>
              <div>{{ reasonLabel(item) }}</div>
              <div v-if="item.errors?.length" class="task-errors">
                <b>{{ t('clouds.task.error_details') }}</b>
                <div v-for="(err, i) in item.errors" :key="i">{{ err }}</div>
              </div>
              <div class="task-scope">
                <span v-if="item.scope?.region">{{ t('clouds.task.scope.region', { value: item.scope.region }) }}</span>
                <span v-if="item.scope?.source_id">{{ t('clouds.task.scope.source', { value: item.scope.source_id }) }}</span>
                <span v-if="item.scope?.bill_date">{{ t('clouds.task.scope.date', { value: item.scope.bill_date }) }}</span>
                <span v-if="item.scope?.resource_ids?.length">{{ t('clouds.task.scope.resources', { n: item.scope.resource_ids.length }) }}</span>
              </div>
              <div v-if="item.checked_at" class="task-time">{{ t('clouds.task.checked_at', { time: fmtTime(item.checked_at) }) }}</div>
              <div v-for="ref in item.evidence_refs || []" :key="ref" class="task-ref">{{ ref }}</div>
            </div>
          </div>
          <div v-if="(run.issues || []).length" class="task-issues">
            <b>{{ t('clouds.task.all_failures') }}</b>
            <div v-for="issue in run.issues" :key="issue.key">
              {{ issue.label || issue.key }}<span v-if="issue.detail">：{{ issue.detail }}</span>
            </div>
          </div>
        </el-collapse-item>
      </el-collapse>
    </div>
  </el-card>
</template>

<style scoped>
.task-result { border-color: var(--el-border-color-light); }
.task-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.task-id { color: var(--el-text-color-secondary); font-size: 12px; margin-left: 10px; }
.task-conclusion { font-size: 16px; font-weight: 600; line-height: 1.6; }
.task-self-heal { color: var(--el-text-color-secondary); font-size: 13px; margin-top: 4px; }
.task-numbers { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin: 14px 0; }
.task-numbers > div { background: var(--el-fill-color-light); border-radius: 6px; padding: 9px 12px; display: flex; align-items: baseline; gap: 8px; }
.task-numbers b { font-size: 20px; }
.task-numbers span { color: var(--el-text-color-secondary); font-size: 12px; }
.task-blockers { color: var(--el-color-warning-dark-2); font-size: 13px; line-height: 1.8; margin-bottom: 10px; }
.task-primary { margin: 10px 0; }
.task-admin-required { color: var(--el-text-color-secondary); font-size: 13px; }
.task-error, .task-action-error { margin-bottom: 10px; }
.task-action-error { color: var(--el-color-danger); font-size: 13px; }
.task-evidence { margin-top: 12px; }
.task-capability { padding: 10px 0; border-bottom: 1px solid var(--el-border-color-lighter); font-size: 13px; line-height: 1.8; }
.task-cap-head { display: flex; justify-content: space-between; gap: 8px; }
.task-scope, .task-time { color: var(--el-text-color-secondary); font-size: 12px; }
.task-errors { color: var(--el-color-danger); font-size: 12px; word-break: break-all; }
.task-scope span { margin-right: 12px; }
.task-ref { font-family: monospace; font-size: 12px; word-break: break-all; }
.task-issues { padding-top: 12px; color: var(--el-color-danger); font-size: 13px; line-height: 1.8; }
@media (max-width: 640px) { .task-numbers { grid-template-columns: 1fr; } }
</style>
