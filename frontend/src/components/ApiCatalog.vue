<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ArrowDown, ArrowRight } from '@element-plus/icons-vue'

import http from '@/api/http'
import CopyBtn from '@/components/CopyBtn.vue'
import { t } from '@/i18n'

const origin = window.location.origin

const spec = ref(null)
const loading = ref(true)
const loadError = ref('')
const keyword = ref('')
const collapsed = reactive({})
const openRows = reactive({})

// tag -> 分组名 i18n 键（未登记的 tag 原样展示）
const TAG_LABELS = {
  auth: 'integrations.apiTag.auth', agent: 'integrations.apiTag.agent', audit: 'integrations.apiTag.audit',
  machines: 'integrations.apiTag.machines', clouds: 'integrations.apiTag.clouds', settings: 'integrations.apiTag.settings',
  bootstrap: 'integrations.apiTag.bootstrap', assets: 'integrations.apiTag.assets', monitor: 'integrations.apiTag.monitor',
  logs: 'integrations.apiTag.logs', alerts: 'integrations.apiTag.alerts', messages: 'integrations.apiTag.messages',
  cost: 'integrations.apiTag.cost', inspections: 'integrations.apiTag.inspections', links: 'integrations.apiTag.links',
  diagnosis: 'integrations.apiTag.diagnosis', ai: 'integrations.apiTag.ai', dba: 'integrations.apiTag.dba',
  dashboard: 'integrations.apiTag.dashboard', expiry: 'integrations.apiTag.expiry', profiles: 'integrations.apiTag.profiles',
  changes: 'integrations.apiTag.changes', topology: 'integrations.apiTag.topology', events: 'integrations.apiTag.events',
  integrations: 'integrations.apiTag.integrations', k8s: 'integrations.apiTag.k8s', k8mgmt: 'integrations.apiTag.k8mgmt',
  tools: 'integrations.apiTag.tools', mcp: 'integrations.apiTag.mcp', synth: 'integrations.apiTag.synth',
  oncall: 'integrations.apiTag.oncall', 'multi-cloud': 'integrations.apiTag.multi-cloud',
  datasources: 'integrations.apiTag.datasources',
}

const METHOD_ORDER = ['get', 'post', 'put', 'patch', 'delete']
const AUTH_TAG_TYPE = {
  public: 'success', bearer: 'warning', optional: 'warning',
  webhook_token: 'info', bridge_token: 'info', agent_token: 'info', other: 'danger',
}

function resolveRef(schema, depth = 0) {
  if (!schema || typeof schema !== 'object' || depth > 6) return schema || {}
  if (schema.$ref) {
    const name = String(schema.$ref).split('/').pop()
    const target = spec.value?.components?.schemas?.[name]
    return target ? resolveRef(target, depth + 1) : { title: name }
  }
  return schema
}

function typeText(schema) {
  if (!schema) return 'object'
  if (schema.$ref) {
    const name = String(schema.$ref).split('/').pop()
    return resolveRef(schema).title || name
  }
  const s = resolveRef(schema)
  if (s.type === 'array') return `${typeText(s.items) || 'object'}[]`
  return s.title || s.type || (s.properties ? 'object' : 'object')
}

function exampleValue(schema) {
  if (!schema) return ''
  if (schema.example !== undefined) return schema.example
  if (schema.default !== undefined) return schema.default
  if (schema.enum?.length) return schema.enum.join(' / ')
  return ''
}

function schemaParams(schema) {
  const s = resolveRef(schema)
  const required = new Set(s.required || [])
  return Object.entries(s.properties || {}).map(([name, sub]) => ({
    name, in: 'body', type: typeText(sub),
    required: required.has(name), description: sub.description || resolveRef(sub).description || '',
    example: exampleValue(sub),
  }))
}

function sampleFromSchema(schema, depth = 0, requiredOnly = false, field = 'value') {
  if (!schema || depth > 6) return null
  const content = resolveRef(schema, depth)
  if (content.example !== undefined) return content.example
  if (content.default !== undefined) return content.default
  if (content.enum?.length) return content.enum[0]
  const variant = content.oneOf?.[0] || content.anyOf?.[0]
  if (variant) return sampleFromSchema(variant, depth + 1, requiredOnly, field)
  if (content.type === 'array') return [sampleFromSchema(content.items || {}, depth + 1, false, field)]
  if (content.type === 'object' || content.properties) {
    const required = new Set(content.required || [])
    const entries = Object.entries(content.properties || {})
      .filter(([name]) => !requiredOnly || required.has(name))
    return Object.fromEntries(entries.map(([name, sub]) => [
      name, sampleFromSchema(sub, depth + 1, requiredOnly, name),
    ]))
  }
  if (content.type === 'integer' || content.type === 'number') return 0
  if (content.type === 'boolean') return false
  if (content.type === 'null') return null
  return `<${field}>`
}

function requestBody(op) {
  return op.requestBody?.content?.['application/json'] || null
}

function bodyParams(op) {
  const js = requestBody(op)
  return js?.schema ? schemaParams(js.schema) : []
}

function bodySample(op) {
  const js = requestBody(op)
  if (!js) return null
  if (js.example !== undefined) return js.example
  const firstExample = Object.values(js.examples || {})[0]
  if (firstExample?.value !== undefined) return firstExample.value
  const schema = resolveRef(js.schema)
  if (!(schema.required || []).length && schema.example === undefined) return null
  return sampleFromSchema(js.schema, 0, true)
}

function responseEntries(op) {
  return Object.entries(op.responses || {})
    .filter(([code]) => /^2\d\d$/.test(code))
    .map(([code, response]) => {
      const content = response.content || {}
      const mime = content['application/json'] ? 'application/json' : Object.keys(content)[0] || ''
      const media = mime ? content[mime] : null
      return {
        code, mime, description: response.description || '',
        schema: media?.schema || null,
        example: media?.example !== undefined
          ? media.example
          : Object.values(media?.examples || {})[0]?.value,
      }
    })
}

function shQuote(value) {
  return `'${String(value).replaceAll("'", `'"'"'`)}'`
}

function authHeader(mode) {
  return {
    bearer: 'Authorization: Bearer $QIANSU_TOKEN',
    optional: 'Authorization: Bearer $QIANSU_TOKEN',
    webhook_token: 'X-Webhook-Token: $QIANSU_WEBHOOK_TOKEN',
    bridge_token: 'X-Copilotd-Token: $QIANSU_COPILOTD_TOKEN',
    agent_token: 'X-Agent-Token: $QIANSU_AGENT_TOKEN',
  }[mode] || ''
}

function curlFor(it) {
  const requiredQuery = it.params.filter((p) => p.in === 'query' && p.required)
  const qs = new URLSearchParams(requiredQuery.map((p) => [p.name, `<${p.name}>`])).toString()
  const url = `${it.url}${qs ? (it.url.includes('?') ? '&' : '?') + qs : ''}`
  const lines = [`curl -X ${it.method} ${shQuote(url)}`]
  const header = authHeader(it.authMode)
  if (header) lines.push(`-H "${header}"`)
  const body = bodySample(it.op)
  if (body !== null) {
    lines.push('-H "Content-Type: application/json"')
    lines.push(`--data ${shQuote(JSON.stringify(body))}`)
  }
  return lines.join(' \\\n  ')
}

const groups = computed(() => {
  if (!spec.value) return []
  const byTag = new Map()
  for (const [path, ops] of Object.entries(spec.value.paths || {})) {
    for (const method of METHOD_ORDER) {
      const op = ops[method]
      if (!op) continue
      const tag = (op.tags && op.tags[0]) || 'other'
      const params = (op.parameters || []).map((p) => ({
        name: p.name, in: p.in, type: typeText(p.schema),
        required: !!p.required, description: p.description || p.schema?.description || '',
        example: exampleValue(p.schema) || p.example || '',
      }))
      const body = bodyParams(op)
      const hasPathParam = path.includes('{')
      const needParams = hasPathParam || params.some((p) => p.required)
        || !!op.requestBody?.required
      if (!byTag.has(tag)) byTag.set(tag, { tag, items: [] })
      byTag.get(tag).items.push({
        method: method.toUpperCase(), key: `${method} ${path}`, path, op,
        url: `${origin}${path}`,
        summary: op.summary || '', description: op.description || '',
        authMode: op['x-auth-mode'] || 'other',
        params, body, needParams, responses: responseEntries(op),
      })
    }
  }
  for (const g of byTag.values()) {
    g.items.sort((a, b) => a.path.localeCompare(b.path)
      || METHOD_ORDER.indexOf(a.method.toLowerCase()) - METHOD_ORDER.indexOf(b.method.toLowerCase()))
  }
  return [...byTag.values()]
})

const filteredGroups = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  if (!kw) return groups.value
  return groups.value
    .map((g) => ({
      ...g,
      items: g.items.filter((it) =>
        (it.path + ' ' + it.summary + ' ' + it.description + ' ' + g.tag).toLowerCase().includes(kw)),
    }))
    .filter((g) => g.items.length)
})

const totalCount = computed(() => groups.value.reduce((n, g) => n + g.items.length, 0))
const pathCount = computed(() => Object.keys(spec.value?.paths || {}).length)

const curlExample = computed(() => [
  `# ${t('integrations.api.quick_create')}`,
  `export QIANSHU_BASE_URL=${shQuote(origin)}`,
  `export QIANSHU_TOKEN=${shQuote(t('integrations.api.quick_token'))}`,
  `# ${t('integrations.api.quick_verify')}`,
  'curl --fail-with-body "$QIANSU_BASE_URL/api/auth/me" \\',
  '  -H "Authorization: Bearer $QIANSU_TOKEN"',
].join('\n'))

function tagLabel(tag) {
  return TAG_LABELS[tag] ? t(TAG_LABELS[tag]) : tag
}

function authLabel(mode) {
  return t(`integrations.api.auth_mode_${mode}`)
}

function authHelp(mode) {
  return t(`integrations.api.auth_help_${mode}`)
}

function isGroupOpen(tag) {
  // 数百个操作默认折叠；搜索时展开匹配项，显式折叠仍然优先。
  return keyword.value.trim() ? collapsed[tag] !== false : collapsed[tag] === true
}

function toggleGroup(tag) {
  collapsed[tag] = !isGroupOpen(tag)
}

function toggleRow(key) {
  openRows[key] = !openRows[key]
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    spec.value = await http.get('/openapi.json')
  } catch (e) {
    loadError.value = t('integrations.api.load_failed')
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="api-catalog">
    <div class="api-hero">
      <div class="api-hero-head">
        <b class="api-title">{{ t('integrations.api.title') }}</b>
        <el-tag v-if="totalCount" size="small" effect="plain" type="info">
          {{ t('integrations.api.count', { operations: totalCount, paths: pathCount }) }}
        </el-tag>
        <div class="api-search">
          <el-input v-model="keyword" size="small" clearable :placeholder="t('integrations.api.search_ph')" />
        </div>
      </div>
      <div class="api-intro">{{ t('integrations.api.intro') }}</div>
      <div class="api-auth">
        <div class="api-auth-line"><span class="api-dot" />{{ t('integrations.api.token_guide') }}</div>
        <div class="api-auth-line"><span class="api-dot" />{{ t('integrations.api.token_permission') }}</div>
        <div class="api-doc-links">
          <a href="/api/docs" target="_blank" rel="noopener">Swagger UI</a>
          <a href="/api/redoc" target="_blank" rel="noopener">ReDoc</a>
          <a href="/api/openapi.json" target="_blank" rel="noopener">OpenAPI JSON</a>
        </div>
      </div>
      <div class="api-code-head">
        <b>{{ t('integrations.api.quickstart') }}</b>
        <CopyBtn :text="curlExample" />
      </div>
      <pre class="code-box">{{ curlExample }}</pre>
    </div>

    <el-alert v-if="loadError" type="error" :closable="false" :title="loadError" class="mb-12">
      <el-button size="small" type="primary" @click="load">{{ t('integrations.api.retry') }}</el-button>
    </el-alert>
    <div v-else-if="loading" class="api-loading">{{ t('integrations.api.loading') }}</div>
    <div v-else-if="!filteredGroups.length" class="api-loading">{{ t('integrations.api.no_match', { kw: keyword }) }}</div>

    <div v-for="g in filteredGroups" :key="g.tag" class="api-group">
      <div class="api-group-head" @click="toggleGroup(g.tag)">
        <el-icon class="api-caret" :size="14">
          <component :is="isGroupOpen(g.tag) ? ArrowDown : ArrowRight" />
        </el-icon>
        <b class="api-group-name">{{ tagLabel(g.tag) }}</b>
        <span class="api-group-tag">{{ g.tag }}</span>
        <el-tag size="small" effect="plain">{{ g.items.length }}</el-tag>
      </div>

      <template v-if="isGroupOpen(g.tag)">
        <div v-for="it in g.items" :key="it.key" class="api-item">
          <div class="api-row" @click="toggleRow(it.key)">
            <span class="api-method" :class="'m-' + it.method.toLowerCase()">{{ it.method }}</span>
            <code class="api-path">{{ it.path }}</code>
            <span class="api-summary">{{ it.summary }}</span>
            <el-tag size="small" effect="plain" :type="AUTH_TAG_TYPE[it.authMode] || 'info'">
              {{ authLabel(it.authMode) }}
            </el-tag>
            <el-tag v-if="it.needParams" size="small" effect="plain" type="info">{{ t('integrations.api.need_params') }}</el-tag>
          </div>
          <div class="api-url-row">
            <span class="api-url">{{ it.url }}</span>
            <CopyBtn :text="it.url" />
          </div>
          <div v-if="openRows[it.key]" class="api-detail">
            <div class="api-auth-detail">
              <b>{{ t('integrations.api.authentication') }}：</b>{{ authHelp(it.authMode) }}
            </div>
            <div v-if="it.description" class="api-desc">{{ it.description }}</div>
            <el-alert v-if="it.method !== 'GET'" type="warning" :closable="false" class="api-write-warning">
              {{ t('integrations.api.write_warning') }}
            </el-alert>
            <template v-if="it.params.length || it.body.length">
              <div class="api-params-title">{{ t('integrations.api.params') }}</div>
              <table class="api-params">
                <thead>
                  <tr>
                    <th>{{ t('integrations.api.th_name') }}</th><th>{{ t('integrations.api.th_in') }}</th><th>{{ t('integrations.api.th_type') }}</th><th>{{ t('integrations.api.th_required') }}</th><th>{{ t('integrations.api.th_example') }}</th><th>{{ t('integrations.api.th_desc') }}</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="p in [...it.params, ...it.body]" :key="p.in + p.name">
                    <td><code>{{ p.name }}</code></td>
                    <td>{{ p.in }}</td>
                    <td>{{ p.type }}</td>
                    <td>
                      <el-tag size="small" :type="p.required ? 'danger' : 'info'" effect="plain">
                        {{ p.required ? t('integrations.api.required') : t('integrations.api.optional') }}
                      </el-tag>
                    </td>
                    <td><code v-if="p.example !== ''">{{ p.example }}</code><span v-else>—</span></td>
                    <td class="api-td-desc">{{ p.description || '—' }}</td>
                  </tr>
                </tbody>
              </table>
            </template>
            <div v-else class="api-desc">{{ t('integrations.api.no_params') }}</div>

            <div class="api-code-head">
              <b>{{ t('integrations.api.request_example') }}</b>
              <CopyBtn :text="curlFor(it)" />
            </div>
            <pre class="code-box">{{ curlFor(it) }}</pre>

            <div class="api-params-title">{{ t('integrations.api.response') }}</div>
            <template v-if="it.responses.length">
              <table class="api-params">
                <thead><tr><th>{{ t('integrations.api.response_status') }}</th><th>{{ t('integrations.api.th_type') }}</th><th>{{ t('integrations.api.th_desc') }}</th></tr></thead>
                <tbody>
                  <tr v-for="r in it.responses" :key="r.code">
                    <td><code>{{ r.code }}</code></td>
                    <td>{{ r.schema ? typeText(r.schema) : t('integrations.api.no_content') }}</td>
                    <td>{{ r.description || '—' }}</td>
                  </tr>
                </tbody>
              </table>
              <template v-for="r in it.responses" :key="r.code + '-example'">
                <pre v-if="r.schema && Object.keys(r.schema).length" class="code-box response-example">{{ JSON.stringify(r.example !== undefined ? r.example : sampleFromSchema(r.schema), null, 2) }}</pre>
              </template>
            </template>
            <div v-else class="api-desc">{{ t('integrations.api.response_undocumented') }}</div>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.api-catalog { display: flex; flex-direction: column; gap: 12px; }
.mb-12 { margin-bottom: 12px; }
.api-hero {
  border: 1px solid var(--el-border-color-lighter); border-radius: 12px;
  padding: 16px 18px; background: var(--el-fill-color-blank);
}
.api-hero-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.api-title { font-size: 15px; }
.api-search { margin-left: auto; width: 260px; }
.api-intro { margin-top: 10px; font-size: 12.5px; line-height: 1.8; color: var(--el-text-color-regular); }
.api-auth { margin-top: 8px; display: flex; flex-direction: column; gap: 6px; }
.api-auth-line {
  font-size: 12.5px; color: var(--el-text-color-regular);
  display: flex; gap: 7px; line-height: 1.7;
}
.api-dot {
  flex-shrink: 0; width: 6px; height: 6px; border-radius: 50%;
  background: var(--el-color-primary); margin-top: 8px;
}
.api-doc-links { display: flex; gap: 16px; margin: 4px 0 0 13px; font-size: 12.5px; }
.api-doc-links a { color: var(--el-color-primary); text-decoration: none; }
.api-doc-links a:hover { text-decoration: underline; }
.api-code-head {
  display: flex; align-items: center; gap: 10px;
  margin: 12px 0 6px; font-size: 13px;
}
.code-box {
  background: var(--el-fill-color-light); padding: 12px 14px; border-radius: 8px;
  font-size: 12px; overflow-x: auto; font-family: 'JetBrains Mono', Consolas, monospace;
  margin: 0; white-space: pre-wrap; overflow-wrap: anywhere;
}
.api-loading {
  padding: 26px 0; text-align: center;
  color: var(--el-text-color-secondary); font-size: 13px;
}
.api-group {
  border: 1px solid var(--el-border-color-lighter); border-radius: 12px;
  background: var(--el-fill-color-blank); overflow: hidden;
}
.api-group-head {
  display: flex; align-items: center; gap: 9px;
  padding: 11px 16px; cursor: pointer; user-select: none;
  background: var(--el-fill-color-light);
  transition: background 0.15s;
}
.api-group-head:hover { background: var(--el-color-primary-light-9); }
.api-caret { color: var(--el-text-color-secondary); }
.api-group-name { font-size: 13.5px; }
.api-group-tag {
  font-size: 11px; color: var(--el-text-color-secondary);
  font-family: 'JetBrains Mono', Consolas, monospace;
}
.api-item { border-top: 1px solid var(--el-border-color-lighter); padding: 10px 16px; }
.api-item:hover { background: var(--el-fill-color-light); }
.api-row {
  display: flex; align-items: center; gap: 10px;
  cursor: pointer; min-width: 0; flex-wrap: wrap;
}
.api-method {
  flex-shrink: 0; width: 54px; text-align: center;
  font-size: 11px; font-weight: 700; color: #fff;
  padding: 3px 0; border-radius: 5px; letter-spacing: 0.5px;
}
.m-get { background: linear-gradient(135deg, #34d399, #059669); }
.m-post { background: linear-gradient(135deg, #60a5fa, #3b82f6); }
.m-put { background: linear-gradient(135deg, #fbbf24, #d97706); }
.m-patch { background: linear-gradient(135deg, #c4b5fd, #8b5cf6); }
.m-delete { background: linear-gradient(135deg, #f87171, #dc2626); }
.api-path {
  font-family: 'JetBrains Mono', Consolas, monospace; font-size: 12.5px;
  color: var(--el-text-color-primary); overflow-wrap: anywhere;
}
.api-summary {
  flex: 1; min-width: 120px; font-size: 12px; color: var(--el-text-color-secondary);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.api-url-row {
  display: flex; align-items: center; gap: 8px;
  margin-top: 5px; padding-left: 64px; min-width: 0;
}
.api-url {
  font-size: 11.5px; color: var(--el-text-color-secondary);
  font-family: 'JetBrains Mono', Consolas, monospace;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.api-detail {
  margin-top: 8px; padding: 10px 12px; border-radius: 8px;
  background: var(--el-fill-color-light); overflow-x: auto;
}
.api-auth-detail { font-size: 12px; line-height: 1.7; color: var(--el-text-color-regular); margin-bottom: 7px; }
.api-desc { font-size: 12px; line-height: 1.7; color: var(--el-text-color-regular); white-space: pre-line; }
.api-write-warning { margin-top: 8px; }
.api-params-title { font-size: 12px; font-weight: 700; margin: 12px 0 5px; }
.api-params { width: 100%; border-collapse: collapse; font-size: 11.5px; }
.api-params th {
  text-align: left; color: var(--el-text-color-secondary); font-weight: 600;
  padding: 4px 8px; border-bottom: 1px solid var(--el-border-color-lighter);
}
.api-params td {
  padding: 4px 8px; border-bottom: 1px solid var(--el-border-color-extra-light);
  color: var(--el-text-color-regular); vertical-align: top;
}
.api-params code {
  font-family: 'JetBrains Mono', Consolas, monospace; font-size: 11.5px;
}
.api-td-desc { word-break: break-word; min-width: 180px; }
.response-example { margin-top: 8px; max-height: 360px; overflow: auto; }
@media (max-width: 800px) {
  .api-search { width: 100%; margin-left: 0; }
  .api-url-row { padding-left: 0; }
}
</style>
