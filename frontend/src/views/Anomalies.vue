<script setup>
// 汇总成本、监控、CDN、画像四域异动，行点击深链到对应处理页
// （/monitor、/cost?tab=cdn、/alerts、/profiles、/cost?tab=anomalies）。
// 总览待办与监控页入口摘要都落到本页（?domain= 预选类型域）。
// 展开行显示关联画像的漂移状态与构成变化摘要，可点回画像详情；
// ?anomaly=<id> 用于定位并展开单条异动。
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { useRoute, useRouter } from 'vue-router'

import { ackAnomalies, costAnomalies } from '@/api/cost'
import { agentAskUrl } from '@/api/agent'
import { useAuthStore } from '@/stores/auth'
import { t } from '@/i18n'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const canWrite = computed(() => ['admin', 'operator'].includes(auth.role))

const ANOMALY_TYPES = {
  payg_spike: { label: 'anomalies.type.payg_spike', tag: 'danger' },
  payg_new: { label: 'anomalies.type.payg_new', tag: 'warning' },
  pack_spike: { label: 'anomalies.type.pack_spike', tag: 'danger' },
  pack_exhausting: { label: 'anomalies.type.pack_exhausting', tag: 'warning' },
  pack_exhausted: { label: 'anomalies.type.pack_exhausted', tag: 'danger' },
  pack_expiry: { label: 'anomalies.type.pack_expiry', tag: 'warning' },
  profile_drift: { label: 'anomalies.type.profile_drift', tag: 'danger' },
  chain_drift: { label: 'anomalies.type.chain_drift', tag: 'danger' },
  host_down: { label: 'anomalies.type.host_down', tag: 'danger' },
  aux_spillover: { label: 'anomalies.type.aux_spillover', tag: 'danger' },
  traffic_spike: { label: 'anomalies.type.traffic_spike', tag: 'danger' },
  growth_accel: { label: 'anomalies.type.growth_accel', tag: 'danger' },
}
const ACK_STATUS = {
  open: { label: 'anomalies.status.open', tag: 'danger' },
  acknowledged: { label: 'anomalies.status.acknowledged', tag: 'success' },
  ignored: { label: 'anomalies.status.ignored', tag: 'info' },
  resolved: { label: 'anomalies.status.resolved', tag: 'info' },
}
const typeLabel = (ty) => ANOMALY_TYPES[ty]?.label || ty
const typeTag = (ty) => ANOMALY_TYPES[ty]?.tag || 'info'

// 类型域筛选（全部/成本/监控/CDN/画像）——CDN 是监控子集（traffic_spike），与后端 ANOMALY_TYPE_DOMAINS 同口径
const DOMAINS = [
  { value: '', label: 'anomalies.domain.all' },
  { value: 'cost', label: 'anomalies.domain.cost' },
  { value: 'monitor', label: 'anomalies.domain.monitor' },
  { value: 'cdn', label: 'anomalies.domain.cdn' },
  { value: 'profile', label: 'anomalies.domain.profile' },
]
const domain = ref('')
const statusFilter = ref('')
const query = reactive({ page: 1, page_size: 20 })
const rows = ref([])
const total = ref(0)
const loading = ref(false)

// 深链定位（?anomaly=<id>）：画像时间线/漂移事件跳过来，只取该条并高亮展开
const focusId = ref(0)
const tableRef = ref(null)

// ---- KPI：近 30 天各类异动数（点卡片=按该域筛选） ----
const kpi = ref({})
const kpiLoading = ref(false)

async function loadKpi() {
  kpiLoading.value = true
  try {
    const list = await Promise.all(DOMAINS.map((d) =>
      costAnomalies({ domain: d.value || undefined, page_size: 1 })
        .then((r) => [d.value, r.total || 0])
        // 失败显示「-」而不是 0，避免把查询失败误读成“确实没有异动”
        .catch(() => [d.value, null]),
    ))
    kpi.value = Object.fromEntries(list)
  } finally {
    kpiLoading.value = false
  }
}

async function load() {
  loading.value = true
  try {
    const params = { ...query }
    if (domain.value) params.domain = domain.value
    if (statusFilter.value) params.status = statusFilter.value
    if (focusId.value) params.anomaly_id = focusId.value
    const data = await costAnomalies(params)
    rows.value = data.items || []
    total.value = data.total || 0
    if (focusId.value) {
      await nextTick()
      const row = rows.value.find((x) => x.id === focusId.value)
      if (row && tableRef.value) tableRef.value.toggleRowExpansion(row, true)
    }
  } finally {
    loading.value = false
  }
}

function rowClass({ row }) {
  return focusId.value && row.id === focusId.value ? 'row-focus' : ''
}

// 翻页/筛选驱动加载：domain 与 status 变更先回第 1 页（page 变了由翻页 watch 加载，避免双请求）
function reloadFirstPage() {
  if (query.page !== 1) query.page = 1   // watch(query.page) 会触发 load
  else load()
}

function setDomain(d) {
  domain.value = d
  reloadFirstPage()
}

// 状态列点选：再点同状态=取消筛选
function setStatus(s) {
  statusFilter.value = statusFilter.value === s ? '' : s
}

watch(statusFilter, reloadFirstPage)
watch(() => query.page, load)
// el-pagination 的 v-model:page-size 只更新值，不会自动重查；
// 切换条数与翻页一样回到第 1 页并重查，避免表格仍保留旧条数。
watch(() => query.page_size, reloadFirstPage)
// 总览/监控页入口带 ?domain= 过来：预选类型域
watch(() => route.query.domain, (d) => {
  const want = d ? String(d) : ''
  if (want !== domain.value && DOMAINS.some((x) => x.value === want)) setDomain(want)
})

// ---- 行点击深链跳对应域（与监控页入口/总览待办同口径） ----
function onRowClick(row, _col, ev) {
  // 展开箭头只展开内容，不触发跳转（否则点展开会误跳走）
  if (ev?.target?.closest?.('.el-table__expand-icon')) return
  gotoAnomaly(row)
}

function gotoAnomaly(a) {
  const key = a.target_key || ''
  if (a.type === 'traffic_spike') {
    router.push({ path: '/cost', query: { tab: 'cdn', domain: key.split(':').slice(1).join(':'), anomaly: a.id } })
  } else if (a.type === 'growth_accel') {
    router.push({
      path: '/monitor',
      query: { product: key.startsWith('rds_space') ? 'rds' : 'ecs', instance: key.split(':').slice(1).join(':') },
    })
  } else if (a.type === 'host_down' || a.type === 'aux_spillover') {
    router.push({ path: '/alerts', query: { tab: 'records' } })
  } else if (a.type === 'profile_drift') {
    // 带画像 key 深链回画像详情，#fast 后缀同属该画像
    router.push({ path: '/profiles', query: { profile: key.replace(/#fast$/, '') } })
  } else if (a.type === 'chain_drift') {
    // 全景对象（跨层联动）：对象 key = target_key 去掉 type 前缀
    router.push({ path: '/profiles', query: { tab: 'object', object: key.split(':').slice(1).join(':') } })
  } else {
    // 成本类（payg_*/pack_*）：落成本异动页签并定位该条
    router.push({ path: '/cost', query: { tab: 'anomalies', anomaly: a.id } })
  }
}

// 关联画像条目 -> 画像详情（与 profile_drift 跳转同口径）
function gotoProfile(pl) {
  if (!pl?.profile_key) return
  router.push({ path: '/profiles', query: { profile: pl.profile_key } })
}

// 问智能体深链：异动条目带对象上下文落到智能体新会话首条
function askAgent(row) {
  const q = t('anomalies.ask.prompt', {
    title: row.title,
    obj: row.target_name || row.target_key,
    type: t(typeLabel(row.type)),
    level: row.level === 'critical' ? t('anomalies.ask.level_suffix') : '',
  })
  window.open(agentAskUrl(q, {
    page: '/anomalies', anomaly_id: row.id, type: row.type,
    target_key: row.target_key, target_name: row.target_name, level: row.level,
  }), '_blank')
}

// 生命周期：确认/忽略/重开（view 角色只读）
async function ackRow(row, status) {
  try {
    await ackAnomalies({
      ids: [row.id], status,
      note: status === 'acknowledged' ? t('anomalies.note.ack')
        : status === 'ignored' ? t('anomalies.note.ignore') : t('anomalies.note.reopen'),
    })
    ElMessage.success(status === 'open' ? t('anomalies.msg.reopened')
      : status === 'acknowledged' ? t('anomalies.msg.acknowledged') : t('anomalies.msg.ignored'))
    loadKpi()
    load()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('anomalies.msg.fail'))
  }
}

// 总览/监控页/画像时间线深链：?domain= 预选类型域，?anomaly=<id> 定位单条
watch(() => route.query.anomaly, (v) => {
  const id = v ? Number(v) : 0
  if (id !== focusId.value) {
    focusId.value = id
    reloadFirstPage()
  }
})

onMounted(() => {
  const d = route.query.domain ? String(route.query.domain) : ''
  if (d && d !== domain.value && DOMAINS.some((x) => x.value === d)) domain.value = d  // 不走 setDomain，下面统一 load
  focusId.value = route.query.anomaly ? Number(route.query.anomaly) : 0
  loadKpi()
  load()
})
</script>

<template>
  <div class="page">
    <!-- KPI：近 30 天各类异动数 -->
    <div v-loading="kpiLoading" class="kpi-row">
      <div
        v-for="d in DOMAINS"
        :key="d.value"
        class="kpi-card"
        :class="{ active: domain === d.value }"
        @click="setDomain(d.value)"
      >
        <div class="kpi-num">{{ kpi[d.value] ?? '-' }}</div>
        <div class="kpi-label">{{ t(d.label) }}</div>
      </div>
      <div class="kpi-hint">{{ t('anomalies.kpi.hint') }}</div>
    </div>

    <div class="toolbar">
      <el-radio-group :model-value="domain" @change="setDomain">
        <el-radio-button v-for="d in DOMAINS" :key="d.value" :value="d.value">{{ t(d.label) }}</el-radio-button>
      </el-radio-group>
      <el-select v-model="statusFilter" :placeholder="t('anomalies.toolbar.status_ph')" clearable style="width: 150px">
        <el-option v-for="(v, k) in ACK_STATUS" :key="k" :value="k" :label="t(v.label)" />
      </el-select>
      <el-button :loading="loading" @click="loadKpi(); load()">{{ t('anomalies.toolbar.refresh') }}</el-button>
      <div style="flex: 1" />
      <span class="sub">{{ t('anomalies.toolbar.hint') }}</span>
    </div>

    <el-table
      ref="tableRef"
      v-loading="loading"
      :data="rows"
      border
      stripe
      size="default"
      class="anomaly-table"
      :row-class-name="rowClass"
      @row-click="onRowClick"
    >
      <el-table-column type="expand">
        <template #default="{ row }">
          <div class="anomaly-content">{{ row.content }}</div>
          <div v-if="row.ack_note" class="anomaly-content sub">{{ t('anomalies.expand.note', { note: row.ack_note }) }}</div>
          <div class="anomaly-content sub">{{ t('anomalies.expand.basis') }}</div>
          <!-- 异动详情带关联画像的漂移状态与构成变化摘要，点击可回画像详情 -->
          <div v-if="row.profile_links?.length" class="anomaly-content">
            <div class="sub" style="margin-bottom: 4px">{{ t('anomalies.expand.profiles_title') }}</div>
            <div v-for="pl in row.profile_links" :key="pl.profile_key" class="link-item" @click.stop="gotoProfile(pl)">
              <el-tag :type="pl.status === 'drift' ? 'danger' : pl.status === 'known' ? 'warning' : pl.status === 'normal' ? 'success' : 'info'" size="small">
                {{ pl.status_label }}
              </el-tag>
              <b class="link-label">{{ pl.label }}</b>
              <span class="link-summary">{{ pl.summary }}</span>
              <span v-if="pl.drift_events?.length" class="sub">{{ t('anomalies.expand.drift_events', { n: pl.drift_events.length }) }}</span>
              <span class="link-go">{{ t('anomalies.expand.goto_profile') }}</span>
            </div>
          </div>
        </template>
      </el-table-column>
      <el-table-column :label="t('anomalies.col.type')" width="130">
        <template #default="{ row }">
          <el-tag :type="typeTag(row.type)" size="small" effect="plain">{{ t(typeLabel(row.type)) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('anomalies.col.target')" width="220" show-overflow-tooltip>
        <template #default="{ row }">
          <!-- 显示账号归属，便于多账号场景识别对象 -->
          <el-tag v-if="row.account" type="info" size="small" effect="plain" style="margin-right: 4px">{{ row.account }}</el-tag>
          <div>{{ row.target_name || row.target_key }}</div>
          <div v-if="row.target_name" class="sub mono">{{ row.target_key }}</div>
        </template>
      </el-table-column>
      <el-table-column :label="t('anomalies.col.summary')" min-width="260" show-overflow-tooltip>
        <template #default="{ row }">
          <div>{{ row.title }}</div>
          <div class="sub">
            <el-tag v-for="b in row.biz || []" :key="b" type="warning" size="small" effect="dark" class="biz-tag">{{ b }}</el-tag>
            <span v-if="row.level === 'critical'" class="danger">{{ t('anomalies.level.critical') }}</span>
            <span v-if="row.type === 'payg_spike' || row.type === 'payg_new'" :title="t('anomalies.amount_note_tip')">{{ t('anomalies.amount_note') }}</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column :label="t('anomalies.col.time')" width="170">
        <template #default="{ row }">
          <div>{{ row.last_seen }}</div>
          <div v-if="row.last_seen !== row.detect_date" class="sub">{{ t('anomalies.col.first_seen', { date: row.detect_date }) }}</div>
        </template>
      </el-table-column>
      <el-table-column :label="t('anomalies.col.status')" width="90">
        <template #default="{ row }">
          <!-- 状态列可直接点选筛选，与顶部状态下拉共用筛选条件 -->
          <el-tooltip :content="t('anomalies.toolbar.status_tip')" placement="top" :show-after="400">
            <el-tag
              :type="ACK_STATUS[row.ack_status]?.tag || 'info'"
              size="small"
              class="status-filter-tag"
              @click.stop="setStatus(row.ack_status)"
            >
              {{ t(ACK_STATUS[row.ack_status]?.label || row.ack_status) }}
            </el-tag>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column :label="t('anomalies.col.ask')" width="100" fixed="right">
        <template #default="{ row }">
          <el-button size="small" text type="primary" @click.stop="askAgent(row)">{{ t('anomalies.action.ask') }}</el-button>
        </template>
      </el-table-column>
      <el-table-column v-if="canWrite" :label="t('anomalies.col.actions')" width="170" fixed="right">
        <template #default="{ row }">
          <el-button v-if="row.ack_status !== 'acknowledged'" size="small" @click.stop="ackRow(row, 'acknowledged')">{{ t('anomalies.action.ack') }}</el-button>
          <el-button v-if="row.ack_status !== 'ignored'" size="small" type="info" plain
                     @click.stop="ackRow(row, 'ignored')">{{ t('anomalies.action.ignore') }}</el-button>
          <el-button v-if="row.ack_status !== 'open'" size="small" type="warning" plain
                     @click.stop="ackRow(row, 'open')">{{ t('anomalies.action.reopen') }}</el-button>
        </template>
      </el-table-column>
    </el-table>

    <div class="pager">
      <el-pagination
        v-model:current-page="query.page"
        v-model:page-size="query.page_size"
        :total="total"
        :page-sizes="[20, 50, 100]"
        layout="total, sizes, prev, pager, next"
      />
    </div>
  </div>
</template>

<style scoped>
.page {
  padding: 16px;
}
.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.danger {
  color: var(--el-color-danger);
  font-size: 12px;
  margin-left: 6px;
}
.mono {
  font-family: monospace;
}
.biz-tag {
  margin-right: 4px;
}

/* KPI */
.kpi-row {
  display: flex;
  gap: 12px;
  align-items: center;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.kpi-card {
  min-width: 110px;
  padding: 12px 18px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: var(--el-bg-color);
  cursor: pointer;
  text-align: center;
  transition: border-color 0.2s, box-shadow 0.2s;
}
.kpi-card:hover {
  box-shadow: var(--el-box-shadow-light);
}
.kpi-card.active {
  border-color: var(--el-color-primary);
}
.kpi-num {
  font-size: 22px;
  font-weight: 700;
  line-height: 1.2;
}
.kpi-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-top: 2px;
}
.kpi-hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-left: 4px;
}

/* 工具条 */
.toolbar {
  display: flex;
  gap: 12px;
  margin-bottom: 12px;
  align-items: center;
  flex-wrap: wrap;
}

/* 表格 */
.anomaly-table :deep(.el-table__row) {
  cursor: pointer;
}
.anomaly-content {
  padding: 8px 16px;
  font-size: 12px;
  line-height: 1.7;
  white-space: pre-wrap;
}

/* 关联画像（异动详情 -> 画像） */
.link-item {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
  padding: 6px 8px;
  margin-bottom: 6px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  cursor: pointer;
}
.link-item:hover {
  border-color: var(--el-color-primary);
}
.link-label {
  font-size: 13px;
}
.link-summary {
  font-size: 12px;
  color: var(--el-text-color-regular);
}
.link-go {
  font-size: 12px;
  color: var(--el-color-primary);
  margin-left: auto;
}
.anomaly-table :deep(.row-focus) > td {
  background: var(--el-color-primary-light-9);
}
.status-filter-tag {
  cursor: pointer;
}
.status-filter-tag:hover {
  filter: brightness(0.95);
}
.pager {
  margin-top: 12px;
  display: flex;
  justify-content: flex-end;
}
</style>
