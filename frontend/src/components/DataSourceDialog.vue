<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

import http from '@/api/http'
import { t } from '@/i18n'

// 新增/编辑数据源：一个对话框完整闭环——
// 教程（分步）/ 配置表单 / 注意事项 / 测试连通按钮 + 结果区（真实版本信息）
const props = defineProps({
  modelValue: { type: Boolean, default: false },
  presetType: { type: String, default: '' },
  editSource: { type: Object, default: null },   // { kind, index, source }
})
const emit = defineEmits(['update:modelValue', 'save'])

// 15 类数据源（指标 8 + 日志 7，es/http 两可——「注册为」可改）；label/ph 为 i18n 键
const TYPES = [
  { value: 'prometheus', labelKey: 'integrations.dsType.prometheus', kind: 'metric', auth: 'headers', phKey: 'integrations.dsPh.prometheus' },
  { value: 'vm', labelKey: 'integrations.dsType.vm', kind: 'metric', auth: 'headers', phKey: 'integrations.dsPh.vm' },
  { value: 'zabbix', labelKey: 'integrations.dsType.zabbix', kind: 'metric', auth: 'zabbix', phKey: 'integrations.dsPh.zabbix' },
  { value: 'influxdb', labelKey: 'integrations.dsType.influxdb', kind: 'metric', auth: 'headers', phKey: 'integrations.dsPh.influxdb' },
  { value: 'graphite', labelKey: 'integrations.dsType.graphite', kind: 'metric', auth: 'headers', phKey: 'integrations.dsPh.graphite' },
  { value: 'es', labelKey: 'integrations.dsType.es', kind: 'metric', auth: 'headers', phKey: 'integrations.dsPh.es' },
  { value: 'opentsdb', labelKey: 'integrations.dsType.opentsdb', kind: 'metric', auth: 'headers', phKey: 'integrations.dsPh.opentsdb' },
  { value: 'http', labelKey: 'integrations.dsType.http', kind: 'metric', auth: 'headers', phKey: 'integrations.dsPh.http' },
  { value: 'loki', labelKey: 'integrations.dsType.loki', kind: 'log', auth: 'headers', phKey: 'integrations.dsPh.loki' },
  { value: 'sls', labelKey: 'integrations.dsType.sls', kind: 'log', auth: 'sls', phKey: 'integrations.dsPh.sls' },
  { value: 'cls', labelKey: 'integrations.dsType.cls', kind: 'log', auth: 'cls', phKey: 'integrations.dsPh.cls' },
  { value: 'graylog', labelKey: 'integrations.dsType.graylog', kind: 'log', auth: 'headers', phKey: 'integrations.dsPh.graylog' },
  { value: 'filebeat', labelKey: 'integrations.dsType.filebeat', kind: 'log', auth: 'headers', phKey: 'integrations.dsPh.filebeat' },
  { value: 'skywalking', labelKey: 'integrations.dsType.skywalking', kind: 'log', auth: 'headers', phKey: 'integrations.dsPh.skywalking' },
  { value: 'zipkin', labelKey: 'integrations.dsType.zipkin', kind: 'log', auth: 'headers', phKey: 'integrations.dsPh.zipkin' },
]
const TYPE_MAP = Object.fromEntries(TYPES.map((tp) => [tp.value, tp]))
const AMBIGUOUS = ['es', 'http']   // 两可类型：允许改注册类别

const form = reactive({
  name: '', type: 'prometheus', url: '',
  user: '', password: '', token: '', headersText: '',
  verify_ssl: false,   // TLS 校验默认关闭，兼容内网自签证书
})
const kindMode = ref('metric')   // es/http 的注册类别
const dsGuides = ref(null)       // GET /integrations/ds-guides（15 类教程）
const testing = ref(false)
const testResult = ref(null)
const saving = ref(false)

const meta = computed(() => {
  const m = TYPE_MAP[form.type] || TYPES[0]
  return { ...m, label: t(m.labelKey), ph: t(m.phKey) }
})
const kind = computed(() => (AMBIGUOUS.includes(form.type) ? kindMode.value : meta.value.kind))
const kindLabel = computed(() => (kind.value === 'metric' ? t('integrations.dsDlg.kind_metric') : t('integrations.dsDlg.kind_log')))

// 教程正文：优先后端 ds-guides（与指南库同源），缺失时用本地教程兜底——弹窗永远四区齐全
// （本地兜底值为 i18n 键，渲染时 t() 化）
const TUTORIALS = {
  prometheus: {
    steps: [
      'integrations.dsTut.prometheus.s1',
      'integrations.dsTut.prometheus.s2',
      'integrations.dsTut.prometheus.s3',
    ],
    notes: ['integrations.dsTut.prometheus.n1', 'integrations.dsTut.prometheus.n2'],
  },
  vm: {
    steps: [
      'integrations.dsTut.vm.s1',
      'integrations.dsTut.vm.s2',
      'integrations.dsTut.vm.s3',
    ],
    notes: ['integrations.dsTut.vm.n1', 'integrations.dsTut.vm.n2'],
  },
  zabbix: {
    steps: [
      'integrations.dsTut.zabbix.s1',
      'integrations.dsTut.zabbix.s2',
      'integrations.dsTut.zabbix.s3',
    ],
    notes: ['integrations.dsTut.zabbix.n1', 'integrations.dsTut.zabbix.n2'],
  },
  influxdb: {
    steps: [
      'integrations.dsTut.influxdb.s1',
      'integrations.dsTut.influxdb.s2',
      'integrations.dsTut.influxdb.s3',
    ],
    notes: ['integrations.dsTut.influxdb.n1', 'integrations.dsTut.influxdb.n2'],
  },
  graphite: {
    steps: [
      'integrations.dsTut.graphite.s1',
      'integrations.dsTut.graphite.s2',
    ],
    notes: ['integrations.dsTut.graphite.n1', 'integrations.dsTut.graphite.n2'],
  },
  es: {
    steps: [
      'integrations.dsTut.es.s1',
      'integrations.dsTut.es.s2',
      'integrations.dsTut.es.s3',
    ],
    notes: ['integrations.dsTut.es.n1', 'integrations.dsTut.es.n2'],
  },
  opentsdb: {
    steps: [
      'integrations.dsTut.opentsdb.s1',
      'integrations.dsTut.opentsdb.s2',
    ],
    notes: ['integrations.dsTut.opentsdb.n1', 'integrations.dsTut.opentsdb.n2'],
  },
  http: {
    steps: [
      'integrations.dsTut.http.s1',
      'integrations.dsTut.http.s2',
      'integrations.dsTut.http.s3',
    ],
    notes: ['integrations.dsTut.http.n1', 'integrations.dsTut.http.n2'],
  },
  loki: {
    steps: [
      'integrations.dsTut.loki.s1',
      'integrations.dsTut.loki.s2',
      'integrations.dsTut.loki.s3',
    ],
    notes: ['integrations.dsTut.loki.n1', 'integrations.dsTut.loki.n2'],
  },
  sls: {
    steps: [
      'integrations.dsTut.sls.s1',
      'integrations.dsTut.sls.s2',
      'integrations.dsTut.sls.s3',
    ],
    notes: ['integrations.dsTut.sls.n1', 'integrations.dsTut.sls.n2'],
  },
  cls: {
    steps: [
      'integrations.dsTut.cls.s1',
      'integrations.dsTut.cls.s2',
      'integrations.dsTut.cls.s3',
    ],
    notes: ['integrations.dsTut.cls.n1', 'integrations.dsTut.cls.n2'],
  },
  graylog: {
    steps: [
      'integrations.dsTut.graylog.s1',
      'integrations.dsTut.graylog.s2',
      'integrations.dsTut.graylog.s3',
    ],
    notes: ['integrations.dsTut.graylog.n1', 'integrations.dsTut.graylog.n2'],
  },
  filebeat: {
    steps: [
      'integrations.dsTut.filebeat.s1',
      'integrations.dsTut.filebeat.s2',
      'integrations.dsTut.filebeat.s3',
    ],
    notes: ['integrations.dsTut.filebeat.n1', 'integrations.dsTut.filebeat.n2'],
  },
  skywalking: {
    steps: [
      'integrations.dsTut.skywalking.s1',
      'integrations.dsTut.skywalking.s2',
      'integrations.dsTut.skywalking.s3',
    ],
    notes: ['integrations.dsTut.skywalking.n1', 'integrations.dsTut.skywalking.n2'],
  },
  zipkin: {
    steps: [
      'integrations.dsTut.zipkin.s1',
      'integrations.dsTut.zipkin.s2',
    ],
    notes: ['integrations.dsTut.zipkin.n1', 'integrations.dsTut.zipkin.n2'],
  },
}

const tutorial = computed(() => {
  const remote = (dsGuides.value || {})[form.type]
  if (remote && Array.isArray(remote.steps) && remote.steps.length) {
    return { label: remote.label || meta.value.label, steps: remote.steps, notes: remote.notes || [] }
  }
  const local = TUTORIALS[form.type] || { steps: [], notes: [] }
  return {
    label: meta.value.label,
    steps: local.steps.map((k) => t(k)),
    notes: local.notes.map((k) => t(k)),
  }
})

// 动态地址归一（前端渲染层）：{base_url}/{webhook_url}/{host} 按当前浏览器访问地址替换——
// 用户用 IP 访问显 IP，配了域名显域名；后端已替换过的也按 origin 再归一
function localText(s) {
  if (!s || typeof s !== 'string') return s
  return s
    .split('{webhook_url}').join(`${location.origin}/api/events/webhook`)
    .split('{base_url}').join(location.origin)
    .split('{host}').join(location.hostname)
}

async function loadGuides() {
  if (dsGuides.value) return
  try {
    dsGuides.value = await http.get('/integrations/ds-guides')
  } catch { /* 拦截器提示 */ }
}

function resetForm() {
  const edit = props.editSource
  const src = edit?.source || {}
  form.name = src.name || ''
  form.type = src.type || props.presetType || 'prometheus'
  form.url = src.url || ''
  form.user = src.user || ''
  form.password = src.password || ''
  form.token = src.token || ''
  form.headersText = Object.entries(src.headers || {}).map(([k, v]) => `${k}: ${v}`).join('\n')
  form.verify_ssl = src.verify_ssl ?? false   // 未保存该字段时默认关闭，兼容内网自签证书
  kindMode.value = edit ? edit.kind === 'log_sources' ? 'log' : 'metric' : (TYPE_MAP[form.type]?.kind || 'metric')
  testResult.value = null
}

watch(() => props.modelValue, (v) => {
  if (v) { loadGuides(); resetForm() }
})

function onTypeChange() {
  testResult.value = null
  if (!AMBIGUOUS.includes(form.type)) kindMode.value = meta.value.kind
}

// 表单修改即作废测试结果：
// 连通结果只对当前参数有效，改了任何字段都要重测
watch(form, () => {
  if (!testing.value) testResult.value = null
})

function parseHeaders() {
  const out = {}
  for (const line of (form.headersText || '').split('\n')) {
    const i = line.indexOf(':')
    if (i > 0) out[line.slice(0, i).trim()] = line.slice(i + 1).trim()
  }
  return Object.keys(out).length ? out : undefined
}

// 测试连通（保存前）：POST /datasources/test——取到版本/系统信息才算通
async function testConnection() {
  if (!form.url.trim()) return ElMessage.warning(t('integrations.dsDlg.msg.need_url'))
  testing.value = true
  testResult.value = null
  try {
    testResult.value = await http.post('/datasources/test', {
      name: form.name || meta.value.label,
      type: form.type,
      url: form.url.trim(),
      headers: parseHeaders(),
      user: form.user || undefined,
      password: form.password || undefined,
      token: form.token || undefined,
      verify_ssl: form.verify_ssl,
    })
  } catch { /* 拦截器提示 */ } finally { testing.value = false }
}

// 结果一行化：连通 · Zabbix 7.0.32（detail 里的"版本 x.y.z"折成产品口径）
const testLine = computed(() => {
  const r = testResult.value
  if (!r) return ''
  if (r.pending) return r.detail || t('integrations.dsDlg.test_pending_hint')
  if (!r.ok) return r.detail || t('integrations.dsDlg.down')
  const m = /版本\s+([\w.\-]+)/.exec(r.detail || '')
  const rest = (r.detail || '').replace(/版本\s+[\w.\-]+/, '').replace(/^ · | · $/g, '')
  const ver = m ? `${meta.value.label} ${m[1]}` : (r.detail || '')
  return rest ? `${ver} · ${rest}` : ver
})

async function save() {
  if (!form.name.trim()) return ElMessage.warning(t('integrations.dsDlg.msg.need_name'))
  if (!form.url.trim()) return ElMessage.warning(t('integrations.dsDlg.msg.need_url2'))
  saving.value = true
  try {
    const source = { name: form.name.trim(), type: form.type, url: form.url.trim(),
                     verify_ssl: form.verify_ssl }   // 保存时显式携带 TLS 校验选择
    const headers = parseHeaders()
    if (headers) source.headers = headers
    for (const k of ['user', 'password', 'token']) {
      if (form[k]) source[k] = form[k]
    }
    emit('save', {
      kind: kind.value === 'metric' ? 'metric_sources' : 'log_sources',
      source,
      editIndex: props.editSource ? props.editSource.index : -1,
      editKind: props.editSource ? props.editSource.kind : '',
    })
    emit('update:modelValue', false)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    :title="editSource ? t('integrations.dsDlg.edit_title') : t('integrations.dsDlg.add_title')"
    width="940px"
    class="dsd"
    @update:model-value="(v) => emit('update:modelValue', v)"
  >
    <div class="dsd-body">
      <!-- ① 教程（分步）+ ④ 注意事项 -->
      <div class="dsd-col">
        <div class="dsd-sec">
          <div class="dsd-title">{{ t('integrations.dsDlg.tutorial_title', { label: tutorial.label }) }}</div>
          <div v-if="tutorial.steps.length" class="dsd-steps">
            <div v-for="(s, i) in tutorial.steps" :key="i" class="dsd-step">
              <span class="dsd-idx">{{ i + 1 }}</span>
              <div class="dsd-step-text">{{ localText(s) }}</div>
            </div>
          </div>
          <div v-else class="dsd-empty">{{ t('integrations.dsDlg.no_tutorial') }}</div>
        </div>
        <div class="dsd-sec">
          <div class="dsd-title">{{ t('integrations.dsDlg.notes') }}</div>
          <el-alert type="warning" :closable="false" class="dsd-notes">
            <ul class="dsd-note-list">
              <li v-for="(n, i) in tutorial.notes" :key="i">{{ localText(n) }}</li>
              <li v-if="!tutorial.notes.length">{{ t('integrations.dsDlg.default_note') }}</li>
            </ul>
          </el-alert>
        </div>
      </div>

      <!-- ② 配置表单 + ③ 测试连通按钮与结果区 -->
      <div class="dsd-col">
        <div class="dsd-sec">
          <div class="dsd-title">{{ t('integrations.dsDlg.conn_title') }}</div>
          <el-form label-width="76px" size="default">
            <el-form-item :label="t('integrations.dsDlg.type')">
              <el-select v-model="form.type" style="width: 100%" @change="onTypeChange">
                <el-option v-for="tp in TYPES" :key="tp.value" :label="t(tp.labelKey)" :value="tp.value" />
              </el-select>
              <div v-if="AMBIGUOUS.includes(form.type)" class="dsd-kind">
                <span class="dsd-kind-label">{{ t('integrations.dsDlg.register_as') }}</span>
                <el-radio-group v-model="kindMode" size="small">
                  <el-radio-button value="metric">{{ t('integrations.dsDlg.kind_metric') }}</el-radio-button>
                  <el-radio-button value="log">{{ t('integrations.dsDlg.kind_log') }}</el-radio-button>
                </el-radio-group>
              </div>
            </el-form-item>
            <el-form-item :label="t('integrations.dsDlg.name')">
              <el-input v-model="form.name" :placeholder="t('integrations.dsDlg.name_ph', { n: meta.label.toLowerCase() })" />
            </el-form-item>
            <el-form-item :label="t('integrations.dsDlg.url')">
              <el-input v-model="form.url" :placeholder="meta.ph" />
            </el-form-item>
            <template v-if="meta.auth === 'zabbix'">
              <el-form-item label="API Token">
                <el-input v-model="form.token" :placeholder="t('integrations.dsDlg.zbx_token_ph')" />
              </el-form-item>
              <el-form-item :label="t('integrations.dsDlg.account')">
                <el-input v-model="form.user" :placeholder="t('integrations.dsDlg.zbx_user_ph')" />
              </el-form-item>
              <el-form-item :label="t('integrations.dsDlg.password')">
                <el-input v-model="form.password" type="password" show-password :placeholder="t('integrations.dsDlg.password_ph')" />
              </el-form-item>
            </template>
            <el-form-item v-else-if="meta.auth === 'headers'" :label="t('integrations.dsDlg.headers')">
              <el-input v-model="form.headersText" type="textarea" :rows="2"
                        :placeholder="t('integrations.dsDlg.headers_ph')" />
            </el-form-item>
            <el-form-item v-else-if="meta.auth === 'sls'" :label="t('integrations.dsDlg.creds')">
              <span class="dsd-sls">{{ t('integrations.dsDlg.sls_note') }}</span>
            </el-form-item>
            <el-form-item v-else-if="meta.auth === 'cls'" :label="t('integrations.dsDlg.creds')">
              <span class="dsd-sls">{{ t('integrations.dsDlg.cls_note') }}</span>
            </el-form-item>
            <el-form-item :label="t('integrations.dsDlg.tls')">
              <!-- TLS 校验默认关闭以兼容内网自签证书，可按需开启 -->
              <el-switch v-model="form.verify_ssl" />
              <span class="dsd-sls" style="margin-left: 8px">{{ t('integrations.dsDlg.tls_note') }}</span>
            </el-form-item>
          </el-form>
        </div>
        <div class="dsd-sec">
          <div class="dsd-title">{{ t('integrations.dsDlg.test_title') }}</div>
          <div class="dsd-test-bar">
            <el-button type="primary" plain :loading="testing" @click="testConnection">{{ t('integrations.dsDlg.test_btn') }}</el-button>
            <span class="dsd-test-hint">{{ t('integrations.dsDlg.test_hint') }}</span>
          </div>
          <div v-if="testResult" class="dsd-result">
            <el-tag :type="testResult.pending ? 'info' : (testResult.ok ? 'success' : 'danger')" size="small">
              {{ testResult.pending ? t('integrations.dsDlg.query_pending') : (testResult.ok ? t('integrations.dsDlg.up') : t('integrations.dsDlg.down')) }}
            </el-tag>
            <span class="dsd-result-line">{{ testLine }}</span>
            <span v-if="testResult.latency_ms != null" class="dsd-result-ms">{{ testResult.latency_ms }}ms</span>
          </div>
          <div v-else class="dsd-empty">{{ t('integrations.dsDlg.not_tested') }}</div>
        </div>
      </div>
    </div>
    <template #footer>
      <el-button @click="emit('update:modelValue', false)">{{ t('integrations.dsDlg.cancel') }}</el-button>
      <el-button type="primary" :loading="saving" @click="save">
        {{ editSource ? t('integrations.dsDlg.save_edit') : t('integrations.dsDlg.save_apply') }}（{{ kindLabel }}）
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.dsd-body { display: flex; gap: 22px; }
.dsd-col { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 18px; }
.dsd-sec { border: 1px solid var(--el-border-color-lighter); border-radius: 10px; padding: 12px 14px; }
.dsd-title {
  font-size: 13px; font-weight: 700; margin-bottom: 10px;
  padding-left: 9px; border-left: 3px solid var(--el-color-primary);
}
.dsd-steps { display: flex; flex-direction: column; gap: 9px; }
.dsd-step { display: flex; gap: 9px; font-size: 12.5px; line-height: 1.65; }
.dsd-idx {
  flex-shrink: 0; width: 18px; height: 18px; border-radius: 50%;
  background: var(--el-color-primary-light-7); color: var(--el-color-primary);
  font-size: 11px; font-weight: 700; display: flex; align-items: center; justify-content: center;
  margin-top: 1px;
}
.dsd-step-text { word-break: break-word; }
.dsd-notes { background: var(--el-color-warning-light-9); }
.dsd-note-list { margin: 0; padding-left: 16px; font-size: 12px; line-height: 1.8; color: var(--el-text-color-regular); }
.dsd-kind { display: flex; align-items: center; gap: 8px; margin-top: 8px; }
.dsd-kind-label { font-size: 12px; color: var(--el-text-color-secondary); }
.dsd-sls { font-size: 12px; color: var(--el-text-color-secondary); line-height: 1.6; }
.dsd-test-bar { display: flex; align-items: center; gap: 10px; }
.dsd-test-hint { font-size: 12px; color: var(--el-text-color-secondary); }
.dsd-result {
  margin-top: 10px; padding: 9px 12px; border-radius: 8px;
  background: var(--el-fill-color-light); display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
}
.dsd-result-line { font-size: 12.5px; font-weight: 600; word-break: break-all; }
.dsd-result-ms { font-size: 11px; color: var(--el-text-color-secondary); }
.dsd-empty { font-size: 12px; color: var(--el-text-color-secondary); }
</style>
