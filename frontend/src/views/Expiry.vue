<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { syncAssets } from '@/api/assets'
import { agentAskUrl } from '@/api/agent'
import { backfillDomains, domainDetail, domainList, updateDomain } from '@/api/domains'
import { expiryItems, ignoreExpiry, registerExternalDomain, removeExternalDomain, unignoreExpiry } from '@/api/expiry'
import { t } from '@/i18n'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const canWrite = computed(() => ['admin', 'operator'].includes(auth.role))

const loading = ref(false)
const syncing = ref(false)
const data = ref(null)
const loadError = ref('')

// 筛选项 labelKey 渲染时取 i18n（zh=原选项文案）
const CATEGORY_OPTIONS = [
  { value: '', labelKey: 'expiry.cat.all' },
  { value: 'ssl_cert', labelKey: 'expiry.cat.ssl_cert' },
  { value: 'domain', labelKey: 'expiry.cat.domain' },
  { value: 'ecs', labelKey: 'expiry.cat.ecs' },
  { value: 'rds', labelKey: 'expiry.cat.rds' },
  { value: 'redis', labelKey: 'expiry.cat.redis' },
  { value: 'es', labelKey: 'expiry.cat.es' },
  { value: 'pack', labelKey: 'expiry.cat.pack' },
]
const LEVEL_OPTIONS = [
  { value: '', labelKey: 'expiry.level.all' },
  { value: 'expired', labelKey: 'expiry.level.expired' },
  { value: 'critical', labelKey: 'expiry.level.critical' },
  { value: 'warning', labelKey: 'expiry.level.warning' },
  { value: 'ok', labelKey: 'expiry.level.farther' },
]
const LEVEL_TAGS = { expired: 'danger', critical: 'danger', warning: 'warning', ok: 'success' }
const LEVEL_LABEL_KEYS = { expired: 'expiry.level.expired', critical: 'expiry.level.critical', warning: 'expiry.level.warning', ok: 'expiry.level.ok' }

const filters = reactive({ category: '', level: '', showIgnored: false, days: '' })

// 总览卡片跳转带筛选，只显示对应到期项
import { useRoute } from 'vue-router'
const _route = useRoute()
if (_route.query.days !== undefined) filters.days = String(_route.query.days)

const items = computed(() => {
  let arr = data.value?.items || []
  if (filters.days === '-1') arr = arr.filter((x) => (x.days_left ?? 0) < 0)
  else if (filters.days) arr = arr.filter((x) => (x.days_left ?? 999) <= Number(filters.days))
  return arr
})

// 顶部统计只算当前视图可见项；已忽略项单独提示，避免"红字有 7 条、列表一条没有"。
// 用 items（已应用分类/级别/剩余天数筛选）现算，口径与表格一致，不能用后端全量 summary。
const statSummary = computed(() => {
  if (!data.value) return {}
  const arr = items.value
  const n = (lv) => arr.filter((i) => i.level === lv).length
  return { expired: n('expired'), critical: n('critical'), warning: n('warning'), total: arr.length }
})

function filterLevel(level) {
  filters.level = level
  load()
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const d = await expiryItems({
      category: filters.category, level: filters.level,
      include_ignored: filters.showIgnored ? 1 : 0,
    })
    data.value = d
  } catch (e) {
    data.value = null
    loadError.value = e?.response?.data?.detail || t('expiry.table.load_fail')
  } finally {
    loading.value = false
  }
}

// 域名/证书是单独的同步器（每日 09:20 自动跑一次）
async function doSync() {
  syncing.value = true
  try {
    const d = await syncAssets('domain')
    const c = await syncAssets('ssl_cert')
    ElMessage.success(t('expiry.msg.sync_done', { domains: d.total, certs: c.total }))
    // /assets/sync 已标废弃，后端随响应给多语言 notice——透出给用户，
    // 提示正式同步入口在「运维管理 → 同步云资产」（域名/证书同步过渡期仍走本接口）
    if (d.deprecated && d.notice) ElMessage.warning({ message: d.notice, duration: 8000 })
    await load()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('expiry.msg.sync_failed'))
  } finally {
    syncing.value = false
  }
}

/* ---------------- 外部域名到期登记：本账号 API 查不到的域名 ---------------- */
const domainDialog = reactive({ visible: false, saving: false, form: { domain: '', expire_time: '', note: '' } })

function openDomainDialog() {
  domainDialog.form = { domain: '', expire_time: '', note: '' }
  domainDialog.visible = true
}

async function saveDomain() {
  const f = domainDialog.form
  if (!f.domain.trim() || !f.expire_time) {
    ElMessage.warning(t('expiry.msg.domain_required'))
    return
  }
  domainDialog.saving = true
  try {
    await registerExternalDomain(f)
    ElMessage.success(t('expiry.msg.domain_registered', { domain: f.domain.trim(), date: f.expire_time }))
    domainDialog.visible = false
    await load()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('expiry.msg.register_failed'))
  } finally {
    domainDialog.saving = false
  }
}

async function toggleIgnore(row) {
  const key = `${row.category}:${row.instance_id || row.name}`
  try {
    if (row.ignored) {
      await unignoreExpiry(key)
      ElMessage.success(t('expiry.msg.resumed'))
    } else {
      await ignoreExpiry({ target_key: key, note: '' })
      ElMessage.success(t('expiry.msg.ignored'))
    }
    load()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || t('expiry.msg.op_failed'))
  }
}

async function removeDomain(row) {
  try {
    await ElMessageBox.confirm(t('expiry.remove.confirm', { name: row.name }), t('expiry.remove.title'), { type: 'warning' })
  } catch { return }
  try {
    await removeExternalDomain(row.instance_id)
    ElMessage.success(t('expiry.msg.deleted'))
    await load()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('expiry.msg.delete_failed'))
  }
}

const fmtDate = (s) => (s ? s.slice(0, 10) : '-')
const daysText = (d) => (d < 0 ? t('expiry.days.expired', { n: Math.abs(d) }) : t('expiry.days.left', { n: d }))

// 证书单一事实：来源标注 tls_probe/cas_order（兼容历史 synth/cas 值）
const certSrcLabel = (s) => ({
  tls_probe: t('expiry.det.src_synth'), cas_order: t('expiry.det.src_cas'),
  synth: t('expiry.det.src_synth'), cas: t('expiry.det.src_cas'),
}[s] || '—')
const certSrcShort = (s) => ({
  tls_probe: t('expiry.det.src_synth_short'), cas_order: t('expiry.det.src_cas_short'),
  synth: t('expiry.det.src_synth_short'), cas: t('expiry.det.src_cas_short'),
}[s] || '—')
// 来源不一致：两来源日期并列 + 序列号不匹配提示（不静默取一）
const conflictText = (c) => {
  if (!c) return ''
  let s = t('common.cert.conflict_pair', {
    probe: fmtDate(c.probe_not_after || c.cert_probe_not_after),
    order: fmtDate(c.order_not_after || c.cert_order_not_after),
    diff: c.diff_days ?? c.cert_diff_days ?? 0,
  })
  if (c.serial_mismatch || c.cert_serial_mismatch) s = `${t('common.cert.conflict_serial')}；${s}`
  return s
}

// 问智能体深链：到期项带对象上下文落到智能体新会话首条
function askAgent(row) {
  const exp = row.expire_time ? t('expiry.ask.exp', { date: fmtDate(row.expire_time) }) : ''
  const q = t('expiry.ask.q', {
    category: row.category_label || row.category, name: row.name,
    id: row.instance_id || '-', exp, days: daysText(row.days_left),
  })
  window.open(agentAskUrl(q, {
    page: '/expiry', category: row.category, name: row.name,
    instance_id: row.instance_id, expire_time: row.expire_time, days_left: row.days_left,
  }), '_blank')
}

/* ---------------- 域名登记：域名→证书→SLB/CDN→业务一处收口 ---------------- */
// ?tab= 深链：完整度卡「域名缺登记」落 /expiry?tab=domains，不落默认「到期总账」页签
const activeTab = ref(['expiry', 'domains'].includes(_route.query.tab) ? String(_route.query.tab) : 'expiry')
const domLoading = ref(false)
const domError = ref('')
const domRows = ref([])
const domTotal = ref(0)
const domFilters = reactive({ q: '', level: '', source: '', status: '' })

const SOURCE_OPTIONS = [
  { value: '', labelKey: 'expiry.src.all' },
  { value: 'link', labelKey: 'expiry.src.link' },
  { value: 'cdn', labelKey: 'expiry.src.cdn' },
  { value: 'profile', labelKey: 'expiry.src.profile' },
  { value: 'synth', labelKey: 'expiry.src.synth' },
  { value: 'diagnosis', labelKey: 'expiry.src.diagnosis' },
  { value: 'biz', labelKey: 'expiry.src.biz' },
  { value: 'registrar', labelKey: 'expiry.src.registrar' },
  { value: 'cert', labelKey: 'expiry.src.cert' },
]
const DOM_STATUS_OPTIONS = [
  { value: '', labelKey: 'expiry.dom_status.all' },
  { value: 'active', labelKey: 'expiry.dom_status.active' },
  { value: 'disabled', labelKey: 'expiry.dom_status.disabled' },
]
const CERT_LEVEL_TAGS = { expired: 'danger', critical: 'danger', warning: 'warning', ok: 'success', none: 'info' }

// refresh=true：写角色点「刷新」时先跑六源回填再列；筛选/切页签只读列表，
// GET 不再有写副作用（viewer 也能安全打开）
async function loadDomains(refresh = false) {
  domLoading.value = true
  domError.value = ''
  try {
    if (refresh && canWrite.value) await backfillDomains()
    const d = await domainList({
      q: domFilters.q, level: domFilters.level,
      source: domFilters.source, status: domFilters.status,
    })
    domRows.value = d.items || []
    domTotal.value = d.total || 0
  } catch (e) {
    domRows.value = []
    domTotal.value = 0
    domError.value = e?.response?.data?.detail || t('expiry.dom.load_fail')
  } finally {
    domLoading.value = false
  }
}

function onTabChange(name) {
  if (name === 'domains' && !domRows.value.length) loadDomains()
}

function certDaysSort(a, b) {
  return (a.cert_days_left ?? 10 ** 6) - (b.cert_days_left ?? 10 ** 6)
}

function canOpenDetail(name) {
  return !!name && String(name).includes('.') && !String(name).includes('*')
}

/* 详情：聚合六处证据（链路/CDN/画像/拨测/诊断/业务线 + 证书 + 注册资产） */
const detailDialog = reactive({ visible: false, loading: false, domain: '', data: null })

async function openDetail(domain) {
  if (!canOpenDetail(domain)) return
  detailDialog.domain = domain
  detailDialog.data = null
  detailDialog.visible = true
  detailDialog.loading = true
  try {
    detailDialog.data = await domainDetail(domain)
  } catch (e) {
    detailDialog.visible = false
    ElMessage.error(e?.response?.data?.detail || t('expiry.dom.detail_fail'))
  } finally {
    detailDialog.loading = false
  }
}

/* 编辑：精确改（只提交改动字段；业务/厂商/账号清空=恢复自动/归属登记值） */
const editDialog = reactive({
  visible: false, saving: false, domain: '',
  form: { business: '', vendor: '', account_name: '', note: '', status: 'active' },
  orig: { business: '', vendor: '', account_name: '', note: '', status: 'active' },
})

function editRow(row) {
  editDialog.domain = row.domain
  editDialog.form = {
    business: row.business || '', vendor: row.vendor || '',
    account_name: row.account_name || '', note: row.note || '',
    status: row.status || 'active',
  }
  editDialog.orig = { ...editDialog.form }
  editDialog.visible = true
}

function editFromDetail() {
  const r = detailDialog.data?.registry
  if (r) editRow(r)
}

async function saveEdit() {
  const f = editDialog.form
  const o = editDialog.orig
  const patch = {}
  for (const k of ['business', 'vendor', 'account_name', 'note', 'status']) {
    if ((f[k] ?? '') !== (o[k] ?? '')) patch[k] = f[k]
  }
  if (!Object.keys(patch).length) {
    ElMessage.info(t('expiry.msg.no_change'))
    return
  }
  editDialog.saving = true
  try {
    await updateDomain(editDialog.domain, patch)
    ElMessage.success(t('expiry.msg.saved', { domain: editDialog.domain }))
    editDialog.visible = false
    await loadDomains()
    if (detailDialog.visible && detailDialog.domain === editDialog.domain) {
      openDetail(editDialog.domain)
    }
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('expiry.msg.save_failed'))
  } finally {
    editDialog.saving = false
  }
}

onMounted(() => {
  load()
  loadDomains()
})
</script>

<template>
  <div class="page">
    <el-tabs v-model="activeTab" @tab-change="onTabChange">
      <el-tab-pane :label="t('expiry.tab.expiry')" name="expiry">
    <div class="head">
      <div>
        <span style="font-weight: 600; font-size: 16px">{{ t('expiry.tab.expiry') }}</span>
        <span class="sub">{{ t('expiry.head.sub') }}</span>
      </div>
      <span>
        <el-button v-if="canWrite" size="small" type="success" @click="openDomainDialog">{{ t('expiry.act.register') }}</el-button>
        <el-button v-if="canWrite" size="small" :loading="syncing" @click="doSync">{{ t('expiry.act.sync') }}</el-button>
        <el-select v-model="filters.days" :placeholder="t('expiry.filter.days_ph')" clearable style="width: 150px" @change="load">
          <el-option :label="t('expiry.filter.d.expired')" value="-1" />
          <el-option :label="t('expiry.filter.d.7')" value="7" />
          <el-option :label="t('expiry.filter.d.22')" value="22" />
          <el-option :label="t('expiry.filter.d.30')" value="30" />
          <el-option :label="t('expiry.filter.d.90')" value="90" />
        </el-select>
        <el-switch v-model="filters.showIgnored" :active-text="t('expiry.switch.show_ignored')" :inactive-text="t('expiry.switch.active_only')" @change="load" />
      </span>
    </div>

    <div class="stat-row">
      <div class="stat expired clickable" @click="filterLevel('expired')">
        <div class="num">{{ statSummary.expired ?? '-' }}</div>
        <div class="label">{{ t('expiry.stat.expired') }}</div>
      </div>
      <div class="stat critical clickable" @click="filterLevel('critical')">
        <div class="num">{{ statSummary.critical ?? '-' }}</div>
        <div class="label">{{ t('expiry.stat.critical') }}</div>
      </div>
      <div class="stat warning clickable" @click="filterLevel('warning')">
        <div class="num">{{ statSummary.warning ?? '-' }}</div>
        <div class="label">{{ t('expiry.stat.warning') }}</div>
      </div>
      <div class="stat total clickable" @click="filterLevel('')">
        <div class="num">{{ statSummary.total ?? '-' }}</div>
        <div class="label">{{ t('expiry.stat.total') }}</div>
        <div v-if="!filters.showIgnored && data?.ignored_summary?.total" class="sub ignored-note">
          {{ t('expiry.stat.ignored_n', { n: data.ignored_summary.total }) }}
        </div>
      </div>
      <div class="stat sync">
        <div class="num small">{{ t('expiry.stat.sync_time') }}</div>
        <div class="label">{{ t('expiry.stat.sync_label') }}</div>
      </div>
    </div>
    <el-alert v-if="loadError" type="error" :closable="false" show-icon :title="loadError" style="margin-bottom: 10px">
      <el-button size="small" @click="load">{{ t('common.retry') }}</el-button>
    </el-alert>
    <div v-if="data?.stale_omitted?.ssl_cert" class="sub stale-note">
      {{ t('expiry.stat.stale_cert_n', { n: data.stale_omitted.ssl_cert }) }}
    </div>
    <!-- 停用账号的旧资产不进账：明说数量与恢复方式，不静默少东西 -->
    <div v-if="data?.stale_omitted?.disabled_account" class="sub stale-note">
      {{ t('expiry.stat.disabled_n', { n: data.stale_omitted.disabled_account }) }}
    </div>

    <div class="filters">
      <el-select v-model="filters.category" style="width: 160px" :placeholder="t('expiry.filter.category_ph')" @change="load">
        <el-option v-for="c in CATEGORY_OPTIONS" :key="c.value" :value="c.value" :label="t(c.labelKey)" />
      </el-select>
      <el-select v-model="filters.level" style="width: 130px" :placeholder="t('expiry.filter.level_ph')" @change="load">
        <el-option v-for="l in LEVEL_OPTIONS" :key="l.value" :value="l.value" :label="t(l.labelKey)" />
      </el-select>
      <span class="sub">{{ t('expiry.filter.sort_tip') }}</span>
    </div>

    <el-table stripe :data="items" v-loading="loading" border
              :empty-text="loadError ? t('expiry.table.load_fail') : loading ? t('expiry.table.loading') : t('expiry.table.empty')">
      <el-table-column :label="t('expiry.col.level')" width="90">
        <template #default="{ row }">
          <el-tag :type="LEVEL_TAGS[row.level]" size="small" effect="dark">{{ t(LEVEL_LABEL_KEYS[row.level]) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('expiry.col.category')" width="150">
        <template #default="{ row }">{{ row.category_label }}</template>
      </el-table-column>
      <!-- 归属标识：多账号看得出属于谁 -->
      <el-table-column :label="t('expiry.col.vendor_account')" width="150">
        <template #default="{ row }">
          <span v-if="row.vendor || row.account_name" class="sub">
            {{ [row.vendor, row.account_name].filter(Boolean).join(' / ') }}
          </span>
          <span v-else class="sub">—</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('expiry.col.name')" min-width="200" show-overflow-tooltip>
        <template #default="{ row }">
          <el-button v-if="(row.category === 'domain' || row.category === 'ssl_cert') && canOpenDetail(row.name)"
                     text type="primary" class="dom-link" @click="openDetail(row.name)">{{ row.name }}</el-button>
          <span v-else>{{ row.name }}</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('expiry.col.instance')" min-width="180" show-overflow-tooltip>
        <template #default="{ row }">
          <span class="mono sub">{{ row.instance_id }}</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('expiry.col.expire')" width="110">
        <template #default="{ row }">{{ fmtDate(row.expire_time) }}</template>
      </el-table-column>
      <el-table-column :label="t('expiry.col.days_left')" width="100">
        <template #default="{ row }">
          <span :class="{ 'days-bad': row.days_left <= 30 }">{{ daysText(row.days_left) }}</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('expiry.col.hint')" min-width="220">
        <template #default="{ row }">
          <span class="sub">{{ row.hint }}</span>
          <el-tag v-if="row.status === 'Manual'" size="small" type="info" style="margin-left: 6px">{{ t('expiry.tag.manual') }}</el-tag>
          <!-- 证书行统一解析：来源标注 + 两来源不一致提示 -->
          <template v-if="row.category === 'ssl_cert'">
            <el-tag size="small" type="info" style="margin-left: 6px">{{ certSrcShort(row.cert_source) }}</el-tag>
            <el-tooltip v-if="row.source_conflict" :content="conflictText(row)" placement="top">
              <el-tag size="small" type="warning" style="margin-left: 6px">{{ t('common.cert.conflict') }}</el-tag>
            </el-tooltip>
          </template>
        </template>
      </el-table-column>
      <el-table-column :label="t('expiry.col.ask')" width="100">
        <template #default="{ row }">
          <el-button size="small" text type="primary" @click="askAgent(row)">{{ t('expiry.act.ask') }}</el-button>
        </template>
      </el-table-column>
      <el-table-column v-if="canWrite" :label="t('expiry.col.ops')" width="130">
        <template #default="{ row }">
          <el-button v-if="row.ignored" type="primary" size="default" @click="toggleIgnore(row)">{{ t('expiry.act.resume') }}</el-button>
          <el-button v-else size="default" @click="toggleIgnore(row)">{{ t('expiry.act.ignore') }}</el-button>
          <el-button v-if="row.status === 'Manual'" type="danger" size="default" text @click="removeDomain(row)">{{ t('expiry.act.delete') }}</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 登记外部域名 -->
    <el-dialog v-model="domainDialog.visible" :title="t('expiry.dlg.title')" width="460px">
      <el-form label-width="90px">
        <el-form-item :label="t('expiry.dlg.domain')" required>
          <el-input v-model="domainDialog.form.domain" :placeholder="t('expiry.dlg.domain_ph')" maxlength="255" />
        </el-form-item>
        <el-form-item :label="t('expiry.dlg.expire')" required>
          <el-date-picker v-model="domainDialog.form.expire_time" type="date" value-format="YYYY-MM-DD"
                          :placeholder="t('expiry.dlg.expire_ph')" style="width: 100%" />
        </el-form-item>
        <el-form-item :label="t('expiry.dlg.note')">
          <el-input v-model="domainDialog.form.note" :placeholder="t('expiry.dlg.note_ph')" maxlength="500" />
        </el-form-item>
      </el-form>
      <div class="sub" style="margin-bottom: 8px">
        {{ t('expiry.dlg.tip') }}
      </div>
      <template #footer>
        <el-button @click="domainDialog.visible = false">{{ t('expiry.common.cancel') }}</el-button>
        <el-button type="primary" :loading="domainDialog.saving" @click="saveDomain">{{ t('expiry.dlg.submit') }}</el-button>
      </template>
    </el-dialog>
      </el-tab-pane>

      <!-- 域名登记：域名→证书→SLB/CDN→业务 一处收口 -->
      <el-tab-pane :label="t('expiry.tab.domains', { n: domTotal })" name="domains">
        <div class="head">
          <div>
            <span style="font-weight: 600; font-size: 16px">{{ t('expiry.dom.title') }}</span>
            <span class="sub">{{ t('expiry.dom.sub') }}</span>
          </div>
          <span>
            <el-button v-if="canWrite" size="small" :loading="domLoading" @click="loadDomains(true)">{{ t('expiry.dom.refresh') }}</el-button>
            <el-select v-model="domFilters.level" :placeholder="t('expiry.dom.ph.level')" clearable style="width: 120px" @change="loadDomains()">
              <el-option v-for="l in LEVEL_OPTIONS" :key="l.value" :value="l.value" :label="t(l.labelKey)" />
            </el-select>
            <el-select v-model="domFilters.source" :placeholder="t('expiry.dom.ph.source')" clearable style="width: 120px" @change="loadDomains()">
              <el-option v-for="s in SOURCE_OPTIONS" :key="s.value" :value="s.value" :label="t(s.labelKey)" />
            </el-select>
            <el-select v-model="domFilters.status" :placeholder="t('expiry.dom.ph.status')" clearable style="width: 100px" @change="loadDomains()">
              <el-option v-for="s in DOM_STATUS_OPTIONS" :key="s.value" :value="s.value" :label="t(s.labelKey)" />
            </el-select>
            <el-input v-model="domFilters.q" :placeholder="t('expiry.dom.ph.q')" clearable style="width: 170px"
                      @keyup.enter="loadDomains()" @clear="loadDomains()" />
          </span>
        </div>

        <el-alert v-if="domError" type="error" :closable="false" show-icon :title="domError" style="margin-bottom: 10px">
          <el-button size="small" @click="loadDomains()">{{ t('common.retry') }}</el-button>
        </el-alert>
        <el-table stripe :data="domRows" v-loading="domLoading" border
                  :default-sort="{ prop: 'cert_days_left', order: 'ascending' }"
                  :empty-text="domError ? t('expiry.dom.load_fail') : domLoading ? t('expiry.table.loading') : t('expiry.dom.empty')">
          <el-table-column prop="cert_days_left" :label="t('expiry.dom.col_cert')" width="130" sortable :sort-method="certDaysSort">
            <template #default="{ row }">
              <template v-if="row.cert_not_after">
                <el-tag :type="CERT_LEVEL_TAGS[row.cert_level]" size="small" effect="dark">{{ daysText(row.cert_days_left) }}</el-tag>
                <div class="sub">{{ fmtDate(row.cert_not_after) }}</div>
                <el-tooltip v-if="row.cert_source_conflict || row.cert_serial_mismatch" :content="conflictText(row)" placement="top">
                  <el-tag size="small" type="warning">{{ t('common.cert.conflict') }}</el-tag>
                </el-tooltip>
              </template>
              <span v-else class="sub">{{ t('expiry.dom.no_cert') }}</span>
            </template>
          </el-table-column>
          <el-table-column :label="t('expiry.dom.col_domain')" min-width="180" show-overflow-tooltip>
            <template #default="{ row }">
              <el-button v-if="canOpenDetail(row.domain)" text type="primary" class="dom-link" @click="openDetail(row.domain)">{{ row.domain }}</el-button>
              <span v-else>{{ row.domain }}</span>
            </template>
          </el-table-column>
          <el-table-column :label="t('expiry.dom.col_serial')" width="110" show-overflow-tooltip>
            <template #default="{ row }">
              <span v-if="row.cert_serial" class="mono sub">{{ row.cert_serial }}</span>
              <span v-else class="sub">—</span>
            </template>
          </el-table-column>
          <el-table-column :label="t('expiry.dom.col_source')" min-width="150">
            <template #default="{ row }">
              <el-tag v-for="s in row.source_labels" :key="s" size="small" class="src-tag">{{ s }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column :label="t('expiry.dom.col_slb')" min-width="150" show-overflow-tooltip>
            <template #default="{ row }">
              <span v-if="row.slb" class="sub">{{ t('expiry.dom.slb_cell', { chains: (row.slb.chains || []).join('/') || t('expiry.dom.slb_chain'), rules: row.slb.edge_count, req: row.slb.req_24h }) }}</span>
              <span v-else class="sub">—</span>
            </template>
          </el-table-column>
          <el-table-column :label="t('expiry.dom.col_cdn')" min-width="120" show-overflow-tooltip>
            <template #default="{ row }">
              <span v-if="row.cdn" class="sub">{{ t('expiry.dom.cdn_cell', { product: row.cdn.product, gb: row.cdn.traffic_30d_gb }) }}</span>
              <span v-else class="sub">—</span>
            </template>
          </el-table-column>
          <el-table-column :label="t('expiry.dom.col_business')" min-width="110" show-overflow-tooltip>
            <template #default="{ row }">
              <span v-if="row.business">{{ row.business }}</span>
              <span v-else class="sub">—</span>
            </template>
          </el-table-column>
          <el-table-column :label="t('expiry.dom.col_vendor')" min-width="130" show-overflow-tooltip>
            <template #default="{ row }">
              <span v-if="row.vendor || row.account_name" class="sub">{{ [row.vendor, row.account_name].filter(Boolean).join(' / ') }}</span>
              <span v-else class="sub">{{ t('expiry.common.unassigned') }}</span>
            </template>
          </el-table-column>
          <el-table-column :label="t('expiry.dom.col_status')" width="80">
            <template #default="{ row }">
              <el-tag :type="row.status === 'disabled' ? 'info' : 'success'" size="small">{{ row.status === 'disabled' ? t('expiry.common.disabled') : t('expiry.common.enabled') }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column v-if="canWrite" :label="t('expiry.dom.col_ops')" width="90">
            <template #default="{ row }">
              <el-button size="small" text type="primary" @click="editRow(row)">{{ t('expiry.act.edit') }}</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>
    </el-tabs>

    <!-- 域名详情：六处证据聚合 -->
    <el-dialog v-model="detailDialog.visible" width="780px" :title="t('expiry.det.title', { domain: detailDialog.domain })">
      <div v-loading="detailDialog.loading">
        <template v-if="detailDialog.data">
          <div class="det-sec">
            <div class="det-title">{{ t('expiry.det.registry') }}
              <el-button v-if="canWrite" size="small" text type="primary" @click="editFromDetail">{{ t('expiry.act.edit') }}</el-button>
            </div>
            <div class="det-grid">
              <span><span class="det-k">{{ t('expiry.det.k.business') }}</span> {{ detailDialog.data.registry.business || '—' }}</span>
              <span><span class="det-k">{{ t('expiry.det.k.vendor_account') }}</span> {{ [detailDialog.data.registry.vendor, detailDialog.data.registry.account_name].filter(Boolean).join(' / ') || t('expiry.common.unassigned') }}</span>
              <span><span class="det-k">{{ t('expiry.det.k.status') }}</span> {{ detailDialog.data.registry.status === 'disabled' ? t('expiry.common.disabled') : t('expiry.common.enabled') }}</span>
              <span><span class="det-k">{{ t('expiry.det.k.manual') }}</span> {{ (detailDialog.data.registry.manual_fields || []).join('、') || t('expiry.common.none') }}</span>
            </div>
            <div class="det-line">{{ t('expiry.det.note_line', { v: detailDialog.data.registry.note || '—' }) }}</div>
            <div class="det-line">
              {{ t('expiry.det.source_prefix') }}<el-tag v-for="s in detailDialog.data.registry.sources" :key="s" size="small" class="src-tag">{{ (detailDialog.data.source_labels || {})[s] || s }}</el-tag>
            </div>
          </div>

          <div class="det-sec">
            <div class="det-title">{{ t('expiry.det.cert') }}</div>
            <!-- 差异可见：两来源到期日不一致时并列展示（不静默取一） -->
            <div v-if="detailDialog.data.cert.source_conflict || detailDialog.data.cert.serial_mismatch"
                 class="det-line conflict-line">
              ⚠ {{ t('common.cert.conflict') }}：{{ conflictText(detailDialog.data.cert) }}
            </div>
            <div class="det-grid">
              <span><span class="det-k">{{ t('expiry.det.k.expire') }}</span> {{ fmtDate(detailDialog.data.cert.not_after) }}
                <el-tag v-if="detailDialog.data.cert.not_after" :type="CERT_LEVEL_TAGS[detailDialog.data.cert.level]" size="small">{{ daysText(detailDialog.data.cert.days_left ?? 0) }}</el-tag>
                <el-tag v-if="detailDialog.data.cert.below_warn" type="warning" size="small">{{ t('common.cert.below_warn', { n: detailDialog.data.cert.warn_days }) }}</el-tag>
              </span>
              <span><span class="det-k">{{ t('expiry.det.k.serial') }}</span> {{ detailDialog.data.cert.serial || '—' }}</span>
              <span><span class="det-k">{{ t('expiry.det.k.forensics') }}</span> {{ certSrcLabel(detailDialog.data.cert.source) }}</span>
            </div>
            <div v-for="(ev, i) in detailDialog.data.cert.evidence" :key="i" class="det-line">
              · [{{ certSrcShort(ev.source) }}] {{ ev.cert_name || '' }} {{ fmtDate(ev.not_after) }} {{ ev.note }}
            </div>
            <div v-if="!(detailDialog.data.cert.evidence || []).length" class="det-line">{{ t('expiry.det.cert_no_evidence') }}</div>
          </div>

          <div class="det-sec">
            <div class="det-title">{{ t('expiry.det.slb', { n: detailDialog.data.slb.edges.length }) }}</div>
            <table class="det-table">
              <tr><th>{{ t('expiry.det.th.chain') }}</th><th>{{ t('expiry.det.th.path') }}</th><th>{{ t('expiry.det.th.rule') }}</th><th>{{ t('expiry.det.th.backend') }}</th><th>{{ t('expiry.det.th.req24') }}</th><th>5xx</th><th>{{ t('expiry.det.th.status') }}</th></tr>
              <tr v-for="(e, i) in detailDialog.data.slb.edges" :key="i">
                <td>{{ e.chain }}</td><td class="mono">{{ e.path_pattern || '/' }}</td>
                <td>{{ e.rule_name || '—' }}</td><td class="mono">{{ e.upstream_addr }}{{ e.ecs_name ? `（${e.ecs_name}）` : '' }}</td>
                <td>{{ e.req_count }}</td><td>{{ e.cnt_5xx }}</td><td>{{ e.status }}</td>
              </tr>
            </table>
            <div v-if="!detailDialog.data.slb.edges.length" class="det-line">{{ t('expiry.det.slb_no_evidence') }}</div>
          </div>

          <div class="det-sec">
            <div class="det-title">{{ t('expiry.det.cdn') }}</div>
            <div v-for="(c, i) in (detailDialog.data.cdn.daily || []).slice(0, 5)" :key="i" class="det-line">
              · {{ fmtDate(c.stat_date) }} {{ c.product }} {{ c.traffic_gb }} GB
            </div>
            <div v-if="!(detailDialog.data.cdn.daily || []).length" class="det-line">{{ t('expiry.det.cdn_no_evidence') }}</div>
          </div>

          <div class="det-sec">
            <div class="det-title">{{ t('expiry.det.synth') }}</div>
            <div v-for="c in detailDialog.data.synth.checks" :key="c.id" class="det-line">
              {{ t('expiry.det.synth_line', { type: c.check_type, target: c.target, detail: (c.results[0] || {}).detail || t('expiry.det.synth_no_result') }) }}
            </div>
            <div v-if="!detailDialog.data.synth.checks.length" class="det-line">{{ t('expiry.det.synth_no_evidence') }}</div>
          </div>

          <div class="det-sec">
            <div class="det-title">{{ t('expiry.det.profile') }}</div>
            <div v-for="(p, i) in detailDialog.data.profile.objects" :key="i" class="det-line">
              {{ t('expiry.det.profile_line', { label: p.label || p.object_key, tier: p.tier, status: p.status, learned: p.learned ? t('expiry.det.learned') : '', note: p.last_drift_note }) }}
            </div>
            <div v-if="!detailDialog.data.profile.objects.length" class="det-line">{{ t('expiry.det.profile_no_evidence') }}</div>
          </div>

          <div class="det-sec">
            <div class="det-title">{{ t('expiry.det.diagnosis') }}</div>
            <div v-for="s in detailDialog.data.diagnosis.sessions" :key="s.id" class="det-line">
              {{ t('expiry.det.diagnosis_line', { date: fmtDate(s.created_at), level: s.level, question: s.question }) }}
            </div>
            <div v-if="!detailDialog.data.diagnosis.sessions.length" class="det-line">{{ t('expiry.det.diagnosis_no_evidence') }}</div>
          </div>

          <div class="det-sec">
            <div class="det-title">{{ t('expiry.det.business') }}</div>
            <div v-for="(b, i) in detailDialog.data.business.identifiers" :key="i" class="det-line">
              {{ t('expiry.det.biz_identifier', { business: b.business, ident: b.ident_value }) }}
            </div>
            <div v-if="detailDialog.data.business.ownership" class="det-line">
              {{ t('expiry.det.ownership', { v: [detailDialog.data.business.ownership.vendor, detailDialog.data.business.ownership.account_name, detailDialog.data.business.ownership.business].filter(Boolean).join(' / ') }) }}
            </div>
            <div class="det-line">
              {{ t('expiry.det.derived', { v: [detailDialog.data.business.derived.vendor, detailDialog.data.business.derived.account_name, detailDialog.data.business.derived.business].filter(Boolean).join(' / ') || t('expiry.det.derived_none') }) }}
            </div>
            <div v-for="(h, i) in detailDialog.data.business.hints" :key="'h' + i" class="det-line">
              {{ t('expiry.det.hint', { ref: h.ref }) }}
            </div>
          </div>

          <div class="det-sec">
            <div class="det-title">{{ t('expiry.det.assets') }}</div>
            <div v-for="a in detailDialog.data.assets.domain" :key="a.instance_id" class="det-line">
              {{ t('expiry.det.asset_domain', { id: a.instance_id, status: a.status, date: fmtDate(a.expire_time) }) }}
            </div>
            <div v-for="a in detailDialog.data.assets.certs" :key="a.instance_id" class="det-line">
              {{ t('expiry.det.asset_cert', { name: a.cert_name, status: a.status, date: fmtDate(a.expire_time) }) }}
              <!-- 资产证书行统一解析：与订单日期不同时标注解析结果 -->
              <span v-if="a.resolved_not_after && fmtDate(a.resolved_not_after) !== fmtDate(a.expire_time)" class="conflict-line">
                → {{ t('expiry.det.asset_cert_resolved', { src: certSrcLabel(a.resolved_source), date: fmtDate(a.resolved_not_after) }) }}
              </span>
            </div>
            <div v-if="!detailDialog.data.assets.domain.length && !detailDialog.data.assets.certs.length" class="det-line">{{ t('expiry.det.assets_no_evidence') }}</div>
          </div>
        </template>
      </div>
    </el-dialog>

    <!-- 编辑登记字段（精确改） -->
    <el-dialog v-model="editDialog.visible" width="480px" :title="t('expiry.edit.title', { domain: editDialog.domain })">
      <el-form label-width="90px">
        <el-form-item :label="t('expiry.edit.k.business')">
          <el-input v-model="editDialog.form.business" maxlength="128" :placeholder="t('expiry.edit.business_ph')" />
        </el-form-item>
        <el-form-item :label="t('expiry.edit.k.vendor')">
          <el-input v-model="editDialog.form.vendor" maxlength="32" :placeholder="t('expiry.edit.vendor_ph')" />
        </el-form-item>
        <el-form-item :label="t('expiry.edit.k.account')">
          <el-input v-model="editDialog.form.account_name" maxlength="64" :placeholder="t('expiry.edit.account_ph')" />
        </el-form-item>
        <el-form-item :label="t('expiry.edit.k.note')">
          <el-input v-model="editDialog.form.note" type="textarea" :rows="2" maxlength="500" :placeholder="t('expiry.edit.note_ph')" />
        </el-form-item>
        <el-form-item :label="t('expiry.edit.k.status')">
          <el-select v-model="editDialog.form.status" style="width: 100%">
            <el-option value="active" :label="t('expiry.common.enabled')" />
            <el-option value="disabled" :label="t('expiry.edit.opt_disabled')" />
          </el-select>
        </el-form-item>
      </el-form>
      <div class="sub" style="margin-bottom: 8px">
        {{ t('expiry.edit.tip') }}
      </div>
      <template #footer>
        <el-button @click="editDialog.visible = false">{{ t('expiry.common.cancel') }}</el-button>
        <el-button type="primary" :loading="editDialog.saving" @click="saveEdit">{{ t('expiry.common.save') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 14px;
}
.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-left: 8px;
}
.mono {
  font-family: monospace;
}
.stale-note { margin: -4px 0 10px; }
.stat-row {
  display: flex;
  gap: 12px;
  margin-bottom: 14px;
}
.stat {
  flex: 1;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  padding: 14px 0;
  text-align: center;
  background: var(--el-bg-color);
}
.stat .num {
  font-size: 26px;
  font-weight: 600;
}
.stat .num.small {
  font-size: 16px;
  line-height: 34px;
}
.stat .label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-top: 2px;
}
.stat.clickable {
  cursor: pointer;
  transition: border-color 0.18s, box-shadow 0.18s;
}
.stat.clickable:hover {
  border-color: var(--el-color-primary-light-5);
  box-shadow: 0 4px 14px rgba(15, 23, 42, 0.06);
}
.stat .ignored-note {
  margin-top: 2px;
  font-size: 11px;
}
.stat.expired .num { color: var(--el-color-danger); }
.stat.critical .num { color: var(--el-color-danger); }
.stat.warning .num { color: var(--el-color-warning); }
.stat.sync .num { color: var(--el-color-success); }
.filters {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}
.days-bad {
  color: var(--el-color-danger);
  font-weight: 600;
}
/* 域名登记 */
.dom-link {
  padding: 0;
}
.src-tag {
  margin-right: 4px;
  margin-bottom: 2px;
}
.det-sec {
  margin-bottom: 14px;
}
.det-title {
  font-weight: 600;
  margin-bottom: 6px;
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.det-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 18px;
  font-size: 12px;
}
.det-k {
  color: var(--el-text-color-secondary);
  margin-right: 4px;
}
.det-line {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  line-height: 1.9;
}
.conflict-line {
  color: var(--el-color-warning);
  font-weight: 600;
}
.det-table {
  width: 100%;
  font-size: 12px;
  border-collapse: collapse;
}
.det-table th,
.det-table td {
  border: 1px solid var(--el-border-color-lighter);
  padding: 4px 8px;
  text-align: left;
}
</style>
