<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'

import {
  createSavedQuery,
  deleteSavedQuery,
  listSavedQueries,
  logSearch,
  logSources,
  logTargets,
  slsLogstores,
  slsProjects,
} from '@/api/logs'
import { useAuthStore } from '@/stores/auth'
import { t } from '@/i18n'

const auth = useAuthStore()
const router = useRouter()

const RANGES = [
  { key: '15m', label: 'logs.range.15m' },
  { key: '1h', label: 'logs.range.1h' },
  { key: '4h', label: 'logs.range.4h' },
  { key: '24h', label: 'logs.range.24h' },
  { key: '3d', label: 'logs.range.3d' },
  { key: '7d', label: 'logs.range.7d' },
]

// SLB 访问日志优先展示的字段顺序（其余按字母序追加；字段名按标准 SLB 索引）
const PRIORITY = [
  '__time__', 'status', 'request_uri', 'request_method', 'host', 'http_host',
  'upstream_addr', 'upstream_response_time', 'upstream_status', 'request_time',
  'read_request_time', 'client_ip', 'scheme', 'body_bytes_sent',
  'http_user_agent', 'ssl_protocol', 'slbid', 'slb_vport', '__topic__',
]

// 示例按源类型切换：SLS/CLS 同构、LogsQL、ES query DSL
const EXAMPLES_BY_KIND = {
  sls: [
    { label: 'logs.examples.error', q: 'level: ERROR' },
    { label: 'logs.examples.warn', q: 'level: WARN' },
    { label: 'logs.examples.5xx', q: 'status >= 500' },
    { label: 'logs.examples.slow', q: 'upstream_response_time > 1' },
    { label: 'logs.examples.uri_prefix', q: 'request_uri: /api/v1*' },
    { label: 'logs.examples.status_dist', q: '* | select status, count(*) as cnt group by status order by cnt desc' },
    { label: 'logs.examples.top_uri', q: '* | select request_uri, count(*) as cnt group by request_uri order by cnt desc limit 10' },
  ],
  loki: [
    { label: 'logs.examples.loki.error', q: '{app="nginx"} |= "ERROR"' },
    { label: 'logs.examples.loki.filter', q: '{app="nginx"} != "health" |~ "(?i)timeout|refused"' },
    { label: 'logs.examples.loki.json', q: '{app="api"} | json | status >= 500' },
    { label: 'logs.examples.loki.all', q: '{job=~".+"}' },
  ],
  es: [
    { label: 'logs.examples.es.error', q: 'level: ERROR' },
    { label: 'logs.examples.es.5xx', q: 'status >= 500' },
    { label: 'logs.examples.es.dsl', q: '{"query":{"bool":{"must":[{"match":{"level":"ERROR"}}]}}}' },
    { label: 'logs.examples.es.range', q: '{"query":{"range":{"status":{"gte":500}}}}' },
  ],
}

// ---------------- 多源日志查询 ----------------
const sources = ref([])
const sourceName = ref('')
const loadingSources = ref(false)
const noSources = ref(false)      // 无可查询日志源 -> 真实空态引导（非报错）
const sourcesError = ref('')      // 日志源接口失败 -> 报错态，不能引导去重配
const targets = ref([])           // CLS 日志主题 / ES 索引
const target = ref('')
const loadingTargets = ref(false)

// ---------------- SLS 目标（原有两级 project/logstore） ----------------
const projects = ref([])
const project = ref('')
const logstores = ref([])
const logstore = ref('')

const queryText = ref('')
const range = ref('1h')
const page = ref(1)
const pageSize = ref(20)
const searching = ref(false)
const result = ref(null)
const searchError = ref(false)   // 查询失败态，区别于"尚未查询/暂无数据"
const savedList = ref([])
const loadingProjects = ref(false)
const loadingLogstores = ref(false)

const saveDialogVisible = ref(false)
const saveName = ref('')
const saving = ref(false)
const detailVisible = ref(false)
const detailRow = ref(null)
const showAllFields = ref(false)
// 跨 project 应用常用查询时，传给 loadLogstores 优先选中的 logstore
const pendingLogstore = ref('')
// 跨源应用常用查询：源切换+目标加载完成后接着应用
const pendingSaved = ref(null)

const canWrite = computed(() => ['admin', 'operator'].includes(auth.role))

const currentSource = computed(() =>
  sources.value.find((s) => s.name === sourceName.value) || null)
const sourceType = computed(() => currentSource.value?.type || 'sls')
const isSls = computed(() => sourceType.value === 'sls')
const isLoki = computed(() => sourceType.value === 'loki')
const isEs = computed(() => sourceType.value === 'es')
// sls 走 project/logstore 两级；cls/es 单目标（主题/索引）；loki 无目标（LogQL 自带）
const needTarget = computed(() => sourceType.value === 'cls' || isEs.value)
const syntaxKind = computed(() => (isLoki.value ? 'loki' : isEs.value ? 'es' : 'sls'))
const EXAMPLES = computed(() => EXAMPLES_BY_KIND[syntaxKind.value] || EXAMPLES_BY_KIND.sls)
const queryPlaceholder = computed(() => {
  if (isLoki.value) return t('logs.query.ph.loki')
  if (isEs.value) return t('logs.query.ph.es')
  return t('logs.query.ph')
})
const targetPlaceholder = computed(() =>
  isEs.value ? t('logs.target.ph.es') : t('logs.target.ph.cls'))
const readyToSave = computed(() => {
  if (isSls.value) return !!logstore.value
  if (needTarget.value) return !!target.value
  return true
})
const saveContext = computed(() => {
  const parts = [sourceName.value]
  if (isSls.value) parts.push(project.value, logstore.value)
  else if (needTarget.value) parts.push(target.value)
  parts.push(range.value)
  return parts.filter(Boolean).join(' / ')
})

// 请求序号：防止切换源/project/logstore 后旧响应覆盖新状态
let reqSeq = 0
let targetSeq = 0

const logs = computed(() => result.value?.logs || [])
const total = computed(() => {
  const r = result.value
  if (!r) return 0
  return (r.page - 1) * r.page_size + (r.logs?.length || 0) + (r.has_more ? r.page_size : 0)
})

// 动态列：SQL 结果保持返回顺序；检索结果按 PRIORITY 排序
const columns = computed(() => {
  const rows = logs.value
  if (!rows.length) return []
  const keys = new Map()
  rows.forEach((r) => Object.keys(r).forEach((k) => { if (!keys.has(k)) keys.set(k, keys.size) }))
  let list = [...keys.keys()]
  if (!result.value?.is_sql) {
    list.sort((a, b) => {
      const ia = PRIORITY.indexOf(a)
      const ib = PRIORITY.indexOf(b)
      if (ia !== -1 || ib !== -1) return (ia === -1 ? 999 : ia) - (ib === -1 ? 999 : ib)
      return a.localeCompare(b)
    })
  }
  return showAllFields.value ? list : list.slice(0, 10)
})

function cellValue(v) {
  if (v === null || v === undefined) return ''
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

function fmtTime(v) {
  const ts = Number(v)
  if (!Number.isFinite(ts) || ts <= 0) return cellValue(v)
  const d = new Date(ts > 1e12 ? ts : ts * 1000)
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

// 保存的查询与示例做轻量语法高亮（先转义再上色）
function highlightQuery(q) {
  const esc = String(q)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
  return esc
    .replace(/(&quot;[^&]*?&quot;)/g, '<span class="q-str">$1</span>')
    .replace(/\b(and|or|not|select|from|where|group|by|order|limit|as|count)\b/gi, '<span class="q-kw">$1</span>')
    .replace(/([A-Za-z_][\w.]*)\s*:/g, '<span class="q-field">$1</span>:')
}

// 源类型/状态标签：复用集成中心类型键（integrations.type.*），缺键退后端中文标签
function sourceTypeLabel(s) {
  const k = `integrations.type.${s.type}`
  const v = t(k)
  return v === k ? (s.type_label || s.type) : v
}

function statusLabel(status) {
  return t(`logs.source.status.${status}`)
}

function goIntegrations() {
  // 日志源登记在「数据源」页签，不落默认「集成管理」
  router.push('/integrations?tab=data')
}

async function loadSources() {
  loadingSources.value = true
  try {
    const data = await logSources()
    sources.value = data.sources || []
    noSources.value = !!data.empty
    sourcesError.value = ''
    const def = data.default
    sourceName.value = (def && sources.value.some((s) => s.name === def))
      ? def
      : ((sources.value.find((s) => s.queryable) || {}).name || '')
  } catch (e) {
    sources.value = []
    noSources.value = false
    sourcesError.value = e?.response?.data?.detail || t('logs.empty.sources_fail')
    sourceName.value = ''
  } finally {
    loadingSources.value = false
  }
}

async function loadTargetsForSource() {
  page.value = 1
  target.value = ''
  targets.value = []
  if (isSls.value) {
    await loadProjects()
  } else if (needTarget.value) {
    const seq = ++targetSeq
    loadingTargets.value = true
    try {
      const data = await logTargets(sourceName.value)
      if (seq !== targetSeq) return
      targets.value = data.items || []
    } catch {
      if (seq !== targetSeq) return
      targets.value = []
    } finally {
      if (seq === targetSeq) loadingTargets.value = false
    }
  }
  // 跨源应用常用查询：目标加载完成后接着应用
  if (pendingSaved.value) {
    const sq = pendingSaved.value
    pendingSaved.value = null
    applySavedTargets(sq)
  }
}

async function loadProjects() {
  loadingProjects.value = true
  try {
    const data = await slsProjects()
    projects.value = data.projects || []
    // 默认态不写死任何客户 project 优先，
    // 进页留空占位，由用户从下拉选择（多 project 环境猜错代价高）
    if (project.value && !projects.value.includes(project.value)) project.value = ''
  } finally {
    loadingProjects.value = false
  }
}

async function loadLogstores() {
  if (!project.value) {
    logstores.value = []
    logstore.value = ''
    return
  }
  const current = project.value
  const want = pendingLogstore.value // 跨 project 应用常用查询时优先选中的 logstore
  pendingLogstore.value = ''
  loadingLogstores.value = true
  logstores.value = []
  logstore.value = ''
  try {
    const data = await slsLogstores(current, sourceName.value)
    if (current !== project.value) return
    logstores.value = data.logstores || []
    // 空占位+下拉：不写死客户 logstore 默认；常用查询带的 logstore 仍优先
    logstore.value = (want && logstores.value.includes(want)) ? want : ''
  } finally {
    if (current === project.value) loadingLogstores.value = false
  }
}

// 回到第 1 页再查（page 已是 1 时直接查，避免 watcher + 显式调用双请求）
function resetAndSearch() {
  if (page.value === 1) doSearch()
  else page.value = 1
}

async function doSearch() {
  if (isSls.value && (!project.value || !logstore.value)) return
  if (needTarget.value && !target.value) return
  if (isLoki.value && !queryText.value.trim()) return
  const seq = ++reqSeq
  searching.value = true
  try {
    const params = {
      source: sourceName.value,
      query: queryText.value || '',
      range: range.value,
      page: page.value,
      page_size: pageSize.value,
    }
    if (isSls.value) {
      params.project = project.value
      params.logstore = logstore.value
    }
    if (needTarget.value) params.target = target.value
    const data = await logSearch(params)
    if (seq !== reqSeq) return // 过期响应，丢弃
    result.value = data
    searchError.value = false
  } catch {
    if (seq !== reqSeq) return
    result.value = null
    searchError.value = true   // 失败态：空表给"查询失败，重试"而非"暂无数据"
  } finally {
    if (seq === reqSeq) searching.value = false
  }
}

async function loadSaved() {
  try {
    savedList.value = await listSavedQueries()
  } catch {
    savedList.value = []
  }
}

function onSearchClick() {
  resetAndSearch()
}

function applyExample(q) {
  queryText.value = q
  onSearchClick()
}

// 应用常用查询：source 空=旧行 SLS；跨源先切源再落目标
function applySaved(sq) {
  queryText.value = sq.query
  range.value = sq.time_range
  const slsName = (sources.value.find((s) => s.type === 'sls') || {}).name || ''
  const wantSource = sq.source || slsName
  if (wantSource && wantSource !== sourceName.value) {
    if (sources.value.some((s) => s.name === wantSource)) {
      pendingSaved.value = sq
      sourceName.value = wantSource // watch -> loadTargetsForSource -> applySavedTargets
      return
    }
    ElMessage.warning(t('logs.saved.source_missing'))
    return
  }
  applySavedTargets(sq)
}

function applySavedTargets(sq) {
  if (isSls.value) {
    if (sq.project && sq.project !== project.value) {
      // project 变化 -> watch 异步触发 loadLogstores，期间用 pendingLogstore 指定目标
      pendingLogstore.value = sq.logstore || ''
      project.value = sq.project
      return
    }
    if (sq.logstore && logstores.value.includes(sq.logstore) && sq.logstore !== logstore.value) {
      logstore.value = sq.logstore // 触发 watch -> 搜索
      return
    }
    resetAndSearch()
    return
  }
  if (needTarget.value && sq.target && sq.target !== target.value) {
    if (targets.value.some((tg) => tg.id === sq.target) || isEs.value) {
      target.value = sq.target // 触发 watch -> 搜索（ES 允许手输索引名）
      return
    }
    ElMessage.warning(t('logs.saved.target_missing'))
    return
  }
  resetAndSearch()
}

async function removeSaved(sq) {
  try {
    await ElMessageBox.confirm(t('logs.remove.confirm', { name: sq.name }), t('logs.remove.confirm_title'), { type: 'warning' })
  } catch {
    return
  }
  try {
    await deleteSavedQuery(sq.id)
    ElMessage.success(t('logs.msg.deleted'))
    loadSaved()
  } catch {
    /* http 层已提示 */
  }
}

async function saveQuery() {
  if (!saveName.value.trim()) {
    ElMessage.warning(t('logs.msg.name_required'))
    return
  }
  saving.value = true
  try {
    await createSavedQuery({
      name: saveName.value.trim(),
      source: sourceName.value,
      target: needTarget.value ? target.value : '',
      project: isSls.value ? project.value : '',
      logstore: isSls.value ? logstore.value : '',
      query: queryText.value,
      time_range: range.value,
    })
    ElMessage.success(t('logs.msg.saved'))
    saveDialogVisible.value = false
    saveName.value = ''
    loadSaved()
  } catch {
    /* http 层已提示 */
  } finally {
    saving.value = false
  }
}

watch(sourceName, () => {
  result.value = null
  searchError.value = false
  loadTargetsForSource()
})
watch(project, () => {
  result.value = null
  searchError.value = false
  loadLogstores()
})
watch(logstore, () => {
  result.value = null
  searchError.value = false
  resetAndSearch()
})
watch(target, () => {
  if (!needTarget.value) return
  result.value = null
  searchError.value = false
  resetAndSearch()
})
// 翻页直接查；改每页条数必须回第 1 页，否则 page=3&page_size=100 会与前页重叠
// 避免跨页重叠
watch(page, () => doSearch())
watch(pageSize, () => {
  if (page.value === 1) doSearch()
  else page.value = 1
})

onMounted(async () => {
  loadSaved()
  await loadSources()
  if (!noSources.value) await loadTargetsForSource()
})
</script>

<template>
  <div class="page">
    <!-- 无任何可查询日志源是真实空态；源列表加载失败仍按错误展示 -->
    <div v-if="sourcesError" class="guide-box">
      <div class="guide-title">{{ sourcesError }}</div>
      <div class="guide-sub">{{ t('logs.empty.sources_fail_hint') }}</div>
      <el-button type="primary" @click="loadSources">{{ t('common.retry') }}</el-button>
    </div>

    <div v-else-if="noSources" class="guide-box">
      <div class="guide-title">{{ t('logs.empty.no_sources') }}</div>
      <div class="guide-sub">{{ t('logs.empty.guide') }}</div>
      <el-button type="primary" @click="goIntegrations">{{ t('logs.empty.goto_integrations') }}</el-button>
      <div class="k8s-note">{{ t('logs.k8s.note') }}</div>
    </div>

    <template v-else>
      <div class="toolbar">
        <el-select
          v-model="sourceName"
          filterable
          :placeholder="t('logs.source.ph')"
          style="width: 280px"
          :loading="loadingSources"
        >
          <el-option v-for="s in sources" :key="s.name" :label="s.name" :value="s.name" :disabled="!s.queryable">
            <span class="src-name">{{ s.name }}</span>
            <span class="src-meta">{{ sourceTypeLabel(s) }}</span>
            <el-tag
              v-if="s.status !== 'ready'"
              size="small"
              :type="s.status === 'no_credential' ? 'warning' : 'info'"
              class="src-status"
            >{{ statusLabel(s.status) }}</el-tag>
          </el-option>
        </el-select>
        <template v-if="isSls">
          <el-select
            v-model="project"
            filterable
            :placeholder="t('logs.toolbar.project_ph')"
            style="width: 280px"
            :loading="loadingProjects"
          >
            <el-option v-for="p in projects" :key="p" :label="p" :value="p" />
          </el-select>
          <el-select
            v-model="logstore"
            filterable
            :placeholder="t('logs.toolbar.logstore_ph')"
            style="width: 240px"
            :loading="loadingLogstores"
            :disabled="!project"
          >
            <el-option v-for="l in logstores" :key="l" :label="l" :value="l" />
          </el-select>
        </template>
        <el-select
          v-else-if="needTarget"
          v-model="target"
          filterable
          :allow-create="isEs"
          :placeholder="targetPlaceholder"
          style="width: 280px"
          :loading="loadingTargets"
        >
          <el-option v-for="tg in targets" :key="tg.id" :label="tg.name" :value="tg.id">
            <span class="src-name">{{ tg.name }}</span>
            <span v-if="tg.detail" class="src-meta">{{ tg.detail }}</span>
          </el-option>
        </el-select>
        <el-radio-group v-model="range">
          <el-radio-button v-for="r in RANGES" :key="r.key" :value="r.key">{{ t(r.label) }}</el-radio-button>
        </el-radio-group>
        <div style="flex: 1" />
        <el-button
          type="primary"
          :disabled="!canWrite || !readyToSave"
          @click="saveDialogVisible = true"
        >{{ t('logs.toolbar.save') }}</el-button>
      </div>

      <!-- 与 K8s 容器日志的分工：容器日志不进本页 -->
      <div class="k8s-note">
        <el-button link type="primary" @click="router.push({ name: 'k8s', query: { tab: 'logs' } })">
          {{ t('logs.k8s.note') }}
        </el-button>
      </div>

      <div class="toolbar">
        <el-input
          v-model="queryText"
          :placeholder="queryPlaceholder"
          style="flex: 1"
          clearable
          @keyup.enter="onSearchClick"
        >
          <template #append>
            <el-button :loading="searching" @click="onSearchClick">{{ t('logs.query.run') }}</el-button>
          </template>
        </el-input>
        <el-popover placement="bottom-start" :width="420" trigger="click">
          <template #reference>
            <el-button>{{ t('logs.syntax.title') }}</el-button>
          </template>
          <!-- 语法帮助按源类型切换：SLS/CLS 同构、LogsQL、ES query DSL -->
          <div v-if="syntaxKind === 'sls'" class="syntax-help">
            <p><b>{{ t('logs.syntax.search_title') }}</b></p>
            <ul>
              <li><code>status: 500</code> {{ t('logs.syntax.exact') }}</li>
              <li><code>request_uri: /api/v1*</code> {{ t('logs.syntax.fuzzy_pre') }}<b>{{ t('logs.syntax.fuzzy_bold') }}</b>{{ t('logs.syntax.fuzzy_post') }}</li>
              <li><code>status &gt;= 500 and not status: 503</code> — and / or / not</li>
              <li><code>"error timeout"</code> {{ t('logs.syntax.phrase') }}</li>
            </ul>
            <p><b>{{ t('logs.syntax.sql_title') }}</b>{{ t('logs.syntax.sql_pre') }}<code>|</code> {{ t('logs.syntax.sql_post') }}</p>
            <ul>
              <li><code>* | select count(*) as cnt</code></li>
              <li><code>* | select status, count(*) cnt group by status</code></li>
            </ul>
          </div>
          <div v-else-if="syntaxKind === 'loki'" class="syntax-help">
            <p><b>{{ t('logs.syntax.loki_title') }}</b></p>
            <ul>
              <li><code>{app="nginx"}</code> {{ t('logs.syntax.loki_stream') }}</li>
              <li><code>|= "error"</code> {{ t('logs.syntax.loki_filter') }}</li>
              <li><code>|~ "(?i)timeout"</code> {{ t('logs.syntax.loki_regex') }}</li>
              <li><code>| json | status &gt;= 500</code> {{ t('logs.syntax.loki_json') }}</li>
            </ul>
          </div>
          <div v-else class="syntax-help">
            <p><b>{{ t('logs.syntax.es_title') }}</b></p>
            <ul>
              <li><code>level: ERROR</code> {{ t('logs.syntax.es_exact') }}</li>
              <li><code>status &gt;= 500</code> {{ t('logs.syntax.es_range') }}</li>
            </ul>
            <p><b>{{ t('logs.syntax.es_dsl_title') }}</b>{{ t('logs.syntax.es_dsl_pre') }}</p>
            <ul>
              <li><code>{"query":{"match":{"level":"ERROR"}}}</code></li>
              <li>{{ t('logs.syntax.es_time') }}</li>
            </ul>
          </div>
        </el-popover>
      </div>

      <div class="examples">
        <span class="ex-label">{{ t('logs.examples.title') }}</span>
        <el-tag
          v-for="ex in EXAMPLES"
          :key="ex.q"
          class="ex-tag"
          effect="plain"
          @click="applyExample(ex.q)"
        >{{ t(ex.label) }}</el-tag>
      </div>

      <div v-if="savedList.length" class="saved">
        <span class="ex-label">{{ t('logs.saved.title') }}</span>
        <el-tooltip
          v-for="sq in savedList"
          :key="sq.id"
          placement="top"
          :show-after="300"
        >
          <template #content>
            <div class="saved-tip" v-html="highlightQuery(sq.query || '*')" />
          </template>
          <el-tag
            class="ex-tag"
            type="success"
            :closable="canWrite"
            @click="applySaved(sq)"
            @close="removeSaved(sq)"
          >{{ sq.name }}</el-tag>
        </el-tooltip>
      </div>

      <el-table stripe
        v-loading="searching"
        :data="logs"
        border
        size="small"
        max-height="560"
        @row-click="(row) => { detailRow = row; detailVisible = true }"
      >
        <template #empty>
          <!-- 空态三分：查询失败≠暂无数据≠尚未查询 -->
          <div v-if="searchError" class="empty-box">
            {{ t('logs.empty.error_pre') }}<el-button link type="primary" @click="onSearchClick">{{ t('logs.empty.retry') }}</el-button>
          </div>
          <div v-else class="empty-box">
            {{ result ? t('logs.empty.no_data') : t('logs.empty.not_searched') }}
          </div>
        </template>
        <el-table-column
          v-for="col in columns"
          :key="col"
          :prop="col"
          :label="col"
          :min-width="col === '__time__' ? 165 : 150"
          :formatter="(row) => (col === '__time__' ? fmtTime(row[col]) : cellValue(row[col]))"
          show-overflow-tooltip
        />
        <template v-if="result && Object.keys(logs[0] || {}).length > 10" #append>
          <div class="more-fields">
            <el-button link type="primary" size="small" @click.stop="showAllFields = !showAllFields">
              {{ showAllFields ? t('logs.table.collapse_fields') : t('logs.table.expand_fields', { n: Object.keys(logs[0] || {}).length }) }}
            </el-button>
          </div>
        </template>
      </el-table>

      <div class="pager">
        <!-- sls/es 才有 offset 翻页（响应 paged 字段）；cls/loki 单页 -->
        <el-pagination
          v-if="!result?.is_sql && result?.paged !== false"
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[20, 50, 100]"
          layout="sizes, prev, pager, next"
          background
        />
        <div v-else style="flex: 1" />
        <span v-if="result" class="sub">
          {{ result.is_sql ? t('logs.result.sql_rows', { n: result.logs.length }) : t('logs.result.returned', { n: result.logs.length }) }}
        </span>
      </div>

      <el-dialog v-model="detailVisible" :title="t('logs.detail.title')" width="760px">
        <pre class="detail-json">{{ JSON.stringify(detailRow, null, 2) }}</pre>
      </el-dialog>

      <el-dialog v-model="saveDialogVisible" :title="t('logs.save.title')" width="520px">
        <el-form label-width="90px">
          <el-form-item :label="t('logs.save.name')" required>
            <el-input v-model="saveName" :placeholder="t('logs.save.name_ph')" maxlength="64" />
          </el-form-item>
          <el-form-item :label="t('logs.save.content')">
            <div class="save-preview">
              <div class="sub">{{ saveContext }}</div>
              <code v-html="highlightQuery(queryText || '*')" />
            </div>
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="saveDialogVisible = false">{{ t('logs.action.cancel') }}</el-button>
          <el-button type="primary" :loading="saving" @click="saveQuery">{{ t('logs.action.save') }}</el-button>
        </template>
      </el-dialog>
    </template>
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  gap: 12px;
  margin-bottom: 12px;
  align-items: center;
}
.src-name {
  float: left;
}
.src-meta {
  float: right;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  margin-left: 12px;
}
.src-status {
  float: right;
  margin-left: 8px;
}
.k8s-note {
  margin-bottom: 10px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.guide-box {
  padding: 60px 20px;
  text-align: center;
}
.guide-title {
  font-size: 16px;
  font-weight: 600;
  margin-bottom: 8px;
}
.guide-sub {
  font-size: 13px;
  color: var(--el-text-color-secondary);
  margin-bottom: 16px;
}
.examples,
.saved {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 10px;
}
.ex-label {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
.ex-tag {
  cursor: pointer;
  font-family: inherit;
}
.ex-tag :deep(.q-kw) {
  color: var(--el-color-warning);
  font-weight: 600;
}
.ex-tag :deep(.q-field) {
  color: var(--el-color-primary);
}
.ex-tag :deep(.q-str) {
  color: var(--el-color-success);
}
.saved-tip :deep(.q-kw) {
  color: #f5c67b;
  font-weight: 600;
}
.saved-tip :deep(.q-field) {
  color: #8cc5ff;
}
.saved-tip :deep(.q-str) {
  color: #a4da89;
}
.syntax-help {
  font-size: 13px;
  line-height: 1.6;
}
.syntax-help ul {
  padding-left: 18px;
  margin: 4px 0 8px;
}
.syntax-help code {
  background: var(--el-fill-color-light);
  padding: 0 4px;
  border-radius: 3px;
}
.more-fields {
  text-align: center;
  padding: 2px 0;
}
.empty-box {
  padding: 24px 0;
  text-align: center;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.pager {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 10px;
}
.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.save-preview {
  width: 100%;
  background: var(--el-fill-color-light);
  border-radius: 6px;
  padding: 8px 10px;
  word-break: break-all;
}
.save-preview :deep(.q-kw) {
  color: var(--el-color-warning);
  font-weight: 600;
}
.save-preview :deep(.q-field) {
  color: var(--el-color-primary);
}
.save-preview :deep(.q-str) {
  color: var(--el-color-success);
}
.detail-json {
  background: var(--el-fill-color-light);
  border-radius: 6px;
  padding: 12px;
  max-height: 60vh;
  overflow: auto;
  font-size: 12px;
  line-height: 1.5;
}
</style>
