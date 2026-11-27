<script setup>
// 首屏与智能体共用 C1；这里只做展示投影，不再请求或计算第二套成本事实。
import { computed, ref, watch } from 'vue'

import MetricChart from '@/components/MetricChart.vue'
import CostPie from '@/views/CostPie.vue'
import { t } from '@/i18n'

const emit = defineEmits(['update:detailVisible', 'update:detailPage'])
const props = defineProps({
  insights: { type: Object, default: null },
  insightsLoading: { type: Boolean, default: false },
  fmtDate: { type: Function, required: true },
  fmtMoney: { type: Function, required: true },
  fmtQty: { type: Function, required: true },
  detailError: { type: String, default: '' },
  detailInstance: { type: Object, default: null },
  detailLoading: { type: Boolean, default: false },
  detailQuery: { type: Object, required: true },
  detailRows: { type: Array, default: () => [] },
  detailTotal: { type: Number, default: 0 },
  detailVisible: { type: Boolean, default: false },
  exportDetail: { type: Function, required: true },
  exportingDetail: { type: Boolean, default: false },
  openDetail: { type: Function, required: true },
  askAgent: { type: Function, required: true },
  agentStarting: { type: Boolean, default: false },
  canAskAgent: { type: Boolean, default: false },
})

const selectedKey = ref('')
const productFilter = ref('')
const accounts = computed(() => [
  ...(props.insights?.accounts || []),
  ...(props.insights?.unattributed || []),
])
const accountKey = (item) => `${item.account?.unattributed ? 'unattributed' : 'account'}:${item.account?.id}`
const detailAccount = computed(() => accounts.value.find((x) => accountKey(x) === selectedKey.value)
  || accounts.value[0] || null)
watch(accounts, (items) => {
  if (!items.some((x) => accountKey(x) === selectedKey.value)) {
    selectedKey.value = items[0] ? accountKey(items[0]) : ''
    productFilter.value = ''
  }
}, { immediate: true })

const money = (value) => value == null ? '—' : props.fmtMoney(value)
const objectLabel = (obj) => [obj?.product_name || obj?.product_code,
  obj?.instance_name || obj?.instance_id, obj?.billing_item].filter(Boolean).join(' / ') || '—'
const statusType = (item) => ({
  available: item.anomalies?.length ? 'warning' : 'success',
  partial: 'warning', no_data: 'info', not_applicable: 'info',
}[item.status] || 'info')
const conclusion = (item) => {
  if (item.status === 'not_applicable') return t('cost.insight.conclusion.na')
  if (item.status === 'no_data') return t('cost.insight.conclusion.no_data')
  if (item.status === 'partial') return t('cost.insight.conclusion.partial')
  if (item.anomalies?.length) return t('cost.insight.conclusion.anomalies', { n: item.anomalies.length })
  return t('cost.insight.conclusion.verified')
}
const anomalyAnswer = (item) => {
  const finding = item.anomalies?.[0]
  if (finding) return t('cost.insight.anomaly.value', {
    object: objectLabel(finding.object), value: money(finding.value), baseline: money(finding.baseline),
  })
  if (item.analysis?.anomaly_status === 'no_anomaly_in_verified_scope') return t('cost.insight.anomaly.none')
  if (item.status === 'no_data' || item.status === 'not_applicable') return t('cost.insight.anomaly.no_evidence')
  return t('cost.insight.anomaly.unverified')
}
const topProduct = (item) => item.products?.[0] || null
const emptyAmountText = (item) => {
  if (item.totals?.total === 0 && item.coverage?.complete) return t('cost.insight.verified_zero')
  if (item.totals?.total != null) return t('cost.insight.no_product_rank')
  return t('cost.insight.no_verified_amount')
}
const firstRecommendation = (item) => item.recommendations?.[0] || null
const coverageText = (item) => t('cost.insight.coverage.summary', {
  complete: item.coverage?.complete_days || 0, requested: item.coverage?.requested_days || 0,
})
const historyText = (finding) => finding.history_range?.from
  ? `${finding.history_range.from} ~ ${finding.history_range.to}` : t('cost.insight.history.insufficient')

const trendSeries = computed(() => {
  const days = detailAccount.value?.days || []
  return [
    { name: t('cost.insight.series.payg'), points: days.filter((d) => d.payg != null).map((d) => [d.date, d.payg]) },
    { name: t('cost.insight.series.amortized'), points: days.filter((d) => d.amortized != null).map((d) => [d.date, d.amortized]) },
  ].filter((s) => s.points.length)
})
const trendMarks = computed(() => {
  const days = Object.fromEntries((detailAccount.value?.days || []).map((d) => [d.date, d.payg]))
  return (detailAccount.value?.anomalies || []).filter((a) => days[a.date] != null).map((a) => ({
    ts: new Date(`${a.date}T00:00:00`).getTime(), value: days[a.date], label: `×${a.ratio}`,
  }))
})
const productSeries = computed(() => {
  const rows = detailAccount.value?.products || []
  const top = rows.slice(0, 8).map((p) => ({ name: p.object.product_name || p.object.product_code, value: p.total }))
  const rest = rows.slice(8).reduce((sum, p) => sum + (p.total || 0), 0)
  if (rest > 0.01) top.push({ name: t('cost.insight.other'), value: Math.round(rest * 100) / 100 })
  return top
})
const filteredInstances = computed(() => (detailAccount.value?.instances || [])
  .filter((x) => !productFilter.value || x.object.product_code === productFilter.value)
  .slice(0, 15))

function openInstance(row) {
  const account = detailAccount.value
  if (!account) return
  props.openDetail({
    instance_id: row.object.instance_id, instance_name: row.object.instance_name,
    product_code: row.object.product_code,
  }, account.account.id)
}
function locateFinding(account, finding) {
  selectedKey.value = accountKey(account)
  productFilter.value = finding.object?.product_code || ''
  props.openDetail({
    instance_id: finding.object?.instance_id || '', instance_name: finding.object?.instance_name || '',
    product_code: finding.object?.product_code || '',
  }, account.account.id)
}
</script>

<template>
  <div v-loading="insightsLoading && !insights" class="insight-overview">
    <el-card
      v-for="item in accounts" :key="accountKey(item)" shadow="never"
      class="task-result insight-card" :data-insight-account="item.account.id"
      :data-unattributed="item.account.unattributed ? 'true' : 'false'" :data-status="item.status"
    >
      <template #header>
        <div class="task-head">
          <div>
            <b>{{ item.account.unattributed ? t('cost.insight.unattributed', { id: item.account.id }) : item.account.name }}</b>
            <span class="task-id">{{ item.range.start }} ~ {{ item.range.end }}</span>
          </div>
          <el-tag :type="statusType(item)" size="small">{{ t(`cost.insight.status.${item.status}`) }}</el-tag>
        </div>
      </template>
      <div class="task-conclusion">{{ conclusion(item) }}</div>
      <div class="insight-answers">
        <section data-answer="spend">
          <h3>{{ t('cost.insight.answer.spend') }}</h3>
          <div class="insight-main-number">{{ money(item.totals.total) }}</div>
          <template v-if="topProduct(item)">
            <b>{{ topProduct(item).object.product_name || topProduct(item).object.product_code }}</b>
            <div>{{ money(topProduct(item).total) }} · {{ topProduct(item).share_pct ?? '—' }}%</div>
          </template>
          <div v-else class="sub">{{ emptyAmountText(item) }}</div>
        </section>
        <section data-answer="anomaly">
          <h3>{{ t('cost.insight.answer.anomaly') }}</h3>
          <div class="insight-answer-text">{{ anomalyAnswer(item) }}</div>
          <div v-if="item.observations?.length" class="sub">
            {{ t('cost.insight.first_observed', { n: item.observations.length }) }}
          </div>
        </section>
        <section data-answer="action">
          <h3>{{ t('cost.insight.answer.action') }}</h3>
          <template v-if="firstRecommendation(item)">
            <b>{{ firstRecommendation(item).title }}</b>
            <div class="insight-reason">{{ firstRecommendation(item).reason }}</div>
          </template>
          <div v-else class="insight-answer-text">{{ t('cost.insight.no_recommendation') }}</div>
        </section>
      </div>
      <div class="insight-coverage">{{ coverageText(item) }} · {{ t(`cost.insight.reason.${item.reason_code}`) }}</div>
      <div v-if="!item.account.unattributed && canAskAgent" class="task-primary">
        <el-button type="primary" :loading="agentStarting" @click="props.askAgent(item)">{{ t('cost.insight.ask_agent') }}</el-button>
      </div>
      <el-collapse class="task-evidence">
        <el-collapse-item :title="t('cost.insight.full_evidence')">
          <div class="evidence-block">
            <b>{{ t('cost.insight.coverage.title') }}</b>
            <div>{{ coverageText(item) }} · {{ t('cost.insight.collected_at', { time: item.coverage.collected_at || '—' }) }}</div>
            <div v-if="item.coverage.missing_bill_dates?.length">
              {{ t('cost.insight.missing_bill') }}：{{ item.coverage.missing_bill_dates.join(', ') }}
            </div>
            <div v-if="item.coverage.missing_amortized_dates?.length">
              {{ t('cost.insight.missing_amortized') }}：{{ item.coverage.missing_amortized_dates.join(', ') }}
            </div>
          </div>
          <div v-if="item.anomalies?.length" class="evidence-block">
            <b>{{ t('cost.insight.answer.anomaly') }}</b>
            <div v-for="finding in item.anomalies" :key="finding.id" class="finding-row">
              <span>{{ finding.date }} · {{ objectLabel(finding.object) }} · {{ money(finding.value) }} / {{ t('cost.insight.baseline') }} {{ money(finding.baseline) }}</span>
              <span class="sub">{{ t('cost.insight.history') }}：{{ historyText(finding) }}</span>
              <el-button text type="primary" @click="locateFinding(item, finding)">{{ t('cost.insight.view_bill') }}</el-button>
              <div v-for="ref in finding.evidence_refs" :key="ref" class="task-ref">{{ ref }}</div>
            </div>
          </div>
          <div v-if="item.recommendations?.length" class="evidence-block">
            <b>{{ t('cost.insight.answer.action') }}</b>
            <div v-for="rec in item.recommendations" :key="rec.id" class="finding-row">
              <span>{{ rec.title }} · {{ money(rec.impact_amount) }}</span>
              <div>{{ rec.reason }}</div>
              <div class="sub">{{ rec.time_range.from }} ~ {{ rec.time_range.to }} · {{ t('cost.insight.history') }}：{{ historyText(rec) }}</div>
              <el-button text type="primary" @click="locateFinding(item, rec)">{{ t('cost.insight.view_bill') }}</el-button>
              <div v-for="ref in rec.evidence_refs" :key="ref" class="task-ref">{{ ref }}</div>
            </div>
          </div>
        </el-collapse-item>
      </el-collapse>
    </el-card>
    <el-empty v-if="insights && !accounts.length" :description="t('cost.insight.no_accounts')" />

    <template v-if="detailAccount">
      <div v-if="accounts.length > 1" class="detail-account-switch">
        <span>{{ t('cost.insight.detail_account') }}</span>
        <el-radio-group v-model="selectedKey" size="small" @change="productFilter = ''">
          <el-radio-button v-for="item in accounts" :key="accountKey(item)" :value="accountKey(item)">
            {{ item.account.unattributed ? t('cost.common.unassigned') : item.account.name }}
          </el-radio-button>
        </el-radio-group>
      </div>
      <div class="charts-row">
        <div class="chart-card wide">
          <MetricChart v-if="trendSeries.length" :title="t('cost.ov.trend_title')" :series="trendSeries" :marks="trendMarks" :height="260" />
          <div v-else class="empty-chart">{{ emptyAmountText(detailAccount) }}</div>
          <div v-if="trendMarks.length" class="marks-legend sub">{{ t('cost.ov.marks_legend') }}</div>
        </div>
        <div class="chart-card">
          <div class="pie-title">{{ t('cost.ov.pie_title') }}</div>
          <CostPie v-if="productSeries.length" :rows="productSeries" />
          <div v-else class="empty-chart">{{ emptyAmountText(detailAccount) }}</div>
        </div>
      </div>

      <el-table v-if="detailAccount.products.length" stripe :data="detailAccount.products.slice(0, 10)" size="small" class="product-table">
        <el-table-column :label="t('cost.ov.col_product')" min-width="180">
          <template #default="{ row }">{{ row.object.product_name || row.object.product_code }}</template>
        </el-table-column>
        <el-table-column :label="t('cost.ov.col_total')" width="140" align="right"><template #default="{ row }">{{ money(row.total) }}</template></el-table-column>
        <el-table-column :label="t('cost.ov.col_payg')" width="120" align="right"><template #default="{ row }">{{ money(row.payg) }}</template></el-table-column>
        <el-table-column :label="t('cost.ov.col_amortized')" width="120" align="right"><template #default="{ row }">{{ money(row.amortized) }}</template></el-table-column>
        <el-table-column :label="t('cost.ov.col_order')" width="130" align="right"><template #default="{ row }">{{ money((row.order || 0) + (row.refund || 0)) }}</template></el-table-column>
        <el-table-column :label="t('cost.ov.col_share')" width="200">
          <template #default="{ row }"><el-progress :percentage="Math.max(0, Math.min(100, Math.round(row.share_pct || 0)))" :stroke-width="8" /></template>
        </el-table-column>
      </el-table>

      <div class="section-title">{{ t('cost.ov.instance_title') }}</div>
      <div class="toolbar">
        <el-select v-model="productFilter" clearable filterable :placeholder="t('cost.ov.product_filter_ph')" style="width: 240px">
          <el-option v-for="p in detailAccount.products" :key="p.object.product_code" :label="p.object.product_name || p.object.product_code" :value="p.object.product_code" />
        </el-select>
        <span class="sub">{{ t('cost.ov.click_row_tip') }}</span>
      </div>
      <el-table stripe :data="filteredInstances" border @row-click="openInstance">
        <el-table-column :label="t('cost.ov.col_instance')" min-width="220">
          <template #default="{ row }">{{ row.object.instance_name || row.object.instance_id }}<div class="sub mono">{{ row.object.instance_id }}</div></template>
        </el-table-column>
        <el-table-column :label="t('cost.ov.col_product')" width="140"><template #default="{ row }">{{ row.object.product_name || row.object.product_code }}</template></el-table-column>
        <el-table-column :label="t('cost.ov.col_total')" width="140" align="right"><template #default="{ row }">{{ money(row.total) }}</template></el-table-column>
        <el-table-column :label="t('cost.ov.col_order_short')" width="120" align="right"><template #default="{ row }">{{ money((row.order || 0) + (row.refund || 0)) }}</template></el-table-column>
        <el-table-column :label="t('cost.insight.evidence_days')" width="100" align="right"><template #default="{ row }">{{ row.evidence_refs.length }}</template></el-table-column>
      </el-table>
    </template>

    <el-drawer
      :model-value="props.detailVisible" @update:model-value="v => emit('update:detailVisible', v)"
      :title="t('cost.ov.detail_title', { name: props.detailInstance?.instance_name || props.detailInstance?.instance_id || '' })" size="720px"
    >
      <div class="sub" style="margin-bottom: 8px">{{ t('cost.ov.detail_note') }}</div>
      <el-button size="small" :loading="props.exportingDetail" style="margin-bottom: 8px" @click="props.exportDetail(props.detailInstance?.instance_id || '', props.detailInstance?.account_id, props.detailInstance?.product_code || '')">
        {{ t('cost.btn.export_detail') }}
      </el-button>
      <el-alert v-if="props.detailError" type="error" :closable="false" show-icon :title="props.detailError" style="margin-bottom: 8px" />
      <el-table stripe :data="props.detailRows" v-loading="props.detailLoading" size="small" border :empty-text="props.detailError ? t('cost.load.failed') : undefined">
        <el-table-column :label="t('cost.ov.col_date')" width="110"><template #default="{ row }">{{ props.fmtDate(row.stat_date) }}</template></el-table-column>
        <el-table-column prop="product_name" :label="t('cost.ov.col_product')" width="120" show-overflow-tooltip />
        <el-table-column :label="t('cost.common.vendor_account')" width="130">
          <template #default="{ row }"><el-tag v-if="row.vendor" type="info" size="small" effect="plain">{{ row.vendor }}</el-tag><el-tag v-if="row.account_name" type="info" size="small" effect="plain" style="margin-left: 4px">{{ row.account_name }}</el-tag><span v-if="!row.vendor && !row.account_name" class="sub">—</span></template>
        </el-table-column>
        <el-table-column prop="billing_item" :label="t('cost.ov.col_billing_item')" min-width="150" show-overflow-tooltip />
        <el-table-column prop="subscription_type" :label="t('cost.ov.col_billing_mode')" width="110" />
        <el-table-column :label="t('cost.ov.col_amount')" width="110" align="right"><template #default="{ row }"><b>{{ props.fmtMoney(row.pretax_amount) }}</b></template></el-table-column>
        <el-table-column :label="t('cost.ov.col_usage')" width="90" align="right"><template #default="{ row }">{{ props.fmtQty(row.usage) }}</template></el-table-column>
      </el-table>
      <div class="pager">
        <el-pagination :current-page="props.detailQuery.page" :page-size="props.detailQuery.page_size" :total="props.detailTotal" layout="total, prev, pager, next" background @update:current-page="v => emit('update:detailPage', v)" />
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.insight-overview { min-width: 0; }
.task-result.insight-card { border-color: var(--el-border-color-light); margin-bottom: 12px; }
.task-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.task-id { color: var(--el-text-color-secondary); font-size: 12px; margin-left: 10px; }
.task-conclusion { font-size: 16px; font-weight: 600; line-height: 1.6; }
.insight-answers { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin: 14px 0; }
.insight-answers section { background: var(--el-fill-color-light); border-radius: 6px; padding: 12px; line-height: 1.7; }
.insight-answers h3 { margin: 0 0 8px; font-size: 13px; color: var(--el-text-color-secondary); }
.insight-main-number { font-size: 24px; font-weight: 600; margin-bottom: 6px; }
.insight-answer-text, .insight-reason { font-size: 13px; line-height: 1.7; }
.insight-coverage { color: var(--el-text-color-secondary); font-size: 12px; }
.task-primary { margin: 10px 0; }
.task-evidence { margin-top: 12px; }
.evidence-block { padding: 8px 0 12px; font-size: 13px; line-height: 1.8; }
.finding-row { padding: 8px 0; border-bottom: 1px solid var(--el-border-color-lighter); }
.task-ref { font-family: monospace; font-size: 12px; word-break: break-all; color: var(--el-text-color-secondary); }
.detail-account-switch { display: flex; align-items: center; gap: 12px; margin: 18px 0 12px; }
.chart-card { border: 1px solid var(--el-border-color-lighter); border-radius: 8px; padding: 8px; background: var(--el-bg-color-overlay); }
.charts-row { display: grid; grid-template-columns: 2fr 1fr; gap: 12px; margin-bottom: 14px; }
.empty-chart { height: 240px; display: flex; align-items: center; justify-content: center; color: var(--el-text-color-secondary); font-size: 13px; }
.marks-legend { padding: 0 8px 4px; }
.mono { font-family: 'JetBrains Mono', Consolas, monospace; font-size: 12px; }
.pager { display: flex; justify-content: flex-end; margin-top: 10px; }
.pie-title { font-size: 13px; font-weight: 500; padding: 6px 8px 0; }
.product-table { margin-bottom: 18px; }
.section-title { font-size: 14px; font-weight: 600; margin: 6px 0 10px; }
.sub { font-size: 12px; color: var(--el-text-color-secondary); }
.toolbar { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }
.wide { min-width: 140px; }
@media (max-width: 800px) { .insight-answers, .charts-row { grid-template-columns: 1fr; } }
</style>
