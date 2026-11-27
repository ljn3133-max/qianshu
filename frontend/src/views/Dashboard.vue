<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useRouter } from 'vue-router'
import {
  Bell, CircleCheck, Clock, Coin, Connection, DataAnalysis, Files,
  Grid, Lock, Monitor, Promotion, Search, Umbrella, Warning,
} from '@element-plus/icons-vue'

import http from '@/api/http'
import { ackAnomalies } from '@/api/cost'
import { assetStats, cloudOverview } from '@/api/assets'
import { dashboardSummary } from '@/api/dashboard'
import { collectStatus, triggerCollect } from '@/api/monitor'
import { t } from '@/i18n'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const auth = useAuthStore()
// 采集是 admin/operator 动作（后端 require_roles），viewer 只看状态
const canCollect = computed(() => ['admin', 'operator'].includes(auth.role))

const ICONS = { Monitor, Coin, Lock, Connection, Files, Promotion, Umbrella, Grid, Search }

// 产品卡片由后端配置下发，适配不同环境的产品集。内置表仅作接口失败时的兜底。
const FALLBACK_CARDS = [
  { key: 'ecs', labelKey: 'dashboard.cards.ecs', icon: 'Monitor', color: '#409eff' },
  { key: 'rds', labelKey: 'dashboard.cards.rds', icon: 'Coin', color: '#67c23a' },
  { key: 'redis', label: 'Redis', icon: 'Lock', color: '#f56c6c' },
  { key: 'slb', labelKey: 'dashboard.cards.slb', icon: 'Connection', color: '#e6a23c' },
  { key: 'oss', labelKey: 'dashboard.cards.oss', icon: 'Files', color: '#9a6fe0' },
  { key: 'cdn', label: 'CDN', icon: 'Promotion', color: '#13c2c2' },
  { key: 'waf', labelKey: 'dashboard.cards.waf', icon: 'Umbrella', color: '#fa8c16' },
  { key: 'ack', labelKey: 'dashboard.cards.ack', icon: 'Grid', color: '#722ed1' },
  { key: 'es', label: 'Elasticsearch', icon: 'Search', color: '#52c1a' },
]
const PRODUCT_CARDS = ref(FALLBACK_CARDS)

const stats = ref({})
// 资产底账·多云口径：按已接入云账号动态生成卡片，未接入的厂商不出现
const cloud = ref(null)
const cloudDetail = ref(null)
const cloudDetailVisible = ref(false)
const loading = ref(false)
const collector = ref(null)
const dash = ref(null)

const VENDOR_COLORS = {
  aliyun: '#ff6a00', tencent: '#006eff', volc: '#1664ff', baidu: '#2932e1',
  huawei: '#c7000b', aws: '#ff9900', azure: '#0078d4',
  vcenter: '#607d8b', esxi: '#78909c', onprem: '#546e7a', kvm: '#8d6e63',
}
const VENDOR_BADGES = {
  aliyun: '阿里', tencent: '腾讯', volc: '火山', baidu: '百度', huawei: '华为',
  aws: 'AWS', azure: 'Azure', vcenter: 'vC', esxi: 'ESXi', onprem: '自建', kvm: 'KVM',
}
const vendorColor = (v) => VENDOR_COLORS[v] || '#409eff'
const vendorBadge = (label, vendor) => VENDOR_BADGES[vendor]
  || (label ? (/[一-龥]/.test(label) ? label.slice(0, 2) : label.slice(0, 2).toUpperCase()) : '?')
const stackPct = (cnt, by) => {
  const total = Object.values(by || {}).reduce((a, b) => a + (Number(b) || 0), 0)
  return total ? Math.max(2, Math.round((Number(cnt) || 0) / total * 100)) : 0
}

// 产品构成色：同一产品跨账号同色，未知产品走稳定哈希色，
// 避免"每个账号一套颜色"导致跨卡不可比。
const PRODUCT_COLORS = {
  ecs: '#3b82f6', rds: '#10b981', redis: '#ef4444', slb: '#f59e0b',
  oss: '#8b5cf6', cdn: '#06b6d4', waf: '#f97316', ack: '#6366f1',
  es: '#14b8a6', mongodb: '#22c55e', kafka: '#eab308', nas: '#a855f7',
  eip: '#0ea5e9', nat: '#64748b', domain: '#ec4899', ssl_cert: '#84cc16',
  vmware_vm: '#64748b', vmware_host: '#475569',
}
const PRODUCT_PALETTE = ['#3b82f6', '#10b981', '#f59e0b', '#8b5cf6', '#06b6d4', '#ef4444', '#6366f1', '#14b8a6']
const productColor = (p) => PRODUCT_COLORS[p]
  || PRODUCT_PALETTE[[...String(p || '')].reduce((a, c) => a + c.charCodeAt(0), 0) % PRODUCT_PALETTE.length]
const pctOf = (p, a) => {
  const total = Number(a?.asset_total) || 0
  return total > 0 ? Math.max(2, Math.round(((Number(p?.total) || 0) / total) * 100)) : 0
}
const pctText = (p, a) => `${pctOf(p, a)}%`
// 非运行=机器/实例停摆（不含证书过期：证书走到期总账，混进来会把"22 项非运行"误读成宕机）
const BAD_STATUS = new Set(['Stopped', 'Deleted', 'locked', 'offline', 'inactive', 'Locked'])
// 风险状态仅用于详情行标注（过期证书该红就红，但不计入"非运行"）
const RISK_STATUS = new Set(['EXPIRED', 'Expired', 'expired', 'Failed', 'Error'])
const isRisk = (st) => BAD_STATUS.has(st) || RISK_STATUS.has(st)
const unhealthyOf = (card) => (card?.products || []).reduce((n, p) =>
  n + Object.entries(p.by_status || {}).reduce((m, [st, c]) => m + (BAD_STATUS.has(st) ? c : 0), 0), 0)
const syncText = (a) => (a?.last_sync_at
  ? t('dashboard.assets.last_sync', { time: a.last_sync_at.replace('T', ' ').slice(5, 16) })
  : t('dashboard.assets.never_sync'))

// 底账页眉汇总：总资产 / 云账号 / 产品类型 / 非运行 / 最近同步
const ledgerTotal = computed(() => cloud.value?.total ?? totalAssets(stats.value))
const ledgerLastSync = computed(() => {
  const list = (cloud.value?.accounts || []).map((a) => a.last_sync_at).filter(Boolean)
  return list.length ? list.sort().slice(-1)[0].replace('T', ' ').slice(5, 16) : ''
})

const cloudProductsCount = computed(() => {
  const set = new Set()
  ;(cloud.value?.accounts || []).forEach((a) => (a.products || []).forEach((p) => set.add(p.product)))
  ;(cloud.value?.unassigned?.products || []).forEach((p) => set.add(p.product))
  return set.size
})
const cloudDisabledCount = computed(
  () => (cloud.value?.accounts || []).filter((a) => a.enabled === false).length)
const cloudUnhealthy = computed(() => {
  const list = []
  ;(cloud.value?.accounts || []).forEach((a) => list.push(...(a.products || [])))
  list.push(...(cloud.value?.unassigned?.products || []))
  return list.reduce((n, p) =>
    n + Object.entries(p.by_status || {}).reduce((m, [st, c]) => m + (BAD_STATUS.has(st) ? c : 0), 0), 0)
})
const detailColor = computed(() => (cloudDetail.value?.vendor
  ? vendorColor(cloudDetail.value.vendor) : '#94a3b8'))

function openCloudCard(card) {
  cloudDetail.value = card
  cloudDetailVisible.value = true
}
function goCloudProduct(product, card) {
  cloudDetailVisible.value = false
  router.push(product === 'host' ? { path: '/machines' } : {
    path: '/ops',
    query: {
      product,
      ...(card?.vendor ? { vendor: card.vendor } : {}),
      ...(card?.id ? { account_id: card.id } : {}),
    },
  })
}

// 数据完整度卡：只读工具体检，分数+Top3 缺口点进跳建议入口
const completeness = ref(null)
const completenessFail = ref(false)
const topGaps = computed(() => (completeness.value?.gaps || []).slice(0, 3))

const totalAssets = (obj) => Object.values(obj).reduce((a, v) => a + (v.total || 0), 0)

const sevTag = { critical: 'danger', warning: 'warning', info: 'info' }
const catTag = { 告警: 'danger', 成本: 'warning', 全景: 'warning', 巡检: 'primary', 到期: 'info' }
const catIcon = { 告警: Bell, 成本: Coin, 全景: DataAnalysis, 巡检: DataAnalysis, 到期: Clock }

const criticalCount = computed(() => blocking.value.filter((t) => t.sev === 'critical').length)

const blocking = computed(() =>
  (dash.value?.todos || []).filter((t) => t.sev === 'critical' || t.sev === 'warning'))

const stateClass = computed(() =>
  dash.value ? (dash.value.ready === false && dash.value.ok ? 'notready' : (dash.value.ok ? 'ok' : 'bad')) : '')

const dashFail = ref(false)   // 总览接口失败：不能把"读取失败"渲染成"环境未就绪"

async function load() {
  loading.value = true
  try {
    const [d, s, c, cl] = await Promise.allSettled(
      [dashboardSummary(), assetStats(), collectStatus(), cloudOverview()])
    dashFail.value = d.status === 'rejected'
    if (d.status === 'fulfilled') dash.value = d.value || null
    if (s.status === 'fulfilled') stats.value = s.value || {}
    if (cl.status === 'fulfilled') cloud.value = cl.value || null
    if (c.status === 'fulfilled') collector.value = c.value || null
    // 完整体检走只读工具通道（POST /ai/tools/execute）；失败不打扰主内容
    try {
      const r = await http.post('/ai/tools/execute', { tool: 'data_completeness_check', params: {} })
      completeness.value = r?.result || null
      completenessFail.value = !r?.result
    } catch (e) {
      completeness.value = null
      completenessFail.value = true
    }
  } finally {
    loading.value = false
  }
}

function go(link) {
  if (link) router.push(link)   // 空落点不推（待办无 link 时点了不该报错）
}

// 待你处理=本页下方待办区就地滚动，数字卡点击后到对应处理位
const todosSection = ref(null)
function scrollToTodos() {
  if (!blocking.value.length) return
  todosSection.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

// 就地收口：
// 成本/全景类待办在卡片上直接确认/忽略，不用再去异动记录页找那条
async function resolveTodo(todo, status) {
  if (!todo.id) return
  try {
    await ackAnomalies({
      ids: [todo.id], status,
      note: status === 'acknowledged' ? t('dashboard.todo.ack_note') : t('dashboard.todo.ignore_note'),
    })
    ElMessage.success(status === 'acknowledged' ? t('dashboard.todo.ack_ok') : t('dashboard.todo.ignore_ok'))
    await load()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('dashboard.todo.op_fail'))
  }
}

// 就地一键诊断（不用先进告警页再点）
function diagFrom(todo) {
  const q = t('dashboard.diag.prompt', {
    title: todo.title,
    detail: todo.detail ? t('dashboard.diag.detail_part', { detail: todo.detail }) : '',
  })
  router.push({ path: '/diagnose', query: { q: q.slice(0, 500) } })
}

// 指标采集卡提供「立即采集」，采集完成即刷新本卡
const collecting = ref(false)
async function runCollect() {
  collecting.value = true
  try {
    const r = await triggerCollect()
    ElMessage.success(t('dashboard.system.collect_ok', {
      points: r.points_written ?? 0,
      err_part: r.errors?.length ? t('monitor.collect.err_part', { n: r.errors.length }) : '',
    }))
    await load()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('monitor.collect.fail'))
  } finally {
    collecting.value = false
  }
}

onMounted(load)

// 产品卡片后端下发（失败静默走兜底表，不打扰用户）
http.get('/settings/product-cards').then(cards => {
  if (Array.isArray(cards) && cards.length) PRODUCT_CARDS.value = cards
}).catch(() => {})
</script>

<template>
  <div class="page" v-loading="loading">
    <el-alert v-if="dashFail" type="error" :closable="false" show-icon
              :title="t('dashboard.load.fail')" style="margin-bottom: 12px">
      <el-button size="small" @click="load">{{ t('common.retry') }}</el-button>
    </el-alert>
    <!-- ============ 今日状态总览（结论 + 关键数字一屏） ============ -->
    <div class="overview" :class="stateClass">
      <div class="ov-state">
        <div class="ov-icon">
          <el-icon v-if="dash && dash.ready === false && dash.ok"><Clock /></el-icon>
          <el-icon v-else-if="dash?.ok"><CircleCheck /></el-icon>
          <el-icon v-else-if="dash"><Warning /></el-icon>
        </div>
        <div>
          <div class="ov-title">
            <template v-if="!dash">{{ t('dashboard.overview.loading') }}</template>
            <template v-else-if="dash.ready === false && dash.ok">{{ t('dashboard.overview.not_ready') }}</template>
            <template v-else-if="dash.ok">{{ t('dashboard.overview.ok') }}</template>
            <template v-else>{{ t('dashboard.overview.todo_count', { n: blocking.length }) }}</template>
          </div>
          <div class="ov-sub">
            <template v-if="dash?.ready === false && dash.ok">
              <span v-for="r in dash.not_ready_reasons" :key="r" style="margin-right: 12px">{{ r }}</span>
            </template>
            <template v-else-if="dash && !dash.ok">
              <template v-if="criticalCount">{{ t('dashboard.overview.critical', { n: criticalCount }) }}</template>
              {{ t('dashboard.overview.blocking', { n: blocking.length - criticalCount }) }}
            </template>
            <template v-else-if="dash">{{ t('dashboard.overview.all_normal') }}</template>
          </div>
        </div>
      </div>
      <div class="ov-metrics">
        <div class="ov-m clickable" :title="t('dashboard.metric.payg_tip')" @click="go('/cost?tab=drill')">
          <el-icon :size="20" color="#67c23a"><Coin /></el-icon>
          <div>
            <div class="ov-num">
              {{ dash?.stats?.payg_yesterday != null ? `¥${Number(dash.stats.payg_yesterday).toLocaleString('zh-CN', { maximumFractionDigits: 1 })}` : '-' }}
              <span v-if="dash?.stats?.payg_change_pct != null" class="ov-delta"
                    :class="{ up: dash.stats.payg_change_pct > 0, down: dash.stats.payg_change_pct < 0 }">
                {{ dash.stats.payg_change_pct > 0 ? '↑' : '↓' }}{{ Math.abs(dash.stats.payg_change_pct) }}%
              </span>
            </div>
            <div class="ov-label">{{ t('dashboard.metric.payg_label') }}</div>
            <!-- 成本停更暴露：昨日按量显示 '-' 时让"为什么"可读 -->
            <div v-if="dash?.stats?.cost_stale_days >= 2" class="ov-label stale-warn"
                 @click.stop="go('/clouds')">
              {{ t('dashboard.metric.payg_stale', { n: dash.stats.cost_stale_days }) }}
            </div>
          </div>
        </div>
        <div class="ov-m clickable" :title="t('dashboard.metric.alerts_tip')" @click="go('/alerts')">
          <el-icon :size="20" color="#f56c6c"><Bell /></el-icon>
          <div>
            <div class="ov-num" :class="{ bad: dash?.stats?.alerts_firing > 0 }">{{ dash?.stats?.alerts_firing ?? '-' }}</div>
            <div class="ov-label">{{ t('dashboard.metric.alerts_label') }}</div>
          </div>
        </div>
        <div class="ov-m clickable" :title="t('dashboard.metric.anomaly_tip')" @click="go('/anomalies')">
          <el-icon :size="20" color="#e6a23c"><DataAnalysis /></el-icon>
          <div>
            <div class="ov-num" :class="{ bad: dash?.stats?.anomalies_open > 0 }">{{ dash?.stats?.anomalies_30d ?? '-' }}</div>
            <div class="ov-label">{{ t('dashboard.metric.anomaly_label') }}<template v-if="dash?.stats?.anomalies_open">{{ t('dashboard.metric.anomaly_open', { n: dash.stats.anomalies_open }) }}</template></div>
          </div>
        </div>
        <div class="ov-m clickable" :title="t('dashboard.metric.todos_tip')" @click="scrollToTodos">
          <el-icon :size="20" color="#e6a23c"><Warning /></el-icon>
          <div>
            <div class="ov-num" :class="{ bad: criticalCount > 0 }">{{ blocking.length }}</div>
            <div class="ov-label">{{ t('dashboard.metric.todos_label') }}</div>
          </div>
        </div>
        <div class="ov-m clickable" :title="t('dashboard.metric.packs_tip')" @click="go('/cost?tab=packs')">
          <el-icon :size="20" color="#e6a23c"><Clock /></el-icon>
          <div>
            <div class="ov-num" :class="{ bad: dash?.stats?.urgent_pack && dash.stats.urgent_pack.days_left <= 30 }">
              {{ dash?.stats?.urgent_pack ? t('dashboard.metric.days_left', { n: dash.stats.urgent_pack.days_left }) : t('dashboard.metric.enough') }}
            </div>
            <div class="ov-label">{{ dash?.stats?.urgent_pack ? t('dashboard.metric.pack_urgent', { label: dash.stats.urgent_pack.label }) : t('dashboard.metric.packs_label') }}</div>
          </div>
        </div>
      </div>
    </div>

    <!-- ============ 要你处理的 ============ -->
    <template v-if="blocking.length">
      <div ref="todosSection" class="sec-title">{{ t('dashboard.todos.title') }}</div>
      <div class="todos">
        <div v-for="(todo, i) in blocking" :key="i" class="todo" :class="`sev-${todo.sev}`">
          <div class="todo-icon" :class="`cat-${todo.cat}`">
            <el-icon :size="18"><component :is="catIcon[todo.cat] || Bell" /></el-icon>
          </div>
          <div class="todo-main">
            <div class="todo-head">
              <el-tag :type="sevTag[todo.sev] || 'info'" size="small" effect="dark">{{ todo.sev }}</el-tag>
              <el-tag :type="catTag[todo.cat] || 'info'" size="small" effect="plain">{{ todo.cat }}</el-tag>
              <el-tag v-for="b in todo.biz" :key="b" type="warning" size="small" effect="dark">{{ b }}</el-tag>
              <el-tag v-if="todo.age_days >= 3" type="warning" size="small" effect="plain">{{ t('dashboard.todos.aged', { n: todo.age_days }) }}</el-tag>
              <el-tag v-else-if="todo.age_days >= 1" type="info" size="small" effect="plain">{{ t('dashboard.todos.age_days', { n: todo.age_days }) }}</el-tag>
            </div>
            <div class="todo-title">{{ todo.title }}<span v-if="todo.money" class="todo-money" :title="t('dashboard.todos.money_tip')">{{ todo.money }}</span></div>
            <div v-if="todo.detail" class="todo-detail">{{ todo.detail }}</div>
            <div class="todo-hint">{{ todo.hint }}</div>
          </div>
          <div class="todo-actions">
            <el-button type="primary" size="small" @click="go(todo.link)">{{ t('dashboard.todos.go') }}</el-button>
            <el-button v-if="todo.cat === '告警'" size="small" @click="diagFrom(todo)">{{ t('dashboard.todos.diagnose') }}</el-button>
            <template v-if="todo.id && (todo.cat === '成本' || todo.cat === '全景')">
              <el-button size="default" type="success" plain @click="resolveTodo(todo, 'acknowledged')">{{ t('dashboard.todos.done') }}</el-button>
              <el-button size="default" type="info" plain @click="resolveTodo(todo, 'ignored')">{{ t('dashboard.todos.false_alarm') }}</el-button>
            </template>
          </div>
        </div>
      </div>
      <div v-if="dash?.overflow?.count" class="overflow-line">
        {{ t('dashboard.todos.overflow', { n: dash.overflow.count }) }}<template v-if="dash.overflow.oldest_days">{{ t('dashboard.todos.overflow_oldest', { n: dash.overflow.oldest_days }) }}</template>——
        {{ t('dashboard.todos.overflow_hint') }}
      </div>
    </template>

    <!-- ============ 资产底账（多云口径：按已接入账号动态生成，未接入不显示） ============ -->
    <div class="ledger">
      <div class="ledger-head">
        <div class="ledger-title-wrap">
          <div class="ledger-title">{{ t('dashboard.assets.title') }}</div>
          <div class="ledger-sub">{{ t('dashboard.assets.hint') }}</div>
        </div>
        <div class="ledger-stats">
          <div class="ls">
            <span class="ls-num">{{ ledgerTotal }}</span>
            <span class="ls-label">{{ t('dashboard.ledger.total') }}</span>
          </div>
          <div class="ls">
            <span class="ls-num">{{ cloud ? cloud.accounts.length : '—' }}</span>
            <span class="ls-label">
              {{ t('dashboard.ledger.accounts') }}
              <span v-if="cloudDisabledCount" class="ls-sub">{{ t('dashboard.ledger.accounts_disabled', { n: cloudDisabledCount }) }}</span>
            </span>
          </div>
          <div class="ls">
            <span class="ls-num">{{ cloudProductsCount }}</span>
            <span class="ls-label">{{ t('dashboard.ledger.kinds') }}</span>
          </div>
          <div class="ls">
            <span class="ls-num" :class="{ bad: (cloud ? cloudUnhealthy : 0) > 0 }">{{ cloud ? cloudUnhealthy : 0 }}</span>
            <span class="ls-label">{{ t('dashboard.ledger.unhealthy') }}</span>
          </div>
          <div v-if="ledgerLastSync" class="ls ls-sync">
            <span class="ls-label">{{ t('dashboard.ledger.updated', { time: ledgerLastSync }) }}</span>
          </div>
        </div>
      </div>

      <div v-if="cloud && (cloud.accounts.length || cloud.unassigned.asset_total)" class="acct-grid">
        <div
          v-for="a in cloud.accounts" :key="a.id" class="acct"
          :class="{ 'acct-disabled': a.enabled === false }"
          :style="{ '--vc': vendorColor(a.vendor) }" @click="openCloudCard(a)"
        >
          <div class="acct-top">
            <div class="acct-avatar">{{ vendorBadge(a.vendor_label, a.vendor) }}</div>
            <div class="acct-id">
              <div class="acct-name" :title="`${a.vendor_label} · ${a.name}`">{{ a.name }}</div>
              <div class="acct-vendor">
                {{ a.vendor_label }}
                <el-tag v-if="a.enabled === false" size="small" type="info" effect="plain">{{ t('dashboard.ledger.disabled') }}</el-tag>
              </div>
            </div>
            <span class="acct-status" :class="'acct-status-' + (a.status || 'untested')">
              <i class="acct-status-dot" />{{ t(`dashboard.cloud.status.${a.status || 'untested'}`) }}
            </span>
          </div>

          <div class="acct-metrics">
            <div class="acct-total">
              <span class="acct-num">{{ a.asset_total }}</span>
              <span class="acct-unit">{{ t('dashboard.ledger.items') }}</span>
            </div>
            <div class="acct-kinds">{{ t('dashboard.ledger.kinds_n', { n: a.products.length }) }}</div>
          </div>

          <div class="acct-bar" :title="t('dashboard.ledger.bar_tip')">
            <span
              v-for="p in a.products" :key="p.product"
              :style="{ width: pctOf(p, a) + '%', background: productColor(p.product) }"
              :title="`${p.label} · ${p.total}`"
            />
            <span v-if="!a.products.length" class="acct-bar-empty" />
          </div>

          <div class="acct-legend">
            <span v-for="p in a.products.slice(0, 4)" :key="p.product" class="legend-item">
              <i :style="{ background: productColor(p.product) }" />{{ p.label }}<b>{{ p.total }}</b>
            </span>
            <span v-if="a.products.length > 4" class="legend-more">+{{ a.products.length - 4 }}</span>
            <span v-if="!a.products.length" class="legend-more">{{ t('dashboard.assets.not_synced') }}</span>
          </div>

          <div class="acct-foot">
            <span class="acct-sync">{{ syncText(a) }}</span>
            <span v-if="unhealthyOf(a)" class="acct-bad">{{ t('dashboard.ledger.bad_n', { n: unhealthyOf(a) }) }}</span>
            <span class="acct-more">{{ t('dashboard.ledger.detail') }} →</span>
          </div>
        </div>

        <div
          v-if="cloud.unassigned.asset_total" class="acct acct-orphan"
          :style="{ '--vc': '#94a3b8' }"
          @click="openCloudCard({ id: 0, name: t('dashboard.assets.unassigned'), vendor: '', vendor_label: t('dashboard.assets.unassigned'), status: '', asset_total: cloud.unassigned.asset_total, products: cloud.unassigned.products, last_sync_at: null })"
        >
          <div class="acct-top">
            <div class="acct-avatar">?</div>
            <div class="acct-id">
              <div class="acct-name">{{ t('dashboard.assets.unassigned') }}</div>
              <div class="acct-vendor">{{ t('dashboard.ledger.orphan_hint') }}</div>
            </div>
          </div>
          <div class="acct-metrics">
            <div class="acct-total">
              <span class="acct-num">{{ cloud.unassigned.asset_total }}</span>
              <span class="acct-unit">{{ t('dashboard.ledger.items') }}</span>
            </div>
            <div class="acct-kinds">{{ t('dashboard.ledger.kinds_n', { n: cloud.unassigned.products.length }) }}</div>
          </div>
          <div class="acct-bar">
            <span
              v-for="p in cloud.unassigned.products" :key="p.product"
              :style="{ width: pctOf(p, { asset_total: cloud.unassigned.asset_total }) + '%', background: productColor(p.product) }"
              :title="`${p.label} · ${p.total}`"
            />
          </div>
          <div class="acct-legend">
            <span v-for="p in cloud.unassigned.products.slice(0, 4)" :key="p.product" class="legend-item">
              <i :style="{ background: productColor(p.product) }" />{{ p.label }}<b>{{ p.total }}</b>
            </span>
            <span v-if="cloud.unassigned.products.length > 4" class="legend-more">+{{ cloud.unassigned.products.length - 4 }}</span>
          </div>
          <div class="acct-foot">
            <span class="acct-sync">{{ t('dashboard.ledger.orphan_foot') }}</span>
            <span class="acct-more">{{ t('dashboard.ledger.detail') }} →</span>
          </div>
        </div>
      </div>
      <el-empty v-else-if="cloud" :description="t('dashboard.assets.empty')" :image-size="60" />
      <!-- 接口异常兜底：保留原产品卡（不让资产区整块消失） -->
      <div v-else class="acct-grid">
        <div
          v-for="c in PRODUCT_CARDS" :key="c.key" class="acct acct-fallback"
          :style="{ '--vc': c.color }" @click="go(`/ops?product=${c.key}`)"
        >
          <div class="acct-top">
            <div class="acct-avatar"><el-icon><component :is="ICONS[c.icon]" /></el-icon></div>
            <div class="acct-id">
              <div class="acct-name">{{ c.labelKey ? t(c.labelKey) : c.label }}</div>
              <div class="acct-vendor">{{ t('dashboard.assets.hint') }}</div>
            </div>
          </div>
          <div class="acct-metrics">
            <div class="acct-total">
              <span class="acct-num">{{ stats[c.key]?.total ?? '-' }}</span>
              <span class="acct-unit">{{ t('dashboard.ledger.items') }}</span>
            </div>
          </div>
          <div class="acct-bar">
            <span
              v-for="(cnt, st) in (stats[c.key]?.by_status || {})" :key="st"
              :style="{ width: stackPct(cnt, stats[c.key].by_status) + '%', background: isRisk(st) ? '#ef4444' : productColor(c.key) }"
              :title="`${st} · ${cnt}`"
            />
          </div>
          <div class="acct-legend">
            <span
              v-for="(cnt, st) in (stats[c.key]?.by_status || {})" :key="st"
              class="legend-item"
            ><i :style="{ background: isRisk(st) ? '#ef4444' : productColor(c.key) }" />{{ st }}<b>{{ cnt }}</b></span>
            <span v-if="!stats[c.key]" class="legend-more">{{ t('dashboard.assets.not_synced') }}</span>
          </div>
        </div>
      </div>
    </div>

    <!-- 账号资产构成详情：点产品行进运维管理（带厂商/账号深链） -->
    <el-dialog
      v-model="cloudDetailVisible" width="640px" class="ledger-dialog"
      :title="t('dashboard.assets.detail_title')"
    >
      <div class="d-hero" :style="{ '--vc': detailColor }">
        <div class="d-avatar">{{ vendorBadge(cloudDetail?.vendor_label, cloudDetail?.vendor) }}</div>
        <div class="d-id">
          <div class="d-name" :title="cloudDetail?.name">{{ cloudDetail?.name }}</div>
          <div class="d-meta">
            <template v-if="cloudDetail?.vendor_label">{{ cloudDetail.vendor_label }}</template>
            <template v-if="cloudDetail?.status"> · {{ t(`dashboard.cloud.status.${cloudDetail.status}`) }}</template>
            <template v-if="cloudDetail?.last_sync_at"> · {{ syncText(cloudDetail) }}</template>
          </div>
        </div>
        <div class="d-total">
          <span>{{ cloudDetail?.asset_total ?? 0 }}</span>
          <small>{{ t('dashboard.ledger.items') }}</small>
        </div>
      </div>

      <div class="d-list">
        <div
          v-for="p in (cloudDetail?.products || [])" :key="p.product" class="d-row"
          @click="goCloudProduct(p.product, cloudDetail)"
        >
          <span class="d-dot" :style="{ background: productColor(p.product) }" />
          <div class="d-main">
            <div class="d-line">
              <b class="d-label">{{ p.label }}</b>
              <span class="d-count">{{ p.total }}</span>
              <span class="d-pct">{{ pctText(p, cloudDetail) }}</span>
            </div>
            <div class="d-bar">
              <span :style="{ width: pctOf(p, cloudDetail) + '%', background: productColor(p.product) }" />
            </div>
            <div class="d-pills">
              <span
                v-for="(cnt, st) in p.by_status" :key="st" class="d-pill"
                :class="{ bad: isRisk(st) }"
              >{{ st }} {{ cnt }}</span>
            </div>
          </div>
          <span class="d-arrow">→</span>
        </div>
        <el-empty
          v-if="!(cloudDetail?.products || []).length"
          :description="t('dashboard.assets.not_synced')" :image-size="50"
        />
      </div>
      <template #footer>
        <span class="detail-hint">{{ t('dashboard.assets.detail_hint') }}</span>
      </template>
    </el-dialog>

    <!-- ============ 系统运行 ============ -->
    <div class="sec-title">{{ t('dashboard.system.title') }}</div>
    <div class="bottom">
      <el-card shadow="never">
        <template #header>
          <div class="collect-head">
            <b>{{ t('dashboard.system.collect_title') }}</b>{{ t('dashboard.system.collect_suffix') }}
            <!-- 「立即采集」与监控仪表盘按钮同 API；仅 admin/operator 可见 -->
            <el-button v-if="canCollect" size="small" type="primary" plain :loading="collecting"
                       @click="runCollect">{{ t('dashboard.system.collect_run') }}</el-button>
          </div>
        </template>
        <template v-if="collector">
          <el-descriptions :column="2" border size="small">
            <el-descriptions-item :label="t('dashboard.system.last_collect')">{{ collector.last_finished_at?.replace('T', ' ').slice(0, 19) || t('dashboard.system.never_run') }}</el-descriptions-item>
            <el-descriptions-item :label="t('dashboard.system.duration')">{{ collector.last_duration_s }}s</el-descriptions-item>
            <el-descriptions-item :label="t('dashboard.system.points_written')">{{ t('dashboard.system.points', { n: collector.points_written }) }}</el-descriptions-item>
            <el-descriptions-item :label="t('dashboard.system.errors')">
              <span :class="{ bad: (collector.errors || []).length > 0 }">
                {{ (collector.errors || []).length || t('dashboard.system.none') }}
              </span>
            </el-descriptions-item>
          </el-descriptions>
          <div v-if="collector.errors?.length" class="err-list">
            <div v-for="e in collector.errors.slice(0, 3)" :key="e" class="err-item">{{ e }}</div>
          </div>
        </template>
        <el-empty v-else :description="collector ? t('dashboard.system.collect_not_running') : t('dashboard.system.collect_loading')" :image-size="60" />
      </el-card>

      <!-- ============ 数据完整度（数据完整性顾问：六面覆盖体检，只读） ============ -->
      <el-card shadow="never">
        <template #header>
          <b>{{ t('dashboard.completeness.title') }}</b>
          <span class="sub">{{ t('dashboard.completeness.hint') }}</span>
        </template>
        <template v-if="completeness">
          <div class="dc-score-row">
            <div class="dc-score" :class="{ bad: completeness.score < 60, warn: completeness.score >= 60 && completeness.score < 85 }">{{ completeness.score }}</div>
            <div class="dc-label">{{ t('dashboard.completeness.score_label') }}</div>
          </div>
          <div class="dc-bar"><div class="dc-bar-fill" :class="{ bad: completeness.score < 60, warn: completeness.score >= 60 && completeness.score < 85 }" :style="{ width: completeness.score + '%' }" /></div>
          <div v-if="completeness.degraded" class="dc-gap-sug">{{ t('dashboard.completeness.degraded', { n: completeness.degraded }) }}</div>
          <template v-if="topGaps.length">
            <div class="dc-gaps-title">{{ t('dashboard.completeness.top_gaps', { n: topGaps.length }) }}</div>
            <div v-for="(g, i) in topGaps" :key="i" class="dc-gap">
              <div class="dc-gap-text">{{ g.gap }}</div>
              <div class="dc-gap-sug">{{ g.suggestion }}</div>
              <el-button size="small" text type="primary" @click="go(g.entry)">{{ t('dashboard.todos.go') }}</el-button>
            </div>
          </template>
          <div v-else class="dc-gap-sug">{{ t('dashboard.completeness.all_good') }}</div>
        </template>
        <el-empty v-else :description="completenessFail ? t('dashboard.completeness.fail') : t('dashboard.completeness.loading')" :image-size="60" />
      </el-card>
    </div>
  </div>
</template>

<style scoped>
.page { padding: 16px; }
.sub { color: var(--el-text-color-secondary); font-size: 12px; margin-left: 8px; }
.sec-title {
  font-weight: 600;
  font-size: 14px;
  margin: 18px 0 10px;
  padding-left: 8px;
  border-left: 3px solid var(--el-color-primary);
}

/* ---- 状态总览 ---- */
.overview {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  padding: 18px 22px;
  border-radius: 10px;
  border: 1px solid var(--el-border-color-lighter);
  background: var(--el-bg-color);
  flex-wrap: wrap;
}
.overview.ok { background: linear-gradient(90deg, var(--el-color-success-light-9), var(--el-bg-color)); border-color: var(--el-color-success-light-5); }
.overview.bad { background: linear-gradient(90deg, var(--el-color-danger-light-9), var(--el-bg-color)); border-color: var(--el-color-danger-light-5); }
.overview.notready { background: linear-gradient(90deg, var(--el-color-warning-light-9), var(--el-bg-color)); border-color: var(--el-color-warning-light-5); }
/* 暗色主题：写死的浅色渐变会糊在深底上看不清——去渐变改纯色淡染（变量随主题自适应） */
html.dark .overview.ok { background: var(--el-color-success-light-9); }
html.dark .overview.bad { background: var(--el-color-danger-light-9); }
html.dark .overview.notready { background: var(--el-color-warning-light-9); }
.ov-state { display: flex; gap: 14px; align-items: center; }
.ov-icon :deep(.el-icon) { font-size: 34px; }
.overview.ok .ov-icon { color: var(--el-color-success); }
.overview.bad .ov-icon { color: var(--el-color-danger); }
.overview.notready .ov-icon { color: var(--el-color-warning); }
.ov-title { font-size: 19px; font-weight: 600; }
.ov-sub { margin-top: 3px; font-size: 12px; color: var(--el-text-color-secondary); }
.ov-metrics { display: flex; gap: 26px; flex-wrap: wrap; }
.ov-m { display: flex; gap: 8px; align-items: center; min-width: 110px; }
.ov-num { font-size: 21px; font-weight: 700; line-height: 1.2; }
.ov-num.bad { color: var(--el-color-danger); }
.ov-label { font-size: 11px; color: var(--el-text-color-secondary); }
/* 账单停更警示：昨日按量显示 '-' 时的"为什么" */
.stale-warn { color: var(--el-color-danger); cursor: pointer; }
.ov-m.clickable { cursor: pointer; border-radius: 8px; padding: 6px 8px; transition: background 0.2s; }
.ov-m.clickable:hover { background: var(--el-fill-color-light); }
.ov-delta { font-size: 13px; margin-left: 4px; }
.ov-delta.up { color: var(--el-color-danger); }
.ov-delta.down { color: var(--el-color-success); }

/* ---- 待办 ---- */
.todos { display: flex; flex-direction: column; gap: 8px; }
.todo {
  display: flex;
  gap: 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-left: 4px solid var(--el-color-info);
  border-radius: 8px;
  padding: 10px 14px;
  background: var(--el-bg-color);
  align-items: center;
}
.todo.sev-critical { border-left-color: var(--el-color-danger); }
.todo.sev-warning { border-left-color: var(--el-color-warning); }
.todo-icon {
  width: 36px; height: 36px; border-radius: 8px; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
}
.todo-icon.cat-告警 { background: var(--el-color-danger-light-9); color: var(--el-color-danger); }
.todo-icon.cat-成本 { background: var(--el-color-warning-light-9); color: var(--el-color-warning); }
.todo-icon.cat-巡检 { background: var(--el-color-primary-light-9); color: var(--el-color-primary); }
.todo-icon.cat-到期 { background: var(--el-color-info-light-9); color: var(--el-color-info); }
.todo-main { flex: 1; min-width: 0; }
.todo-head { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 2px; }
.todo-title { font-weight: 600; font-size: 14px; }
.todo-money { color: var(--el-color-danger); font-weight: 700; margin-left: 10px; }
.todo-detail { font-size: 12px; color: var(--el-text-color-regular); margin-top: 2px; }
.todo-hint { font-size: 12px; color: var(--el-text-color-secondary); margin-top: 2px; }
.todo-actions { display: flex; flex-direction: column; gap: 6px; flex-shrink: 0; }
.overflow-line {
  margin: 8px 0 0; padding: 8px 12px;
  border: 1px dashed var(--el-color-warning); border-radius: 6px;
  font-size: 12px; color: var(--el-text-color-secondary);
}

/* ---- 资产底账：页眉汇总 + 账号卡 + 构成条 ---- */
.ledger {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 16px;
  padding: 18px 20px 20px;
  background: linear-gradient(180deg, var(--el-fill-color-lighter) 0%, var(--el-bg-color) 150px);
}
html.dark .ledger { background: var(--el-bg-color); }
.ledger-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}
.ledger-title-wrap { min-width: 0; }
.ledger-title { font-size: 17px; font-weight: 650; letter-spacing: 0.01em; }
.ledger-sub { margin-top: 3px; font-size: 12px; color: var(--el-text-color-secondary); }
.ledger-stats { display: flex; align-items: center; gap: 22px; flex-wrap: wrap; }
.ls { display: flex; flex-direction: column; align-items: flex-start; gap: 1px; }
.ls-num { font-size: 20px; font-weight: 650; line-height: 1.1; font-variant-numeric: tabular-nums; }
.ls-num.bad { color: var(--el-color-danger); }
.ls-label { font-size: 11px; color: var(--el-text-color-secondary); }
.ls-sub { margin-left: 4px; color: var(--el-color-warning); }
.ls-sync { padding-left: 16px; border-left: 1px solid var(--el-border-color-lighter); }
/* 停用账号仍要在底账里可见（资产是人登记的），但不能和采集中的账号长得一样 */
.acct-disabled { opacity: 0.62; }
.acct-disabled::after {
  content: '';
  position: absolute;
  inset: 0;
  border-radius: inherit;
  border: 1px dashed var(--el-border-color);
  pointer-events: none;
}
.acct-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 14px; }
.acct {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px 16px 14px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 14px;
  background: var(--el-bg-color);
  cursor: pointer;
  overflow: hidden;
  transition: transform 0.18s, box-shadow 0.18s, border-color 0.18s;
}
.acct::before {
  content: '';
  position: absolute;
  inset: 0 0 auto 0;
  height: 3px;
  background: linear-gradient(90deg, var(--vc), transparent 72%);
  opacity: 0.9;
}
.acct:hover {
  transform: translateY(-2px);
  border-color: var(--el-color-primary-light-5);
  box-shadow: 0 10px 28px rgba(15, 23, 42, 0.1);
}
html.dark .acct:hover { box-shadow: 0 10px 28px rgba(0, 0, 0, 0.35); }
/* 云接口异常时的产品卡兜底：虚线边提示这是降级视图，不是账号底账 */
.acct-fallback { border-style: dashed; }
.acct-fallback:hover { border-style: solid; }
.acct-top { display: flex; align-items: center; gap: 10px; min-width: 0; }
.acct-avatar {
  width: 40px;
  height: 40px;
  border-radius: 11px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  font-weight: 650;
  letter-spacing: 0.02em;
  color: var(--vc);
  background: color-mix(in srgb, var(--vc) 12%, transparent);
  border: 1px solid color-mix(in srgb, var(--vc) 24%, transparent);
}
.acct-id { flex: 1; min-width: 0; }
.acct-name {
  font-size: 14px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.acct-vendor {
  margin-top: 1px;
  font-size: 11.5px;
  color: var(--el-text-color-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.acct-status {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 8px;
  border-radius: 999px;
  font-size: 11px;
  flex-shrink: 0;
  background: var(--el-fill-color-light);
  color: var(--el-text-color-secondary);
}
.acct-status-dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.acct-status-ok { color: var(--el-color-success); background: var(--el-color-success-light-9); }
.acct-status-error { color: var(--el-color-danger); background: var(--el-color-danger-light-9); }
.acct-status-probing,
.acct-status-syncing,
.acct-status-seeding { color: var(--el-color-warning); background: var(--el-color-warning-light-9); }
.acct-status-untested,
.acct-status-unsupported { color: var(--el-text-color-secondary); }
.acct-metrics { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
.acct-total { display: flex; align-items: baseline; gap: 6px; }
.acct-num {
  font-size: 30px;
  font-weight: 700;
  line-height: 1;
  letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
}
.acct-unit { font-size: 11.5px; color: var(--el-text-color-secondary); }
.acct-kinds { font-size: 11.5px; color: var(--el-text-color-secondary); }
.acct-bar {
  display: flex;
  gap: 2px;
  height: 7px;
  border-radius: 4px;
  overflow: hidden;
  background: var(--el-fill-color-light);
}
.acct-bar span { height: 100%; border-radius: 2px; transition: width 0.35s; }
.acct-bar-empty { width: 100%; height: 100%; background: var(--el-fill-color); }
.acct-legend { display: flex; flex-wrap: wrap; gap: 6px 12px; min-height: 17px; }
.legend-item {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 11.5px;
  color: var(--el-text-color-regular);
  white-space: nowrap;
}
.legend-item i { width: 7px; height: 7px; border-radius: 2px; display: inline-block; }
.legend-item b { font-weight: 600; color: var(--el-text-color-primary); }
.legend-more { font-size: 11.5px; color: var(--el-text-color-secondary); }
.acct-foot {
  display: flex;
  align-items: center;
  gap: 10px;
  padding-top: 10px;
  border-top: 1px dashed var(--el-border-color-lighter);
  font-size: 11.5px;
  color: var(--el-text-color-secondary);
}
.acct-sync { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.acct-bad { color: var(--el-color-danger); flex-shrink: 0; }
.acct-more {
  margin-left: auto;
  color: var(--el-color-primary);
  flex-shrink: 0;
  opacity: 0.8;
}
.acct:hover .acct-more { opacity: 1; }
.acct-orphan { border-style: dashed; }
.acct-orphan::before { background: linear-gradient(90deg, #94a3b8, transparent 72%); }

/* 资产详情弹窗：账号头图 + 产品占比行（替代原表格） */
.ledger-dialog :deep(.el-dialog__body) { padding-top: 6px; }
.d-hero {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 16px;
  border-radius: 12px;
  border: 1px solid var(--el-border-color-lighter);
  background: linear-gradient(120deg, color-mix(in srgb, var(--vc) 10%, transparent), var(--el-fill-color-lighter));
  margin-bottom: 14px;
}
.d-avatar {
  width: 44px;
  height: 44px;
  border-radius: 12px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  font-weight: 650;
  color: var(--vc);
  background: color-mix(in srgb, var(--vc) 14%, transparent);
  border: 1px solid color-mix(in srgb, var(--vc) 26%, transparent);
}
.d-id { flex: 1; min-width: 0; }
.d-name { font-size: 15px; font-weight: 650; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.d-meta { margin-top: 2px; font-size: 11.5px; color: var(--el-text-color-secondary); }
.d-total { text-align: right; flex-shrink: 0; }
.d-total span { font-size: 26px; font-weight: 700; line-height: 1; font-variant-numeric: tabular-nums; }
.d-total small { display: block; margin-top: 2px; font-size: 11px; color: var(--el-text-color-secondary); }
.d-list { display: flex; flex-direction: column; gap: 8px; max-height: 52vh; overflow-y: auto; }
.d-row {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  padding: 10px 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 10px;
  cursor: pointer;
  transition: border-color 0.18s, background 0.18s;
}
.d-row:hover { border-color: var(--el-color-primary-light-5); background: var(--el-fill-color-lighter); }
.d-dot { width: 9px; height: 9px; border-radius: 3px; margin-top: 4px; flex-shrink: 0; }
.d-main { flex: 1; min-width: 0; }
.d-line { display: flex; align-items: baseline; gap: 8px; }
.d-label { font-size: 13px; }
.d-count { margin-left: auto; font-size: 13px; font-weight: 650; font-variant-numeric: tabular-nums; }
.d-pct { width: 40px; text-align: right; font-size: 11.5px; color: var(--el-text-color-secondary); }
.d-bar { margin-top: 6px; height: 5px; border-radius: 3px; background: var(--el-fill-color); overflow: hidden; }
.d-bar span { display: block; height: 100%; border-radius: 3px; }
.d-pills { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 6px; }
.d-pill {
  font-size: 10.5px;
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--el-fill-color-light);
  color: var(--el-text-color-secondary);
}
.d-pill.bad { background: var(--el-color-danger-light-9); color: var(--el-color-danger); }
.d-arrow { color: var(--el-text-color-placeholder); font-size: 13px; align-self: center; }
.detail-hint { font-size: 12px; color: var(--el-text-color-secondary); }

/* ---- 底部 ---- */
.bottom { margin-top: 16px; display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 12px; }
.collect-head { display: flex; align-items: center; gap: 8px; }
.collect-head .el-button { margin-left: auto; }
.err-list { margin-top: 8px; }
.err-item { font-size: 12px; color: var(--el-color-danger); margin-bottom: 2px; word-break: break-all; }

/* ---- 数据完整度卡 ---- */
.dc-score-row { display: flex; align-items: baseline; gap: 10px; }
.dc-score { font-size: 30px; font-weight: 700; color: var(--el-color-success); line-height: 1.1; }
.dc-score.warn { color: var(--el-color-warning); }
.dc-score.bad { color: var(--el-color-danger); }
.dc-label { font-size: 12px; color: var(--el-text-color-secondary); }
.dc-bar { margin: 8px 0 4px; height: 5px; border-radius: 3px; background: var(--el-fill-color-light); overflow: hidden; }
.dc-bar-fill { height: 100%; border-radius: 3px; background: var(--el-color-success); transition: width 0.3s; }
.dc-bar-fill.warn { background: var(--el-color-warning); }
.dc-bar-fill.bad { background: var(--el-color-danger); }
.dc-gaps-title { margin-top: 10px; font-size: 12px; font-weight: 600; color: var(--el-text-color-secondary); }
.dc-gap {
  margin-top: 8px; padding: 8px 10px;
  border: 1px solid var(--el-border-color-lighter); border-radius: 6px;
  background: var(--el-fill-color-blank);
}
.dc-gap-text { font-size: 13px; font-weight: 500; }
.dc-gap-sug { margin-top: 3px; font-size: 12px; color: var(--el-text-color-secondary); line-height: 1.5; }
</style>
