<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'

import http from '@/api/http'
import LinksDraft from '@/components/LinksDraft.vue'
import { t } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import { assetStatusLabel, fmtTime } from '@/utils/format'

const auth = useAuthStore()
const canWrite = computed(() => ['admin', 'operator'].includes(auth.role))

// 资产拓扑 · 固定坐标画布型架构图：
//   网格背景 + 滚轮缩放 + 拖拽平移 + 组件框拖拽微调（localStorage 持久）+ 正交直角走线
// 数据面：手动/定时扫描 + 新鲜度可见 + 组件变化 diff（"不知道啥时候出现的、准不准"根治）
// 双版共用同一画布密度：明细=带成员 chips；汇报=组件名+数量+脱敏导出

const route = useRoute()
const router = useRouter()
const topTab = ref(route.query.tab === 'links' ? 'links' : 'arch')   // arch 资产架构图 | links 链路确认
const mode = ref('detail')          // detail | report
const theme = ref('tech')           // tech | biz
const redact = ref(true)
const search = ref('')
// 数据源分版：汇总/阿里云/私有云/腾讯云/未归属；非法值回退汇总并提示
const source = ref(String(route.query.source || 'all').trim().toLowerCase() || 'all')
const loading = ref(false)
const scanning = ref(false)
const arch = ref(null)
const loadError = ref('')
let archSeq = 0
const detail = ref(null)
const drawerOpen = ref(false)

// 架构角色枚举 → 中文，详情抽屉不直接显示 app/db/third_party
const ROLE_LABEL_KEYS = {
  app: 'topology.role.app', gateway: 'topology.role.gateway',
  ingress: 'topology.role.ingress', db: 'topology.role.db',
  middleware: 'topology.role.middleware', bigdata: 'topology.role.bigdata',
  third_party: 'topology.role.third_party',
}
function roleLabel(r) {
  return ROLE_LABEL_KEYS[r] ? t(ROLE_LABEL_KEYS[r]) : (r || '—')
}
const historyVisible = ref(false)
const scans = ref([])

// ---------- 画布视口 ----------
const pan = reactive({ x: 40, y: 20 })
const zoom = ref(1)
const MIN_Z = 0.35
const MAX_Z = 2.2

// ---------- 框位置微调（用户拖出来的布局持久化） ----------
// v2 聚合键包含 source+account；v1 坐标不兼容，读取和重置时清理旧键
const POS_KEY = 'qianshu.topology.pos.v2'
const OLD_POS_KEY = 'qianshu.topology.pos'
// localStorage 脏数据（非法 JSON）不能让整个页面在 setup 阶段白屏
function readPosOverride() {
  try {
    localStorage.removeItem(OLD_POS_KEY)
    const v = JSON.parse(localStorage.getItem(POS_KEY) || '{}')
    return v && typeof v === 'object' ? v : {}
  } catch {
    localStorage.removeItem(POS_KEY)
    return {}
  }
}
const posOverride = ref(readPosOverride())

const C = {
  tech: {
    bg: '#0b1626', grid: 'rgba(127,211,255,0.10)', band: 'rgba(127,211,255,0.045)',
    bandBorder: 'rgba(127,211,255,0.22)', bandText: '#7fd3ff',
    box: 'rgba(18,34,56,0.95)', boxBorder: 'rgba(127,211,255,0.5)',
    title: '#eaf3fb', text: '#93b0c9', chip: 'rgba(127,211,255,0.13)', chipText: '#b6d2e8',
    line: 'rgba(127,211,255,0.55)', accent: '#4fd1ff',
  },
  biz: {
    bg: '#f4f7fb', grid: 'rgba(64,158,255,0.12)', band: 'rgba(64,158,255,0.04)',
    bandBorder: 'rgba(64,158,255,0.22)', bandText: '#337ecc',
    box: '#ffffff', boxBorder: '#b7c9dc',
    title: '#2c3e50', text: '#5a6b7c', chip: '#edf2f8', chipText: '#4a5c6c',
    line: 'rgba(64,158,255,0.5)', accent: '#409eff',
  },
}

const ROLE_COLORS = {
  ingress: '#409eff', gateway: '#e6a23c', app: '#67c23a', bigdata: '#13c2c2',
  middleware: '#9a6fe0', db: '#f56c6c', third_party: '#909399',
}

// 多云/混合云：分组可按「云账号」切层（阿里云·账号A / vCenter / ESXi 各自区块），
// 组件框带厂商/账号标签 + 图例——混合云同屏。数据源=资产行 account_id → 云账号台账（后端已带标签）。
// 分组方式使用平实表述：'role'=按架构层级 | 'vendor'=按云账号
const laneMode = ref('role')
const VENDOR_COLORS = {
  aliyun: '#ff7a45', tencent: '#1890ff', volc: '#722ed1', baidu: '#13c2c2',
  huawei: '#f5222d', aws: '#fa8c16', azure: '#2f54eb',
  esxi: '#52c41a', vcenter: '#a0d911', onprem: '#8c8c8c',
}
const UNASSIGNED_COLOR = '#909399'
function vendorColor(v) {
  return VENDOR_COLORS[v] || UNASSIGNED_COLOR
}

const BOX_W = 300
const GAP_X = 40
const GAP_Y = 34
const BAND_PAD_TOP = 52
const PER_ROW = 4
const SECTION_PAD_TOP = 46
const SECTION_GAP = 16
const WIDTH = 36 + PER_ROW * (BOX_W + GAP_X) + 24

const SOURCE_LABEL_KEYS = {
  all: 'topology.source.all', private: 'topology.source.private',
  unassigned: 'topology.source.unassigned', aliyun: 'topology.source.aliyun',
  tencent: 'topology.source.tencent',
}
function sourceLabelOf(key) {
  if (SOURCE_LABEL_KEYS[key]) return t(SOURCE_LABEL_KEYS[key])
  return (arch.value?.sources || []).find((s) => s.key === key)?.label || key
}
const sourceTabs = computed(() => {
  const list = arch.value?.sources || []
  if (!list.length) return [{ key: 'all', label: t('topology.source.all'), count: 0 }]
  return list.filter((s) => s.key === 'all' || s.count > 0 || s.key === source.value)
})
const sourceSuffix = computed(() =>
  source.value === 'all' ? '' : '-' + sourceLabelOf(source.value))

function syncSourceQuery() {
  const q = { ...route.query }
  if (source.value === 'all') delete q.source
  else q.source = source.value
  router.replace({ query: q })
}
function setSource(key) {
  if (!key || key === source.value) return
  source.value = key
  laneMode.value = key === 'private' ? 'region' : 'role'   // 私有云按集群/地域看更直观
  syncSourceQuery()
  load()
}

async function load() {
  const seq = ++archSeq
  loading.value = true
  loadError.value = ''
  try {
    const r = mode.value === 'report' && redact.value ? 1 : 0
    const data = await http.get('/topology/arch', { params: { redact: r, source: source.value } })
    if (seq === archSeq) arch.value = data
  } catch (e) {
    if (seq === archSeq && e?.response?.status === 400 && source.value !== 'all') {
      // 陈旧/非法深链：不画空图被误读成"没有资产"，明确提示并回汇总
      source.value = 'all'
      syncSourceQuery()
      ElMessage.warning(t('topology.msg.bad_source'))
      loading.value = false
      return load()
    }
    if (seq === archSeq) {
      loadError.value = e?.response?.data?.detail || t('topology.msg.load_failed')
      ElMessage.error(loadError.value)
    }
  } finally {
    if (seq === archSeq) loading.value = false
  }
}

async function loadScans() {
  try {
    scans.value = await http.get('/topology/scans', { params: { limit: 15 } })
  } catch { /* 静默 */ }
}

async function onScan() {
  // 异步扫描：资产同步 1-2 分钟，POST 立即返回，2 秒轮询状态
  scanning.value = true
  try {
    const r = await http.post('/topology/scan')
    if (r.started === false) {
      ElMessage.warning(r.message || t('topology.msg.scan_running'))
      scanning.value = false
      return
    }
    ElMessage.info(r.message || t('topology.msg.scan_started'))
    const timer = setInterval(async () => {
      try {
        const st = await http.get('/topology/scan/status')
        if (!st.running && st.last_result) {
          clearInterval(timer)
          scanning.value = false
          const res = st.last_result
          if (res.error) {
            ElMessage.error(t('topology.msg.scan_error', { err: res.error }))
            return
          }
          const d = res.diff || {}
          const parts = []
          if (d.added?.length) parts.push(t('topology.diff.added', { n: d.added.length }))
          if (d.removed?.length) parts.push(t('topology.diff.removed', { n: d.removed.length }))
          if (d.grown?.length) parts.push(t('topology.diff.grown', { n: d.grown.length }))
          if (d.shrunk?.length) parts.push(t('topology.diff.shrunk', { n: d.shrunk.length }))
          ElMessage.success(t('topology.scan.done', {
            ms: res.duration_ms,
            diff: parts.length ? parts.join(t('topology.list_sep')) : t('topology.scan.no_change'),
          }))
          await load()
          loadScans()
        }
      } catch {
        clearInterval(timer)
        scanning.value = false
      }
    }, 2000)
    // 兜底：10 分钟未完成自动放弃轮询
    setTimeout(() => { clearInterval(timer); scanning.value = false }, 600000)
  } catch {
    ElMessage.error(t('topology.msg.scan_failed_start'))
    scanning.value = false
  }
}

function boxHeight(grp) {
  if (mode.value === 'report') return 84
  const chips = Math.min(grp.count, 12)
  const rows = Math.ceil(chips / 2)
  return 72 + rows * 28 + (grp.count > 12 ? 24 : 0)
}

function visibleGroups() {
  const g = arch.value
  if (!g) return []
  return g.groups   // 画布型：全量画（架构图里没出现的=不存在），搜索负责聚焦
}

// 按云账号分组：每账号一个分组容器；同组件跨账号=各容器一框（成员按账号过滤）。
// 数据源分版后，容器在单云视图=该云内账号；汇总版则由云容器（sections）先包一层。
function vendorBandDefs(grps) {
  const lanes = new Map()
  const laneOf = (key, label, color) => {
    if (!lanes.has(key)) lanes.set(key, { key, label, color, grps: [] })
    return lanes.get(key)
  }
  const accById = new Map((arch.value?.accounts || []).map((a) => [a.id, a]))
  for (const grp of grps) {
    if (grp.external || !grp.members?.length) {
      // 外部占位框没有成员，不能按成员拆账号；单列「外部依赖」泳道，防丢框
      laneOf('ext', t('topology.legend.external'), UNASSIGNED_COLOR).grps.push({
        ...grp, id: `${grp.id}@@ext`, baseId: grp.id, laneKey: 'ext',
      })
      continue
    }
    const byLane = new Map()
    for (const m of grp.members) {
      const key = (m.meta || {}).account_id ? `a${m.meta.account_id}` : 'none'
      if (!byLane.has(key)) byLane.set(key, [])
      byLane.get(key).push(m)
    }
    for (const [key, members] of byLane) {
      let lane
      if (key === 'none') {
        lane = laneOf('none', t('topology.legend.unassigned'), UNASSIGNED_COLOR)
      } else {
        const acc = accById.get(Number(key.slice(1)))
        lane = laneOf(key, acc ? `${acc.vendor_label || acc.vendor} · ${acc.name}` : key,
                      acc ? vendorColor(acc.vendor) : UNASSIGNED_COLOR)
      }
      lane.grps.push({
        ...grp,
        id: `${grp.id}@@${key}`,   // 拆分后唯一定位（拖拽/连线）
        baseId: grp.id,
        laneKey: key,
        members,
        count: members.length,
      })
    }
  }
  return [...lanes.values()].sort((a, b) => {
    const un = (x) => (x.key === 'none' || x.key === 'ext' ? 1 : 0)
    return un(a) - un(b) || a.label.localeCompare(b.label)
  })
}

// 按地域/集群分组（私有云更直观：region → 集群 → 数据中心回退，后端已算好 container）
function regionBandDefs(grps) {
  const lanes = new Map()
  const un = t('topology.region.unspecified')
  for (const grp of grps) {
    const label = grp.container || un
    const key = 'r:' + label
    if (!lanes.has(key)) lanes.set(key, { key, label, color: '', grps: [] })
    lanes.get(key).grps.push(grp)
  }
  return [...lanes.values()].sort((a, b) =>
    (a.label === un ? 1 : 0) - (b.label === un ? 1 : 0) || a.label.localeCompare(b.label))
}

function bandDefsFor(grps) {
  if (laneMode.value === 'vendor') return vendorBandDefs(grps)
  if (laneMode.value === 'region') return regionBandDefs(grps)
  return (arch.value?.roles || []).map((role) => ({
    key: role.key, label: role.label, color: '',   // 架构层级分组保持主题色（不改既有视觉）
    grps: grps.filter((x) => x.role === role.key),
  }))
}

const layout = computed(() => {
  const g = arch.value
  if (!g) return { sections: [], bands: [], boxes: [], width: WIDTH, height: 900 }
  const boxes = []
  const bands = []
  const sections = []
  const srcOrder = (g.sources || []).map((s) => s.key)
  const rank = (k) => { const i = srcOrder.indexOf(k); return i < 0 ? 99 : i }
  const bySrc = new Map()
  for (const grp of visibleGroups()) {
    const k = grp.source || 'unassigned'
    if (!bySrc.has(k)) bySrc.set(k, [])
    bySrc.get(k).push(grp)
  }
  // 汇总版：外层按数据源分段（云容器），段内保留完整架构层级；单云版不渲染段框
  const multi = source.value === 'all' && bySrc.size > 1
  let y = 24
  for (const srcKey of [...bySrc.keys()].sort((a, b) => rank(a) - rank(b))) {
    const grps = bySrc.get(srcKey)
    const secTop = y
    if (multi) y += SECTION_PAD_TOP
    for (const band of bandDefsFor(grps)) {
      const bgrps = band.grps
      if (!bgrps.length) continue
      const rows = Math.ceil(bgrps.length / PER_ROW)
      const rowHeights = []
      for (let r = 0; r < rows; r++) {
        rowHeights.push(Math.max(...bgrps.slice(r * PER_ROW, r * PER_ROW + PER_ROW).map(boxHeight)))
      }
      const bandH = BAND_PAD_TOP + rowHeights.reduce((a, b) => a + b, 0) + (rows - 1) * GAP_Y + 22
      bands.push({
        key: `${srcKey}:${band.key}`, laneKey: band.key, sectionKey: srcKey,
        label: band.label, y, height: bandH, count: bgrps.length, color: band.color, multi,
      })
      bgrps.forEach((grp, i) => {
        const row = Math.floor(i / PER_ROW)
        const col = i % PER_ROW
        const top = y + BAND_PAD_TOP + rowHeights.slice(0, row).reduce((a, b) => a + b + GAP_Y, 0)
        const h = boxHeight(grp)
        const baseX = (multi ? 60 : 36) + col * (BOX_W + GAP_X)
        const o = posOverride.value[grp.id]
        boxes.push({
          ...grp,
          sectionKey: srcKey,
          laneKey: band.key,
          bandColor: band.color,
          x: o ? o.x : baseX,
          y: o ? o.y : top,
          baseX, baseY: top,
          w: BOX_W,
          h,
        })
      })
      y += bandH + 18
    }
    if (multi) {
      const meta = (g.sources || []).find((s) => s.key === srcKey) || {}
      sections.push({
        key: srcKey, y: secTop, height: y - secTop + 6,
        label: sourceLabelOf(srcKey), color: vendorColor(srcKey),
        accounts: (meta.accounts || []).length, regions: (meta.regions || []).length,
        groups: grps.length,
      })
      y += SECTION_GAP
    }
  }
  return { sections, bands, boxes, width: WIDTH, height: y + 40 }
})

// 组件框上的厂商/账号标签（按云账号分组时容器标题已说明，不重复）
function accountTag(b) {
  if (laneMode.value === 'vendor') return ''
  return b.account_label || ''
}
function accountColor(b) {
  if (laneMode.value === 'vendor') return b.bandColor || UNASSIGNED_COLOR
  const accs = b.accounts || []
  return accs.length === 1 ? vendorColor(accs[0].vendor) : UNASSIGNED_COLOR
}
// 外部占位框（单云视图里的他云对端）：不展开成员名，只说明来自哪个数据源
function boxTitle(b) {
  return b.external ? t('topology.box.external', { label: b.source_label }) : b.label
}
// 框内地域 chip（最多 2 个 + n）
function regionText(b) {
  const rs = (b.regions || []).map((r) => r.region).filter(Boolean)
  if (!rs.length) return ''
  return rs.slice(0, 2).join(' · ') + (rs.length > 2 ? ` +${rs.length - 2}` : '')
}

// 正交直角走线（Visio 式）：竖直优先，同排水平绕行。
// 分组拆分后连线按 baseId 归位：同组对优先（同账号内部依赖），否则代表性首框互连（跨账号依赖如实跨线）
function linkPaths() {
  const g = arch.value
  if (!g) return []
  const byBase = {}
  layout.value.boxes.forEach((b) => {
    const base = b.baseId || b.id
    ;(byBase[base] ||= []).push(b)
  })
  const out = []
  for (const l of g.links) {
    const sArr = byBase[l.source]
    const tArr = byBase[l.target]
    if (!sArr || !tArr) continue
    const pairs = []
    const usedT = new Set()
    for (const a of sArr) {
      const same = tArr.find((b) => b.laneKey === a.laneKey && b.sectionKey === a.sectionKey
        && !usedT.has(b.id))
      if (same) {
        pairs.push([a, same])
        usedT.add(same.id)
      }
    }
    if (!pairs.length) pairs.push([sArr[0], tArr[0]])
    for (const [a, b] of pairs) {
      let d
      if (Math.abs((a.y + a.h / 2) - (b.y + b.h / 2)) < 60) {
        // 同高：左右走线
        const [left, right] = a.x < b.x ? [a, b] : [b, a]
        const x1 = left.x + left.w
        const x2 = right.x
        const cy = left.y + left.h / 2
        const midX = (x1 + x2) / 2
        d = `M ${x1} ${cy} H ${midX} V ${cy} H ${x2}`
      } else {
        // 上下走线：竖直-横-竖直
        const top = a.y < b.y ? a : b
        const bottom = a.y < b.y ? b : a
        const x1 = top.x + top.w / 2
        const y1 = top.y + top.h
        const x2 = bottom.x + bottom.w / 2
        const y2 = bottom.y
        const midY = (y1 + y2) / 2
        d = `M ${x1} ${y1} V ${midY} H ${x2} V ${y2}`
      }
      out.push({
        id: `${l.source}->${l.target}@${a.id}`, d,
        label: (l.cross_source
          ? `${t('topology.legend.cross_source')}${l.cross_source_label ? ' · ' + l.cross_source_label : ''}`
          : l.types.join('+')),
        cross: !!l.cross_source,
      })
    }
  }
  return out
}

function chipRows(grp) {
  const chips = grp.members.slice(0, 12).map((m) => m.label)
  const rows = []
  for (let i = 0; i < chips.length; i += 2) rows.push(chips.slice(i, i + 2))
  return rows
}

// 厂商/账号图例：颜色=厂商，条目=厂商 · 账号（有未归属成员时补一条灰点）
const legendItems = computed(() => {
  const g = arch.value
  if (!g) return []
  const items = (g.accounts || []).map((a) => ({
    key: `a${a.id}`,
    label: `${a.vendor_label || a.vendor} · ${a.name}`,
    color: vendorColor(a.vendor),
  }))
  const hasUnassigned = g.groups.some((grp) =>
    (grp.members || []).some((m) => !(m.meta || {}).account_id))
  if (hasUnassigned) items.push({ key: 'none', label: t('topology.legend.unassigned'), color: UNASSIGNED_COLOR })
  if ((g.groups || []).some((grp) => grp.external))
    items.push({ key: 'ext', label: t('topology.legend.external'), color: UNASSIGNED_COLOR })
  if ((g.links || []).some((l) => l.cross_source))
    items.push({ key: 'cross', label: t('topology.legend.cross_source'), color: C[theme.value].accent })
  return items
})

function matchBox(b) {
  const kw = search.value.trim().toLowerCase()
  if (!kw) return true
  const hay = [b.label, b.component, b.account_label, b.source_label,
    ...(b.regions || []).map((r) => r.region),
    ...(b.members || []).map((m) => m.label)]
  return hay.some((x) => String(x || '').toLowerCase().includes(kw))
}

// ---------- 画布交互：缩放/平移/框拖拽 ----------
function onWheel(e) {
  e.preventDefault()
  const rect = e.currentTarget.getBoundingClientRect()
  const cx = e.clientX - rect.left
  const cy = e.clientY - rect.top
  const factor = e.deltaY > 0 ? 0.9 : 1.1
  const nz = Math.min(MAX_Z, Math.max(MIN_Z, zoom.value * factor))
  pan.x = cx - (cx - pan.x) * (nz / zoom.value)
  pan.y = cy - (cy - pan.y) * (nz / zoom.value)
  zoom.value = nz
}

const dragState = ref(null)
function onCanvasDown(e) {
  if (e.button !== 0) return
  dragState.value = { type: 'pan', sx: e.clientX, sy: e.clientY, ox: pan.x, oy: pan.y }
}
function onBoxDown(e, b) {
  if (e.button !== 0) return
  e.stopPropagation()
  dragState.value = {
    type: 'box', id: b.id,
    sx: e.clientX, sy: e.clientY, ox: b.x, oy: b.y,
    moved: false,
  }
}
function onMove(e) {
  const st = dragState.value
  if (!st) return
  const dx = e.clientX - st.sx
  const dy = e.clientY - st.sy
  if (st.type === 'pan') {
    pan.x = st.ox + dx
    pan.y = st.oy + dy
  } else {
    if (Math.abs(dx) + Math.abs(dy) > 3) st.moved = true
    posOverride.value = {
      ...posOverride.value,
      [st.id]: { x: st.ox + dx / zoom.value, y: st.oy + dy / zoom.value },
    }
  }
}
function onUp() {
  const st = dragState.value
  if (st?.type === 'box' && st.moved) {
    localStorage.setItem(POS_KEY, JSON.stringify(posOverride.value))
  }
  dragState.value = null
}
function resetLayout() {
  posOverride.value = {}
  localStorage.removeItem(POS_KEY)
  localStorage.removeItem(OLD_POS_KEY)
  pan.x = 40
  pan.y = 20
  zoom.value = 1
  ElMessage.success(t('topology.msg.reset_done'))
}

function goDiagnose(b) {
  router.push({ path: '/diagnose', query: { q: t('topology.ask.diagnose_q', { label: b.label }).slice(0, 500) } })
}
function goAlerts(b) {
  // tab=records 直达告警记录页签，并保留关键字上下文，不落规则页
  router.push({ path: '/alerts', query: { keyword: b.label, tab: 'records' } })
}

// ---------- 未归属成员转到归属配置 ----------
// 未归属成员一键带到系统设置-归属配置并预填实体键；配置后回流本页（未配置保持"未归属"）
function ownKeyOf(row) {
  const meta = row?.meta || {}
  if (meta.instance_id) return { key_type: 'instance', key_value: meta.instance_id }
  if (meta.ip) return { key_type: 'ip', key_value: String(meta.ip).split(':')[0] }
  const id = String(row?.id || '')
  if (id.startsWith('domain:')) return { key_type: 'domain', key_value: id.slice(7) }
  if (id.startsWith('eip:')) return { key_type: 'ip', key_value: id.slice(4) }
  if (id.startsWith('host:')) return { key_type: 'ip', key_value: id.slice(5).split(':')[0] }
  return { key_type: 'name', key_value: row?.label || '' }
}
function goOwnership(row) {
  const q = row ? ownKeyOf(row) : {}
  router.push({ path: '/settings', query: { tab: 'ownership', ...q } })
}

function onBoxClick(b) {
  detail.value = b
  drawerOpen.value = true
}

// ---------- 导出（画布全量，不受当前缩放影响） ----------
function svgFull() {
  const el = document.getElementById('arch-svg')
  if (!el) return null
  const clone = el.cloneNode(true)
  const g = clone.querySelector('#world')
  if (g) g.setAttribute('transform', 'translate(0,0) scale(1)')
  clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg')
  clone.setAttribute('width', layout.value.width)
  clone.setAttribute('height', layout.value.height)
  clone.setAttribute('viewBox', `0 0 ${layout.value.width} ${layout.value.height}`)
  return clone
}
function exportSvg() {
  const clone = svgFull()
  if (!clone) return
  const blob = new Blob([new XMLSerializer().serializeToString(clone)], { type: 'image/svg+xml' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = t('topology.export.name') + sourceSuffix.value + (redact.value ? t('topology.export.redacted') : '') + '.svg'
  a.click()
  URL.revokeObjectURL(a.href)
  ElMessage.success(t('topology.export.svg_done') + (redact.value ? t('topology.export.svg_suffix_redacted') : t('topology.export.svg_suffix_warn')))
}
function exportPng() {
  const clone = svgFull()
  if (!clone) return
  const svgStr = new XMLSerializer().serializeToString(clone)
  const img = new Image()
  img.onload = () => {
    const canvas = document.createElement('canvas')
    canvas.width = layout.value.width * 2
    canvas.height = layout.value.height * 2
    const ctx = canvas.getContext('2d')
    ctx.scale(2, 2)
    ctx.fillStyle = C[theme.value].bg
    ctx.fillRect(0, 0, layout.value.width, layout.value.height)
    ctx.drawImage(img, 0, 0)
    const a = document.createElement('a')
    a.href = canvas.toDataURL('image/png')
    a.download = t('topology.export.name') + sourceSuffix.value + (redact.value ? t('topology.export.redacted') : '') + '.png'
    a.click()
    ElMessage.success(t('topology.export.png_done'))
  }
  img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svgStr)
}

// ---------- 新鲜度 ----------
const agoText = computed(() => {
  const ts = arch.value?.last_scan?.scanned_at
  if (!ts) return t('topology.ago.never')
  const diff = Date.now() - new Date(ts + 'Z').getTime()
  const m = Math.floor(diff / 60000)
  if (m < 1) return t('topology.ago.just')
  if (m < 60) return t('topology.ago.minutes', { m })
  return t('topology.ago.hours', { h: Math.floor(m / 60) })
})
const diffChips = computed(() => {
  const d = arch.value?.last_scan?.diff
  if (!d) return []
  const out = []
  if (d.added?.length) out.push({ text: t('topology.diff.added', { n: d.added.length }), type: 'success', list: d.added })
  if (d.removed?.length) out.push({ text: t('topology.diff.removed', { n: d.removed.length }), type: 'danger', list: d.removed })
  if (d.grown?.length) out.push({ text: t('topology.diff.grown', { n: d.grown.length }), type: 'warning', list: d.grown.map((x) => `${x.label} ${x.from}→${x.to}`) })
  if (d.shrunk?.length) out.push({ text: t('topology.diff.shrunk', { n: d.shrunk.length }), type: 'info', list: d.shrunk.map((x) => `${x.label} ${x.from}→${x.to}`) })
  return out
})
function showDiff(list, title) {
  ElMessageBox.alert(list.join(t('topology.list_sep')) || t('topology.diff.none'), title, { confirmButtonText: t('topology.common.got_it') })
}

watch(mode, () => {
  if (mode.value === 'detail') redact.value = false
  load()
})
watch(redact, () => { if (mode.value === 'report') load() })

onMounted(() => {
  const focus = route.query.focus
  if (focus) {
    search.value = String(focus)
    ElMessage.info(t('topology.msg.located', { focus }))
  }
  if (source.value === 'private') laneMode.value = 'region'
  load()
  loadScans()
})
</script>

<template>
  <div class="page">
    <el-tabs v-model="topTab">
      <el-tab-pane :label="t('topology.tab.arch')" name="arch">
        <div v-loading="loading">
    <div class="toolbar">
      <el-radio-group v-model="mode" size="default">
        <el-radio-button value="detail">{{ t('topology.mode.detail') }}</el-radio-button>
        <el-radio-button value="report">{{ t('topology.mode.report') }}</el-radio-button>
      </el-radio-group>
      <span class="group-label">{{ t('topology.lane.label') }}</span>
      <el-select v-model="laneMode" style="width: 150px">
        <el-option :label="t('topology.lane.role')" value="role" />
        <el-option :label="t('topology.lane.vendor')" value="vendor" />
        <el-option :label="t('topology.lane.region')" value="region" />
      </el-select>
      <template v-if="mode === 'report'">
        <el-radio-group v-model="theme" size="default">
          <el-radio-button value="tech">{{ t('topology.theme.tech') }}</el-radio-button>
          <el-radio-button value="biz">{{ t('topology.theme.biz') }}</el-radio-button>
        </el-radio-group>
        <el-switch v-model="redact" :active-text="t('topology.redact.on')" :inactive-text="t('topology.redact.off')" />
        <el-button type="primary" @click="exportPng">{{ t('topology.act.export_png') }}</el-button>
        <el-button @click="exportSvg">{{ t('topology.act.export_svg') }}</el-button>
      </template>
      <el-input v-model="search" :placeholder="t('topology.search_ph')" clearable style="width: 200px" />
      <el-button @click="resetLayout">{{ t('topology.act.reset') }}</el-button>
    </div>

    <!-- 数据新鲜度 + 扫描 -->
    <div class="fresh-bar">
      <el-tag :type="arch?.last_scan?.scanned_at ? 'success' : 'info'" effect="plain">
        {{ t('topology.fresh.scanned_at', { time: arch?.last_scan?.scanned_at ? fmtTime(arch.last_scan.scanned_at).slice(0, 16) : '—', ago: agoText }) }}
      </el-tag>
      <el-tag v-if="arch?.last_scan?.trigger" type="info" effect="plain" size="small">
        {{ arch.last_scan.trigger === 'manual' ? t('topology.fresh.manual') : t('topology.fresh.scheduled') }}
      </el-tag>
      <el-button v-if="canWrite" size="small" type="primary" :loading="scanning" @click="onScan">{{ t('topology.act.rescan') }}</el-button>
      <el-button size="small" text @click="historyVisible = true">{{ t('topology.act.history') }}</el-button>
      <el-tag
        v-for="c in diffChips"
        :key="c.text"
        :type="c.type"
        size="small"
        class="diff-chip"
        @click="showDiff(c.list, c.text)"
      >
        {{ c.text }}
      </el-tag>
      <span v-if="arch" class="stats">
        {{ t('topology.stats', { groups: arch.stats?.groups ?? '', links: arch.stats?.links ?? '', members: arch.stats?.members ?? '' }) }}
      </span>
    </div>

    <!-- 数据源分版：汇总不混云，每朵云独立成版；计数来自后端全量索引 -->
    <div v-if="arch" class="source-bar">
      <span class="group-label">{{ t('topology.source.label') }}</span>
      <el-radio-group :model-value="source" size="small" @change="setSource">
        <el-radio-button v-for="s in sourceTabs" :key="s.key" :value="s.key">
          {{ sourceLabelOf(s.key) }} {{ s.count }}
        </el-radio-button>
      </el-radio-group>
      <el-tag v-if="arch.stats?.mixed_groups_eliminated" type="success" effect="plain" size="small">
        {{ t('topology.source.mixed_fixed', { n: arch.stats.mixed_groups_eliminated }) }}
      </el-tag>
    </div>

    <!-- 加载失败：画布留白会被误读成"架构为空"，给明确错误与重试 -->
    <el-empty v-if="loadError && !arch" :description="loadError">
      <el-button type="primary" @click="load">{{ t('common.retry') }}</el-button>
    </el-empty>

    <!-- 画布 -->
    <div
      v-else
      class="canvas"
      :style="{ background: C[theme].bg, cursor: dragState?.type === 'pan' ? 'grabbing' : 'grab' }"
      @wheel.prevent="onWheel"
      @mousedown="onCanvasDown"
      @mousemove="onMove"
      @mouseup="onUp"
      @mouseleave="onUp"
    >
      <svg
        id="arch-svg"
        :width="layout.width"
        :height="layout.height"
        :viewBox="`0 0 ${layout.width} ${layout.height}`"
        xmlns="http://www.w3.org/2000/svg"
      >
        <defs>
          <pattern id="grid" width="28" height="28" patternUnits="userSpaceOnUse">
            <circle cx="1" cy="1" r="1" :fill="C[theme].grid" />
          </pattern>
        </defs>
        <rect x="0" y="0" :width="layout.width" :height="layout.height" fill="url(#grid)" />

        <g id="world" :transform="`translate(${pan.x},${pan.y}) scale(${zoom})`">
          <!-- 汇总版云容器：每个数据源一段，段内保留完整架构层级 -->
          <g v-for="s in layout.sections" :key="'sec:' + s.key">
            <rect x="6" :y="s.y" :width="layout.width - 12" :height="s.height" rx="18"
                  fill="none" :stroke="s.color" stroke-width="2" opacity="0.55" />
            <text x="22" :y="s.y + 28" :fill="s.color" font-size="15" font-weight="700">
              {{ t('topology.section.title', { label: s.label, accounts: s.accounts, regions: s.regions, groups: s.groups }) }}
            </text>
          </g>

          <!-- 分组容器（架构层级 / 云账号 / 地域） -->
          <g v-for="b in layout.bands" :key="b.key">
            <rect :x="b.multi ? 16 : 12" :y="b.y" :width="layout.width - (b.multi ? 32 : 24)" :height="b.height" rx="14"
                  :fill="C[theme].band" :stroke="b.color || C[theme].bandBorder" stroke-dasharray="7 5" />
            <text :x="b.multi ? 34 : 30" :y="b.y + 32" :fill="b.color || C[theme].bandText" font-size="16" font-weight="700">
              {{ t('topology.band.title', { label: b.label, n: b.count }) }}
            </text>
          </g>

          <!-- 依赖连线（正交直角；跨云依赖虚线） -->
          <g>
            <path v-for="p in linkPaths()" :key="p.id" :d="p.d" fill="none"
                  :stroke="C[theme].line" :stroke-width="1.8" opacity="0.7"
                  :stroke-dasharray="p.cross ? '6 4' : ''">
              <title>{{ p.label }}</title>
            </path>
          </g>

          <!-- 组件框 -->
          <g v-for="b in layout.boxes" :key="b.id"
             :opacity="matchBox(b) ? 1 : 0.16"
             style="cursor: move"
             @mousedown="onBoxDown($event, b)"
             @click="onBoxClick(b)">
            <rect :x="b.x" :y="b.y" :width="b.w" :height="b.h" rx="12"
                  :fill="C[theme].box" :stroke="ROLE_COLORS[b.role] || C[theme].boxBorder"
                  stroke-width="2" :stroke-dasharray="b.external ? '7 4' : ''" />
            <rect :x="b.x" :y="b.y" :width="7" :height="b.h" rx="3.5"
                  :fill="ROLE_COLORS[b.role] || C[theme].accent" />
            <text :x="b.x + 22" :y="b.y + 30" :fill="C[theme].title" font-size="15" font-weight="700">
              {{ boxTitle(b).length > 17 ? boxTitle(b).slice(0, 16) + '…' : boxTitle(b) }}
            </text>
            <!-- 厂商/账号标签（按架构层级分组时显示；按云账号分组由容器标题说明） -->
            <text v-if="!b.external && accountTag(b)" :x="b.x + b.w - 12" :y="b.y + 30" text-anchor="end"
                  :fill="accountColor(b)" font-size="10.5">
              {{ accountTag(b).length > 16 ? accountTag(b).slice(0, 15) + '…' : accountTag(b) }}
            </text>
            <text :x="b.x + 22" :y="b.y + 52" :fill="C[theme].text" font-size="11.5">
              {{ mode === 'report' ? t('topology.box.nodes', { n: b.count }) : (b.external ? t('topology.box.external_hint') : (b.cluster && b.cluster !== 'all' ? t('topology.box.cluster', { n: b.count }) : t('topology.box.instances', { n: b.count }))) }}
            </text>
            <!-- 地域 chip（最多 2 个 + n；私有云看集群/地域靠它一眼定位） -->
            <text v-if="regionText(b)" :x="b.x + b.w - 12" :y="b.y + 52" text-anchor="end"
                  :fill="C[theme].chipText" font-size="10.5">
              {{ regionText(b) }}
            </text>

            <g v-if="mode === 'detail'">
              <g v-for="(row, ri) in chipRows(b)" :key="ri">
                <g v-for="(chip, ci) in row" :key="ci">
                  <!-- 长实例名会截断，悬停给全名（此前截断后再也看不到完整值） -->
                  <title>{{ chip }}</title>
                  <rect :x="b.x + 18 + ci * 136" :y="b.y + 66 + ri * 28" width="130" height="22" rx="6"
                        :fill="C[theme].chip" />
                  <text :x="b.x + 26 + ci * 136" :y="b.y + 81 + ri * 28"
                        :fill="C[theme].chipText" font-size="10.5">
                    {{ chip.length > 15 ? chip.slice(0, 14) + '…' : chip }}
                  </text>
                </g>
              </g>
              <text v-if="b.count > 12" :x="b.x + 22" :y="b.y + 74 + 6 * 28"
                    :fill="C[theme].text" font-size="10.5">{{ t('topology.box.more', { n: b.count }) }}</text>
            </g>
          </g>
        </g>
      </svg>
    </div>

    <!-- 厂商/账号图例（混合云同屏：颜色=厂商，标签=厂商 · 账号） -->
    <div v-if="legendItems.length" class="vendor-legend">
      <span class="legend-title">{{ t('topology.legend.title') }}</span>
      <span v-for="it in legendItems" :key="it.key" class="legend-item">
        <span class="legend-dot" :style="{ background: it.color }" />
        {{ it.label }}
      </span>
    </div>

    <div class="hint">
      {{ t('topology.hint') }}
    </div>
        </div>
      </el-tab-pane>
      <el-tab-pane :label="t('topology.tab.links')" name="links">
        <LinksDraft />
      </el-tab-pane>
    </el-tabs>

    <!-- 详情抽屉 -->
    <el-drawer v-model="drawerOpen" :title="detail ? boxTitle(detail) : t('topology.detail.title')" size="560px">
      <template v-if="detail">
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item :label="t('topology.detail.role')">{{ roleLabel(detail.role) }}</el-descriptions-item>
          <el-descriptions-item :label="t('topology.detail.source')">
            {{ sourceLabelOf(detail.source) }}<span v-if="detail.external"> · {{ t('topology.legend.external') }}</span>
          </el-descriptions-item>
          <el-descriptions-item :label="t('topology.detail.component')">{{ detail.component }}</el-descriptions-item>
          <el-descriptions-item v-if="detail.cluster" :label="t('topology.detail.cluster')">{{ detail.cluster }}</el-descriptions-item>
          <el-descriptions-item :label="t('topology.detail.vendor_account')">
            <template v-if="detail.account_label">{{ detail.account_label }}</template>
            <template v-else>
              {{ t('topology.legend.unassigned') }}
              <el-link type="primary" size="small" class="own-link" @click="goOwnership()">{{ t('topology.act.ownership') }}</el-link>
            </template>
          </el-descriptions-item>
          <el-descriptions-item :label="t('topology.detail.count')">{{ detail.count }}</el-descriptions-item>
        </el-descriptions>
        <div class="nav-row">
          <el-button size="small" type="primary" text @click="goDiagnose(detail)">{{ t('topology.act.diagnose') }}</el-button>
          <el-button size="small" text @click="goAlerts(detail)">{{ t('topology.act.alerts') }}</el-button>
        </div>
        <div class="member-title">{{ t('topology.detail.members_title') }}</div>
        <el-table :data="detail.members" size="small" border max-height="420">
          <el-table-column prop="label" :label="t('topology.detail.col_name')" min-width="150" />
          <el-table-column :label="t('topology.detail.col_vendor')" width="170">
            <template #default="{ row }">
              <span v-if="row.meta?.account">{{ row.meta.vendor_label || row.meta.vendor }} · {{ row.meta.account }}</span>
              <span v-else>
                <span class="tip">{{ t('topology.common.unassigned') }}</span>
                <el-link type="primary" size="small" class="own-link" @click="goOwnership(row)">{{ t('topology.act.ownership') }}</el-link>
              </span>
            </template>
          </el-table-column>
          <el-table-column :label="t('topology.detail.col_region')" width="120">
            <template #default="{ row }">{{ row.meta?.region || row.meta?.cluster || row.meta?.zone || '—' }}</template>
          </el-table-column>
          <el-table-column v-if="!redact" :label="t('topology.detail.col_vpc')" width="130">
            <template #default="{ row }">{{ row.meta?.vpc_id || '—' }}</template>
          </el-table-column>
          <el-table-column :label="t('topology.detail.col_business')" width="90">
            <template #default="{ row }">
              <span v-if="row.meta?.business">{{ row.meta.business }}</span>
              <span v-else class="tip">—</span>
            </template>
          </el-table-column>
          <el-table-column :label="t('topology.detail.col_status')" width="90">
            <template #default="{ row }">{{ row.meta?.status ? assetStatusLabel(row.meta.status) : '—' }}</template>
          </el-table-column>
          <el-table-column v-if="!redact" :label="t('topology.detail.col_ip')" width="130">
            <template #default="{ row }">{{ row.meta?.ip || '—' }}</template>
          </el-table-column>
        </el-table>
      </template>
    </el-drawer>

    <!-- 扫描历史 -->
    <el-drawer v-model="historyVisible" :title="t('topology.hist.title')" size="560px">
      <el-table :data="scans" size="small" border>
        <el-table-column :label="t('topology.hist.col_time')" width="150">
          <template #default="{ row }">{{ fmtTime(row.scanned_at).slice(0, 16) }}</template>
        </el-table-column>
        <el-table-column :label="t('topology.hist.col_trigger')" width="80">
          <template #default="{ row }">{{ row.trigger === 'manual' ? t('topology.hist.manual') : t('topology.hist.scheduled') }}</template>
        </el-table-column>
        <el-table-column :label="t('topology.hist.col_scale')" width="120">
          <template #default="{ row }">{{ t('topology.hist.scale', { groups: row.groups ?? '', links: row.links ?? '' }) }}</template>
        </el-table-column>
        <el-table-column :label="t('topology.hist.col_change')">
          <template #default="{ row }">
            <template v-if="row.diff">
              <el-tag v-if="row.diff.added?.length" size="small" type="success" class="diff-chip">{{ t('topology.hist.added', { n: row.diff.added.length }) }}</el-tag>
              <el-tag v-if="row.diff.removed?.length" size="small" type="danger" class="diff-chip">{{ t('topology.hist.removed', { n: row.diff.removed.length }) }}</el-tag>
              <el-tag v-if="row.diff.grown?.length" size="small" type="warning" class="diff-chip">{{ t('topology.hist.grown', { n: row.diff.grown.length }) }}</el-tag>
              <el-tag v-if="row.diff.shrunk?.length" size="small" type="info" class="diff-chip">{{ t('topology.hist.shrunk', { n: row.diff.shrunk.length }) }}</el-tag>
              <span v-if="row.diff.note" class="stats">{{ row.diff.note }}</span>
            </template>
          </template>
        </el-table-column>
      </el-table>
    </el-drawer>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.group-label {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
/* 数据源分版栏：与刷新条同一视觉层级（数据源是一次请求返回的全量索引） */
.source-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding: 8px 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: var(--el-bg-color);
}
.fresh-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding: 8px 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: var(--el-bg-color);
}
.diff-chip {
  cursor: pointer;
}
.stats {
  margin-left: auto;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.canvas {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 10px;
  overflow: hidden;
  height: calc(100vh - 300px);
  min-height: 520px;
  user-select: none;
}
.hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.tip {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.own-link {
  margin-left: 6px;
  font-size: 12px;
}
.vendor-legend {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.legend-title {
  font-weight: 600;
}
.legend-item {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}
.legend-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  display: inline-block;
}
.nav-row { margin-bottom: 10px; display: flex; gap: 8px; }
.member-title {
  margin: 14px 0 8px;
  font-weight: 600;
}
</style>
