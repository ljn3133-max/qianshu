<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { QuestionFilled } from '@element-plus/icons-vue'
import { GraphChart } from 'echarts/charts'
import { LegendComponent, TooltipComponent } from 'echarts/components'
import * as echarts from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'

import {
  addIdentifier, businessMap, confirmBusiness, createBusiness, deleteBusiness, deleteIdentifier,
  generateBizDraft, generateDraft, linkStatus, listBusinesses, listEdges, listHosts, setEdgesStatus,
} from '@/api/link'
import CopyBtn from '@/components/CopyBtn.vue'
import { isDark } from '@/composables/theme'
import { t } from '@/i18n'
import { fmtTime } from '@/utils/format'
import { useAuthStore } from '@/stores/auth'

echarts.use([GraphChart, LegendComponent, TooltipComponent, CanvasRenderer])

const auth = useAuthStore()
const canWrite = computed(() => ['admin', 'operator'].includes(auth.role))

/* ---------------- 草稿生成 ---------------- */
const status = ref(null)
const generating = ref(false)

/* 草稿生成结果 -> 提示文案（纯函数，backend/tests/test_link_draft_mock.py 抽本函数源码
   独立跑 node 并断言中文原文）：skipped（SLS 未配置）/ busy（生成中）响应没有 edge_count
   字段——绝不拼出 undefined 文案，说人话 + 可行动指引。
   注意：本函数被后端测试抽取独立执行，不得引用 t/模块；中文兜底文案必须内联且逐字不变，
   应用内展示走返回的 i18n 键由 t() 翻译（en 用键，message 直通=后端下发动态文案不翻译）。 */
function formatDraftResult(r) {
  if (!r) return { type: 'error', i18n: ['topology.draft.no_response', {}], text: '草稿生成失败：无响应' }
  if (r.busy) return r.message ? { type: 'warning', text: r.message } : { type: 'warning', i18n: ['topology.draft.busy_fallback', {}], text: '草稿生成中，请稍候再点「生成 / 刷新草稿」' }
  if (r.skipped) return r.message ? { type: 'warning', text: r.message } : { type: 'warning', i18n: ['topology.draft.skipped_fallback', { reason: r.skipped }], text: `草稿未生成：${r.skipped}` }
  if (r.edge_count == null) return { type: 'error', i18n: ['topology.draft.no_edge_count', {}], text: '草稿生成失败：响应缺边数（请查看后端日志）' }
  let text = `草稿已生成：${r.edge_count} 条边（剔除野域名 ${r.filtered_host_count ?? 0} 个）`
  if (r.cloud_rules_total) {
    text += `；云转发规则标注 ${r.rule_edges ?? 0} 条，仅配置无流量补边 ${r.cloud_only_edges ?? 0} 条`
  }
  return {
    type: 'success',
    i18n: [
      'topology.draft.ok',
      { edges: r.edge_count, hosts: r.filtered_host_count ?? 0, rules: r.rule_edges ?? 0, cloud: r.cloud_only_edges ?? 0 },
    ],
    text,
  }
}

async function loadStatus() {
  status.value = await linkStatus()
}

async function doGenerate() {
  generating.value = true
  try {
    const r = await generateDraft()
    const m = formatDraftResult(r)
    // i18n 键存在=界面自产文案（走翻译）；否则 text=后端 message 直通（动态数据不翻译）
    const text = m.i18n
      ? t(m.i18n[0], m.i18n[1]) + (r?.cloud_rules_total ? t('topology.draft.ok_rules', { rules: r.rule_edges ?? 0, cloud: r.cloud_only_edges ?? 0 }) : '')
      : m.text
    if (m.type === 'success') ElMessage.success(text)
    else if (m.type === 'warning') ElMessage.warning(text)
    else ElMessage.error(text)
    await loadAll()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('topology.msg.draft_failed'))
  } finally {
    generating.value = false
  }
}

/* ---------------- 数据 ---------------- */
const hosts = ref([])
const edges = ref([])
const loading = ref(true)   // 初值 true：进页首帧不能误显示“还没有链路数据”
const activeTab = ref('health')

async function loadAll() {
  loading.value = true
  try {
    const [h, e] = await Promise.all([listHosts({}), listEdges({})])
    hosts.value = h.items
    edges.value = e.items
    await nextTick()
    renderGraph()
  } finally {
    loading.value = false
  }
}

/* ---------------- 域名健康一览 ---------------- */
const LEVEL_TAGS = { critical: 'danger', warning: 'warning', ok: 'success' }
const LEVEL_LABEL_KEYS = { critical: 'topology.link.level.critical', warning: 'topology.link.level.warning', ok: 'topology.link.level.ok' }
const levelLabel = (lv) => (LEVEL_LABEL_KEYS[lv] ? t(LEVEL_LABEL_KEYS[lv]) : lv)
const hostFilter = ref('')

const displayHosts = computed(() => {
  const kw = hostFilter.value.trim().toLowerCase()
  return hosts.value.filter(h => !kw || h.host.toLowerCase().includes(kw))
})

function fmtCount(n) {
  return (n ?? 0).toLocaleString()
}

function fmtPct(v) {
  return (v * 100).toFixed(3) + '%'
}

// RT 无请求或无样本时一律显示「—」，不能把空数据当作 0。
// 云端“仅配置无流量”补边的 rt_avg_ms 是占位 0.0，显示为「0 ms」会误导为响应极快。
function fmtRt(row) {
  if (!row || !row.req_count || !(row.rt_avg_ms > 0)) return '—'
  return `${row.rt_avg_ms} ms`
}

/* ---------------- 详情抽屉（域名 / 后端节点共用） ---------------- */
const drawer = reactive({ visible: false, host: null, backend: null })

const drawerEdges = computed(() => {
  if (drawer.host) return edges.value.filter(e => e.host === drawer.host.host)
  if (drawer.backend) {
    return edges.value.filter(e => (e.ecs_name || e.upstream_addr) === drawer.backend)
  }
  return []
})

function openHost(h) {
  drawer.host = h
  drawer.backend = null
  drawer.visible = true
}

function openBackend(name) {
  drawer.host = null
  drawer.backend = name
  drawer.visible = true
}

const drawerBackendMeta = computed(() => {
  // 后端节点的 CPU/上游信息，从边数据聚合
  if (!drawer.backend) return null
  const es = drawerEdges.value
  const cpu = es.find(e => e.ecs_cpu != null)?.ecs_cpu ?? null
  return { cpu, hosts: [...new Set(es.map(e => e.host))] }
})

/* ---------------- 拓扑图 ---------------- */
const graphEl = ref(null)
let chart = null
const chainFilter = ref('')

const CHAIN_LABEL_KEYS = { outer: 'topology.link.chain.outer', inner: 'topology.link.chain.inner' }
// 链路短标签（外层/内层）
const chainShort = (c) => t(c === 'outer' ? 'topology.link.chain.outer_short' : 'topology.link.chain.inner_short')
const chainLabel = (c) => (CHAIN_LABEL_KEYS[c] ? t(CHAIN_LABEL_KEYS[c]) : c)
const graphHeight = ref(600)

const graphData = computed(() => {
  const es = edges.value.filter(e => !chainFilter.value || e.chain === chainFilter.value)
  const hostAgg = {}
  const beAgg = {}
  const linkAgg = {}
  for (const e of es) {
    const target = e.ecs_name || e.upstream_addr
    hostAgg[e.host] = (hostAgg[e.host] || 0) + e.req_count
    beAgg[target] = (beAgg[target] || 0) + e.req_count
    // 规则边按路径拆分后同 (域名,后端) 会有多行——拓扑图一根线聚合总流量
    // （避免 echarts 画出重叠的多条同向曲线），规则明细看边明细页签
    const k = e.host + '->' + target
    const l = linkAgg[k] || (linkAgg[k] = {
      source: e.host, target, value: 0, c5xx: 0, rtSum: 0, chain: e.chain, rules: [],
    })
    l.value += e.req_count
    l.c5xx += e.cnt_5xx
    l.rtSum += e.rt_avg_ms * (e.req_count || 0)
    const label = e.rule_name || e.path_pattern
    if (label && !l.rules.includes(label)) l.rules.push(label)
  }
  const links = []
  for (const l of Object.values(linkAgg)) {
    // 无请求的边没有 RT 样本，留空而不是 0ms（空数据≠0）
    l.rt = l.value ? Math.round(l.rtSum / l.value * 10) / 10 : null
    links.push(l)
  }
  const hostList = Object.entries(hostAgg).sort((a, b) => b[1] - a[1])
  const beList = Object.entries(beAgg).sort((a, b) => b[1] - a[1])
  return { links, hostList, beList }
})

function nodeSize(req) {
  return Math.round(16 + Math.log10(Math.max(req, 10)) * 5)
}

function edgeWidth(v) {
  return Math.round((1 + Math.log10(Math.max(v, 10)) / 1.8) * 10) / 10
}

function renderGraph() {
  if (!graphEl.value || activeTab.value !== 'graph') return
  if (!chart) {
    chart = echarts.init(graphEl.value)
    chart.on('click', onGraphClick)
  }
  const dark = isDark.value
  const { links, hostList, beList } = graphData.value
  const spacing = 64
  const height = Math.max(hostList.length, beList.length) * spacing + 100
  graphHeight.value = height

  const hostLevel = {}
  for (const h of hosts.value) hostLevel[h.host] = h.level

  const nodes = []
  hostList.forEach(([h, req], i) => {
    const lv = hostLevel[h] || 'ok'
    nodes.push({
      id: h, name: h, x: 170, y: 50 + i * spacing,
      symbolSize: nodeSize(req), category: 0,
      itemStyle: { color: lv === 'ok' ? '#409eff' : lv === 'warning' ? '#e6a23c' : '#f56c6c' },
      label: { position: 'right', distance: 6 },
      req,
    })
  })
  beList.forEach(([b, req], i) => {
    nodes.push({
      id: b, name: b, x: 560, y: 50 + i * spacing,
      symbolSize: nodeSize(req), category: 1,
      itemStyle: { color: '#13c2c2' },
      label: { position: 'left', distance: 6 },
      req,
    })
  })
  const edgeColor = dark ? 'rgba(139,148,158,0.45)' : 'rgba(96,98,100,0.35)'
  const seriesLinks = links.map(l => ({
    source: l.source, target: l.target, value: l.value,
    lineStyle: {
      width: edgeWidth(l.value),
      color: l.c5xx / Math.max(l.value, 1) >= 0.001 ? '#f56c6c' : edgeColor,
      curveness: 0.08,
    },
    c5xx: l.c5xx, rt: l.rt, chain: l.chain, rules: l.rules,
  }))

  chart.resize({ height })
  chart.setOption({
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'item', confine: true,
      textStyle: { fontSize: 12 },
      formatter: (p) => {
        if (p.dataType === 'edge') {
          const d = p.data
          const rate = d.value ? (d.c5xx / d.value * 100).toFixed(3) : '—'
          return `<b>${d.source}</b> → <b>${d.target}</b><br/>` +
            `${t('topology.link.tip.chain', { v: chainLabel(d.chain) })}<br/>` +
            `${t('topology.link.tip.requests', { n: fmtCount(d.value) })}<br/>` +
            `${t('topology.link.tip.c5xx', { n: fmtCount(d.c5xx), rate })}<br/>` +
            `${t('topology.link.tip.rt', { ms: d.rt == null ? '—' : d.rt })}` +
            (d.rules?.length ? `<br/>${t('topology.link.tip.rules', { v: d.rules.slice(0, 4).join('、') })}` : '')
        }
        const d = p.data
        return `<b>${d.name}</b><br/>${t('topology.link.tip.node_req', { n: fmtCount(d.req) })}`
      },
    },
    series: [{
      type: 'graph', layout: 'none', roam: true,
      data: nodes, links: seriesLinks,
      // echarts graph 的 label.show 默认是 false；必须显式打开，
      // 否则只有 hover 才显示节点名称，无法直接识别节点。
      label: { show: true, fontSize: 11, color: dark ? '#c9d1d9' : '#303133' },
      // 有 5xx 的边常显红字，不用 hover 也能一眼看到哪里在报错
      edgeLabel: {
        show: true, fontSize: 10, color: '#f56c6c',
        formatter: (p) => (p.data.c5xx > 0 ? `5xx ${fmtCount(p.data.c5xx)}` : ''),
      },
      edgeSymbol: ['none', 'arrow'], edgeSymbolSize: 7,
      emphasis: { focus: 'adjacency', lineStyle: { width: 6 } },
      z: 3, zlevel: 0,
    }],
  }, true)
}

function onGraphClick(params) {
  if (params.dataType === 'node') {
    const name = params.data.id
    const h = hosts.value.find(x => x.host === name)
    if (h) openHost(h)
    else openBackend(name)
  } else if (params.dataType === 'edge') {
    const d = params.data
    openBackend(d.target)
  }
}

watch([chainFilter, activeTab, () => edges.value.length], () => {
  nextTick(renderGraph)
})

/* ---------------- 边确认 ---------------- */
const edgeFilter = ref('')
const edgeSelection = ref([])

const displayEdges = computed(() => {
  if (!edgeFilter.value) return edges.value
  return edges.value.filter(e => e.status === edgeFilter.value)
})

function onSelChange(rows) {
  edgeSelection.value = rows
}

async function applyEdgeStatus(ids, status) {
  try {
    const r = await setEdgesStatus({ ids, status })
    const action = status === 'confirmed' ? t('topology.link.action_confirmed')
      : status === 'ignored' ? t('topology.link.action_ignored')
        : t('topology.link.action_restored')
    ElMessage.success(t('topology.link.edge_updated', { action, n: r.updated }))
    await Promise.all([loadStatus(), loadAll()])
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('topology.msg.edge_status_failed'))
  }
}

function batchEdgeStatus(status) {
  const ids = edgeSelection.value.map(r => r.id)
  if (!ids.length) {
    ElMessage.warning(t('topology.msg.select_edges'))
    return
  }
  applyEdgeStatus(ids, status)
}

const setOneEdgeStatus = (row, status) => applyEdgeStatus([row.id], status)

/* ---------------- 业务映射（biz_identifiers） ---------------- */
const IDENT_LABEL_KEYS = {
  host: 'topology.link.ident.host', app_name: 'topology.link.ident.app_name', db_name: 'topology.link.ident.db_name',
  ecs_name: 'topology.link.ident.ecs_name', rds_instance: 'topology.link.ident.rds_instance',
  ip: 'topology.link.ident.ip', instance: 'topology.link.ident.instance',
}
const identLabel = (v) => (IDENT_LABEL_KEYS[v] ? t(IDENT_LABEL_KEYS[v]) : v)
// ip/instance 与归属登记共用实体键：IP 去端口、大小写不敏感
const IDENT_PLACEHOLDER_KEYS = { app_name: 'topology.link.ident_ph.app_name', ip: 'topology.link.ident_ph.ip', instance: 'topology.link.ident_ph.instance' }
const biz = reactive({ list: [], loading: false, selected: null, map: null, mapLoading: false })
const bizDialog = reactive({ visible: false, form: { name: '', code: '', description: '' } })
const identDialog = reactive({ visible: false, form: { ident_type: 'host', ident_value: '', note: '' } })

async function loadBusinesses(selectId) {
  biz.loading = true
  try {
    const r = await listBusinesses()
    biz.list = r.items || []
    if (selectId) {
      const found = biz.list.find(b => b.id === selectId)
      if (found) biz.selected = found
    }
    if ((!biz.selected || !biz.list.some(b => b.id === biz.selected.id)) && biz.list.length) {
      biz.selected = biz.list[0]
    }
    if (biz.selected) loadBizMap(biz.selected.id)
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('topology.msg.biz_list_failed'))
  } finally {
    biz.loading = false
  }
}

function selectBiz(b) {
  biz.selected = b
  loadBizMap(b.id)
}

async function loadBizMap(id) {
  biz.mapLoading = true
  try {
    biz.map = await businessMap(id)
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('topology.msg.biz_map_failed'))
  } finally {
    biz.mapLoading = false
  }
}

function openBizDialog() {
  bizDialog.form = { name: '', code: '', description: '' }
  bizDialog.visible = true
}

const bizDraftLoading = ref(false)
const bizDraftResult = ref(null)
const bizConfirmingId = ref(0)

function conflictKind(kind) {
  return t(`topology.link.biz_conflict_kind.${kind}`)
}

function conflictReason(reason) {
  return t(`topology.link.biz_conflict_reason.${reason}`)
}

async function doGenerateBizDraft() {
  bizDraftLoading.value = true
  try {
    const r = await generateBizDraft()
    bizDraftResult.value = r
    if (r.created > 0) {
      ElMessage.success(t('topology.msg.biz_draft_done', { n: r.created, m: r.identifiers }))
    } else {
      ElMessage.info(t('topology.msg.biz_draft_none', { n: r.skipped_existing?.length || 0 }))
    }
    await loadBusinesses()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('topology.msg.biz_draft_failed'))
  } finally {
    bizDraftLoading.value = false
  }
}

async function saveBiz() {
  try {
    const r = await createBusiness(bizDialog.form)
    ElMessage.success(t('topology.msg.biz_created', { name: r.name }))
    bizDialog.visible = false
    await loadBusinesses(r.id)
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('topology.msg.biz_create_failed'))
  }
}

async function confirmBiz(b) {
  try {
    await ElMessageBox.confirm(
      t('topology.link.biz_confirm_text', { name: b.name, n: identCount(b) }),
      t('topology.link.biz_confirm_title'), { type: 'warning' },
    )
  } catch { return }
  bizConfirmingId.value = b.id
  try {
    await confirmBusiness(b.id)
    ElMessage.success(t('topology.msg.biz_confirmed', { name: b.name }))
    await loadBusinesses(b.id)
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('topology.msg.biz_confirm_failed'))
  } finally {
    bizConfirmingId.value = 0
  }
}

async function removeBiz(b) {
  try {
    await ElMessageBox.confirm(t('topology.link.remove_confirm', { name: b.name }), t('topology.link.remove_title'), { type: 'warning' })
  } catch { return }
  try {
    await deleteBusiness(b.id)
    ElMessage.success(t('topology.msg.biz_deleted'))
    biz.selected = null
    biz.map = null
    await loadBusinesses()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('topology.msg.biz_delete_failed'))
  }
}

function openIdentDialog() {
  identDialog.form = { ident_type: 'host', ident_value: '', note: '' }
  identDialog.visible = true
}

async function saveIdent() {
  try {
    await addIdentifier(biz.selected.id, identDialog.form)
    ElMessage.success(t('topology.msg.ident_added'))
    identDialog.visible = false
    await Promise.all([loadBizMap(biz.selected.id), loadBusinesses(biz.selected.id)])
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('topology.msg.ident_add_failed'))
  }
}

async function removeIdent(row) {
  try {
    await deleteIdentifier(biz.selected.id, row.id)
    ElMessage.success(t('topology.msg.ident_removed'))
    await Promise.all([loadBizMap(biz.selected.id), loadBusinesses(biz.selected.id)])
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('topology.msg.ident_remove_failed'))
  }
}

function identCount(b) {
  return Object.values(b.ident_counts || {}).reduce((s, n) => s + n, 0)
}

onMounted(async () => {
  await Promise.all([loadStatus(), loadAll(), loadBusinesses()])
})

onBeforeUnmount(() => {
  if (chart) { chart.dispose(); chart = null }
})
</script>

<template>
  <div class="page">
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <div>
            <span style="font-weight: 600">{{ t('topology.link.title') }}</span>
            <span class="sub">{{ t('topology.link.sub') }}</span>
          </div>
          <el-button v-if="canWrite" type="primary" :loading="generating" @click="doGenerate">
            {{ generating ? t('topology.link.generating') : t('topology.link.gen') }}
          </el-button>
        </div>
      </template>

      <div v-if="status" class="stat-line">
        <el-tag :type="status.running ? 'warning' : 'success'" size="small">
          {{ status.running ? t('topology.link.running') : t('topology.link.idle') }}
        </el-tag>
        <span>
          {{ t('topology.link.counts', { hosts: hosts.length, edges: (status.counts?.draft || 0) + (status.counts?.confirmed || 0) + (status.counts?.ignored || 0) }) }}
          <template v-if="status.counts">{{ t('topology.link.counts_detail', { draft: status.counts.draft || 0, confirmed: status.counts.confirmed || 0, ignored: status.counts.ignored || 0 }) }}</template>
        </span>
        <span v-if="status.last_run_at" class="sub">{{ t('topology.link.last_run', { time: fmtTime(status.last_run_at) }) }}</span>
        <span v-if="status.last_result?.skipped" class="sub" style="color: var(--el-color-warning)">
          {{ status.last_result.message || t('topology.draft.skipped_fallback', { reason: status.last_result.skipped }) }}
        </span>
        <span v-else-if="status.last_result" class="sub">
          {{ t('topology.link.matched', { matched: status.last_result.matched_ecs ?? 0, total: status.last_result.edge_count ?? 0, hosts: status.last_result.filtered_host_count ?? 0 }) }}
          <template v-if="status.last_result.cloud_rules_total">
            {{ t('topology.link.cloud_rules', { n: status.last_result.rule_edges ?? 0 }) }}
          </template>
        </span>
        <el-tooltip v-if="status.last_result?.filtered_hosts_top?.length"
                    :content="t('topology.link.filtered_tip', { list: status.last_result.filtered_hosts_top.slice(0, 5).join('、') })"
                    placement="bottom">
          <el-icon style="color: var(--el-text-color-secondary); cursor: help; font-size: 14px"><QuestionFilled /></el-icon>
        </el-tooltip>
      </div>

      <el-tabs v-model="activeTab">
        <!-- ============ 页签 1：域名健康一览 ============ -->
        <el-tab-pane :label="t('topology.link.tab.health')" name="health">
          <div style="margin-bottom: 10px; display: flex; align-items: center; gap: 10px">
            <el-input v-model="hostFilter" :placeholder="t('topology.link.host_filter_ph')" clearable style="width: 240px" />
            <span class="sub">{{ t('topology.link.health_tip') }}</span>
          </div>
          <el-table stripe v-loading="loading" :data="displayHosts" size="small" border
                    row-key="host" @row-click="openHost" style="cursor: pointer">
            <el-table-column :label="t('topology.link.col_level')" width="70">
              <template #default="{ row }">
                <el-tag :type="LEVEL_TAGS[row.level]" size="small">{{ levelLabel(row.level) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="host" :label="t('topology.link.col_host')" min-width="210" show-overflow-tooltip>
              <template #default="{ row }">
                {{ row.host }}<CopyBtn :text="row.host" />
              </template>
            </el-table-column>
            <el-table-column :label="t('topology.link.col_chain')" width="110">
              <template #default="{ row }">
                <el-tag v-for="c in row.chains" :key="c" size="small" style="margin-right: 4px">
                  {{ chainShort(c) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column :label="t('topology.link.col_req24')" width="110" align="right">
              <template #default="{ row }">{{ fmtCount(row.req_count) }}</template>
            </el-table-column>
            <el-table-column :label="t('topology.link.col_c5xx')" width="130" align="right">
              <template #default="{ row }">
                <span :style="row.cnt_5xx ? 'color: #f56c6c' : ''">
                  {{ fmtCount(row.cnt_5xx) }}（{{ fmtPct(row.rate_5xx) }}）
                </span>
              </template>
            </el-table-column>
            <el-table-column :label="t('topology.link.col_c4xx')" width="80" align="right">
              <template #default="{ row }">{{ fmtCount(row.cnt_4xx) }}</template>
            </el-table-column>
            <el-table-column :label="t('topology.link.col_rt')" width="90" align="right">
              <template #default="{ row }">{{ fmtRt(row) }}</template>
            </el-table-column>
            <el-table-column :label="t('topology.link.col_cpu')" width="100" align="right">
              <template #default="{ row }">
                <span :style="row.ecs_cpu != null && row.ecs_cpu >= 90 ? 'color: #f56c6c' : ''">
                  {{ row.ecs_cpu != null ? row.ecs_cpu + '%' : '-' }}
                </span>
              </template>
            </el-table-column>
            <el-table-column prop="app_name" :label="t('topology.link.col_app')" min-width="140" show-overflow-tooltip>
              <template #default="{ row }">{{ row.app_name || '-' }}</template>
            </el-table-column>
            <el-table-column :label="t('topology.link.col_err')" width="90" align="right">
              <template #default="{ row }">
                <span :style="row.app_error_cnt >= 100000 ? 'color: #e6a23c' : ''">
                  {{ row.app_name ? fmtCount(row.app_error_cnt) : '-' }}
                </span>
              </template>
            </el-table-column>
            <el-table-column :label="t('topology.link.col_backend')" min-width="200" show-overflow-tooltip>
              <template #default="{ row }">{{ row.backends.join('、') }}</template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <!-- ============ 页签 2：拓扑图 ============ -->
        <el-tab-pane :label="t('topology.link.tab.graph')" name="graph">
          <el-alert v-if="!hosts.length && !loading" type="info" :closable="false" style="margin-bottom: 10px"
                    :title="t('topology.link.graph_empty')" />
          <div style="margin-bottom: 10px; display: flex; align-items: center; gap: 12px">
            <el-radio-group v-model="chainFilter" size="small">
              <el-radio-button value="">{{ t('topology.link.chain_all') }}</el-radio-button>
              <el-radio-button value="outer">{{ t('topology.link.chain_outer') }}</el-radio-button>
              <el-radio-button value="inner">{{ t('topology.link.chain_inner') }}</el-radio-button>
            </el-radio-group>
            <span class="sub">
              {{ t('topology.link.graph_legend') }}
            </span>
          </div>
          <div ref="graphEl" :style="{ width: '100%', height: graphHeight + 'px' }" />
        </el-tab-pane>

        <!-- ============ 页签 3：边明细（确认） ============ -->
        <el-tab-pane :label="t('topology.link.tab.edges')" name="edges">
          <div style="margin-bottom: 10px; display: flex; align-items: center; gap: 12px; flex-wrap: wrap">
            <el-radio-group v-model="edgeFilter" size="small">
              <el-radio-button value="">{{ t('topology.link.f_all') }}</el-radio-button>
              <el-radio-button value="draft">{{ t('topology.link.f_draft') }}</el-radio-button>
              <el-radio-button value="confirmed">{{ t('topology.link.f_confirmed') }}</el-radio-button>
              <el-radio-button value="ignored">{{ t('topology.link.f_ignored') }}</el-radio-button>
            </el-radio-group>
            <template v-if="canWrite">
              <el-button size="small" type="success" :disabled="!edgeSelection.length" @click="batchEdgeStatus('confirmed')">
                {{ t('topology.link.batch_confirm', { n: edgeSelection.length }) }}
              </el-button>
              <el-button size="small" type="danger" plain :disabled="!edgeSelection.length" @click="batchEdgeStatus('ignored')">
                {{ t('topology.link.batch_ignore') }}
              </el-button>
            </template>
            <span class="sub">{{ t('topology.link.edges_tip') }}</span>
          </div>
          <el-table stripe v-loading="loading" :data="displayEdges" size="small" border
                    @selection-change="onSelChange" row-key="id">
            <el-table-column v-if="canWrite" type="selection" width="42" reserve-selection />
            <el-table-column type="expand">
              <template #default="{ row }">
                <div class="expand-box">
                  <div class="sub" style="margin-bottom: 6px">
                    {{ t('topology.link.rule_source_line', {
                      src: row.rule_name ? t('topology.link.rule_source_named', { name: row.rule_name })
                        : row.rule_source === 'cloud' ? t('topology.link.rule_source_cloud')
                          : t('topology.link.rule_source_log'),
                      path: row.path_pattern || t('topology.link.path_legacy'),
                    }) }}
                  </div>
                  <div class="sub" style="margin-bottom: 6px">{{ t('topology.link.path_evidence') }}</div>
                  <el-tag v-for="p in row.paths" :key="p.path" size="small" style="margin-right: 8px">
                    {{ p.path }} = {{ fmtCount(p.cnt) }}
                  </el-tag>
                  <span v-if="!row.paths?.length" class="sub">{{ t('topology.link.no_paths') }}</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column :label="t('topology.link.col_chain')" width="70">
              <template #default="{ row }">
                <el-tag size="small">{{ chainShort(row.chain) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="host" :label="t('topology.link.col_host')" min-width="180" show-overflow-tooltip />
            <el-table-column :label="t('topology.link.col_rule_path')" min-width="180" show-overflow-tooltip>
              <template #default="{ row }">
                <el-tag v-if="row.rule_name" size="small" type="warning" style="margin-right: 6px">
                  {{ row.rule_name }}
                </el-tag>
                <span style="font-family: monospace">{{ row.path_pattern || t('topology.link.all_paths') }}</span>
                <el-tag v-if="row.rule_source === 'cloud'" size="small" type="info" style="margin-left: 6px">
                  {{ t('topology.link.config_only') }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column :label="t('topology.link.col_backend')" min-width="140">
              <template #default="{ row }">
                <span style="font-family: monospace">{{ row.upstream_addr }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="ecs_name" :label="t('topology.link.col_ecs')" min-width="130" show-overflow-tooltip>
              <template #default="{ row }">{{ row.ecs_name || t('topology.link.unlinked') }}</template>
            </el-table-column>
            <el-table-column :label="t('topology.link.col_req_short')" width="100" align="right">
              <template #default="{ row }">{{ fmtCount(row.req_count) }}</template>
            </el-table-column>
            <el-table-column :label="t('topology.link.col_c5xx')" width="70" align="right">
              <template #default="{ row }">{{ fmtCount(row.cnt_5xx) }}</template>
            </el-table-column>
            <el-table-column label="RT(ms)" width="80" align="right">
              <template #default="{ row }">{{ fmtRt(row) }}</template>
            </el-table-column>
            <el-table-column :label="t('topology.link.col_status')" width="80">
              <template #default="{ row }">
                <el-tag :type="row.status === 'draft' ? 'info' : row.status === 'confirmed' ? 'success' : 'danger'"
                        size="small">{{ row.status === 'draft' ? t('topology.link.st.draft') : row.status === 'confirmed' ? t('topology.link.st.confirmed') : t('topology.link.st.ignored') }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column v-if="canWrite" :label="t('topology.link.col_ops')" width="130">
              <template #default="{ row }">
                <el-button v-if="row.status === 'draft'" link type="success" size="small" @click.stop="setOneEdgeStatus(row, 'confirmed')">{{ t('topology.link.act.confirm') }}</el-button>
                <el-button v-if="row.status === 'draft'" link type="danger" size="small" @click.stop="setOneEdgeStatus(row, 'ignored')">{{ t('topology.link.act.ignore') }}</el-button>
                <el-button v-if="row.status !== 'draft'" link size="small" @click.stop="setOneEdgeStatus(row, 'draft')">{{ t('topology.link.act.restore') }}</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <!-- ============ 页签 4：业务映射 ============ -->
        <el-tab-pane :label="t('topology.link.tab.biz')" name="biz">
          <el-alert type="info" :closable="false" style="margin-bottom: 12px">
            <template #title>
              {{ t('topology.link.biz_intro') }}
            </template>
            <div class="biz-steps">
              {{ t('topology.link.biz_steps') }}
            </div>
          </el-alert>
          <div v-if="bizDraftResult" class="biz-review" data-testid="biz-draft-review">
            <div class="biz-review-title">
              <strong>{{ t('topology.link.biz_review_title') }}</strong>
              <span class="sub">
                {{ t('topology.link.biz_draft_evidence', {
                  edges: bizDraftResult.evidence?.confirmed_log_edges || 0,
                  assets: bizDraftResult.evidence?.tagged_assets || 0,
                }) }}
              </span>
            </div>
            <template v-if="bizDraftResult.conflicts?.length">
              <div class="biz-review-subtitle">{{ t('topology.link.biz_conflicts_title', { n: bizDraftResult.conflicts.length }) }}</div>
              <el-table :data="bizDraftResult.conflicts" size="small" border max-height="240">
                <el-table-column :label="t('topology.link.col_type')" width="150">
                  <template #default="{ row }">{{ conflictKind(row.kind) }} · {{ identLabel(row.ident_type) }}</template>
                </el-table-column>
                <el-table-column prop="value" :label="t('topology.link.col_value')" min-width="150" show-overflow-tooltip />
                <el-table-column :label="t('topology.link.biz_candidates')" min-width="150" show-overflow-tooltip>
                  <template #default="{ row }">{{ row.candidates.join(' / ') }}</template>
                </el-table-column>
                <el-table-column :label="t('topology.link.biz_unresolved_reason')" min-width="240">
                  <template #default="{ row }">{{ conflictReason(row.reason) }}</template>
                </el-table-column>
              </el-table>
            </template>
            <template v-if="bizDraftResult.unresolved_hosts?.length">
              <div class="biz-review-subtitle">{{ t('topology.link.biz_unresolved_title', { n: bizDraftResult.unresolved_hosts.length }) }}</div>
              <el-table :data="bizDraftResult.unresolved_hosts" size="small" border max-height="200">
                <el-table-column prop="host" :label="t('topology.link.col_host')" min-width="220" show-overflow-tooltip />
                <el-table-column :label="t('topology.link.biz_unresolved_reason')" min-width="300">
                  <template #default="{ row }">{{ t(`topology.link.biz_host_reason.${row.reason}`) }}</template>
                </el-table-column>
              </el-table>
            </template>
            <el-alert v-if="!bizDraftResult.conflicts?.length && !bizDraftResult.unresolved_hosts?.length"
                      type="success" :closable="false" :title="t('topology.link.biz_review_clear')" />
          </div>
          <el-row :gutter="14">
            <el-col :span="7">
              <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px">
                <span style="font-weight: 600">{{ t('topology.link.biz_list_title') }}</span>
                <span>
                  <el-button v-if="canWrite" size="small" type="success" :loading="bizDraftLoading"
                             @click="doGenerateBizDraft">{{ t('topology.link.gen_biz_draft') }}</el-button>
                  <el-button v-if="canWrite" size="small" type="primary" @click="openBizDialog">{{ t('topology.link.act.new') }}</el-button>
                </span>
              </div>
              <el-table stripe v-loading="biz.loading" :data="biz.list" size="small" border
                        highlight-current-row @row-click="selectBiz" style="cursor: pointer">
                <el-table-column prop="name" :label="t('topology.link.col_name')" min-width="100" show-overflow-tooltip />
                <el-table-column prop="code" :label="t('topology.link.col_code')" width="70" />
                <el-table-column :label="t('topology.link.col_status')" width="78">
                  <template #default="{ row }">
                    <el-tag :type="row.status === 'draft' ? 'warning' : 'success'" size="small">
                      {{ row.status === 'draft' ? t('topology.link.st.draft') : t('topology.link.st.confirmed') }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column :label="t('topology.link.col_ident_count')" width="70" align="right">
                  <template #default="{ row }">{{ identCount(row) }}</template>
                </el-table-column>
                <el-table-column v-if="canWrite" :label="t('topology.link.col_ops')" width="124">
                  <template #default="{ row }">
                    <el-button v-if="row.status === 'draft'" link type="success" size="small"
                               :loading="bizConfirmingId === row.id" @click.stop="confirmBiz(row)">
                      {{ t('topology.link.act.confirm') }}
                    </el-button>
                    <el-button link type="danger" size="small" @click.stop="removeBiz(row)">{{ t('topology.link.act.delete') }}</el-button>
                  </template>
                </el-table-column>
              </el-table>
              <div class="sub" style="margin-top: 8px">{{ t('topology.link.biz_multi_tip') }}</div>
            </el-col>
            <el-col :span="17">
              <template v-if="biz.map">
                <el-descriptions :column="3" border size="small" style="margin-bottom: 12px">
                  <el-descriptions-item :label="t('topology.link.d.business')">
                    {{ biz.map.business.name }}
                    <el-tag :type="biz.map.business.status === 'draft' ? 'warning' : 'success'" size="small" style="margin-left: 6px">
                      {{ biz.map.business.status === 'draft' ? t('topology.link.st.draft') : t('topology.link.st.confirmed') }}
                    </el-tag>
                  </el-descriptions-item>
                  <el-descriptions-item :label="t('topology.link.d.chains')">
                    {{ biz.map.chains.map(c => chainShort(c)).join('、') || '-' }}
                  </el-descriptions-item>
                  <el-descriptions-item :label="t('topology.link.d.req24')">{{ fmtCount(biz.map.total_req_24h) }}</el-descriptions-item>
                  <el-descriptions-item :label="t('topology.link.d.desc')" :span="2">{{ biz.map.business.description || '-' }}</el-descriptions-item>
                  <el-descriptions-item :label="t('topology.link.d.edge_status')">
                    {{ t('topology.link.edge_status_counts', { draft: biz.map.edge_status.draft || 0, confirmed: biz.map.edge_status.confirmed || 0, ignored: biz.map.edge_status.ignored || 0 }) }}
                  </el-descriptions-item>
                </el-descriptions>

                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px">
                  <span style="font-weight: 600">{{ t('topology.link.ident_title') }}</span>
                  <el-button v-if="canWrite" size="small" type="primary" @click="openIdentDialog">{{ t('topology.link.attach_ident') }}</el-button>
                </div>
                <el-table stripe :data="biz.map.identifiers" size="small" border style="margin-bottom: 14px">
                  <el-table-column :label="t('topology.link.col_type')" width="100">
                    <template #default="{ row }">
                      <el-tag size="small">{{ identLabel(row.ident_type) }}</el-tag>
                    </template>
                  </el-table-column>
                  <el-table-column prop="ident_value" :label="t('topology.link.col_value')" min-width="180" show-overflow-tooltip />
                  <el-table-column prop="note" :label="t('topology.link.col_note')" min-width="120" show-overflow-tooltip />
                  <el-table-column prop="created_by" :label="t('topology.link.col_operator')" width="90" />
                  <el-table-column v-if="canWrite" label="" width="60">
                    <template #default="{ row }">
                      <el-button link type="danger" size="small" @click="removeIdent(row)">{{ t('topology.link.act.remove') }}</el-button>
                    </template>
                  </el-table-column>
                </el-table>

                <div style="font-weight: 600; margin-bottom: 8px">
                  {{ biz.map.business.status === 'draft' ? t('topology.link.resolved_preview') : t('topology.link.resolved_title') }}
                </div>
                <el-table stripe v-loading="biz.mapLoading" :data="biz.map.domains" size="small" border>
                  <el-table-column prop="host" :label="t('topology.link.col_host')" min-width="180" show-overflow-tooltip />
                  <el-table-column :label="t('topology.link.col_level')" width="70">
                    <template #default="{ row }">
                      <el-tag :type="LEVEL_TAGS[row.level]" size="small">{{ levelLabel(row.level) }}</el-tag>
                    </template>
                  </el-table-column>
                  <el-table-column :label="t('topology.link.col_req24')" width="100" align="right">
                    <template #default="{ row }">{{ fmtCount(row.req_count) }}</template>
                  </el-table-column>
                  <el-table-column :label="t('topology.link.col_c5xx')" width="120" align="right">
                    <template #default="{ row }">{{ fmtCount(row.cnt_5xx) }}（{{ fmtPct(row.rate_5xx) }}）</template>
                  </el-table-column>
                  <el-table-column :label="t('topology.link.col_backend')" min-width="200" show-overflow-tooltip>
                    <template #default="{ row }">{{ row.backends.join('、') }}</template>
                  </el-table-column>
                </el-table>
                <div v-if="biz.map.backends?.length" style="margin-top: 10px">
                  <span class="sub">{{ t('topology.link.backends_label') }}</span>
                  <el-tag v-for="b in biz.map.backends" :key="b" size="small" style="margin: 2px 6px 2px 0">{{ b }}</el-tag>
                  <template v-if="biz.map.apps?.length">
                    <span class="sub" style="margin-left: 10px">{{ t('topology.link.apps_label') }}</span>
                    <el-tag v-for="a in biz.map.apps" :key="a" size="small" type="warning" style="margin: 2px 6px 2px 0">{{ a }}</el-tag>
                  </template>
                </div>
              </template>
              <el-empty v-else-if="!biz.mapLoading && !biz.loading"
                        :description="biz.map ? t('topology.link.biz_loading') : t('topology.link.biz_empty')" />
            </el-col>
          </el-row>
        </el-tab-pane>
      </el-tabs>
    </el-card>

    <!-- ============ 详情抽屉 ============ -->
    <el-drawer v-model="drawer.visible" size="620px" :title="drawer.host ? drawer.host.host : drawer.backend">
      <template v-if="drawer.host">
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item :label="t('topology.link.d.level')">
            <el-tag :type="LEVEL_TAGS[drawer.host.level]" size="small">{{ levelLabel(drawer.host.level) }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item :label="t('topology.link.d.chain')">{{ drawer.host.chains.map(c => chainShort(c)).join('、') }}</el-descriptions-item>
          <el-descriptions-item :label="t('topology.link.d.req24')">{{ fmtCount(drawer.host.req_count) }}</el-descriptions-item>
          <el-descriptions-item :label="t('topology.link.d.c5xx')">{{ fmtCount(drawer.host.cnt_5xx) }}（{{ fmtPct(drawer.host.rate_5xx) }}）</el-descriptions-item>
          <el-descriptions-item :label="t('topology.link.d.c4xx')">{{ fmtCount(drawer.host.cnt_4xx) }}</el-descriptions-item>
          <el-descriptions-item :label="t('topology.link.d.rt')">{{ drawer.host.rt_avg_ms == null ? '—' : drawer.host.rt_avg_ms + ' ms' }}</el-descriptions-item>
          <el-descriptions-item :label="t('topology.link.d.cpu')">{{ drawer.host.ecs_cpu != null ? drawer.host.ecs_cpu + '%' : '-' }}</el-descriptions-item>
          <el-descriptions-item :label="t('topology.link.d.app')">{{ drawer.host.app_name || '-' }}</el-descriptions-item>
          <el-descriptions-item :label="t('topology.link.d.err')">{{ fmtCount(drawer.host.app_error_cnt) }}</el-descriptions-item>
          <el-descriptions-item :label="t('topology.link.d.backend')">{{ drawer.host.backends.join('、') }}</el-descriptions-item>
        </el-descriptions>
        <div v-if="drawer.host.signals?.length" style="margin-top: 10px">
          <el-alert type="warning" :closable="false"
                    :title="t('topology.link.signals', { v: drawer.host.signals.join('；') })" />
        </div>
      </template>
      <template v-else-if="drawerBackendMeta">
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item :label="t('topology.link.d.backend_node')">{{ drawer.backend }}</el-descriptions-item>
          <el-descriptions-item :label="t('topology.link.d.cpu24')">
            {{ drawerBackendMeta.cpu != null ? drawerBackendMeta.cpu + '%' : t('topology.link.d.no_data') }}
          </el-descriptions-item>
          <el-descriptions-item :label="t('topology.link.d.served_hosts')">{{ drawerBackendMeta.hosts.join('、') }}</el-descriptions-item>
        </el-descriptions>
      </template>

      <div style="margin-top: 14px; font-weight: 600; margin-bottom: 8px">{{ t('topology.link.edge_details_title') }}</div>
      <el-table stripe :data="drawerEdges" size="small" border>
        <el-table-column :label="t('topology.link.d.chain')" width="60">
          <template #default="{ row }">{{ chainShort(row.chain) }}</template>
        </el-table-column>
        <el-table-column prop="host" :label="t('topology.link.col_host')" min-width="140" show-overflow-tooltip />
        <el-table-column :label="t('topology.link.col_rule_path2')" min-width="110" show-overflow-tooltip>
          <template #default="{ row }">{{ row.rule_name || row.path_pattern || t('topology.link.all_paths') }}</template>
        </el-table-column>
        <el-table-column prop="upstream_addr" :label="t('topology.link.col_upstream')" min-width="130">
          <template #default="{ row }"><span style="font-family: monospace">{{ row.upstream_addr }}</span></template>
        </el-table-column>
        <el-table-column :label="t('topology.link.col_req_short')" width="90" align="right">
          <template #default="{ row }">{{ fmtCount(row.req_count) }}</template>
        </el-table-column>
        <el-table-column :label="t('topology.link.col_c5xx')" width="60" align="right">
          <template #default="{ row }">{{ fmtCount(row.cnt_5xx) }}</template>
        </el-table-column>
        <el-table-column label="RT(ms)" width="70" align="right">
          <template #default="{ row }">{{ fmtRt(row) }}</template>
        </el-table-column>
      </el-table>
      <div v-for="e in drawerEdges.slice(0, 2)" :key="e.id" style="margin-top: 8px">
        <div class="sub" style="margin-bottom: 4px">{{ t('topology.link.path_prefix', { host: e.host }) }}</div>
        <el-tag v-for="p in e.paths" :key="p.path" size="small" style="margin-right: 6px; margin-bottom: 4px">
          {{ p.path }} = {{ fmtCount(p.cnt) }}
        </el-tag>
      </div>
    </el-drawer>

    <!-- ============ 新建业务线弹窗 ============ -->
    <el-dialog v-model="bizDialog.visible" :title="t('topology.link.biz_dialog_title')" width="460px">
      <el-form label-width="70px">
        <el-form-item :label="t('topology.link.col_name')" required>
          <el-input v-model="bizDialog.form.name" maxlength="128" :placeholder="t('topology.link.biz_name_ph')" />
        </el-form-item>
        <el-form-item :label="t('topology.link.col_code')">
          <el-input v-model="bizDialog.form.code" maxlength="64" :placeholder="t('topology.link.biz_code_ph')" />
        </el-form-item>
        <el-form-item :label="t('topology.link.d.desc')">
          <el-input v-model="bizDialog.form.description" type="textarea" :rows="2" maxlength="500" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="bizDialog.visible = false">{{ t('topology.link.act.cancel') }}</el-button>
        <el-button type="primary" :disabled="!bizDialog.form.name.trim()" @click="saveBiz">{{ t('topology.link.act.create') }}</el-button>
      </template>
    </el-dialog>

    <!-- ============ 挂接标识弹窗 ============ -->
    <el-dialog v-model="identDialog.visible" :title="t('topology.link.ident_dialog_title')" width="480px">
      <el-form label-width="70px">
        <el-form-item :label="t('topology.link.col_type')">
          <el-select v-model="identDialog.form.ident_type" style="width: 200px">
            <el-option v-for="val in Object.keys(IDENT_LABEL_KEYS)" :key="val" :value="val" :label="identLabel(val)" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('topology.link.col_value')">
          <el-select v-if="identDialog.form.ident_type === 'host'"
                     v-model="identDialog.form.ident_value" filterable allow-create default-first-option
                     style="width: 100%" :placeholder="t('topology.link.ident_value_ph')">
            <el-option v-for="h in hosts" :key="h.host" :value="h.host" :label="h.host" />
          </el-select>
          <el-input v-else v-model="identDialog.form.ident_value" maxlength="255"
                    :placeholder="IDENT_PLACEHOLDER_KEYS[identDialog.form.ident_type] ? t(IDENT_PLACEHOLDER_KEYS[identDialog.form.ident_type]) : t('topology.link.ident_default_ph')" />
        </el-form-item>
        <el-form-item :label="t('topology.link.col_note')">
          <el-input v-model="identDialog.form.note" maxlength="500" />
        </el-form-item>
      </el-form>
      <div class="sub" style="margin-bottom: 8px">
        {{ t('topology.link.ident_tip') }}
      </div>
      <template #footer>
        <el-button @click="identDialog.visible = false">{{ t('topology.link.act.cancel') }}</el-button>
        <el-button type="primary" :disabled="!identDialog.form.ident_value.trim()" @click="saveIdent">{{ t('topology.link.act.attach') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.page {
  padding: 16px;
}
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
}
.sub {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  margin-left: 8px;
}
.stat-line {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
  font-size: 13px;
  flex-wrap: wrap;
}
.expand-box {
  padding: 8px 16px;
}
.biz-steps {
  font-size: 13px;
  line-height: 1.7;
  margin-top: 4px;
}
.biz-review {
  margin-bottom: 14px;
  padding: 12px;
  border: 1px solid var(--el-border-color-light);
  border-radius: 4px;
}
.biz-review-title {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.biz-review-subtitle {
  margin: 10px 0 8px;
  font-size: 13px;
  font-weight: 600;
}
</style>
