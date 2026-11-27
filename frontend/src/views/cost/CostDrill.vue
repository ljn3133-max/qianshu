<script setup>
// 昨日拆解：金额=账单金额（pretax_amount，判断花费以此为准）。
// 产品/场景/实例表按产品×账号拆行并带厂商/账号归属；花钱产品数按产品名去重，拆行后行数≠产品数。
import { computed } from 'vue'
import { t } from '@/i18n'

const emit = defineEmits(['update:drillProduct', 'update:drillInstances'])
const props = defineProps(['amount', 'available', 'billing_items', 'consumers', 'date', 'days_left', 'delta_pct', 'drill', 'drillInstances', 'drillLoading', 'drillProduct', 'drillProductClick', 'drillScenarios', 'fmtYuan2', 'instance_id', 'instances', 'name', 'note', 'packs', 'pct', 'pool_pct', 'prev_total', 'products', 'scenario_note', 'total', 'unit', 'why'])

const drillProductCount = computed(() => new Set((props.drill?.products || []).map((p) => p.product)).size)
</script>

<template>
        <div v-if="props.drill&& !props.drill?.available" class="sub">{{ props.drill?.note }}</div>
        <template v-else-if="props.drill">
          <div v-loading="props.drillLoading" class="stat-cards">
            <div class="stat-card">
              <div class="stat-label">{{ t('cost.drill.stat_payg') }}</div>
              <div class="stat-value">{{ props.fmtYuan2(props.drill?.total) }}</div>
              <div class="stat-sub">
                {{ t('cost.drill.vs_prev', { date: props.drill?.date ?? '', v: props.fmtYuan2(props.drill?.prev_total) }) }}
                <span v-if="props.drill?.delta_pct != null" :style="{ color: props.drill?.delta_pct > 0 ? '#f56c6c' : '#67c23a' }">
                  {{ props.drill?.delta_pct > 0 ? '↑' : '↓' }}{{ Math.abs(props.drill?.delta_pct) }}%
                </span>
              </div>
            </div>
            <div class="stat-card">
              <div class="stat-label">{{ t('cost.drill.stat_products') }}</div>
              <div class="stat-value">{{ drillProductCount }}</div>
              <div class="stat-sub">{{ t('cost.drill.click_product_tip') }}</div>
            </div>
            <div class="stat-card">
              <div class="stat-label">{{ t('cost.drill.stat_instances') }}</div>
              <div class="stat-value">{{ props.drill?.instances.length }}</div>
              <div class="stat-sub">{{ props.drillProduct ? t('cost.drill.filtered', { p: props.drillProduct }) : t('cost.drill.instances_tip') }}</div>
            </div>
          </div>

          <div class="sec-title">{{ t('cost.drill.product_title') }}</div>
          <el-table stripe :data="props.drill?.products" size="small" border highlight-current-row
                    style="cursor: pointer" @row-click="props.drillProductClick">
            <el-table-column prop="product" :label="t('cost.drill.col_product')" min-width="180" />
            <!-- 归属标识：多账号看得出属于谁 -->
            <el-table-column :label="t('cost.common.vendor_account')" width="150">
              <template #default="{ row }">
                <el-tag v-if="row.vendor" type="info" size="small" effect="plain">{{ row.vendor }}</el-tag>
                <el-tag v-if="row.account_name" type="info" size="small" effect="plain" style="margin-left: 4px">{{ row.account_name }}</el-tag>
                <span v-if="!row.vendor && !row.account_name" class="sub">—</span>
              </template>
            </el-table-column>
            <el-table-column :label="t('cost.drill.col_amount')" width="150" align="right" sortable prop="amount">
              <template #default="{ row }">
                <b>{{ props.fmtYuan2(row.amount) }}</b>
              </template>
            </el-table-column>
            <el-table-column :label="t('cost.drill.col_share')" width="160">
              <template #default="{ row }">
                <el-progress :percentage="Math.min(100, row.pct)" :stroke-width="8" :show-text="false" style="width: 80px; display: inline-flex" />
                <span style="margin-left: 8px">{{ row.pct }}%</span>
              </template>
            </el-table-column>
            <el-table-column :label="t('cost.drill.col_vs_prev')" width="110" align="right">
              <template #default="{ row }">
                <span v-if="row.delta_pct == null">-</span>
                <span v-else :style="{ color: row.delta_pct > 0 ? '#f56c6c' : '#67c23a' }">
                  {{ row.delta_pct > 0 ? '+' : '' }}{{ row.delta_pct }}%
                </span>
              </template>
            </el-table-column>
            <el-table-column width="72" align="center">
              <template #default="{ row }">
                <el-button link type="primary" size="small"
                           @click.stop="props.why(t('cost.drill.why_product', { p: row.product }))">{{ t('cost.drill.why') }}</el-button>
              </template>
            </el-table-column>
          </el-table>

          <div class="sec-title">
            {{ t('cost.drill.scenario_title') }}
            <span class="sub">{{ t('cost.drill.scenario_sub') }}</span>
          </div>
          <el-table stripe :data="props.drillScenarios" size="small" border>
            <el-table-column prop="scenario" :label="t('cost.drill.col_scenario')" min-width="260" show-overflow-tooltip />
            <el-table-column prop="product" :label="t('cost.drill.col_product')" width="130" show-overflow-tooltip />
            <!-- 归属标识：多账号看得出属于谁 -->
            <el-table-column :label="t('cost.common.vendor_account')" width="150">
              <template #default="{ row }">
                <el-tag v-if="row.vendor" type="info" size="small" effect="plain">{{ row.vendor }}</el-tag>
                <el-tag v-if="row.account_name" type="info" size="small" effect="plain" style="margin-left: 4px">{{ row.account_name }}</el-tag>
                <span v-if="!row.vendor && !row.account_name" class="sub">—</span>
              </template>
            </el-table-column>
            <el-table-column :label="t('cost.drill.col_amount')" width="150" align="right" sortable prop="amount">
              <template #default="{ row }">
                <b>{{ props.fmtYuan2(row.amount) }}</b>
              </template>
            </el-table-column>
            <el-table-column :label="t('cost.drill.col_share')" width="140">
              <template #default="{ row }">
                <el-progress :percentage="Math.min(100, row.pct)" :stroke-width="8" :show-text="false"
                             style="width: 70px; display: inline-flex" />
                <span style="margin-left: 8px">{{ row.pct }}%</span>
              </template>
            </el-table-column>
            <el-table-column :label="t('cost.drill.col_instances')" min-width="220" show-overflow-tooltip>
              <template #default="{ row }">{{ (row.instances || []).join('、') }}</template>
            </el-table-column>
            <el-table-column width="72" align="center">
              <template #default="{ row }">
                <el-button link type="primary" size="small"
                           @click.stop="props.why(t('cost.drill.why_scenario', { s: row.scenario }))">{{ t('cost.drill.why') }}</el-button>
              </template>
            </el-table-column>
          </el-table>
          <div class="sub" style="margin: 6px 0 2px">{{ props.drill?.scenario_note }}</div>

          <div class="sec-title">
            {{ t('cost.drill.instance_title') }}
            <span class="sub">{{ props.drillProduct ? t('cost.drill.filtered_hint', { p: props.drillProduct }) : '' }}</span>
            <el-button v-if="props.drillProduct" size="small" link type="primary"
                       @click="() => { emit('update:drillProduct', ''); emit('update:drillInstances', props.drill?.instances) }">{{ t('cost.drill.show_all') }}</el-button>
          </div>
          <el-table stripe :data="props.drillInstances" size="small" border>
            <el-table-column prop="name" :label="t('cost.drill.col_instance')" min-width="180" show-overflow-tooltip />
            <el-table-column prop="instance_id" :label="t('cost.drill.col_instance_id')" width="190" show-overflow-tooltip>
              <template #default="{ row }"><span class="mono">{{ row.instance_id }}</span></template>
            </el-table-column>
            <el-table-column :label="t('cost.drill.col_amount')" width="150" align="right" sortable prop="amount">
              <template #default="{ row }">
                <b>{{ props.fmtYuan2(row.amount) }}</b>
              </template>
            </el-table-column>
            <el-table-column prop="product" :label="t('cost.drill.col_product')" width="110" show-overflow-tooltip />
            <!-- 归属标识：多账号看得出属于谁 -->
            <el-table-column :label="t('cost.common.vendor_account')" width="150">
              <template #default="{ row }">
                <el-tag v-if="row.vendor" type="info" size="small" effect="plain">{{ row.vendor }}</el-tag>
                <el-tag v-if="row.account_name" type="info" size="small" effect="plain" style="margin-left: 4px">{{ row.account_name }}</el-tag>
                <span v-if="!row.vendor && !row.account_name" class="sub">—</span>
              </template>
            </el-table-column>
            <el-table-column :label="t('cost.drill.col_share')" width="110">
              <template #default="{ row }">{{ row.pct }}%</template>
            </el-table-column>
            <el-table-column :label="t('cost.drill.col_scenario')" min-width="180" show-overflow-tooltip>
              <template #default="{ row }">{{ (row.billing_items || []).join('、') }}</template>
            </el-table-column>
            <el-table-column width="72" align="center">
              <template #default="{ row }">
                <el-button link type="primary" size="small"
                           @click.stop="props.why(t('cost.drill.why_instance', { id: row.instance_id, name: row.name }))">{{ t('cost.drill.why') }}</el-button>
              </template>
            </el-table-column>
          </el-table>

          <div class="sec-title">{{ t('cost.drill.pack_title') }}</div>
          <el-table stripe :data="props.drill?.packs" size="small" border>
            <el-table-column prop="type_label" :label="t('cost.drill.col_pack_pool')" min-width="160" />
            <el-table-column :label="t('cost.drill.col_pool_left')" width="150">
              <template #default="{ row }">
                {{ row.pool_pct }}%<template v-if="row.days_left != null"> / {{ t('cost.drill.days_short', { n: row.days_left }) }}</template>
              </template>
            </el-table-column>
            <el-table-column :label="t('cost.drill.col_consumers')" min-width="300">
              <template #default="{ row }">
                <div v-if="row.consumers?.length">
                  <div v-for="c in row.consumers" :key="c.name" style="font-size: 12px">
                    {{ t('cost.drill.consumer_line', { name: c.name, v: Number(c.value).toLocaleString(), unit: c.unit, pct: c.pct }) }}
                  </div>
                </div>
                <span v-else class="sub">{{ t('cost.drill.no_attribution') }}</span>
                <div class="sub">{{ row.note }}</div>
              </template>
            </el-table-column>
          </el-table>
        </template>
</template>

<style scoped>
.stat-card { height: 100%; display: flex; flex-direction: column; justify-content: center; }
.stat-label { white-space: nowrap; }
.mono { font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 12px; }
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
.stat-value.danger { color: var(--el-color-danger); }
.stat-value.warn { color: var(--el-color-warning); }
.sub { font-size: 12px;
  color: var(--el-text-color-secondary); }

.sec-title { font-weight: 600; margin: 12px 0 8px; }
.wide { min-width: 140px; }
</style>
