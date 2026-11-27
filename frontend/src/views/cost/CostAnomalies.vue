<script setup>
// 成本异动列表：由 Cost.vue 传 domain=cost 只查成本类，监控/画像类归各自域。
import { agentAskUrl } from '@/api/agent'
import { t } from '@/i18n'

const emit = defineEmits(['update:anomalyLevelFilter', 'update:anomalyStatusFilter', 'update:anomalyTypeFilter', 'update:anomalySelection', 'update:anomalyPage'])
const props = defineProps(['ACK_STATUS', 'ANOMALY_TYPES', 'ackAnomaliesBatch', 'ack_note', 'ack_status', 'anomalies', 'anomaliesError', 'anomaliesLoading', 'anomaliesTotal', 'anomalyLevelFilter', 'anomalyQuery', 'anomalySelection', 'anomalyStatusFilter', 'anomalyTypeFilter', 'baseline', 'biz', 'canWrite', 'content', 'detect_date', 'detecting', 'doDetect', 'fmtMoney', 'instance_id', 'last_seen', 'level', 'loadAnomalies', 'page', 'page_size', 'ratio', 'related_packs', 'rows', 'tag', 'target_key', 'title', 'typeLabel', 'typeTag', 'type_label'])

// 状态/类型文案按值映射 i18n（父级 Cost.vue 传入的 label/typeLabel 为中文，仅作兜底）
const ANOM_STATUS_KEYS = {
  open: 'cost.anom.status.open', acknowledged: 'cost.anom.status.acknowledged',
  ignored: 'cost.anom.status.ignored', resolved: 'cost.anom.status.resolved',
}
const ANOM_TYPE_KEYS = {
  payg_spike: 'cost.anom.type.payg_spike', payg_new: 'cost.anom.type.payg_new',
  pack_spike: 'cost.anom.type.pack_spike', pack_exhausting: 'cost.anom.type.pack_exhausting',
  pack_exhausted: 'cost.anom.type.pack_exhausted', pack_expiry: 'cost.anom.type.pack_expiry',
  profile_drift: 'cost.anom.type.profile_drift', host_down: 'cost.anom.type.host_down',
  aux_spillover: 'cost.anom.type.aux_spillover', traffic_spike: 'cost.anom.type.traffic_spike',
  growth_accel: 'cost.anom.type.growth_accel',
}
const statusLabel = (k) => (ANOM_STATUS_KEYS[k] ? t(ANOM_STATUS_KEYS[k]) : (props.ACK_STATUS[k]?.label || k))
const typeLabel = (k) => (ANOM_TYPE_KEYS[k] ? t(ANOM_TYPE_KEYS[k]) : props.typeLabel(k))
// 级别列此前直接渲染 critical/warning/info 原值（中文界面里突兀），按词典本地化
const ANOM_LEVEL_KEYS = {
  critical: 'cost.anom.level.critical', warning: 'cost.anom.level.warning', info: 'cost.anom.level.info',
}
const levelLabel = (k) => (ANOM_LEVEL_KEYS[k] ? t(ANOM_LEVEL_KEYS[k]) : k)

// 问智能体深链：成本异动带对象上下文落到智能体新会话首条
function askAgent(row) {
  const q = t('cost.anom.ask_q', {
    title: row.title, target: row.target_key, type: typeLabel(row.type),
    value: row.value, baseline: row.baseline,
    ratio: row.ratio ? t('cost.anom.ask_ratio', { ratio: row.ratio }) : '',
  })
  window.open(agentAskUrl(q, {
    page: '/cost?tab=anomalies', anomaly_id: row.id, type: row.type,
    target_key: row.target_key, level: row.level,
  }), '_blank')
}
</script>

<template>
        <div class="toolbar">
          <el-select :model-value="anomalyTypeFilter" @update:model-value="v => emit('update:anomalyTypeFilter', v)" clearable :placeholder="t('cost.anom.ph_type')" style="width: 160px" @change="() => { emit('update:anomalyPage', 1); props.loadAnomalies() }">
            <el-option v-for="(v, k) in props.ANOMALY_TYPES" :key="k" :label="ANOM_TYPE_KEYS[k] ? t(ANOM_TYPE_KEYS[k]) : v.label" :value="k" />
          </el-select>
          <el-select :model-value="anomalyLevelFilter" @update:model-value="v => emit('update:anomalyLevelFilter', v)" clearable :placeholder="t('cost.anom.ph_level')" style="width: 110px" @change="() => { emit('update:anomalyPage', 1); props.loadAnomalies() }">
            <el-option v-for="(labelKey, k) in ANOM_LEVEL_KEYS" :key="k" :label="t(labelKey)" :value="k" />
          </el-select>
          <el-select :model-value="anomalyStatusFilter" @update:model-value="v => emit('update:anomalyStatusFilter', v)" clearable :placeholder="t('cost.anom.ph_status')" style="width: 110px" @change="() => { emit('update:anomalyPage', 1); props.loadAnomalies() }">
            <el-option v-for="(v, k) in props.ACK_STATUS" :key="k" :label="ANOM_STATUS_KEYS[k] ? t(ANOM_STATUS_KEYS[k]) : v.label" :value="k" />
          </el-select>
          <template v-if="props.canWrite">
            <el-button-group>
              <el-button size="small" :disabled="!props.anomalySelection?.length" @click="props.ackAnomaliesBatch('acknowledged')">
                {{ t('cost.anom.ack') }}{{ props.anomalySelection?.length ? t('cost.anom.ack_n', { n: props.anomalySelection?.length }) : '' }}
              </el-button>
              <el-button size="small" :disabled="!props.anomalySelection?.length" @click="props.ackAnomaliesBatch('ignored')">
                {{ t('cost.anom.ignore') }}
              </el-button>
              <el-button size="small" :disabled="!props.anomalySelection?.length" @click="props.ackAnomaliesBatch('open')">
                {{ t('cost.anom.reopen') }}
              </el-button>
            </el-button-group>
          </template>
          <span class="sub">{{ t('cost.anom.batch_tip') }}</span>
          <div style="flex: 1" />
          <el-button-group>
            <el-button size="small" :loading="props.detecting" :disabled="!props.canWrite" @click="props.doDetect">
              {{ t('cost.anom.detect_now') }}
            </el-button>
          </el-button-group>
        </div>

        <el-table stripe
          v-loading="anomaliesLoading" :data="props.anomalies" size="small" border
          :empty-text="props.anomaliesError ? t('cost.load.failed') : undefined"
          row-key="id" @selection-change="(rows) => emit('update:anomalySelection', rows)">
          <el-table-column v-if="props.canWrite" type="selection" width="40" reserve-selection />
          <el-table-column type="expand">
            <template #default="{ row }">
              <div class="anomaly-content">{{ row.content }}</div>
              <div v-if="row.ack_note" class="anomaly-content sub">{{ t('cost.anom.ack_note', { v: row.ack_note }) }}</div>
            </template>
          </el-table-column>
          <el-table-column :label="t('cost.anom.col_date')" width="105">
            <template #default="{ row }">
              {{ row.last_seen }}
              <div v-if="row.last_seen !== row.detect_date" class="sub">{{ t('cost.anom.first_seen', { date: row.detect_date }) }}</div>
            </template>
          </el-table-column>
          <el-table-column :label="t('cost.anom.col_status')" width="80">
            <template #default="{ row }">
              <el-tag :type="props.ACK_STATUS[row.ack_status]?.tag || 'info'" size="small" effect="plain">
                {{ statusLabel(row.ack_status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column :label="t('cost.anom.col_level')" width="95">
            <template #default="{ row }">
              <el-tag :type="row.level === 'critical' ? 'danger' : row.level === 'info' ? 'info' : 'warning'" size="small" effect="dark">
                {{ levelLabel(row.level) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column :label="t('cost.anom.col_type')" width="110">
            <template #default="{ row }">
              <el-tag :type="props.typeTag(row.type)" size="small" effect="plain">{{ typeLabel(row.type) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column :label="t('cost.anom.col_content')" min-width="260">
            <template #default="{ row }">
              <el-tag v-for="b in row.biz" :key="b" type="warning" size="small" effect="dark" style="margin-right: 4px">{{ b }}</el-tag>
              {{ row.title }}
              <div class="sub mono">{{ row.target_key }}</div>
              <div v-if="row.related_packs?.length" style="margin-top: 4px">
                <el-tooltip
                  v-for="p in row.related_packs" :key="p.instance_id"
                  :content="t('cost.anom.pack_tooltip', { date: p.detect_date })"
                  placement="top">
                  <el-tag size="small" type="danger" effect="plain" style="margin-right: 6px">
                    {{ t('cost.anom.pack_suspect', { label: p.type_label, date: p.detect_date }) }}
                  </el-tag>
                </el-tooltip>
              </div>
            </template>
          </el-table-column>
          <el-table-column :label="t('cost.anom.col_value')" width="170" align="right">
            <template #default="{ row }">
              <template v-if="row.type === 'payg_spike' || row.type === 'payg_new'">
                {{ props.fmtMoney(row.value) }} / {{ props.fmtMoney(row.baseline) }}
                <div class="sub">{{ t('cost.anom.value_basis') }}</div>
              </template>
              <template v-else>
                {{ row.value }} / {{ row.baseline }}
              </template>
            </template>
          </el-table-column>
          <el-table-column :label="t('cost.anom.col_ratio')" width="80" align="right">
            <template #default="{ row }">
              <span v-if="row.ratio" class="danger">×{{ row.ratio }}</span>
              <span v-else class="sub">-</span>
            </template>
          </el-table-column>
          <el-table-column :label="t('cost.anom.col_ask')" width="100">
            <template #default="{ row }">
              <el-button size="small" text type="primary" @click="askAgent(row)">{{ t('cost.common.ask_agent') }}</el-button>
            </template>
          </el-table-column>
        </el-table>
        <div class="pager">
          <el-pagination
            :current-page="props.anomalyQuery.page"
            :page-size="props.anomalyQuery.page_size"
            :total="props.anomaliesTotal"
            layout="total, prev, pager, next"
            background
            @update:current-page="v => emit('update:anomalyPage', v)"
            @current-change="props.loadAnomalies"
          />
        </div>
</template>

<style scoped>
.anomaly-content { padding: 4px 12px;
  font-size: 12px;
  white-space: pre-wrap;
  line-height: 1.8; }
.stat-value.danger { color: var(--el-color-danger); }
.danger { color: var(--el-color-danger); }
.mono { font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 12px; }
.pager { display: flex;
  justify-content: flex-end;
  margin-top: 10px; }
.sub { font-size: 12px;
  color: var(--el-text-color-secondary); }
.toolbar { display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 10px; }
</style>
