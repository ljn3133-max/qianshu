<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import TermDialog from '@/components/TermDialog.vue'
import http from '@/api/http'
import { ElMessage } from 'element-plus'
import { t } from '@/i18n'

import { dbaBackups, dbaInstances, dbaSpace, insightRecords, redisInstances, redisKeys, slowlogRecords, slowlogTop, spaceCollectStatus, triggerSpaceCollect } from '@/api/dba'
import MetricChart from '@/components/MetricChart.vue'
import { useAuthStore } from '@/stores/auth'

// SSH 终端权限与机器管理页同一权限码（后端 ws 仅 admin/operator）；此前本页无门禁，
// viewer 不展示必然连接失败的终端入口
const auth = useAuthStore()
const canTerm = computed(() => auth.hasCode('machines.ssh'))

/* ==================== 实例列表（四个页签共享） ==================== */
const instances = ref([])
const instLoading = ref(false)
const instanceNote = ref('')      // 无云凭证等说明（执行层给的人话）
const noCredential = ref(false)

async function loadInstances() {
  instLoading.value = true
  try {
    const r = await dbaInstances()   // http.js 拦截器直接返回数据本身
    instances.value = r.instances || []
    instanceNote.value = r.note || ''
    noCredential.value = !!r.no_credential
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('dba.msg.instanceLoadFailed'))
  } finally {
    instLoading.value = false
  }
}

function instLabel(i) {
  // 归属标识：实例 ID + 账号，多账号看得出属于谁
  return t('dba.filter.instLabel', { name: i.name, engine: i.engine, version: i.engine_version, id: i.instance_id, account: i.account ? ' · ' + i.account : '' })
}

/* ==================== 页签1：慢SQL ==================== */
const slow = reactive({ instanceId: '', days: 7, loading: false, rows: [] })
const slowDetail = reactive({ visible: false, loading: false, rows: [], total: 0, page: 1, keywordRow: null, note: '' })
const SLOW_PAGE_SIZE = 50

async function loadSlowTop() {
  if (!slow.instanceId) return
  slow.loading = true
  try {
    const r = await slowlogTop({ instance_id: slow.instanceId, days: slow.days })
    slow.rows = r.rows || []
    if (!slow.rows.length) ElMessage.info(t('dba.msg.slowEmpty'))
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('dba.msg.slowQueryFailed'))
  } finally {
    slow.loading = false
  }
}

async function openSlowDetail(row) {
  slowDetail.keywordRow = row
  slowDetail.visible = true
  slowDetail.page = 1
  await loadSlowDetail()
}

async function loadSlowDetail() {
  slowDetail.loading = true
  try {
    const r = await slowlogRecords({
      instance_id: slow.instanceId,
      hours: slow.days * 24,
      page: slowDetail.page, page_size: SLOW_PAGE_SIZE,
      // 只看这条模板的记录（MySQL=模板哈希，PG=归一化键），而不是全实例流水
      sql_hash: slowDetail.keywordRow?.sql_hash || '',
    })
    slowDetail.rows = r.rows || []
    slowDetail.total = r.total || 0
    slowDetail.note = r.note || ''
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('dba.msg.slowDetailFailed'))
  } finally {
    slowDetail.loading = false
  }
}

/* ==================== 页签2：SQL 洞察明细 ==================== */
const insight = reactive({ instanceId: '', hours: 1, keyword: '', loading: false, rows: [], total: 0, page: 1 })
const INSIGHT_PAGE_SIZE = 100

async function loadInsight() {
  if (!insight.instanceId) return
  insight.loading = true
  try {
    const r = await insightRecords({
      instance_id: insight.instanceId,
      hours: insight.hours,
      keyword: insight.keyword,
      page: insight.page,
    })
    insight.rows = r.rows || []
    insight.total = r.total || 0
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('dba.msg.insightFailed'))
  } finally {
    insight.loading = false
  }
}

function searchInsight() {
  insight.page = 1
  loadInsight()
}

/* ==================== 页签3：空间趋势 ==================== */
// 空间趋势每日自动采集时刻 09:40 为默认值（backend config DBA_COLLECT_HOUR/MINUTE 可配）
const space = reactive({ instanceId: '', days: 30, loading: false, current: null, trend: [] })
const spaceJob = reactive({ collecting: false, status: null })

async function loadSpace() {
  if (!space.instanceId) return
  space.loading = true
  try {
    const r = await dbaSpace({ instance_id: space.instanceId, days: space.days })
    space.current = r.current
    space.trend = r.trend || []
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('dba.msg.spaceFailed'))
  } finally {
    space.loading = false
    space.queried = true   // 区分"没查询"与"查询后无数据"
  }
  loadSpaceStatus()
}

async function loadSpaceStatus() {
  try {
    spaceJob.status = await spaceCollectStatus()
  } catch { /* 状态仅辅助展示，失败不提示 */ }
}

async function collectNow() {
  spaceJob.collecting = true
  try {
    const r = await triggerSpaceCollect()
    spaceJob.status = r
    // 上一轮未结束时后端返回 200 + skipped（状态是上一轮的残留值），
    // 不能再报「采集完成 N 个实例」；部分失败也要让用户看见
    if (r.skipped) {
      ElMessage.warning(t('dba.msg.collectSkipped', { reason: r.skipped }))
    } else if ((r.errors || []).length) {
      ElMessage.warning(t('dba.msg.collectPartial', { n: r.instances, err: r.errors.length }))
    } else {
      ElMessage.success(t('dba.msg.collectDone', { n: r.instances }))
    }
    loadSpace()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('dba.msg.collectFailed'))
  } finally {
    spaceJob.collecting = false
  }
}

const GB = 1024 * 1024 * 1024
function gb(bytes) {
  return (bytes / GB).toFixed(2)
}

const spaceSeries = () => {
  if (!space.trend.length) return []
  return [
    { name: t('dba.chart.seriesData'), points: space.trend.map(p => [p.date, +(p.data_size / GB).toFixed(2)]) },
    { name: t('dba.chart.seriesDisk'), points: space.trend.map(p => [p.date, +(p.disk_used / GB).toFixed(2)]) },
    { name: t('dba.chart.seriesBackup'), points: space.trend.map(p => [p.date, +(p.backup_size / GB).toFixed(2)]) },
  ]
}

/* ==================== 页签4：Redis 大key/热key ==================== */
const redis = reactive({ instances: [], instanceId: '', type: 'big', loading: false, rows: [] })

async function loadRedisInstances() {
  try {
    const r = await redisInstances()
    redis.instances = r.instances || []
    if (!redis.instanceId && redis.instances.length) {
      redis.instanceId = redis.instances[0].instance_id
      loadRedisKeys()   // 与慢SQL页签一致：进页面默认加载首个实例
    }
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('dba.msg.redisInstancesFailed'))
  }
}

async function loadRedisKeys() {
  if (!redis.instanceId) return
  redis.loading = true
  try {
    const r = await redisKeys({ instance_id: redis.instanceId, type: redis.type })
    redis.rows = r.rows || []
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('dba.msg.redisKeysFailed'))
  } finally {
    redis.loading = false
  }
}

function redisLabel(i) {
  const cap = i.capacity_mb ? ` ${Math.round(i.capacity_mb / 1024)}GB` : ''
  // 归属标识：实例 ID + 账号，多账号看得出属于谁
  return t('dba.filter.redisLabel', { name: i.name, arch: i.architecture || 'redis', version: i.engine_version, cap, id: i.instance_id, account: i.account ? ' · ' + i.account : '' })
}

/* ==================== 页签5：备份状态 ==================== */
const backup = reactive({ instanceId: '', days: 7, loading: false, policy: null, backups: [] })

// 备份状态映射：阿里云 RDS BackupStatus 成功枚举是 BackupOK 系，保留 Success 兼容；
// 按枚举判定 + 中文映射，未知枚举兜底原样显示
const BACKUP_STATUS = {
  BackupOK: { label: t('dba.status.backupOk'), type: 'success' },
  Success: { label: t('dba.status.backupOk'), type: 'success' },   // 兼容旧口径
  BackupFailed: { label: t('dba.status.backupFailed'), type: 'danger' },
  BackupInProgress: { label: t('dba.status.backupRunning'), type: 'warning' },
}
function backupStatus(s) {
  return BACKUP_STATUS[s] || { label: s || t('dba.status.unknown'), type: 'info' }
}

async function loadBackups() {
  if (!backup.instanceId) return
  backup.loading = true
  try {
    const r = await dbaBackups({ instance_id: backup.instanceId, days: backup.days })
    backup.policy = r.policy || null
    backup.backups = r.backups || []
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('dba.msg.backupQueryFailed'))
  } finally {
    backup.loading = false
  }
}

function fmtSize(bytes) {
  if (bytes == null) return '-'
  if (bytes >= GB) return `${gb(bytes)} GB`
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  return `${(bytes / 1024).toFixed(1)} KB`
}

function fmtDur(s) {
  if (s == null) return '-'
  if (s < 60) return `${s}s`
  return `${Math.floor(s / 60)}m${Math.round(s % 60)}s`
}

/* ==================== 初始化 ==================== */
const activeTab = ref('slow')

// ---- 数据库机器 SSH 终端（机器库选机，D13 反哺 K8s 终端能力）----
const dbaTermVisible = ref(false)
const dbaTermTitle = ref('')
const dbaTermUrl = ref('')
const dbaMachines = ref([])
const dbaMachineDialog = ref(false)

async function openDbaMachines() {
  dbaMachineDialog.value = true
  try {
    const r = await http.get('/machines')
    dbaMachines.value = r.items || []
  } catch { /* 拦截器提示 */ }
}

function dbaSsh(row) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  const token = localStorage.getItem('qianshu.token') || ''
  dbaTermTitle.value = `SSH · ${row.ssh_user}@${row.ip}`
  dbaTermUrl.value = `${proto}://${location.host}/ws/machines/${row.id}/ssh?token=${token}&cols=120&rows=32`
  dbaMachineDialog.value = false
  dbaTermVisible.value = true
}

// 切页签自动加载：首次进入空间/备份/洞察等页签时自动查询，避免有数据仍显示空表
watch(activeTab, (tab) => {
  if (tab === 'insight' && !insight.rows.length && insight.instanceId) loadInsight()
  if (tab === 'space' && !space.trend.length && space.instanceId) loadSpace()
  if (tab === 'backup' && !backup.backups.length && backup.instanceId) loadBackups()
  if (tab === 'redis' && !redis.rows.length && redis.instanceId) loadRedisKeys()
})

onMounted(async () => {
  await loadInstances()
  if (instances.value.length) {
    slow.instanceId = instances.value[0].instance_id
    const withInsight = instances.value.find(i => i.sql_insight)
    if (withInsight) insight.instanceId = withInsight.instance_id
    space.instanceId = instances.value[0].instance_id
    backup.instanceId = instances.value[0].instance_id
  }
  loadSlowTop()
  loadRedisInstances()
  // 竞态兜底：实例列表返回前切过页签会被 watch 跳过，加载后重试当前页签
  const t0 = activeTab.value
  if (t0 === 'insight' && !insight.rows.length && insight.instanceId) loadInsight()
  if (t0 === 'space' && !space.trend.length && space.instanceId) loadSpace()
  if (t0 === 'backup' && !backup.backups.length && backup.instanceId) loadBackups()
})
</script>

<template>
  <div class="page">
    <el-tabs v-model="activeTab" class="dba-tabs">
      <!-- ============ 页签1：慢SQL ============ -->
      <el-tab-pane :label="t('dba.tabs.slowlog')" name="slow">
        <el-card shadow="never">
          <div class="toolbar">
            <el-button v-if="canTerm" type="primary" plain @click="openDbaMachines">{{ t('dba.actions.machineSsh') }}</el-button>
            <el-select v-model="slow.instanceId" filterable style="width: 320px" :placeholder="t('dba.filter.selectInstance')">
              <el-option v-for="i in instances" :key="i.instance_id" :value="i.instance_id" :label="instLabel(i)" />
            </el-select>
            <el-radio-group v-model="slow.days">
              <el-radio-button :value="1">{{ t('dba.filter.last1d') }}</el-radio-button>
              <el-radio-button :value="3">{{ t('dba.filter.last3d') }}</el-radio-button>
              <el-radio-button :value="7">{{ t('dba.filter.last7d') }}</el-radio-button>
            </el-radio-group>
            <el-button type="primary" :loading="slow.loading" @click="loadSlowTop">{{ t('dba.actions.query') }}</el-button>
            <span class="sub">{{ t('dba.filter.slowHint') }}</span>
          </div>
          <el-table stripe :data="slow.rows" v-loading="slow.loading" size="small" border>
            <el-table-column type="index" label="#" width="46" />
            <el-table-column prop="db" :label="t('dba.table.db')" width="130" show-overflow-tooltip />
            <el-table-column prop="sql_text" label="SQL" min-width="320" show-overflow-tooltip />
            <el-table-column prop="count" :label="t('dba.table.execCount')" width="80" sortable />
            <el-table-column :label="t('dba.table.avg')" width="90" sortable sort-by="avg_ms">
              <template #default="{ row }">{{ row.avg_ms }} ms</template>
            </el-table-column>
            <el-table-column :label="t('dba.table.max')" width="90" sortable sort-by="max_ms">
              <template #default="{ row }">{{ row.max_ms }} ms</template>
            </el-table-column>
            <el-table-column :label="t('dba.table.totalTime')" width="100" sortable sort-by="total_ms">
              <template #default="{ row }">{{ (row.total_ms / 1000).toFixed(1) }} s</template>
            </el-table-column>
            <el-table-column :label="t('dba.table.maxRows')" width="110" sortable sort-by="max_rows_examined">
              <template #default="{ row }">{{ row.max_rows_examined.toLocaleString() }}</template>
            </el-table-column>
            <el-table-column :label="t('dba.table.action')" width="80">
              <template #default="{ row }">
                <el-button link type="primary" size="small" @click="openSlowDetail(row)">{{ t('dba.actions.detail') }}</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>

      <!-- ============ 页签2：SQL 洞察明细 ============ -->
      <el-tab-pane :label="t('dba.tabs.insight')" name="insight">
        <el-card shadow="never">
          <el-alert v-if="noCredential" type="warning" :closable="false" class="hint"
                    :title="instanceNote || t('dba.empty.noCredential')" show-icon />
          <div class="toolbar">
            <el-select v-model="insight.instanceId" filterable style="width: 320px" :placeholder="t('dba.filter.selectInstance')">
              <el-option v-for="i in instances" :key="i.instance_id" :value="i.instance_id" :label="instLabel(i)" :disabled="!i.sql_insight">
                <span>{{ instLabel(i) }}</span>
                <el-tag v-if="!i.sql_insight" size="small" type="info" style="margin-left: 8px">{{ t('dba.status.notEnabled') }}</el-tag>
              </el-option>
            </el-select>
            <el-radio-group v-model="insight.hours">
              <el-radio-button :value="1">{{ t('dba.filter.last1h') }}</el-radio-button>
              <el-radio-button :value="3">{{ t('dba.filter.last3h') }}</el-radio-button>
              <el-radio-button :value="12">{{ t('dba.filter.last12h') }}</el-radio-button>
              <el-radio-button :value="24">{{ t('dba.filter.last24h') }}</el-radio-button>
            </el-radio-group>
            <el-input v-model="insight.keyword" :placeholder="t('dba.filter.sqlKeyword')" clearable style="width: 200px" @keyup.enter="searchInsight" />
            <el-button type="primary" :loading="insight.loading" @click="searchInsight">{{ t('dba.actions.query') }}</el-button>
          </div>
          <el-alert type="info" :closable="false" class="hint"
            :title="t('dba.empty.insightHint')" />
          <el-table stripe :data="insight.rows" v-loading="insight.loading" size="small" border>
            <el-table-column prop="time" :label="t('dba.table.time')" width="170" />
            <el-table-column prop="db" :label="t('dba.table.db')" width="130" show-overflow-tooltip />
            <el-table-column prop="user" :label="t('dba.table.account')" width="110" />
            <el-table-column prop="host" :label="t('dba.table.client')" width="140" />
            <el-table-column prop="rows_sent" :label="t('dba.table.rowsSent')" width="80" />
            <el-table-column prop="sql_text" label="SQL" min-width="320" show-overflow-tooltip />
          </el-table>
          <div class="pager">
            <el-pagination background layout="total, prev, pager, next" :total="insight.total"
              :page-size="INSIGHT_PAGE_SIZE" :current-page="insight.page"
              @current-change="(p) => { insight.page = p; loadInsight() }" />
          </div>
        </el-card>
      </el-tab-pane>

      <!-- ============ 页签3：空间趋势 ============ -->
      <el-tab-pane :label="t('dba.tabs.space')" name="space">
        <el-card shadow="never">
          <div class="toolbar">
            <el-select v-model="space.instanceId" filterable style="width: 320px" :placeholder="t('dba.filter.selectInstance')">
              <el-option v-for="i in instances" :key="i.instance_id" :value="i.instance_id" :label="instLabel(i)" />
            </el-select>
            <el-radio-group v-model="space.days">
              <el-radio-button :value="7">{{ t('dba.filter.last7d') }}</el-radio-button>
              <el-radio-button :value="30">{{ t('dba.filter.last30d') }}</el-radio-button>
              <el-radio-button :value="90">{{ t('dba.filter.last90d') }}</el-radio-button>
            </el-radio-group>
            <el-button type="primary" :loading="space.loading" @click="loadSpace">{{ t('dba.actions.query') }}</el-button>
            <el-button :loading="spaceJob.collecting" @click="collectNow">{{ t('dba.actions.collectNow') }}</el-button>
            <span v-if="spaceJob.status?.last_finished_at" class="sub">
              {{ t('dba.filter.lastCollect', { date: spaceJob.status.stat_date, n: spaceJob.status.instances }) }}
            </span>
          </div>
          <el-row v-if="space.current" :gutter="12" class="stat-row">
            <el-col :span="6"><div class="stat"><div class="stat-label">{{ t('dba.stat.dataSize') }}</div><div class="stat-val">{{ gb(space.current.data_size) }} GB</div></div></el-col>
            <el-col :span="6"><div class="stat"><div class="stat-label">{{ t('dba.stat.diskUsed') }}</div><div class="stat-val">{{ gb(space.current.disk_used) }} GB</div></div></el-col>
            <el-col :span="6"><div class="stat"><div class="stat-label">{{ t('dba.stat.logSize') }}</div><div class="stat-val">{{ gb(space.current.log_size) }} GB</div></div></el-col>
            <el-col :span="6"><div class="stat"><div class="stat-label">{{ t('dba.stat.backupTotal') }}</div><div class="stat-val">{{ gb(space.current.backup_size) }} GB</div></div></el-col>
          </el-row>
          <MetricChart v-if="space.trend.length" :title="t('dba.tabs.space')" unit="GB" :series="spaceSeries()" :height="280" />
          <el-empty v-else-if="!space.loading"
                    :description="space.queried ? t('dba.empty.spaceNoData') : t('dba.empty.spaceSelectFirst')" />
        </el-card>
      </el-tab-pane>

      <!-- ============ 页签4：Redis 大key/热key ============ -->
      <el-tab-pane :label="t('dba.tabs.redis')" name="redis">
        <el-card shadow="never">
          <div class="toolbar">
            <el-select v-model="redis.instanceId" filterable style="width: 320px" :placeholder="t('dba.filter.selectRedis')">
              <el-option v-for="i in redis.instances" :key="i.instance_id" :value="i.instance_id" :label="redisLabel(i)" />
            </el-select>
            <el-radio-group v-model="redis.type" @change="loadRedisKeys">
              <el-radio-button value="big">{{ t('dba.filter.bigKey') }}</el-radio-button>
              <el-radio-button value="hot">{{ t('dba.filter.hotKey') }}</el-radio-button>
            </el-radio-group>
            <el-button type="primary" :loading="redis.loading" @click="loadRedisKeys">{{ t('dba.actions.query') }}</el-button>
            <span class="sub">{{ t('dba.filter.redisHint') }}</span>
          </div>
          <el-alert v-if="redis.type === 'hot' && !redis.rows.length && !redis.loading" type="info" :closable="false" class="hint"
            :title="t('dba.empty.hotKeyHint')" />
          <el-alert v-else-if="redis.type === 'big' && !redis.rows.length && !redis.loading" type="info" :closable="false" class="hint"
            :title="t('dba.empty.bigKeyEmpty')" />
          <el-table stripe :data="redis.rows" v-loading="redis.loading" size="small" border>
            <el-table-column type="index" label="#" width="46" />
            <el-table-column prop="key" label="Key" min-width="360" show-overflow-tooltip />
            <el-table-column prop="key_type" :label="t('dba.table.type')" width="90" />
            <el-table-column prop="db" label="DB" width="60" />
            <el-table-column prop="node_id" :label="t('dba.table.node')" width="200" show-overflow-tooltip />
            <el-table-column v-if="redis.type === 'big'" :label="t('dba.table.size')" width="110" sortable sort-by="size">
              <template #default="{ row }">{{ fmtSize(row.size) }}</template>
            </el-table-column>
            <template v-if="redis.type === 'hot'">
              <el-table-column prop="hot" :label="t('dba.table.hotValue')" width="100" sortable />
              <el-table-column prop="lfu" :label="t('dba.table.lfu')" width="100" sortable />
              <el-table-column :label="t('dba.table.inFlow')" width="110">
                <template #default="{ row }">{{ fmtSize(row.in_bytes) }}</template>
              </el-table-column>
              <el-table-column :label="t('dba.table.outFlow')" width="110">
                <template #default="{ row }">{{ fmtSize(row.out_bytes) }}</template>
              </el-table-column>
            </template>
          </el-table>
        </el-card>
      </el-tab-pane>

      <!-- ============ 页签5：备份状态 ============ -->
      <el-tab-pane :label="t('dba.tabs.backup')" name="backup">
        <el-card shadow="never">
          <div class="toolbar">
            <el-select v-model="backup.instanceId" filterable style="width: 320px" :placeholder="t('dba.filter.selectInstance')">
              <el-option v-for="i in instances" :key="i.instance_id" :value="i.instance_id" :label="instLabel(i)" />
            </el-select>
            <el-radio-group v-model="backup.days">
              <el-radio-button :value="3">{{ t('dba.filter.last3d') }}</el-radio-button>
              <el-radio-button :value="7">{{ t('dba.filter.last7d') }}</el-radio-button>
              <el-radio-button :value="30">{{ t('dba.filter.last30d') }}</el-radio-button>
            </el-radio-group>
            <el-button type="primary" :loading="backup.loading" @click="loadBackups">{{ t('dba.actions.query') }}</el-button>
          </div>
          <el-descriptions v-if="backup.policy" :column="4" border size="small" class="policy">
            <el-descriptions-item :label="t('dba.form.backupMethod')">{{ backup.policy.backup_method }}</el-descriptions-item>
            <el-descriptions-item :label="t('dba.form.logBackup')">{{ backup.policy.backup_log }}</el-descriptions-item>
            <el-descriptions-item :label="t('dba.form.dataRetention')">{{ t('dba.form.retentionDays', { n: backup.policy.retention_days }) }}</el-descriptions-item>
            <el-descriptions-item :label="t('dba.form.logRetention')">{{ t('dba.form.retentionHours', { n: backup.policy.log_retention_hours }) }}</el-descriptions-item>
          </el-descriptions>
          <el-table stripe :data="backup.backups" v-loading="backup.loading" size="small" border>
            <el-table-column prop="start_time" :label="t('dba.table.startTime')" width="170" />
            <el-table-column prop="end_time" :label="t('dba.table.endTime')" width="170" />
            <el-table-column :label="t('dba.table.duration')" width="80">
              <template #default="{ row }">{{ fmtDur(row.duration_s) }}</template>
            </el-table-column>
            <el-table-column :label="t('dba.table.type')" width="130">
              <template #default="{ row }">
                <el-tag size="small" :type="row.type === 'FullBackup' ? 'primary' : 'warning'">
                  {{ row.type === 'FullBackup' ? t('dba.status.fullBackup') : t('dba.status.incrBackup') }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="mode" :label="t('dba.table.trigger')" width="90">
              <template #default="{ row }">{{ row.mode === 'Automated' ? t('dba.status.auto') : t('dba.status.manual') }}</template>
            </el-table-column>
            <el-table-column prop="method" :label="t('dba.table.method')" width="90" />
            <el-table-column :label="t('dba.table.status')" width="90">
              <template #default="{ row }">
                <el-tag size="small" :type="backupStatus(row.status).type">{{ backupStatus(row.status).label }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column :label="t('dba.table.size')" width="110">
              <template #default="{ row }">{{ fmtSize(row.size) }}</template>
            </el-table-column>
            <el-table-column prop="backup_id" :label="t('dba.table.backupId')" width="120" />
          </el-table>
        </el-card>
      </el-tab-pane>
    </el-tabs>

    <el-dialog v-model="dbaMachineDialog" :title="t('dba.dialog.machineTitle')" width="520px">
      <el-table :data="dbaMachines" size="small" border :empty-text="t('dba.empty.machineEmpty')">
        <el-table-column prop="host_name" :label="t('dba.table.hostName')" width="130" />
        <el-table-column prop="ip" label="IP" width="140" />
        <el-table-column prop="role" :label="t('dba.table.role')" width="100" />
        <el-table-column :label="t('dba.table.action')" width="90">
          <template #default="{ row }">
            <el-button v-if="canTerm" size="small" text type="success" @click="dbaSsh(row)">SSH</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-dialog>
    <TermDialog v-model="dbaTermVisible" :title="dbaTermTitle" :ws-url="dbaTermUrl" />

    <!-- ============ 慢SQL明细弹窗（单模板的逐次执行记录） ============ -->
    <el-dialog v-model="slowDetail.visible" :title="t('dba.dialog.slowDetail')" width="920px" top="5vh">
      <div v-if="slowDetail.keywordRow" class="sub" style="margin-bottom: 10px">
        {{ t('dba.detail.onlyTemplate') }}{{ slowDetail.keywordRow.sql_text.slice(0, 120) }}{{ slowDetail.keywordRow.sql_text.length > 120 ? '…' : '' }}
        <template v-if="slowDetail.note"> · {{ slowDetail.note }}</template>
      </div>
      <el-alert v-if="!slowDetail.loading && !slowDetail.rows.length" type="info" :closable="false"
                :title="t('dba.empty.slowDetailEmpty')" style="margin-bottom: 10px" />
      <el-table stripe :data="slowDetail.rows" v-loading="slowDetail.loading" size="small" border max-height="480">
        <el-table-column prop="time" :label="t('dba.table.time')" width="170" />
        <el-table-column prop="db" :label="t('dba.table.db')" width="120" show-overflow-tooltip />
        <el-table-column prop="host" :label="t('dba.table.client')" width="150" show-overflow-tooltip />
        <el-table-column :label="t('dba.table.duration')" width="90">
          <template #default="{ row }">{{ row.query_ms }} ms</template>
        </el-table-column>
        <el-table-column :label="t('dba.table.lockWait')" width="80">
          <template #default="{ row }">{{ row.lock_ms }} ms</template>
        </el-table-column>
        <el-table-column :label="t('dba.table.rowsExamined')" width="100">
          <template #default="{ row }">{{ row.rows_examined.toLocaleString() }}</template>
        </el-table-column>
        <el-table-column prop="sql_text" label="SQL" min-width="280" show-overflow-tooltip />
      </el-table>
      <div class="pager">
        <el-pagination background layout="total, prev, pager, next" :total="slowDetail.total"
          :page-size="SLOW_PAGE_SIZE" :current-page="slowDetail.page"
          @current-change="(p) => { slowDetail.page = p; loadSlowDetail() }" />
      </div>
    </el-dialog>
  </div>
</template>

<script>
export default { name: 'Dba' }
</script>

<style scoped>
.page {
  padding: 16px;
}
.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 14px;
  flex-wrap: wrap;
}
.sub {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.hint {
  margin-bottom: 10px;
}
.stat-row {
  margin-bottom: 14px;
}
.stat {
  border: 1px solid var(--el-border-color-light);
  border-radius: 6px;
  padding: 10px 14px;
}
.stat-label {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.stat-val {
  font-size: 20px;
  font-weight: 600;
  margin-top: 4px;
}
.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}
.policy {
  margin-bottom: 14px;
}
</style>
