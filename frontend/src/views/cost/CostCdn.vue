<script setup>
// CDN 域名消耗：顶部提示最近 CDN 流量异动数（traffic_spike，详情在监控域）。
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { costAnomalies } from '@/api/cost'
import { t } from '@/i18n'

const emit = defineEmits(['update:cdnDays', 'update:urlDomain'])
const props = defineProps(['CDN_DAYS_OPTIONS', 'avg_daily_gb', 'cdnData', 'cdnDays', 'cdnLoading', 'cdnSeries', 'daily_total', 'delta_gb', 'domain', 'domains', 'fontWeight', 'grand_total_gb', 'last_daily_gb', 'loadCdnDomains', 'loadCdnUrls', 'marginRight', 'note', 'peak_day', 'peak_day_gb', 'product', 'requests', 'share_pct', 'total_gb', 'urlData', 'urlDomain', 'urlLoading', 'urls', 'window_days'])

const router = useRouter()
const cdnAnomalyCount = ref(0)

async function loadCdnAnomalyCount() {
  try {
    const data = await costAnomalies({ domain: 'cdn', page_size: 1 })
    cdnAnomalyCount.value = data.total || 0
  } catch (e) {
    cdnAnomalyCount.value = 0
  }
}
onMounted(loadCdnAnomalyCount)

function gotoMonitorAnomalies() {
  router.push('/monitor')   // 监控域「异动」列有 CDN 流量异动明细，可再跳回对应域名
}
</script>

<template>
        <el-alert v-if="cdnAnomalyCount > 0" type="warning" :closable="false" show-icon class="anomaly-tip">
          <template #title>
            {{ t('cost.cdn.anomaly_tip', { n: cdnAnomalyCount }) }}
            <el-link type="primary" :underline="false" @click="gotoMonitorAnomalies">{{ t('cost.cdn.anomaly_link') }}</el-link>
          </template>
        </el-alert>
        <div class="toolbar">
          <span class="sub">{{ t('cost.cdn.basis') }}</span>
          <el-select :model-value="cdnDays" @update:model-value="v => emit('update:cdnDays', v)" size="small" style="width: 110px; margin-left: auto" @change="props.loadCdnDomains">
            <el-option v-for="d in props.CDN_DAYS_OPTIONS" :key="d" :value="d" :label="t('cost.cdn.days_option', { n: d })" />
          </el-select>
        </div>
        <div v-loading="cdnLoading">
          <el-empty v-if="!props.cdnLoading&& !props.cdnData?.domains?.length"
                    :description="props.cdnData?.note || (props.cdnData ? t('cost.cdn.no_collection') : t('cost.cdn.loading_domains'))" />
          <template v-else>
            <div class="stat-cards">
              <div class="stat-card">
                <div class="stat-label">{{ t('cost.cdn.stat_total') }}</div>
                <div class="stat-value">{{ props.cdnData?.grand_total_gb }} GB</div>
                <div class="stat-sub">{{ t('cost.cdn.window_days', { n: props.cdnData?.window_days ?? '' }) }}</div>
              </div>
              <div class="stat-card">
                <div class="stat-label">{{ t('cost.cdn.stat_avg') }}</div>
                <div class="stat-value">{{ props.cdnData?.avg_daily_gb }} {{ t('cost.cdn.gb_per_day') }}</div>
                <div class="stat-sub">{{ t('cost.cdn.avg_scale_tip') }}</div>
              </div>
              <div class="stat-card">
                <div class="stat-label">{{ t('cost.cdn.stat_top') }}</div>
                <div class="stat-value" style="font-size: 18px">{{ props.cdnData?.domains[0]?.domain || '-' }}</div>
                <div class="stat-sub">{{ t('cost.cdn.top_sub', { pct: props.cdnData?.domains[0]?.share_pct ?? 0, gb: props.cdnData?.domains[0]?.avg_daily_gb ?? 0 }) }}</div>
              </div>
            </div>
            <div class="charts-row">
              <div class="chart-card wide">
                <MetricChart v-if="props.cdnSeries?.length" :title="t('cost.cdn.chart_title')" :series="props.cdnSeries" :height="260" />
                <div v-else class="empty-chart">{{ t('cost.common.no_data') }}</div>
              </div>
            </div>
            <el-table stripe :data="props.cdnData?.domains" size="small" border>
              <el-table-column prop="domain" :label="t('cost.cdn.col_domain')" min-width="220" show-overflow-tooltip />
              <el-table-column :label="t('cost.cdn.col_product')" width="80">
                <template #default="{ row }">{{ row.product === 'dcdn' ? t('cost.cdn.product_dcdn') : t('cost.cdn.product_cdn') }}</template>
              </el-table-column>
              <el-table-column :label="t('cost.cdn.col_total')" width="110" align="right">
                <template #default="{ row }">{{ row.total_gb }} GB</template>
              </el-table-column>
              <el-table-column :label="t('cost.cdn.col_share')" width="180">
                <template #default="{ row }">
                  <el-progress :percentage="Math.min(100, row.share_pct)" :stroke-width="12"
                              :color="row.share_pct > 80 ? '#f56c6c' : '#409eff'" />
                  <span class="sub">{{ row.share_pct }}%</span>
                </template>
              </el-table-column>
              <el-table-column :label="t('cost.cdn.col_avg')" width="100" align="right">
                <template #default="{ row }">{{ row.avg_daily_gb }} GB</template>
              </el-table-column>
              <el-table-column :label="t('cost.cdn.col_last')" width="100" align="right">
                <template #default="{ row }">{{ row.last_daily_gb }} GB</template>
              </el-table-column>
            </el-table>
            <div v-if="props.cdnData?.note" class="sub" style="margin-top: 8px">{{ props.cdnData?.note }}</div>

            <!-- URL/文件级下钻：流量突增归因（谁在烧、峰值日谁涨） -->
            <div class="sec-title" style="margin-top: 18px">{{ t('cost.cdn.url_title') }}</div>
            <div class="toolbar">
              <span class="sub">{{ t('cost.cdn.url_tip') }}</span>
              <el-select :model-value="urlDomain" @update:model-value="v => emit('update:urlDomain', v)" filterable size="small" style="width: 280px; margin-left: auto"
                         @change="props.loadCdnUrls">
                <el-option v-for="d in (props.cdnData?.domains || [])" :key="d.domain" :value="d.domain"
                           :label="t('cost.cdn.url_domain_option', { domain: d.domain, gb: d.total_gb })" />
              </el-select>
            </div>
            <div v-loading="urlLoading">
              <el-empty v-if="!props.urlLoading&& !props.urlData?.urls?.length"
                        :description="props.urlData?.note || (props.urlData ? t('cost.cdn.url_no_data') : t('cost.cdn.url_select_hint'))"
                        :image-size="60" />
              <template v-else-if="props.urlData?.urls?.length">
                <div class="sub" style="margin-bottom: 6px">
                  {{ t('cost.cdn.daily_total') }}
                  <template v-for="(g, d) in props.urlData?.daily_total" :key="d">
                    <span :style="{ marginRight: '10px', fontWeight: d === props.urlData?.peak_day ? 700 : 400 }">
                      {{ t('cost.cdn.daily_item', { d: d.slice(5), gb: g }) }}<template v-if="d === props.urlData?.peak_day">{{ t('cost.cdn.peak_day') }}</template>
                    </span>
                  </template>
                </div>
                <el-table stripe :data="props.urlData?.urls" size="small" border>
                  <el-table-column prop="uri" :label="t('cost.cdn.col_url')" min-width="320" show-overflow-tooltip />
                  <el-table-column :label="t('cost.cdn.col_total')" width="100" align="right">
                    <template #default="{ row }">{{ row.total_gb }} GB</template>
                  </el-table-column>
                  <el-table-column :label="t('cost.cdn.col_share')" width="80" align="right">
                    <template #default="{ row }">{{ row.share_pct }}%</template>
                  </el-table-column>
                  <el-table-column :label="t('cost.cdn.col_requests')" width="90" align="right">
                    <template #default="{ row }">{{ row.requests.toLocaleString() }}</template>
                  </el-table-column>
                  <el-table-column :label="t('cost.cdn.col_peak')" width="100" align="right">
                    <template #default="{ row }">{{ row.peak_day_gb }} GB</template>
                  </el-table-column>
                  <el-table-column :label="t('cost.cdn.col_delta')" width="130" align="right">
                    <template #default="{ row }">
                      <span :style="{ color: row.delta_gb > 0 ? '#f56c6c' : '#67c23a', fontWeight: 600 }">
                        {{ row.delta_gb > 0 ? '+' : '' }}{{ row.delta_gb }} GB
                      </span>
                    </template>
                  </el-table-column>
                </el-table>
                <div class="sub" style="margin-top: 6px">{{ props.urlData?.note }}</div>
              </template>
            </div>
          </template>
        </div>
</template>

<style scoped>
.chart-card { border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  padding: 8px;
  background: var(--el-bg-color-overlay); }
.charts-row { display: grid;
  grid-template-columns: 2fr 1fr;
  gap: 12px;
  margin-bottom: 14px; }
.empty-chart { height: 240px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--el-text-color-secondary);
  font-size: 13px; }
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
.toolbar { display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 10px; }

.sec-title { font-weight: 600; margin: 12px 0 8px; }
.wide { min-width: 140px; }
.anomaly-tip { margin-bottom: 10px; }
</style>
