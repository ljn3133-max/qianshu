<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import http from '@/api/http'
import { t } from '@/i18n'
import { useAuthStore } from '@/stores/auth'

// 值班与价值：值班表/升级链 + 每月价值账单 + 知识探针自检
const auth = useAuthStore()
// 联系人增删后端仅 admin；校准/探针为 admin|operator，两档分开：
// 两档不能合并成 canWrite，否则 operator 会看到必然 403 的联系人按钮
const canWrite = ['admin', 'operator'].includes(auth.role)
const canAdmin = auth.role === 'admin'

const contacts = ref([])
const escalations = ref([])
const value = ref(null)
const calibration = ref(null)
const multiCloud = ref(null)
const probeResult = ref(null)
const probing = ref(false)
const dialogOpen = ref(false)

const form = reactive({ name: '', role: '', channel: 'inapp', contact: '', level: 1, enabled: true })

// 金额展示：带币种和千分位，值班总览才能直接看出花费量级
function fmtMoney(v) {
  const n = Number(v || 0)
  return n.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

// 校准条目类型 -> i18n 键（未知类型原样展示）
const CAL_TYPE_KEYS = {
  new_host: 'oncall.cal.type.new_host',
  new_domain: 'oncall.cal.type.new_domain',
  baseline: 'oncall.cal.type.baseline',
  suggest: 'oncall.cal.type.suggest',
}

async function load() {
  try {
    ;[contacts.value, escalations.value, value.value, calibration.value, multiCloud.value] = await Promise.all([
      http.get('/oncall/contacts'),
      http.get('/oncall/escalations'),
      http.get('/oncall/value-report'),
      http.get('/oncall/calibration'),
      http.get('/multi-cloud/overview'),
    ])
  } catch { /* 拦截器提示 */ }
}

async function save() {
  if (!form.name) return ElMessage.warning(t('oncall.msg.name_required'))
  await http.post('/oncall/contacts', { ...form })
  ElMessage.success(t('oncall.msg.added'))
  dialogOpen.value = false
  load()
}

async function remove(row) {
  await ElMessageBox.confirm(t('oncall.remove.confirm', { name: row.name }), t('oncall.common.confirm_title'), { type: 'warning' })
  await http.delete(`/oncall/contacts/${row.id}`)
  load()
}

async function confirmCal(item) {
  await http.post(`/oncall/calibration/confirm/${item.id}`, { note: '' })
  ElMessage.success(t('oncall.msg.cal_confirmed'))
  calibration.value = await http.get('/oncall/calibration')
}

async function genCalibration() {
  await http.post('/oncall/calibration/generate')
  calibration.value = await http.get('/oncall/calibration')
  ElMessage.success(t('oncall.msg.week_generated'))
}

async function runProbe() {
  probing.value = true
  try {
    probeResult.value = await http.get('/oncall/knowledge-probe')
    ElMessage({
      type: probeResult.value.drifted?.length ? 'warning' : 'success',
      message: probeResult.value.drifted?.length
        ? t('oncall.probe.drift', { n: probeResult.value.drifted.length })
        : t('oncall.probe.ok'),
    })
  } finally {
    probing.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="page">
    <!-- 值班表 -->
    <el-card shadow="never">
      <template #header>
        <div class="head">
          <div class="oc-head">
            <b>{{ t('oncall.roster.title') }}</b>
            <el-button v-if="canAdmin" type="primary" size="small" @click="dialogOpen = true">{{ t('oncall.roster.add') }}</el-button>
          </div>
          <div class="tip" style="margin: 4px 0 2px">{{ t('oncall.roster.tip') }}</div>
        </div>
      </template>
      <el-table :data="contacts" border size="small" :empty-text="t('oncall.roster.empty')">
        <el-table-column prop="level" :label="t('oncall.roster.col_level')" width="70" />
        <el-table-column prop="name" :label="t('oncall.roster.col_name')" width="110" />
        <el-table-column prop="role" :label="t('oncall.roster.col_role')" width="120" />
        <el-table-column :label="t('oncall.roster.col_channel')" width="90">
          <template #default="{ row }"><el-tag size="small">{{ row.channel }}</el-tag></template>
        </el-table-column>
        <el-table-column prop="contact" :label="t('oncall.roster.col_contact')" min-width="140" />
        <el-table-column :label="t('oncall.roster.col_enabled')" width="80">
          <template #default="{ row }">{{ row.enabled ? t('oncall.yn.yes') : t('oncall.yn.no') }}</template>
        </el-table-column>
        <el-table-column v-if="canAdmin" label="" width="80">
          <template #default="{ row }">
            <el-button link type="danger" size="small" @click="remove(row)">{{ t('oncall.common.delete') }}</el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="sub-title">{{ t('oncall.esc.title') }}</div>
      <el-table :data="escalations" border size="small" :empty-text="t('oncall.esc.empty')">
        <el-table-column :label="t('oncall.esc.col_time')" width="150">
          <template #default="{ row }">{{ row.notified_at.replace('T', ' ').slice(0, 16) }}</template>
        </el-table-column>
        <el-table-column prop="level" :label="t('oncall.esc.col_level')" width="80" />
        <el-table-column prop="contact" :label="t('oncall.esc.col_contact')" width="110" />
        <el-table-column :label="t('oncall.esc.col_status')" width="90">
          <template #default="{ row }">
            <el-tag :type="row.ok ? 'success' : 'danger'" size="small">{{ row.ok ? t('oncall.esc.success') : t('oncall.esc.failed') }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="t('oncall.esc.col_detail')" min-width="220">
          <template #default="{ row }">{{ row.detail || '-' }}</template>
        </el-table-column>
        <el-table-column :label="t('oncall.esc.col_ack')" width="80">
          <template #default="{ row }">
            <el-tag :type="row.acked ? 'success' : 'warning'" size="small">{{ row.acked ? t('oncall.esc.acked') : t('oncall.esc.pending') }}</el-tag>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 跨云总览（多云/多账号管理面） -->
    <el-card shadow="never">
      <template #header>
        <div class="head">
          <b>{{ t('oncall.cloud.title') }}</b>
          <!-- 三视角口径：本卡按云账号聚合（Dashboard 按产品、运维管理按厂商筛） -->
          <span class="tip">{{ t('oncall.cloud.scope') }}</span>
        </div>
      </template>
      <div v-if="multiCloud" class="value-grid">
        <div class="v-item"><div class="num">{{ multiCloud.totals.accounts }}</div><div class="label">{{ t('oncall.cloud.stat_accounts') }}</div></div>
        <div class="v-item"><div class="num">{{ multiCloud.totals.assets }}</div><div class="label">{{ t('oncall.cloud.stat_assets') }}</div></div>
        <div class="v-item"><div class="num">¥{{ fmtMoney(multiCloud.totals.cost_7d) }}</div><div class="label">{{ t('oncall.cloud.stat_cost7d') }}</div></div>
        <div class="v-item"><div class="num">{{ multiCloud.totals.alerts }}</div><div class="label">{{ t('oncall.cloud.stat_alerts') }}</div></div>
      </div>
      <el-table v-if="multiCloud" :data="multiCloud.accounts" border size="small">
        <el-table-column prop="name" :label="t('oncall.cloud.col_account')" width="140" />
        <el-table-column prop="kind" :label="t('oncall.cloud.col_kind')" width="80" />
        <el-table-column prop="assets_total" :label="t('oncall.cloud.col_assets')" width="80" />
        <el-table-column :label="t('oncall.cloud.col_products')" min-width="200">
          <template #default="{ row }">
            <el-tag v-for="(n, p) in row.assets_by_product" :key="p" size="small" class="diff-chip">{{ p }} {{ n }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="t('oncall.cloud.col_cost7d')" width="110">
          <template #default="{ row }">¥{{ fmtMoney(row.cost_7d) }}</template>
        </el-table-column>
        <el-table-column prop="alerts_total" :label="t('oncall.cloud.col_alerts')" width="70" />
      </el-table>
    </el-card>

    <!-- 价值账单 -->
    <el-card shadow="never">
      <template #header><b>{{ t('oncall.value.title', { period: value?.period ?? '' }) }}</b></template>
      <div v-if="value" class="value-grid">
        <div class="v-item"><div class="num">{{ value.alerts.total }}</div><div class="label">{{ t('oncall.value.alerts_total') }}</div></div>
        <div class="v-item"><div class="num">{{ value.alerts.critical }}</div><div class="label">{{ t('oncall.value.alerts_critical') }}</div></div>
        <div class="v-item"><div class="num">{{ value.alerts.recovered }}</div><div class="label">{{ t('oncall.value.alerts_recovered') }}</div></div>
        <div class="v-item"><div class="num">{{ value.auto_diagnosis }}</div><div class="label">{{ t('oncall.value.auto_diag') }}</div></div>
        <div class="v-item"><div class="num">{{ value.cost_findings }}</div><div class="label">{{ t('oncall.value.cost_findings') }}</div></div>
        <div class="v-item"><div class="num">{{ value.inspection_runs }}</div><div class="label">{{ t('oncall.value.inspections') }}</div></div>
      </div>
      <ul v-if="value" class="hl">
        <li v-for="h in (value.highlights || []).filter(Boolean)" :key="h">{{ h }}</li>
      </ul>
      <div class="tip">{{ value?.note }}</div>
    </el-card>

    <!-- 引导式校准周报 -->
    <el-card shadow="never">
      <template #header>
        <div class="head">
          <b>{{ t('oncall.cal.title', { week: calibration?.week ?? '', n: calibration?.pending || 0 }) }}</b>
          <span class="tip">{{ t('oncall.cal.tip') }}</span>
          <el-button v-if="canWrite" size="small" @click="genCalibration">{{ t('oncall.cal.gen') }}</el-button>
        </div>
      </template>
      <el-table v-if="calibration?.items?.length" :data="calibration.items" border size="small">
        <el-table-column :label="t('oncall.cal.col_type')" width="110">
          <template #default="{ row }">
            <el-tag size="small">{{ CAL_TYPE_KEYS[row.type] ? t(CAL_TYPE_KEYS[row.type]) : row.type }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="t('oncall.cal.col_learn')" min-width="360">
          <template #default="{ row }">{{ row.content }}</template>
        </el-table-column>
        <el-table-column :label="t('oncall.cal.col_status')" width="100">
          <template #default="{ row }">
            <el-button v-if="row.status === 'pending' && canWrite" size="default" type="primary" @click="confirmCal(row)">{{ t('oncall.cal.confirm') }}</el-button>
            <el-tag v-else type="success" size="small">{{ t('oncall.cal.confirmed') }}</el-tag>
          </template>
        </el-table-column>
      </el-table>
      <div v-else class="tip">{{ t('oncall.cal.empty') }}</div>
    </el-card>

    <!-- 知识保鲜 -->
    <el-card shadow="never">
      <template #header>
        <div class="head">
          <b>{{ t('oncall.probe.title') }}</b>
          <span class="tip">{{ t('oncall.probe.tip') }}</span>
          <el-button v-if="canWrite" size="small" :loading="probing" @click="runProbe">{{ t('oncall.probe.run') }}</el-button>
        </div>
      </template>
      <el-table v-if="probeResult" :data="probeResult.results" border size="small">
        <el-table-column label="" width="60">
          <template #default="{ row }">
            <el-tag :type="row.ok ? 'success' : 'danger'" size="small">{{ row.ok ? t('oncall.probe.pass') : t('oncall.probe.drift_tag') }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="probe" :label="t('oncall.probe.col_probe')" width="140" />
        <el-table-column prop="detail" :label="t('oncall.probe.col_result')" min-width="200" />
      </el-table>
      <div v-else class="tip">{{ t('oncall.probe.empty') }}</div>
    </el-card>

    <el-dialog v-model="dialogOpen" :title="t('oncall.dialog.title')" width="460px">
      <el-form :model="form" label-width="90px">
        <el-form-item :label="t('oncall.dialog.name')"><el-input v-model="form.name" /></el-form-item>
        <el-form-item :label="t('oncall.dialog.role')"><el-input v-model="form.role" :placeholder="t('oncall.dialog.role_ph')" /></el-form-item>
        <el-form-item :label="t('oncall.dialog.channel')">
          <el-select v-model="form.channel">
            <el-option :label="t('oncall.dialog.ch.inapp')" value="inapp" />
            <el-option :label="t('oncall.dialog.ch.wecom')" value="wecom" />
            <el-option :label="t('oncall.dialog.ch.sms')" value="sms" />
            <el-option :label="t('oncall.dialog.ch.phone')" value="phone" />
          </el-select>
          <!-- 前置条件说明（短信/电话需阿里云配置） -->
          <div class="tip" style="width: 100%; line-height: 1.7">
            {{ t('oncall.dialog.ch_tip') }}
          </div>
        </el-form-item>
        <el-form-item :label="t('oncall.dialog.contact')"><el-input v-model="form.contact" :placeholder="t('oncall.dialog.contact_ph')" /></el-form-item>
        <el-form-item :label="t('oncall.dialog.level')"><el-input-number v-model="form.level" :min="1" :max="9" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogOpen = false">{{ t('oncall.common.cancel') }}</el-button>
        <el-button type="primary" @click="save">{{ t('oncall.common.save') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.oc-head { display: flex; justify-content: space-between; align-items: center; }
.page { display: flex; flex-direction: column; gap: 14px; }
.head { display: flex; align-items: center; gap: 12px; }
.head .tip { flex: 1; }
.tip { font-size: 12px; color: var(--el-text-color-secondary); }
.sub-title { margin: 14px 0 8px; font-weight: 600; }
.value-grid { display: flex; gap: 24px; flex-wrap: wrap; margin-bottom: 10px; }
.v-item .num { font-size: 26px; font-weight: 700; }
.v-item .label { font-size: 12px; color: var(--el-text-color-secondary); }
.hl { margin: 6px 0; padding-left: 18px; font-size: 13px; }
</style>
