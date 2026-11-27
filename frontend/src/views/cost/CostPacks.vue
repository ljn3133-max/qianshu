<script setup>
// 资源包：转按量金额=账单金额口径的日均多花（pretax_amount，判断花费以此为准）。
import { t } from '@/i18n'

const emit = defineEmits(['update:packStateFilter'])

// 状态/筛选文案按值映射 i18n（父级 Cost.vue 传入的 label/stateText 为中文，仅作兜底）
const PACK_FILTER_KEYS = {
  '': 'cost.packs.filter.all', exhausting: 'cost.packs.filter.exhausting',
  expiring: 'cost.packs.filter.expiring', ok: 'cost.packs.filter.ok',
}
const PACK_STATE_KEYS = {
  exhausted: 'cost.packs.state.exhausted', exhausting: 'cost.packs.state.exhausting',
  expiring: 'cost.packs.state.expiring', rotating: 'cost.packs.state.rotating', ok: 'cost.packs.state.ok',
}
const packStateLabel = (state) => (PACK_STATE_KEYS[state] ? t(PACK_STATE_KEYS[state]) : (props.packStateText[state] || state))
const props = defineProps(['ADVISE_TAGS', 'PACK_STATES', 'advice', 'adviceLoading', 'applicable_products', 'days_left', 'deduct_type', 'detail', 'effective_time', 'exhausted', 'exhausting', 'expiring', 'expiry_time', 'fmtDate', 'fmtQty', 'fmtYuan', 'impactTotalDaily', 'instance_id', 'items', 'level', 'money', 'note', 'packItems', 'packStateFilter', 'packStateTag', 'packStateText', 'packSummary', 'package_type', 'packsLoading', 'packsSnapshot', 'pool_days_left', 'pool_packs', 'pool_remaining_pct', 'products_label', 'rate_from_history', 'rate_pct_per_day', 'remaining_amount', 'remaining_pct', 'remaining_unit', 'round', 'scope_label', 'snapshot_date', 'state', 'status', 'title', 'total', 'total_amount', 'total_unit', 'type_label'])
</script>

<template>
        <div v-if="props.advice?.items?.length || props.adviceLoading" v-loading="adviceLoading"
             style="margin-bottom: 14px">
          <div style="font-weight: 600; margin-bottom: 8px">
            {{ t('cost.packs.advice_title') }}
            <span class="sub">{{ t('cost.packs.advice_note', { note: props.advice?.note ?? '', date: props.advice?.snapshot_date ?? '' }) }}</span>
          </div>
          <div v-for="(it, i) in (props.advice?.items || [])" :key="i" class="advice-item">
            <el-tag :type="props.ADVISE_TAGS[it.level]" size="small" effect="dark">{{ it.level === 'urgent' ? t('cost.packs.level.urgent') : it.level === 'warning' ? t('cost.packs.level.warning') : t('cost.packs.level.info') }}</el-tag>
            <b style="margin-left: 8px">{{ it.title }}</b>
            <span v-if="it.money" style="color: var(--el-color-danger); margin-left: 8px">{{ it.money }}</span>
            <div class="sub" style="margin-left: 8px">{{ it.detail }}</div>
          </div>
        </div>
        <div class="stat-cards">
          <div class="stat-card">
            <div class="stat-label">{{ t('cost.packs.stat_total') }}</div>
            <div class="stat-value">{{ props.packSummary?.total }}</div>
            <div class="stat-sub">{{ t('cost.packs.snapshot', { date: props.packsSnapshot || '-' }) }}</div>
          </div>
          <div class="stat-card">
            <div class="stat-label">{{ t('cost.packs.stat_exhausted') }}</div>
            <div class="stat-value danger">{{ props.packSummary?.exhausted }}</div>
            <div class="stat-sub">
              <template v-if="props.packSummary?.exhausted > 0">
                <template v-if="props.impactTotalDaily > 0">
                  {{ t('cost.packs.payg_impact', { v: props.fmtYuan(props.impactTotalDaily) }) }}
                </template>
                <template v-else>{{ t('cost.packs.payg_already') }}</template>
              </template>
              <template v-else>{{ t('cost.packs.no_exhausted') }}</template>
            </div>
          </div>
          <div class="stat-card">
            <div class="stat-label">{{ t('cost.packs.stat_exhausting') }}</div>
            <div class="stat-value warn">{{ props.packSummary?.exhausting }}</div>
            <div class="stat-sub">{{ t('cost.packs.exhausting_sub') }}</div>
          </div>
          <div class="stat-card">
            <div class="stat-label">{{ t('cost.packs.stat_expiring') }}</div>
            <div class="stat-value warn">{{ props.packSummary?.expiring }}</div>
            <div class="stat-sub">{{ t('cost.packs.expiring_sub') }}</div>
          </div>
        </div>

        <div class="toolbar">
          <el-radio-group :model-value="packStateFilter" @update:model-value="v => emit('update:packStateFilter', v)" size="small">
            <el-radio-button v-for="s in props.PACK_STATES" :key="s.value" :value="s.value">{{ PACK_FILTER_KEYS[s.value] ? t(PACK_FILTER_KEYS[s.value]) : s.label }}</el-radio-button>
          </el-radio-group>
          <div style="flex: 1" />
          <span class="sub">{{ t('cost.packs.rate_tip') }}</span>
        </div>

        <el-table stripe v-loading="packsLoading" :data="props.packItems" size="small" border>
          <el-table-column type="expand">
            <template #default="{ row }">
              <div class="pack-expand">
                <div>{{ t('cost.packs.expand_type', { v: row.type_label, scope: row.scope_label || '-' }) }}</div>
                <div>{{ t('cost.packs.expand_products', { label: row.products_label }) }}<span class="sub mono">（{{ row.applicable_products || '-' }}）</span></div>
                <div>{{ t('cost.packs.expand_instance') }}<span class="mono">{{ row.instance_id }}</span></div>
                <div>{{ t('cost.packs.expand_meta', { package: row.package_type || '-', deduct: row.deduct_type || '-', status: row.status || '-' }) }}</div>
                <div>
                  {{ t('cost.packs.expand_pool', { n: row.pool_packs }) }}
                  <b>{{ t('cost.packs.expand_pool_left', { pct: row.pool_remaining_pct }) }}</b>
                  <template v-if="row.pool_days_left != null">{{ t('cost.packs.expand_pool_days', { n: row.pool_days_left }) }}</template>
                  <template v-else>{{ t('cost.packs.expand_pool_idle') }}</template>
                  <span class="sub">{{ t('cost.packs.expand_pool_note') }}</span>
                </div>
                <div>{{ t('cost.packs.expand_time', { from: props.fmtDate(row.effective_time), to: props.fmtDate(row.expiry_time) }) }}</div>
              </div>
            </template>
          </el-table-column>
          <el-table-column :label="t('cost.packs.col_pack')" min-width="200">
            <template #default="{ row }">
              <b>{{ row.type_label }}</b>
              <div class="sub mono">{{ row.instance_id }}</div>
            </template>
          </el-table-column>
          <el-table-column :label="t('cost.packs.col_scope')" min-width="170" show-overflow-tooltip>
            <template #default="{ row }">
              {{ row.products_label }}
            </template>
          </el-table-column>
          <!-- 归属标识：多账号看得出属于谁 -->
          <el-table-column :label="t('cost.common.vendor_account')" width="150">
            <template #default="{ row }">
              <el-tag v-if="row.vendor" type="info" size="small" effect="plain">{{ row.vendor }}</el-tag>
              <el-tag v-if="row.account_name" type="info" size="small" effect="plain" style="margin-left: 4px">{{ row.account_name }}</el-tag>
              <span v-if="!row.vendor && !row.account_name" class="sub">—</span>
            </template>
          </el-table-column>
          <el-table-column :label="t('cost.packs.col_state')" width="100">
            <template #default="{ row }">
              <el-tag :type="props.packStateTag[row.state] || 'info'" size="small" effect="dark">
                {{ packStateLabel(row.state) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column :label="t('cost.packs.col_remaining')" width="170" align="right">
            <template #default="{ row }">
              <b>{{ props.fmtQty(row.remaining_amount) }}</b> {{ row.remaining_unit }} / {{ props.fmtQty(row.total_amount) }} {{ row.total_unit }}
            </template>
          </el-table-column>
          <el-table-column :label="t('cost.packs.col_remaining_pct')" width="180">
            <template #default="{ row }">
              <el-progress
                :percentage="Math.min(100, Math.round(row.remaining_pct * 10) / 10)"
                :stroke-width="8"
                :status="row.remaining_pct <= 5 ? 'exception' : undefined"
              />
            </template>
          </el-table-column>
          <el-table-column :label="t('cost.packs.col_rate')" width="120" align="right">
            <template #default="{ row }">
              {{ row.rate_pct_per_day > 0 ? t('cost.packs.rate_unit', { v: row.rate_pct_per_day }) : '-' }}
              <el-tag v-if="row.rate_pct_per_day > 0" size="small" type="info" effect="plain">
                {{ row.rate_from_history ? t('cost.packs.rate_measured') : t('cost.packs.rate_estimated') }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column :label="t('cost.packs.col_days_left')" width="150" align="right">
            <template #default="{ row }">
              <span v-if="row.state === 'rotating'" class="sub">
                {{ t('cost.packs.pack_days', { n: row.days_left }) }}
                <div>{{ row.pool_days_left != null ? t('cost.packs.pool_days', { n: row.pool_days_left }) : t('cost.packs.pool_enough') }}</div>
              </span>
              <span v-else-if="row.days_left != null">{{ t('cost.drill.days_short', { n: row.days_left }) }}</span>
              <span v-else class="sub">{{ t('cost.packs.infinite') }}</span>
            </template>
          </el-table-column>
          <el-table-column :label="t('cost.packs.col_expiry')" width="110" align="right">
            <template #default="{ row }">
              {{ props.fmtDate(row.expiry_time) }}
              <div class="sub">{{ row.days_to_expiry != null ? t('cost.packs.days_to_expiry', { n: row.days_to_expiry }) : '' }}</div>
            </template>
          </el-table-column>
        </el-table>
</template>

<style scoped>
.advice-item { border: 1px solid var(--el-border-color-lighter);
  border-left: 3px solid var(--el-color-warning);
  border-radius: 6px;
  padding: 8px 12px;
  margin-bottom: 6px; }
.stat-value.danger { color: var(--el-color-danger); }
.danger { color: var(--el-color-danger); }
.mono { font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 12px; }
.pack-expand { padding: 4px 12px;
  font-size: 12px;
  line-height: 1.9;
  color: var(--el-text-color-secondary); }
.stat-card { padding: 14px 16px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: var(--el-bg-color-overlay); }
.stat-cards { display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
  margin-bottom: 14px; }
.stat-label { font-size: 12px;
  color: var(--el-text-color-secondary); }
.stat-sub { font-size: 12px;
  color: var(--el-text-color-secondary); }
.stat-value { font-size: 24px;
  font-weight: 600;
  margin: 6px 0 2px; }
.stat-value.warn { color: var(--el-color-warning); }
.sub { font-size: 12px;
  color: var(--el-text-color-secondary); }
.toolbar { display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 10px; }
</style>
