<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useRoute, useRouter } from 'vue-router'

import { t } from '@/i18n'
import { levelLabel } from '@/utils/format'
import {
  acceptProfile, knowObject, knowProfile, listObjects, listProfiles, objectChain,
  profileDetail, runObjects, runProfiles,
} from '@/api/profile'
import { ackAnomalies } from '@/api/cost'
import { agentAskUrl } from '@/api/agent'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const canWrite = computed(() => ['admin', 'operator'].includes(auth.role))

const items = ref([])
const loading = ref(true)   // 初值 true：进页首帧不说'还没有画像'
const running = ref(false)

async function load() {
  loading.value = true
  try {
    const r = await listProfiles()
    items.value = r.items || []
  } finally {
    loading.value = false
  }
}

async function runNow() {
  running.value = true
  try {
    const r = await runProfiles()
    ElMessage.success(t('profiles.msg.sample_done', { profiles: r.profiles, drifted: r.drifted }))
    await load()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('profiles.msg.sample_failed'))
  } finally {
    running.value = false
  }
}

async function accept(p) {
  try {
    await ElMessageBox.confirm(
      t('profiles.accept.confirm', { label: p.label }),
      t('profiles.accept.title'), { type: 'info', confirmButtonText: t('profiles.accept.confirm_btn'), cancelButtonText: t('profiles.common.cancel') })
  } catch { return }
  try {
    const r = await acceptProfile(p.profile_key)
    ElMessage.success(t('profiles.msg.accepted', { top1: r.top1 || '-', n: r.closed_events }))
    await load()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('profiles.msg.op_failed'))
  }
}

/* ---------------- 「已知道」+分级静默 ---------------- */
// 时钟每分钟刷新一次，静默倒计时显示才走字
const now = ref(new Date())
let clockTimer = null
onMounted(() => { clockTimer = setInterval(() => { now.value = new Date() }, 60000) })
onUnmounted(() => { if (clockTimer) clearInterval(clockTimer) })

const knowDialog = reactive({
  visible: false, key: '', label: '', tier: 'core', silence3d: false, isObject: false,
})

function openKnow(p, isObject = false) {
  knowDialog.visible = true
  knowDialog.key = isObject ? p.object_key : p.profile_key
  knowDialog.label = p.label || knowDialog.key
  knowDialog.tier = p.tier || 'core'
  knowDialog.silence3d = false
  knowDialog.isObject = isObject
}

async function confirmKnow() {
  try {
    const r = knowDialog.isObject
      ? await knowObject(knowDialog.key, knowDialog.silence3d)
      : await knowProfile(knowDialog.key, knowDialog.silence3d)
    const until = r.silence_until
      ? t('profiles.know.until_silenced', { time: fmtTime(new Date(r.silence_until)) })
      : t('profiles.know.until_no_silence')
    ElMessage.success(t('profiles.know.done', {
      tier: r.tier === 'aux' ? t('profiles.know.tier_aux') : t('profiles.know.tier_core'),
      until,
    }))
    knowDialog.visible = false
    await load()
    await loadObjects()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('profiles.msg.op_failed'))
  }
}

function fmtTime(d) {
  const p = (x) => String(x).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

function silenceText(o) {
  if (o.status !== 'known') return ''
  if (!o.silence_until) return t('profiles.silence.none')
  const ms = new Date(o.silence_until) - now.value
  if (ms <= 0) return t('profiles.silence.expired')
  const mins = Math.floor(ms / 60000)
  const d = Math.floor(mins / 1440)
  const h = Math.floor((mins % 1440) / 60)
  const m = mins % 60
  const rest = d ? t('profiles.silence.rest_dh', { d, h }) : (h ? t('profiles.silence.rest_hm', { h, m }) : t('profiles.silence.rest_m', { m }))
  return t('profiles.silence.active', { time: fmtTime(new Date(o.silence_until)), rest })
}

/* ---------------- 全景对象：对象×多层×联动 ---------------- */
const tab = ref('legacy')
const objItems = ref([])
const objLoading = ref(true)
const objRunning = ref(false)
const objError = ref('')
const objNote = ref('')   // 后端 enabled=false 的说明（回滚开关），不能再当「没有对象」展示
const chain = reactive({ visible: false, loading: false, data: null, days: 7, error: '', key: '' })

async function loadObjects() {
  objLoading.value = true
  objError.value = ''
  try {
    const r = await listObjects()
    objItems.value = r.items || []
    objNote.value = r.enabled === false ? (r.note || t('profiles.obj.disabled')) : ''
  } catch (e) {
    // 失败不能渲染成空表：与「还没有对象」区分开
    objItems.value = []
    objError.value = e?.response?.data?.detail || t('profiles.obj.load_fail')
  } finally {
    objLoading.value = false
  }
}

async function runObjectsNow() {
  objRunning.value = true
  try {
    const r = await runObjects(0)
    ElMessage.success(t('profiles.msg.obj_done', { snapshots: r.snapshots, learned: r.learned, events: r.events }))
    await loadObjects()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('profiles.msg.obj_failed'))
  } finally {
    objRunning.value = false
  }
}

async function openChain(row) {
  chain.visible = true
  chain.loading = true
  chain.data = null
  chain.error = ''
  chain.key = row.object_key
  try {
    chain.data = await objectChain(row.object_key, chain.days)
  } catch (e) {
    chain.error = e?.response?.data?.detail || t('profiles.chain.load_fail')
  } finally {
    chain.loading = false
  }
}

// 因果链事件就地收口（与评估同屏，不跳页找入口）
async function resolveChain(e, status) {
  try {
    await ackAnomalies({
      ids: [e.id], status,
      note: status === 'acknowledged' ? t('profiles.chain.note_ack') : t('profiles.chain.note_ignore'),
    })
    ElMessage.success(t('profiles.msg.resolved'))
    if (chain.data?.object?.object_key || chain.data?.object?.key) {
      chain.data = await objectChain(chain.data.object.key || chain.data.object.object_key, chain.days)
    }
  } catch (err) {
    ElMessage.error(err.response?.data?.detail || t('profiles.msg.op_failed'))
  }
}

function layerCell(layers, name, fmt) {
  const v = layers?.[name]
  return v && Object.keys(v).length ? fmt(v) : '—'
}

/* 详情抽屉 */
const drawer = reactive({ visible: false, loading: false, data: null, error: '', key: '' })

async function openDetail(p) {
  drawer.visible = true
  drawer.loading = true
  drawer.data = null
  drawer.error = ''
  drawer.key = p.profile_key
  try {
    drawer.data = await profileDetail(p.profile_key)
  } catch (e) {
    drawer.error = e?.response?.data?.detail || t('profiles.detail.load_fail')
  } finally {
    drawer.loading = false
  }
}

const STATUS_TAGS = { drift: 'danger', normal: 'success', known: 'warning', observing: 'info' }
const STATUS_KEYS = { drift: 'profiles.status.drift', normal: 'profiles.status.normal', known: 'profiles.status.known', observing: 'profiles.status.observing' }
// 状态文案（对象表/因果链里 drift 显示「联动失衡」，其余按状态取 i18n）
const statusLabel = (s) => (s === 'drift' ? t('profiles.status.linkage_drift') : (STATUS_KEYS[s] ? t(STATUS_KEYS[s]) : t('profiles.status.observing')))

// 概览形态：漂移置顶占 C 位，稳定组折叠
// 已知道独立成组（静默中/恢复提醒倒计时），漂移=还需要看的
const grouped = computed(() => {
  const all = items.value || []
  return {
    drift: all.filter((p) => p.status === 'drift'),
    known: all.filter((p) => p.status === 'known'),
    observing: all.filter((p) => p.status === 'observing'),
    normal: all.filter((p) => !['drift', 'known', 'observing'].includes(p.status)),
  }
})
const normalExpanded = ref(false)

function changeSummary(p) {
  const cur = p.top1 ? p.top1.name : '—'
  if (p.status === 'drift' && p.baseline_top1) return t('profiles.change.both', { from: p.baseline_top1, to: cur })
  return t('profiles.change.cur', { cur })
}

// 问智能体深链：漂移卡带画像上下文落到智能体新会话首条
function askAgent(p) {
  const note = p.drift_note ? t('profiles.ask.note', { note: String(p.drift_note).split('\n')[0] }) : ''
  const q = t('profiles.ask.q', {
    label: p.label, key: p.profile_key, summary: changeSummary(p), note,
  })
  window.open(agentAskUrl(q, {
    page: '/profiles', profile_key: p.profile_key, label: p.label, status: p.status,
  }), '_blank')
}

/* ---------------- 双向关联：与异动页互跳 ---------------- */
// 关联异动/告警/变更条目 -> 对应处理页（异动带 id 深链定位到该条）
function gotoRelated(e) {
  if (e.kind === 'anomaly' && e.id) {
    router.push({ path: '/anomalies', query: { anomaly: e.id } })
  } else if (e.kind === 'alert') {
    router.push({ path: '/alerts', query: { tab: 'records' } })
  } else if (e.kind === 'change') {
    router.push({ path: '/changes' })
  }
}

// 漂移事件 -> 异动页该条（profile_drift 也进异动事件流）
function gotoAnomalyEvent(e) {
  if (e?.id) router.push({ path: '/anomalies', query: { anomaly: e.id } })
}

// 进页面自动查库（画像已在库，不该空白）；异动页点回的深链：
// ?profile= 直开画像详情（profile_drift 点回）；?tab=object&object= 直开全景因果链
// （chain_drift 点回）。加载完成后处理一次。
onMounted(async () => {
  await load()
  await loadObjects()
  const q = route.query
  if (q.tab === 'object' || q.object) tab.value = 'object'
  if (q.profile) {
    const p = items.value.find((x) => x.profile_key === q.profile)
    if (p) openDetail(p)
  }
  if (q.object) {
    const row = objItems.value.find((o) => o.object_key === q.object)
    if (row) {
      openChain(row)
    } else {
      // 对象不在清单里也直开因果链（build_chain 按对象 key 查询）
      chain.visible = true
      chain.loading = true
      chain.data = null
      chain.error = ''
      chain.key = String(q.object)
      try {
        chain.data = await objectChain(String(q.object), chain.days)
      } catch (e) {
        chain.error = e?.response?.data?.detail || t('profiles.chain.load_fail')
      } finally {
        chain.loading = false
      }
    }
  }
})

function membersOf(p) {
  return p?.members || (p?.top1 ? [p.top1] : [])
}
</script>

<template>
  <div class="page">
    <el-tabs v-model="tab">
      <el-tab-pane :label="t('profiles.tab.legacy')" name="legacy">
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <div>
            <span style="font-weight: 600">{{ t('profiles.card.title') }}</span>
            <span class="sub">
              {{ t('profiles.card.sub') }}
            </span>
          </div>
          <el-button type="primary" :loading="running" :disabled="!canWrite" @click="runNow">
            {{ t('profiles.card.run') }}
          </el-button>
        </div>
      </template>

      <!-- 概览：漂移占 C 位（变化才是异常），稳定折叠在下 -->
      <div v-if="grouped.drift.length" class="drift-banner">
        <div class="db-title">{{ t('profiles.banner.title', { n: grouped.drift.length }) }}</div>
        <div class="sub">{{ t('profiles.banner.sub') }}</div>
      </div>

      <template v-if="grouped.drift.length">
        <div class="group-title gt-drift">{{ t('profiles.group.drift', { n: grouped.drift.length }) }}</div>
        <el-row :gutter="14" v-loading="loading">
          <el-col :xs="24" :sm="12" :lg="8" v-for="p in grouped.drift" :key="p.profile_key" style="margin-bottom: 14px">
            <el-card shadow="hover" class="profile-card pc-drift" @click="openDetail(p)">
              <div class="p-head">
                <b>{{ p.label }}</b>
                <el-tag type="danger" size="small" effect="dark">{{ p.status_label }}</el-tag>
              </div>
              <div class="pc-change">{{ changeSummary(p) }}</div>
              <div class="sub" v-if="p.drift_note">{{ (p.drift_note || '').split('\n')[0] }}</div>
              <div class="p-actions">
                <el-button size="small" type="primary" text @click.stop="openDetail(p)">{{ t('profiles.act.detail') }}</el-button>
                <el-button size="small" type="primary" text @click.stop="askAgent(p)">{{ t('profiles.act.ask') }}</el-button>
                <el-button v-if="canWrite && p.learned" size="small" type="success" text @click.stop="accept(p)">
                  {{ t('profiles.act.accept') }}
                </el-button>
                <el-button v-if="canWrite" size="small" type="warning" text @click.stop="openKnow(p)">
                  {{ t('profiles.act.know') }}
                </el-button>
              </div>
            </el-card>
          </el-col>
        </el-row>
      </template>

      <template v-if="grouped.known.length">
        <div class="group-title gt-known">{{ t('profiles.group.known', { n: grouped.known.length }) }}</div>
        <el-row :gutter="14">
          <el-col :xs="24" :sm="12" :lg="8" v-for="p in grouped.known" :key="p.profile_key" style="margin-bottom: 14px">
            <el-card shadow="hover" class="profile-card pc-known" @click="openDetail(p)">
              <div class="p-head">
                <b>{{ p.label }}</b>
                <el-tag type="warning" size="small">{{ p.status_label }}</el-tag>
              </div>
              <div class="pc-change">{{ changeSummary(p) }}</div>
              <div class="silence-line">{{ silenceText(p) }}</div>
              <div class="p-actions">
                <el-button size="small" type="primary" text @click.stop="openDetail(p)">{{ t('profiles.act.detail') }}</el-button>
                <el-button v-if="canWrite && p.learned" size="small" type="success" text @click.stop="accept(p)">
                  {{ t('profiles.act.accept') }}
                </el-button>
                <el-button v-if="canWrite" size="small" type="warning" text @click.stop="openKnow(p)">
                  {{ t('profiles.act.know') }}
                </el-button>
              </div>
            </el-card>
          </el-col>
        </el-row>
      </template>

      <template v-if="grouped.observing.length">
        <div class="group-title">{{ t('profiles.group.observing', { n: grouped.observing.length }) }}</div>
        <el-row :gutter="14">
          <el-col :xs="24" :sm="12" :lg="8" v-for="p in grouped.observing" :key="p.profile_key" style="margin-bottom: 14px">
            <el-card shadow="hover" class="profile-card" @click="openDetail(p)">
              <div class="p-head">
                <b>{{ p.label }}</b>
                <el-tag type="info" size="small">{{ p.status_label }}</el-tag>
              </div>
              <div class="pc-change">{{ changeSummary(p) }}</div>
              <div class="sub">{{ t('profiles.card.samples', { n: p.n_samples }) }}</div>
              <div class="p-actions">
                <el-button size="small" type="primary" text @click.stop="openDetail(p)">{{ t('profiles.act.detail') }}</el-button>
              </div>
            </el-card>
          </el-col>
        </el-row>
      </template>

      <div class="group-title gt-normal" @click="normalExpanded = !normalExpanded">
        {{ t('profiles.group.normal', { n: grouped.normal.length }) }}
        <span class="tip">{{ normalExpanded ? t('profiles.group.collapse') : t('profiles.group.expand') }}</span>
      </div>
      <el-row :gutter="14" v-if="normalExpanded">
        <el-col :xs="24" :sm="12" :lg="8" v-for="p in grouped.normal" :key="p.profile_key" style="margin-bottom: 14px">
          <el-card shadow="hover" class="profile-card" @click="openDetail(p)">
            <div class="p-head">
              <b>{{ p.label }}</b>
              <el-tag type="success" size="small">{{ p.status_label }}</el-tag>
            </div>
            <div class="pc-change">{{ changeSummary(p) }}</div>
            <div class="p-actions">
              <el-button size="small" type="primary" text @click.stop="openDetail(p)">{{ t('profiles.act.detail') }}</el-button>
            </div>
          </el-card>
        </el-col>
      </el-row>
      <el-empty v-if="!loading && !items.length" :description="t('profiles.empty')" />
    </el-card>
      </el-tab-pane>

      <el-tab-pane :label="t('profiles.tab.object')" name="object">
        <el-card shadow="never">
          <template #header>
            <div class="card-header">
              <div>
                <span style="font-weight: 600">{{ t('profiles.obj.title') }}</span>
                <span class="sub">
                  {{ t('profiles.obj.sub') }}
                </span>
              </div>
              <el-button type="primary" :loading="objRunning" :disabled="!canWrite" @click="runObjectsNow">
                {{ t('profiles.obj.run') }}
              </el-button>
            </div>
          </template>
          <el-alert v-if="objError" type="error" :closable="false" show-icon style="margin-bottom: 10px"
                    :title="objError">
            <el-button size="small" @click="loadObjects">{{ t('common.retry') }}</el-button>
          </el-alert>
          <el-alert v-else-if="objNote" type="info" :closable="false" show-icon style="margin-bottom: 10px"
                    :title="objNote" />
          <el-table :data="objItems" v-loading="objLoading" stripe size="small" border
                    style="cursor: pointer" @row-click="openChain">
            <el-table-column prop="label" :label="t('profiles.obj.col_label')" min-width="170" show-overflow-tooltip />
            <el-table-column prop="object_type" :label="t('profiles.obj.col_type')" width="80" />
            <el-table-column :label="t('profiles.obj.col_status')" width="140">
              <template #default="{ row }">
                <el-tag :type="STATUS_TAGS[row.status] || 'info'" size="small" effect="dark">
                  {{ statusLabel(row.status) }}
                </el-tag>
                <div class="silence-line" v-if="row.status === 'known'">{{ silenceText(row) }}</div>
              </template>
            </el-table-column>
            <el-table-column :label="t('profiles.obj.col_baseline')" min-width="160">
              <template #default="{ row }">
                <el-tag v-for="l in row.baseline_layers" :key="l" size="small" style="margin: 1px">{{ l }}</el-tag>
                <span v-if="!row.baseline_layers?.length" class="sub">{{ t('profiles.obj.insufficient') }}</span>
              </template>
            </el-table-column>
            <el-table-column :label="t('profiles.obj.col_coupling')" min-width="140">
              <template #default="{ row }">
                <span class="sub mono">{{ (row.couplings || []).join('、') || '—' }}</span>
              </template>
            </el-table-column>
            <el-table-column :label="t('profiles.obj.col_ops')" width="90">
              <template #default="{ row }">
                <el-button v-if="canWrite && (row.status === 'drift' || row.status === 'known')"
                           size="small" type="warning" text @click.stop="openKnow(row, true)">
                  {{ t('profiles.act.know') }}
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>
    </el-tabs>

    <el-drawer v-model="chain.visible" size="760px"
               :title="chain.data ? t('profiles.chain.title_obj', { label: chain.data.object.label }) : t('profiles.chain.title')">
      <div v-loading="chain.loading">
        <el-alert v-if="chain.error" type="error" :closable="false" show-icon :title="chain.error">
          <el-button size="small" @click="openChain({ object_key: chain.key })">{{ t('common.retry') }}</el-button>
        </el-alert>
        <template v-else-if="chain.data">
          <div class="p-title">
            {{ t('profiles.chain.status') }}
            <el-tag :type="STATUS_TAGS[chain.data.object.status] || 'info'" size="small" effect="dark">
              {{ statusLabel(chain.data.object.status) }}
            </el-tag>
            <span class="silence-line" v-if="chain.data.object.status === 'known'">
              {{ silenceText(chain.data.object) }}
            </span>
            <el-button v-if="canWrite && (chain.data.object.status === 'drift' || chain.data.object.status === 'known')"
                       size="small" type="warning" plain style="margin-left: 10px"
                       @click="openKnow(chain.data.object, true)">
              {{ t('profiles.act.know') }}
            </el-button>
          </div>
          <div class="p-title" style="margin-top: 14px">{{ t('profiles.chain.events_title') }}</div>
          <el-empty v-if="!chain.data.events?.length" :description="t('profiles.chain.events_empty')" :image-size="60" />
          <div v-for="e in chain.data.events" :key="e.id" class="event-item">
            <el-tag :type="e.level === 'critical' ? 'danger' : 'warning'" size="small">{{ levelLabel(e.level) }}</el-tag>
            <span class="mono sub">{{ e.last_seen }}</span>
            <el-tag v-if="e.status === 'resolved'" size="small" type="info">{{ t('profiles.chain.closed') }}</el-tag>
            <template v-else-if="canWrite">
              <!-- 收口与评估同屏：chain_drift 落地页可直接确认/忽略 -->
              <el-button size="small" type="success" plain @click="resolveChain(e, 'acknowledged')">{{ t('profiles.chain.resolved') }}</el-button>
              <el-button size="small" type="info" plain @click="resolveChain(e, 'ignored')">{{ t('profiles.chain.false_positive') }}</el-button>
            </template>
            <div style="white-space: pre-wrap; font-size: 12px; margin-top: 4px">{{ e.chain }}</div>
          </div>

          <div class="p-title" style="margin-top: 14px">{{ t('profiles.chain.timeline_title', { n: chain.days }) }}</div>
          <el-table :data="chain.data.timeline" stripe size="small" border>
            <el-table-column prop="date" :label="t('profiles.chain.col_date')" width="100" />
            <el-table-column :label="t('profiles.chain.col_cdn')">
              <template #default="{ row }">
                {{ layerCell(row.layers, 'cdn', v => `${v.traffic_gb ?? '—'}GB / ${(v.requests ?? 0).toLocaleString()}`) }}
              </template>
            </el-table-column>
            <el-table-column :label="t('profiles.chain.col_slb')">
              <template #default="{ row }">
                {{ layerCell(row.layers, 'slb', v => `${(v.requests ?? 0).toLocaleString()} / ${v.code_5xx ?? 0}`) }}
              </template>
            </el-table-column>
            <el-table-column :label="t('profiles.chain.col_app')" width="110">
              <template #default="{ row }">
                {{ layerCell(row.layers, 'app', v => (v.errors ?? 0).toLocaleString()) }}
              </template>
            </el-table-column>
            <el-table-column :label="t('profiles.chain.col_host')" width="100">
              <template #default="{ row }">
                {{ layerCell(row.layers, 'host', v => (v.mem_peak != null ? v.mem_peak + '%' : '—')) }}
              </template>
            </el-table-column>
            <el-table-column :label="t('profiles.chain.col_change')" width="70">
              <template #default="{ row }">
                {{ layerCell(row.layers, 'change', v => v.count ?? 0) }}
              </template>
            </el-table-column>
            <el-table-column :label="t('profiles.chain.col_cost')" width="130">
              <template #default="{ row }">
                {{ layerCell(row.layers, 'cost', v => (v.yuan != null
                  ? t('profiles.chain.bill', { yuan: v.yuan })
                  : (v.pack_gb != null ? t('profiles.chain.pack', { gb: v.pack_gb }) : '—'))) }}
              </template>
            </el-table-column>
          </el-table>
        </template>
      </div>
    </el-drawer>

    <el-drawer v-model="drawer.visible" size="640px" :title="drawer.data?.label || t('profiles.detail.title')">
      <div v-loading="drawer.loading">
        <el-alert v-if="drawer.error" type="error" :closable="false" show-icon :title="drawer.error">
          <el-button size="small" @click="openDetail({ profile_key: drawer.key })">{{ t('common.retry') }}</el-button>
        </el-alert>
        <template v-else-if="drawer.data">
          <div class="p-title">{{ t('profiles.detail.samples_title') }}</div>
          <el-table stripe :data="drawer.data.samples" size="small" border>
            <el-table-column prop="date" :label="t('profiles.detail.col_date')" width="110" />
            <el-table-column :label="t('profiles.detail.col_top')">
              <template #default="{ row }">
                <span v-for="m in row.members" :key="m.name" class="mono" style="margin-right: 10px">
                  {{ m.name }} {{ m.share_pct }}%
                </span>
              </template>
            </el-table-column>
          </el-table>

          <div class="p-title" style="margin-top: 14px">{{ t('profiles.detail.baseline_title') }}</div>
          <el-table stripe :data="drawer.data.baseline.members || []" size="small" border>
            <el-table-column prop="name" :label="t('profiles.detail.col_member')" min-width="140" show-overflow-tooltip />
            <el-table-column :label="t('profiles.detail.col_share')" width="110">
              <template #default="{ row }">{{ row.share_avg }}%</template>
            </el-table-column>
            <el-table-column :label="t('profiles.detail.col_range')" width="130">
              <template #default="{ row }">{{ row.share_min }}% ~ {{ row.share_max }}%</template>
            </el-table-column>
            <el-table-column :label="t('profiles.detail.col_freq')" width="90">
              <template #default="{ row }">{{ Math.round(row.freq * 100) }}%</template>
            </el-table-column>
          </el-table>

          <!-- 该对象近 7 天的 告警/异动/变更 时间线 -->
          <div class="p-title" style="margin-top: 14px">{{ t('profiles.detail.related_title') }}</div>
          <el-empty v-if="!drawer.data.related_events?.length" :description="t('profiles.detail.related_empty')" :image-size="60" />
          <div v-for="e in drawer.data.related_events" :key="e.kind + e.ts + e.title" class="event-item">
            <el-tag size="small" :type="e.kind === 'change' ? 'warning' : e.kind === 'alert' ? 'danger' : 'info'">
              {{ e.kind === 'alert' ? t('profiles.kind.alert') : e.kind === 'anomaly' ? t('profiles.kind.anomaly') : t('profiles.kind.change') }}
            </el-tag>
            <span class="mono sub">{{ (e.ts || '').replace('T', ' ').slice(0, 16) }}</span>
            <span class="sub" v-if="e.status">{{ e.status }}</span>
            <el-tag v-if="e.linked" type="danger" size="small" effect="plain">{{ t('profiles.detail.linked') }}</el-tag>
            <el-button size="small" text type="primary" class="rel-go" @click="gotoRelated(e)">
              {{ e.kind === 'anomaly' ? t('profiles.go.anomaly') : e.kind === 'alert' ? t('profiles.go.alert') : t('profiles.go.change') }}
            </el-button>
            <div class="rel-line">{{ e.title }}</div>
            <div class="sub mono" v-if="e.service || e.user">
              {{ t('profiles.detail.sys_user', { service: e.service || '—', user: e.user || '—' }) }}
            </div>
            <div class="sub" v-if="e.detail">{{ e.detail }}</div>
          </div>

          <template v-if="drawer.data.events?.length">
            <div class="p-title" style="margin-top: 14px">{{ t('profiles.detail.drift_title') }}</div>
            <div v-for="e in drawer.data.events" :key="e.id" class="event-item">
              <el-tag :type="e.level === 'critical' ? 'danger' : 'warning'" size="small">{{ levelLabel(e.level) }}</el-tag>
              <span class="mono sub">{{ e.detect_date }}</span>
              <el-tag v-if="e.ack_status === 'resolved'" size="small" type="info">{{ t('profiles.chain.closed') }}</el-tag>
              <!-- 漂移事件在异动事件流里也有一条，可点回定位 -->
              <el-button size="small" text type="primary" class="rel-go" @click="gotoAnomalyEvent(e)">{{ t('profiles.go.anomaly') }}</el-button>
              <div style="white-space: pre-wrap; font-size: 12px; margin-top: 4px">{{ e.content }}</div>
            </div>
          </template>
        </template>
      </div>
    </el-drawer>

    <!-- 「知道了」对话框：core 静默 1 天固定；aux 勾选"静默 3 天"才生效（默认持续通知） -->
    <el-dialog v-model="knowDialog.visible" :title="t('profiles.know.title')" width="430px">
      <div class="know-title">{{ t('profiles.know.desc', { label: knowDialog.label }) }}</div>
      <div class="sub" style="margin: 8px 0 4px">
        {{ t('profiles.know.core_tip') }}
      </div>
      <div class="sub" style="margin-bottom: 8px">
        {{ t('profiles.know.aux_tip') }}
      </div>
      <el-checkbox v-if="knowDialog.tier === 'aux'" v-model="knowDialog.silence3d">
        {{ t('profiles.know.silence3d') }}
      </el-checkbox>
      <template #footer>
        <el-button @click="knowDialog.visible = false">{{ t('profiles.common.cancel') }}</el-button>
        <el-button type="primary" @click="confirmKnow">{{ t('profiles.act.know') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.drift-banner {
  background: var(--el-color-danger-light-9); border: 1px solid var(--el-color-danger-light-5);
  border-left: 4px solid var(--el-color-danger); border-radius: 10px;
  padding: 12px 16px; margin-bottom: 14px;
}
.db-title { font-size: 15px; font-weight: 700; color: var(--el-color-danger); }
.group-title { font-size: 13.5px; font-weight: 600; margin: 10px 0 10px; display: flex; gap: 10px; align-items: baseline; }
.gt-drift { color: var(--el-color-danger); }
.gt-known { color: var(--el-color-warning); }
.gt-normal { cursor: pointer; user-select: none; }
.pc-drift { border-color: var(--el-color-danger-light-5); }
.pc-known { border-color: var(--el-color-warning-light-5); }
.pc-change { font-size: 13px; margin: 6px 0; }
.pc-change b { color: var(--el-color-primary); }
.silence-line { font-size: 12px; color: var(--el-color-warning); margin: 4px 0; }
.rel-line { font-size: 13px; margin: 4px 0 2px; font-weight: 600; }
.rel-go { float: right; }
.know-title { font-size: 14px; font-weight: 600; }
.tip { font-size: 12px; color: var(--el-text-color-secondary); font-weight: 400; }
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
.mono {
  font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 12px;
}
.profile-card {
  height: 100%;
}
.p-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.p-title {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin: 6px 0 4px;
}
.p-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
}
.p-name {
  width: 130px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 12px;
}
.p-pct {
  width: 52px;
  text-align: right;
  font-size: 12px;
}
.p-actions {
  margin-top: 10px;
  display: flex;
  gap: 8px;
}
.event-item {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 8px 10px;
  margin-bottom: 8px;
}
</style>
