<script setup>
// 机器管理：目标机资产台账——
// SSH 凭证加密托管、角色标签、所属集群、水位（CPU/内存/磁盘）、连通检测；
// 部署集群/添加节点/备份等管理任务从机器库选机，不再表单现填 SSH。
import { onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useRoute, useRouter } from 'vue-router'
import http from '@/api/http'
import { t } from '@/i18n'
import { fmtTime } from '@/utils/format'
import { useAuthStore } from '@/stores/auth'
import TermDialog from '@/components/TermDialog.vue'
import { ArrowDown, CircleCheckFilled, Connection, Cpu, WarningFilled } from '@element-plus/icons-vue'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
// 主机名册增删改后端要求 admin（/machines POST/PUT/DELETE），此前用 k8s.write
// 不能用含 operator 的 k8s.write 判定，否则 operator 看得到按钮、点了才 403。
const canAdmin = auth.role === 'admin'
const canWrite = canAdmin
const canTerm = auth.hasCode ? auth.hasCode('machines.ssh') : ['admin', 'operator'].includes(auth.role)

// 主机名册互链：/machines?keyword=<ip 或主机名> 预填搜索——
// K8s 资源清单 Node 行、运维管理详情抽屉按 IP 跳过来直接定位同机（ipnorm 已做 IP 互通）
// 关联资产 → 运维管理对应产品页签（Ops 支持 ?product= 深链）
function goAsset(a) {
  if (a.product === 'host') {
    router.push({ path: '/machines', query: { keyword: a.name || '' } })
    return
  }
  router.push({ path: '/ops', query: { product: a.product || 'ecs' } })
}

// ---- SSH 终端 ----
const termVisible = ref(false)
const termTitle = ref('')
const termUrl = ref('')

function openSsh(row) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  const token = localStorage.getItem('qianshu.token') || ''
  termTitle.value = `SSH · ${row.ssh_user}@${row.ip}`
  termUrl.value = `${proto}://${location.host}/ws/machines/${row.id}/ssh?token=${token}&cols=120&rows=32`
  termVisible.value = true
}

// ---- 机器详情（信息全貌+操作收进，防外围误触）----
const machineDetail = ref(null)
const machineDetailVisible = ref(false)

function openMachineDetail(row) {
  machineDetail.value = row
  machineDetailVisible.value = true
}

function machineOp(cmd, row) {
  if (cmd === 'edit') return openEdit(row)
  if (cmd === 'del') return remove(row)
  if (cmd === 'test') return testMachine(row)
}

const machines = ref([])
const summary = ref({ total: 0, ok: 0, error: 0, assigned: 0 })
// 预填搜索：/machines?keyword=<ip>
const keyword = ref(String(route.query.keyword || ''))
const loading = ref(false)
const dialogVisible = ref(false)
const editing = ref(null)
const testingId = ref(0)
const form = reactive({ host_name: '', ip: '', ssh_port: 22, ssh_user: 'root', ssh_password: '', role: '未分配' })
const ROLES = ['未分配', 'master', 'etcd', 'node', 'loadbalancer']

// 角色/状态显示名（'未分配' 是前后端约定的哨兵值不动，只翻显示；master 等本就是英文）
function roleLabel(r) {
  return r === '未分配' ? t('machines.role.unassigned') : r
}
function statusLabel(s) {
  return { ok: t('machines.status.ok'), error: t('machines.status.error'), unknown: t('machines.status.unknown') }[s] || s
}

async function load() {
  loading.value = true
  try {
    const r = await http.get('/machines', { params: { keyword: keyword.value } })
    machines.value = r.items || []
    summary.value = r.summary || summary.value
  } catch { /* 拦截器提示 */ } finally { loading.value = false }
}

watch(() => route.query.keyword, (value) => {
  keyword.value = String(value || '')
  load()
})

function openAdd() {
  editing.value = null
  Object.assign(form, { host_name: '', ip: '', ssh_port: 22, ssh_user: 'root', ssh_password: '', role: '未分配' })
  dialogVisible.value = true
}

function openEdit(row) {
  editing.value = row
  Object.assign(form, {
    host_name: row.host_name, ip: row.ip, ssh_port: row.ssh_port,
    ssh_user: row.ssh_user, ssh_password: '', role: row.role,
  })
  dialogVisible.value = true
}

async function save() {
  if (!form.ip) return ElMessage.warning(t('machines.msg.needIp'))
  try {
    if (editing.value) {
      await http.put(`/machines/${editing.value.id}`, { ...form })
    } else {
      await http.post('/machines', { ...form })
    }
    ElMessage.success(t('machines.msg.saved'))
    dialogVisible.value = false
    load()
  } catch { /* 拦截器提示 */ }
}

async function testMachine(row) {
  testingId.value = row.id
  try {
    const r = await http.post(`/machines/${row.id}/test`)
    ElMessage({ type: r.ok ? 'success' : 'error', message: `${row.ip}: ${r.detail}` })
    load()
  } finally { testingId.value = 0 }
}

async function remove(row) {
  try {
    await ElMessageBox.confirm(t('machines.confirm.delBody', { ip: row.ip }), t('machines.confirm.delTitle'), { type: 'warning' })
    await http.delete(`/machines/${row.id}`)
    ElMessage.success(t('machines.msg.deleted'))
    load()
  } catch { /* 取消或错误 */ }
}

function usageText(v) {
  return v == null ? '—' : v + '%'
}
function usageType(v) {
  return v == null ? '' : (v >= 90 ? 'danger' : v >= 70 ? 'warning' : 'success')
}

onMounted(load)
</script>

<template>
  <div class="page">
    <div class="stats-row">
      <div class="stat-card"><div class="ic ic-blue"><el-icon><Cpu /></el-icon></div>
        <div><div class="num">{{ summary.total }}</div><div class="label">{{ t('machines.stats.total') }}</div></div></div>
      <div class="stat-card"><div class="ic ic-green"><el-icon><CircleCheckFilled /></el-icon></div>
        <div><div class="num ok">{{ summary.ok }}</div><div class="label">{{ t('machines.status.ok') }}</div></div></div>
      <div class="stat-card"><div class="ic ic-orange"><el-icon><WarningFilled /></el-icon></div>
        <div><div class="num warn">{{ summary.error }}</div><div class="label">{{ t('machines.stats.error') }}</div></div></div>
      <div class="stat-card"><div class="ic ic-purple"><el-icon><Connection /></el-icon></div>
        <div><div class="num">{{ summary.assigned }}</div><div class="label">{{ t('machines.stats.assigned') }}</div></div></div>
    </div>

    <div class="toolbar">
      <el-button v-if="canWrite" type="primary" @click="openAdd">{{ t('machines.toolbar.add') }}</el-button>
      <el-input v-model="keyword" :placeholder="t('machines.toolbar.search')" clearable style="width: 240px" @change="load" />
      <el-button @click="load">{{ t('machines.toolbar.refresh') }}</el-button>
      <span class="tip">{{ t('machines.toolbar.tip') }}</span>
    </div>

    <el-table :data="machines" v-loading="loading" border size="default" v-table-resize :empty-text="t('machines.table.empty')">
      <el-table-column prop="host_name" :label="t('machines.table.hostName')" min-width="130" />
      <el-table-column prop="ip" label="IP" min-width="130">
        <template #default="{ row }"><span class="mono">{{ row.ip }}:{{ row.ssh_port }}</span></template>
      </el-table-column>
      <el-table-column prop="ssh_user" :label="t('machines.table.sshUser')" width="100" />
      <el-table-column :label="t('machines.table.credential')" width="90">
        <template #default="{ row }">
          <el-tag size="small" :type="row.has_password ? 'primary' : 'info'">{{ row.has_password ? t('machines.cred.password') : t('machines.cred.key') }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="role" :label="t('machines.table.role')" width="110">
        <template #default="{ row }">
          <el-tag size="small" :type="row.role === '未分配' ? 'info' : 'success'">{{ roleLabel(row.role) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('machines.table.cluster')" width="130">
        <template #default="{ row }">
          <!-- 互链：集群名可点进 K8s 集群管理 -->
          <el-link v-if="row.cluster_name" type="primary" :underline="false" @click="router.push('/k8s?tab=clusters')">
            {{ row.cluster_name }}
          </el-link>
          <span v-else>—</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('machines.table.links')" min-width="180">
        <template #default="{ row }">
          <template v-if="row.asset_count || row.link_count">
            <div class="mono">{{ t('machines.links.assets', { n: row.asset_count || 0 }) }} · {{ t('machines.links.edges', { n: row.link_count || 0 }) }}</div>
            <!-- 互链：关联资产可点进运维管理对应产品页签 -->
            <div v-if="row.asset_count" class="sub">
              <template v-for="(a, i) in (row.assets || []).slice(0, 2)" :key="a.instance_id || i">
                <el-link type="primary" :underline="false" @click="goAsset(a)">{{ a.name }}</el-link><span v-if="i < Math.min(row.asset_count, 2) - 1"> / </span>
              </template>
              <template v-if="row.asset_count > 2"> …</template>
            </div>
          </template>
          <span v-else class="sub">—</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('machines.table.status')" width="90">
        <template #default="{ row }">
          <el-tag size="small" :type="row.status === 'ok' ? 'success' : (row.status === 'error' ? 'danger' : 'info')">
            {{ statusLabel(row.status) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('machines.table.usage')" width="190">
        <template #default="{ row }">
          <el-tag size="small" :type="usageType(row.cpu_pct)" effect="plain" class="usage">{{ usageText(row.cpu_pct) }}</el-tag>
          <el-tag size="small" :type="usageType(row.mem_pct)" effect="plain" class="usage">{{ usageText(row.mem_pct) }}</el-tag>
          <el-tag size="small" :type="usageType(row.disk_pct)" effect="plain" class="usage">{{ usageText(row.disk_pct) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="last_seen" :label="t('machines.table.lastSeen')" width="160">
        <template #default="{ row }">{{ row.last_seen ? fmtTime(row.last_seen) : '—' }}</template>
      </el-table-column>
      <el-table-column :label="t('machines.table.actions')" width="180">
        <template #default="{ row }">
          <el-button size="small" text type="primary" @click="openMachineDetail(row)">{{ t('machines.actions.detail') }}</el-button>
          <el-button v-if="canTerm" size="small" text type="success" @click="openSsh(row)">SSH</el-button>
          <el-dropdown trigger="click" @command="(cmd) => machineOp(cmd, row)">
            <el-button size="small" text type="primary">{{ t('machines.actions.more') }}<el-icon class="more-ic"><ArrowDown /></el-icon></el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="test" :disabled="testingId === row.id">{{ t('machines.actions.testFull') }}</el-dropdown-item>
                <el-dropdown-item v-if="canWrite" command="edit">{{ t('machines.actions.edit') }}</el-dropdown-item>
                <el-dropdown-item v-if="canWrite" command="del" class="danger-item">{{ t('machines.actions.delete') }}</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </template>
      </el-table-column>
    </el-table>

    <TermDialog v-model="termVisible" :title="termTitle" :ws-url="termUrl" />

    <el-drawer v-model="machineDetailVisible" size="420px" :title="t('machines.drawer.title', { name: machineDetail?.host_name || machineDetail?.ip || '' })">
      <div v-if="machineDetail" class="md-wrap">
        <el-descriptions :column="1" border size="default">
          <el-descriptions-item :label="t('machines.table.hostName')">{{ machineDetail.host_name }}</el-descriptions-item>
          <el-descriptions-item :label="t('machines.detail.address')"><span class="mono">{{ machineDetail.ip }}:{{ machineDetail.ssh_port }}</span></el-descriptions-item>
          <el-descriptions-item :label="t('machines.table.sshUser')">{{ machineDetail.ssh_user }}</el-descriptions-item>
          <el-descriptions-item :label="t('machines.table.credential')">{{ machineDetail.has_password ? t('machines.cred.passwordAes') : t('machines.cred.key') }}</el-descriptions-item>
          <el-descriptions-item :label="t('machines.detail.roleCluster')">{{ roleLabel(machineDetail.role) }} · {{ machineDetail.cluster_name || t('machines.role.unassigned') }}</el-descriptions-item>
          <el-descriptions-item :label="t('machines.table.links')">
            {{ t('machines.links.assets', { n: machineDetail.asset_count || 0 }) }} · {{ t('machines.links.edges', { n: machineDetail.link_count || 0 }) }}
            <div v-if="machineDetail.asset_count" class="sub">{{ (machineDetail.assets || []).map((a) => `${a.name}（${a.product}:${a.instance_id}）`).join('、') }}</div>
          </el-descriptions-item>
          <el-descriptions-item :label="t('machines.table.status')">
            <el-tag size="small" :type="machineDetail.status === 'ok' ? 'success' : (machineDetail.status === 'error' ? 'danger' : 'info')">
              {{ statusLabel(machineDetail.status) }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item :label="t('machines.table.usage')">
            {{ usageText(machineDetail.cpu_pct) }} / {{ usageText(machineDetail.mem_pct) }} / {{ usageText(machineDetail.disk_pct) }}
            <!-- 水位来源标注：本表水位只在 SSH 检测时刷新；Agent 上报的主机指标在监控仪表盘 -->
            <div class="sub">{{ t('machines.usage.source_tip') }}</div>
          </el-descriptions-item>
          <el-descriptions-item :label="t('machines.table.lastSeen')">{{ machineDetail.last_seen ? fmtTime(machineDetail.last_seen) : '—' }}</el-descriptions-item>
        </el-descriptions>
        <div class="md-ops">
          <el-button type="primary" size="small" :loading="testingId === machineDetail.id" @click="machineOp('test', machineDetail)">{{ t('machines.actions.test') }}</el-button>
          <el-button v-if="canTerm" type="success" size="small" plain @click="openSsh(machineDetail)">{{ t('machines.actions.sshTerm') }}</el-button>
          <el-button v-if="canWrite" size="small" @click="machineOp('edit', machineDetail)">{{ t('machines.actions.edit') }}</el-button>
          <el-button v-if="canWrite" type="danger" size="small" plain @click="machineOp('del', machineDetail)">{{ t('machines.actions.delete') }}</el-button>
        </div>
      </div>
    </el-drawer>

    <el-dialog v-model="dialogVisible" :title="editing ? t('machines.dialog.editTitle') : t('machines.toolbar.add')" width="520px">
      <el-form :model="form" label-width="100px">
        <el-form-item :label="t('machines.table.hostName')"><el-input v-model="form.host_name" :placeholder="t('machines.form.hostPlaceholder')" /></el-form-item>
        <el-form-item label="IP"><el-input v-model="form.ip" :placeholder="t('machines.form.ipPlaceholder')" /></el-form-item>
        <el-form-item :label="t('machines.form.sshPort')"><el-input-number v-model="form.ssh_port" :min="1" :max="65535" /></el-form-item>
        <el-form-item :label="t('machines.table.sshUser')"><el-input v-model="form.ssh_user" /></el-form-item>
        <el-form-item :label="t('machines.form.sshPassword')">
          <el-input v-model="form.ssh_password" type="password" show-password
                    :placeholder="editing ? t('machines.form.pwdKeep', { cred: editing.has_password ? t('machines.cred.password') : t('machines.cred.key') }) : t('machines.form.pwdNew')" />
        </el-form-item>
        <el-form-item :label="t('machines.table.role')">
          <el-select v-model="form.role" style="width: 180px">
            <el-option v-for="r in ROLES" :key="r" :label="roleLabel(r)" :value="r" />
          </el-select>
        </el-form-item>
        <div class="tip">{{ t('machines.form.pwdTip') }}</div>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">{{ t('machines.dialog.cancel') }}</el-button>
        <el-button type="primary" @click="save">{{ t('machines.dialog.save') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.page { display: flex; flex-direction: column; gap: 16px; }
.stats-row { display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); gap: 14px; }
.stat-card {
  display: flex; align-items: center; gap: 14px;
  border: 1px solid var(--el-border-color-lighter); border-radius: 12px;
  padding: 18px 20px; background: var(--el-fill-color-blank);
}
.stat-card:hover { box-shadow: 0 6px 18px rgba(59, 130, 246, 0.12); }
.stat-card .num { font-size: 28px; font-weight: 700; line-height: 1.15; white-space: nowrap; }
.stat-card .ic {
  width: 46px; height: 46px; border-radius: 12px; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center; font-size: 22px; color: #fff;
}
.ic-blue { background: linear-gradient(135deg, #60a5fa, #3b82f6); }
.ic-green { background: linear-gradient(135deg, #6ee7b7, #10b981); }
.ic-purple { background: linear-gradient(135deg, #c4b5fd, #8b5cf6); }
.ic-orange { background: linear-gradient(135deg, #fdba74, #f97316); }
.stat-card .num.ok { color: var(--el-color-success); }
.stat-card .num.warn { color: var(--el-color-danger); }
.stat-card .label { font-size: 13px; color: var(--el-text-color-secondary); margin-top: 2px; white-space: nowrap; }
.toolbar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.tip { font-size: 12.5px; color: var(--el-text-color-secondary); }
.mono { font-family: Consolas, monospace; font-size: 12.5px; }
.usage { margin-right: 4px; }
.more-ic { margin-left: 2px; font-size: 12px; }
.danger-item { color: var(--el-color-danger) !important; }
.md-wrap { display: flex; flex-direction: column; gap: 16px; }
.md-ops { display: flex; gap: 10px; }
</style>
