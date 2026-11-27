<template>
  <!-- 自定义工作流编辑器：
       左=定义列表，右=卡片式步骤编辑（拖动排序/选工具/参数模板/失败策略），
       预览执行（零副作用）+ 试跑（真跑走既有 run，变更步骤照样只建工单）+ JSON 导入导出。
       形态选"结构化步骤编辑器"而非节点画布：所见即所跑，理由见 docs/active/engineering.md §3.3。 -->
  <div class="wf-editor-page">
    <div class="page-head">
      <div>
        <div class="page-title">{{ t('workflow.title') }}</div>
        <div class="page-sub">{{ t('workflow.subtitle') }}</div>
      </div>
      <div class="head-actions">
        <el-button text type="primary" @click="goRun">{{ t('workflow.btn.go_run') }}</el-button>
        <el-button type="primary" @click="startNew">{{ t('workflow.btn.new') }}</el-button>
        <el-button @click="openImport">{{ t('workflow.btn.import') }}</el-button>
        <el-button @click="exportAll">{{ t('workflow.btn.export_all') }}</el-button>
      </div>
    </div>

    <div v-if="!isAdmin" class="pane-empty">{{ t('workflow.msg.no_permission') }}</div>

    <div v-else class="wf-editor-cols">
      <!-- 左：定义列表，两区为内置只读展示 + 自定义可编辑区 -->
      <div class="wf-def-list">
        <!-- 内置区：只读展示（名称/域/描述/步骤摘要），点开只读详情，可「另存为自定义」 -->
        <div class="def-sec def-sec-builtin">
          <div class="def-list-title">{{ t('workflow.builtin.section', { n: builtins.length }) }}</div>
          <div class="def-list-hint sub">{{ t('workflow.builtin.hint') }}</div>
          <div v-if="!builtins.length" class="list-empty">{{ t('workflow.builtin.empty') }}</div>
          <div
            v-for="b in builtins" :key="b.key" class="def-card def-card-builtin"
            :class="{ 'def-card-active': builtinSel && builtinSel.key === b.key && !editing }"
          >
            <div class="def-main" @click="selectBuiltin(b)">
              <div class="def-name">
                {{ b.name }}
                <el-tag size="small" type="info" effect="plain">{{ t('workflow.builtin.badge') }}</el-tag>
              </div>
              <div class="def-desc">{{ b.description || '—' }}</div>
              <div class="def-meta">
                <span class="mono">{{ b.key }}</span>
                <el-tag v-if="b.domain" size="small" effect="plain">{{ b.domain }}</el-tag>
                <span class="sub">{{ t('workflow.list.steps_n', { n: (b.steps_summary || []).length }) }}</span>
              </div>
              <div v-if="(b.steps_summary || []).length" class="def-steps-line sub">
                {{ t('workflow.builtin.steps_line', { names: (b.steps_summary || []).join(' → ') }) }}
              </div>
            </div>
            <div class="def-actions">
              <el-button size="small" text type="primary" @click="selectBuiltin(b)">{{ t('workflow.builtin.detail_btn') }}</el-button>
              <el-button size="small" text type="primary" @click="saveAsBuiltin(b)">{{ t('workflow.btn.save_as') }}</el-button>
            </div>
          </div>
        </div>

        <!-- 自定义区：可编辑（既有 CRUD 零变化） -->
        <div class="def-sec def-sec-custom">
          <div class="def-list-title">{{ t('workflow.custom.section', { n: defs.length }) }}</div>
          <div v-if="!defs.length" class="list-empty">{{ t('workflow.list.empty') }}</div>
          <div
            v-for="d in defs" :key="d.key" class="def-card"
            :class="{ 'def-card-active': d.key === originalKey && editing }"
          >
          <div class="def-main" @click="startEdit(d)">
            <div class="def-name">{{ d.name }}</div>
            <div class="def-desc">{{ d.description || '—' }}</div>
            <div class="def-meta">
              <span class="mono">{{ d.key }}</span>
              <el-tag v-if="d.domain" size="small" effect="plain">{{ d.domain }}</el-tag>
              <el-tag v-if="d.has_change_steps" size="small" type="warning" effect="plain">{{ t('workflow.list.change_steps') }}</el-tag>
              <span class="sub">{{ t('workflow.list.steps_n', { n: (d.steps || []).length }) }}</span>
              <span class="sub">{{ t('workflow.list.v', { v: d.version }) }}</span>
              <el-tag size="small" :type="d.enabled ? 'success' : 'info'" effect="plain">
                {{ d.enabled ? t('workflow.list.enabled') : t('workflow.list.disabled') }}
              </el-tag>
            </div>
            <div v-if="(d.params_hint || []).length" class="def-tags sub">
              {{ t('workflow.list.params_hint', { names: d.params_hint.join(' / ') }) }}
            </div>
          </div>
          <div class="def-actions">
            <el-button size="small" text type="primary" @click="startEdit(d)">{{ t('workflow.btn.edit') }}</el-button>
            <el-button size="small" text type="primary" @click="openHistory(d)">{{ t('workflow.btn.history') }}</el-button>
            <el-dropdown class="def-export-dd" @command="(cmd) => exportDef(d, cmd)">
              <el-button size="small" text type="primary">{{ t('workflow.btn.export') }}</el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="yaml">{{ t('workflow.io.export_yaml') }}</el-dropdown-item>
                  <el-dropdown-item command="json">{{ t('workflow.io.export_json') }}</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
            <el-button size="small" text type="danger" @click="removeDef(d)">{{ t('workflow.btn.delete') }}</el-button>
          </div>
        </div>
        </div>
      </div>

      <!-- 右：内置只读详情 / 编辑面板 -->
      <div class="wf-edit-pane">
        <!-- 内置只读详情：步骤序列/工具/失败策略 + 参数表（不可编辑，要定制走「另存为自定义」） -->
        <template v-if="builtinSel && !editing">
          <div class="detail-head">
            <div class="detail-title">
              {{ builtinDetail ? builtinDetail.name : builtinSel.name }}
              <el-tag size="small" type="info" effect="plain">{{ t('workflow.builtin.badge') }}</el-tag>
            </div>
            <div class="detail-tags">
              <span class="mono">{{ builtinSel.key }}</span>
              <el-tag v-if="builtinDetail && builtinDetail.domain" size="small" effect="plain">{{ builtinDetail.domain }}</el-tag>
              <el-tag v-if="builtinDetail && builtinDetail.has_change_steps" size="small" type="warning" effect="plain">{{ t('workflow.list.change_steps') }}</el-tag>
            </div>
            <div class="detail-desc">{{ builtinDetail ? builtinDetail.description : '' }}</div>
          </div>
          <div v-if="detailLoading" class="list-empty">{{ t('workflow.detail.loading') }}</div>
          <template v-else-if="builtinDetail">
            <div class="steps-title">{{ t('workflow.detail.steps_title') }}</div>
            <el-table :data="builtinDetail.steps || []" size="small" class="detail-table">
              <el-table-column prop="idx" :label="t('workflow.preview.idx')" width="56" />
              <el-table-column prop="label" :label="t('workflow.preview.label')" width="150" />
              <el-table-column :label="t('workflow.detail.kind')" width="110">
                <template #default="scope">
                  <el-tag size="small" effect="plain" :type="kindType(scope.row.kind)">{{ kindLabel(scope.row.kind) }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column :label="t('workflow.detail.tools')">
                <template #default="scope">
                  <span class="mono">{{ (scope.row.tools || []).length ? scope.row.tools.join(' / ') : '—' }}</span>
                </template>
              </el-table-column>
              <el-table-column :label="t('workflow.detail.policy')" width="110">
                <template #default="scope">
                  <span class="sub">{{ scope.row.continue_on_fail ? t('workflow.preview.cof_on') : t('workflow.preview.cof_off') }}</span>
                </template>
              </el-table-column>
            </el-table>
            <div class="form-tip">{{ t('workflow.detail.builtin_policy') }}</div>
            <div class="steps-title">{{ t('workflow.detail.params_title') }}</div>
            <el-table v-if="schemaRows.length" :data="schemaRows" size="small" class="detail-table">
              <el-table-column prop="name" :label="t('workflow.detail.name')" width="150" />
              <el-table-column prop="type" :label="t('workflow.detail.type')" width="90" />
              <el-table-column :label="t('workflow.detail.required')" width="90">
                <template #default="scope">{{ scope.row.required ? t('workflow.detail.required') : t('workflow.detail.optional') }}</template>
              </el-table-column>
              <el-table-column :label="t('workflow.detail.sensitive_col')" width="84">
                <template #default="scope">
                  <el-tag v-if="scope.row.sensitive" size="small" type="danger" effect="plain">{{ t('workflow.detail.sensitive') }}</el-tag>
                  <span v-else class="sub">—</span>
                </template>
              </el-table-column>
              <el-table-column :label="t('workflow.detail.desc')">
                <template #default="scope">
                  <span class="sub">{{ scope.row.description }}<template v-if="scope.row.has_default">（{{ t('workflow.detail.default') }}: {{ scope.row.default }}）</template></span>
                </template>
              </el-table-column>
            </el-table>
            <div v-else class="list-empty">{{ t('workflow.detail.no_params') }}</div>
            <div v-if="!hasToolSteps" class="detail-warn">{{ t('workflow.builtin.no_tool_steps') }}</div>
            <div class="edit-foot">
              <el-button type="primary" @click="saveAsBuiltin(builtinSel)">{{ t('workflow.btn.save_as') }}</el-button>
              <span class="form-tip">{{ t('workflow.builtin.readonly') }}</span>
            </div>
          </template>
        </template>
        <div v-else-if="!editing" class="pane-empty">{{ t('workflow.list.empty') }}</div>
        <template v-else>
          <div class="form-grid">
            <div class="form-row">
              <span class="form-label">{{ t('workflow.form.key') }}</span>
              <el-input v-model="form.key" class="mono" :disabled="!isNew" :placeholder="'my_workflow'" />
            </div>
            <div class="form-tip">{{ t('workflow.form.key_hint') }}</div>
            <div class="form-row">
              <span class="form-label">{{ t('workflow.form.name') }}</span>
              <el-input v-model="form.name" :placeholder="t('workflow.form.name')" />
            </div>
            <div class="form-row">
              <span class="form-label">{{ t('workflow.form.domain') }}</span>
              <el-input v-model="form.domain" :placeholder="t('workflow.form.domain_hint')" />
            </div>
            <div class="form-row">
              <span class="form-label">{{ t('workflow.form.description') }}</span>
              <el-input v-model="form.description" type="textarea" :rows="2" />
            </div>
            <div class="form-row form-inline">
              <span class="form-label">{{ t('workflow.form.enabled') }}</span>
              <el-switch v-model="form.enabled" />
            </div>
          </div>

          <div class="steps-title">{{ t('workflow.step.title') }}</div>
          <div class="steps-hint sub">{{ t('workflow.step.kind_hint') }}</div>
          <div v-if="!steps.length" class="list-empty">{{ t('workflow.step.empty') }}</div>
          <div class="step-list">
            <div
              v-for="(s, i) in steps" :key="i" class="step-card"
              draggable="true" @dragstart="onDragStart(i)" @dragover.prevent @drop="onDrop(i)"
            >
              <div class="step-head">
                <span class="step-order mono">{{ t('workflow.step.order', { n: i + 1 }) }}</span>
                <div class="step-tools">
                  <el-button size="small" text :title="t('workflow.step.move_up')" @click="moveStep(i, -1)">↑</el-button>
                  <el-button size="small" text :title="t('workflow.step.move_down')" @click="moveStep(i, 1)">↓</el-button>
                  <el-button size="small" text type="danger" @click="removeStep(i)">{{ t('workflow.step.remove') }}</el-button>
                </div>
              </div>
              <div class="step-body">
                <div class="step-field">
                  <span class="step-field-label">{{ t('workflow.step.label') }}</span>
                  <el-input v-model="s.label" size="small" :placeholder="t('workflow.step.label')" />
                </div>
                <div class="step-field">
                  <span class="step-field-label">{{ t('workflow.step.tool') }}</span>
                  <el-select v-model="s.tool" size="small" filterable :placeholder="t('workflow.step.tool_placeholder')" @change="onToolChange(s)">
                    <el-option-group :label="t('workflow.step.kind_read')">
                      <el-option v-for="tool in readTools" :key="tool.name" :value="tool.name" :label="tool.name" />
                    </el-option-group>
                    <el-option-group :label="t('workflow.step.kind_change')">
                      <el-option v-for="tool in changeTools" :key="tool.name" :value="tool.name" :label="tool.name" />
                    </el-option-group>
                  </el-select>
                  <div class="form-tip">
                    {{ toolKind(s.tool) === 'change' ? t('workflow.step.kind_hint') : t('workflow.step.read_hint') }}
                  </div>
                  <div class="form-tip">
                    {{ toolParamNames(s.tool).length
                      ? t('workflow.step.tool_params_hint', { names: toolParamNames(s.tool).join(' / ') })
                      : t('workflow.step.no_params_hint') }}
                  </div>
                </div>
                <div class="step-field">
                  <span class="step-field-label">{{ t('workflow.step.params') }}</span>
                  <div class="param-rows">
                    <div v-for="(row, j) in s.rows" :key="j" class="param-row">
                      <el-input v-model="row.k" class="param-k" size="small" :placeholder="t('workflow.step.param_name')" />
                      <el-input v-model="row.v" class="param-v" size="small" :placeholder="t('workflow.step.param_value')" />
                      <el-button size="small" text type="danger" class="param-del" @click="s.rows.splice(j, 1)">×</el-button>
                    </div>
                  </div>
                  <el-button size="small" text type="primary" class="param-add" @click="s.rows.push({ k: '', v: '' })">
                    {{ t('workflow.step.add_param') }}
                  </el-button>
                  <div class="form-tip">{{ t('workflow.step.sensitive_hint') }}</div>
                </div>
                <div class="step-foot">
                  <span class="cof-wrap">
                    <el-switch v-model="s.continue_on_fail" />
                    <span class="step-field-label">{{ t('workflow.step.continue_on_fail') }}</span>
                  </span>
                  <span class="form-tip">{{ t('workflow.step.continue_on_fail_hint') }}</span>
                </div>
              </div>
            </div>
          </div>
          <el-button size="small" text type="primary" class="param-add" @click="addStep">
            {{ t('workflow.btn.add_step') }}
          </el-button>

          <div class="edit-foot">
            <el-button @click="openPreview">{{ t('workflow.btn.preview') }}</el-button>
            <el-button :disabled="isNew" @click="openRun">{{ t('workflow.btn.try_run') }}</el-button>
            <el-button type="primary" :loading="saveBusy" @click="save">{{ t('workflow.btn.save') }}</el-button>
            <el-button @click="cancelEdit">{{ t('workflow.btn.cancel') }}</el-button>
          </div>
        </template>
      </div>
    </div>

    <!-- 预览执行：逐步展开"将调用什么工具、解析出什么参数、是否建单"，零副作用 -->
    <el-dialog v-model="previewOpen" :title="t('workflow.preview.title')" width="760px">
      <div class="preview-wrap">
        <div class="steps-title">{{ t('workflow.preview.params_title') }}</div>
        <div v-for="(row, i) in previewParams" :key="i" class="param-row">
          <el-input v-model="row.k" class="param-k" size="small" :placeholder="t('workflow.step.param_name')" />
          <el-input v-model="row.v" class="param-v" size="small" :placeholder="t('workflow.step.param_value')" />
          <el-button size="small" text type="danger" class="param-del" @click="previewParams.splice(i, 1)">×</el-button>
        </div>
        <el-button size="small" text type="primary" class="param-add" @click="previewParams.push({ k: '', v: '' })">
          {{ t('workflow.step.add_param') }}
        </el-button>
        <el-table v-if="previewPlan.length" :data="previewPlan" size="small" class="preview-table">
          <el-table-column prop="idx" :label="t('workflow.preview.idx')" width="56" />
          <el-table-column prop="label" :label="t('workflow.preview.label')" width="140" />
          <el-table-column prop="tool" :label="t('workflow.preview.tool')" width="160" />
          <el-table-column :label="t('workflow.preview.kind')" width="130">
            <template #default="scope">
              <el-tag size="small" :type="scope.row.kind === 'change' ? 'warning' : 'success'" effect="plain">
                {{ scope.row.kind === 'change' ? t('workflow.preview.will_ticket') : t('workflow.preview.will_read') }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column :label="t('workflow.preview.params')">
            <template #default="scope">
              <span class="mono">{{ formatParams(scope.row.params) }}</span>
              <el-tag v-if="scope.row.status === 'missing_param'" size="small" type="danger" effect="plain">
                {{ t('workflow.preview.status_missing') }}：{{ scope.row.error }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column :label="t('workflow.step.continue_on_fail')" width="100">
            <template #default="scope">
              <span class="sub">{{ scope.row.continue_on_fail ? t('workflow.preview.cof_on') : t('workflow.preview.cof_off') }}</span>
            </template>
          </el-table-column>
        </el-table>
        <div v-else class="list-empty">{{ t('workflow.preview.empty') }}</div>
      </div>
      <template #footer>
        <el-button @click="previewOpen = false">{{ t('workflow.btn.close') }}</el-button>
        <el-button type="primary" :loading="previewBusy" @click="doPreview">{{ t('workflow.btn.preview') }}</el-button>
      </template>
    </el-dialog>

    <!-- 试跑：真跑（走既有 run 端点；变更步骤照样只建待确认工单） -->
    <el-dialog v-model="runOpen" :title="t('workflow.run.title', { name: form.name })" width="760px">
      <div class="preview-wrap">
        <div class="steps-title">{{ t('workflow.run.params_title') }}</div>
        <div v-for="(row, i) in runParams" :key="i" class="param-row">
          <el-input v-model="row.k" class="param-k" size="small" :placeholder="t('workflow.step.param_name')" />
          <el-input v-model="row.v" class="param-v" size="small" :placeholder="t('workflow.step.param_value')" />
          <el-button size="small" text type="danger" class="param-del" @click="runParams.splice(i, 1)">×</el-button>
        </div>
        <el-button size="small" text type="primary" class="param-add" @click="runParams.push({ k: '', v: '' })">
          {{ t('workflow.step.add_param') }}
        </el-button>
        <template v-if="runResult">
          <div class="steps-title">{{ t('workflow.run.result_title') }}</div>
          <el-tag size="small" :type="runResult.status === 'done' ? 'success' : 'danger'" effect="plain" class="run-status">
            {{ runResult.status === 'done' ? t('workflow.status.ok') : t('workflow.status.failed') }}
          </el-tag>
          <el-table :data="runResult.steps || []" size="small" class="preview-table">
            <el-table-column type="index" :label="t('workflow.preview.idx')" width="56" />
            <el-table-column prop="step" :label="t('workflow.preview.label')" width="150" />
            <el-table-column :label="t('workflow.run.status')" width="90">
              <template #default="scope">
                <el-tag size="small" effect="plain">{{ statusLabel(scope.row.status) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="detail" :label="t('workflow.run.detail')" />
          </el-table>
          <div v-if="runResult.artifact && runResult.artifact.report_md" class="md-body" v-html="renderReport(runResult.artifact.report_md)"></div>
        </template>
      </div>
      <template #footer>
        <el-button @click="runOpen = false">{{ t('workflow.btn.close') }}</el-button>
        <el-button type="primary" :loading="runBusy" @click="doRun">{{ t('workflow.btn.try_run') }}</el-button>
      </template>
    </el-dialog>

    <!-- 导入 YAML/JSON（schema 服务端同口径校验；同 key 冲突 409→确认覆盖，version+1） -->
    <el-dialog v-model="importOpen" :title="t('workflow.io.import_title')" width="640px">
      <div class="preview-wrap">
        <div class="io-hint sub">{{ t('workflow.io.import_hint') }}</div>
        <div class="file-row">
          <input type="file" accept="application/json,.json,.yaml,.yml" @change="onImportFile" />
        </div>
        <el-input v-model="importText" type="textarea" :rows="8" class="mono" />
      </div>
      <template #footer>
        <el-button @click="importOpen = false">{{ t('workflow.btn.cancel') }}</el-button>
        <el-button type="primary" :loading="importBusy" @click="doImport">{{ t('workflow.io.import_btn') }}</el-button>
      </template>
    </el-dialog>

    <!-- 历史版本：版本列表+时间+恢复按钮+当前版标注 -->
    <el-dialog v-model="historyOpen" :title="t('workflow.hist.title', { name: historyName })" width="720px">
      <div class="preview-wrap">
        <div v-if="historyLoading" class="list-empty">{{ t('workflow.detail.loading') }}</div>
        <template v-else>
          <el-table v-if="historyItems.length" :data="historyItems" size="small" class="preview-table">
            <el-table-column :label="t('workflow.hist.version')" width="76">
              <template #default="scope"><span class="mono">v{{ scope.row.version }}</span></template>
            </el-table-column>
            <el-table-column :label="t('workflow.hist.time')" width="160">
              <template #default="scope"><span class="sub">{{ fmtTime(scope.row.created_at) }}</span></template>
            </el-table-column>
            <el-table-column :label="t('workflow.hist.by')" width="100">
              <template #default="scope"><span class="sub">{{ scope.row.created_by || '—' }}</span></template>
            </el-table-column>
            <el-table-column :label="t('workflow.hist.summary')">
              <template #default="scope">
                <span>{{ scope.row.name || '—' }}</span>
                <span class="sub">（{{ t('workflow.list.steps_n', { n: scope.row.steps_n }) }}）</span>
              </template>
            </el-table-column>
            <el-table-column :label="t('workflow.hist.status')" width="96">
              <template #default="scope">
                <el-tag v-if="scope.row.current" size="small" type="success" effect="plain">{{ t('workflow.hist.current') }}</el-tag>
                <span v-else class="sub">—</span>
              </template>
            </el-table-column>
            <el-table-column :label="t('workflow.hist.action')" width="110">
              <template #default="scope">
                <el-button v-if="!scope.row.current" size="small" text type="primary" @click="restoreVersion(scope.row)">
                  {{ t('workflow.hist.restore') }}
                </el-button>
                <span v-else class="sub">—</span>
              </template>
            </el-table-column>
          </el-table>
          <div v-else class="list-empty">{{ t('workflow.hist.empty') }}</div>
          <div class="form-tip">{{ t('workflow.hist.restore_hint') }}</div>
        </template>
      </div>
      <template #footer>
        <el-button @click="historyOpen = false">{{ t('workflow.btn.close') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
// 自定义工作流编辑器：管理端仅 admin（API 同口径）；
// 步骤只能从白名单工具里选（服务端硬校验，前端同口径提示）；敏感参数只允许占位符。
import MarkdownIt from 'markdown-it'
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useRouter } from 'vue-router'

import {
  createCustomWorkflow, deleteCustomWorkflow, exportWorkflowDef, getWorkflowDetail,
  importWorkflowDef, listCustomWorkflows, listWorkflowVersions, listWorkflows,
  previewCustomWorkflow, restoreWorkflowVersion, runWorkflow, saveAsWorkflow,
  updateCustomWorkflow, workflowToolCatalog,
} from '@/api/agent'
import { useAuthStore } from '@/stores/auth'
import { t } from '@/i18n'

const auth = useAuthStore()
const router = useRouter()

// 本页是编排面；运行/运行记录/定时在智能体工作流面板
// （选中内置工作流时带 ?workflow=key 直达其运行参数框）
function goRun() {
  const b = builtinSel.value
  router.push(b && b.key ? { path: '/agent', query: { workflow: String(b.key) } } : '/agent')
}
const md = new MarkdownIt({ html: false, breaks: true })

const isAdmin = computed(() => auth.user?.role === 'admin')

const defs = ref([])
const builtins = ref([])          // 内置工作流（只读展示区）
const builtinSel = ref(null)      // 当前选中的内置工作流（右栏只读详情）
const builtinDetail = ref(null)   // GET /agent/workflows/{key}/detail 返回
const detailLoading = ref(false)
const catalog = ref([])
const editing = ref(false)
const isNew = ref(false)
const originalKey = ref('')
const form = reactive({ key: '', name: '', domain: '', description: '', enabled: true })
const steps = ref([])
const dragIdx = ref(-1)
const saveBusy = ref(false)

const previewOpen = ref(false)
const previewBusy = ref(false)
const previewParams = ref([{ k: '', v: '' }])
const previewPlan = ref([])

const runOpen = ref(false)
const runBusy = ref(false)
const runParams = ref([{ k: '', v: '' }])
const runResult = ref(null)

const importOpen = ref(false)
const importBusy = ref(false)
const importText = ref('')

const historyOpen = ref(false)      // 历史版本
const historyLoading = ref(false)
const historyItems = ref([])
const historyKey = ref('')
const historyName = ref('')

const readTools = computed(() => catalog.value.filter((x) => x.kind === 'read'))
const changeTools = computed(() => catalog.value.filter((x) => x.kind === 'change'))

// 内置只读详情：params_schema -> 表格行；步骤里有无可迁移的工具步骤（另存为可用性提示）
const schemaRows = computed(() => Object.entries((builtinDetail.value && builtinDetail.value.params_schema) || {})
  .map(([name, f]) => ({
    name,
    type: (f || {}).type || 'string',
    required: !!(f || {}).required,
    description: (f || {}).description || '',
    sensitive: !!(f || {}).sensitive,
    default: (f || {}).default,
    has_default: (f || {}).default !== undefined,
  })))
const hasToolSteps = computed(() => ((builtinDetail.value && builtinDetail.value.steps) || [])
  .some((s) => (s.tools || []).length > 0))

function kindLabel(kind) {
  return {
    internal: t('workflow.detail.kind_internal'),
    read: t('workflow.step.kind_read'),
    change: t('workflow.step.kind_change'),
  }[kind] || kind
}
function kindType(kind) {
  return { internal: 'info', read: 'success', change: 'warning' }[kind] || 'info'
}

// 敏感参数名口径（与后端 custom.is_sensitive_key 同源：精确名单 + 后缀）
const SENSITIVE_KEYS = new Set([
  'ak', 'sk', 'access_key', 'access_key_secret', 'accesskey', 'accesskey_secret',
  'secret', 'secret_key', 'client_secret', 'password', 'new_password', 'old_password',
  'passwd', 'pwd', 'token', 'api_key', 'apikey', 'api_secret', 'kube_token',
  'initial_password', 'otp_secret', 'password_hash', 'private_key', 'ssh_key',
])
const SENSITIVE_SUFFIXES = ['_password', '_secret', '_token', '_sk']

function isSensitiveKey(k) {
  const x = String(k || '').trim().toLowerCase()
  return SENSITIVE_KEYS.has(x) || SENSITIVE_SUFFIXES.some((s) => x.endsWith(s))
}
function hasPlaceholder(v) {
  return typeof v === 'string' && /\{\{\s*[a-zA-Z_][\w]*\s*\}\}/.test(v)
}

function toolKind(name) {
  return (catalog.value.find((x) => x.name === name) || {}).kind || ''
}
function toolParamNames(name) {
  const tool = catalog.value.find((x) => x.name === name)
  return (tool?.params || []).map((p) => p.name)
}

function displayValue(v) {
  if (v === null || v === undefined) return ''
  if (typeof v === 'string') return v
  return JSON.stringify(v)
}

function coerceValue(raw) {
  const s = String(raw ?? '')
  if (/^\{\{\s*[a-zA-Z_][\w]*\s*\}\}$/.test(s.trim())) return s.trim()
  if (s === 'true') return true
  if (s === 'false') return false
  if (/^-?\d+$/.test(s)) return Number(s)
  if (/^-?\d+\.\d+$/.test(s)) return Number(s)
  if ((s.startsWith('{') && s.endsWith('}')) || (s.startsWith('[') && s.endsWith(']'))) {
    try { return JSON.parse(s) } catch { return s }
  }
  return s
}

function rowsToTemplate(rows, stepNo) {
  const out = {}
  for (const row of rows) {
    const k = String(row.k || '').trim()
    if (!k) {
      if (String(row.v || '').trim()) throw new Error(t('workflow.msg.need_param_name', { n: stepNo }))
      continue
    }
    const v = coerceValue(row.v)
    if (isSensitiveKey(k) && !hasPlaceholder(String(row.v || ''))) {
      throw new Error(t('workflow.msg.sensitive_literal', { n: stepNo, key: k }))
    }
    out[k] = v
  }
  return out
}

function templateToRows(tmpl) {
  return Object.entries(tmpl || {}).map(([k, v]) => ({ k, v: displayValue(v) }))
}

// 组装保存载荷（与后端 validate_steps 同口径的前置校验，报错定位到第几步）
function buildStepsPayload() {
  if (!steps.value.length) throw new Error(t('workflow.msg.need_steps'))
  return steps.value.map((s, i) => {
    if (!s.tool) throw new Error(t('workflow.msg.need_tool', { n: i + 1 }))
    const allowed = toolParamNames(s.tool)
    for (const row of s.rows) {
      const k = String(row.k || '').trim()
      if (!k) {
        if (String(row.v || '').trim()) throw new Error(t('workflow.msg.need_param_name', { n: i + 1 }))
        continue
      }
      if (allowed.length && !allowed.includes(k)) {
        throw new Error(t('workflow.msg.bad_param_key', { n: i + 1, key: k }))
      }
    }
    return {
      tool: s.tool,
      label: String(s.label || '').trim() || s.tool,
      params_template: rowsToTemplate(s.rows, i + 1),
      continue_on_fail: !!s.continue_on_fail,
    }
  })
}

function emptyStep() {
  return { tool: '', label: '', rows: [{ k: '', v: '' }], continue_on_fail: false }
}

async function loadAll() {
  const [d, c, w] = await Promise.all([
    listCustomWorkflows(), workflowToolCatalog(), listWorkflows().catch(() => ({ items: [] })),
  ])
  defs.value = d.items || []
  catalog.value = c.items || []
  builtins.value = (w.items || []).filter((x) => !x.custom)
}

function startNew() {
  editing.value = true
  isNew.value = true
  builtinSel.value = null
  originalKey.value = ''
  form.key = ''
  form.name = ''
  form.domain = ''
  form.description = ''
  form.enabled = true
  steps.value = [emptyStep()]
}

function startEdit(d) {
  editing.value = true
  isNew.value = false
  builtinSel.value = null
  originalKey.value = d.key
  form.key = d.key
  form.name = d.name
  form.domain = d.domain
  form.description = d.description
  form.enabled = !!d.enabled
  steps.value = (d.steps || []).map((s) => ({
    tool: s.tool || '',
    label: s.label || '',
    rows: templateToRows(s.params_template),
    continue_on_fail: !!s.continue_on_fail,
  }))
  if (!steps.value.length) steps.value = [emptyStep()]
}

function cancelEdit() {
  editing.value = false
  isNew.value = false
  originalKey.value = ''
}

/* ---------------- 内置工作流：只读详情 + 另存为自定义 ---------------- */

async function selectBuiltin(b) {
  editing.value = false
  isNew.value = false
  originalKey.value = ''
  builtinSel.value = b
  builtinDetail.value = null
  detailLoading.value = true
  try {
    builtinDetail.value = await getWorkflowDetail(b.key)
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || e?.message || t('workflow.msg.save_failed'))
  } finally {
    detailLoading.value = false
  }
}

async function afterSaveAs(out) {
  ElMessage.success(t('workflow.msg.saved_as', { key: out.key, n: out.copied_steps }))
  if ((out.omitted_labels || []).length) {
    ElMessage.warning(t('workflow.msg.saved_as_omitted', { names: out.omitted_labels.join(' / ') }))
  }
  await loadAll()
  const d = defs.value.find((x) => x.key === out.key)
  if (d) startEdit(d)   // 副本进自定义区并直接进编辑器
}

async function saveAsBuiltin(b) {
  if (!b || !b.key) return
  try {
    const out = await saveAsWorkflow(b.key, false)
    await afterSaveAs(out)
    return
  } catch (e) {
    if (e?.response?.status !== 409) {
      return ElMessage.error(e?.response?.data?.detail || e?.message || t('workflow.msg.save_as_failed'))
    }
  }
  // 副本已存在（409）：确认后幂等覆盖（version+1，不产生重复行）
  try {
    await ElMessageBox.confirm(
      t('workflow.msg.confirm_overwrite_copy', { key: `${b.key}-copy` }),
      t('workflow.btn.save_as'),
      { type: 'warning', confirmButtonText: t('workflow.btn.save_as'), cancelButtonText: t('workflow.btn.cancel') },
    )
  } catch { return }
  try {
    const out = await saveAsWorkflow(b.key, true)
    await afterSaveAs(out)
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || e?.message || t('workflow.msg.save_as_failed'))
  }
}

function addStep() { steps.value.push(emptyStep()) }
function removeStep(i) { steps.value.splice(i, 1) }
function moveStep(i, delta) {
  const j = i + delta
  if (j < 0 || j >= steps.value.length) return
  const [it] = steps.value.splice(i, 1)
  steps.value.splice(j, 0, it)
}
function onDragStart(i) { dragIdx.value = i }
function onDrop(i) {
  const from = dragIdx.value
  dragIdx.value = -1
  if (from < 0 || from === i) return
  const [it] = steps.value.splice(from, 1)
  steps.value.splice(i, 0, it)
}

function onToolChange(s) {
  // 换工具后参数行按新工具参数表留着让用户改（不静默清空已填内容）
  s.rows = s.rows.filter((r) => String(r.k || '').trim() === '' || toolParamNames(s.tool).includes(r.k))
  if (!s.rows.length) s.rows = [{ k: '', v: '' }]
}

async function save() {
  if (!String(form.key || '').trim()) return ElMessage.warning(t('workflow.msg.need_key'))
  if (!String(form.name || '').trim()) return ElMessage.warning(t('workflow.msg.need_name'))
  let payload
  try {
    payload = buildStepsPayload()
  } catch (e) {
    return ElMessage.error(e.message)
  }
  saveBusy.value = true
  try {
    const body = {
      key: String(form.key).trim(),
      name: String(form.name).trim(),
      description: form.description || '',
      domain: form.domain || '',
      steps: payload,
      enabled: !!form.enabled,
    }
    const out = isNew.value
      ? await createCustomWorkflow(body)
      : await updateCustomWorkflow(originalKey.value, body)
    ElMessage.success(t('workflow.msg.saved', { v: out.version }))
    await loadAll()
    if (isNew.value) {
      isNew.value = false
      originalKey.value = out.key
    }
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || e?.message || t('workflow.msg.save_failed'))
  } finally {
    saveBusy.value = false
  }
}

async function removeDef(d) {
  try {
    await ElMessageBox.confirm(
      t('workflow.msg.confirm_delete', { name: d.name }),
      t('workflow.msg.confirm_delete_title'),
      { type: 'warning', confirmButtonText: t('workflow.btn.delete'), cancelButtonText: t('workflow.btn.cancel') },
    )
  } catch { return }
  try {
    await deleteCustomWorkflow(d.key)
    ElMessage.success(t('workflow.msg.deleted', { name: d.name }))
    if (originalKey.value === d.key) cancelEdit()
    await loadAll()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || e?.message || t('workflow.msg.save_failed'))
  }
}

/* ---------------- 预览执行（零副作用） ---------------- */

function paramsFromRows(rows) {
  const out = {}
  for (const r of rows) {
    const k = String(r.k || '').trim()
    if (k) out[k] = r.v ?? ''
  }
  return out
}

function openPreview() {
  previewPlan.value = []
  previewOpen.value = true
}
async function doPreview() {
  let payload
  try {
    payload = buildStepsPayload()
  } catch (e) {
    return ElMessage.error(e.message)
  }
  previewBusy.value = true
  try {
    const r = await previewCustomWorkflow({ steps: payload, params: paramsFromRows(previewParams.value) })
    previewPlan.value = r.steps || []
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || e?.message || t('workflow.msg.preview_failed'))
  } finally {
    previewBusy.value = false
  }
}

function formatParams(p) {
  try {
    return JSON.stringify(p || {}, null, 0)
  } catch {
    return String(p)
  }
}

/* ---------------- 试跑（真跑走既有 run 端点） ---------------- */

function openRun() {
  if (isNew.value || !originalKey.value) return ElMessage.warning(t('workflow.run.need_save'))
  runResult.value = null
  runOpen.value = true
}

async function doRun() {
  if (runBusy.value) return
  runBusy.value = true
  try {
    const r = await runWorkflow(originalKey.value, paramsFromRows(runParams.value), null)
    runResult.value = r
    ElMessage.success(r.status === 'done' ? t('workflow.run.done') : t('workflow.run.failed'))
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || e?.message || t('workflow.msg.save_failed'))
  } finally {
    runBusy.value = false
  }
}

function renderReport(text) {
  return md.render(String(text || ''))
}

function statusLabel(s) {
  return { ok: t('workflow.status.ok'), failed: t('workflow.status.failed'), skipped: t('workflow.status.skipped') }[s] || s
}

/* ---------------- 导入 / 导出（YAML/JSON 双格式；导出走服务端序列化，导入 schema 服务端同口径） ---------------- */

function downloadJson(name, obj) {
  const blob = new Blob([JSON.stringify(obj, null, 2)], { type: 'application/json' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = name
  a.click()
  URL.revokeObjectURL(a.href)
}

function pickExport(d) {
  return {
    key: d.key, name: d.name, description: d.description, domain: d.domain,
    steps: d.steps, enabled: d.enabled,
  }
}

function showErr(e, fallback) {
  ElMessage.error(e?.response?.data?.detail || e?.message || fallback)
}

async function exportDef(d, fmt) {
  try {
    const text = await exportWorkflowDef(d.key, fmt)
    const ext = fmt === 'json' ? 'json' : 'yaml'
    const name = `qianshu-workflow-${d.key}.${ext}`
    const blob = new Blob([text], { type: fmt === 'json' ? 'application/json' : 'application/yaml' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = name
    a.click()
    URL.revokeObjectURL(a.href)
    ElMessage.success(t('workflow.io.exported', { name }))
  } catch (e) {
    showErr(e, t('workflow.msg.export_failed'))
  }
}

function exportAll() {
  if (!defs.value.length) return ElMessage.warning(t('workflow.list.empty'))
  downloadJson('qianshu-workflows.json', {
    format: 'qianshu.workflow.v1', exported_at: new Date().toISOString(),
    workflows: defs.value.map(pickExport),
  })
  ElMessage.success(t('workflow.io.exported', { name: `qianshu-workflows.json` }))
}

function openImport() {
  importText.value = ''
  importOpen.value = true
}

function onImportFile(e) {
  const file = e.target?.files?.[0]
  if (!file) return
  const reader = new FileReader()
  reader.onload = () => { importText.value = String(reader.result || '') }
  reader.readAsText(file)
}

function finishImport(out) {
  const ok = out?.imported || 0
  const overwritten = (out?.overwritten || []).length
  ElMessage.success(t('workflow.io.import_done', { ok })
    + (overwritten ? t('workflow.io.import_overwritten', { n: overwritten }) : ''))
  importOpen.value = false
  loadAll().catch(() => { /* 列表刷新失败由下次操作兜底 */ })
}

async function doImport() {
  const text = String(importText.value || '').trim()
  if (!text) return ElMessage.error(t('workflow.io.no_workflows'))
  importBusy.value = true
  try {
    const out = await importWorkflowDef(text, false)
    finishImport(out)
  } catch (e) {
    if (e?.response?.status !== 409) return showErr(e, t('workflow.io.bad_format'))
    // key 冲突（409）：确认后覆盖导入（overwrite 语义照抄 save-as 副本覆盖）
    try {
      await ElMessageBox.confirm(
        t('workflow.msg.confirm_overwrite_import'),
        t('workflow.io.import_title'),
        { type: 'warning', confirmButtonText: t('workflow.io.import_btn'), cancelButtonText: t('workflow.btn.cancel') },
      )
    } catch { return }
    try {
      const out = await importWorkflowDef(text, true)
      finishImport(out)
    } catch (e2) {
      showErr(e2, t('workflow.io.bad_format'))
    }
  } finally {
    importBusy.value = false
  }
}

/* ---------------- 历史版本 + 一键回滚 ---------------- */

function fmtTime(s) {
  return String(s || '').replace('T', ' ').slice(0, 19)
}

async function openHistory(d) {
  historyKey.value = d.key
  historyName.value = d.name
  historyItems.value = []
  historyOpen.value = true
  historyLoading.value = true
  try {
    const r = await listWorkflowVersions(d.key)
    historyItems.value = r.items || []
  } catch (e) {
    showErr(e, t('workflow.hist.load_failed'))
  } finally {
    historyLoading.value = false
  }
}

async function restoreVersion(row) {
  try {
    await ElMessageBox.confirm(
      t('workflow.hist.confirm_restore', { name: historyName.value, v: row.version }),
      t('workflow.hist.restore'),
      { type: 'warning', confirmButtonText: t('workflow.hist.restore'), cancelButtonText: t('workflow.btn.cancel') },
    )
  } catch { return }
  try {
    const out = await restoreWorkflowVersion(historyKey.value, row.version)
    ElMessage.success(t('workflow.hist.restored', { v: row.version, cur: out.version }))
    await loadAll()
    await openHistory({ key: historyKey.value, name: historyName.value })
  } catch (e) {
    showErr(e, t('workflow.hist.restore_failed'))
  }
}

onMounted(() => {
  if (isAdmin.value) loadAll().catch(() => { /* 列表加载失败由空态兜底 */ })
})
</script>

<style scoped>
.wf-editor-page {
  height: 100%;
  overflow-y: auto;
  padding: 12px 14px;
}
.page-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 12px;
}
.page-title {
  font-size: 18px;
  font-weight: 600;
}
.page-sub {
  margin-top: 4px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.head-actions {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}
.wf-editor-cols {
  display: flex;
  gap: 12px;
  align-items: flex-start;
}
.wf-def-list {
  width: 320px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.def-list-title {
  font-weight: 600;
  font-size: 13px;
}
.list-empty {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  padding: 12px 4px;
}
.def-card {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  padding: 10px;
  background: var(--el-bg-color-overlay);
}
.def-card-active {
  border-color: var(--el-color-primary);
}
.def-main {
  cursor: pointer;
}
.def-name {
  font-weight: 600;
}
.def-desc {
  margin-top: 4px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}
.def-meta {
  margin-top: 6px;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}
.def-tags {
  margin-top: 4px;
}
.def-actions {
  margin-top: 6px;
  display: flex;
  gap: 2px;
  align-items: center;
}
.def-export-dd {
  display: inline-flex;
  align-items: center;
  margin: 0 2px;
}
.wf-edit-pane {
  flex: 1;
  min-width: 0;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: var(--el-bg-color-overlay);
  padding: 14px;
}
.pane-empty {
  padding: 40px 12px;
  text-align: center;
  color: var(--el-text-color-secondary);
}
.form-grid {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.form-row {
  display: flex;
  align-items: center;
  gap: 10px;
}
.form-inline {
  gap: 14px;
}
.form-label {
  width: 92px;
  flex-shrink: 0;
  font-size: 12px;
  color: var(--el-text-color-regular);
}
.form-tip {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  line-height: 1.6;
}
.steps-title {
  margin: 14px 0 6px;
  font-weight: 600;
  font-size: 13px;
}
.steps-hint {
  margin-bottom: 8px;
}
.step-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.step-card {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  padding: 10px 12px;
  background: var(--el-fill-color-blank);
}
.step-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.step-order {
  cursor: move;
  font-weight: 600;
}
.step-tools {
  display: flex;
  gap: 2px;
}
.step-body {
  margin-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.step-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.step-field-label {
  font-size: 12px;
  color: var(--el-text-color-regular);
}
.param-rows {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.param-row {
  display: flex;
  gap: 6px;
  align-items: center;
}
.param-k {
  width: 180px;
}
.param-v {
  flex: 1;
}
.param-del {
  flex-shrink: 0;
}
.param-add {
  align-self: flex-start;
}
.step-foot {
  display: flex;
  align-items: center;
  gap: 12px;
}
.cof-wrap {
  display: flex;
  align-items: center;
  gap: 6px;
}
.edit-foot {
  margin-top: 14px;
  display: flex;
  gap: 8px;
}
.preview-wrap {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.preview-table {
  margin-top: 6px;
}
.run-status {
  align-self: flex-start;
}
.md-body {
  margin-top: 8px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  padding: 10px 12px;
  font-size: 13px;
  line-height: 1.7;
}
.io-hint {
  margin-bottom: 4px;
}
.file-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
/* ---- 两区列表：内置只读 + 自定义 ---- */
.def-sec {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.def-sec-builtin {
  padding-bottom: 2px;
}
.def-sec-custom {
  margin-top: 14px;
  border-top: 1px solid var(--el-border-color-lighter);
  padding-top: 12px;
}
.def-list-hint {
  line-height: 1.6;
}
.def-card-builtin {
  background: var(--el-fill-color-lighter);
}
.def-steps-line {
  margin-top: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  word-break: break-all;
}
/* ---- 内置只读详情 ---- */
.detail-head {
  margin-bottom: 4px;
}
.detail-title {
  font-size: 16px;
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: 8px;
}
.detail-tags {
  margin-top: 6px;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}
.detail-desc {
  margin-top: 6px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.7;
}
.detail-table {
  margin-top: 6px;
}
.detail-warn {
  margin-top: 10px;
  padding: 8px 10px;
  border: 1px solid var(--el-color-warning-light-5);
  border-radius: 8px;
  color: var(--el-color-warning);
  font-size: 12px;
  line-height: 1.7;
}
</style>
