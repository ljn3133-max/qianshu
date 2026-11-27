<script setup>
import { computed, nextTick, onMounted, reactive, ref } from 'vue'
import html2pdf from 'html2pdf.js'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import MarkdownIt from 'markdown-it'

import { listChannels, testChannel } from '@/api/alerts'
import { generateReport, getPushSettings, getReport, inspectionStatus, listRuns, runDetail, triggerInspection, updatePushSettings } from '@/api/inspection'
import { fmtTime, levelLabel } from '@/utils/format'
import { useAuthStore } from '@/stores/auth'
import { t } from '@/i18n'

const auth = useAuthStore()
const canWrite = computed(() => ['admin', 'operator'].includes(auth.role))
const router = useRouter()

// 一键诊断：把巡检事实拼成问题带去诊断页（Agent 只读取证）
function diagFrom(i) {
  // 问题措辞跟事实类型走：成本类问消耗/费用影响，其余按链路故障排查
  const suffix = i.category === 'cost'
    ? t('inspections.facts.diagCostSuffix')
    : t('inspections.facts.diagLinkSuffix')
  const q = t('inspections.facts.diagQuestion', { title: i.title, detail: (i.detail || '').slice(0, 150), suffix })
  router.push({ path: '/diagnose', query: { q: q.slice(0, 500) } })
}
const isAdmin = computed(() => auth.role === 'admin')

const activeTab = ref('facts')
const md = new MarkdownIt({ html: false, breaks: true })

/* ---------------- 运行状态 + 手动巡检 ---------------- */
const status = ref(null)
const running = ref(false)

async function loadStatus() {
  status.value = await inspectionStatus()
}

async function doRun() {
  running.value = true
  try {
    const r = await triggerInspection()
    ElMessage.success(t('inspections.msg.runDone', { n: r.item_count, s: (r.duration_ms / 1000).toFixed(0) }))
    currentRunId.value = r.run_id
    await Promise.all([loadStatus(), loadRuns(), loadItems()])
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('inspections.msg.runFailed'))
  } finally {
    running.value = false
  }
}

/* ---------------- 运行历史 ---------------- */
const runs = ref([])
const runsTotal = ref(0)
const runsPage = reactive({ page: 1, pageSize: 10 })

async function loadRuns() {
  const r = await listRuns({ page: runsPage.page, page_size: runsPage.pageSize })
  runs.value = r.items
  runsTotal.value = r.total
  if (!currentRunId.value && runs.value.length) {
    currentRunId.value = runs.value[0].id
    await loadItems()
  }
}

function selectRun(id) {
  currentRunId.value = id
  loadItems()
}

/* ---------------- 事实清单 ---------------- */
const CATEGORIES = [
  { value: '', label: t('inspections.filter.all') },
  { value: 'cost', label: t('inspections.filter.catCost') },
  { value: 'health', label: t('inspections.filter.catHealth') },
  { value: 'slb', label: t('inspections.filter.catSlb') },
  { value: 'logs', label: t('inspections.filter.catLogs') },
  { value: 'idle', label: t('inspections.filter.catIdle') },
]
const CATEGORY_LABELS = Object.fromEntries(CATEGORIES.map(c => [c.value, c.label]))
const LEVELS = [
  { value: '', label: t('inspections.filter.all') },
  { value: 'critical', label: t('inspections.filter.levelCritical') },
  { value: 'warning', label: t('inspections.filter.levelWarning') },
  { value: 'info', label: t('inspections.filter.levelInfo') },
]
const LEVEL_TAGS = { critical: 'danger', warning: 'warning', info: 'info' }
const CHANNEL_TYPES = { wecom_bot: t('inspections.push.chanWecom'), dingtalk_bot: t('inspections.push.chanDingtalk'), webhook: 'Webhook' }

const currentRunId = ref(null)
const category = ref('')
const level = ref('')
const currentRun = ref(null)
const counts = ref({})
const items = ref([])
const itemsLoading = ref(false)

const levelOrder = { critical: 0, warning: 1, info: 2 }
const displayItems = computed(() =>
  [...items.value].sort((a, b) =>
    (levelOrder[a.level] - levelOrder[b.level]) || a.id - b.id))

async function loadItems() {
  if (!currentRunId.value) return
  itemsLoading.value = true
  try {
    const r = await runDetail(currentRunId.value, {
      category: category.value, level: level.value,
    })
    currentRun.value = r.run
    counts.value = r.counts
    items.value = r.items
  } finally {
    itemsLoading.value = false
  }
}

function countFor(cat) {
  return cat ? (counts.value[cat] || 0) : Object.values(counts.value).reduce((a, b) => a + b, 0)
}

/* ---------------- 巡检日报 ---------------- */
const report = ref(null)
const reportLoading = ref(false)
const generating = ref(false)
const notifyOnGenerate = ref(false)

async function loadReport() {
  reportLoading.value = true
  try {
    const r = await getReport()
    report.value = r.report
  } finally {
    reportLoading.value = false
  }
}

async function doGenerate() {
  generating.value = true
  try {
    const r = await generateReport({ notify: notifyOnGenerate.value })
    ElMessage.success(t('inspections.msg.reportDone', { s: (r.duration_ms / 1000).toFixed(0) }))
    await loadReport()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('inspections.msg.reportFailed'))
  } finally {
    generating.value = false
  }
}

const renderedReport = computed(() =>
  report.value?.content_md ? md.render(report.value.content_md) : '')

/* ---------------- 日报 PDF 导出：复用诊断页 teleport+全屏白底，避免裁切或空白 ---------------- */
const exportDialog = reactive({ visible: false, version: 'full', generating: false })
const exportBox = ref(null)
const IPV4_RE = /\b(?:\d{1,3}\.){3}\d{1,3}\b/g

const exportHtml = computed(() => {
  if (!report.value?.content_md) return ''
  const src = exportDialog.version === 'redacted'
    ? report.value.content_md.replace(IPV4_RE, 'x.x.x.x')
    : report.value.content_md
  return md.render(src)
})

async function doExport() {
  exportDialog.generating = true
  try {
    await nextTick()
    await new Promise((resolve) => setTimeout(resolve, 400))   // 等渲染稳定（字体/markdown）
    const filename = exportDialog.version === 'redacted'
      ? t('inspections.export.fileRedacted', { date: report.value.report_date })
      : t('inspections.export.fileFull', { date: report.value.report_date })
    await html2pdf().set({
      margin: [10, 10, 14, 10],
      filename,
      image: { type: 'jpeg', quality: 0.95 },
      html2canvas: { scale: 2, useCORS: true, backgroundColor: '#ffffff', windowWidth: 900 },
      jsPDF: { unit: 'mm', format: 'a4', orientation: 'portrait' },
      pagebreak: { mode: ['css', 'legacy'], avoid: ['.markdown-body'] },
    }).from(exportBox.value).save()
    exportDialog.visible = false
    ElMessage.success(t('inspections.msg.pdfExported'))
  } catch (e) {
    ElMessage.error(t('inspections.msg.pdfFailed', { err: e.message || e }))
  } finally {
    exportDialog.generating = false
  }
}

/* ---------------- 推送设置 ---------------- */
const channels = ref([])
const pushForm = reactive({ enabled: false, channel_ids: [] })
const pushSaving = ref(false)

async function loadPush() {
  const [ps, ch] = await Promise.all([getPushSettings(), listChannels()])
  pushForm.enabled = ps.enabled
  pushForm.channel_ids = ps.channel_ids
  channels.value = Array.isArray(ch) ? ch : (ch.items || [])
}

async function savePush() {
  pushSaving.value = true
  try {
    await updatePushSettings({ enabled: pushForm.enabled, channel_ids: pushForm.channel_ids })
    ElMessage.success(t('inspections.msg.pushSaved'))
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('inspections.msg.saveFailed'))
  } finally {
    pushSaving.value = false
  }
}

// 测试推送：复用通知渠道测试接口（POST /alerts/channels/{id}/test），
// 逐个已选渠道发一条测试消息——只有用户点按钮才发，不做任何自动外发
const testingPush = ref(false)
async function testPush() {
  if (!pushForm.channel_ids.length) return ElMessage.warning(t('inspections.msg.selectChannel'))
  testingPush.value = true
  let okN = 0
  for (const id of pushForm.channel_ids) {
    try {
      await testChannel(id)
      okN += 1
    } catch { /* 拦截器已提示单渠道失败 */ }
  }
  testingPush.value = false
  ElMessage({
    type: okN === pushForm.channel_ids.length ? 'success' : 'warning',
    message: t('inspections.msg.testDone', { ok: okN, total: pushForm.channel_ids.length }),
  })
}

function onTabChange(name) {
  if (name === 'report' && !report.value) loadReport()
  if (name === 'push' && !channels.value.length) loadPush()
}

onMounted(async () => {
  await Promise.all([loadStatus(), loadRuns()])
})
</script>

<template>
  <div class="page">
    <!-- 状态栏 -->
    <el-card shadow="never" class="status-card">
      <div class="status-row">
        <div>
          <div class="status-title">
            {{ t('inspections.status.title') }}
            <el-tag v-if="status?.running" type="warning" size="small">{{ t('inspections.status.running') }}</el-tag>
            <el-tag v-else-if="status?.latest_run?.status === 'ok'" type="success" size="small">{{ t('inspections.status.ok') }}</el-tag>
            <!-- 最近一轮失败时此前不显示任何状态（看着像正常的），补失败徽标 -->
            <el-tag v-else-if="status?.latest_run?.status === 'error'" type="danger" size="small">{{ t('inspections.runs.statusError') }}</el-tag>
          </div>
          <!-- 08:10/08:30 为默认值（backend config INSPECTION_HOUR/MINUTE、INSPECTION_REPORT_HOUR/MINUTE 可配） -->
          <div class="status-sub">
            {{ t('inspections.status.schedule') }}
            {{ status?.latest_run ? fmtTime(status.latest_run.started_at) : t('inspections.status.none') }}
            <template v-if="status?.latest_run">{{ t('inspections.status.latestFacts', { n: status.latest_run.item_count }) }}</template>
          </div>
        </div>
        <el-button v-if="canWrite" type="primary" :loading="running" @click="doRun">
          {{ t('inspections.status.runNow') }}
        </el-button>
      </div>
    </el-card>

    <el-tabs v-model="activeTab" @tab-change="onTabChange">
      <el-tab-pane :label="t('inspections.facts.title')" name="facts">
        <div class="main-grid">
      <!-- 左：运行历史 -->
      <el-card shadow="never" class="runs-card">
        <template #header>{{ t('inspections.runs.header', { n: runsTotal }) }}</template>
        <el-table stripe :data="runs" size="small" highlight-current-row
                  @current-change="(row) => row && selectRun(row.id)">
          <el-table-column prop="id" label="ID" width="52" />
          <el-table-column :label="t('inspections.runs.colDate')" width="96">
            <template #default="{ row }">{{ row.run_date }}</template>
          </el-table-column>
          <el-table-column :label="t('inspections.runs.colStart')" width="140">
            <template #default="{ row }">{{ fmtTime(row.started_at) }}</template>
          </el-table-column>
          <el-table-column :label="t('inspections.runs.colFacts')" width="56" align="center">
            <template #default="{ row }">{{ row.item_count }}</template>
          </el-table-column>
          <el-table-column :label="t('inspections.runs.colStatus')" width="80">
            <template #default="{ row }">
              <el-tag :type="row.status === 'ok' ? 'success' : 'danger'" size="small">
                {{ row.status === 'ok' ? t('inspections.runs.statusOk') : t('inspections.runs.statusError') }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column :label="t('inspections.runs.colError')">
            <template #default="{ row }">
              <span v-if="row.error" class="err-text" :title="row.error">{{ t('inspections.runs.scanError') }}</span>
              <span v-else>-</span>
            </template>
          </el-table-column>
        </el-table>
        <el-pagination v-if="runsTotal > runsPage.pageSize" layout="prev, pager, next" small
                       :total="runsTotal" :page-size="runsPage.pageSize"
                       v-model:current-page="runsPage.page" @current-change="loadRuns"
                       class="pager" />
      </el-card>

      <!-- 右：事实清单 -->
      <el-card shadow="never" class="items-card">
        <template #header>
          <div class="items-header">
            <span>{{ t('inspections.facts.title') }} <span v-if="currentRun" class="muted">run #{{ currentRun.id }}</span></span>
            <div class="filters">
              <el-select v-model="category" size="small" style="width: 120px" @change="loadItems">
                <el-option v-for="c in CATEGORIES" :key="c.value" :value="c.value"
                           :label="c.value ? `${c.label} (${countFor(c.value)})` : `${t('inspections.filter.all')} (${countFor('')})`" />
              </el-select>
              <el-select v-model="level" size="small" style="width: 90px" @change="loadItems">
                <el-option v-for="l in LEVELS" :key="l.value" :value="l.value" :label="l.label" />
              </el-select>
            </div>
          </div>
        </template>
        <div v-loading="itemsLoading" class="items-body">
          <el-empty v-if="!itemsLoading && !displayItems.length">
            <template #description>
              <!-- 筛选无命中≠无事实：区分"筛选无结果"与"当前巡检无条目" -->
              <template v-if="currentRunId && (category || level)">{{ t('inspections.empty.noMatch') }}</template>
              <template v-else-if="currentRunId">{{ t('inspections.empty.noItems') }}</template>
              <template v-else>{{ runs.length ? t('inspections.empty.selectRun') : t('inspections.empty.neverRun') }}</template>
            </template>
            <el-button v-if="currentRunId && (category || level)" size="small"
                       @click="category = ''; level = ''; loadItems()">{{ t('inspections.filter.clear') }}</el-button>
          </el-empty>
          <div v-for="i in displayItems" :key="i.id" class="fact-item" :class="`lv-${i.level}`">
            <div class="fact-head">
              <el-tag :type="LEVEL_TAGS[i.level]" size="small" effect="dark">{{ levelLabel(i.level) }}</el-tag>
              <span class="fact-cat">{{ CATEGORY_LABELS[i.category] || i.category }}</span>
              <el-tag v-for="b in i.biz" :key="b" type="warning" size="small" effect="dark" style="margin-right: 2px">{{ b }}</el-tag>
              <span class="fact-title">{{ i.title }}</span>
              <el-button v-if="i.level !== 'info' && canWrite" size="small" type="primary" text
                         style="margin-left: auto" @click="diagFrom(i)">{{ t('inspections.facts.diagnose') }}</el-button>
            </div>
            <div v-if="i.detail" class="fact-detail">{{ i.detail }}</div>
          </div>
        </div>
      </el-card>
        </div>
      </el-tab-pane>

      <el-tab-pane :label="t('inspections.report.title')" name="report">
        <el-card shadow="never" v-loading="reportLoading">
          <template #header>
            <div class="report-header">
              <span v-if="report">
                {{ t('inspections.report.headDate', { date: report.report_date }) }}
                <el-tag :type="LEVEL_TAGS[report.level] || 'info'" size="small">{{ levelLabel(report.level) }}</el-tag>
                <span class="muted">{{ report.provider_label }} / {{ report.model }}</span>
              </span>
              <span v-else>{{ t('inspections.report.title') }}</span>
              <div class="report-actions">
                <el-button v-if="report && report.status !== 'error'" size="small" @click="exportDialog.visible = true">
                  {{ t('inspections.report.exportPdf') }}
                </el-button>
                <template v-if="canWrite">
                  <el-checkbox v-model="notifyOnGenerate" size="small">{{ t('inspections.report.notify') }}</el-checkbox>
                  <el-button type="primary" size="small" :loading="generating" @click="doGenerate">
                    {{ report ? t('inspections.report.regenerate') : t('inspections.report.generate') }}
                  </el-button>
                </template>
              </div>
            </div>
          </template>
          <el-empty v-if="!reportLoading && !report"
                    :description="t('inspections.report.empty')" />
          <el-alert v-else-if="report && report.status === 'error'" type="error" :closable="false"
                    :title="t('inspections.report.genFailed', { err: report.error })" />
          <!-- eslint-disable-next-line vue/no-v-html -->
          <div v-else-if="report" class="markdown-body report-body" v-html="renderedReport" />
        </el-card>
      </el-tab-pane>

      <el-tab-pane v-if="isAdmin" :label="t('inspections.push.tab')" name="push">
        <el-card shadow="never">
          <template #header>{{ t('inspections.push.title') }}</template>
          <el-form label-width="140px" style="max-width: 640px">
            <el-form-item :label="t('inspections.push.botSwitch')">
              <el-switch v-model="pushForm.enabled" />
              <div class="form-tip">{{ t('inspections.push.tipSwitch') }}</div>
            </el-form-item>
            <el-form-item :label="t('inspections.push.channels')">
              <el-select v-model="pushForm.channel_ids" multiple :placeholder="t('inspections.push.selectChannel')" style="width: 100%">
                <!-- 已停用渠道禁选：停用=不发，选了也是空转 -->
                <el-option v-for="c in channels" :key="c.id" :value="c.id" :disabled="!c.enabled"
                           :label="c.enabled ? t('inspections.push.channelLabel', { name: c.name, type: CHANNEL_TYPES[c.type] || c.type }) : t('inspections.push.channelDisabled', { name: c.name, type: CHANNEL_TYPES[c.type] || c.type })" />
              </el-select>
              <div class="form-tip">{{ t('inspections.push.tipChannels') }}</div>
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="pushSaving" @click="savePush">{{ t('inspections.actions.save') }}</el-button>
              <el-button :loading="testingPush" :disabled="!pushForm.channel_ids.length" @click="testPush">{{ t('inspections.push.test') }}</el-button>
              <span class="form-tip" style="margin-left: 8px">{{ t('inspections.push.tipTest') }}</span>
            </el-form-item>
          </el-form>
        </el-card>
      </el-tab-pane>
    </el-tabs>

    <!-- ============ 日报 PDF 导出弹窗 ============ -->
    <el-dialog v-model="exportDialog.visible" :title="t('inspections.export.title')" width="480px">
      <el-radio-group v-model="exportDialog.version">
        <div style="display: flex; flex-direction: column; gap: 10px">
          <el-radio value="full">{{ t('inspections.export.optFull') }}</el-radio>
          <el-radio value="redacted">{{ t('inspections.export.optRedacted') }}</el-radio>
        </div>
      </el-radio-group>
      <template #footer>
        <el-button @click="exportDialog.visible = false">{{ t('inspections.actions.cancel') }}</el-button>
        <el-button type="primary" :loading="exportDialog.generating" @click="doExport">
          {{ exportDialog.generating ? t('inspections.export.generating') : t('inspections.export.submit') }}
        </el-button>
      </template>
    </el-dialog>

    <!-- teleport 到 body：脱离 el-main 的 overflow 容器，否则 html2canvas 截出空白 PDF -->
    <teleport to="body">
      <div v-if="exportDialog.generating" class="pdf-overlay">
        <div ref="exportBox" class="pdf-root">
          <div class="pdf-page">
            <h1 class="pdf-title">{{ t('inspections.export.pdfTitle', { date: report?.report_date }) }}</h1>
            <div class="pdf-meta-line">
              {{ exportDialog.version === 'redacted' ? t('inspections.export.metaRedacted') : t('inspections.export.metaFull') }}
            </div>
            <div class="markdown-body" v-html="exportHtml" />
          </div>
        </div>
      </div>
    </teleport>
  </div>
</template>

<style scoped>
/* PDF 导出层（与诊断页同款：A4 宽 794px @96dpi） */
.pdf-overlay {
  position: fixed;
  inset: 0;
  background: var(--el-bg-color);
  z-index: 3000;
  overflow: auto;
  display: flex;
  justify-content: center;
}
.pdf-root {
  width: 794px;
  flex-shrink: 0;
  background: var(--el-bg-color);
  color: #1f2329;
}
.pdf-page {
  padding: 24px 28px;
}
.pdf-title {
  font-size: 22px;
  text-align: center;
  margin: 0 0 10px;
  border-bottom: 2px solid #3370ff;
  padding-bottom: 10px;
}
.pdf-meta-line {
  font-size: 12px;
  color: #646a73;
  margin-bottom: 12px;
}
.page {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.status-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.status-title {
  font-size: 15px;
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: 8px;
}
.status-sub {
  margin-top: 4px;
  font-size: 12px;
  opacity: 0.7;
}
.main-grid {
  display: grid;
  grid-template-columns: 420px 1fr;
  gap: 12px;
  align-items: start;
}
.runs-card :deep(.el-card__body) {
  padding: 8px;
}
.pager {
  margin-top: 8px;
  justify-content: center;
}
.err-text {
  color: var(--el-color-danger);
  font-size: 12px;
}
.items-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.filters {
  display: flex;
  gap: 8px;
}
.muted {
  opacity: 0.6;
  font-weight: normal;
  font-size: 12px;
}
.items-body {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-height: 200px;
  max-height: calc(100vh - 260px);
  overflow-y: auto;
}
.fact-item {
  border: 1px solid var(--el-border-color);
  border-left: 3px solid var(--el-border-color);
  border-radius: 6px;
  padding: 8px 12px;
}
.fact-item.lv-critical {
  border-left-color: var(--el-color-danger);
}
.fact-item.lv-warning {
  border-left-color: var(--el-color-warning);
}
.fact-item.lv-info {
  border-left-color: var(--el-color-info);
}
.fact-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.fact-cat {
  font-size: 12px;
  opacity: 0.65;
  white-space: nowrap;
}
.fact-title {
  font-size: 13px;
  word-break: break-all;
}
.fact-detail {
  margin-top: 6px;
  font-size: 12px;
  opacity: 0.75;
  white-space: pre-wrap;
  word-break: break-all;
}
.report-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
}
.report-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}
.report-body {
  font-size: 13px;
  line-height: 1.7;
}
.report-body :deep(h1),
.report-body :deep(h2) {
  font-size: 15px;
  margin: 14px 0 8px;
}
.report-body :deep(h3) {
  font-size: 13px;
  margin: 10px 0 6px;
}
.report-body :deep(table) {
  border-collapse: collapse;
  margin: 8px 0;
}
.report-body :deep(th),
.report-body :deep(td) {
  border: 1px solid var(--el-border-color);
  padding: 4px 10px;
  font-size: 12px;
}
.report-body :deep(code) {
  background: var(--el-fill-color);
  padding: 1px 4px;
  border-radius: 3px;
  font-size: 12px;
}
.form-tip {
  font-size: 12px;
  opacity: 0.6;
  line-height: 1.4;
  margin-top: 2px;
}
</style>
