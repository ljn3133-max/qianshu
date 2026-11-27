<script setup>
// 监控与提醒：两层策略的唯一配置页。
// 1) 模块提醒矩阵：这个模块的事能不能进总览/站内信/外推（只停提醒，不停采集）。
// 2) 数据源策略：按云账号设优先级（采集/评估节奏）+ 三个去向 + 确认后的重申天数。
// 最终去向 = 模块开关 AND 账号开关。模块/账号清单都从 GET /policy/overview 取，前端不硬编码。
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import {
  bulkSetPriority, getPolicyOverview, updateAccountPolicy, updateModulePolicy,
} from '@/api/policy'
import { useAuthStore } from '@/stores/auth'
import { t } from '@/i18n'

const auth = useAuthStore()
const isAdmin = computed(() => auth.role === 'admin')

const loading = ref(false)
const savingModules = ref(false)
const modules = ref([])
const accounts = ref([])
const priorities = ref({})
const policyEnabled = ref(true)
const selected = ref([])
const savingRow = ref(0)
const filter = reactive({ vendor: '', keyword: '' })

const DEST_KEYS = ['overview', 'inapp', 'push']
const PRIORITY_TAGS = { high: 'danger', medium: 'warning', low: 'info' }
const DEST_KEYS_REPEAT = ['overview', 'inapp', 'push']

const destLabel = (k) => t(`settings.policy.dest.${k}`)
const priorityLabel = (p) => t(`settings.policy.priority.${p}`)
const moduleName = (m) => {
  const k = `settings.policy.module.${m.module}`
  const v = t(k)
  return v === k ? m.name : v
}
const moduleDesc = (m) => {
  const k = `settings.policy.module_desc.${m.module}`
  const v = t(k)
  return v === k ? m.desc : v
}
const scopeLabel = (s) => t(`settings.policy.scope.${s}`)

const vendors = computed(() => {
  const seen = []
  for (const a of accounts.value) if (!seen.includes(a.vendor)) seen.push(a.vendor)
  return seen
})

const filteredAccounts = computed(() => {
  const kw = filter.keyword.trim().toLowerCase()
  return accounts.value.filter((a) => {
    if (filter.vendor && a.vendor !== filter.vendor) return false
    if (kw && !a.name.toLowerCase().includes(kw)) return false
    return true
  })
})

async function load() {
  loading.value = true
  try {
    const r = await getPolicyOverview()
    modules.value = r.modules || []
    accounts.value = r.accounts || []
    priorities.value = r.priorities || {}
    policyEnabled.value = r.policy_enabled !== false
  } catch { /* 拦截器提示 */ } finally { loading.value = false }
}

/* ---------------- 模块矩阵 ---------------- */
async function saveModules() {
  if (!isAdmin.value) return
  savingModules.value = true
  try {
    const r = await updateModulePolicy(modules.value.map((m) => ({
      module: m.module, enabled: m.enabled,
      overview: m.overview, inapp: m.inapp, push: m.push,
    })))
    if (r.modules) modules.value = r.modules
    ElMessage.success(t('settings.policy.msg.modules_saved'))
  } catch { /* 拦截器提示 */ } finally { savingModules.value = false }
}

// 总开关关闭时只置灰三个去向（模板 :disabled），不重置它们：
// 重新打开时保留用户上次的去向配置，避免"关一下再开"把只进总览的配置悄悄改回全开。

/* ---------------- 账号策略 ---------------- */
function repeatEffectiveLabel(row) {
  const v = row.repeat_effective
  return v == null ? t('settings.policy.repeat.never_effective') : t('settings.policy.repeat.days', { n: v })
}

const CUSTOM = '__custom__'
function repeatValue(row) {
  if (row.repeat_days == null) return 'inherit'
  if (Number(row.repeat_days) <= 0) return 'never'
  return Number(row.repeat_days)
}
function isExtraRepeat(row) {
  const v = repeatValue(row)
  return typeof v === 'number' && ![7, 30, 90].includes(v)
}

async function onRepeatChange(row, v) {
  if (v === CUSTOM) {
    let input
    try {
      const r = await ElMessageBox.prompt(
        t('settings.policy.repeat.custom_prompt'),
        t('settings.policy.repeat.custom_title'),
        { inputPattern: /^\d{1,4}$/, inputErrorMessage: t('settings.policy.repeat.custom_err') },
      )
      input = r.value
    } catch { return }
    row.repeat_days = Math.min(3650, Number(input))
  } else if (v === 'inherit') {
    row.repeat_days = null
  } else if (v === 'never') {
    row.repeat_days = 0
  } else {
    row.repeat_days = Number(v)
  }
  await saveAccount(row)
}

async function saveAccount(row) {
  if (!isAdmin.value) return
  savingRow.value = row.id
  try {
    const r = await updateAccountPolicy(row.id, {
      priority: row.priority,
      notify_overview: row.notify_overview,
      notify_inapp: row.notify_inapp,
      notify_push: row.notify_push,
      repeat_days: row.repeat_days,
    })
    Object.assign(row, r)
    ElMessage.success(t('settings.policy.msg.account_saved', { name: row.name }))
  } catch { /* 拦截器提示 */ } finally { savingRow.value = 0 }
}

async function onBulk(priority) {
  if (!isAdmin.value || selected.value.length === 0) {
    return ElMessage.warning(t('settings.policy.msg.select_first'))
  }
  try {
    await ElMessageBox.confirm(
      t('settings.policy.bulk_confirm', { n: selected.value.length, level: priorityLabel(priority) }),
      t('settings.policy.bulk_title'),
      { type: 'warning' },
    )
  } catch { return }
  try {
    await bulkSetPriority(selected.value.map((a) => a.id), priority)
    ElMessage.success(t('settings.policy.msg.bulk_done', { n: selected.value.length }))
    selected.value = []
    load()
  } catch { /* 拦截器提示 */ }
}

onMounted(load)
</script>

<template>
  <div v-loading="loading" class="policy-page">
    <el-alert
      v-if="!policyEnabled"
      type="warning" :closable="false" show-icon
      :title="t('settings.policy.disabled_banner')"
    />
    <el-alert
      type="info" :closable="false" show-icon class="policy-tip"
      :title="t('settings.policy.intro')" />

    <!-- 1. 模块提醒矩阵 -->
    <div class="block-head">
      <h3>{{ t('settings.policy.modules_title') }}</h3>
      <span class="sub">{{ t('settings.policy.modules_sub') }}</span>
      <div class="grow" />
      <el-button v-if="isAdmin" type="primary" :loading="savingModules" @click="saveModules">
        {{ t('settings.policy.btn.save_modules') }}
      </el-button>
    </div>
    <el-table stripe :data="modules" border size="small">
      <el-table-column :label="t('settings.policy.col.module')" min-width="230">
        <template #default="{ row }">
          <div class="mod-name">
            {{ moduleName(row) }}
            <el-tag size="small" type="info" effect="plain">{{ scopeLabel(row.scope) }}</el-tag>
          </div>
          <div class="sub">{{ moduleDesc(row) }}</div>
        </template>
      </el-table-column>
      <el-table-column :label="t('settings.policy.col.enabled')" width="100" align="center">
        <template #default="{ row }">
          <el-switch v-model="row.enabled" :disabled="!isAdmin || !policyEnabled"
                      />
        </template>
      </el-table-column>
      <el-table-column v-for="d in DEST_KEYS" :key="d" :label="destLabel(d)" width="110" align="center">
        <template #default="{ row }">
          <el-switch
            v-if="row.destinations.includes(d)"
            v-model="row[d]"
            :disabled="!isAdmin || !policyEnabled || !row.enabled"
          />
          <span v-else class="sub">—</span>
        </template>
      </el-table-column>
    </el-table>

    <!-- 2. 数据源策略 -->
    <div class="block-head">
      <h3>{{ t('settings.policy.accounts_title') }}</h3>
      <span class="sub">{{ t('settings.policy.accounts_sub') }}</span>
      <div class="grow" />
      <template v-if="isAdmin">
        <el-button size="small" :disabled="!selected.length" @click="onBulk('high')">{{ t('settings.policy.btn.bulk_high') }}</el-button>
        <el-button size="small" :disabled="!selected.length" @click="onBulk('medium')">{{ t('settings.policy.btn.bulk_medium') }}</el-button>
        <el-button size="small" :disabled="!selected.length" @click="onBulk('low')">{{ t('settings.policy.btn.bulk_low') }}</el-button>
      </template>
    </div>
    <div class="toolbar">
      <el-select v-model="filter.vendor" clearable :placeholder="t('settings.policy.filter.vendor')" style="width: 150px">
        <el-option v-for="v in vendors" :key="v" :label="v" :value="v" />
      </el-select>
      <el-input v-model="filter.keyword" clearable :placeholder="t('settings.policy.filter.keyword')" style="width: 200px" />
      <span class="sub">{{ t('settings.policy.accounts_hint') }}</span>
    </div>
    <el-table
      stripe :data="filteredAccounts" border size="small"
      row-key="id" :empty-text="t('settings.policy.accounts_empty')"
      @selection-change="(rows) => (selected = rows)"
    >
      <el-table-column v-if="isAdmin" type="selection" width="42" />
      <el-table-column :label="t('settings.policy.col.account')" min-width="190">
        <template #default="{ row }">
          <div class="acct-name">
            {{ row.name }}
            <el-tag v-if="!row.enabled" size="small" type="info">{{ t('settings.policy.disabled_account') }}</el-tag>
          </div>
          <div class="sub mono">{{ row.vendor }}</div>
        </template>
      </el-table-column>
      <el-table-column :label="t('settings.policy.col.priority')" width="190">
        <template #default="{ row }">
          <el-radio-group
            v-model="row.priority" size="small" :disabled="!isAdmin || !policyEnabled"
            @change="saveAccount(row)"
          >
            <el-radio-button value="high">{{ t('settings.policy.priority.high_short') }}</el-radio-button>
            <el-radio-button value="medium">{{ t('settings.policy.priority.medium_short') }}</el-radio-button>
            <el-radio-button value="low">{{ t('settings.policy.priority.low_short') }}</el-radio-button>
          </el-radio-group>
        </template>
      </el-table-column>
      <el-table-column v-for="d in DEST_KEYS_REPEAT" :key="'acc-' + d" :label="destLabel(d)" width="90" align="center">
        <template #default="{ row }">
          <el-switch
            v-model="row['notify_' + d]"
            :disabled="!isAdmin || !policyEnabled"
            @change="saveAccount(row)"
          />
        </template>
      </el-table-column>
      <el-table-column :label="t('settings.policy.col.repeat')" width="165">
        <template #default="{ row }">
          <el-select
            :model-value="repeatValue(row)" size="small" :disabled="!isAdmin || !policyEnabled"
            @change="(v) => onRepeatChange(row, v)"
          >
            <el-option value="inherit" :label="t('settings.policy.repeat.inherit')" />
            <el-option :value="7" :label="t('settings.policy.repeat.days', { n: 7 })" />
            <el-option :value="30" :label="t('settings.policy.repeat.days', { n: 30 })" />
            <el-option :value="90" :label="t('settings.policy.repeat.days', { n: 90 })" />
            <el-option v-if="isExtraRepeat(row)" :value="repeatValue(row)" :label="t('settings.policy.repeat.days', { n: repeatValue(row) })" />
            <el-option value="never" :label="t('settings.policy.repeat.never')" />
            <el-option :value="CUSTOM" :label="t('settings.policy.repeat.custom')" />
          </el-select>
          <div class="sub">{{ repeatEffectiveLabel(row) }}</div>
        </template>
      </el-table-column>
      <el-table-column :label="t('settings.policy.col.cadence')" min-width="240">
        <template #default="{ row }">
          <span class="sub" v-loading="savingRow === row.id">{{ row.cadence }}</span>
        </template>
      </el-table-column>
    </el-table>

    <!-- 3. 规则说明 -->
    <el-collapse class="policy-rules">
      <el-collapse-item :title="t('settings.policy.rules_title')" name="rules">
        <el-table :data="Object.entries(priorities).map(([k, v]) => ({ key: k, ...v }))" size="small" border>
          <el-table-column :label="t('settings.policy.col.priority')" width="120">
            <template #default="{ row }">
              <el-tag size="small" :type="PRIORITY_TAGS[row.key]">{{ row.label }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="cadence" :label="t('settings.policy.col.cadence')" />
          <el-table-column :label="t('settings.policy.col.repeat')" width="170">
            <template #default="{ row }">
              {{ row.repeat_days == null ? t('settings.policy.repeat.never_effective') : t('settings.policy.repeat.days', { n: row.repeat_days }) }}
            </template>
          </el-table-column>
        </el-table>
        <ul class="rule-list">
          <li>{{ t('settings.policy.rule.and') }}</li>
          <li>{{ t('settings.policy.rule.switch') }}</li>
          <li>{{ t('settings.policy.rule.repeat') }}</li>
          <li>{{ t('settings.policy.rule.force') }}</li>
          <li>{{ t('settings.policy.rule.collect') }}</li>
        </ul>
      </el-collapse-item>
    </el-collapse>
  </div>
</template>

<style scoped>
.policy-page { display: flex; flex-direction: column; gap: 4px; }
.policy-tip { margin-bottom: 8px; }
.block-head { display: flex; align-items: baseline; gap: 10px; margin: 18px 0 10px; }
.block-head h3 { margin: 0; font-size: 15px; }
.grow { flex: 1; }
.toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.mod-name { font-weight: 600; display: flex; align-items: center; gap: 6px; }
.acct-name { font-weight: 600; }
.policy-rules { margin-top: 20px; }
.rule-list { margin: 12px 0 0; padding-left: 20px; line-height: 1.9; color: var(--el-text-color-secondary); }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
</style>
