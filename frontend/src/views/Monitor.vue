<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { useRoute, useRouter } from 'vue-router'

import { listAssets } from '@/api/assets'
import { costAnomalies } from '@/api/cost'
import { collectStatus, instanceHistory, instanceMetrics, triggerCollect } from '@/api/monitor'
import MetricChart from '@/components/MetricChart.vue'
import { t } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import { assetStatusLabel } from '@/utils/format'

// 采集是写操作（后端 require_roles admin/operator）；viewer 不应看到必然 403 的按钮
const auth = useAuthStore()
const canCollect = computed(() => ['admin', 'operator'].includes(auth.role))

const PRODUCTS = [
  { key: 'ecs', label: 'ECS' },
  { key: 'rds', label: 'RDS' },
  { key: 'redis', label: 'Redis' },
  { key: 'slb', label: 'SLB' },
  // VMware 同时提供 VM 与宿主机曲线，避免只有告警而无法查看对应指标
  { key: 'vmware_vm', labelKey: 'monitor.product.vmware_vm' },
  { key: 'vmware_host', labelKey: 'monitor.product.vmware_host' },
]

const product = ref('ecs')
const range = ref('24h')
const instances = ref([])
const instanceId = ref('')
const loadingList = ref(false)
const loadingMetrics = ref(false)
const instancesLoaded = ref(false)
const instancesError = ref('')   // 加载失败态（区别于"暂无资产"）
const collecting = ref(false)
const metrics = ref([])
const metricsError = ref('')   // 指标查询失败态：不能拿上一个实例的曲线冒充当前实例

// ---- 异动入口摘要：显示计数，并通过「查看全部」进入完整异动页 ----
// 深链：/monitor?product=ecs&instance=i-xxx（异动"点击跳对应页"直接落到对象）
const route = useRoute()
const router = useRouter()
function applyDeepLink() {
  const p = route.query.product
  const i = route.query.instance ? String(route.query.instance) : ''
  if (p && PRODUCTS.some((x) => x.key === p) && String(p) !== product.value) {
    product.value = String(p)   // 触发 loadInstances，实例用 route.query 实时值兜底
    return true
  }
  if (i && i !== instanceId.value && instances.value.some((x) => x.instance_id === i)) {
    instanceId.value = i        // 同产品：直接切实例
  }
  return false
}
watch(() => [route.query.product, route.query.instance], applyDeepLink)

const monitorAnomalyTotal = ref(0)
const monitorAnomalyTypes = ref({})
const anomaliesLoading = ref(false)
const anomaliesError = ref(false)   // 查询失败与无异动必须区分
const ANOMALY_TYPE_LABELS = {
  traffic_spike: 'monitor.anomaly.traffic_spike',
  growth_accel: 'monitor.anomaly.growth_accel',
  host_down: 'monitor.anomaly.host_down',
  // 与异动页、成本页统一类型名称，避免同一类型出现不同叫法
  aux_spillover: 'monitor.anomaly.aux_spillover',
}

async function loadAnomalies() {
  anomaliesLoading.value = true
  try {
    const data = await costAnomalies({ domain: 'monitor', page_size: 100 })
    monitorAnomalyTotal.value = data.total || 0
    const byType = {}
    for (const a of data.items || []) byType[a.type] = (byType[a.type] || 0) + 1
    monitorAnomalyTypes.value = byType
    anomaliesError.value = false
  } catch (e) {
    // 失败时不清零：清零会把接口故障伪装成「近 30 天无异动」
    anomaliesError.value = true
  } finally {
    anomaliesLoading.value = false
  }
}

function gotoAnomalies() {
  router.push({ path: '/anomalies', query: { domain: 'monitor' } })
}

const instanceOptions = computed(() =>
  instances.value.map((i) => ({
    value: i.instance_id,
    // 下拉 label 显示账号归属，便于多账号场景识别实例
    label: t('monitor.instance.option', {
      name: i.name || i.instance_id,
      id: i.instance_id,
      acct: i.account_name ? t('monitor.instance.acct', { a: i.account_name }) : '',
    }),
    status: i.status,
  })),
)

const currentInstance = computed(
  () => instances.value.find((i) => i.instance_id === instanceId.value) || null,
)

async function loadInstances() {
  const currentProduct = product.value
  loadingList.value = true
  instancesError.value = ''
  instances.value = []
  instanceId.value = ''
  try {
    const data = await listAssets({ product: currentProduct, page_size: 200 })
    if (currentProduct !== product.value) return // 已切到别的产品，丢弃过期结果
    instances.value = data.items.filter((i) => i.status !== 'Deleted')
    // 深链目标不在前 200 台时（后端页大小上限 200），按实例 ID 精确补取一条，
    // 不能静默退回首台让用户以为跳对了
    const want = route.query.instance ? String(route.query.instance) : ''
    if (want && !instances.value.some((i) => i.instance_id === want)
        && (data.total || 0) > instances.value.length) {
      try {
        const one = await listAssets({ product: currentProduct, keyword: want, page_size: 1 })
        const hit = (one.items || []).find((i) => i.instance_id === want)
        if (hit && hit.status !== 'Deleted') instances.value.unshift(hit)
      } catch { /* 补取失败仍退回首台 */ }
      if (currentProduct !== product.value) return
    }
    if (instances.value.length) {
      // 深链进来的实例优先（异动"点击跳对应页"），不存在再退回首台
      instanceId.value = instances.value.some((i) => i.instance_id === want)
        ? want : instances.value[0].instance_id
    }
  } catch (e) {
    // 加载失败必须显示错误与重试入口，不能吞成“暂无资产”
    if (currentProduct === product.value) {
      instancesError.value = e?.response?.data?.detail || t('monitor.instances.load_fail')
    }
  } finally {
    if (currentProduct === product.value) { loadingList.value = false; instancesLoaded.value = true }
  }
}

async function runCollect() {
  collecting.value = true
  const vmware = product.value.startsWith('vmware_')
  try {
    // 只采集当前页签产品，确保返回点数与当前产品一致
    const r = await triggerCollect(product.value)
    const points = r.points_written ?? 0
    const errs = r.errors || []
    const warns = r.warnings || []
    if (vmware) {
      if (points > 0) {
        ElMessage.success(t('monitor.collect.vmware_ok', { points }))
      } else if (warns.length || !errs.length) {
        ElMessage.warning(t('monitor.collect.vmware_empty'))
      } else {
        ElMessage.error(t('monitor.collect.vmware_fail', { n: errs.length }))
      }
    } else if (errs.length) {
      ElMessage.warning(t('monitor.collect.partial', {
        points, n: errs.length,
      }))
    } else {
      ElMessage.success(t('monitor.collect.ok', { points, err_part: '' }))
    }
    loadMetrics()
    loadCollectStatus()   // 触发后同步刷新按钮旁的最近采集状态
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('monitor.collect.fail'))
  } finally {
    collecting.value = false
  }
}

// 「立即采集」旁显示最近采集状态，与总览「指标采集」卡共用 /monitor/collect/status
const collector = ref(null)
async function loadCollectStatus() {
  try {
    collector.value = await collectStatus()
  } catch { collector.value = null }
}
// vSphere 采集成功但 0 样本通常与统计级别或只读权限有关；页面需展示具体原因、
// 当前账号本次采集点数和可展开的分步排障教程，并提供云账号管理与重试入口，
// 避免只显示“暂无数据”。
const isVmware = computed(() => product.value.startsWith('vmware_'))
const vsphereStatus = computed(() => collector.value?.vsphere || null)
const vmwareHelpVisible = ref(false)
const vsphereErrors = computed(() => vsphereStatus.value?.errors || [])
const vmwareAccounts = computed(() => vsphereStatus.value?.account_details || [])
const vmwareWarning = computed(() => {
  if (!isVmware.value) return false
  const v = vsphereStatus.value
  if (!v || !v.accounts) return false
  // 上一次采集若是另一个 VMware 页签的范围，不在本页签展示（避免拿别的产品状态吓人）
  if (v.scope && v.scope !== product.value) return false
  return (v.errors || []).length > 0 || (v.points_written ?? 0) === 0
})

const collectStatusLine = computed(() => {
  const c = isVmware.value ? vsphereStatus.value : collector.value
  if (!c) return ''
  // 状态是「上一轮」的：范围与当前页签不符时不展示，避免把 ECS 的状态挂在 VMware 页上
  if (c.scope && c.scope !== 'all' && c.scope !== product.value) return ''
  return t(isVmware.value ? 'monitor.collect.status_vmware' : 'monitor.collect.status_line', {
    time: c.last_finished_at ? String(c.last_finished_at).replace('T', ' ').slice(0, 16) : t('monitor.collect.never'),
    points: c.points_written ?? 0,
    err: (c.errors || []).length,
  })
})

async function loadMetrics() {
  if (!instanceId.value) {
    metrics.value = []
    return
  }
  const currentProduct = product.value
  const currentInstance = instanceId.value
  loadingMetrics.value = true
  metricsError.value = ''
  // 切实例/时间档先清图：请求在途或失败时，旧实例曲线不能继续挂在新区间标题下
  metrics.value = []
  try {
    // 近30天走 VictoriaMetrics 本地历史库（CMS 只保留 31 天），其余直查云监控；
    // VMware 指标只在本机 VM（vSphere 采集器写入），全部档位走本地库
    const fetcher = (range.value === '30d' || currentProduct.startsWith('vmware_'))
      ? instanceHistory : instanceMetrics
    const data = await fetcher(currentProduct, currentInstance, range.value)
    if (currentProduct !== product.value || currentInstance !== instanceId.value) return
    metrics.value = data.metrics || []
  } catch (e) {
    if (currentProduct === product.value && currentInstance === instanceId.value) {
      metricsError.value = e?.response?.data?.detail || t('monitor.empty.metrics_fail')
    }
  } finally {
    if (currentProduct === product.value && currentInstance === instanceId.value) {
      loadingMetrics.value = false
    }
  }
}

watch(product, () => {
  metrics.value = []
  loadInstances()
  loadCollectStatus()   // 状态按产品范围展示，切页签要重新取上一轮状态
})
watch([instanceId, range], loadMetrics)

// 自动刷新采集状态：警告条来自后台每轮采集结果，页面常开时也需更新，
// 不能只在进入页面、切换页签或手动采集时刷新。
const STATUS_POLL_MS = 60000
let statusTimer = null

onMounted(() => {
  if (!applyDeepLink()) loadInstances()   // 深链 product 变更会经 watch 触发加载
  loadAnomalies()
  loadCollectStatus()
  statusTimer = setInterval(loadCollectStatus, STATUS_POLL_MS)
})

onUnmounted(() => {
  if (statusTimer) clearInterval(statusTimer)
})
</script>

<template>
  <div class="page">
    <el-tabs v-model="product">
      <el-tab-pane v-for="p in PRODUCTS" :key="p.key" :label="p.labelKey ? t(p.labelKey) : p.label" :name="p.key" />
    </el-tabs>

    <div class="toolbar">
      <el-select
        v-model="instanceId"
        filterable
        class="instance-select"
        :placeholder="t('monitor.toolbar.select_instance')"
        :loading="loadingList"
      >
        <el-option v-for="o in instanceOptions" :key="o.value" :label="o.label" :value="o.value" />
      </el-select>
      <el-radio-group v-model="range">
        <el-radio-button value="1h">{{ t('monitor.range.1h') }}</el-radio-button>
        <el-radio-button value="24h">{{ t('monitor.range.24h') }}</el-radio-button>
        <el-radio-button value="7d">{{ t('monitor.range.7d') }}</el-radio-button>
        <el-radio-button value="30d">{{ t('monitor.range.30d') }}</el-radio-button>
      </el-radio-group>
      <el-button-group>
        <el-button :loading="loadingMetrics" @click="loadMetrics">{{ t('monitor.toolbar.refresh') }}</el-button>
        <!-- 按产品独立采集：按钮和返回点数均限定为当前页签产品 -->
        <el-button v-if="canCollect" :loading="collecting" :title="t('monitor.toolbar.collect_scope_tip')"
                   @click="runCollect">{{ t('monitor.toolbar.collect_scope') }}</el-button>
      </el-button-group>
      <!-- 采集状态与触发按钮同位展示 -->
      <span v-if="collectStatusLine" class="sub collect-status">{{ collectStatusLine }}</span>
      <div style="flex: 1" />
      <span v-if="currentInstance" class="sub">
        {{ currentInstance.name }} · {{ currentInstance.region }} ·
        <el-tag size="small">{{ assetStatusLabel(currentInstance.status) }}</el-tag>
      </span>
    </div>

    <!-- 接了 vCenter 却没图时给出环境侧原因 + 分步教程，避免用户判断为"功能没做/坏了" -->
    <el-alert v-if="vmwareWarning" :title="t('monitor.vmware.warn_title')" type="warning"
              :closable="false" show-icon class="vmware-warn">
      <div class="vmware-warn-body">
        <div>{{ t('monitor.vmware.warn_body') }}</div>
        <div v-for="a in vmwareAccounts" :key="a.name" class="sub vmware-account">
          {{ t('monitor.vmware.account_line', { name: a.name, points: a.points, err: a.error ? ` · ${a.error}` : '' }) }}
          <!-- 后端已读回 vCenter 实时档统计级别：0 时直接点破原因，不用用户自己去 vSphere 里猜 -->
          <div v-if="a.stats_level !== undefined" class="vmware-stats-level"
               :class="{ 'is-bad': a.stats_level === 0 }">
            {{ a.stats_level === 0
              ? t('monitor.vmware.stats_level_zero')
              : t('monitor.vmware.stats_level', { level: a.stats_level }) }}
            <span v-if="a.interval_enabled === false"> · {{ t('monitor.vmware.interval_disabled') }}</span>
          </div>
          <div v-else-if="a.hist_levels && a.hist_levels.length" class="vmware-stats-level"
               :class="{ 'is-bad': a.hist_levels.every(l => l === 0) }">
            {{ a.hist_levels.every(l => l === 0)
              ? t('monitor.vmware.stats_level_all_zero')
              : t('monitor.vmware.stats_level_hist', { levels: [...new Set(a.hist_levels)].join('/') }) }}
          </div>
          <!-- 0 样本时后端逐档探测：全空 = vCenter 性能数据没生成，避免让用户白折腾统计级别/权限 -->
          <div v-if="a.perf_probes" class="vmware-stats-level"
               :class="{ 'is-bad': Object.values(a.perf_probes).every(v => v === 0) }">
            <template v-if="Object.values(a.perf_probes).every(v => v === 0)">
              {{ t('monitor.vmware.probes_dead') }}
            </template>
            <template v-else-if="Object.values(a.perf_probes).every(v => Number.isInteger(v))">
              {{ t('monitor.vmware.probes_detail', {
                s20: a.perf_probes['20'], s300: a.perf_probes['300'], s1800: a.perf_probes['1800']
              }) }}
            </template>
            <template v-else>
              {{ t('monitor.vmware.probes_error') }}
            </template>
          </div>
        </div>
        <div v-if="vsphereErrors.length" class="vmware-errors">
          <div class="sub">{{ t('monitor.vmware.errors_title') }}</div>
          <ul>
            <li v-for="(e, i) in vsphereErrors" :key="i">{{ e }}</li>
          </ul>
        </div>
        <el-button link type="primary" @click="vmwareHelpVisible = true">
          {{ t('monitor.vmware.help_btn') }}
        </el-button>
      </div>
    </el-alert>

    <!-- vCenter 排障教程：写清在 vSphere Client 里点哪里、怎么验、回来怎么重试 -->
    <el-dialog v-model="vmwareHelpVisible" :title="t('monitor.vmware.help_title')"
               width="760px" top="6vh">
      <div class="vmware-help">
        <p class="vmware-help-intro">{{ t('monitor.vmware.help_intro') }}</p>
        <ol>
          <li v-for="n in 6" :key="n">{{ t(`monitor.vmware.step${n}`) }}</li>
        </ol>
        <el-alert :title="t('monitor.vmware.help_note')" type="info" :closable="false" show-icon />
      </div>
      <template #footer>
        <el-button @click="vmwareHelpVisible = false">{{ t('layout.cancel') }}</el-button>
        <el-button @click="vmwareHelpVisible = false; router.push('/clouds')">{{ t('monitor.vmware.open_clouds') }}</el-button>
        <el-button v-if="canCollect" type="primary" :loading="collecting"
                   @click="vmwareHelpVisible = false; runCollect()">{{ t('monitor.vmware.retry') }}</el-button>
      </template>
    </el-dialog>

    <el-card shadow="never" class="anomaly-card" v-loading="anomaliesLoading">
      <template #header>
        <div class="anomaly-head">
          <span>{{ t('monitor.anomaly.title') }}</span>
          <span class="sub">{{ t('monitor.anomaly.subtitle') }}</span>
        </div>
      </template>
      <div class="anomaly-summary">
        <template v-if="monitorAnomalyTotal > 0">
          <span class="sub">{{ t('monitor.anomaly.count_pre') }}<b class="anomaly-count">{{ monitorAnomalyTotal }}</b>{{ t('monitor.anomaly.count_suf') }}</span>
          <el-tag v-for="(n, ty) in monitorAnomalyTypes" :key="ty" type="danger" size="small" effect="plain">
            {{ t(ANOMALY_TYPE_LABELS[ty] || ty) }} {{ n }}
          </el-tag>
        </template>
        <span v-else-if="anomaliesError" class="sub error">{{ t('monitor.anomaly.load_fail') }}</span>
        <span v-else class="sub">{{ t('monitor.anomaly.none') }}</span>
        <el-button v-if="anomaliesError" link type="primary" size="small" @click="loadAnomalies">{{ t('common.retry') }}</el-button>
        <el-button type="primary" plain size="small" @click="gotoAnomalies">{{ t('monitor.anomaly.view_all') }}</el-button>
      </div>
    </el-card>

    <div v-loading="loadingMetrics">
      <!-- 24h 档直查云监控要数秒：给图表区骨架占位，避免整页空白被当成"坏了" -->
      <div v-if="loadingMetrics && !metrics.length" class="charts">
        <div v-for="i in 4" :key="i" class="chart-card skeleton-card">
          <div class="sk-line sk-title" />
          <div class="sk-chart" />
        </div>
      </div>
      <template v-else-if="metrics.length">
        <div class="charts">
          <div v-for="m in metrics" :key="m.metric" class="chart-card">
            <div v-if="!m.series.length" class="empty-metric">
              <span>{{ t('monitor.chart.no_data', { label: m.label }) }}</span>
            </div>
            <MetricChart v-else :title="m.label" :unit="m.unit" :series="m.series" />
          </div>
        </div>
      </template>
      <el-empty v-else-if="!loadingList && !loadingMetrics && instancesError"
                :description="t('monitor.empty.load_fail', { err: instancesError })">
        <el-button type="primary" @click="loadInstances">{{ t('monitor.empty.retry') }}</el-button>
      </el-empty>
      <el-empty v-else-if="!loadingMetrics && instanceId && metricsError"
                :description="metricsError">
        <el-button type="primary" @click="loadMetrics">{{ t('common.retry') }}</el-button>
      </el-empty>
      <el-empty v-else-if="!loadingList && !loadingMetrics && !instanceId && instancesLoaded"
                :description="t('monitor.empty.no_assets')">
        <el-button type="primary" @click="router.push('/ops')">{{ t('monitor.empty.go_ops') }}</el-button>
      </el-empty>
      <el-empty v-else-if="!loadingList && !loadingMetrics && !instancesLoaded"
                :description="t('monitor.empty.loading')" />
      <el-empty v-else-if="!loadingMetrics && instanceId" :description="t('monitor.empty.no_data')" />
    </div>
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 12px;
  margin-bottom: 12px;
  align-items: center;
}
.toolbar :deep(.el-radio-group),
.toolbar :deep(.el-button-group) {
  flex-shrink: 0;
}
.instance-select {
  width: 380px;
  max-width: 100%;
  flex: 0 1 380px;
}
.skeleton-card {
  height: 276px;
  padding: 14px;
  box-sizing: border-box;
}
.sk-line, .sk-chart {
  background: linear-gradient(90deg, var(--el-fill-color) 25%, var(--el-fill-color-lighter) 37%, var(--el-fill-color) 63%);
  background-size: 400% 100%;
  animation: sk-shimmer 1.4s ease infinite;
  border-radius: 6px;
}
.sk-title { width: 38%; height: 14px; margin-bottom: 14px; }
.sk-chart { height: 216px; }
@keyframes sk-shimmer {
  0% { background-position: 100% 0; }
  100% { background-position: 0 0; }
}
.charts {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(460px, 1fr));
  gap: 12px;
}
.chart-card {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: var(--el-bg-color);
  padding: 4px;
}
.empty-metric {
  height: 240px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
/* 采集状态行复用 .sub 字号和颜色，仅增加左侧间距，与触发按钮分开 */
.collect-status {
  margin-left: 8px;
}
.vmware-warn {
  margin-bottom: 12px;
}
.vmware-warn-body {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.vmware-account {
  margin-left: 2px;
}
.vmware-stats-level {
  margin-left: 12px;
  font-size: 12px;
}
.vmware-stats-level.is-bad {
  color: var(--el-color-danger);
  font-weight: 600;
}
.vmware-errors ul {
  margin: 2px 0 0 18px;
  padding: 0;
  font-size: 12px;
  color: var(--el-color-danger);
}
.vmware-help {
  font-size: 13px;
  line-height: 1.75;
}
.vmware-help-intro {
  margin: 0 0 8px;
}
.vmware-help ol {
  margin: 0 0 12px;
  padding-left: 22px;
}
.vmware-help li {
  margin-bottom: 8px;
}
.anomaly-card {
  margin-bottom: 12px;
}
.anomaly-head {
  display: flex;
  align-items: center;
  gap: 12px;
}
.anomaly-summary {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.anomaly-count {
  color: var(--el-color-danger);
  font-size: 15px;
}
</style>
