<script setup>
import { computed, nextTick, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import MarkdownIt from 'markdown-it'
import html2pdf from 'html2pdf.js'

import { listHosts } from '@/api/link'
import { createDiagCase, diagCaseDetail, diagSessionDetail, listDiagCases, listDiagSessions, runDiagnosis, updateDiagCase } from '@/api/diagnosis'
import { fmtTime } from '@/utils/format'
import { acceptCase } from '@/api/diagnosis'
import { useAuthStore } from '@/stores/auth'
import DiagReport from '@/views/DiagReport.vue'
import { t } from '@/i18n'

const auth = useAuthStore()
const route = useRoute()
const canWrite = computed(() => ['admin', 'operator'].includes(auth.role))
const md = new MarkdownIt({ html: false, breaks: true })
const activeTab = ref('diagnose')

const LEVEL_TAGS = { critical: 'danger', warning: 'warning', ok: 'success', unknown: 'info' }
const LEVEL_LABELS = { critical: t('diagnose.status.critical'), warning: t('diagnose.status.warning'), ok: t('diagnose.status.ok'), unknown: t('diagnose.status.unknown') }
const SCENARIOS = { link_fault: t('diagnose.scenario.linkFault'), cost_analysis: t('diagnose.scenario.costAnalysis'), dba: t('diagnose.scenario.dba') }
const scenarioLabel = (s) => SCENARIOS[s] || t('diagnose.scenario.linkFault')

const fmtSec = (ms) => ((ms || 0) / 1000).toFixed(0) + 's'

/* ==================== 发起诊断 ==================== */
const question = ref('')
const running = ref(false)
// current: 正在看的报告对象（session 详情或 case 详情）；source 区分两者
const current = ref(null)
const currentSource = ref('session')   // 'session' | 'case'
const detailLoading = ref(false)
const suggestions = ref([])

async function loadSuggestions() {
  try {
    const r = await listHosts({})
    const hosts = r.items || []
    // 只对异常主机生成建议，避免给健康主机拼"打不开"等误导性故障话术
    const abnormal = hosts.filter(h => h.level !== 'ok')
    suggestions.value = abnormal.slice(0, 8).map(h => t('diagnose.form.suggestion', { host: h.host }))
  } catch {
    suggestions.value = []
  }
}

async function doRun() {
  const q = question.value.trim()
  if (q.length < 2) {
    ElMessage.warning(t('diagnose.msg.tooShort'))
    return
  }
  running.value = true
  current.value = null
  try {
    const r = await runDiagnosis({ question: q })
    current.value = r
    currentSource.value = 'session'
    ElMessage.success(t('diagnose.msg.runDone', { s: (r.duration_ms / 1000).toFixed(0), n: r.tool_calls }))
    await Promise.all([loadHistory(), loadCases()])
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('diagnose.msg.runFail'))
  } finally {
    running.value = false
  }
}

/* ==================== 诊断历史 ==================== */
const history = ref([])
const historyTotal = ref(0)
const page = reactive({ page: 1, pageSize: 10 })

async function loadHistory() {
  const r = await listDiagSessions({ page: page.page, page_size: page.pageSize })
  history.value = r.items
  historyTotal.value = r.total
}

async function openHistory(row) {
  detailLoading.value = true
  try {
    current.value = await diagSessionDetail(row.id)
    currentSource.value = 'session'
    if (current.value.case_id) {
      activeTab.value = 'cases'
      await openCase({ id: current.value.case_id })
    } else {
      activeTab.value = 'diagnose'
    }
  } finally {
    detailLoading.value = false
  }
}

/* ==================== 案例库 ==================== */
const cases = ref([])
const casesTotal = ref(0)
const casePage = reactive({ page: 1, pageSize: 10 })
const caseFilter = reactive({ keyword: '', host: '', level: '' })

// 采纳 AI 建议为经验：必须经人确认才进经验库
async function acceptCaseFor(row) {
  try {
    await acceptCase(row.id)
    ElMessage.success(t('diagnose.msg.accepted'))
    loadCases()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('diagnose.msg.opFail'))
  }
}

async function loadCases() {
  const r = await listDiagCases({ ...caseFilter, page: casePage.page, page_size: casePage.pageSize })
  cases.value = r.items
  casesTotal.value = r.total
}

async function openCase(row) {
  detailLoading.value = true
  try {
    current.value = await diagCaseDetail(row.id)
    currentSource.value = 'case'
  } finally {
    detailLoading.value = false
  }
}

/* ---------- 入库弹窗 ---------- */
const caseDialog = reactive({ visible: false, submitting: false, sessionId: null, preview: null, root_cause: '', solution: '', tags: '' })

function openCaseDialog() {
  if (!current.value || currentSource.value !== 'session') return
  caseDialog.sessionId = current.value.id
  caseDialog.preview = current.value
  caseDialog.root_cause = ''
  caseDialog.solution = ''
  caseDialog.tags = ''
  caseDialog.visible = true
}

async function submitCase() {
  caseDialog.submitting = true
  try {
    const c = await createDiagCase({
      session_id: caseDialog.sessionId,
      root_cause: caseDialog.root_cause,
      solution: caseDialog.solution,
      tags: caseDialog.tags,
    })
    caseDialog.visible = false
    ElMessage.success(t('diagnose.msg.saved', { id: c.id }))
    current.value.case_id = c.id
    await Promise.all([loadHistory(), loadCases()])
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('diagnose.msg.saveFail'))
  } finally {
    caseDialog.submitting = false
  }
}

/* ---------- 复盘编辑弹窗 ---------- */
const editDialog = reactive({ visible: false, submitting: false, id: null, root_cause: '', solution: '', tags: '' })

function openEditDialog() {
  if (!current.value || currentSource.value !== 'case') return
  editDialog.id = current.value.id
  editDialog.root_cause = current.value.root_cause || ''
  editDialog.solution = current.value.solution || ''
  editDialog.tags = current.value.tags || ''
  editDialog.visible = true
}

async function submitEdit() {
  editDialog.submitting = true
  try {
    const c = await updateDiagCase(editDialog.id, {
      root_cause: editDialog.root_cause,
      solution: editDialog.solution,
      tags: editDialog.tags,
    })
    editDialog.visible = false
    ElMessage.success(t('diagnose.msg.updated'))
    current.value = c
    await loadCases()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('diagnose.msg.updateFail'))
  } finally {
    editDialog.submitting = false
  }
}

/* ==================== PDF 导出 ==================== */
const exportDialog = reactive({ visible: false, version: 'full', generating: false })
const exportBox = ref(null)
const exportData = ref(null)

function openExportDialog() {
  if (!current.value) return
  exportDialog.version = 'full'
  exportDialog.visible = true
}

const IPV4_RE = /\b(?:\d{1,3}\.){3}\d{1,3}\b/g

function redactDeep(obj) {
  if (typeof obj === 'string') return obj.replace(IPV4_RE, 'x.x.x.x')
  if (Array.isArray(obj)) return obj.map(redactDeep)
  if (obj && typeof obj === 'object') {
    const out = {}
    for (const [k, v] of Object.entries(obj)) out[k] = redactDeep(v)
    return out
  }
  return obj
}

async function doExport() {
  exportDialog.generating = true
  try {
    const r = current.value
    exportData.value = exportDialog.version === 'redacted' ? redactDeep(JSON.parse(JSON.stringify(r))) : r
    await nextTick()
    await new Promise(resolve => setTimeout(resolve, 400))   // 等渲染稳定（字体/markdown）
    const host = r.host || t('diagnose.status.unknownHost')
    const ts = (r.diagnosed_at || r.created_at || '').replace(/[-:T]/g, '').slice(0, 14)
    const filename = t('diagnose.export.filename', {
      host,
      version: exportDialog.version === 'redacted' ? t('diagnose.export.redactedShort') : t('diagnose.export.fullShort'),
      ts,
    })
    await html2pdf().set({
      margin: [10, 10, 14, 10],
      filename,
      image: { type: 'jpeg', quality: 0.95 },
      html2canvas: { scale: 2, useCORS: true, backgroundColor: '#ffffff', windowWidth: 900 },
      jsPDF: { unit: 'mm', format: 'a4', orientation: 'portrait' },
      pagebreak: { mode: ['css', 'legacy'], avoid: ['.pdf-step', '.pdf-md'] },
    }).from(exportBox.value).save()
    exportDialog.visible = false
    ElMessage.success(t('diagnose.msg.pdfExported'))
  } catch (e) {
    ElMessage.error(t('diagnose.msg.pdfFail', { err: e.message || e }))
  } finally {
    exportDialog.generating = false
  }
}

onMounted(async () => {
  // 一键诊断直达：其他页面带 ?q= 跳过来，预填问题并自动开跑（不用再点一次）
  const q = (route.query.q || '').toString().trim()
  if (q) {
    question.value = q.slice(0, 500)
    if (canWrite.value) doRun()
  }
  await Promise.all([loadSuggestions(), loadHistory(), loadCases()])
})
</script>

<template>
  <div class="page">
    <el-tabs v-model="activeTab" class="diag-tabs">
      <!-- ============ 页签1：诊断 ============ -->
      <el-tab-pane :label="t('diagnose.tabs.diagnose')" name="diagnose">
        <el-card shadow="never">
          <template #header>
            <div class="card-header">
              <div>
                <span style="font-weight: 600">{{ t('diagnose.form.title') }}</span>
                <span class="sub">{{ t('diagnose.form.sub') }}</span>
              </div>
            </div>
          </template>

          <el-input v-model="question" type="textarea" :rows="2" maxlength="500" show-word-limit
                    :placeholder="t('diagnose.form.placeholder')"
                    :disabled="!canWrite || running" @keyup.ctrl.enter="doRun" />
          <div style="margin-top: 10px; display: flex; align-items: center; gap: 10px">
            <el-button type="primary" :loading="running" :disabled="!canWrite" @click="doRun">
              {{ running ? t('diagnose.form.running') : t('diagnose.form.start') }}
            </el-button>
            <span class="sub">{{ t('diagnose.form.hotkeyHint') }}</span>
          </div>

          <div v-if="suggestions.length" style="margin-top: 12px">
            <span class="sub" style="margin-left: 0">{{ t('diagnose.form.suggestLabel') }}</span>
            <el-tag v-for="s in suggestions.slice(0, 6)" :key="s" size="small" effect="plain"
                    style="margin: 4px 6px 0 0; cursor: pointer"
                    @click="question = s">
              {{ s.length > 34 ? s.slice(0, 34) + '…' : s }}
            </el-tag>
          </div>
        </el-card>

        <!-- 诊断结果 -->
        <el-card v-if="running" shadow="never" style="margin-top: 16px">
          <el-skeleton :rows="6" animated />
          <div class="sub" style="margin-top: 8px">{{ t('diagnose.form.loading') }}</div>
        </el-card>

        <el-card v-else-if="current && currentSource === 'session'" shadow="never" style="margin-top: 16px" v-loading="detailLoading">
          <template #header>
            <div class="card-header">
              <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap">
                <el-tag :type="LEVEL_TAGS[current.level] || 'info'">{{ LEVEL_LABELS[current.level] || current.level }}</el-tag>
                <el-tag type="info" effect="plain" size="small">{{ scenarioLabel(current.scenario) }}</el-tag>
                <span style="font-weight: 600">{{ t('diagnose.detail.reportTitle') }}</span>
                <span class="sub">{{ fmtTime(current.created_at) }} · {{ fmtSec(current.duration_ms) }} · {{ t('diagnose.detail.toolCalls', { n: current.tool_calls }) }}</span>
                <div style="flex: 1"></div>
                <el-button size="small" @click="openExportDialog">{{ t('diagnose.detail.exportPdf') }}</el-button>
                <el-button v-if="!current.case_id" size="small" type="primary" :disabled="!canWrite" @click="openCaseDialog">
                  {{ t('diagnose.detail.saveToCase') }}
                </el-button>
                <el-button v-else size="small" type="success" @click="activeTab = 'cases'; openCase({ id: current.case_id })">
                  {{ t('diagnose.detail.viewCase') }}
                </el-button>
              </div>
            </div>
          </template>
          <diag-report :report="current" />
        </el-card>

        <!-- 诊断历史 -->
        <el-card shadow="never" style="margin-top: 16px">
          <template #header>
            <div class="card-header">
              <span style="font-weight: 600">{{ t('diagnose.history.title') }}</span>
              <el-pagination v-model:current-page="page.page" :total="historyTotal"
                             :page-size="page.pageSize" layout="total, prev, pager, next"
                             style="margin-right: 0" @current-change="loadHistory" />
            </div>
          </template>
          <el-table stripe :data="history" size="small" border style="cursor: pointer" @row-click="openHistory">
            <el-table-column :label="t('diagnose.table.level')" width="80">
              <template #default="{ row }">
                <el-tag :type="LEVEL_TAGS[row.level] || 'info'" size="small">{{ LEVEL_LABELS[row.level] || row.level }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="question" :label="t('diagnose.table.symptom')" min-width="280" show-overflow-tooltip />
            <el-table-column :label="t('diagnose.table.scenario')" width="110">
              <template #default="{ row }">{{ scenarioLabel(row.scenario) }}</template>
            </el-table-column>
            <el-table-column prop="host" :label="t('diagnose.table.targetDomain')" min-width="160" show-overflow-tooltip>
              <template #default="{ row }">{{ row.host || '-' }}</template>
            </el-table-column>
            <el-table-column :label="t('diagnose.table.toolCalls')" width="70" align="right">
              <template #default="{ row }">{{ row.tool_calls }}</template>
            </el-table-column>
            <el-table-column :label="t('diagnose.table.duration')" width="80" align="right">
              <template #default="{ row }">{{ fmtSec(row.duration_ms) }}</template>
            </el-table-column>
            <el-table-column :label="t('diagnose.table.stored')" width="90">
              <template #default="{ row }">
                <el-tag v-if="row.case_id" type="success" size="small" effect="plain">{{ t('diagnose.status.saved') }}</el-tag>
                <span v-else class="sub">-</span>
              </template>
            </el-table-column>
            <el-table-column :label="t('diagnose.table.time')" width="160">
              <template #default="{ row }">{{ fmtTime(row.created_at) }}
                <el-tag size="small" :type="row.trigger === 'auto' ? 'warning' : 'info'" effect="plain" style="margin-left:4px">
                  {{ row.trigger === 'auto' ? t('diagnose.status.auto') : t('diagnose.status.manual') }}
                </el-tag>
              </template>
            </el-table-column>
          </el-table>
          <div class="sub" style="margin-top: 6px">{{ t('diagnose.history.hint') }}</div>
        </el-card>
      </el-tab-pane>

      <!-- ============ 页签2：案例库 ============ -->
      <el-tab-pane :label="t('diagnose.tabs.cases', { n: casesTotal })" name="cases">
        <el-card shadow="never">
          <div style="display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 12px">
            <el-input v-model="caseFilter.keyword" :placeholder="t('diagnose.form.keywordPlaceholder')" clearable
                      style="width: 260px" @keyup.enter="casePage.page = 1; loadCases()" @clear="casePage.page = 1; loadCases()" />
            <el-input v-model="caseFilter.host" :placeholder="t('diagnose.table.domain')" clearable style="width: 160px"
                      @keyup.enter="casePage.page = 1; loadCases()" @clear="casePage.page = 1; loadCases()" />
            <el-select v-model="caseFilter.level" :placeholder="t('diagnose.table.level')" clearable style="width: 110px"
                       @change="casePage.page = 1; loadCases()">
              <el-option :label="t('diagnose.status.critical')" value="critical" />
              <el-option :label="t('diagnose.status.warning')" value="warning" />
              <el-option :label="t('diagnose.status.ok')" value="ok" />
            </el-select>
            <el-button type="primary" @click="casePage.page = 1; loadCases()">{{ t('diagnose.actions.search') }}</el-button>
          </div>

          <el-table stripe :data="cases" size="small" border style="cursor: pointer" @row-click="openCase">
            <el-table-column :label="t('diagnose.table.level')" width="80">
              <template #default="{ row }">
                <el-tag :type="LEVEL_TAGS[row.level] || 'info'" size="small">{{ LEVEL_LABELS[row.level] || row.level }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column :label="t('diagnose.table.status')" width="90">
              <template #default="{ row }">
                <el-tag size="small" :type="row.status === 'pending' ? 'warning' : 'success'" effect="plain">
                  {{ row.status === 'pending' ? t('diagnose.status.pending') : t('diagnose.status.saved') }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="question" :label="t('diagnose.table.symptom')" min-width="240" show-overflow-tooltip />
            <el-table-column prop="host" :label="t('diagnose.table.domain')" min-width="140" show-overflow-tooltip>
              <template #default="{ row }">{{ row.host || '-' }}</template>
            </el-table-column>
            <el-table-column prop="root_cause" :label="t('diagnose.form.rootCause')" min-width="180" show-overflow-tooltip>
              <template #default="{ row }">{{ row.root_cause || t('diagnose.status.notReviewed') }}</template>
            </el-table-column>
            <el-table-column :label="t('diagnose.form.tags')" min-width="140">
              <template #default="{ row }">
                <el-tag v-for="tag in (row.tags || '').split(',').filter(Boolean)" :key="tag"
                        size="small" effect="plain" style="margin-right: 4px">{{ tag }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column :label="t('diagnose.table.diagnosedAt')" width="160">
              <template #default="{ row }">{{ fmtTime(row.diagnosed_at) }}</template>
            </el-table-column>
            <el-table-column v-if="canWrite" label="" width="80">
              <template #default="{ row }">
                <el-button v-if="row.status === 'pending'" link type="success" size="small"
                           @click.stop="acceptCaseFor(row)">{{ t('diagnose.actions.accept') }}</el-button>
              </template>
            </el-table-column>
          </el-table>
          <div style="display: flex; justify-content: flex-end; margin-top: 10px">
            <el-pagination v-model:current-page="casePage.page" :total="casesTotal"
                           :page-size="casePage.pageSize" layout="total, prev, pager, next"
                           @current-change="loadCases" />
          </div>
        </el-card>

        <!-- 案例详情（与诊断报告同一展示形态） -->
        <el-card v-if="current && currentSource === 'case'" shadow="never" style="margin-top: 16px" v-loading="detailLoading">
          <template #header>
            <div class="card-header">
              <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap">
                <el-tag :type="LEVEL_TAGS[current.level] || 'info'">{{ LEVEL_LABELS[current.level] || current.level }}</el-tag>
                <span style="font-weight: 600">{{ t('diagnose.detail.caseTitle', { id: current.id }) }}</span>
                <span class="sub">{{ fmtTime(current.diagnosed_at) }} · {{ fmtSec(current.duration_ms) }} · {{ t('diagnose.detail.toolCalls', { n: current.tool_calls }) }}</span>
                <div style="flex: 1"></div>
                <el-button size="small" @click="openExportDialog">{{ t('diagnose.detail.exportPdf') }}</el-button>
                <el-button size="small" type="primary" :disabled="!canWrite" @click="openEditDialog">{{ t('diagnose.detail.editReview') }}</el-button>
              </div>
            </div>
          </template>
          <diag-report :report="current" />
        </el-card>
      </el-tab-pane>
    </el-tabs>

    <!-- ============ 入库弹窗 ============ -->
    <el-dialog v-model="caseDialog.visible" :title="t('diagnose.detail.saveToCase')" width="560px">
      <el-alert v-if="caseDialog.preview" type="info" :closable="false" style="margin-bottom: 14px">
        <template #title>
          {{ LEVEL_LABELS[caseDialog.preview.level] || caseDialog.preview.level }} ·
          {{ caseDialog.preview.host || t('diagnose.status.unrecognizedHost') }} · {{ caseDialog.preview.question }}
        </template>
        {{ t('diagnose.dialog.saveAlert') }}
      </el-alert>
      <el-form label-width="90px">
        <el-form-item :label="t('diagnose.form.rootCause')">
          <el-input v-model="caseDialog.root_cause" maxlength="500" :placeholder="t('diagnose.form.rootCausePlaceholder')" />
        </el-form-item>
        <el-form-item :label="t('diagnose.form.solution')">
          <el-input v-model="caseDialog.solution" type="textarea" :rows="3" maxlength="5000"
                    :placeholder="t('diagnose.form.solutionPlaceholder')" />
        </el-form-item>
        <el-form-item :label="t('diagnose.form.tags')">
          <el-input v-model="caseDialog.tags" maxlength="255" :placeholder="t('diagnose.form.tagsPlaceholder')" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="caseDialog.visible = false">{{ t('diagnose.actions.cancel') }}</el-button>
        <el-button type="primary" :loading="caseDialog.submitting" @click="submitCase">{{ t('diagnose.actions.submit') }}</el-button>
      </template>
    </el-dialog>

    <!-- ============ 复盘编辑弹窗 ============ -->
    <el-dialog v-model="editDialog.visible" :title="t('diagnose.dialog.editTitle')" width="560px">
      <el-form label-width="90px">
        <el-form-item :label="t('diagnose.form.rootCause')">
          <el-input v-model="editDialog.root_cause" maxlength="500" />
        </el-form-item>
        <el-form-item :label="t('diagnose.form.solution')">
          <el-input v-model="editDialog.solution" type="textarea" :rows="3" maxlength="5000" />
        </el-form-item>
        <el-form-item :label="t('diagnose.form.tags')">
          <el-input v-model="editDialog.tags" maxlength="255" :placeholder="t('diagnose.form.tagsPlaceholderShort')" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editDialog.visible = false">{{ t('diagnose.actions.cancel') }}</el-button>
        <el-button type="primary" :loading="editDialog.submitting" @click="submitEdit">{{ t('diagnose.actions.save') }}</el-button>
      </template>
    </el-dialog>

    <!-- ============ PDF 导出弹窗 ============ -->
    <el-dialog v-model="exportDialog.visible" :title="t('diagnose.dialog.exportTitle')" width="480px">
      <el-radio-group v-model="exportDialog.version">
        <div style="display: flex; flex-direction: column; gap: 10px">
          <el-radio value="full">{{ t('diagnose.dialog.exportFull') }}</el-radio>
          <el-radio value="redacted">{{ t('diagnose.dialog.exportRedacted') }}</el-radio>
        </div>
      </el-radio-group>
      <div class="sub" style="margin-top: 12px">{{ t('diagnose.dialog.exportHint') }}</div>
      <template #footer>
        <el-button @click="exportDialog.visible = false">{{ t('diagnose.actions.cancel') }}</el-button>
        <el-button type="primary" :loading="exportDialog.generating" @click="doExport">
          {{ exportDialog.generating ? t('diagnose.export.generating') : t('diagnose.actions.export') }}
        </el-button>
      </template>
    </el-dialog>

    <!-- ============ PDF 渲染层（生成期间全屏白色预览）============
         teleport 到 body：脱离 el-main 的 overflow 布局容器，否则 html2canvas
         截图区域被祖先裁剪成空白（3KB 空白 PDF 的根因） -->
    <teleport to="body">
      <div v-if="exportDialog.generating" class="pdf-overlay">
        <div ref="exportBox" class="pdf-root">
          <div v-if="exportData" class="pdf-page">
        <h1 class="pdf-title">{{ t('diagnose.export.title') }}</h1>
        <table class="pdf-meta">
          <tr>
            <td class="k">{{ t('diagnose.table.symptom') }}</td><td>{{ exportData.question }}</td>
          </tr>
          <tr>
            <td class="k">{{ t('diagnose.table.diagnosedAt') }}</td><td>{{ fmtTime(exportData.diagnosed_at || exportData.created_at) }}</td>
            <td class="k">{{ t('diagnose.export.totalDuration') }}</td><td>{{ fmtSec(exportData.duration_ms) }}</td>
            <td class="k">{{ t('diagnose.export.toolCallsCount') }}</td><td>{{ exportData.tool_calls }}</td>
          </tr>
          <tr>
            <td class="k">{{ t('diagnose.table.targetDomain') }}</td><td>{{ exportData.host || '-' }}</td>
            <td class="k">{{ t('diagnose.table.level') }}</td><td>{{ LEVEL_LABELS[exportData.level] || exportData.level }}</td>
            <td class="k">{{ t('diagnose.export.version') }}</td><td>{{ exportDialog.version === 'redacted' ? t('diagnose.export.redacted') : t('diagnose.export.full') }}</td>
          </tr>
        </table>

        <div v-if="exportData.root_cause !== undefined" class="pdf-section">
          <h2>{{ t('diagnose.export.review') }}</h2>
          <p><b>{{ t('diagnose.export.rootCauseLabel') }}</b>{{ exportData.root_cause || t('diagnose.status.notFilled') }}</p>
          <p><b>{{ t('diagnose.export.solutionLabel') }}</b>{{ exportData.solution || t('diagnose.status.notFilled') }}</p>
          <p><b>{{ t('diagnose.export.tagsLabel') }}</b>{{ exportData.tags || t('diagnose.status.none') }}</p>
        </div>

        <div class="pdf-section">
          <h2>{{ t('diagnose.export.trail') }}</h2>
          <div v-for="s in exportData.steps || []" :key="s.step" class="pdf-step">
            <p><b>{{ t('diagnose.export.stepNo', { n: s.step }) }} {{ s.tool ? '[' + s.tool + ']' : '[' + t('diagnose.export.finalStep') + ']' }}</b>
               {{ s.duration_ms ? t('diagnose.export.stepCost', { ms: s.duration_ms }) : '' }}{{ t('diagnose.export.stepSummary', { summary: s.summary }) }}</p>
            <p class="pdf-sub">{{ t('diagnose.export.thought', { thought: s.thought }) }}</p>
            <p v-if="s.params && Object.keys(s.params).length" class="pdf-sub">{{ t('diagnose.export.params', { params: JSON.stringify(s.params) }) }}</p>
          </div>
        </div>

        <div class="pdf-section">
          <h2>{{ t('diagnose.export.conclusion') }}</h2>
          <div class="pdf-md" v-html="md.render(exportData.conclusion_md || t('diagnose.status.noConclusion'))" />
        </div>

        <p class="pdf-footer">{{ t('diagnose.export.footer', { version: exportDialog.version === 'redacted' ? t('diagnose.export.redacted') : t('diagnose.export.full') }) }}</p>
          </div>
        </div>
      </div>
      <!-- 浮标在 .pdf-root 之外，html2canvas 只截 exportBox，不会进 PDF -->
      <div v-if="exportDialog.generating" class="pdf-busy" role="status">{{ t('diagnose.export.busy') }}</div>
    </teleport>
  </div>
</template>

<script>
export default { name: 'Diagnose' }
</script>

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

/* ---------- PDF 导出层：生成期间全屏白色预览（A4 宽 794px @96dpi） ---------- */
.pdf-overlay {
  position: fixed;
  inset: 0;
  background: var(--el-bg-color);
  z-index: 3000;
  overflow: auto;
  display: flex;
  justify-content: center;
}
.pdf-busy {
  position: fixed;
  top: 16px;
  right: 16px;
  z-index: 3100;
  background: #3370ff;
  color: #ffffff;
  padding: 8px 16px;
  border-radius: 4px;
  font-size: 13px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.2);
}
.pdf-root {
  width: 794px;
  flex-shrink: 0;
  background: var(--el-bg-color);
  color: #1f2329;
  font-family: 'Helvetica Neue', Helvetica, 'PingFang SC', 'Microsoft YaHei', sans-serif;
}
.pdf-page {
  padding: 24px 28px;
}
.pdf-title {
  font-size: 22px;
  text-align: center;
  margin: 0 0 14px;
  border-bottom: 2px solid #3370ff;
  padding-bottom: 10px;
}
.pdf-meta {
  width: 100%;
  border-collapse: collapse;
  margin-bottom: 14px;
  font-size: 13px;
}
.pdf-meta td {
  border: 1px solid #d0d3d9;
  padding: 5px 8px;
}
.pdf-meta td.k {
  background: var(--el-fill-color-light);
  width: 80px;
  white-space: nowrap;
  font-weight: 600;
}
.pdf-section h2 {
  font-size: 15px;
  border-left: 4px solid #3370ff;
  padding-left: 8px;
  margin: 16px 0 8px;
}
.pdf-step p {
  margin: 3px 0;
  font-size: 13px;
  line-height: 1.6;
}
.pdf-sub {
  color: #646a73;
  font-size: 12px;
}
.pdf-md :deep(h2) {
  font-size: 14px;
  margin: 10px 0 6px;
}
.pdf-md :deep(p) {
  margin: 5px 0;
  line-height: 1.7;
  font-size: 13px;
}
.pdf-md :deep(ol), .pdf-md :deep(ul) {
  padding-left: 18px;
  margin: 5px 0;
}
.pdf-md :deep(li) {
  margin: 3px 0;
  line-height: 1.6;
  font-size: 13px;
}
.pdf-footer {
  margin-top: 18px;
  text-align: center;
  color: #8f959e;
  font-size: 11px;
  border-top: 1px solid #d0d3d9;
  padding-top: 8px;
}
</style>
