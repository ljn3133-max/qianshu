<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  ArrowDown, Cloudy, Compass, Connection, Cpu, DataLine, Document, Grid, Monitor,
  Platform, Share, Ship, Sunrise, Tools, TrendCharts,
} from '@element-plus/icons-vue'

import http from '@/api/http'
import ApiCatalog from '@/components/ApiCatalog.vue'
import CopyBtn from '@/components/CopyBtn.vue'
import DataSourceDialog from '@/components/DataSourceDialog.vue'
import { t } from '@/i18n'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const router = useRouter()
const origin = window.location.origin
// 权限与后端逐一对齐：operator/viewer 不展示无权限的数据源「保存/删除」、
// 云账号「停用」、集群「删除/接入」入口，避免点击后才 403。
const auth = useAuthStore()
const canManage = computed(() => ['admin', 'operator'].includes(auth.role))
const isAdmin = computed(() => auth.role === 'admin')

// ?tab= 深链：数据顾问「应用日志未接入」等指引落 /integrations?tab=data，
// 不支持则落到默认「集成管理」页签——指引到了楼里没到柜台
const tab = ref(['manage', 'event', 'data', 'skills'].includes(route.query.tab) ? String(route.query.tab) : 'manage')
const overview = ref(null)
const guide = ref(null)
const guideVisible = ref(false)
const guideTesting = ref(false)
const guideTestResult = ref(null)
const testing = ref('')
const skills = ref(null)
const mcpTab = ref('mcp')
const compJson = ref('')
const compSaving = ref(false)

const ICONS = {
  Share, Compass, Platform, Tools, TrendCharts, DataLine, Document,
  Connection, Grid, Monitor, Cloudy, Sunrise, Ship, Cpu,
}
const TILE_COLORS = {
  gitlab: 'ic-orange', gitea: 'ic-green', github: 'ic-purple', jenkins: 'ic-blue',
  skywalking: 'ic-cyan', prometheus: 'ic-orange', loki: 'ic-purple', otlp: 'ic-cyan',
  k8s: 'ic-blue', zabbix: 'ic-pink', aliyun: 'ic-orange', tencent: 'ic-cyan',
  aws: 'ic-purple', vsphere: 'ic-blue',
}
const iconOf = (g) => ICONS[g?.icon] || DataLine

// 分类/类型标签：值为 i18n 键，渲染处 t() 化（产品名 zh/en 同形）
const CAT_LABELS = {
  event: 'integrations.cat.event', metric: 'integrations.cat.metric',
  log: 'integrations.cat.log', cloud: 'integrations.cat.cloud',
}
const TYPE_LABELS = {
  prometheus: 'integrations.type.prometheus', vm: 'integrations.type.vm', zabbix: 'integrations.type.zabbix',
  influxdb: 'integrations.type.influxdb', graphite: 'integrations.type.graphite', es: 'integrations.type.es',
  opentsdb: 'integrations.type.opentsdb', http: 'integrations.type.http', loki: 'integrations.type.loki',
  sls: 'integrations.type.sls', cls: 'integrations.type.cls', graylog: 'integrations.type.graylog', filebeat: 'integrations.type.filebeat',
  skywalking: 'integrations.type.skywalking', zipkin: 'integrations.type.zipkin',
  wecom_bot: 'integrations.type.wecom_bot', dingtalk_bot: 'integrations.type.dingtalk_bot',
  webhook: 'integrations.type.webhook',
  k8s: 'integrations.type.k8s', gitlab: 'integrations.type.gitlab', gitea: 'integrations.type.gitea',
  github: 'integrations.type.github', jenkins: 'integrations.type.jenkins',
}
const catLabel = (c) => (CAT_LABELS[c] ? t(CAT_LABELS[c]) : c)
const typeLabel = (x) => (TYPE_LABELS[x] ? t(TYPE_LABELS[x]) : x)

async function loadComponents() {
  try {
    const d = await http.get('/settings/components')
    compJson.value = JSON.stringify(d.rules, null, 2)
  } catch { /* 拦截器提示 */ }
}

async function saveComponents() {
  let rules
  try {
    rules = JSON.parse(compJson.value)
  } catch {
    ElMessage.error(t('integrations.msg.json_invalid'))
    return
  }
  compSaving.value = true
  try {
    await http.put('/settings/components', { rules })
    ElMessage.success(t('integrations.msg.comp_saved'))
    loadComponents()
  } finally {
    compSaving.value = false
  }
}

const dsConfig = ref({ metric_sources: [], log_sources: [] })
const dsBuiltin = ref(null)
const dsSaving = ref(false)
const dsProbe = ref(null)
const dsProbing = ref(false)
const k8sCount = ref(null)   // null=未知（读取失败），0=确实未接入

async function probeDatasources() {
  dsProbing.value = true
  try {
    dsProbe.value = await http.get('/datasources')
  } catch { /* 拦截器提示 */ } finally { dsProbing.value = false }
}

async function loadDsConfig() {
  try {
    const cfg = await http.get('/settings/datasource-config')
    dsConfig.value = {
      metric_sources: Array.isArray(cfg.metric_sources) ? cfg.metric_sources : [],
      log_sources: Array.isArray(cfg.log_sources) ? cfg.log_sources : [],
    }
    dsBuiltin.value = cfg.builtin_metric_source || null
  } catch { /* 拦截器提示 */ }
}

async function saveDsConfig() {
  dsSaving.value = true
  try {
    const r = await http.put('/settings/datasource-config', dsConfig.value)
    ElMessage.success(r.persisted ? t('integrations.msg.ds_saved_env') : t('integrations.msg.ds_saved'))
    probeDatasources()
  } finally {
    dsSaving.value = false
  }
}

// ---- 新增/编辑数据源（弹窗完整闭环：教程/表单/注意事项/测试连通） ----
const dsDialog = ref(false)
const dsPreset = ref('')
const dsEdit = ref(null)

function openDsDialog(type = '', edit = null) {
  dsPreset.value = type || ''
  dsEdit.value = edit
  dsDialog.value = true
}

function editSource(row) {
  openDsDialog('', { kind: row._kind, index: row._idx, source: dsConfig.value[row._kind][row._idx] })
}

async function removeSource(row) {
  try {
    await ElMessageBox.confirm(
      t('integrations.confirm.remove_ds', { name: row.name }), t('integrations.confirm.delete_title'),
      { type: 'warning', confirmButtonText: t('integrations.btn.delete'), cancelButtonText: t('integrations.btn.cancel') },
    )
  } catch { return }
  dsConfig.value[row._kind].splice(row._idx, 1)
  saveDsConfig()
}

function onDsSave({ kind, source, editIndex, editKind }) {
  if (editIndex >= 0 && editKind) {
    if (editKind === kind) dsConfig.value[editKind][editIndex] = source
    else {
      dsConfig.value[editKind].splice(editIndex, 1)
      dsConfig.value[kind].push(source)
    }
  } else {
    dsConfig.value[kind].push(source)
  }
  saveDsConfig()
}

// 探测结果行（指标+日志+APM 汇一列看）；配置行按 名称+类型 挂探测状态
const dsProbeRows = computed(() => ([
  ...((dsProbe.value && dsProbe.value.metrics) || []),
  ...((dsProbe.value && dsProbe.value.logs) || []),
  ...((dsProbe.value && dsProbe.value.apm) || []),
]))

const dsRows = computed(() => {
  const out = []
  for (const kind of ['metric_sources', 'log_sources']) {
    dsConfig.value[kind].forEach((s, i) => {
      const probe = dsProbeRows.value.find((r) => r.name === s.name && r.type === s.type)
        || dsProbeRows.value.find((r) => r.name === s.name) || null
      out.push({ ...s, _kind: kind, _idx: i, probe })
    })
  }
  return out
})

const eventGuides = computed(() => (overview.value?.guides || []).filter((g) => g.category === 'event'))
const dataGuides = computed(() => (overview.value?.guides || []).filter((g) => g.category !== 'event'))
const webhookBase = computed(() => `${origin}/api/events/webhook`)

// 卡片状态徽标（真实数据：事件=最近到达证据；数据源=探测结果；K8s=已接集群数）
function cardStatus(g) {
  if (g.category === 'event') {
    const es = overview.value?.event_status?.[g.key]
    if (!es) {
      return overview.value?.webhook?.token_configured
        ? { text: t('integrations.status.token_ok'), type: 'success' }
        : { text: t('integrations.status.token_missing'), type: 'warning' }
    }
    if (es.verdict === 'connected') {
      return { text: t('integrations.status.connected_recent', { n: es.evidence?.count_recent ?? 0 }), type: 'success' }
    }
    if (es.verdict === 'error') return { text: t('integrations.status.token_missing'), type: 'danger' }
    return (es.evidence?.count_total ?? 0) > 0
      ? { text: t('integrations.status.once_no_recent'), type: 'warning' }
      : { text: t('integrations.status.none_yet'), type: 'info' }
  }
  if (g.key === 'k8s') {
    if (k8sCount.value === null) {
      return { text: t('integrations.status.load_failed'), type: 'danger' }
    }
    return k8sCount.value
      ? { text: t('integrations.status.clusters', { n: k8sCount.value }), type: 'success' }
      : { text: t('integrations.status.not_connected'), type: 'info' }
  }
  const types = { prometheus: ['prometheus', 'vm'], loki: ['loki'], zabbix: ['zabbix'] }[g.key]
    || (g.ds_type ? [g.ds_type] : [])
  if (!types.length) return null
  const rows = dsProbeRows.value.filter((r) => types.includes(r.type))
  if (!rows.length) return { text: t('integrations.status.not_connected'), type: 'info' }
  const ok = rows.filter((r) => r.ok).length
  return ok
    ? { text: t('integrations.status.connected_ratio', { ok, total: rows.length }), type: 'success' }
    : { text: t('integrations.status.down'), type: 'danger' }
}

// ---- 集成管理：已接入集成全量清单 + 真实状态（无证据=未验证，不许摆设） ----
const registry = ref(null)
const registryLoading = ref(false)
const regFilter = ref('all')
const regSearch = ref('')
const rowTesting = ref('')

const REG_CATS = [
  { value: 'all', label: 'integrations.regCat.all' },
  { value: 'event', label: 'integrations.regCat.event' },
  { value: 'metric', label: 'integrations.regCat.metric' },
  { value: 'log', label: 'integrations.regCat.log' },
  { value: 'channel', label: 'integrations.regCat.channel' },
  { value: 'k8s', label: 'integrations.regCat.k8s' },
  { value: 'cloud', label: 'integrations.regCat.cloud' },
]
const STATUS_TAG = { connected: 'success', unverified: 'warning', error: 'danger', disabled: 'info' }

function verdictTag(v) {
  return { connected: 'success', unverified: 'warning', error: 'danger' }[v] || 'info'
}
const VERDICT_TEXT = {
  connected: 'integrations.verdict.connected', unverified: 'integrations.verdict.unverified', error: 'integrations.verdict.error',
}
function verdictText(v) {
  return (VERDICT_TEXT[v] ? t(VERDICT_TEXT[v]) : (v || ''))
}
function verificationMethod(row) {
  return t(`integrations.verify.${row.category}`)
}
function testButtonLabel(row) {
  if (row.category === 'event') return t('integrations.btn.check_event')
  if (row.category === 'channel') return t('integrations.btn.check_channel')
  return t('integrations.btn.test_readonly')
}
function fmtTime(iso) {
  return String(iso || '').replace('T', ' ').slice(0, 16)
}

async function loadRegistry() {
  registryLoading.value = true
  try {
    registry.value = await http.get('/integrations/registry')
  } catch { /* 拦截器提示 */ } finally { registryLoading.value = false }
}

const regRows = computed(() => {
  const rows = (registry.value && registry.value.rows) || []
  const kw = regSearch.value.trim().toLowerCase()
  return rows.filter((r) => {
    if (regFilter.value !== 'all' && r.category !== regFilter.value) return false
    if (kw && !`${r.name} ${r.type} ${r.endpoint}`.toLowerCase().includes(kw)) return false
    return true
  })
})
const regSummary = computed(() => (registry.value && registry.value.summary) || {})

function findDsCfg(row) {
  const list = dsConfig.value[row.cfg_kind] || []
  return list.find((s) => s.name === row.ref_id && s.type === row.type) || null
}

function mapProbe(r) {
  if (r.pending || r.ok === null || r.ok === undefined) {
    return { ok: null, verdict: 'unverified', message: r.detail || t('integrations.verdict.unverified') }
  }
  return r.ok
    ? { ok: true, verdict: 'connected', message: t('integrations.verdict.connected_detail', { detail: r.detail || '' }) }
    : { ok: false, verdict: 'error', message: t('integrations.verdict.error_detail', { detail: r.detail || '' }) }
}

// 行内「测试」：每类走真实验证——事件源=最近到达证据；数据源=版本探测；
// 渠道=只读凭证自检（绝不发消息）；K8s/云账号=真实连通探测
async function testRow(row) {
  rowTesting.value = row.id
  try {
    let r
    if (row.category === 'event') {
      r = await http.post(`/integrations/test/${row.type}`)
    } else if (row.category === 'metric' || row.category === 'log') {
      const cfg = findDsCfg(row) || {}
      r = mapProbe(await http.post('/datasources/test', {
        name: row.name, type: row.type,
        url: cfg.url || (row.endpoint.startsWith('http') ? row.endpoint : ''),
        headers: cfg.headers, user: cfg.user, password: cfg.password, token: cfg.token,
        verify_ssl: cfg.verify_ssl,
      }))
    } else if (row.category === 'channel') {
      r = await http.post(`/integrations/check-channel/${row.ref_id}`)
    } else if (row.category === 'k8s') {
      const t = await http.post(`/k8s/clusters/${row.ref_id}/test`)
      r = { ok: !!t.ok, verdict: t.ok ? 'connected' : 'error', message: t.detail || '' }
    } else if (row.category === 'cloud') {
      const t = await http.post(`/clouds/accounts/${row.ref_id}/test`)
      r = t.supported === false
        ? { ok: null, verdict: 'unverified', message: t.message || '' }
        : { ok: !!t.ok, verdict: t.ok ? 'connected' : 'error', message: t.message || '' }
    }
    if (r) {
      ElMessage({
        type: r.verdict === 'connected' ? 'success' : (r.verdict === 'error' ? 'error' : 'warning'),
        message: t('integrations.msg.test_result', { name: row.name, msg: r.message || verdictText(r.verdict) }),
        duration: 7000,
      })
      loadRegistry()
    }
  } catch { /* 拦截器提示 */ } finally { rowTesting.value = '' }
}

// 通知渠道管理已收口到 系统设置→通知渠道：本页只读展示+跳转，
// 不再有渠道增删改/启停写口（同一张 alert_channels 表只留 Settings 一个管理面）
function goChannelSettings() {
  router.push('/settings?tab=channels')
}

function editRow(row) {
  if (row.category === 'metric' || row.category === 'log') {
    const idx = (dsConfig.value[row.cfg_kind] || [])
      .findIndex((s) => s.name === row.ref_id && s.type === row.type)
    if (idx >= 0) {
      openDsDialog('', { kind: row.cfg_kind, index: idx, source: dsConfig.value[row.cfg_kind][idx] })
    } else {
      ElMessage.warning(t('integrations.msg.ds_not_found'))
    }
  } else if (row.category === 'channel') {
    goChannelSettings()
  } else if (row.category === 'k8s') {
    openK8sEdit(row)
  } else if (row.category === 'cloud') {
    router.push('/clouds')
  } else if (row.category === 'event') {
    openGuide(row.type)
  }
}

function toggleLabel(row) {
  if (row.category === 'cloud') return row.enabled ? t('integrations.btn.disable') : t('integrations.btn.enable')
  return t('integrations.btn.remove')
}

async function toggleRow(row) {
  if (row.category === 'cloud') {
    const on = !row.enabled
    try {
      await ElMessageBox.confirm(
        on
          ? t('integrations.confirm.enable_item', { name: row.name })
          : t('integrations.confirm.disable_item', { name: row.name }),
        on ? t('integrations.confirm.enable_title') : t('integrations.confirm.disable_title'),
        { type: 'warning', confirmButtonText: t('integrations.btn.ok'), cancelButtonText: t('integrations.btn.cancel') },
      )
    } catch { return }
    await http.put(`/clouds/accounts/${row.ref_id}`, { enabled: on })
    ElMessage.success(on
      ? t('integrations.msg.enabled_item', { name: row.name })
      : t('integrations.msg.disabled_item', { name: row.name }))
  } else if (row.category === 'metric' || row.category === 'log') {
    try {
      await ElMessageBox.confirm(
        t('integrations.confirm.remove_ds_full', { name: row.name }), t('integrations.confirm.remove_title'),
        { type: 'warning', confirmButtonText: t('integrations.btn.remove'), cancelButtonText: t('integrations.btn.cancel') },
      )
    } catch { return }
    const idx = (dsConfig.value[row.cfg_kind] || [])
      .findIndex((s) => s.name === row.ref_id && s.type === row.type)
    if (idx >= 0) {
      dsConfig.value[row.cfg_kind].splice(idx, 1)
      await saveDsConfig()
    }
  } else if (row.category === 'k8s') {
    try {
      await ElMessageBox.confirm(
        t('integrations.confirm.remove_cluster', { name: row.name }), t('integrations.confirm.remove_title'),
        { type: 'warning', confirmButtonText: t('integrations.btn.remove'), cancelButtonText: t('integrations.btn.cancel') },
      )
    } catch { return }
    await http.delete(`/k8s/clusters/${row.ref_id}`)
    ElMessage.success(t('integrations.msg.cluster_removed', { name: row.name }))
    load()
  }
  loadRegistry()
}

function onAddIntegration(cmd) {
  if (cmd === 'ds') openDsDialog()
  else if (cmd === 'channel') goChannelSettings()
  else if (cmd === 'k8s') openK8s()
  else if (cmd === 'cloud') router.push('/clouds')
}

async function load() {
  try {
    ;[overview.value, skills.value] = await Promise.all([
      http.get('/integrations/overview'),
      http.get('/tools/skills'),
    ])
    if (overview.value?.datasources) dsProbe.value = overview.value.datasources
  } catch { /* 拦截器提示 */ }
  try {
    const cs = await http.get('/k8s/clusters')
    k8sCount.value = Array.isArray(cs) ? cs.length : (cs?.items?.length || 0)
  } catch { k8sCount.value = null }
  loadRegistry()
}

// ---- 统一教程对话框（五段：这是什么/配置步骤/接入点/注意事项/测试连通） ----
async function openGuide(key) {
  guideTestResult.value = null
  guide.value = await http.get(`/integrations/guide/${key}`)
  guideVisible.value = true
}

// 动态地址归一（前端渲染层）：{base_url}/{webhook_url}/{host} 按当前浏览器访问地址替换——
// 用户用 IP 访问显 IP，配了域名显域名；后端已替换过的也按 origin 再归一
function localText(s) {
  if (!s || typeof s !== 'string') return s
  let out = s
    .split('{webhook_url}').join(`${origin}/api/events/webhook`)
    .split('{base_url}').join(origin)
    .split('{host}').join(location.hostname)
  const base = guide.value?.base_url
  if (base) {
    out = out.split(base).join(origin)
    const bh = base.split('://')[1]?.split(':')[0]
    if (bh) out = out.split(`://${bh}`).join(`://${location.hostname}`)
  }
  return out
}

// 兼容教程两种返回形状（统一五段 / 旧 payload_hint 形状）——接口换代页面不塌
const guideNotes = computed(() => {
  const g = guide.value
  if (!g) return []
  const notes = Array.isArray(g.notes) ? [...g.notes] : []
  if (g.payload_hint) notes.push(t('integrations.guide.protocol', { hint: g.payload_hint }))
  return notes
})
const guideEndpoint = computed(() => guide.value?.endpoint || guide.value?.webhook_url || '')
const guideTestable = computed(() => guide.value
  ? (guide.value.testable ?? guide.value.category === 'event') : false)

async function selfTest(key) {
  guideTesting.value = true
  testing.value = key
  guideTestResult.value = null
  try {
    const r = await http.post(`/integrations/test/${key}`)
    guideTestResult.value = r
    ElMessage({
      type: r.verdict === 'connected' ? 'success' : (r.verdict === 'error' ? 'error' : 'warning'),
      message: r.message, duration: 7000,
    })
    load()
    loadRegistry()
  } catch { /* 拦截器提示 */ } finally {
    guideTesting.value = false
    testing.value = ''
  }
}

// ---- K8s 接入（教程在统一教程对话框，本弹窗只做接入表单+连通测试） ----
const k8sDialog = ref(false)
const k8sTesting = ref(false)
const k8sSaving = ref(false)
const k8sTestResult = ref(null)
const k8sEditId = ref(0)
const k8sForm = reactive({ name: '', api_url: '', token: '', verify_tls: false, ns_scope: '' })

function openK8s() {
  k8sTestResult.value = null
  k8sEditId.value = 0
  k8sForm.name = ''
  k8sForm.api_url = ''
  k8sForm.token = ''
  k8sForm.verify_tls = false
  k8sForm.ns_scope = ''
  k8sDialog.value = true
}

// 集成管理行内「编辑」：集群名/API 地址为登记事实不可改，可改 Token（留空=不改）/
// TLS 校验/命名空间范围（PATCH /k8s/clusters/{cid} 语义一致）
async function openK8sEdit(row) {
  k8sTestResult.value = null
  k8sEditId.value = row.ref_id
  k8sForm.name = row.name
  k8sForm.api_url = row.endpoint
  k8sForm.token = ''
  try {
    const cs = await http.get('/k8s/clusters')
    const arr = Array.isArray(cs) ? cs : (cs?.items || [])
    const cur = arr.find((c) => c.id === row.ref_id)
    k8sForm.verify_tls = cur ? !!cur.verify_tls : true
    k8sForm.ns_scope = cur ? (cur.ns_scope || '') : ''
  } catch {
    k8sForm.verify_tls = true
    k8sForm.ns_scope = ''
  }
  k8sDialog.value = true
}

async function testK8s() {
  if (k8sEditId.value) {
    k8sTesting.value = true
    try {
      k8sTestResult.value = await http.post(`/k8s/clusters/${k8sEditId.value}/test`)
      ElMessage({ type: k8sTestResult.value.ok ? 'success' : 'error', message: k8sTestResult.value.detail })
    } finally {
      k8sTesting.value = false
    }
    return
  }
  if (!k8sForm.api_url || !k8sForm.token) return ElMessage.warning(t('integrations.msg.need_api_token'))
  k8sTesting.value = true
  try {
    k8sTestResult.value = await http.post('/k8s/clusters/test', { ...k8sForm })
    ElMessage({ type: k8sTestResult.value.ok ? 'success' : 'error', message: k8sTestResult.value.detail })
  } finally {
    k8sTesting.value = false
  }
}

async function saveK8s() {
  if (k8sEditId.value) {
    k8sSaving.value = true
    try {
      const payload = { verify_tls: k8sForm.verify_tls, ns_scope: k8sForm.ns_scope }
      if (k8sForm.token) payload.token = k8sForm.token
      await http.patch(`/k8s/clusters/${k8sEditId.value}`, payload)
      ElMessage.success(t('integrations.msg.cluster_updated'))
      k8sDialog.value = false
      k8sEditId.value = 0
      load()
      loadRegistry()
    } finally {
      k8sSaving.value = false
    }
    return
  }
  if (!k8sForm.name || !k8sForm.api_url || !k8sForm.token) return ElMessage.warning(t('integrations.msg.need_name_api_token'))
  k8sSaving.value = true
  try {
    await http.post('/k8s/clusters', { ...k8sForm })
    ElMessage.success(t('integrations.msg.cluster_added'))
    k8sDialog.value = false
    load()
    loadRegistry()
  } finally {
    k8sSaving.value = false
  }
}

const mcpConfig = computed(() => JSON.stringify({
  mcpServers: {
    qianshu: {
      url: `${origin}/api/mcp`,
      headers: { Authorization: t('integrations.mcp.token_placeholder') },
    },
  },
}, null, 2))

onMounted(() => { load(); loadDsConfig(); loadComponents() })
</script>

<template>
  <div class="page">
    <div class="head">
      <b style="font-size: 16px">{{ t('integrations.title') }}</b>
    </div>

    <el-tabs v-model="tab">
      <!-- ===== 集成管理：已接入集成全量清单 + 真实状态 + 新增/编辑/测试/停用 ===== -->
      <el-tab-pane :label="t('integrations.tabs.manage')" name="manage">
        <div class="toolbar">
          <el-dropdown v-if="isAdmin" @command="onAddIntegration">
            <el-button type="primary">
              {{ t('integrations.btn.add') }}<el-icon class="el-icon--right"><ArrowDown /></el-icon>
            </el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="ds">{{ t('integrations.add.ds') }}</el-dropdown-item>
                <el-dropdown-item command="channel">{{ t('integrations.add.channel') }}</el-dropdown-item>
                <el-dropdown-item command="k8s">{{ t('integrations.add.k8s') }}</el-dropdown-item>
                <el-dropdown-item command="cloud">{{ t('integrations.add.cloud') }}</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
          <el-button :loading="registryLoading" @click="loadRegistry">{{ t('integrations.btn.refresh') }}</el-button>
          <el-select v-model="regFilter" style="width: 150px">
            <el-option v-for="c in REG_CATS" :key="c.value" :label="t(c.label)" :value="c.value" />
          </el-select>
          <el-input v-model="regSearch" :placeholder="t('integrations.reg.search_ph')" clearable style="width: 210px" />
          <span v-if="regSummary.total" class="tip">
            {{ t('integrations.reg.total', { n: regSummary.total }) }} ·
            <span class="rs-ok">{{ t('integrations.reg.connected', { n: regSummary.connected || 0 }) }}</span> ·
            <span class="rs-warn">{{ t('integrations.reg.unverified', { n: regSummary.unverified || 0 }) }}</span> ·
            <span class="rs-err">{{ t('integrations.reg.error', { n: regSummary.error || 0 }) }}</span>
            <template v-if="regSummary.disabled"> · {{ t('integrations.reg.disabled', { n: regSummary.disabled }) }}</template>
          </span>
        </div>

        <el-alert type="info" :closable="false" class="mb-12">
          {{ t('integrations.reg.real_data_note') }}
        </el-alert>

        <!-- 通知渠道只读摘要：管理面唯一在 系统设置→通知渠道，本页只读展示+跳转 -->
        <el-card shadow="never" class="mb-12">
          <template #header>
            <div style="display: flex; align-items: center; gap: 12px">
              <b>{{ t('integrations.ch_ro.title') }}</b>
              <el-button size="small" type="primary" @click="goChannelSettings">{{ t('integrations.btn.ch_settings') }}</el-button>
            </div>
          </template>
          <div class="tip">{{ t('integrations.ch_ro.note') }}</div>
        </el-card>

        <el-table :data="regRows" size="small" border v-loading="registryLoading"
                  :empty-text="t('integrations.reg.empty')">
          <el-table-column :label="t('integrations.col.status')" width="235">
            <template #default="{ row }">
              <el-tag :type="STATUS_TAG[row.status] || 'info'" size="small">{{ row.status_text }}</el-tag>
              <div class="reg-detail" :title="row.detail">{{ row.detail }}</div>
            </template>
          </el-table-column>
          <el-table-column :label="t('integrations.col.name')" width="150" show-overflow-tooltip>
            <template #default="{ row }">
              {{ row.name }}
              <el-tag v-if="row.builtin" size="small" type="success" effect="plain">{{ t('integrations.tag.builtin') }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column :label="t('integrations.col.category')" width="100">
            <template #default="{ row }">{{ row.category_label }}</template>
          </el-table-column>
          <el-table-column :label="t('integrations.col.type')" width="135">
            <template #default="{ row }">{{ typeLabel(row.type) }}</template>
          </el-table-column>
          <el-table-column prop="endpoint" :label="t('integrations.col.endpoint')" min-width="190" show-overflow-tooltip />
          <el-table-column :label="t('integrations.col.verification')" min-width="220">
            <template #default="{ row }"><span class="tip">{{ verificationMethod(row) }}</span></template>
          </el-table-column>
          <el-table-column :label="t('integrations.col.last_data')" width="155">
            <template #default="{ row }">
              <span v-if="row.last_data_at" class="reg-last">{{ row.last_data_label }} {{ fmtTime(row.last_data_at) }}</span>
              <span v-else class="tip">—</span>
            </template>
          </el-table-column>
          <el-table-column :label="t('integrations.col.actions')" width="280" fixed="right">
            <template #default="{ row }">
              <el-button v-if="row.testable && canManage" size="small" text type="primary"
                         :loading="rowTesting === row.id" @click="testRow(row)">{{ testButtonLabel(row) }}</el-button>
              <el-button v-if="row.category === 'event'" size="small" text type="primary"
                         @click="editRow(row)">{{ t('integrations.btn.detail') }}</el-button>
              <el-button v-else-if="row.category === 'channel'" size="small" text type="primary"
                         @click="goChannelSettings">{{ t('integrations.btn.ch_settings') }}</el-button>
              <el-button v-else-if="row.editable && isAdmin" size="small" text type="primary"
                         @click="editRow(row)">{{ t('integrations.btn.edit') }}</el-button>
              <el-button v-if="isAdmin && (row.category === 'cloud' || row.removable)"
                         size="small" text :type="row.removable ? 'danger' : 'warning'"
                         @click="toggleRow(row)">{{ toggleLabel(row) }}</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <!-- ===== 事件源 ===== -->
      <el-tab-pane :label="t('integrations.tabs.event')" name="event">
        <el-alert v-if="overview && !overview.webhook.token_configured" type="warning" :closable="false"
                  :title="t('integrations.event.token_warn')" class="mb-12" />
        <div class="url-bar">
          <span>{{ t('integrations.event.endpoint_label') }}</span>
          <code>{{ webhookBase }}/{{ '{' + t('integrations.event.path_ph') + '}' }}</code>
          <CopyBtn :text="webhookBase + '/gitlab'" />
          <span class="tip">{{ t('integrations.event.endpoints_tip') }}</span>
        </div>
        <div class="card-grid">
          <div v-for="g in eventGuides" :key="g.key" class="int-card">
            <div class="ic-head">
              <div class="ic-tile" :class="TILE_COLORS[g.key] || 'ic-blue'">
                <el-icon :size="20"><component :is="iconOf(g)" /></el-icon>
              </div>
              <div class="ic-main">
                <div class="ic-name">{{ g.name }}</div>
                <div class="ic-cat">
                  <el-tag size="small" effect="plain" type="info">{{ catLabel(g.category) }}</el-tag>
                </div>
              </div>
              <el-tag v-if="cardStatus(g)" size="small" :type="cardStatus(g).type">{{ cardStatus(g).text }}</el-tag>
            </div>
            <div class="ic-actions">
              <el-button size="small" type="primary" @click.stop="openGuide(g.key)">{{ t('integrations.btn.guide') }}</el-button>
            </div>
          </div>
        </div>
      </el-tab-pane>

      <!-- ===== 数据源 ===== -->
      <el-tab-pane :label="t('integrations.tabs.data')" name="data">
        <div class="toolbar">
          <el-button type="primary" @click="openDsDialog()">{{ t('integrations.btn.add_ds') }}</el-button>
          <span v-if="overview" class="tip">
            {{ t('integrations.ds.summary', {
              mok: overview.datasources.summary.metric_ok, mtot: overview.datasources.summary.metric_total,
              lok: overview.datasources.summary.log_ok, ltot: overview.datasources.summary.log_total,
            }) }}
          </span>
          <el-button size="small" :loading="dsProbing" @click="probeDatasources">{{ t('integrations.btn.reprobe') }}</el-button>
        </div>

        <div v-if="dsBuiltin" class="builtin-row">
          <el-tag size="small" type="success" effect="plain">{{ t('integrations.tag.builtin') }}</el-tag>
          <span>{{ dsBuiltin.label }}：{{ dsBuiltin.name }}</span>
          <span class="tip">{{ t('integrations.ds.builtin_tip') }}</span>
        </div>

        <el-table :data="dsRows" size="small" border class="mb-12"
                  :empty-text="t('integrations.ds.empty')">
          <el-table-column :label="t('integrations.col.status')" width="170">
            <template #default="{ row }">
              <template v-if="row.probe">
                <el-tag v-if="row.probe.pending" type="info" size="small">{{ t('integrations.ds.pending_adapter') }}</el-tag>
                <template v-else>
                  <el-tag :type="row.probe.ok ? 'success' : 'danger'" size="small">
                    {{ row.probe.ok ? t('integrations.ds.up') : t('integrations.ds.down') }}
                  </el-tag>
                  <span class="ds-detail" :title="row.probe.detail">
                    {{ row.probe.detail ? ' · ' + row.probe.detail : '' }}
                  </span>
                </template>
              </template>
              <el-tag v-else type="info" size="small">{{ t('integrations.ds.untested') }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="name" :label="t('integrations.col.name')" width="150" />
          <el-table-column :label="t('integrations.col.type')" width="140">
            <template #default="{ row }">{{ typeLabel(row.type) }}</template>
          </el-table-column>
          <el-table-column prop="url" :label="t('integrations.col.url')" min-width="200" show-overflow-tooltip />
          <el-table-column :label="t('integrations.col.actions')" width="130" fixed="right">
            <template #default="{ row }">
              <template v-if="isAdmin">
                <el-button size="small" text type="primary" @click="editSource(row)">{{ t('integrations.btn.edit') }}</el-button>
                <el-button size="small" text type="danger" @click="removeSource(row)">{{ t('integrations.btn.delete') }}</el-button>
              </template>
              <span v-else class="tip">—</span>
            </template>
          </el-table-column>
        </el-table>

        <div class="card-grid">
          <div v-for="g in dataGuides" :key="g.key" class="int-card">
            <div class="ic-head">
              <div class="ic-tile" :class="TILE_COLORS[g.key] || 'ic-blue'">
                <el-icon :size="20"><component :is="iconOf(g)" /></el-icon>
              </div>
              <div class="ic-main">
                <div class="ic-name">{{ g.name }}</div>
                <div class="ic-cat">
                  <el-tag size="small" effect="plain" type="info">{{ catLabel(g.category) }}</el-tag>
                </div>
              </div>
              <el-tag v-if="cardStatus(g)" size="small" :type="cardStatus(g).type">{{ cardStatus(g).text }}</el-tag>
            </div>
            <div class="ic-actions">
              <el-button v-if="isAdmin && g.ds_type" size="small" type="primary"
                         @click.stop="openDsDialog(g.ds_type)">{{ t('integrations.btn.config') }}</el-button>
              <el-button v-else-if="isAdmin && (g.key === 'tencent' || g.key === 'aws')" size="small" type="primary"
                         @click.stop="openDsDialog('prometheus')">{{ t('integrations.btn.config') }}</el-button>
              <el-button v-else-if="isAdmin && g.key === 'k8s'" size="small" type="primary"
                         @click.stop="openK8s()">{{ t('integrations.btn.connect_cluster') }}</el-button>
              <el-button v-else-if="['aliyun', 'vsphere', 'kvm', 'vmware'].includes(g.key)" size="small" type="primary"
                         @click.stop="router.push('/clouds')">{{ t('integrations.btn.cloud_mgmt') }}</el-button>
              <el-button size="small" text type="primary" @click.stop="openGuide(g.key)">{{ t('integrations.btn.guide') }}</el-button>
            </div>
          </div>
        </div>
      </el-tab-pane>

      <!-- ===== 能力开放 ===== -->
      <el-tab-pane :label="t('integrations.tabs.skills')" name="skills">
        <el-radio-group v-model="mcpTab" class="mb-12">
          <el-radio-button value="mcp">{{ t('integrations.skill.tab_mcp') }}</el-radio-button>
          <el-radio-button value="skills">{{ t('integrations.skill.tab_list', { n: skills?.count ?? 0 }) }}</el-radio-button>
          <el-radio-button value="api">{{ t('integrations.skill.tab_api') }}</el-radio-button>
          <el-radio-button value="comp">{{ t('integrations.skill.tab_comp') }}</el-radio-button>
        </el-radio-group>

        <template v-if="mcpTab === 'mcp'">
          <el-card shadow="never">
            <template #header><b>{{ t('integrations.mcp.title') }}</b></template>
            <div class="tip">{{ t('integrations.mcp.desc', { n: skills?.count ?? 0 }) }}</div>
            <pre class="code-box">{{ mcpConfig }}</pre>
            <div class="tip">{{ t('integrations.mcp.tools_json') }}<code>GET /api/tools/mcp</code></div>
          </el-card>
        </template>

        <template v-else-if="mcpTab === 'skills'">
          <el-card shadow="never">
            <template #header><b>{{ t('integrations.skill.list_title', { n: skills?.count ?? 0 }) }}</b></template>
            <el-table :data="skills?.skills || []" size="small" border max-height="520">
              <el-table-column prop="name" :label="t('integrations.skill.col_name')" width="150" />
              <el-table-column prop="title" :label="t('integrations.skill.col_what')" min-width="180" show-overflow-tooltip />
              <el-table-column :label="t('integrations.skill.col_scenario')" width="130">
                <template #default="{ row }">
                  <el-tag v-for="s in row.scenarios" :key="s" size="small" class="sc-tag">{{ s }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="usage" :label="t('integrations.skill.col_usage')" min-width="220" show-overflow-tooltip />
              <el-table-column label="" width="90">
                <template #default="{ row }"><CopyBtn :text="row.example" /></template>
              </el-table-column>
            </el-table>
          </el-card>
        </template>

        <template v-else-if="mcpTab === 'comp'">
          <el-card shadow="never">
            <template #header>
              <div class="head">
                <b>{{ t('integrations.comp.title') }}</b>
                <span class="tip">{{ t('integrations.comp.tip') }}</span>
              </div>
            </template>
            <el-input v-model="compJson" type="textarea" :rows="16" class="mono" />
            <div class="comp-ops">
              <el-button type="primary" size="small" :loading="compSaving" @click="saveComponents">{{ t('integrations.comp.save') }}</el-button>
              <el-button size="small" @click="loadComponents">{{ t('integrations.comp.reset') }}</el-button>
            </div>
          </el-card>
        </template>

        <template v-else>
          <ApiCatalog />
        </template>
      </el-tab-pane>
    </el-tabs>

    <!-- 统一教程对话框（这是什么/配置步骤/接入点/注意事项/测试连通） -->
    <el-dialog v-model="guideVisible" :title="t('integrations.guide.title', { name: guide?.name || '' })" width="760px">
      <template v-if="guide">
        <div class="gs-title"><span class="gs-dot" />{{ t('integrations.guide.what') }}</div>
        <div class="gs-summary">{{ localText(guide.summary) }}</div>

        <div class="gs-title"><span class="gs-dot" />{{ t('integrations.guide.steps') }}</div>
        <div class="gs-steps">
          <div v-for="(s, i) in guide.steps" :key="i" class="gs-step">
            <span class="gs-idx">{{ i + 1 }}</span>
            <div class="gs-text">{{ localText(s) }}</div>
          </div>
        </div>
        <template v-for="(c, i) in (guide.code_blocks || [])" :key="i">
          <div class="guide-code-head">
            <b>{{ c.title }}</b>
            <CopyBtn :text="localText(c.code)" />
          </div>
          <pre class="code-box">{{ localText(c.code) }}</pre>
        </template>

        <div class="gs-title"><span class="gs-dot" />{{ t('integrations.guide.endpoint') }}</div>
        <div v-if="guideEndpoint" class="url-bar gs-url">
          <code>{{ localText(guideEndpoint) }}</code>
          <CopyBtn :text="localText(guideEndpoint)" />
        </div>
        <div v-else class="gs-summary">{{ t('integrations.guide.no_endpoint') }}</div>

        <div class="gs-title"><span class="gs-dot" />{{ t('integrations.guide.notes') }}</div>
        <el-alert type="warning" :closable="false">
          <ul class="gs-notes">
            <li v-for="(n, i) in guideNotes" :key="i">{{ localText(n) }}</li>
          </ul>
        </el-alert>

        <div class="gs-title"><span class="gs-dot" />{{ t('integrations.guide.test') }}</div>
        <div class="gs-test">
          <span class="tip">{{ localText(guide.test_hint || (guideTestable ? t('integrations.guide.test_hint') : '')) }}</span>
          <div class="gs-test-ops">
            <el-button v-if="guideTestable" size="small" type="primary"
                       :loading="testing === guide.key" @click="selfTest(guide.key)">{{ t('integrations.guide.check_link') }}</el-button>
            <el-button v-else-if="guide.key === 'k8s'" size="small" type="primary"
                       @click="guideVisible = false; openK8s()">{{ t('integrations.guide.open_connect') }}</el-button>
            <el-button v-else-if="guide.ds_type" size="small" type="primary"
                       @click="guideVisible = false; openDsDialog(guide.ds_type)">{{ t('integrations.guide.go_config') }}</el-button>
          </div>
          <div v-if="guideTestResult" class="gs-test-result">
            <el-tag :type="verdictTag(guideTestResult.verdict)" size="small">
              {{ verdictText(guideTestResult.verdict) }}
            </el-tag>
            <span>{{ guideTestResult.message }}</span>
          </div>
        </div>
      </template>
    </el-dialog>

    <!-- K8s 接入/编辑对话框 -->
    <el-dialog v-model="k8sDialog" :title="k8sEditId ? t('integrations.k8s.edit_title') : t('integrations.k8s.add_title')" width="560px">
      <el-form :model="k8sForm" label-width="110px">
        <el-form-item :label="t('integrations.k8s.name')"><el-input v-model="k8sForm.name" :placeholder="t('integrations.k8s.name_ph')" :disabled="!!k8sEditId" /></el-form-item>
        <el-form-item :label="t('integrations.k8s.api')"><el-input v-model="k8sForm.api_url" placeholder="https://<master>:6443" :disabled="!!k8sEditId" /></el-form-item>
        <el-form-item label="SA Token">
          <el-input v-model="k8sForm.token" type="textarea" :rows="3" show-password
                    :placeholder="k8sEditId ? t('integrations.k8s.token_keep') : t('integrations.k8s.token_ph')" />
        </el-form-item>
        <el-form-item :label="t('integrations.k8s.tls')">
          <el-switch v-model="k8sForm.verify_tls" :active-text="t('integrations.k8s.tls_on')" :inactive-text="t('integrations.k8s.tls_off')" />
        </el-form-item>
        <el-form-item :label="t('integrations.k8s.ns')">
          <el-input v-model="k8sForm.ns_scope" :placeholder="t('integrations.k8s.ns_ph')" />
        </el-form-item>
      </el-form>
      <div v-if="k8sTestResult" class="tip" :style="{ color: k8sTestResult.ok ? '#67c23a' : '#f56c6c' }">
        {{ t('integrations.k8s.test_result') }}{{ k8sTestResult.detail }}
      </div>
      <template #footer>
        <el-button @click="k8sDialog = false">{{ t('integrations.btn.cancel') }}</el-button>
        <el-button :loading="k8sTesting" @click="testK8s">{{ t('integrations.k8s.test_btn') }}</el-button>
        <el-button type="primary" :loading="k8sSaving" @click="saveK8s">{{ t('integrations.k8s.connect_btn') }}</el-button>
      </template>
    </el-dialog>

    <!-- 新增/编辑数据源（教程/表单/注意事项/测试连通四区闭环） -->
    <DataSourceDialog v-model="dsDialog" :preset-type="dsPreset" :edit-source="dsEdit" @save="onDsSave" />
  </div>
</template>

<style scoped>
.page { display: flex; flex-direction: column; gap: 12px; }
.head { display: flex; align-items: center; gap: 12px; }
.tip { font-size: 12px; color: var(--el-text-color-secondary); }
.mb-12 { margin-bottom: 12px; }
.url-bar {
  display: flex; align-items: center; gap: 8px; padding: 10px 12px;
  background: var(--el-fill-color-light); border-radius: 8px; margin-bottom: 12px; flex-wrap: wrap;
}
.url-bar code { background: var(--el-bg-color); padding: 2px 8px; border-radius: 4px; }
.toolbar { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }
.builtin-row {
  display: flex; align-items: center; gap: 8px; padding: 8px 10px; margin-bottom: 10px;
  border: 1px solid var(--el-color-success-light-7); border-radius: 6px;
  background: var(--el-color-success-light-9); font-size: 12.5px;
}
.ds-detail {
  font-size: 12px; color: var(--el-text-color-secondary); margin-left: 2px;
  display: inline-block; max-width: 260px; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; vertical-align: bottom;
}
/* 集成管理：状态细字 + 摘要计数着色 */
.reg-detail {
  font-size: 11.5px; color: var(--el-text-color-secondary); margin-top: 3px;
  line-height: 1.5; max-width: 220px;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.reg-last { font-size: 12px; color: var(--el-text-color-regular); }
.rs-ok { color: var(--el-color-success); font-weight: 600; }
.rs-warn { color: var(--el-color-warning); font-weight: 600; }
.rs-err { color: var(--el-color-danger); font-weight: 600; }
.card-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 14px;
}
.int-card {
  border: 1px solid var(--el-border-color-lighter); border-radius: 12px;
  padding: 16px 18px; background: var(--el-fill-color-blank);
  transition: box-shadow 0.2s, border-color 0.2s;
}
.int-card:hover {
  box-shadow: 0 6px 18px rgba(59, 130, 246, 0.14);
  border-color: var(--el-color-primary-light-5);
}
.ic-head { display: flex; align-items: center; gap: 12px; }
.ic-tile {
  width: 42px; height: 42px; border-radius: 10px; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center; color: #fff;
}
.ic-blue { background: linear-gradient(135deg, #60a5fa, #3b82f6); }
.ic-green { background: linear-gradient(135deg, #6ee7b7, #10b981); }
.ic-purple { background: linear-gradient(135deg, #c4b5fd, #8b5cf6); }
.ic-cyan { background: linear-gradient(135deg, #67e8f9, #06b6d4); }
.ic-orange { background: linear-gradient(135deg, #fdba74, #f97316); }
.ic-pink { background: linear-gradient(135deg, #f9a8d4, #ec4899); }
.ic-main { flex: 1; min-width: 0; }
.ic-name { font-weight: 700; font-size: 14.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ic-cat { margin-top: 4px; }
.ic-actions { margin-top: 12px; display: flex; gap: 8px; }
.code-box {
  background: var(--el-fill-color-light); padding: 12px 14px; border-radius: 8px;
  font-size: 12px; overflow-x: auto; font-family: 'JetBrains Mono', Consolas, monospace;
}
.guide-code-head { display: flex; align-items: center; gap: 10px; margin: 12px 0 6px; font-size: 13px; }
.sc-tag { margin-right: 4px; }
.mono :deep(textarea) { font-family: 'JetBrains Mono', Consolas, monospace; font-size: 12px; }
.comp-ops { margin-top: 10px; display: flex; gap: 10px; }
.gs-title {
  display: flex; align-items: center; gap: 7px;
  font-size: 13px; font-weight: 700; margin: 16px 0 8px;
}
.gs-title:first-child { margin-top: 0; }
.gs-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--el-color-primary); }
.gs-summary { font-size: 13px; line-height: 1.8; color: var(--el-text-color-regular); }
.gs-steps { display: flex; flex-direction: column; gap: 8px; }
.gs-step { display: flex; gap: 9px; font-size: 12.5px; line-height: 1.7; }
.gs-idx {
  flex-shrink: 0; width: 18px; height: 18px; border-radius: 50%;
  background: var(--el-color-primary-light-7); color: var(--el-color-primary);
  font-size: 11px; font-weight: 700; display: flex; align-items: center; justify-content: center;
  margin-top: 1px;
}
.gs-text { word-break: break-word; }
.gs-url { margin-bottom: 0; }
.gs-notes { margin: 0; padding-left: 16px; font-size: 12px; line-height: 1.8; color: var(--el-text-color-regular); }
.gs-test { display: flex; flex-direction: column; gap: 8px; }
.gs-test-ops { display: flex; gap: 10px; }
.gs-test-result {
  padding: 9px 12px; border-radius: 8px; background: var(--el-fill-color-light);
  display: flex; align-items: center; gap: 8px; font-size: 12.5px; flex-wrap: wrap;
}
</style>
