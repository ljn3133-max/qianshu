<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'

import { listChanges, syncChanges } from '@/api/changes'
import { listCloudAccounts } from '@/api/clouds'
import CopyBtn from '@/components/CopyBtn.vue'
import { useAuthStore } from '@/stores/auth'
import { t } from '@/i18n'

const auth = useAuthStore()
const canWrite = computed(() => ['admin', 'operator'].includes(auth.role))

const rows = ref([])
const total = ref(0)
const loading = ref(false)
const syncing = ref(false)
const loadError = ref('')
const accounts = ref([])
const sources = ref([])
const syncIssues = ref([])
const query = reactive({ hours: 48, keyword: '', account_id: 0, source: '', page: 1, page_size: 50 })

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const r = await listChanges({ ...query })
    rows.value = r.items || []
    total.value = r.total || 0
    sources.value = r.sources || []
  } catch (e) {
    rows.value = []
    total.value = 0
    loadError.value = e?.response?.data?.detail || t('changes.table.load_fail')
  } finally {
    loading.value = false
  }
}

async function sync() {
  syncing.value = true
  syncIssues.value = []
  try {
    const r = await syncChanges(query.hours > 24 ? Math.ceil(query.hours / 24) : 1, query.account_id)
    syncIssues.value = (r.results || []).filter((x) => x.error || x.truncated)
    if (r.errors?.length) {
      ElMessage.error(t('changes.sync.partial_fail', { count: r.errors.length }))
    } else if (r.truncated) {
      ElMessage.warning(t('changes.sync.truncated'))
    } else {
      ElMessage.success(t('changes.sync.success', { fetched: r.fetched, created: r.created }))
    }
    await load()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || t('changes.sync.fail'))
  } finally {
    syncing.value = false
  }
}

function issueText(row) {
  if (row.error) return `${row.account || row.account_id} / ${row.source_label || row.source || '—'}: ${row.error}`
  return `${row.account || row.account_id} / ${row.source_label || row.source}: ${t('changes.sync.result_truncated')}`
}

async function loadMetadata() {
  try {
    const a = await listCloudAccounts()
    accounts.value = (a.items || []).filter((x) => x.enabled)
  } catch { /* 列表与同步仍可用；账号筛选元数据不可用时保留全部口径 */ }
}

onMounted(() => { loadMetadata(); load() })
</script>

<template>
  <div class="page">
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <div>
            <span style="font-weight: 600">{{ t('changes.title') }}</span>
            <span class="sub">{{ t('changes.subtitle') }}</span>
          </div>
          <el-button type="primary" :loading="syncing" :disabled="!canWrite" @click="sync">{{ t('changes.sync.btn') }}</el-button>
        </div>
      </template>

      <div class="toolbar">
        <el-radio-group v-model="query.hours" size="small" @change="() => { query.page = 1; load() }">
          <el-radio-button :value="24">{{ t('changes.range.24h') }}</el-radio-button>
          <el-radio-button :value="48">{{ t('changes.range.48h') }}</el-radio-button>
          <el-radio-button :value="168">{{ t('changes.range.7d') }}</el-radio-button>
        </el-radio-group>
        <el-select v-model="query.account_id" size="small" style="width: 170px" @change="() => { query.page = 1; load() }">
          <el-option :value="0" :label="t('changes.toolbar.all_accounts')" />
          <el-option v-for="a in accounts" :key="a.id" :value="a.id" :label="a.name" />
        </el-select>
        <el-select v-model="query.source" size="small" clearable :placeholder="t('changes.toolbar.all_sources')" style="width: 190px"
                   @change="() => { query.page = 1; load() }">
          <el-option v-for="s in sources" :key="s.key" :value="s.key" :label="s.label" />
        </el-select>
        <el-input v-model="query.keyword" :placeholder="t('changes.toolbar.filter_ph')" clearable style="width: 220px"
                  @keyup.enter="() => { query.page = 1; load() }" @clear="() => { query.page = 1; load() }" />
        <el-button size="small" @click="() => { query.page = 1; load() }">{{ t('changes.toolbar.search') }}</el-button>
      </div>

      <el-alert v-if="loadError" type="error" :closable="false" show-icon class="load-error" :title="loadError">
        <el-button size="small" @click="load">{{ t('common.retry') }}</el-button>
      </el-alert>
      <el-alert v-if="syncIssues.length" type="warning" :closable="false" show-icon class="sync-issues"
                :title="t('changes.sync.incomplete_title')">
        <div v-for="(issue, i) in syncIssues" :key="i">{{ issueText(issue) }}</div>
      </el-alert>
      <el-table stripe v-loading="loading" :data="rows" size="small" border
                :empty-text="loading ? t('changes.table.loading') : t('changes.table.empty')">
        <el-table-column prop="event_time" :label="t('changes.table.time')" width="160">
          <template #default="{ row }">{{ (row.event_time || '').replace('T', ' ').slice(0, 16) }}</template>
        </el-table-column>
        <el-table-column prop="service" :label="t('changes.table.service')" width="100" />
        <el-table-column prop="event_name" :label="t('changes.table.action')" width="200" show-overflow-tooltip />
        <el-table-column prop="user" :label="t('changes.table.operator')" width="130" show-overflow-tooltip />
        <el-table-column :label="t('changes.table.account')" width="120" show-overflow-tooltip>
          <template #default="{ row }">
            <el-tag v-if="row.account" type="info" size="small" effect="plain">{{ row.account }}</el-tag>
            <span v-else class="sub">—</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('changes.table.source')" width="180" show-overflow-tooltip>
          <template #default="{ row }">{{ row.source_label || row.source || '—' }}</template>
        </el-table-column>
        <el-table-column prop="resource" :label="t('changes.table.resource')" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">
            {{ row.resource }}<CopyBtn v-if="row.resource" :text="row.resource" />
          </template>
        </el-table-column>
        <el-table-column :label="t('changes.table.result')" width="80">
          <template #default="{ row }">
            <el-tag :type="row.error_code ? 'danger' : 'success'" size="small">
              {{ row.error_code || t('changes.table.success') }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="summary" :label="t('changes.table.summary')" min-width="260" show-overflow-tooltip />
      </el-table>
      <div class="pager">
        <el-pagination background layout="total, prev, pager, next" :total="total"
          :page-size="query.page_size" :current-page="query.page"
          @current-change="(p) => { query.page = p; load() }" />
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.card-header { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.toolbar { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }
.sub { color: var(--el-text-color-secondary); font-size: 12px; margin-left: 8px; }
.sync-issues { margin-bottom: 12px; }
.pager { display: flex; justify-content: flex-end; margin-top: 10px; }
</style>
