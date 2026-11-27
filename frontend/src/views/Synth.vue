<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import http from '@/api/http'
import { t } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import { fmtTime } from '@/utils/format'

// 拨测写操作权限：创建/编辑/执行/启停=admin|operator，
// 删除=仅 admin（后端 synth.py 同口径）。此前整页无门禁，viewer 点了全 403。
const auth = useAuthStore()
const canWrite = computed(() => ['admin', 'operator'].includes(auth.role))
const canDelete = computed(() => auth.role === 'admin')

// 合成监控/拨测：HTTP/TCP/DNS 拨测任务管理 + 手动执行 + 结果历史
// HTTP 使用浏览器探测，展示状态码/耗时/证书到期

const checks = ref([])
const typeFilter = ref('')
const loading = ref(false)
const dialogOpen = ref(false)
const resultDrawer = ref(false)
const results = ref([])
const timingRow = ref(null)   // 瀑布弹窗展示的行
// 时序图数据（最近结果的耗时趋势）
const trend = computed(() => {
  const list = (results.value || []).slice(0, 30).slice().reverse()
  if (list.length < 2) return null
  const W = 660, H = 130, pad = 26
  const maxTotal = Math.max(...list.map((r) => r.timing?.total_ms || r.latency_ms || 0), 1)
  const pt = (v, i) => {
    const x = pad + (i / (list.length - 1)) * (W - pad * 2)
    const y = H - 18 - (v / maxTotal) * (H - 34)
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }
  const totalPts = list.map((r, i) => pt(r.timing?.total_ms || r.latency_ms || 0, i)).join(' ')
  // TTFB：只画连续有采样的区间。此前缺失一律按 0 画，浏览器/TCP/DNS 结果或
  // 改过类型的任务会在趋势线上砸出「0ms」假坑。
  const ttfbRuns = []
  let run = []
  list.forEach((r, i) => {
    const v = r.timing?.ttfb_ms
    if (v === undefined || v === null) {
      if (run.length) ttfbRuns.push(run)
      run = []
    } else {
      run.push(pt(v, i))
    }
  })
  if (run.length) ttfbRuns.push(run)
  // 单点画不出线，不画；至少两个点才成段
  const ttfbLines = ttfbRuns.filter((x) => x.length >= 2).map((x) => x.join(' '))
  const hasTtfb = ttfbLines.length > 0
  return { W, H, pad, maxTotal, totalPts, ttfbLines, hasTtfb, n: list.length }
})

const wfVisible = computed({
  get: () => !!timingRow.value,
  set: (v) => { if (!v) timingRow.value = null },
})
// 分段定义：只渲染 timing 里真实存在的键；不能不论拨测类型都硬画
// DNS/TCP/TLS/首字节/传输五段，浏览器拨测没这些采样，
// 图表就把"没测"画成 0ms；TCP/DNS 拨测同样被塞进不存在的 TLS/传输段）。
const SEG_DEFS = {
  dns_ms: { color: '#409eff' },        // HTTP/TCP/DNS
  tcp_ms: { color: '#67c23a' },        // HTTP/TCP
  tls_ms: { color: '#e6a23c' },        // 仅 https
  ttfb_ms: { color: '#9a6fe0' },       // HTTP
  transfer_ms: { color: '#13c2c2' },   // HTTP
  startup_ms: { color: '#f56c6c' },    // 浏览器启动
  setup_ms: { color: '#909399' },      // CDP 通道就绪
  navigate_ms: { color: '#9a6fe0' },   // 导航→渲染就绪
  dom_ms: { color: '#13c2c2' },        // DOM/截图采集
  other_ms: { color: '#c0c4cc' },      // 已测分段之外的时间（不凭空吞掉）
}
const SEG_KEYS = Object.keys(SEG_DEFS).filter((k) => k !== 'other_ms')

// 按当前响应 timing 的真实字段裁出分段；有测量值但不足总耗时，补“其他未归因”
// target 用于 http:// 目标（历史数据里可能存着 tls_ms=0 的占位，不画成 TLS 分段）
function segsOf(timing, target = '') {
  if (!timing) return []
  const plainHttp = String(target || '').startsWith('http://')
  const segs = SEG_KEYS
    .filter((k) => timing[k] !== undefined && timing[k] !== null)
    .filter((k) => !(k === 'tls_ms' && plainHttp))
    .map((k) => ({ key: k, color: SEG_DEFS[k].color, ms: Number(timing[k]) || 0 }))
  const measured = segs.reduce((s, x) => s + x.ms, 0)
  const gap = (Number(timing.total_ms) || 0) - measured
  if (segs.length && gap > 0.5) {
    segs.push({ key: 'other_ms', color: SEG_DEFS.other_ms.color, ms: Math.round(gap * 10) / 10 })
  }
  return segs
}

// 分段全名/短名（渲染时取 i18n；旧实现短名=全名按空格取首段）
const segLabel = (seg) => t(`synth.seg.${seg.key}`)
const segShort = (seg) => t(`synth.seg.${seg.key}.short`)

// 浏览器拨测：净加载=导航+DOM/截图（启动与通道开销单列）
function netLoadMs(timing) {
  const nav = timing?.navigate_ms
  const dom = timing?.dom_ms
  if (nav !== undefined || dom !== undefined) return Math.round(((nav || 0) + (dom || 0)) * 10) / 10
  return Math.max(0, Math.round(((timing?.total_ms || 0) - (timing?.startup_ms || 0)
    - (timing?.setup_ms || 0)) * 10) / 10)
}
function overheadMs(timing) {
  return Math.round((((timing?.startup_ms || 0) + (timing?.setup_ms || 0))) * 10) / 10
}

function shotUrl(fname) {
  return `/api/synth/screenshots/${fname}?token=${localStorage.getItem('qianshu.token') || ''}`
}

// 证书剩余天数：后端把「证书 N 天后到期」拼在 detail 里，
// 但 HTTP 探测有 timing 时只渲染分段条、detail 被整个藏掉——到期风险看不见。
function certDays(detail) {
  const m = /证书\s*(-?\d+)\s*天后到期/.exec(detail || '')
  return m ? Number(m[1]) : null
}

function segPct(timing, seg) {
  const total = timing?.total_ms || 1
  const ms = seg.ms !== undefined ? seg.ms : (timing?.[seg.key] || 0)
  return Math.max(1.5, (ms / total) * 100)
}

// 瀑布图：每段按累计偏移定位（首尾相接=真实时间轴）
function waterfall(timing, target = '') {
  let offset = 0
  const total = timing?.total_ms || 1
  return segsOf(timing, target).map((seg) => {
    const item = {
      ...seg,
      left: (offset / total) * 100,
      width: Math.max(1, (seg.ms / total) * 100),
      pct: total ? (seg.ms / total) * 100 : 0,
    }
    offset += seg.ms
    return item
  })
}

// 与历史均值比对（同任务最近 10 次）
function segDelta(timing, key) {
  const list = (results.value || []).filter((r) => r.timing && r.timing[key] !== undefined).slice(1, 11)
  if (!list.length) return null
  const avg = list.reduce((a, r) => a + (r.timing[key] || 0), 0) / list.length
  const cur = timing[key] || 0
  return avg ? ((cur - avg) / avg) * 100 : 0
}
const current = ref(null)
const runningId = ref(0)

const form = reactive({
  name: '', check_type: 'http', target: '', interval_sec: 60,
  timeout_sec: 10, expect_status: '200-399', cert_warn_days: 30, enabled: true,
  accept_self_signed: false,   // 内网自签证书目标可跳过 TLS 校验
})
const editingId = ref(0)     // 0=新建；否则=编辑该拨测（复用弹窗，PUT 已有）
const trialRun = ref(true)   // 创建后可选立即试拨一次（调既有测试接口）

const shownChecks = computed(() =>
  typeFilter.value ? checks.value.filter((c) => c.check_type === typeFilter.value) : checks.value)

async function load() {
  loading.value = true
  try {
    checks.value = await http.get('/synth/checks')
  } catch { /* 拦截器提示 */ } finally {
    loading.value = false
  }
}

function openCreate() {
  editingId.value = 0
  Object.assign(form, {
    name: '', check_type: 'http', target: '', interval_sec: 60,
    timeout_sec: 10, expect_status: '200-399', cert_warn_days: 30, enabled: true,
    accept_self_signed: false,
  })
  trialRun.value = true
  dialogOpen.value = true
}

// 编辑入口：复用弹窗与既有 PUT，不要求删除重建
function openEdit(row) {
  editingId.value = row.id
  Object.assign(form, {
    name: row.name, check_type: row.check_type, target: row.target,
    interval_sec: row.interval_sec, timeout_sec: row.timeout_sec ?? 10,
    expect_status: row.expect_status, cert_warn_days: row.cert_warn_days,
    enabled: !!row.enabled, accept_self_signed: !!row.accept_self_signed,
  })
  dialogOpen.value = true
}

async function save() {
  if (!form.name || !form.target) return ElMessage.warning(t('synth.msg.name_target_required'))
  try {
    if (editingId.value) {
      await http.put(`/synth/checks/${editingId.value}`, { ...form })
      ElMessage.success(t('synth.msg.saved'))
      dialogOpen.value = false
      await load()
      return
    }
    const created = await http.post('/synth/checks', { ...form })
    ElMessage.success(trialRun.value ? t('synth.msg.created_trial') : t('synth.msg.created_sched'))
    dialogOpen.value = false
    await load()
    // 创建后可选立即试拨一次：调既有"执行一次"接口，当场看到结果
    if (trialRun.value && created?.id) await runOne(created)
  } catch { /* 拦截器提示 */ }
}

async function runOne(row) {
  runningId.value = row.id
  try {
    const r = await http.post(`/synth/checks/${row.id}/run`)
    ElMessage({
      type: r.ok ? 'success' : 'error',
      message: `${t(r.ok ? 'synth.result.pass' : 'synth.result.fail')} · ${r.latency_ms}ms · ${r.detail}`,
      duration: 4000,
    })
    load()
    if (current.value?.id === row.id) openResults(row)
  } finally {
    runningId.value = 0
  }
}

async function toggle(row) {
  await http.put(`/synth/checks/${row.id}`, { ...row, enabled: !row.enabled })
  load()
}

async function remove(row) {
  await ElMessageBox.confirm(t('synth.remove.confirm', { name: row.name }), t('synth.common.confirm_title'), { type: 'warning' })
  await http.delete(`/synth/checks/${row.id}`)
  ElMessage.success(t('synth.msg.deleted'))
  load()
}

async function openResults(row) {
  current.value = row
  results.value = await http.get('/synth/results', { params: { check_id: row.id, limit: 30 } })
  resultDrawer.value = true
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="toolbar">
      <el-button v-if="canWrite" type="primary" @click="openCreate">{{ t('synth.toolbar.create') }}</el-button>
      <el-select v-model="typeFilter" :placeholder="t('synth.toolbar.type_filter')" clearable style="width: 170px">
        <el-option label="HTTP" value="http" />
        <el-option label="TCP" value="tcp" />
        <el-option label="DNS" value="dns" />
        <el-option :label="t('synth.form.browser')" value="browser" />
      </el-select>
      <el-button @click="load">{{ t('synth.toolbar.refresh') }}</el-button>
      <!-- 连败阈值 3 为默认值（backend services/synth.py FAIL_ALERT_THRESHOLD 可配） -->
      <span class="tip">{{ t('synth.toolbar.tip') }}</span>
    </div>

    <el-table :data="shownChecks" v-loading="loading" border stripe :empty-text="t('synth.table.empty')">
      <el-table-column prop="name" :label="t('synth.table.col_name')" min-width="120" />
      <el-table-column :label="t('synth.table.col_type')" width="80">
        <template #default="{ row }">
          <el-tag size="small">{{ row.check_type.toUpperCase() }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="target" :label="t('synth.table.col_target')" min-width="220" show-overflow-tooltip />
      <el-table-column :label="t('synth.table.col_last')" width="120">
        <template #default="{ row }">
          <el-tag v-if="row.last_ok === true" type="success" size="small">{{ t('synth.result.pass') }}</el-tag>
          <el-tag v-else-if="row.last_ok === false" type="danger" size="small">
            {{ t('synth.table.fail_streak', { n: row.consecutive_fail }) }}
          </el-tag>
          <span v-else class="tip">{{ t('synth.table.not_run') }}</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('synth.table.col_latency')" width="90">
        <template #default="{ row }">{{ row.last_latency_ms != null ? row.last_latency_ms + 'ms' : '—' }}</template>
      </el-table-column>
      <el-table-column :label="t('synth.table.col_interval')" width="80">
        <template #default="{ row }">{{ row.interval_sec }}s</template>
      </el-table-column>
      <el-table-column :label="t('synth.table.col_ops')" width="310" fixed="right">
        <template #default="{ row }">
          <el-button v-if="canWrite" size="small" type="primary" text :loading="runningId === row.id" @click="runOne(row)">{{ t('synth.act.run') }}</el-button>
          <el-button size="small" text @click="openResults(row)">{{ t('synth.act.results') }}</el-button>
          <el-button v-if="canWrite" size="small" text @click="openEdit(row)">{{ t('synth.act.edit') }}</el-button>
          <el-button v-if="canWrite" size="small" text @click="toggle(row)">{{ row.enabled ? t('synth.act.disable') : t('synth.act.enable') }}</el-button>
          <el-button v-if="canDelete" size="small" type="danger" text @click="remove(row)">{{ t('synth.act.delete') }}</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dialogOpen" :title="editingId ? t('synth.dialog.edit_title') : t('synth.dialog.create_title')" width="520px">
      <el-form :model="form" label-width="110px">
        <el-form-item :label="t('synth.form.name')"><el-input v-model="form.name" :placeholder="t('synth.form.name_ph')" /></el-form-item>
        <el-form-item :label="t('synth.form.type')">
          <el-radio-group v-model="form.check_type">
            <el-radio-button value="http">HTTP</el-radio-button>
            <el-radio-button value="tcp">TCP</el-radio-button>
            <el-radio-button value="dns">DNS</el-radio-button>
            <el-radio-button value="browser">{{ t('synth.form.browser') }}</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item :label="t('synth.form.target')">
          <el-input v-model="form.target"
            :placeholder="form.check_type === 'http' ? 'https://www.example.com' : (form.check_type === 'tcp' ? 'host:port' : 'example.com')" />
          <!-- 目标格式说明 -->
          <div class="tip" style="width: 100%; line-height: 1.7">
            {{ t('synth.form.target_tip') }}
          </div>
        </el-form-item>
        <el-form-item :label="t('synth.form.interval')"><el-input-number v-model="form.interval_sec" :min="15" :max="86400" /></el-form-item>
        <el-form-item :label="t('synth.form.timeout')">
          <el-input-number v-model="form.timeout_sec" :min="1" :max="60" />
          <span class="tip" style="margin-left: 8px">{{ t('synth.form.timeout_tip') }}</span>
        </el-form-item>
        <el-form-item v-if="form.check_type === 'http'" :label="t('synth.form.expect_status')">
          <el-input v-model="form.expect_status" placeholder="200-399" />
        </el-form-item>
        <el-form-item v-if="form.check_type === 'http'" :label="t('synth.form.cert_warn')">
          <el-input-number v-model="form.cert_warn_days" :min="1" :max="365" />
          <span class="tip" style="margin-left: 8px">{{ t('synth.form.cert_tip') }}</span>
        </el-form-item>
        <el-form-item v-if="form.check_type === 'http' || form.check_type === 'browser'" :label="t('synth.form.self_signed')">
          <el-switch v-model="form.accept_self_signed" />
          <span class="tip" style="margin-left: 8px">{{ t('synth.form.self_signed_tip') }}</span>
        </el-form-item>
        <el-form-item v-if="!editingId" :label="t('synth.form.after_create')">
          <el-checkbox v-model="trialRun">{{ t('synth.form.trial') }}</el-checkbox>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogOpen = false">{{ t('synth.common.cancel') }}</el-button>
        <el-button type="primary" @click="save">{{ editingId ? t('synth.common.save') : t('synth.common.create') }}</el-button>
      </template>
    </el-dialog>

    <el-drawer v-model="resultDrawer" :title="t('synth.drawer.title', { name: current?.name || '' })" size="520px">
      <!-- 耗时趋势（打开即见：总耗时 vs 首字节） -->
      <div v-if="trend" class="trend-box">
        <div class="trend-title">{{ t('synth.trend.title', { n: trend.n }) }}<span class="tip">{{ trend.hasTtfb ? t('synth.trend.legend') : t('synth.trend.legend_single') }}</span></div>
        <svg :width="trend.W" :height="trend.H" class="trend-svg">
          <polyline :points="trend.totalPts" fill="none" stroke="#409eff" stroke-width="2" />
          <polyline v-for="(pts, ti) in trend.ttfbLines" :key="ti" :points="pts" fill="none" stroke="#9a6fe0" stroke-width="2" stroke-dasharray="4 3" />
          <text :x="trend.pad" y="14" class="trend-label" fill="#409eff">{{ t('synth.trend.total', { ms: trend.maxTotal }) }}</text>
        </svg>
      </div>

      <!-- 结果列表：每行内嵌耗时堆叠条（多行直接对比，不用点开） -->
      <el-table :data="results" size="small" border>
        <el-table-column :label="t('synth.res.col_time')" width="140">
          <template #default="{ row }">{{ fmtTime(row.checked_at).slice(5, 19) }}</template>
        </el-table-column>
        <el-table-column :label="t('synth.res.col_result')" width="64">
          <template #default="{ row }">
            <el-tag :type="row.ok ? 'success' : 'danger'" size="small">{{ row.ok ? t('synth.result.pass') : t('synth.result.fail') }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="t('synth.res.col_timing')" min-width="220">
          <template #default="{ row }">
            <!-- 服务端缺 Chrome 时 timing.total_ms=0，旧逻辑会画一条全 0 分段条，
                 把唯一有用的原因（未安装浏览器）藏掉 -->
            <span v-if="row.timing?.need_browser" class="tip">{{ row.detail }}</span>
            <div v-else-if="row.timing && row.timing.screenshot" class="shot-row" @click="timingRow = row">
              <img :src="shotUrl(row.timing.screenshot)" class="shot-thumb" />
              <div class="mini-nums">
                <span class="mini-total">{{ row.timing.total_ms }}ms</span>
                <span class="mini-seg">{{ t('synth.res.title_label', { v: row.timing.title || '—' }) }}</span>
                <span class="mini-seg">{{ t('synth.res.net_load', { ms: netLoadMs(row.timing), startup: overheadMs(row.timing) }) }}</span>
                <span class="mini-seg">{{ t('synth.res.dom', { kb: ((row.timing.dom_bytes || 0) / 1024).toFixed(0) }) }}</span>
              </div>
            </div>
            <div v-else-if="row.timing && row.timing.total_ms !== undefined" class="mini-timing"
                 @click="timingRow = row">
              <div class="mini-bar">
                <i v-for="seg in segsOf(row.timing, current?.target)" :key="seg.key"
                   :style="{ width: segPct(row.timing, seg) + '%', background: seg.color }" />
              </div>
              <div class="mini-nums">
                <span class="mini-total">{{ row.timing.total_ms }}ms</span>
                <span v-for="seg in segsOf(row.timing, current?.target)" :key="seg.key" class="mini-seg"
                      :style="{ color: seg.color }">{{ segShort(seg) }} {{ seg.ms }}</span>
                <span v-if="certDays(row.detail) !== null" class="mini-seg"
                      :class="{ 'cert-warn': certDays(row.detail) < 30 }">
                  {{ t('synth.res.cert_days', { n: certDays(row.detail) }) }}
                </span>
              </div>
            </div>
            <span v-else class="tip">{{ row.detail }}</span>
          </template>
        </el-table-column>
        <el-table-column label="" width="110">
          <template #default="{ row }">
            <el-button v-if="segsOf(row.timing, current?.target).length" size="small" text type="primary" @click="timingRow = row">{{ t('synth.act.waterfall') }}</el-button>
          </template>
        </el-table-column>
      </el-table>

      <!-- 耗时瀑布大弹窗（打开即见，零滚动） -->
      <el-dialog v-model="wfVisible" width="720px" :title="t('synth.wf.title', { time: timingRow?.checked_at ? fmtTime(timingRow.checked_at) : '' })"
                 top="6vh" destroy-on-close>
        <template v-if="timingRow?.timing">
          <!-- 顶部总览条 -->
          <div class="wf-summary">
            <div class="wf-total">{{ timingRow.timing.total_ms }}<small>{{ t('synth.wf.total_suffix') }}</small></div>
            <div class="wf-meta">
              <span>{{ t('synth.wf.status', { code: timingRow.status_code || '—' }) }}</span>
              <span v-if="timingRow.timing.size_bytes">{{ t('synth.wf.transfer', { kb: (timingRow.timing.size_bytes / 1024).toFixed(1) }) }}</span>
              <span>{{ fmtTime(timingRow.checked_at) }}</span>
            </div>
          </div>

          <!-- 没有真实分段时（历史数据/快照降级/失败）明说，不画一排 0ms -->
          <el-alert v-if="timingRow.timing.fallback" type="warning" :closable="false" show-icon
                    :title="t('synth.wf.snapshot_mode', { ms: timingRow.timing.total_ms })"
                    style="margin-bottom: 10px" />
          <el-alert v-else-if="!segsOf(timingRow.timing, current?.target).length" type="info" :closable="false" show-icon
                    :title="t('synth.wf.no_segments', { ms: timingRow.timing.total_ms })"
                    style="margin-bottom: 10px" />

          <!-- 瀑布图（时间轴首尾相接） -->
          <div v-if="segsOf(timingRow.timing, current?.target).length" class="wf-chart">
            <div v-for="seg in waterfall(timingRow.timing, current?.target)" :key="seg.key" class="wf-row">
              <div class="wf-label">{{ segLabel(seg) }}</div>
              <div class="wf-track">
                <div class="wf-bar" :style="{ left: seg.left + '%', width: seg.width + '%', background: seg.color }">
                  <span class="wf-bar-text">{{ seg.ms }}ms</span>
                </div>
              </div>
              <div class="wf-pct">{{ seg.pct.toFixed(1) }}%</div>
              <div class="wf-delta">
                <span v-if="segDelta(timingRow.timing, seg.key) !== null"
                      :class="segDelta(timingRow.timing, seg.key) > 15 ? 'delta-bad' : (segDelta(timingRow.timing, seg.key) < -15 ? 'delta-good' : '')">
                  {{ segDelta(timingRow.timing, seg.key) > 0 ? '+' : '' }}{{ segDelta(timingRow.timing, seg.key)?.toFixed(0) }}%
                </span>
              </div>
            </div>
            <div class="wf-axis">
              <span>0ms</span>
              <span>{{ (timingRow.timing.total_ms / 2).toFixed(0) }}ms</span>
              <span>{{ timingRow.timing.total_ms }}ms</span>
            </div>
          </div>

          <!-- 分段卡片（数字+占比，横向一排） -->
          <div v-if="segsOf(timingRow.timing, current?.target).length" class="wf-cards">
            <div v-for="seg in waterfall(timingRow.timing, current?.target)" :key="seg.key" class="wf-card" :style="{ borderTopColor: seg.color }">
              <div class="wf-card-label">{{ segLabel(seg) }}</div>
              <div class="wf-card-ms">{{ seg.ms }}<small>ms</small></div>
              <div class="wf-card-pct">{{ t('synth.wf.pct', { pct: seg.pct.toFixed(1) }) }}</div>
            </div>
          </div>
          <div class="tip" style="margin-top: 8px">
            {{ t('synth.wf.delta_tip') }}
          </div>
        </template>
      </el-dialog>
    </el-drawer>
  </div>
</template>

<style scoped>
.page { display: flex; flex-direction: column; gap: 12px; }
.toolbar { display: flex; align-items: center; gap: 12px; }
.tip { font-size: 12px; color: var(--el-text-color-secondary); }
.mini-timing { cursor: pointer; }
.shot-row { display: flex; gap: 10px; align-items: center; cursor: pointer; }
.shot-thumb { width: 90px; height: 52px; object-fit: cover; border-radius: 4px; border: 1px solid var(--el-border-color-lighter); }
.mini-bar { display: flex; height: 12px; border-radius: 4px; overflow: hidden; background: var(--el-fill-color-light); }
.mini-bar i { display: block; height: 12px; min-width: 2px; }
.mini-nums { display: flex; gap: 8px; margin-top: 3px; font-size: 10.5px; flex-wrap: wrap; }
.mini-total { font-weight: 700; }
.mini-seg { opacity: 0.85; }
.mini-seg.cert-warn { color: var(--el-color-danger); opacity: 1; font-weight: 600; }
.trend-box { margin-bottom: 12px; border: 1px solid var(--el-border-color-lighter); border-radius: 8px; padding: 8px 12px; }
.trend-title { font-size: 12.5px; font-weight: 600; margin-bottom: 4px; display: flex; gap: 10px; align-items: baseline; }
.trend-svg { display: block; }
.trend-label { font-size: 10px; }
.wf-summary { display: flex; align-items: baseline; gap: 18px; margin-bottom: 14px; }
.wf-total { font-size: 34px; font-weight: 800; }
.wf-total small { font-size: 13px; font-weight: 400; color: var(--el-text-color-secondary); margin-left: 4px; }
.wf-meta { display: flex; gap: 14px; font-size: 12px; color: var(--el-text-color-secondary); }
.wf-chart { border: 1px solid var(--el-border-color-lighter); border-radius: 8px; padding: 12px 14px; }
.wf-row { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.wf-label { width: 86px; font-size: 12px; text-align: right; color: var(--el-text-color-regular); }
.wf-track { flex: 1; height: 26px; background: var(--el-fill-color-light); border-radius: 4px; position: relative; }
.wf-bar { position: absolute; top: 0; height: 26px; border-radius: 4px; min-width: 3px; display: flex; align-items: center; justify-content: center; overflow: hidden; }
.wf-bar-text { color: #fff; font-size: 10.5px; font-weight: 600; white-space: nowrap; padding: 0 3px; }
.wf-pct { width: 46px; font-size: 11px; text-align: right; color: var(--el-text-color-secondary); }
.wf-delta { width: 46px; font-size: 11px; text-align: right; }
.delta-bad { color: #f56c6c; font-weight: 600; }
.delta-good { color: #67c23a; font-weight: 600; }
.wf-axis { display: flex; justify-content: space-between; font-size: 10px; color: var(--el-text-color-secondary); margin-top: 2px; padding-left: 94px; padding-right: 98px; }
.wf-cards { display: flex; gap: 10px; margin-top: 14px; }
.wf-card { flex: 1; border: 1px solid var(--el-border-color-lighter); border-top: 3px solid; border-radius: 8px; padding: 10px 12px; }
.wf-card-label { font-size: 11px; color: var(--el-text-color-secondary); }
.wf-card-ms { font-size: 22px; font-weight: 700; margin: 2px 0; }
.wf-card-ms small { font-size: 11px; font-weight: 400; margin-left: 2px; }
.wf-card-pct { font-size: 11px; color: var(--el-text-color-secondary); }
.timing-title { font-weight: 600; margin-bottom: 10px; display: flex; gap: 10px; align-items: baseline; }
.seg-row { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; }
.seg-label { width: 90px; font-size: 12px; text-align: right; }
.seg-bar-box { flex: 1; background: var(--el-fill-color-light); border-radius: 4px; height: 14px; }
.seg-bar { height: 14px; border-radius: 4px; min-width: 2px; }
.seg-ms { width: 70px; font-size: 12px; text-align: right; font-weight: 600; }
.seg-pct { width: 50px; font-size: 11px; color: var(--el-text-color-secondary); }
</style>
