<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowDown, QuestionFilled, Setting } from '@element-plus/icons-vue'

import {
  assetExportSchema, cloudOverview, assetAction, getAsset, listAssets, setAssetTier,
  setAssetsTierBatch,
} from '@/api/assets'
import { listCloudAccounts, syncCloudAssets } from '@/api/clouds'
import { exportXlsx } from '@/api/download'
import { instanceMetrics } from '@/api/monitor'
import CopyBtn from '@/components/CopyBtn.vue'
import { useAuthStore } from '@/stores/auth'
import { useRoute, useRouter } from 'vue-router'
import { t } from '@/i18n'
import { assetStatusLabel } from '@/utils/format'

const route = useRoute()
const router = useRouter()
const product = ref('ecs')
// 多云深链：product + vendor + account_id 三参数直达账号资产
const accountId = ref(Number(route.query.account_id) || 0)
const vendorFilter = ref(String(route.query.vendor || ''))

/* 产品页签动态化：按已接入账号的实际资产出页签（未接入的产品不显示）；
   接口失败时退回静态全集，保证老环境可用。 */
const TAB_DEFS = {
  ecs: 'ECS', rds: 'RDS', redis: 'Redis', slb: 'SLB', oss: 'OSS', cdn: 'CDN',
  waf: 'WAF', ack: 'ACK', es: 'ES',
  domain: () => t('ops.tabs.domain'), ssl_cert: () => t('ops.tabs.ssl'),
  cvm: 'CVM 云主机', cdb: 'CDB 数据库', crs: 'Redis 缓存', clb: 'CLB 负载均衡', cos: 'COS 对象存储',
  tc_cdn: '腾讯 CDN', tc_mongodb: '腾讯 MongoDB',
  tc_es: '腾讯 Elasticsearch', tc_postgres: '腾讯 PostgreSQL',
  vmware_vm: () => t('ops.tabs.vmware_vm'), vmware_host: () => t('ops.tabs.vmware_host'),
}
const tabLabel = (name) => {
  const d = TAB_DEFS[name]
  return typeof d === 'function' ? d() : (d || name)
}
const FALLBACK_TABS = ['ecs', 'rds', 'redis', 'slb', 'oss', 'cdn', 'waf', 'ack', 'es',
  'domain', 'ssl_cert', 'cvm', 'cdb', 'crs', 'clb', 'cos',
  'tc_cdn', 'tc_mongodb', 'tc_es', 'tc_postgres']
const tabs = ref(FALLBACK_TABS.map((name) => ({ name, label: tabLabel(name) })))
/* Agent 上报的物理机不在云资产页签里出现，机器请去「机器管理」。 */
const HIDDEN_TABS = new Set(['host'])
const isVmware = computed(() => ['vmware_vm', 'vmware_host'].includes(product.value))

// 深链支持：/ops?product=rds 直达对应产品页签
// 只接受已知且可见的产品码，避免无效深链被误读成无资产
if (route.query.product && Object.prototype.hasOwnProperty.call(TAB_DEFS, String(route.query.product))
    && !HIDDEN_TABS.has(String(route.query.product))) {
  product.value = String(route.query.product)
}

async function loadTabs() {
  try {
    const r = await cloudOverview()
    const present = []
    const labels = {}
    const push = (p) => {
      if (!p?.product || HIDDEN_TABS.has(p.product)) return
      if (p.label) labels[p.product] = p.label
      if (!present.includes(p.product)) present.push(p.product)
    }
    ;(r.accounts || []).forEach((a) => (a.products || []).forEach(push))
    ;(r.unassigned?.products || []).forEach(push)
    if (!present.length) return
    const order = Object.keys(TAB_DEFS)
    present.sort((a, b) => {
      const ia = order.indexOf(a) < 0 ? 99 : order.indexOf(a)
      const ib = order.indexOf(b) < 0 ? 99 : order.indexOf(b)
      return ia - ib
    })
    let list = present.map((name) => ({ name, label: labels[name] || tabLabel(name) }))
    // 深链产品不在当前资产集时补一个页签（直链不应落空）；非深链不补，
    // 避免"只接入了 vCenter 却冒出一个空 ECS 页签"
    if (route.query.product && product.value && !HIDDEN_TABS.has(product.value)
        && !list.some((x) => x.name === product.value)) {
      list = [{ name: product.value, label: tabLabel(product.value) }, ...list]
    }
    tabs.value = list
    if (!list.some((x) => x.name === product.value) && list.length) {
      product.value = list[0].name
    }
  } catch { /* 接口异常保留静态全集 */ }
}
const loading = ref(false)
/* 各产品状态选项与标签色 */
const STATUS_OPTIONS = {
  ecs: ['Running', 'Stopped', 'Deleted'],
  rds: ['Running', 'Deleting', 'Deleted'],
  redis: ['Normal', 'Locked', 'Deleting', 'Deleted'],
  slb: ['active', 'locked', 'Deleted'],
  oss: ['Active', 'Deleted'],
  cdn: ['online', 'offline', 'configuring', 'Deleted'],
  waf: ['Normal', 'Stopped', 'Locked', 'Deleted'],
  ack: ['running', 'deleting', 'Deleted'],
  es: ['active', 'inactive', 'Deleted'],
  domain: ['ok', 'Expired', 'Deleted'],
  ssl_cert: ['ISSUED', 'EXPIRED', 'Deleted'],
  // 多云产品页签：腾讯云状态值与采集映射一致
  cvm: ['RUNNING', 'STOPPED', 'PENDING', 'REBOOTING', 'Deleted'],
  cdb: ['creating', 'running', 'isolated', 'recycled', 'recycling', 'offline', 'Deleted'],
  crs: ['initializing', 'processing', 'running', 'isolated', 'recycled', 'Deleted'],
  clb: ['creating', 'running', 'Deleted'],
  cos: ['Active', 'Deleted'],
  tc_cdn: ['online', 'offline', 'processing', 'closing', 'rejected', 'Deleted'],
  tc_mongodb: ['initializing', 'processing', 'running', 'isolated', 'Deleted'],
  tc_es: ['processing', 'running', 'stopped', 'destroying', 'destroyed', 'isolated', 'initializing', 'Deleted'],
  tc_postgres: [
    'applying', 'init', 'initing', 'running', 'limited run', 'isolating', 'isolated',
    'disisolating', 'recycling', 'recycled', 'job running', 'offline', 'migrating',
    'expanding', 'waitSwitch', 'switching', 'readonly', 'restarting', 'network changing',
    'upgrading', 'audit-switching', 'primary-switching', 'offlining', 'deployment changing',
    'cloning', 'parameter modifying', 'log-switching', 'restoring', 'Deleted',
  ],
  // vSphere 采集状态与后端归一值对齐：VM poweredOn/Off/suspended，
  // 主机 connectionState -> running/disconnected/not_responding）
  vmware_vm: ['running', 'stopped', 'suspended'],
  vmware_host: ['running', 'disconnected', 'not_responding'],
}
const syncing = ref(false)
const items = ref([])
const total = ref(0)
const query = reactive({ keyword: '', status: '', region: '', page: 1, page_size: 50 })
const regions = ref([])
const loadError = ref('')
const exporting = ref(false)

/* ---------------- 资产台账导出 ----------------
   不做"点一下直接下 CSV"：先在内嵌弹窗里选资产类型和每类的具体列；
   后端按产品拆 sheet，产品列不导出；ECS 磁盘/运行状态/系统/VPC/交换机均可勾选。 */
const exportDialogVisible = ref(false)
const exportCatalogLoading = ref(false)
const exportCatalog = ref([])
const exportMaxRows = ref(20000)
const exportSelectedProducts = ref([])
const exportSelectedColumns = reactive({})
const exportProducts = computed(() => {
  const byProduct = Object.fromEntries(exportCatalog.value.map((p) => [p.product, p]))
  const order = tabs.value.map((x) => x.name)
  const present = order.filter((p) => byProduct[p]).map((p) => byProduct[p])
  if (product.value && byProduct[product.value]
      && !present.some((p) => p.product === product.value)) {
    present.unshift(byProduct[product.value])
  }
  return present.length ? present : exportCatalog.value
})

function ensureExportColumns(p) {
  if (!Array.isArray(exportSelectedColumns[p.product])) {
    exportSelectedColumns[p.product] = (p.columns || []).map((c) => c.key)
  }
}

async function openExportDialog() {
  exportDialogVisible.value = true
  exportCatalogLoading.value = true
  try {
    const r = await assetExportSchema()
    exportCatalog.value = r.products || []
    exportMaxRows.value = Number(r.max_rows || 20000)
    const available = exportProducts.value
    for (const p of available) ensureExportColumns(p)
    const current = available.find((p) => p.product === product.value)
    exportSelectedProducts.value = current ? [current.product] : (available[0] ? [available[0].product] : [])
  } catch {
    exportCatalog.value = []
  } finally {
    exportCatalogLoading.value = false
  }
}

function toggleExportProduct(p, checked) {
  ensureExportColumns(p)
  const list = exportSelectedProducts.value.filter((x) => x !== p.product)
  if (checked) list.push(p.product)
  exportSelectedProducts.value = list
}

function setAllExportColumns(p, all) {
  exportSelectedColumns[p.product] = all ? (p.columns || []).map((c) => c.key) : []
}

async function submitExport() {
  if (!exportSelectedProducts.value.length) {
    ElMessage.warning(t('ops.export.need_product'))
    return
  }
  const columns = {}
  for (const p of exportProducts.value) {
    if (!exportSelectedProducts.value.includes(p.product)) continue
    const keys = exportSelectedColumns[p.product] || []
    if (!keys.length) {
      ElMessage.warning(t('ops.export.need_column', { product: p.label }))
      return
    }
    columns[p.product] = keys
  }
  exporting.value = true
  try {
    const ok = await exportXlsx('/assets/export/xlsx', {
      products: exportSelectedProducts.value,
      columns,
      account_id: accountId.value || 0,
      // 厂商/地域/状态是当前页签的筛选；导出其他资产类型时不把当前页签条件强加过去
      vendor: isVmware.value ? '' : (vendorFilter.value || ''),
      keyword: query.keyword || '',
      region: isVmware.value ? '' : (query.region || ''),
      status: query.status || '',
      status_product: product.value,
    }, 'assets.xlsx')
    if (ok) exportDialogVisible.value = false
  } finally {
    exporting.value = false
  }
}

/* ---------------- 列展示设置（按产品页签长期保存） ----------------
   localStorage 不随登录会话过期；新版本新增列会自动追加，旧版本删掉的列忽略。 */
const COLUMN_STORAGE_PREFIX = 'qianshu.ops.columns.v1.'
const columnDialogVisible = ref(false)
const columnDraft = ref([])
const columnLayout = ref([])

const tableColumnCatalog = computed(() => {
  const publicIp = ['ecs', 'cvm'].includes(product.value)
  const hasOs = isVmware.value || ['ecs', 'cvm'].includes(product.value)
  return [
    { key: 'name', labelKey: 'ops.col.name', minWidth: 130 },
    { key: 'instance_id', labelKey: 'ops.col.instance_id', width: 215,
      showWhen: !isVmware.value, computedLabel: instanceIdLabel.value },
    { key: 'vendor_account', labelKey: isVmware.value ? 'ops.col.account' : 'ops.col.vendor_account',
      minWidth: 150 },
    { key: 'os_name', labelKey: 'ops.col.os_version', minWidth: 180, showWhen: hasOs },
    { key: 'ip', labelKey: isVmware.value ? 'ops.col.ip' : 'ops.col.private_ip',
      width: 135, showWhen: publicIp || isVmware.value },
    { key: 'public_ip', labelKey: 'ops.col.public_ip', width: 120, showWhen: publicIp },
    { key: 'status', labelKey: 'ops.col.status', width: 90 },
    { key: 'tier', labelKey: 'ops.col.tier', width: 130 },
    { key: 'spec', labelKey: 'ops.col.spec', minWidth: 140 },
    { key: 'disk', labelKey: product.value === 'vmware_host' ? 'ops.col.storage' : 'ops.col.disk',
      width: 110, showWhen: product.value === 'ecs' || isVmware.value },
    { key: 'region', labelKey: 'ops.col.region', width: 110,
      showWhen: !isVmware.value && regions.value.length > 0 },
    { key: 'expire', labelKey: 'ops.col.expire', width: 130, showWhen: !isVmware.value },
  ].filter((c) => c.showWhen !== false)
})

function columnTitle(c) {
  return c.computedLabel || t(c.labelKey)
}

function columnProps(c) {
  const p = c.width ? { width: c.width } : { minWidth: c.minWidth || 120 }
  if (['name', 'instance_id', 'os_name', 'vendor_account'].includes(c.key)) {
    p.showOverflowTooltip = true
  }
  return p
}

const effectiveColumnLayout = computed(() => {
  const catalog = tableColumnCatalog.value
  const byKey = Object.fromEntries(catalog.map((c) => [c.key, c]))
  const out = []
  const seen = new Set()
  for (const item of columnLayout.value) {
    const c = byKey[item.key]
    if (c && !seen.has(c.key)) {
      out.push({ ...c, visible: item.visible !== false })
      seen.add(c.key)
    }
  }
  for (const c of catalog) {
    if (!seen.has(c.key)) out.push({ ...c, visible: true })
  }
  return out
})
const visibleColumns = computed(() => effectiveColumnLayout.value.filter((c) => c.visible))

function columnStorageKey(p = product.value) {
  return COLUMN_STORAGE_PREFIX + p
}

function loadColumnLayout() {
  let saved = []
  try {
    saved = JSON.parse(localStorage.getItem(columnStorageKey()) || '[]')
  } catch { saved = [] }
  columnLayout.value = Array.isArray(saved)
    ? saved.filter((x) => x && typeof x.key === 'string')
      .map((x) => ({ key: x.key, visible: x.visible !== false }))
    : []
}

function openColumnDialog() {
  columnDraft.value = effectiveColumnLayout.value.map((c) => ({ ...c }))
  columnDialogVisible.value = true
}

function moveColumn(index, delta) {
  const next = index + delta
  if (next < 0 || next >= columnDraft.value.length) return
  const arr = columnDraft.value
  ;[arr[index], arr[next]] = [arr[next], arr[index]]
}

function resetColumns() {
  columnDraft.value = tableColumnCatalog.value.map((c) => ({ ...c, visible: true }))
}

function saveColumns() {
  if (!columnDraft.value.some((c) => c.visible)) {
    ElMessage.warning(t('ops.columns.need_one'))
    return
  }
  const payload = columnDraft.value.map((c) => ({ key: c.key, visible: !!c.visible }))
  localStorage.setItem(columnStorageKey(), JSON.stringify(payload))
  columnLayout.value = payload
  columnDialogVisible.value = false
  ElMessage.success(t('ops.columns.saved'))
}

const detailVisible = ref(false)
const detail = ref(null)
const detailLoading = ref(false)

async function load() {
  const currentProduct = product.value
  // 勾选只对当前页当前筛选有效：重新查询前清空，避免把旧页选择带到新条件
  selectedRows.value = []
  tableRef.value?.clearSelection()
  loading.value = true
  loadError.value = ''
  try {
    const data = await listAssets({
      product: currentProduct, account_id: accountId.value || 0,
      vendor: vendorFilter.value || '', ...query,
    })
    if (currentProduct !== product.value) return // 已切走，丢弃过期结果
    items.value = data.items
    total.value = data.total
    // 地域选项由后端按全量结果返回（此前只取当前页 50 条，跨地域会漏）
    regions.value = data.regions || []
  } catch (e) {
    if (currentProduct === product.value) {
      items.value = []
      total.value = 0
      loadError.value = e?.response?.data?.detail || t('ops.table.load_fail')
    }
  } finally {
    if (currentProduct === product.value) loading.value = false
  }
}

/* 切换产品页签：重置筛选条件并重新加载（各产品的状态值/地域/厂商不同，不能沿用） */
watch(product, () => {
  query.keyword = ''
  query.status = ''
  query.region = ''
  query.page = 1
  items.value = []
  regions.value = []
  detailVisible.value = false
  // VMware 没有云厂商维度：切过去时清掉上一个页签残留的厂商筛选。
  // 这里清空触发的 vendorFilter watch 会因 isVmware 为真直接返回，不会重复请求。
  if (isVmware.value) vendorFilter.value = ''
  loadColumnLayout()
  load()
})

/* 厂商筛选（服务端参数）：切换即回第一页重查；VMware 不适用 */
watch(vendorFilter, (val, old) => {
  if (isVmware.value || val === old) return
  query.page = 1
  load()
})

/* 多云：批量同步走 /clouds/sync，按账号走 Provider 层 */
async function onSync() {
  syncing.value = true
  try {
    const r = await syncCloudAssets({ product: product.value })
    const s = r.summary || {}
    ElMessage.success(
      t('ops.sync.ok', { total: s.total || 0, created: s.created || 0, updated: s.updated || 0 }) +
      (s.failed_regions?.length ? t('ops.sync.failed_regions', { n: s.failed_regions.length }) : '') +
      (s.errors?.length ? t('ops.sync.errors', { n: s.errors.length }) : ''),
    )
    load()
  } finally {
    syncing.value = false
  }
}

/* ---------------- 云厂商/账号列 / 厂商筛选 ----------------
   列表/详情行带 vendor/account_name（后端 assets 输出已映射 cloud_accounts），
   缺失时回退账号表映射（旧采集数据过渡口径：默认归属阿里云）。
   厂商筛选走服务端参数。VMware 没有"云厂商"维度，不显示筛选；
   普通云产品只列当前真实已接入账号的厂商，避免给用户一堆选中就空列表的选项。 */
const VENDOR_OPTIONS = [
  { value: 'aliyun', label: 'ops.vendor.aliyun' },
  { value: 'tencent', label: 'ops.vendor.tencent' },
  { value: 'volc', label: 'ops.vendor.volc' },
  { value: 'baidu', label: 'ops.vendor.baidu' },
  { value: 'huawei', label: 'ops.vendor.huawei' },
  { value: 'aws', label: 'ops.vendor.aws' },
  { value: 'azure', label: 'ops.vendor.azure' },
  // 非公有云：ESXi/vCenter 单列便于按形态筛选
  { value: 'esxi', label: 'ops.vendor.esxi' },
  { value: 'vcenter', label: 'ops.vendor.vcenter' },
  // onprem 只保留历史资产的标签映射；不再作为筛选/建账号入口（裸机归机器管理）
  { value: 'onprem', label: 'ops.vendor.onprem' },
]
const FILTERABLE_VENDORS = VENDOR_OPTIONS.filter((v) => v.value !== 'onprem')
const accounts = ref([])
const accountMap = computed(() => Object.fromEntries(accounts.value.map((a) => [a.id, a])))
const vendorOptions = computed(() => {
  if (isVmware.value) return []
  const present = new Set(accounts.value.map((a) => a.vendor).filter(Boolean))
  if (!present.size) return FILTERABLE_VENDORS
  const list = FILTERABLE_VENDORS.filter((v) => present.has(v.value))
  // 深链/旧筛选值不在账号表中时仍展示当前值，避免筛选框显示空值
  if (vendorFilter.value && !list.some((v) => v.value === vendorFilter.value)) {
    const cur = VENDOR_OPTIONS.find((v) => v.value === vendorFilter.value)
    if (cur) list.push(cur)
  }
  return list
})

function accountLabel(id) {
  return accountMap.value[id]?.name || t('ops.account.default')
}
function rowVendor(row) {
  // 兜底不能发明云厂商（同类断层）：VMware 是虚拟化平台；域名/证书可能是人工
  // 登记的外部资源（无账号），都不能被显示成「阿里云」。
  const manual = ['domain', 'ssl_cert'].includes(product.value)
  const fallback = isVmware.value
    ? (product.value === 'vmware_host' ? 'esxi' : 'vcenter')
    : (manual ? '' : 'aliyun')
  return row.vendor || accountMap.value[row.account_id]?.vendor || fallback
}
// 总览资产卡深链进来的账号筛选（可一键清除回到全部账号）
function clearAccountFilter() {
  accountId.value = 0
  query.page = 1
  load()
}
function vendorAccountLabel(row) {
  const code = rowVendor(row)
  const vendor = t(row.vendor_label
    || VENDOR_OPTIONS.find((v) => v.value === code)?.label
    || code)
  const account = row.account_name || accountLabel(row.account_id)
  return (account && account !== t('ops.account.default') ? `${vendor}·${account}` : vendor) || '—'
}
/* 厂商筛选由服务端执行，保证跨页统计口径一致 */
const filteredItems = computed(() => items.value)

async function loadAccounts() {
  try {
    const r = await listCloudAccounts()
    accounts.value = r.items || []
  } catch { /* 拦截器提示 */ }
}
// 当前筛选到单账号时，按其厂商能力位决定同步按钮是否可用：
// 仅登记/仅探活厂商没有资产采集，点同步只会确定性地空跑。
const syncBlocked = computed(() => {
  if (!accountId.value) return false
  const a = accountMap.value[accountId.value]
  return !!a?.capability && !a.capability.sync
})

async function openDetail(row) {
  detailVisible.value = true
  detailLoading.value = true
  detail.value = null
  detailMetrics.value = []
  detailMetricsError.value = ''
  try {
    detail.value = await getAsset(row.id)
  } finally {
    detailLoading.value = false
  }
  loadDetailMetrics()
}

/* VMware 详情监控状态：不再跳去 K8s 机器管理查 SSH 凭证，
   直接在详情里看最近 1 小时的 CPU/内存最新值与样本量，并给"完整监控"入口。 */
const detailMetrics = ref([])
const detailMetricsLoading = ref(false)
const detailMetricsError = ref('')
const latestMetricValue = (m) => {
  let latest = null
  let samples = 0
  for (const s of m.series || []) {
    for (const p of s.points || []) {
      if (!Array.isArray(p) || p[1] == null) continue
      samples += 1
      if (!latest || p[0] > latest[0]) latest = p
    }
  }
  return { value: latest ? latest[1] : null, at: latest ? latest[0] : null, samples }
}
const hasMetricSamples = computed(() => detailMetrics.value.some((m) => m.samples > 0))

async function loadDetailMetrics() {
  if (!isVmware.value || !detail.value) return
  detailMetricsLoading.value = true
  detailMetricsError.value = ''
  try {
    const r = await instanceMetrics(product.value, detail.value.instance_id, '1h')
    detailMetrics.value = (r.metrics || []).map((m) => ({
      label: m.label, unit: m.unit || '', ...latestMetricValue(m),
    }))
  } catch (e) {
    detailMetricsError.value = e?.response?.data?.detail || t('ops.detail.monitor_fail')
  } finally {
    detailMetricsLoading.value = false
  }
}

function openMonitor() {
  if (!detail.value) return
  router.push({ path: '/monitor', query: { product: product.value, instance: detail.value.instance_id } })
}

/* 非 ECS 产品详情抽屉的字段映射（label 为 i18n 键，渲染时 t() 取值） */
const EXTRA_LABELS = {
  rds: [
    ['engine', 'ops.field.engine'], ['engine_version', 'ops.field.version'], ['instance_role', 'ops.field.role'],
    ['memory_mb', 'ops.field.memory_mb'], ['storage_type', 'ops.field.storage_class'], ['connection_string', 'ops.field.conn_string'],
    ['net_type', 'ops.field.network_type'], ['lock_mode', 'ops.field.lock_mode'],
  ],
  redis: [
    ['engine_version', 'ops.field.version'], ['architecture', 'ops.field.architecture'], ['edition', 'ops.field.edition'],
    ['node_type', 'ops.field.node_type'], ['capacity_mb', 'ops.field.capacity_mb'], ['max_connections', 'ops.field.max_connections'],
    ['max_qps', 'ops.field.max_qps'], ['bandwidth_mb', 'ops.field.bandwidth_mbps'], ['connection_domain', 'ops.field.conn_string'],
    ['port', 'ops.field.port'], ['private_ip', 'ops.field.private_ip'], ['network_type', 'ops.field.network_type'],
  ],
  slb: [
    ['address', 'ops.field.address'], ['address_type', 'ops.field.address_type'], ['network_type', 'ops.field.network_type'],
    ['bandwidth', 'ops.field.bandwidth_mbps'], ['spec', 'ops.field.spec'], ['instance_charge_type', 'ops.field.charge_type'],
    ['internet_charge_type', 'ops.field.internet_charge'], ['master_zone', 'ops.field.primary_zone'], ['slave_zone', 'ops.field.secondary_zone'],
    ['delete_protection', 'ops.field.delete_protection'],
  ],
  oss: [
    ['storage_class', 'ops.field.storage_class'], ['extranet_endpoint', 'ops.field.extranet_endpoint'],
    ['intranet_endpoint', 'ops.field.intranet_endpoint'], ['creation_date', 'ops.field.created_at'],
  ],
  cdn: [
    ['cname', 'ops.field.cname'], ['cdn_type', 'ops.field.cdn_type'], ['coverage', 'ops.field.coverage'],
    ['ssl_protocol', 'ops.field.https'], ['gmt_created', 'ops.field.created_at'],
  ],
  waf: [
    ['waf_version', 'ops.field.waf_version'], ['edition', 'ops.field.instance_edition'], ['pay_type', 'ops.field.pay_type'],
    ['domain_count', 'ops.field.domain_count'], ['start_time', 'ops.field.opened_at'],
  ],
  ack: [
    ['cluster_type', 'ops.field.cluster_type'], ['current_version', 'ops.field.current_version'], ['init_version', 'ops.field.init_version'],
    ['node_count', 'ops.field.node_count'], ['network_mode', 'ops.field.network_mode'], ['external_loadbalancer_id', 'ops.field.linked_slb'],
    ['vpc_id', 'ops.field.vpc'], ['deletion_protection', 'ops.field.delete_protection'], ['timezone', 'ops.field.timezone'],
  ],
  es: [
    ['es_version', 'ops.field.version'], ['node_amount', 'ops.field.node_count'], ['node_spec', 'ops.field.node_spec'],
    ['spec_info', 'ops.field.spec'], ['disk_gb', 'ops.field.disk_gb'], ['disk_type', 'ops.field.disk_type'],
    ['port', 'ops.field.port'], ['payment_type', 'ops.field.pay_type'],
  ],
  // 多云产品：腾讯云
  cvm: [
    ['cpu', 'ops.field.cpu_core'], ['memory_gb', 'ops.field.memory_g'], ['ip_internal', 'ops.field.private_ip'], ['ip_public', 'ops.field.public_ip'],
    ['os_name', 'ops.field.os'], ['image_id', 'ops.field.image'], ['vpc_id', 'ops.field.vpc'], ['subnet_id', 'ops.field.subnet'],
    ['system_disk_gb', 'ops.field.system_disk_g'],
  ],
  cdb: [
    ['engine', 'ops.field.engine'], ['engine_version', 'ops.field.version'], ['memory_mb', 'ops.field.memory_mb'],
    ['storage_capacity_gb', 'ops.field.capacity_gb'], ['cpu', 'ops.field.cpu_core'], ['qps', 'ops.field.qps'],
    ['connection_string', 'ops.field.private_address'], ['port', 'ops.field.port'], ['wan_domain', 'ops.field.wan_domain'],
    ['wan_port', 'ops.field.wan_port'], ['vpc_id', 'ops.field.vpc'], ['subnet_id', 'ops.field.subnet'],
  ],
  crs: [
    ['engine_version', 'ops.field.version'], ['node_type', 'ops.field.architecture'], ['capacity_mb', 'ops.field.capacity_mb'],
    ['max_connections', 'ops.field.max_connections'], ['port', 'ops.field.port'], ['private_ip', 'ops.field.private_ip'],
    ['connection_domain', 'ops.field.conn_address'], ['vpc_id', 'ops.field.vpc'], ['shard_num', 'ops.field.shard_num'],
    ['replicas_num', 'ops.field.replicas_num'],
  ],
  clb: [
    ['address', 'ops.field.vip'], ['address_type', 'ops.field.address_type'], ['network_type', 'ops.field.ip_version'],
    ['forward', 'ops.field.forward_type'], ['domain', 'ops.field.domain'], ['vpc_id', 'ops.field.vpc'], ['master_zone', 'ops.field.primary_zone'],
    ['create_time', 'ops.field.created_at'],
  ],
  cos: [
    ['storage_class', 'ops.field.storage_class'], ['bucket_region', 'ops.field.bucket_region'], ['creation_date', 'ops.field.created_at'],
  ],
  tc_cdn: [
    ['cname', 'ops.field.cname'], ['service_type', 'ops.field.cdn_type'], ['area', 'ops.field.coverage'],
    ['origin_type', 'ops.field.origin_type'], ['disable', 'ops.field.disable'],
    ['read_only', 'ops.field.read_only'], ['create_time', 'ops.field.created_at'], ['update_time', 'ops.field.updated_at'],
  ],
  tc_mongodb: [
    ['engine', 'ops.field.engine'], ['engine_version', 'ops.field.version'], ['cluster_type', 'ops.field.cluster_type'],
    ['memory_mb', 'ops.field.memory_mb'], ['storage_mb', 'ops.field.storage_mb'], ['cpu', 'ops.field.cpu_core'],
    ['machine_type', 'ops.field.spec'], ['secondary_num', 'ops.field.secondary_num'],
    ['replication_set_num', 'ops.field.replication_set_num'], ['connection_string', 'ops.field.private_address'],
    ['port', 'ops.field.port'], ['net_type', 'ops.field.network_type'], ['vpc_id', 'ops.field.vpc'],
    ['subnet_id', 'ops.field.subnet'], ['create_time', 'ops.field.created_at'],
  ],
  tc_es: [
    ['engine', 'ops.field.engine'], ['engine_version', 'ops.field.version'], ['node_num', 'ops.field.node_count'],
    ['cpu', 'ops.field.cpu_core'], ['memory_gb', 'ops.field.memory_g'], ['disk_gb', 'ops.field.disk_gb'],
    ['disk_type', 'ops.field.disk_type'], ['es_domain', 'ops.field.es_domain'], ['es_vip', 'ops.field.private_ip'],
    ['es_port', 'ops.field.port'], ['license_type', 'ops.field.license_type'], ['create_time', 'ops.field.created_at'],
  ],
  tc_postgres: [
    ['engine', 'ops.field.engine'], ['engine_version', 'ops.field.version'], ['memory_gb', 'ops.field.memory_g'],
    ['storage_gb', 'ops.field.capacity_gb'], ['cpu', 'ops.field.cpu_core'], ['connection_string', 'ops.field.private_address'],
    ['port', 'ops.field.port'], ['vpc_id', 'ops.field.vpc'], ['subnet_id', 'ops.field.subnet'],
    ['master_instance_id', 'ops.field.master_instance_id'], ['create_time', 'ops.field.created_at'],
  ],
  // VMware 资产：只展示运维真正用得上的字段，不展示 MOR/实例ID/假地域
  vmware_vm: [
    ['power_state', 'ops.field.power_state'],
    ['tools_status', 'ops.detail.tools_status'],
  ],
  vmware_host: [
    ['datastore_count', 'ops.field.datastore_count'],
    ['datastore_total_gb', 'ops.field.datastore_total'],
    ['datastore_free_gb', 'ops.field.datastore_free'],
    ['model', 'ops.field.hardware_model'],
    ['cpu_model', 'ops.field.cpu_model'],
  ],
}
const EXTRA_FORMATTERS = {
  rds: { instance_role: (v) => ({ Primary: 'ops.enum.primary', ReadOnly: 'ops.enum.readonly', Serverless: 'ops.enum.serverless' }[v] || v) },
  redis: { node_type: (v) => ({ double: 'ops.enum.dual', single: 'ops.enum.single', cluster: 'ops.enum.cluster' }[v] || v) },
  slb: {
    address_type: (v) => ({ internet: 'ops.enum.internet', intranet: 'ops.enum.intranet' }[v] || v),
    delete_protection: (v) => ({ on: 'ops.enum.on', off: 'ops.enum.off_closed' }[v] || v),
  },
  cdn: { ssl_protocol: (v) => ({ on: 'ops.enum.on', off: 'ops.enum.off_not' }[v] || v) },
  waf: { pay_type: (v) => ({ POSTPAY: 'ops.enum.postpaid', PREPAY: 'ops.enum.prepaid' }[v] || v) },
  ack: {
    cluster_type: (v) => ({ ManagedKubernetes: 'ops.enum.managed', DedicatedKubernetes: 'ops.enum.dedicated', ServerlessKubernetes: 'ops.enum.serverless' }[v] || v),
    deletion_protection: (v) => (v === true || v === 'true' ? 'ops.enum.on' : 'ops.enum.off_not'),
  },
  es: { payment_type: (v) => ({ prepaid: 'ops.enum.prepaid', postpaid: 'ops.enum.postpaid' }[v] || v) },
  clb: { address_type: (v) => ({ internet: 'ops.enum.internet', intranet: 'ops.enum.intranet' }[v] || v) },
  tc_cdn: { read_only: (v) => (v === true || v === 'true' ? 'ops.enum.yes' : 'ops.enum.no') },
  tc_mongodb: {
    cluster_type: (v) => ({ replica_set: 'ops.enum.replica_set', sharded: 'ops.enum.sharded' }[v] || v),
    net_type: (v) => ({ basic: 'ops.enum.basic_network', vpc: 'ops.enum.vpc_network' }[v] || v),
  },
  vmware_vm: {
    tools_status: (v) => (v === 'guestToolsRunning'
      ? 'common.asset_status.running' : 'ops.ip.tools_down'),
    power_state: (v) => ({ poweredOn: 'common.asset_status.running',
                           poweredOff: 'common.asset_status.stopped',
                           suspended: 'common.asset_status.suspended' }[v] || v),
  },
}

const auth = useAuthStore()
const canWrite = computed(() => ['admin', 'operator'].includes(auth.role))

async function changeTier(row, tier) {
  try {
    await setAssetTier(row.instance_id, tier)
    row.tier = tier
    ElMessage.success(t('ops.tier.msg', {
      name: row.name || row.instance_id,
      tier: tier === 'aux' ? t('ops.tier.msg_aux') : t('ops.tier.msg_core'),
    }))
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('ops.tier.fail'))
  }
}

/* 批量关键度：
   有勾选=只改勾选行；无勾选=按当前筛选条件整批（服务端按 product + 账号/厂商/
   关键词/状态/地域匹配，未选状态时不动 Deleted 历史行）。 */
const tableRef = ref(null)
const selectedRows = ref([])
const batchBusy = ref(false)
const onSelectionChange = (rows) => { selectedRows.value = rows }

async function batchTier(tier) {
  const ids = selectedRows.value.map((r) => r.instance_id)
  if (!ids.length && !total.value) {
    ElMessage.warning(t('ops.tier.batch_none'))
    return
  }
  const scope = ids.length
    ? t('ops.tier.selected', { n: ids.length })
    : t('ops.tier.filtered', { n: total.value })
  const tierText = tier === 'aux' ? t('ops.tier.aux') : t('ops.tier.core')
  try {
    await ElMessageBox.confirm(
      t('ops.tier.batch_prompt', { scope, product: tabLabel(product.value), tier: tierText }),
      t('ops.tier.batch_title'), { type: 'warning', confirmButtonText: t('ops.tier.batch_confirm') },
    )
  } catch {
    return
  }
  batchBusy.value = true
  try {
    const r = await setAssetsTierBatch(ids.length
      ? { tier, instance_ids: ids, product: product.value }
      : {
        tier, product: product.value, account_id: accountId.value || 0,
        vendor: vendorFilter.value || '', keyword: query.keyword,
        status: query.status, region: query.region,
      })
    ElMessage.success(t('ops.tier.batch_ok', { n: r.updated ?? 0, m: r.matched ?? 0 }))
    tableRef.value?.clearSelection()
    selectedRows.value = []
    load()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('ops.tier.fail'))
  } finally {
    batchBusy.value = false
  }
}

function chargeLabel(code) {
  const key = ({
    PrePaid: 'ops.enum.prepaid', PostPaid: 'ops.enum.postpaid', Prepaid: 'ops.enum.prepaid',
    prepaid: 'ops.enum.prepaid', PayOnDemand: 'ops.enum.postpaid', PayByCLCU: 'ops.enum.pay_by_lcu',
    POSTPAY: 'ops.enum.postpaid', PayAsYouGo: 'ops.enum.postpaid',
  })[code]
  return key ? t(key) : (code || '-')
}

/* 实例 ID 列名按产品说人话：SSL/域名的 ID 是订单号，不是"实例" */
const instanceIdLabel = computed(() => {
  if (product.value === 'ssl_cert') return t('ops.col.instance_id_ssl')
  if (product.value === 'domain') return t('ops.col.instance_id_domain')
  return t('ops.col.instance_id')
})

/* VMware 详情：IP / 系统版本 / 内存都来自采集 extra（键名与云主机不同） */
const detailIp = computed(() => {
  const e = detail.value?.extra || {}
  return e.ip_internal || e.guest_ip || e.mgmt_ip || ''
})
const detailOs = computed(() => {
  const e = detail.value?.extra || {}
  return e.os_name || e.guest_os || ''
})
const detailMemory = computed(() => {
  const e = detail.value?.extra || {}
  if (e.memory_gb != null) return e.memory_gb
  if (e.memory_mb) return Math.round(e.memory_mb / 1024)
  return null
})
const detailCpu = computed(() => {
  const e = detail.value?.extra || {}
  return e.cpu ?? e.cpu_count ?? null
})
const zoneLabel = computed(() => {
  if (product.value === 'vmware_vm') return t('ops.col.zone_vm')
  if (product.value === 'vmware_host') return t('ops.col.zone_host')
  return t('ops.detail.zone')
})

const extraRows = computed(() => {
  if (product.value === 'ecs' || !detail.value?.extra) return []
  const labels = EXTRA_LABELS[product.value] || []
  const fmts = EXTRA_FORMATTERS[product.value] || {}
  return labels
    .filter(([k]) => detail.value.extra[k] !== undefined && detail.value.extra[k] !== '')
    .map(([k, labelKey]) => {
      const v = detail.value.extra[k]
      const f = fmts[k]
      return { label: t(labelKey), value: f ? t(f(v)) : v }
    })
})

function statusTag(s) {
  return {
    Running: 'success', Stopped: 'info', Deleted: 'danger',
    Normal: 'success', active: 'success', Active: 'success', running: 'success',
    RUNNING: 'success',
    online: 'success', offline: 'danger', configuring: 'warning',
    locked: 'warning', Deleting: 'warning', Creating: 'warning', deleting: 'warning',
    inactive: 'info',
    // VMware：关机=灰，断连/无响应=红
    stopped: 'info', suspended: 'warning', disconnected: 'danger', not_responding: 'danger',
    // 多云产品（腾讯云清单同步）：创建/流程=黄，隔离/回收=灰红
    creating: 'warning', initializing: 'info', processing: 'warning',
    isolated: 'warning', recycled: 'info', recycling: 'warning',
    rejected: 'danger', closing: 'warning', destroying: 'warning', destroyed: 'danger',
    applying: 'info', init: 'info', initing: 'info', 'limited run': 'warning',
    isolating: 'warning', disisolating: 'warning', 'job running': 'warning',
    migrating: 'warning', expanding: 'warning', waitSwitch: 'warning', switching: 'warning',
    readonly: 'info', restarting: 'warning', 'network changing': 'warning', upgrading: 'warning',
    'audit-switching': 'warning', 'primary-switching': 'warning', offlining: 'warning',
    'deployment changing': 'warning', cloning: 'warning', 'parameter modifying': 'warning',
    'log-switching': 'warning', restoring: 'warning',
  }[s] || 'warning'
}

/* ---------------- 电源操作（危险操作：输入实例 ID 确认） ---------------- */
// 状态判断兼容 ECS 大写（Running/Stopped）与 VMware 小写（running/stopped/suspended）
function isRunningRow(row) {
  return String(row?.status || '').toLowerCase() === 'running'
}
function isStoppedRow(row) {
  return ['stopped', 'suspended'].includes(String(row?.status || '').toLowerCase())
}
// VMware 的关机/重启是「智能软关」：Tools 运行=优雅，未运行=强制断电（确认框必须红色警示）
function toolsForceMode(row, action) {
  return product.value === 'vmware_vm' && (action === 'stop' || action === 'reboot')
    && row.tools_status !== 'guestToolsRunning'
}
async function powerAction(row, action) {
  const labels = { reboot: 'ops.power.act_reboot', stop: 'ops.power.act_stop', start: 'ops.power.act_start' }
  const isDanger = action === 'reboot' || action === 'stop'
  const isVmwareRow = product.value === 'vmware_vm'
  const force = toolsForceMode(row, action)
  let toolsLine = ''
  if (isVmwareRow && isDanger) {
    if (force) {
      toolsLine = `<p style="color:var(--el-color-danger);font-weight:600">
        ${t(action === 'stop' ? 'ops.power.tools_force_stop' : 'ops.power.tools_force_reboot')}
      </p>`
    } else {
      toolsLine = `<p style="color:var(--el-color-success)">
        ${t(action === 'stop' ? 'ops.power.tools_ok_stop' : 'ops.power.tools_ok_reboot')}
      </p>`
    }
  }
  const html = `
    <p>${t('ops.power.intro_pre')}<b style="color:var(--el-color-danger)">${t(labels[action])}</b>${t('ops.power.intro_post')}</p>
    <p style="margin:8px 0">
      ${t('ops.power.field_name')}<b>${row.name}</b><br/>
      ${t('ops.power.field_id')}<code>${row.instance_id}</code><br/>
      ${t('ops.power.field_ip')}${row.ip_internal || '-'}${t('ops.power.field_status')}${row.status}
      ${isVmwareRow && isDanger ? `<br/>${t('ops.power.field_tools')}${row.tools_status || '-'}` : ''}
    </p>
    ${toolsLine}
    <p style="color:var(--el-color-danger)">
      ${isDanger ? t('ops.power.danger') : ''}
      ${t('ops.power.confirm_prompt')}
    </p>`
  let confirmValue
  try {
    const { value } = await ElMessageBox.prompt(html, t('ops.power.confirm_title', { action: t(labels[action]) }), {
      dangerouslyUseHTMLString: true,
      confirmButtonText: t('ops.power.confirm_btn', { action: t(labels[action]) }),
      cancelButtonText: t('ops.power.cancel'),
      inputPlaceholder: row.instance_id,
      inputValidator: (v) => v === row.instance_id || t('ops.power.mismatch'),
      type: isDanger ? 'warning' : 'info',
    })
    confirmValue = value
  } catch {
    return // 用户取消
  }
  const r = await assetAction(row.id, action, confirmValue)
  ElMessage.success(r.message)
  load()
}

onMounted(async () => {
  loadColumnLayout()
  // 先定页签（动态产品集），再按页签取资产；产品被自动切到第一个页签时由 watch 触发加载
  const before = product.value
  await loadTabs()
  if (product.value === before) load()
  loadAccounts()
})
</script>

<template>
  <div class="page">
    <!-- 产品页签按已接入账号的实际资产动态生成（未接入的不显示） -->
    <el-tabs v-model="product">
      <el-tab-pane v-for="tab in tabs" :key="tab.name" :label="tab.label" :name="tab.name" />
    </el-tabs>

    <div class="toolbar">
      <el-input v-model="query.keyword" :placeholder="t('ops.toolbar.keyword_ph')" clearable style="width: 220px"
                @keyup.enter="query.page = 1; load()" />
      <el-select v-model="query.status" :placeholder="t('ops.toolbar.status_ph')" clearable style="width: 130px" @change="query.page = 1; load()">
        <!-- 状态选项显示中文（ISSUED/EXPIRED/Running 等原值只做接口值，不给用户看） -->
        <el-option v-for="st in STATUS_OPTIONS[product]" :key="st" :label="assetStatusLabel(st)" :value="st" />
      </el-select>
      <!-- VMware 没有地域概念；即使历史数据残留 region 也不给筛选入口 -->
      <el-select v-if="!isVmware && regions.length" v-model="query.region" :placeholder="t('ops.toolbar.region_ph')" clearable style="width: 150px" @change="query.page = 1; load()">
        <el-option v-for="r in regions" :key="r" :label="r" :value="r" />
      </el-select>
      <!-- 云厂商筛选：VMware 没有厂商维度，不显示；普通云产品只列已接入账号的厂商 -->
      <el-select v-if="!isVmware && vendorOptions.length" v-model="vendorFilter"
                 :placeholder="t('ops.toolbar.vendor_ph')" clearable style="width: 210px">
        <el-option v-for="v in vendorOptions" :key="v.value" :label="t(v.label)" :value="v.value" />
      </el-select>
      <!-- 总览资产卡深链：按账号筛选（可关闭回全部账号） -->
      <el-tag v-if="accountId" closable size="large" effect="plain" @close="clearAccountFilter">
        {{ t('ops.toolbar.account_filter', { name: accountLabel(accountId) }) }}
      </el-tag>
      <!-- 批量关键度：未勾选=按当前筛选整批（VMware 近百台不用逐台点） -->
      <el-dropdown v-if="canWrite" trigger="click" @command="batchTier">
        <el-button :loading="batchBusy">
          {{ t('ops.tier.batch') }}<template v-if="selectedRows.length">{{ t('ops.tier.selected', { n: selectedRows.length }) }}</template>
          <el-icon style="margin-left:4px;font-size:12px"><ArrowDown /></el-icon>
        </el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="aux">{{ t('ops.tier.batch_aux') }}</el-dropdown-item>
            <el-dropdown-item command="core">{{ t('ops.tier.batch_core') }}</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
      <el-button @click="query.page = 1; load()">{{ t('ops.toolbar.search') }}</el-button>
      <el-button :icon="Setting" @click="openColumnDialog">{{ t('ops.columns.button') }}</el-button>
      <el-button @click="openExportDialog">{{ t('ops.toolbar.export') }}</el-button>
      <div style="flex: 1" />
      <el-button v-if="canWrite" type="primary" :loading="syncing"
                 :disabled="syncBlocked" :title="syncBlocked ? t('ops.sync.blocked') : ''"
                 @click="onSync">{{ t('ops.toolbar.sync') }}</el-button>
    </div>

    <el-alert v-if="loadError" type="error" :closable="false" show-icon class="load-error"
              :title="loadError">
      <el-button size="small" @click="load">{{ t('common.retry') }}</el-button>
    </el-alert>
    <el-table ref="tableRef" stripe :data="filteredItems" v-loading="loading" border
              :empty-text="loadError ? loadError : undefined"
              @selection-change="onSelectionChange">
      <el-table-column v-if="canWrite" type="selection" width="44" />
      <!-- 列由「列设置」驱动：顺序/显隐按产品页签存 localStorage，长期有效 -->
      <el-table-column v-for="col in visibleColumns" :key="col.key" v-bind="columnProps(col)">
        <template #header>
          <template v-if="col.key === 'tier'">
            <span>{{ columnTitle(col) }}</span>
            <el-tooltip :content="t('ops.tier.hint')" placement="top">
              <el-icon style="margin-left:4px;vertical-align:-2px;cursor:help"><QuestionFilled /></el-icon>
            </el-tooltip>
          </template>
          <span v-else>{{ columnTitle(col) }}</span>
        </template>
        <template #default="{ row }">
          <template v-if="col.key === 'name'">{{ row.name }}</template>
          <template v-else-if="col.key === 'instance_id'">
            <span class="mono">{{ row.instance_id }}</span>
            <CopyBtn :text="row.instance_id" />
          </template>
          <template v-else-if="col.key === 'vendor_account'">
            <el-tag size="small" effect="plain">{{ vendorAccountLabel(row) }}</el-tag>
          </template>
          <template v-else-if="col.key === 'os_name'">{{ row.os_name || '-' }}</template>
          <template v-else-if="col.key === 'ip'">
            <el-link v-if="isVmware && row.ip_internal" type="primary" :underline="false"
                     @click="openDetail(row)">{{ row.ip_internal }}</el-link>
            <span v-else-if="isVmware && row.tools_status && row.tools_status !== 'guestToolsRunning'"
                  class="sub">{{ t('ops.ip.tools_down') }}</span>
            <span v-else>{{ row.ip_internal || '-' }}</span>
          </template>
          <template v-else-if="col.key === 'public_ip'">{{ row.ip_public || '-' }}</template>
          <template v-else-if="col.key === 'status'">
            <el-tag :type="statusTag(row.status)" size="small">{{ assetStatusLabel(row.status) }}</el-tag>
          </template>
          <template v-else-if="col.key === 'tier'">
            <el-select v-if="canWrite" :model-value="row.tier || 'core'" size="small"
                       style="width: 110px" @change="(v) => changeTier(row, v)">
              <el-option value="core" :label="t('ops.tier.core')" />
              <el-option value="aux" :label="t('ops.tier.aux')" />
            </el-select>
            <el-tag v-else size="small" :type="row.tier === 'aux' ? 'info' : 'success'">
              {{ row.tier === 'aux' ? t('ops.tier.aux_short') : t('ops.tier.core_short') }}
            </el-tag>
          </template>
          <template v-else-if="col.key === 'spec'">
            {{ row.instance_type }}
            <!-- VMware 规格/系统/IP/存储已各占一列，不再把 summary 叠在规格下（避免重复两遍） -->
            <template v-if="!isVmware">
              <br />
              <span class="sub">{{ product === 'ecs' ? t('ops.table.spec_ecs', { cpu: row.cpu, mem: row.memory_gb }) : (row.summary || '') }}</span>
            </template>
          </template>
          <template v-else-if="col.key === 'disk'">
            {{ t(product === 'vmware_host' ? 'ops.table.storage_count' : 'ops.table.disk_count', { n: row.disk_count, gb: row.disk_total_gb }) }}
          </template>
          <template v-else-if="col.key === 'region'">{{ row.region || '-' }}</template>
          <template v-else-if="col.key === 'expire'">
            <!-- 证书行统一解析：实测为准/订单为辅 + 来源/差异标注 -->
            <template v-if="row.cert_resolved">
              <span>{{ (row.cert_resolved.not_after || row.expire_time || '').slice(0, 10) }}</span>
              <el-tag v-if="row.cert_resolved.source_conflict" size="small" type="warning" style="margin-left: 4px">{{ t('common.cert.conflict') }}</el-tag>
              <div class="sub">{{ row.cert_resolved.source_label }}</div>
            </template>
            <span v-else-if="row.expire_time">{{ (row.expire_time || '').slice(0, 10) }}</span>
            <span v-else class="sub">{{ t('ops.table.payg') }}</span>
          </template>
          <template v-else>{{ row[col.key] ?? '-' }}</template>
        </template>
      </el-table-column>
      <el-table-column :label="t('ops.col.actions')" width="140" fixed="right">
        <template #default="{ row }">
          <el-button size="small" link type="primary" @click="openDetail(row)">{{ t('ops.action.detail') }}</el-button>
          <!-- 电源高危操作收进「更多」（实例 ID 确认门仍在，防外围误触） -->
          <el-dropdown v-if="['ecs', 'vmware_vm'].includes(product) && canWrite" trigger="click" @command="(cmd) => powerAction(row, cmd)">
            <el-button size="small" link type="primary">{{ t('ops.action.more') }}<el-icon style="margin-left:2px;font-size:12px"><ArrowDown /></el-icon></el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item v-if="isRunningRow(row)" command="reboot">{{ t('ops.power.item_reboot') }}</el-dropdown-item>
                <el-dropdown-item v-if="isRunningRow(row)" command="stop" class="danger-item">{{ t('ops.power.item_stop') }}</el-dropdown-item>
                <el-dropdown-item v-if="isStoppedRow(row)" command="start">{{ t('ops.power.act_start') }}</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </template>
      </el-table-column>
    </el-table>

    <el-pagination
      v-model:current-page="query.page"
      :page-size="query.page_size"
      :total="total"
      layout="total, prev, pager, next"
      style="margin-top: 12px; justify-content: flex-end"
      @current-change="load"
    />

    <!-- 资产导出：先选资产类型，再按类型勾选具体列；后端按产品拆 sheet -->
    <el-dialog v-model="exportDialogVisible" :title="t('ops.export.title')" width="760px" top="6vh">
      <div v-loading="exportCatalogLoading">
        <el-alert type="info" :closable="false" show-icon class="export-tip"
                  :title="t('ops.export.tip', { n: exportMaxRows })" />
        <div v-if="!exportCatalogLoading && !exportProducts.length" class="sub">
          {{ t('ops.export.empty') }}
        </div>
        <div v-for="p in exportProducts" :key="p.product" class="export-product">
          <div class="export-product-head">
            <el-checkbox :model-value="exportSelectedProducts.includes(p.product)"
                         @change="(v) => toggleExportProduct(p, v)">
              {{ p.label }}
            </el-checkbox>
            <span v-if="exportSelectedProducts.includes(p.product)" class="sub">
              {{ exportSelectedColumns[p.product]?.length || 0 }}/{{ (p.columns || []).length }}
            </span>
            <div style="flex: 1" />
            <el-button link size="small" @click="setAllExportColumns(p, true)">{{ t('ops.export.all') }}</el-button>
            <el-button link size="small" @click="setAllExportColumns(p, false)">{{ t('ops.export.none') }}</el-button>
          </div>
          <el-checkbox-group v-if="exportSelectedProducts.includes(p.product)"
                             v-model="exportSelectedColumns[p.product]" class="export-col-grid">
            <el-checkbox v-for="c in p.columns" :key="c.key" :value="c.key">{{ c.label }}</el-checkbox>
          </el-checkbox-group>
        </div>
      </div>
      <template #footer>
        <el-button @click="exportDialogVisible = false">{{ t('ops.export.cancel') }}</el-button>
        <el-button type="primary" :loading="exporting"
                   :disabled="!exportSelectedProducts.length" @click="submitExport">
          {{ t('ops.export.submit') }}
        </el-button>
      </template>
    </el-dialog>

    <!-- 列展示设置：勾选显隐 + 上下移动排序，按产品页签长期保存 -->
    <el-dialog v-model="columnDialogVisible" :title="t('ops.columns.title', { product: tabLabel(product) })" width="520px">
      <div class="sub" style="margin-bottom: 10px">{{ t('ops.columns.tip') }}</div>
      <div v-for="(c, i) in columnDraft" :key="c.key" class="column-row">
        <el-checkbox v-model="c.visible">{{ columnTitle(c) }}</el-checkbox>
        <div style="flex: 1" />
        <el-button link :disabled="i === 0" @click="moveColumn(i, -1)">↑</el-button>
        <el-button link :disabled="i === columnDraft.length - 1" @click="moveColumn(i, 1)">↓</el-button>
      </div>
      <template #footer>
        <el-button @click="resetColumns">{{ t('ops.columns.reset') }}</el-button>
        <el-button @click="columnDialogVisible = false">{{ t('ops.columns.cancel') }}</el-button>
        <el-button type="primary" @click="saveColumns">{{ t('ops.columns.save') }}</el-button>
      </template>
    </el-dialog>

    <!-- 详情抽屉 -->
    <el-drawer v-model="detailVisible" :title="detail?.name || t('ops.detail.title')" size="560px">
      <div v-loading="detailLoading">
        <template v-if="detail">
          <!-- VMware 详情直接带监控状态：不再跳机器管理查 SSH 凭证 -->
          <div v-if="isVmware" class="detail-monitor">
            <div class="detail-monitor-head">
              <span>{{ t('ops.detail.monitor_title') }}</span>
              <div style="flex: 1" />
              <el-button link type="primary" size="small" @click="openMonitor">
                {{ t('ops.detail.monitor_more') }}
              </el-button>
            </div>
            <el-alert v-if="detailMetricsError" type="warning" :closable="false" show-icon
                      :title="detailMetricsError" />
            <div v-else v-loading="detailMetricsLoading" class="metric-row">
              <div v-for="m in detailMetrics" :key="m.label" class="metric-item">
                <div class="metric-label">{{ m.label }}</div>
                <div class="metric-value">
                  <template v-if="m.value != null">{{ m.value.toFixed(1) }}{{ m.unit }}</template>
                  <span v-else class="sub">{{ t('ops.detail.monitor_no_data') }}</span>
                </div>
                <div class="sub">{{ t('ops.detail.monitor_samples', { n: m.samples }) }}</div>
              </div>
            </div>
            <div v-if="!detailMetricsLoading && detailMetrics.length && !hasMetricSamples" class="sub">
              {{ t('ops.detail.monitor_empty_hint') }}
            </div>
          </div>
          <el-descriptions :column="2" border size="small">
            <el-descriptions-item v-if="!isVmware" :label="instanceIdLabel" :span="2">{{ detail.instance_id }}</el-descriptions-item>
            <el-descriptions-item :label="t('ops.col.vendor_account')" :span="2">{{ vendorAccountLabel(detail) }}</el-descriptions-item>
            <el-descriptions-item v-if="detailIp" :label="isVmware ? t('ops.col.ip') : t('ops.col.private_ip')">{{ detailIp }}</el-descriptions-item>
            <el-descriptions-item v-else-if="isVmware" :label="t('ops.col.ip')" :span="2">
              <span class="sub">{{ t('ops.detail.ip_tools_down') }}</span>
            </el-descriptions-item>
            <el-descriptions-item v-if="detail.extra?.ip_public" :label="t('ops.col.public_ip')">{{ detail.extra.ip_public }}</el-descriptions-item>
            <el-descriptions-item :label="t('ops.col.spec')" :span="detailCpu && !isVmware ? 1 : 2">
              {{ detail.instance_type }}<template v-if="detailCpu && !isVmware">（{{ detailCpu }}核 / {{ detailMemory }}G）</template>
            </el-descriptions-item>
            <el-descriptions-item :label="t('ops.col.status')">{{ assetStatusLabel(detail.status) }}</el-descriptions-item>
            <el-descriptions-item v-if="detailOs" :label="t('ops.detail.os')" :span="2">{{ detailOs }}</el-descriptions-item>
            <template v-if="!isVmware">
              <el-descriptions-item :label="t('ops.detail.vpc')">{{ detail.extra?.vpc_id || '-' }}</el-descriptions-item>
              <el-descriptions-item :label="t('ops.detail.vswitch')">{{ detail.extra?.vswitch_id || '-' }}</el-descriptions-item>
            </template>
            <el-descriptions-item :label="zoneLabel">{{ detail.zone || '-' }}</el-descriptions-item>
            <template v-if="!isVmware">
              <el-descriptions-item :label="t('ops.detail.charge')">{{ chargeLabel(detail.charge_type) }}</el-descriptions-item>
              <el-descriptions-item :label="t('ops.detail.expire_time')" :span="2">{{ detail.expire_time || '-' }}</el-descriptions-item>
            </template>
            <!-- 证书行统一解析：与订单到期并列，不一致明示 -->
            <el-descriptions-item v-if="detail.cert_resolved" :label="t('ops.cert.resolved_label')" :span="2">
              {{ (detail.cert_resolved.not_after || '').slice(0, 10) }}（{{ detail.cert_resolved.source_label }}）
              <template v-if="detail.cert_resolved.source_conflict">
                · {{ t('common.cert.conflict') }}：{{ t('common.cert.conflict_pair', {
                  probe: (detail.cert_resolved.probe_not_after || '').slice(0, 10),
                  order: (detail.cert_resolved.order_not_after || '').slice(0, 10),
                  diff: detail.cert_resolved.diff_days ?? 0,
                }) }}
                <template v-if="detail.cert_resolved.serial_mismatch">（{{ t('common.cert.conflict_serial') }}）</template>
              </template>
            </el-descriptions-item>
            <el-descriptions-item :label="t('ops.detail.synced_at')" :span="2">{{ detail.synced_at }}</el-descriptions-item>
          </el-descriptions>

          <!-- 产品差异字段 -->
          <h4 style="margin: 16px 0 8px">{{ t('ops.detail.product_info') }}</h4>
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item v-for="row in extraRows" :key="row.label" :label="row.label">
              {{ row.value }}
            </el-descriptions-item>
          </el-descriptions>

          <h4 v-if="product === 'ecs' || product === 'vmware_vm'" style="margin: 16px 0 8px">
            {{ t('ops.detail.disks_title', { n: detail.extra?.disks?.length || 0, gb: detail.extra?.disk_total_gb }) }}
          </h4>
          <el-table v-if="product === 'ecs'" stripe :data="detail.extra?.disks || []" border size="small">
            <el-table-column prop="device" :label="t('ops.detail.device')" width="100" />
            <el-table-column prop="type" :label="t('ops.detail.type')" width="80">
              <template #default="{ row }">
                <el-tag size="small" :type="row.type === 'system' ? 'primary' : 'success'">
                  {{ row.type === 'system' ? t('ops.detail.system_disk') : t('ops.detail.data_disk') }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="size_gb" :label="t('ops.detail.capacity')" width="80">
              <template #default="{ row }">{{ row.size_gb }}G</template>
            </el-table-column>
            <el-table-column prop="category" :label="t('ops.detail.category')" min-width="120" />
            <el-table-column :label="t('ops.col.expire')" width="105">
              <template #default="{ row }">{{ row.expire_time ? row.expire_time.slice(0, 10) : t('ops.detail.expire_with_instance') }}</template>
            </el-table-column>
          </el-table>
          <!-- VMware 虚拟磁盘：只读清单，标签 + 容量（vSphere 不给单盘计费/到期概念） -->
          <el-table v-if="product === 'vmware_vm'" stripe :data="detail.extra?.disks || []" border size="small">
            <el-table-column prop="label" :label="t('ops.detail.device')" min-width="220" show-overflow-tooltip />
            <el-table-column prop="size_gb" :label="t('ops.detail.capacity')" width="110">
              <template #default="{ row }">{{ row.size_gb }}G</template>
            </el-table-column>
          </el-table>

          <template v-if="!isVmware">
            <h4 style="margin: 16px 0 8px">{{ t('ops.detail.tags') }}</h4>
            <template v-if="detail.tags?.length">
              <el-tag v-for="tag in detail.tags" :key="tag.key" size="small" style="margin-right: 6px">
                {{ tag.key }}: {{ tag.value }}
              </el-tag>
            </template>
            <span v-else class="sub">{{ t('ops.detail.no_tags') }}</span>
          </template>
        </template>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.danger-item { color: var(--el-color-danger) !important; }
.toolbar {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
  align-items: center;
}
.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.export-tip { margin-bottom: 12px; }
.export-product {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 10px 12px;
  margin-bottom: 10px;
}
.export-product-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.export-col-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 2px 8px;
  margin-top: 8px;
  padding-left: 24px;
}
.column-row {
  display: flex;
  align-items: center;
  padding: 4px 0;
  border-bottom: 1px dashed var(--el-border-color-lighter);
}
.detail-monitor {
  margin-bottom: 14px;
  padding: 10px 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  background: var(--el-fill-color-lighter);
}
.detail-monitor-head {
  display: flex;
  align-items: center;
  font-weight: 600;
  margin-bottom: 8px;
}
.metric-row {
  display: flex;
  gap: 12px;
  min-height: 32px;
}
.metric-item {
  flex: 1;
  padding: 8px 10px;
  border-radius: 6px;
  background: var(--el-bg-color);
}
.metric-label { font-size: 12px; color: var(--el-text-color-secondary); }
.metric-value { font-size: 20px; font-weight: 600; margin: 2px 0; }
</style>
