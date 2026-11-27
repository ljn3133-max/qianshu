<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'

import http from '@/api/http'
import { agentAskUrl } from '@/api/agent'
import { useAuthStore } from '@/stores/auth'
import K8sCharts from '@/components/K8sCharts.vue'
import TermDialog from '@/components/TermDialog.vue'
import { t } from '@/i18n'
import { fmtTime } from '@/utils/format'
import {
  ArrowDown, Box, CircleCheckFilled, Connection, Cpu, Files, Grid,
} from '@element-plus/icons-vue'

// ---- Pod exec 终端 ----
const termVisible = ref(false)
const termTitle = ref('')
const termUrl = ref('')

function openPodTerm(row) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  const token = localStorage.getItem('qianshu.token') || ''
  // 带上行所属集群名，避免多集群下回落到第一个集群控制面
  const cname = clusterNameOf(row.cluster_id)
  termTitle.value = t('k8s.term.podTitle', { ns: row.namespace, name: row.name })
  termUrl.value = `${proto}://${location.host}/ws/k8s/pods/${row.namespace}/${row.name}/exec` +
                  `?token=${token}&cluster_name=${encodeURIComponent(cname)}&cols=120&rows=32`
  termVisible.value = true
}

// K8s 管理（容器平台）：总览/集群/任务/资源/统计/事件/日志/失败 八域一页多 tab，
// 侧栏多菜单直达（?tab= 锚点，参考 k8smanager 资源域可发现性）
const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
// 权限码判断：码未下发时回退角色
const canWrite = auth.hasCode ? auth.hasCode('k8s.write') : ['admin', 'operator'].includes(auth.role)
const canDanger = auth.hasCode ? auth.hasCode('k8s.dangerous') : auth.role === 'admin'
const canTerm = auth.hasCode ? auth.hasCode('machines.ssh') : ['admin', 'operator'].includes(auth.role)
const TABS = ['overview', 'clusters', 'tasks', 'resources', 'stats', 'events', 'logs', 'failures', 'audits']
const tab = ref(TABS.includes(route.query.tab) ? route.query.tab : 'resources')
watch(tab, (v) => router.replace({ query: { ...route.query, tab: v } }))
// 侧栏菜单点 ?tab= 时反向同步（此前只有单向——侧栏点了页面不动的根因）
watch(() => route.query.tab, (v) => { if (TABS.includes(v) && v !== tab.value) tab.value = v })
function loadSlowPanels() {
  // 跟随顶部集群下拉：此前不带 cluster_id，切到单集群后镜像/Helm 仍是全集群大杂烩
  const cid = clusterFilter.value || 0
  http.get('/k8mgmt/images', { params: { cluster_id: cid } })
    .then((im) => { mgmtImages.value = im.items || [] }).catch(() => {})
  http.get('/k8mgmt/helm', { params: { cluster_id: cid } }).then((he) => {
    mgmtHelm.value = he.items || []
    mgmtHelmNote.value = he.note || ''
  }).catch(() => {})
}

// 切到管理数据区时补拉（mount 后切回来不再空壳）
watch(tab, (v) => {
  if (['overview', 'stats', 'events', 'audits'].includes(v) && !mgmtOverview.value) loadMgmt()
  if (v === 'stats' && !mgmtImages.value.length) loadSlowPanels()
})
const overview = ref(null)
const nsOptions = ref([])
const clusters = ref([])
const clusterFilter = ref(0)   // 0=全部集群
const clusterDialog = ref(false)
const clusterForm = reactive({ name: '', api_url: '', token: '', verify_tls: false, ns_scope: '', ssh_host: '', ssh_user: 'root', read_only: true })
const testingCid = ref(0)
// ---- 集群管理面配置：ssh_host/读写 ----
// ---- 集群详情（点集群进详情：概要/节点/节点操作/集群操作——操作收进详情防误触）----
const clusterDetail = ref(null)      // 当前查看的集群对象
const clusterDetailVisible = ref(false)
const cdNodes = ref([])
const cdVersion = ref('')
const cdCerts = ref([])
const cdLoading = ref(false)
const cdSyncing = ref(false)

async function cdSync() {
  cdSyncing.value = true
  try {
    await http.post('/k8s/scan', null, { params: { cluster_id: clusterDetail.value?.id || 0 } })
    await openClusterDetail(clusterDetail.value)   // 同步后立刻刷新详情
  } catch { /* 拦截器 */ } finally { cdSyncing.value = false }
}

async function openClusterDetail(row) {
  clusterDetail.value = row
  clusterDetailVisible.value = true
  cdLoading.value = true
  cdNodes.value = []
  cdVersion.value = ''
  cdCerts.value = []
  // 独立请求互不连坐（Promise.all 教训二次踩坑——任一慢/挂全组丢）
  http.get('/k8s/resources', { params: { kind: 'Node', cluster_id: row.id } })
    .then((r) => { cdNodes.value = r.items || [] }).catch(() => {})
  http.get('/k8mgmt/upgrade/versions', { params: { cluster_id: row.id } })
    .then((v) => { cdVersion.value = v.current || '' }).catch(() => {})
  http.get('/k8mgmt/charts', { params: { cluster_id: row.id } })
    .then((ch) => { cdCerts.value = (ch.cert_soon || []).slice(0, 3) }).catch(() => {})
  setTimeout(() => { cdLoading.value = false }, 600)
}

// 详情内操作：带集群上下文打开既有对话框
function cdAction(kind) {
  const name = clusterDetail.value?.name || ''
  if (kind === 'backup') { backupForm.cluster_name = name; openBackupDialog() }
  if (kind === 'upgrade') { upgradeForm.cluster_name = name; upgradeForm.confirm_name = ''; openUpgrade() }
  if (kind === 'wipe') { wipeForm.cluster_name = name; wipeForm.confirm_name = ''; wipeDialog.value = true }
  if (kind === 'addnode') { nodeForm.cluster_name = name; nodeDialog.value = true; loadMachineOptions() }
  if (kind === 'cfg') { openClusterCfg(clusterDetail.value) }
  if (kind === 'restore') { backupForm.cluster_name = name; openBackupDialog() }
}

const clusterCfgDialog = ref(false)
const clusterCfgRow = ref(null)
const clusterCfg = reactive({ ssh_host: '', ssh_user: 'root', read_only: true, token: '', kubeconfig: '' })

function openClusterCfg(row) {
  clusterCfgRow.value = row
  clusterCfg.ssh_host = row.ssh_host || ''
  clusterCfg.ssh_user = row.ssh_user || 'root'
  clusterCfg.read_only = !!row.read_only
  clusterCfg.token = ''
  clusterCfg.kubeconfig = ''
  clusterCfgDialog.value = true
}

async function saveClusterCfg() {
  try {
    await http.patch(`/k8s/clusters/${clusterCfgRow.value.id}`, {
      ssh_host: clusterCfg.ssh_host, ssh_user: clusterCfg.ssh_user,
      read_only: clusterCfg.read_only,
      token: clusterCfg.token || '',
    })
    ElMessage.success(t('k8s.msg.savedMgmt'))
    clusterCfgDialog.value = false
    loadClusters()
    loadMgmt()
  } catch { /* 拦截器提示 */ }
}
const podOptions = ref([])

// ---- 资源 ----
const kind = ref('Pod')
const namespace = ref('')
const keyword = ref('')
const includeDestroyed = ref(false)
const resStatus = ref('')
const resources = ref([])
const resLoading = ref(false)
const detail = ref(null)
const drawerOpen = ref(false)

// ---- 日志 ----
const logNs = ref('')
const logPod = ref('')
const logKeyword = ref('')
const logs = ref([])
const logLoading = ref(false)
const retention = ref(7)

// ---- 失败 ----
const failStatus = ref('pending')
const failures = ref([])
const failLoading = ref(false)
const selection = ref([])
const batchNote = ref('')
const failConfirmCount = ref(0)
const batching = ref(false)

const selCluster = computed(() => {
  const name = backupForm.cluster_name || clusters.value[0]?.name || ''
  return clusters.value.find((c) => c.name === name) || null
})
const selUpCluster = computed(() => clusters.value.find((c) => c.name === upgradeForm.cluster_name) || clusters.value[0] || null)
const statusOptions = computed(() => {
  const set = new Set(['Running', 'Pending', 'Failed', 'Succeeded', 'Unknown', 'NotReady', 'Ready'])
  resources.value.forEach((r) => r.status && set.add(r.status))
  return [...set]
})
const KINDS = ['Pod', 'Service', 'Ingress', 'Deployment', 'StatefulSet', 'Node', 'Namespace', 'ConfigMap', 'Secret', 'PersistentVolumeClaim']
// 集群筛选联动：顶部下拉选中某集群后，卡片区只显示该集群
const filteredClusters = computed(() => clusterFilter.value
  ? clusters.value.filter((c) => c.id === clusterFilter.value)
  : clusters.value)

// ---- 任务中心 ----
const tasks = ref([])
const taskDetail = ref(null)
const taskLogs = ref([])
const taskLogTimer = ref(null)
const lastLogId = ref(0)
const abortingId = ref(0)
const taskDrawerVisible = computed({
  get: () => !!taskDetail.value,
  set: (v) => { if (!v) closeTaskDrawer() },
})

// ---- 总览/统计/事件 ----
const mgmtOverview = ref(null)
const mgmtStats = ref(null)
const mgmtEvents = ref([])
const mgmtCharts = ref({})
const eventsRefresh = ref(0)   // 0=手动，10/30/60 秒档
let eventsTimer = null
const mgmtAudits = ref([])
const mgmtImages = ref([])
const mgmtHelm = ref([])
const mgmtHelmNote = ref('')
const secretVisible = ref(false)
const secretRow = ref(null)
const secretData = ref('')
const secretConfirm = ref('')
const secretLoading = ref(false)

async function loadTasks() {
  try {
    const r = await http.get('/k8mgmt/tasks')
    tasks.value = r.items || []
  } catch { /* 拦截器提示 */ }
}

async function openTask(task) {
  taskDetail.value = task
  taskLogs.value = []
  lastLogId.value = 0
  await pollTaskLogs()
  clearInterval(taskLogTimer.value)
  taskLogTimer.value = setInterval(pollTaskLogs, 2000)   // 日志 2 秒刷新（需求）
}

async function pollTaskLogs() {
  if (!taskDetail.value) return
  try {
    const r = await http.get(`/k8mgmt/tasks/${taskDetail.value.id}/logs`, {
      params: { after_id: lastLogId.value },
    })
    if (r.items?.length) {
      taskLogs.value.push(...r.items)
      lastLogId.value = r.items[r.items.length - 1].id
    }
    const d = await http.get(`/k8mgmt/tasks/${taskDetail.value.id}`)
    taskDetail.value = d
    if (d.status !== 'running' && d.status !== 'pending') clearInterval(taskLogTimer.value)
    loadTasks()
  } catch { /* 拦截器提示 */ }
}

async function abortTask(task) {
  abortingId.value = task.id
  try {
    await http.post(`/k8mgmt/tasks/${task.id}/abort`)
    ElMessage.success(t('k8s.msg.abortRequested'))
    loadTasks()
    if (taskDetail.value?.id === task.id) pollTaskLogs()
  } finally {
    abortingId.value = 0
  }
}

// ---- 管理操作（添加节点/创建集群/备份/升级/清空）----
const nodeDialog = ref(false)
const createDialog = ref(false)
const backupDialog = ref(false)
const upgradeDialog = ref(false)
const wipeDialog = ref(false)
const mgmtSubmitting = ref(false)
const machineOptions = ref([])
const nodeForm = reactive({ machine_id: 0, ip: '', ssh_user: 'root', ssh_password: '', cluster_name: '' })
const createForm = reactive({
  machine_id: 0, ip: '', ssh_user: 'root', ssh_password: '',
  k8s_version: '1.28.2', pod_cidr: '10.244.0.0/16', svc_cidr: '10.96.0.0/12',
  cluster_name: '', cni: 'flannel',
})
// ---- 部署集群向导（4 步，参考 k8smanager 向导产品思路）----
const createStep = ref(0)
const createPrecheck = ref(null)   // {ok, detail}
const createPrechecking = ref(false)

function createNext() {
  if (createStep.value === 0 && (!createForm.cluster_name || !createForm.k8s_version)) {
    return ElMessage.warning(t('k8s.msg.needClusterNameVersion'))
  }
  if (createStep.value === 1 && !createForm.ip) {
    return ElMessage.warning(t('k8s.msg.needMachineOrIp'))
  }
  createStep.value = Math.min(3, createStep.value + 1)
}

function createPrev() {
  createStep.value = Math.max(0, createStep.value - 1)
}

async function runCreatePrecheck() {
  createPrechecking.value = true
  createPrecheck.value = null
  try {
    const r = await http.post('/k8mgmt/precheck/deep', {
      ip: createForm.ip, ssh_user: createForm.ssh_user,
      ssh_password: createForm.ssh_password, machine_id: createForm.machine_id || 0,
    })
    createPrecheck.value = { ok: r.ok, items: r.items || [], summary: r.summary || {}, note: r.note || '' }
  } catch (e) {
    createPrecheck.value = { ok: false, items: [], summary: {}, note: e?.response?.data?.detail || t('k8s.msg.precheckFailed') }
  } finally {
    createPrechecking.value = false
  }
}

async function loadMachineOptions() {
  try {
    const r = await http.get('/machines')
    machineOptions.value = r.items || []
  } catch { /* 拦截器提示 */ }
}

function pickMachine(form) {
  const m = machineOptions.value.find((x) => x.id === form.machine_id)
  if (m) {
    form.ip = m.ip
    form.ssh_user = m.ssh_user
    form.ssh_password = ''   // 凭证由后端从机器库解密取，不回填不显示
    ElMessage.success(t('k8s.msg.machinePicked', { ip: m.ip }))
  }
}
const backupForm = reactive({ cluster_name: '' })
const upgradeForm = reactive({ target_version: '', cluster_name: '', confirm_name: '' })
const wipeForm = reactive({ cluster_name: '', confirm_name: '' })
const upgradeInfo = ref({ current: '', upgrade_path: [] })
const precheck = ref(null)
const prechecking = ref(false)

function setEventsRefresh(sec) {
  eventsRefresh.value = sec
  clearInterval(eventsTimer)
  if (sec > 0) eventsTimer = setInterval(loadMgmt, sec * 1000)
}

function goRes(k) {
  kind.value = k
  tab.value = 'resources'
  loadResources()
}

// 集群卡片统计数字点击：跳资源清单并自动带 kind + cluster_id 筛选
function goResKind(k, cid) {
  if (cid) clusterFilter.value = cid
  kind.value = k
  tab.value = 'resources'
  loadResources()
  loadOverview()   // 顶部统计数字同步为该集群口径
}

// 总览条"待处理失败 N"此前是死数字（周围 Pod/Node 都能点，就它不能），补齐跳转
function goFailures() {
  tab.value = 'failures'
  loadFailures()
}

// cluster_id → 集群名（Secret 清单走管理通道只带 id 的兜底映射）
function clusterNameOf(id) {
  return clusters.value.find((c) => c.id === id)?.name || ''
}

function gotoTasks() {
  tab.value = 'tasks'
  loadTasks()
}

async function submitNode() {
  if (!nodeForm.ip) return ElMessage.warning(t('k8s.msg.needTargetIp'))
  mgmtSubmitting.value = true
  try {
    const r = await http.post('/k8mgmt/nodes/add', { ...nodeForm })
    ElMessage.success(t('k8s.msg.nodeSubmitted', { id: r.task_id }))
    nodeDialog.value = false
    gotoTasks()
  } finally { mgmtSubmitting.value = false }
}

async function submitCreate() {
  if (!createForm.ip || !createForm.cluster_name) return ElMessage.warning(t('k8s.msg.needMasterIpName'))
  if (!createPrecheck.value?.ok) return ElMessage.warning(t('k8s.msg.needPrecheckFirst'))
  mgmtSubmitting.value = true
  try {
    const r = await http.post('/k8mgmt/clusters/create', { ...createForm })
    ElMessage.success(t('k8s.msg.createSubmitted', { id: r.task_id }))
    createDialog.value = false
    createStep.value = 0
    createPrecheck.value = null
    gotoTasks()
  } finally { mgmtSubmitting.value = false }
}

async function submitBackup() {
  mgmtSubmitting.value = true
  try {
    const r = await http.post('/k8mgmt/backups', { ...backupForm })
    ElMessage.success(t('k8s.msg.backupSubmitted', { id: r.task_id }))
    backupDialog.value = false
    gotoTasks()
  } finally { mgmtSubmitting.value = false }
}

async function openUpgrade() {
  upgradeDialog.value = true
  precheck.value = null
  try {
    upgradeInfo.value = await http.get('/k8mgmt/upgrade/versions', { params: { cluster_name: upgradeForm.cluster_name } })
  } catch { /* 拦截器提示 */ }
}

async function runPrecheck() {
  if (!upgradeForm.target_version) return ElMessage.warning(t('k8s.msg.needTargetVersion'))
  prechecking.value = true
  try {
    precheck.value = await http.get('/k8mgmt/upgrade/precheck', {
      params: { target: upgradeForm.target_version.split('.').slice(0, 2).join('.'), cluster_name: upgradeForm.cluster_name },
    })
  } finally { prechecking.value = false }
}

async function submitUpgrade() {
  if (upgradeForm.confirm_name !== upgradeForm.cluster_name) return ElMessage.warning(t('k8s.msg.needClusterConfirm'))
  if (precheck.value && !precheck.value.passed) return ElMessage.error(t('k8s.msg.precheckBlocked'))
  mgmtSubmitting.value = true
  try {
    const r = await http.post('/k8mgmt/upgrade', { ...upgradeForm })
    ElMessage.success(t('k8s.msg.upgradeSubmitted', { id: r.task_id }))
    upgradeDialog.value = false
    gotoTasks()
  } finally { mgmtSubmitting.value = false }
}

async function submitWipe() {
  if (wipeForm.confirm_name !== wipeForm.cluster_name) return ElMessage.warning(t('k8s.msg.needClusterConfirm'))
  mgmtSubmitting.value = true
  try {
    await http.post('/k8mgmt/cluster/wipe', { ...wipeForm })
    ElMessage.success(t('k8s.msg.wipeSubmitted'))
    wipeDialog.value = false
    gotoTasks()
  } finally { mgmtSubmitting.value = false }
}

// ---- 资源操作（yaml 查看/编辑 · 禁止调度 · 驱逐/删除）----
const yamlVisible = ref(false)
const yamlEdit = ref(false)
const yamlText = ref('')
const yamlError = ref('')
const yamlRow = ref(null)
const opDialog = ref(false)
const opRow = ref(null)
const opAction = ref('')          // cordon / uncordon / evict / delete
const opConfirmName = ref('')

async function viewYaml(row, edit = false) {
  yamlRow.value = row
  yamlEdit.value = edit
  yamlError.value = ''
  yamlText.value = ''
  yamlVisible.value = true
  try {
    const r = await http.get(`/k8mgmt/resources/${row.kind}/${row.name}/yaml`, {
      params: { namespace: row.namespace || '', cluster_id: row.cluster_id || 0 },
    })
    if (r.ok === false) {
      yamlError.value = r.error || t('k8s.msg.yamlLoadFailed')
      return
    }
    yamlText.value = r.yaml || ''
  } catch (e) {
    yamlError.value = e?.response?.data?.detail || t('k8s.msg.yamlLoadFailed')
  }
}

async function saveYaml() {
  if (opConfirmName.value !== yamlRow.value.name) return ElMessage.warning(t('k8s.msg.resourceNameMismatch'))
  try {
    await http.put(`/k8mgmt/resources/${yamlRow.value.kind}/${yamlRow.value.name}/yaml`, {
      yaml: yamlText.value, confirm_name: opConfirmName.value,
      cluster_id: yamlRow.value.cluster_id || 0,
    }, { params: { namespace: yamlRow.value.namespace || '' } })
    ElMessage.success(t('k8s.msg.applied'))
    yamlVisible.value = false
  } catch { /* 拦截器提示 */ }
}

function askOp(row, action) {
  opRow.value = row
  opAction.value = action
  opConfirmName.value = ''
  opDialog.value = true
}

const OP_LABELS = {
  cordon: t('k8s.op.cordon'), uncordon: t('k8s.op.uncordon'), drain: t('k8s.op.drain'), evict: t('k8s.op.evict'),
  delete: t('k8s.op.delete'), restart: t('k8s.op.restart'), trigger: t('k8s.op.trigger'), suspend: t('k8s.op.suspend'), resume: t('k8s.op.resume'),
}
// 状态/类型中文映射（模板共用常量；页面刷新才换语言，setup 求值一次）
const TASK_TYPE_LABELS = {
  add_node: t('k8s.op.addNode'), create_cluster: t('k8s.op.createCluster'), backup: t('k8s.op.backup'),
  upgrade: t('k8s.op.upgrade'), wipe: t('k8s.op.wipe'),
}
const TASK_STATUS_LABELS = {
  succeeded: t('k8s.tasks.stSucceeded'), failed: t('k8s.tasks.stFailed'), running: t('k8s.tasks.stRunning'),
  pending: t('k8s.tasks.stPending'), aborted: t('k8s.tasks.stAborted'),
}
const CLUSTER_STATUS_LABELS = {
  ok: t('k8s.status.ok'), error: t('k8s.status.error'), unknown: t('k8s.status.unknown'),
}
const FAIL_STATUS_LABELS = {
  pending: t('k8s.fail.pending'), ignored: t('k8s.fail.ignored'), confirmed: t('k8s.fail.confirmed'),
}
const batchActionWord = () => (batchAction.value === 'restart' ? t('k8s.word.restart') : t('k8s.word.delete'))
const failActionWord = (action) => (action === 'ignore' ? t('k8s.word.ignore') : t('k8s.word.confirm'))
const failActionWordPast = (action) => (action === 'ignore' ? t('k8s.word.ignored') : t('k8s.word.confirmed'))

// 行操作"更多"菜单分发（收纳次要动作，主区只留 详情/yaml/更多）
function rowOp(cmd, row) {
  if (cmd === 'edit') return openFormEdit(row)
  if (cmd === 'secret') return askSecret(row)
  if (cmd === 'term') return openPodTerm(row)
  askOp(row, cmd)
}

async function doOp() {
  const row = opRow.value
  if (opConfirmName.value !== row.name) return ElMessage.warning(t('k8s.msg.resourceNameMismatchOp'))
  const cid = row.cluster_id || 0
  try {
    const ns = row.namespace || 'default'
    if (opAction.value === 'cordon' || opAction.value === 'uncordon') {
      await http.post(`/k8mgmt/resources/Node/${row.name}/cordon`, {
        confirm_name: row.name, action: opAction.value, cluster_id: cid,
      })
    } else if (opAction.value === 'drain') {
      await http.post(`/k8mgmt/resources/Node/${row.name}/drain`, {
        confirm_name: row.name, cluster_id: cid,
      })
    } else if (opAction.value === 'restart') {
      await http.post(`/k8mgmt/resources/${row.kind}/${row.name}/restart`, {
        confirm_name: row.name, cluster_id: cid,
      }, { params: { namespace: ns } })
    } else if (['trigger', 'suspend', 'resume'].includes(opAction.value)) {
      await http.post(`/k8mgmt/resources/CronJob/${row.name}/cronop`, {
        confirm_name: row.name, action: opAction.value, cluster_id: cid,
      }, { params: { namespace: ns } })
    } else {
      await http.post(`/k8mgmt/resources/${row.kind}/${row.name}/evict`, {
        confirm_name: row.name, cluster_id: cid,
      }, { params: { namespace: ns } })
    }
    ElMessage.success(t('k8s.msg.opDone', { op: OP_LABELS[opAction.value] }))
    opDialog.value = false
    loadResources()
  } catch { /* 拦截器提示 */ }
}

// ---- 批量重启/删除（confirm_count 防误选）----
const batchDialog = ref(false)
const batchAction = ref('restart')
const batchConfirmCount = ref(0)
const batchSelection = ref([])

function askBatch(action) {
  if (!batchSelection.value.length) return ElMessage.warning(t('k8s.msg.selectResourcesFirst'))
  // 批量删除沿用单行禁删清单：Node/Namespace 不进批量删除（后端同样硬拦）
  if (action === 'delete') {
    const banned = batchSelection.value.filter((r2) => ['Node', 'Namespace'].includes(r2.kind))
    if (banned.length) {
      return ElMessage.warning(t('k8s.msg.batchDeleteBanned', { names: banned.map((x) => x.name).slice(0, 3).join(t('k8s.word.nameSep')) }))
    }
  }
  batchAction.value = action
  batchConfirmCount.value = 0
  batchDialog.value = true
}

async function submitBatch() {
  try {
    const r = await http.post('/k8mgmt/resources/batch', {
      // 每条带 cluster_id：批量可跨集群勾选，逐条解析目标集群
      items: batchSelection.value.map((r2) => ({ kind: r2.kind, name: r2.name, namespace: r2.namespace, cluster_id: r2.cluster_id || 0 })),
      action: batchAction.value, confirm_count: Number(batchConfirmCount.value) || 0,
    })
    ElMessage.success(t('k8s.msg.batchDone', { done: r.done, failed: r.failed }))
    if (r.failed) {
      const fails = r.results.filter((x) => !x.ok).slice(0, 3).map((x) => `${x.name}: ${x.detail}`).join('\n')
      ElMessageBox.alert(fails, t('k8s.msg.batchPartialFail', { action: batchActionWord() }), { type: 'warning' })
    }
    batchDialog.value = false
    loadResources()
  } catch { /* 拦截器提示 */ }
}

function onSelectionChange(rows) {
  batchSelection.value = rows
}

// ---- 备份集管理与恢复 ----
const backupSets = ref([])
const backupSetsLoading = ref(false)
const restoreDialog = ref(false)
const restoreForm = reactive({ backup_set: '', cluster_name: '', confirm_name: '' })

async function loadBackupSets() {
  backupSetsLoading.value = true
  try {
    const r = await http.get('/k8mgmt/backups/sets', { params: { cluster_name: backupForm.cluster_name } })
    backupSets.value = r.items || []
  } catch { /* 拦截器提示 */ } finally { backupSetsLoading.value = false }
}

function openBackupDialog() {
  backupDialog.value = true
  loadBackupSets()
}

function askRestore(row) {
  restoreForm.backup_set = row.name
  restoreForm.cluster_name = backupForm.cluster_name
  restoreForm.confirm_name = ''
  restoreDialog.value = true
}

async function submitRestore() {
  if (restoreForm.confirm_name !== restoreForm.cluster_name) return ElMessage.warning(t('k8s.msg.needClusterConfirm'))
  try {
    const r = await http.post('/k8mgmt/backups/restore', {
      cluster_name: restoreForm.cluster_name, backup_set: restoreForm.backup_set,
      confirm_name: restoreForm.confirm_name,
    })
    ElMessage.success(t('k8s.msg.restoreSubmitted', { id: r.task_id }))
    restoreDialog.value = false
    backupDialog.value = false
    gotoTasks()
  } catch { /* 拦截器提示 */ }
}

// ---- 图形化编辑（表单改字段：副本/镜像/标签/资源限额）----
const formEditVisible = ref(false)
const formEditRow = ref(null)
const formEdit = reactive({
  confirm_name: '', replicas: '', image: '', labelsText: '',
  limits_cpu: '', limits_mem: '', requests_cpu: '', requests_mem: '',
})

function openFormEdit(row) {
  formEditRow.value = row
  formEdit.confirm_name = ''
  formEdit.replicas = ''
  formEdit.image = ''
  formEdit.labelsText = Object.entries(row.labels || {}).map(([k, v]) => `${k}=${v}`).join('\n')
  formEdit.limits_cpu = formEdit.limits_mem = formEdit.requests_cpu = formEdit.requests_mem = ''
  formEditVisible.value = true
}

async function submitFormEdit() {
  const row = formEditRow.value
  if (formEdit.confirm_name !== row.name) return ElMessage.warning(t('k8s.msg.resourceNameMismatchChange'))
  const labels = {}
  for (const line of formEdit.labelsText.split('\n')) {
    const i = line.indexOf('=')
    if (i > 0 && line.slice(0, i).trim()) labels[line.slice(0, i).trim()] = line.slice(i + 1).trim()
  }
  try {
    const r = await http.put(`/k8mgmt/resources/${row.kind}/${row.name}/form`, {
      confirm_name: row.name, cluster_id: row.cluster_id || 0,
      replicas: formEdit.replicas === '' ? null : Number(formEdit.replicas),
      image: formEdit.image, labels,
      limits_cpu: formEdit.limits_cpu, limits_mem: formEdit.limits_mem,
      requests_cpu: formEdit.requests_cpu, requests_mem: formEdit.requests_mem,
    }, { params: { namespace: row.namespace || '' } })
    ElMessage.success(t('k8s.msg.appliedList', { list: (r.changed || []).join(t('k8s.word.listSep')) }))
    formEditVisible.value = false
    loadResources()
  } catch { /* 拦截器提示 */ }
}

// ---- Helm 操作（升级/卸载，高危门）----
const helmDialog = ref(false)
const helmRow = ref(null)
const helmAction = ref('upgrade')
const helmConfirm = ref('')
const helmChart = ref('')
const helmClusterId = ref(0)

function askHelm(row, action) {
  helmRow.value = row
  helmAction.value = action
  helmConfirm.value = ''
  helmChart.value = row.chart || ''
  helmClusterId.value = action === 'upgrade' ? (clusterFilter.value || 0) : 0
  helmDialog.value = true
}

async function submitHelm() {
  const row = helmRow.value
  if (helmConfirm.value !== row.name) return ElMessage.warning(t('k8s.msg.releaseMismatch'))
  try {
    const url = `/k8mgmt/helm/${row.name}/${helmAction.value}`
    const body = { confirm_name: row.name, chart_ref: helmChart.value }
    if (helmAction.value === 'upgrade') body.cluster_id = helmClusterId.value
    const r = await http.post(url, body, { params: { namespace: row.namespace || 'default' } })
    ElMessage.success(r.ok ? t('k8s.msg.executed') : t('k8s.msg.execError', { detail: (r.detail || '').slice(0, 60) }))
    helmDialog.value = false
    loadMgmt()
  } catch { /* 拦截器提示 */ }
}

// ---- Secret 揭示（高危，揭示即审计）----
function askSecret(row) {
  secretRow.value = row
  secretData.value = ''
  secretConfirm.value = ''
  secretVisible.value = true
}

async function revealSecret() {
  const row = secretRow.value
  if (secretConfirm.value !== row.name) return ElMessage.warning(t('k8s.msg.secretMismatch'))
  secretLoading.value = true
  try {
    const r = await http.post(`/k8mgmt/resources/Secret/${row.name}/reveal`, {
      confirm_name: row.name, cluster_id: row.cluster_id || 0,
    }, { params: { namespace: row.namespace || 'default' } })
    secretData.value = r.data_raw || t('k8s.word.empty')
    ElMessage.warning(t('k8s.msg.revealAudited'))
  } catch { /* 拦截器提示 */ } finally { secretLoading.value = false }
}

function closeTaskDrawer() {
  clearInterval(taskLogTimer.value)
  taskDetail.value = null
}

async function loadMgmt() {
  // 各请求独立渲染、互不拖累：charts 的 SSH 证书采集可能较慢，
  // 不能用 Promise.all 让慢请求拖住总览，也不能让任一失败清空全组
  const cid = clusterFilter.value || 0
  http.get('/k8mgmt/overview', { params: { cluster_id: cid } })
    .then((r) => { mgmtOverview.value = r }).catch(() => {})
  http.get('/k8mgmt/stats', { params: { cluster_id: cid } })
    .then((r) => { mgmtStats.value = r }).catch(() => {})
  http.get('/k8mgmt/charts', { params: { cluster_id: cid } })
    .then((r) => { mgmtCharts.value = r }).catch(() => {})
  http.get('/k8mgmt/events', { params: { cluster_id: cid } })
    .then((ev) => { mgmtEvents.value = ev.items || [] }).catch(() => {})
  http.get('/k8mgmt/audits').then((au) => { mgmtAudits.value = au.items || [] }).catch(() => {})
  // 镜像/Helm（SSH 型慢接口）只在 stats tab 拉取（watch + onMounted 触发）——
  // 不能进页即拉，否则多集群串行 SSH 可能拖穿网关超时
  loadTasks()   // 任务中心进页即加载
}

function diagFromEvent(e) {
  window.open(`/diagnose?q=${encodeURIComponent(t('k8s.msg.diagEventQuery', { ns: e.namespace, name: e.object_name, msg: e.message }))}`, '_blank')
}
const FAIL_TAGS = { OOMKilled: 'danger', CrashLoopBackOff: 'danger', ImagePullBackOff: 'warning', Pending: 'warning' }
// 任务状态本地化：表格/详情使用 k8s.tasks.st* 文案；
// 失败类型（OOMKilled 等）保留原文——运维通用术语。
const TASK_STATUS_KEYS = {
  succeeded: 'k8s.tasks.stSucceeded', failed: 'k8s.tasks.stFailed',
  running: 'k8s.tasks.stRunning', pending: 'k8s.tasks.stPending',
  aborted: 'k8s.tasks.stAborted',
}
function taskStatusLabel(s) {
  const k = TASK_STATUS_KEYS[String(s || '').toLowerCase()]
  return k ? t(k) : (s || '—')
}

async function loadClusters() {
  try {
    const r = await http.get('/k8s/overview')
    clusters.value = r.clusters || []
  } catch { /* 拦截器提示 */ }
}

async function testCluster(row) {
  testingCid.value = row.id
  try {
    const r = await http.post(`/k8s/clusters/${row.id}/test`)
    ElMessage({ type: r.ok ? 'success' : 'error', message: `${row.name}: ${r.detail}` })
    loadClusters()
  } finally {
    testingCid.value = 0
  }
}

async function removeCluster(row) {
  await ElMessageBox.confirm(t('k8s.msg.removeClusterConfirm', { name: row.name }), t('k8s.word.confirm'), { type: 'warning' })
  await http.delete(`/k8s/clusters/${row.id}`)
  ElMessage.success(t('k8s.msg.deleted'))
  clusterFilter.value = 0
  loadClusters(); loadResources(); loadFailures()
}

async function saveCluster() {
  if (!clusterForm.name || !clusterForm.api_url || !clusterForm.token) return ElMessage.warning(t('k8s.msg.clusterRequired'))
  try {
    await http.post('/k8s/clusters', { ...clusterForm })
    ElMessage.success(t('k8s.msg.clusterAdded'))
    clusterDialog.value = false
    loadClusters()
  } catch { /* 拦截器提示（含重复接入提示） */ }
}

async function loadNamespaces() {
  try {
    nsOptions.value = await http.get('/k8s/namespaces')
  } catch { /* 拦截器提示 */ }
}

async function onLogNsChange() {
  logPod.value = ''
  loadPods()
}

async function loadPods() {
  try {
    podOptions.value = await http.get('/k8s/pods', { params: { namespace: logNs.value } })
  } catch { /* 拦截器提示 */ }
}

async function loadOverview() {
  try {
    // cluster_id：顶部统计数字随「集群」下拉联动（0=全部集群口径）
    overview.value = await http.get('/k8s/overview', { params: { cluster_id: clusterFilter.value } })
    clusters.value = overview.value.clusters || []
  } catch { /* 拦截器提示 */ }
}

async function loadResources() {
  if (kind.value === 'Secret') {
    // Secret 清单走管理通道（SA view 无 secrets 权限——K8s 设计边界）
    resLoading.value = true
    try {
      const r = await http.get('/k8mgmt/secrets', {
        params: { namespace: namespace.value || '', cluster_id: clusterFilter.value },
      })
      resources.value = (r.items || []).map((x) => ({ ...x, cluster_id: x.cluster_id || 0 }))
    } catch { /* 拦截器提示 */ } finally { resLoading.value = false }
    return
  }
  resLoading.value = true
  try {
    const r = await http.get('/k8s/resources', {
      params: { kind: kind.value, namespace: namespace.value, keyword: keyword.value,
                status: resStatus.value,
                include_destroyed: includeDestroyed.value ? 1 : 0, cluster_id: clusterFilter.value },
    })
    resources.value = r.items || []
  } finally {
    resLoading.value = false
  }
}

async function scan() {
  try {
    const r = await http.post('/k8s/scan')
    ElMessage.success(t('k8s.msg.scanDone', {
      list: r.clusters.map((c) => t('k8s.msg.scanItem', { cluster: c.cluster, resources: c.resources, newFailures: c.new_failures })).join(t('k8s.word.itemSep')),
    }))
    loadOverview(); loadResources(); loadFailures()
  } catch { /* 拦截器提示 */ }
}

async function loadLogs() {
  logLoading.value = true
  try {
    const r = await http.get('/k8s/logs', {
      params: { namespace: logNs.value, pod: logPod.value, keyword: logKeyword.value,
                include_destroyed: 1, limit: 200, cluster_id: clusterFilter.value },
    })
    logs.value = r.items || []
  } finally {
    logLoading.value = false
  }
}

async function loadRetention() {
  try {
    const r = await http.get('/k8s/log-retention')
    retention.value = r.days
  } catch { /* 拦截器提示 */ }
}

async function saveRetention() {
  try {
    await ElMessageBox.confirm(
      t('k8s.log.retentionConfirmBody', { days: retention.value }),
      t('k8s.log.retentionConfirmTitle'), { type: 'warning', confirmButtonText: t('k8s.log.retentionConfirmOk'), cancelButtonText: t('k8s.log.retentionConfirmCancel') })
  } catch {
    return
  }
  const r = await http.put('/k8s/log-retention', { days: retention.value })
  ElMessage.success(t('k8s.log.retentionSaved', { days: r.days, rows: r.cleaned_rows }))
  loadOverview()
}

async function loadFailures() {
  failLoading.value = true
  try {
    const r = await http.get('/k8s/failures', { params: { status: failStatus.value, cluster_id: clusterFilter.value } })
    failures.value = r.items || []
    selection.value = []
  } finally {
    failLoading.value = false
  }
}

async function batch(action) {
  if (!selection.value.length) return ElMessage.warning(t('k8s.msg.selectItemsFirst'))
  const n = selection.value.length
  if (n > 1) {
    // 数量核对门（批量高危模式）：输入条目数才放行
    try {
      const { value } = await ElMessageBox.prompt(
        t('k8s.fail.batchPromptBody', { action: failActionWord(action), n }),
        t('k8s.fail.batchPromptTitle'), { inputPattern: new RegExp(`^${n}$`), inputErrorMessage: t('k8s.fail.batchCountMismatch') })
      failConfirmCount.value = Number(value)
    } catch { return }
  } else {
    failConfirmCount.value = 1
  }
  batching.value = true
  try {
    const r = await http.post('/k8s/failures/batch', { confirm_count: Number(failConfirmCount.value) || 0,
      ids: selection.value.map((x) => x.id), action, note: batchNote.value,
    })
    ElMessage.success(t('k8s.fail.batchDone', { action: failActionWordPast(action), n: r.updated }))
    batchNote.value = ''
    loadFailures(); loadOverview()
  } finally {
    batching.value = false
  }
}

function goLogs(ns, pod) {
  logNs.value = ns
  logPod.value = pod
  tab.value = 'logs'
  drawerOpen.value = false
  loadPods()
  loadLogs()
  ElMessage.info(t('k8s.msg.logsLocated', { ns, pod }))
}

function diagFrom(row) {
  window.open(`/diagnose?q=${encodeURIComponent(t('k8s.msg.diagFailQuery', { ns: row.namespace, pod: row.pod, type: row.failure_type, diag: (row.diagnosis || '').slice(0, 300) }))}`, '_blank')
}

// 问智能体深链：失败分析行带对象上下文落到智能体新会话首条
function askAgent(row) {
  window.open(agentAskUrl(
    t('k8s.msg.diagFailQuery', { ns: row.namespace, pod: row.pod, type: row.failure_type, diag: (row.diagnosis || '').slice(0, 300) }),
    {
      page: '/k8s?tab=failures', cluster_id: row.cluster_id || 0,
      namespace: row.namespace, pod: row.pod, container: row.container,
      failure_type: row.failure_type, status: row.status,
    },
  ), '_blank')
}

onMounted(() => {
  loadOverview(); loadResources(); loadLogs(); loadRetention(); loadFailures(); loadNamespaces()
  loadMgmt(); loadClusters()   // 管理面（任务/统计/事件/集群）与监控面一起进页即载
  if (tab.value === 'stats') loadSlowPanels()   // 初始直达 stats tab 才拉镜像/Helm（watch 不管初始值）
})
</script>

<template>
  <div class="page">
    <!-- 总览条 -->
    <div class="ov-bar" v-if="overview">
      <el-select v-model="clusterFilter" size="small" style="width: 170px"
                 @change="() => { loadMgmt(); loadOverview(); loadResources(); loadLogs(); loadFailures(); if (tab === 'stats') loadSlowPanels() }">
        <el-option :label="t('k8s.filter.allClusters')" :value="0" />
        <el-option v-for="c in clusters" :key="c.id" :label="c.name" :value="c.id" />
      </el-select>
      <span class="ov-link" :title="t('k8s.ov.seeList')" @click="goResKind('Pod')">{{ 'Pod ' + (overview.resources?.Pod || 0) }}</span>
      <span class="ov-link" :title="t('k8s.ov.seeList')" @click="goResKind('Service')">{{ 'Service ' + (overview.resources?.Service || 0) }}</span>
      <span class="ov-link" :title="t('k8s.ov.seeList')" @click="goResKind('Ingress')">{{ 'Ingress ' + (overview.resources?.Ingress || 0) }}</span>
      <span class="ov-link" :title="t('k8s.ov.seeList')" @click="goResKind('Deployment')">{{ 'Deployment ' + (overview.resources?.Deployment || 0) }}</span>
      <span class="ov-link" :title="t('k8s.ov.seeList')" @click="goResKind('Node')">{{ 'Node ' + (overview.resources?.Node || 0) }}</span>
      <span class="fail ov-link" v-if="overview.pending_failures" :title="t('k8s.ov.seeList')" @click="goFailures">{{ t('k8s.ov.pendingFailures', { n: overview.pending_failures }) }}</span>
      <span>{{ t('k8s.ov.logStat', { rows: overview.log_rows, days: overview.retention_days }) }}</span>
      <el-button v-if="canWrite" size="small" type="primary" @click="scan">{{ t('k8s.actions.scan') }}</el-button>
    </div>

    <!-- 导航由侧栏菜单唯一负责（?tab= 锚点），页内不再放横向 tab 条 -->
      <!-- ===== 总览 ===== -->
      <section v-if="tab === 'overview'" class="tab-page">
        <div v-if="mgmtOverview" class="ov-grid">
          <div class="ov-card clickable" @click="tab = 'clusters'"><div class="ic ic-blue"><el-icon><Connection /></el-icon></div>
            <div><div class="num">{{ mgmtOverview.cluster_total }}</div><div class="label">{{ t('k8s.ov.cardClusterTotal') }}</div></div></div>
          <div class="ov-card clickable" @click="tab = 'clusters'"><div class="ic ic-green"><el-icon><CircleCheckFilled /></el-icon></div>
            <div><div class="num ok">{{ mgmtOverview.cluster_ok }}</div><div class="label">{{ t('k8s.ov.cardClusterOk') }}</div></div></div>
          <div class="ov-card clickable" @click="goRes('Node')"><div class="ic ic-purple"><el-icon><Cpu /></el-icon></div>
            <div><div class="num">{{ mgmtOverview.nodes }}</div><div class="label">{{ t('k8s.ov.cardNodes') }}</div></div></div>
          <div class="ov-card clickable" @click="goRes('Namespace')"><div class="ic ic-cyan"><el-icon><Grid /></el-icon></div>
            <div><div class="num">{{ mgmtOverview.namespaces }}</div><div class="label">{{ t('k8s.ov.cardNamespaces') }}</div></div></div>
          <div class="ov-card clickable" @click="goRes('Pod')"><div class="ic ic-orange"><el-icon><Box /></el-icon></div>
            <div><div class="num">{{ mgmtOverview.pods }}</div><div class="label">{{ t('k8s.ov.cardPods') }}</div></div></div>
          <div class="ov-card clickable" @click="goRes('Deployment')"><div class="ic ic-pink"><el-icon><Files /></el-icon></div>
            <div><div class="num">{{ mgmtOverview.workloads }}</div><div class="label">{{ t('k8s.ov.cardWorkloads') }}</div></div></div>
        </div>
        <K8sCharts v-if="mgmtCharts" :charts="mgmtCharts" style="margin-top: 14px" />
        <el-card v-if="mgmtOverview" shadow="never" style="margin-top: 12px">
          <template #header><b>{{ t('k8s.ov.recentTasks') }}</b></template>
          <el-table :data="mgmtOverview.recent_tasks" size="small" border :empty-text="t('k8s.empty.noTasks')">
            <el-table-column prop="title" :label="t('k8s.table.task')" min-width="200" />
            <el-table-column :label="t('k8s.table.status')" width="100">
              <template #default="{ row }">
                <el-tag :type="{succeeded:'success',failed:'danger',running:'primary',pending:'info',aborted:'warning'}[row.status]" size="small">{{ taskStatusLabel(row.status) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column :label="t('k8s.table.duration')" width="80">
              <template #default="{ row }">{{ row.duration_s || '—' }}s</template>
            </el-table-column>
          </el-table>
        </el-card>
      </section>

      <!-- ===== 任务中心 ===== -->
      <section v-if="tab === 'tasks'" class="tab-page">
        <div class="toolbar">
          <span class="tip">{{ t('k8s.tasks.tip') }}</span>
          <el-button @click="loadTasks">{{ t('k8s.actions.refresh') }}</el-button>
        </div>
        <el-table :data="tasks" border size="small" v-table-resize :empty-text="t('k8s.empty.noTasksHint')">
          <el-table-column prop="id" label="#" width="60" />
          <el-table-column :label="t('k8s.table.type')" width="110">
            <template #default="{ row }">
              <el-tag size="small">{{ TASK_TYPE_LABELS[row.task_type] || row.task_type }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="title" :label="t('k8s.table.task')" min-width="200" show-overflow-tooltip />
          <el-table-column :label="t('k8s.table.status')" width="90">
            <template #default="{ row }">
              <el-tag :type="{succeeded:'success',failed:'danger',running:'primary',pending:'info',aborted:'warning'}[row.status]" size="small">{{ taskStatusLabel(row.status) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="submitted_by" :label="t('k8s.table.submitter')" width="90" />
          <el-table-column :label="t('k8s.table.duration')" width="80">
            <template #default="{ row }">{{ row.duration_s || '—' }}s</template>
          </el-table-column>
          <el-table-column prop="retries" :label="t('k8s.table.retries')" width="60" />
          <el-table-column :label="t('k8s.table.actions')" width="150">
            <template #default="{ row }">
              <el-button size="default" text type="primary" @click="openTask(row)">{{ t('k8s.tasks.detailLogs') }}</el-button>
              <el-button v-if="['running','pending'].includes(row.status)" size="default" text type="danger"
                         :loading="abortingId === row.id" @click="abortTask(row)">{{ t('k8s.tasks.abort') }}</el-button>
            </template>
          </el-table-column>
        </el-table>
      </section>

      <!-- ===== 资源统计 ===== -->
      <section v-if="tab === 'stats'" class="tab-page">
        <el-row :gutter="16" style="margin-bottom: 14px">
          <el-col :span="12">
            <el-card shadow="never">
              <template #header><b>{{ t('k8s.stats.imagesTitle') }}</b> <span class="tip">{{ t('k8s.stats.imagesTip') }}</span></template>
              <el-table :data="mgmtImages.slice(0, 12)" size="small" border :empty-text="t('k8s.empty.noImages')">
                <el-table-column prop="node" :label="t('k8s.word.node')" width="110" />
                <el-table-column prop="image" :label="t('k8s.table.image')" min-width="220" show-overflow-tooltip />
                <el-table-column prop="size_mb" :label="t('k8s.table.sizeMb')" width="100" />
              </el-table>
            </el-card>
          </el-col>
          <el-col :span="12">
            <el-card shadow="never">
              <template #header><b>Helm Releases</b> <span class="tip">{{ mgmtHelmNote || t('k8s.stats.helmDefaultNote') }}</span></template>
              <el-table :data="mgmtHelm.slice(0, 12)" size="small" border :empty-text="mgmtHelmNote || t('k8s.empty.noRelease')">
                <el-table-column prop="name" :label="t('k8s.table.name')" width="140" show-overflow-tooltip />
                <el-table-column prop="namespace" label="NS" width="100" />
                <el-table-column prop="chart" label="Chart" min-width="150" show-overflow-tooltip />
                <el-table-column prop="status" :label="t('k8s.table.status')" width="90" />
                <el-table-column v-if="canWrite" :label="t('k8s.table.actions')" width="130">
                  <template #default="{ row }">
                    <el-button size="small" text type="warning" @click="askHelm(row, 'upgrade')">{{ t('k8s.word.upgrade') }}</el-button>
                    <el-button size="small" text type="danger" @click="askHelm(row, 'uninstall')">{{ t('k8s.op.uninstall') }}</el-button>
                  </template>
                </el-table-column>
              </el-table>
            </el-card>
          </el-col>
        </el-row>
        <div v-if="mgmtStats" class="ov-grid">
          <div class="ov-card"><div class="num">{{ mgmtStats.ns }}</div><div class="label">{{ t('k8s.word.namespace') }}</div></div>
          <div class="ov-card"><div class="num">{{ mgmtStats.node }}</div><div class="label">{{ t('k8s.word.node') }}</div></div>
          <div class="ov-card"><div class="num">{{ mgmtStats.pod }}</div><div class="label">Pod</div></div>
          <div class="ov-card"><div class="num">{{ mgmtStats.workload }}</div><div class="label">{{ t('k8s.word.workload') }}</div></div>
          <div class="ov-card"><div class="num">{{ mgmtStats.service }}</div><div class="label">Service</div></div>
          <div class="ov-card"><div class="num">{{ mgmtStats.ingress }}</div><div class="label">Ingress</div></div>
          <div class="ov-card"><div class="num">{{ mgmtStats.storage }}</div><div class="label">{{ t('k8s.word.storage') }}</div></div>
          <div class="ov-card"><div class="num warn">{{ mgmtStats.warning }}</div><div class="label">{{ t('k8s.stats.pendingAlerts') }}</div></div>
        </div>
      </section>

      <!-- ===== 事件中心 ===== -->
      <section v-if="tab === 'events'" class="tab-page">
        <div class="toolbar">
          <span class="tip">{{ t('k8s.events.autoRefresh') }}</span>
          <el-radio-group :model-value="eventsRefresh" size="small" @change="setEventsRefresh">
            <el-radio-button :value="0">{{ t('k8s.word.manual') }}</el-radio-button>
            <el-radio-button :value="10">10s</el-radio-button>
            <el-radio-button :value="30">30s</el-radio-button>
            <el-radio-button :value="60">60s</el-radio-button>
          </el-radio-group>
        </div>
        <el-table :data="mgmtEvents" border size="small" :empty-text="t('k8s.empty.noEvents')">
          <el-table-column :label="t('k8s.table.type')" width="80">
            <template #default="{ row }">
              <el-tag :type="row.type === 'Warning' ? 'warning' : 'info'" size="small">{{ row.type }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="reason" :label="t('k8s.table.reason')" width="130" />
          <el-table-column prop="namespace" :label="t('k8s.word.namespace')" width="110" />
          <el-table-column :label="t('k8s.table.object')" width="180">
            <template #default="{ row }">{{ row.object_kind }}/{{ row.object_name }}</template>
          </el-table-column>
          <el-table-column prop="message" :label="t('k8s.table.message')" min-width="240" show-overflow-tooltip />
          <el-table-column prop="count" :label="t('k8s.table.count')" width="60" />
          <el-table-column prop="last_time" :label="t('k8s.table.lastTime')" width="150" />
          <el-table-column label="" width="100">
            <template #default="{ row }">
              <el-button size="default" text type="primary" @click="diagFromEvent(row)">{{ t('k8s.actions.aiDiagnose') }}</el-button>
            </template>
          </el-table-column>
        </el-table>
      </section>

      <!-- ===== 操作审计 ===== -->
      <section v-if="tab === 'audits'" class="tab-page">
        <div class="toolbar">
          <span class="tip">{{ t('k8s.audits.tip') }}</span>
          <el-button @click="loadMgmt">{{ t('k8s.actions.refresh') }}</el-button>
          <el-radio-group :model-value="eventsRefresh" size="small" @change="setEventsRefresh">
            <el-radio-button :value="0">{{ t('k8s.word.manual') }}</el-radio-button>
            <el-radio-button :value="10">10s</el-radio-button>
            <el-radio-button :value="30">30s</el-radio-button>
            <el-radio-button :value="60">60s</el-radio-button>
          </el-radio-group>
        </div>
        <el-table :data="mgmtAudits" border size="small" :empty-text="t('k8s.empty.noAudits')">
          <el-table-column prop="ts" :label="t('k8s.table.time')" width="160" />
          <el-table-column prop="username" :label="t('k8s.table.operator')" width="100" />
          <el-table-column prop="action" :label="t('k8s.table.action')" width="200">
            <template #default="{ row }"><span class="mono">{{ row.action }}</span></template>
          </el-table-column>
          <el-table-column :label="t('k8s.table.object')" width="220">
            <template #default="{ row }"><span class="mono">{{ row.resource_type }}/{{ row.resource_id }}</span></template>
          </el-table-column>
          <el-table-column :label="t('k8s.table.detail')" min-width="220">
            <template #default="{ row }"><span class="tip">{{ JSON.stringify(row.detail).slice(0, 120) }}</span></template>
          </el-table-column>
          <el-table-column prop="client_ip" :label="t('k8s.table.clientIp')" width="130" />
        </el-table>
      </section>

      <!-- ===== 集群管理 ===== -->
      <section v-if="tab === 'clusters'" class="tab-page">
        <div class="toolbar">
          <el-button v-if="canWrite" type="primary" @click="clusterDialog = true">{{ t('k8s.cluster.addNew') }}</el-button>
          <el-button v-if="canWrite" @click="createDialog = true">{{ t('k8s.op.createCluster') }}</el-button>
          <span class="tip">{{ t('k8s.cluster.cardsTip') }}</span>
        </div>
        <el-row :gutter="16" v-if="filteredClusters.length" class="cluster-cards">
          <el-col :xs="24" :sm="12" :md="8" v-for="c in filteredClusters" :key="c.id">
            <div class="cluster-card" @click="openClusterDetail(c)">
              <div class="cc-head">
                <b class="cc-name">{{ c.name }}</b>
                <el-tag size="small" :type="c.status === 'ok' ? 'success' : (c.status === 'error' ? 'danger' : 'info')">
                  {{ CLUSTER_STATUS_LABELS[c.status] || c.status }}
                </el-tag>
              </div>
              <div class="cc-api mono">{{ c.api_url }}</div>
              <div class="cc-stats">
                <span class="cc-link" :title="t('k8s.cluster.seePodList')" @click.stop="goResKind('Pod', c.id)"><b>{{ c.kinds?.Pod || 0 }}</b> Pod</span>
                <span class="cc-link" :title="t('k8s.cluster.seeServiceList')" @click.stop="goResKind('Service', c.id)"><b>{{ c.kinds?.Service || 0 }}</b> Service</span>
                <span class="cc-link" :title="t('k8s.cluster.seeIngressList')" @click.stop="goResKind('Ingress', c.id)"><b>{{ c.kinds?.Ingress || 0 }}</b> Ingress</span>
                <span><b>{{ c.resources }}</b> {{ t('k8s.cluster.resUnit') }}</span>
                <span :class="{ 'cc-warn': c.pending_failures }"><b>{{ c.pending_failures }}</b> {{ t('k8s.cluster.pendingUnit') }}</span>
                <span>{{ c.read_only ? t('k8s.status.readOnly') : t('k8s.status.manageable') }}</span>
              </div>
              <div class="cc-foot">
                <el-tag v-if="c.ssh_host" size="small" type="success" effect="plain">{{ t('k8s.cluster.mgmtPlaneHost', { host: c.ssh_host }) }}</el-tag>
                <el-tag v-else size="small" type="warning" effect="plain">{{ t('k8s.cluster.mgmtNotRegistered') }}</el-tag>
                <span class="tip">{{ t('k8s.cluster.detailArrow') }}</span>
              </div>
            </div>
          </el-col>
        </el-row>
        <el-empty v-else :description="t('k8s.empty.noClusters')" />

        <!-- 集群详情（概要/节点/集群操作） -->
        <el-drawer v-model="clusterDetailVisible" size="72%" :title="t('k8s.cluster.detailTitle', { name: clusterDetail?.name || '' })">
          <div v-loading="cdLoading" class="cd-wrap">
            <el-descriptions :column="2" border size="default" class="cd-sum">
              <el-descriptions-item :label="t('k8s.field.clusterName')">{{ clusterDetail?.name }}</el-descriptions-item>
              <el-descriptions-item :label="t('k8s.table.status')">
                <el-tag size="small" :type="clusterDetail?.status === 'ok' ? 'success' : 'info'">
                  {{ CLUSTER_STATUS_LABELS[clusterDetail?.status] || clusterDetail?.status }}
                </el-tag>
              </el-descriptions-item>
              <el-descriptions-item :label="t('k8s.field.k8sVersion')">{{ cdVersion || '—' }}</el-descriptions-item>
              <el-descriptions-item :label="t('k8s.field.apiUrl')"><span class="mono">{{ clusterDetail?.api_url }}</span></el-descriptions-item>
              <el-descriptions-item :label="t('k8s.field.manageMode')">
                <el-tag size="small" :type="clusterDetail?.read_only ? 'info' : 'success'">
                  {{ clusterDetail?.read_only ? t('k8s.status.readOnlyProd') : t('k8s.status.manageable') }}
                </el-tag>
              </el-descriptions-item>
              <el-descriptions-item :label="t('k8s.field.mgmtSsh')">
                <span v-if="clusterDetail?.ssh_host" class="mono">{{ clusterDetail?.ssh_user }}@{{ clusterDetail?.ssh_host }}</span>
                <el-tag v-else size="small" type="warning">{{ t('k8s.status.notRegisteredForOps') }}</el-tag>
              </el-descriptions-item>
              <el-descriptions-item :label="t('k8s.field.nodesResources')">{{ t('k8s.cluster.nodesRes', { nodes: cdNodes.length, res: clusterDetail?.resources }) }}</el-descriptions-item>
              <el-descriptions-item :label="t('k8s.field.lastSync')">{{ clusterDetail?.synced_at ? fmtTime(clusterDetail.synced_at).slice(0, 16) : '—' }}</el-descriptions-item>
              <el-descriptions-item :label="t('k8s.field.clusterCerts')" :span="2">
                <template v-if="cdCerts.length">
                  <el-tag v-for="c in cdCerts" :key="c.name" size="small" effect="plain" style="margin-right: 8px">
                    {{ c.name }} · {{ c.days_left < 0 ? t('k8s.cluster.expiredDaysShort', { n: -c.days_left }) : t('k8s.cluster.leftDaysShort', { n: c.days_left }) }}
                  </el-tag>
                </template>
                <span v-else class="tip">—</span>
              </el-descriptions-item>
            </el-descriptions>

            <div class="cd-title">{{ t('k8s.cluster.nodesTitle', { n: cdNodes.length }) }}</div>
            <div class="cd-node-bar">
              <el-button size="small" type="primary" plain
                         :loading="cdSyncing" @click="cdSync">{{ t('k8s.cluster.syncBtn') }}</el-button>
              <span class="tip">{{ t('k8s.cluster.watermarkTip') }}</span>
            </div>
            <el-table :data="cdNodes" size="small" border
                      :empty-text="cdSyncing ? t('k8s.cluster.syncing') : t('k8s.empty.noNodes')">
              <el-table-column prop="name" :label="t('k8s.word.node')" min-width="150">
                <template #default="{ row }">
                  <span>{{ row.name }}</span>
                  <el-link v-if="!row.machine" type="primary" :underline="false" class="cd-reg-link"
                           @click="router.push('/machines')">{{ t('k8s.cluster.registerMachine') }}</el-link>
                </template>
              </el-table-column>
              <el-table-column :label="t('k8s.table.status')" width="100">
                <template #default="{ row }">
                  <el-tag size="small" :type="row.status === 'Ready' ? 'success' : (row.status === 'NotReady' ? 'danger' : 'info')">
                    {{ row.status || '—' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="IP" width="125">
                <template #default="{ row }"><span class="mono">{{ row.ip || '—' }}</span></template>
              </el-table-column>
              <el-table-column :label="t('k8s.table.version')" width="110">
                <template #default="{ row }">{{ row.kubelet || '—' }}</template>
              </el-table-column>
              <el-table-column label="CPU" width="82">
                <template #default="{ row }">{{ row.machine?.cpu_pct != null ? row.machine.cpu_pct + '%' : '—' }}</template>
              </el-table-column>
              <el-table-column :label="t('k8s.word.mem')" width="82">
                <template #default="{ row }">{{ row.machine?.mem_pct != null ? row.machine.mem_pct + '%' : '—' }}</template>
              </el-table-column>
              <el-table-column :label="t('k8s.word.disk')" width="82">
                <template #default="{ row }">{{ row.machine?.disk_pct != null ? row.machine.disk_pct + '%' : '—' }}</template>
              </el-table-column>
              <el-table-column :label="t('k8s.table.actions')" width="240">
                <template #default="{ row }">
                  <el-button size="small" text type="primary" @click="detail = row; drawerOpen = true">{{ t('k8s.actions.detail') }}</el-button>
                  <el-button size="small" text @click="viewYaml(row)">yaml</el-button>
                  <template v-if="canWrite && canDanger">
                    <el-button size="small" text type="warning" @click="askOp(row, 'cordon')">{{ t('k8s.op.cordon') }}</el-button>
                    <el-button size="small" text type="danger" @click="askOp(row, 'drain')">{{ t('k8s.op.drain') }}</el-button>
                  </template>
                </template>
              </el-table-column>
            </el-table>

            <div v-if="canWrite" class="cd-title">{{ t('k8s.cluster.opsTitle') }}</div>
            <!-- 按钮与后端角色逐一对齐：备份是 admin|operator，
                 addnode/restore/upgrade/wipe/cfg 仅 admin，避免 operator 点击后才 403。 -->
            <div v-if="canWrite" class="cd-ops">
              <el-button v-if="canDanger" @click="cdAction('addnode')">{{ t('k8s.op.addNode') }}</el-button>
              <el-button @click="cdAction('backup')">{{ t('k8s.op.backup') }}</el-button>
              <el-button v-if="canDanger" @click="cdAction('restore')">{{ t('k8s.op.restoreBackup') }}</el-button>
              <el-button v-if="canDanger" @click="cdAction('upgrade')">{{ t('k8s.op.upgrade') }}</el-button>
              <el-button v-if="canDanger" @click="cdAction('cfg')">{{ t('k8s.op.mgmtCfg') }}</el-button>
              <el-button v-if="canDanger" type="danger" plain @click="cdAction('wipe')">{{ t('k8s.op.wipe') }}</el-button>
            </div>
            <div class="tip" style="margin-top: 8px">{{ t('k8s.cluster.opsTip') }}</div>
          </div>
        </el-drawer>

        <!-- 接入新集群 -->
        <el-dialog v-model="clusterDialog" :title="t('k8s.cluster.addTitle')" width="620px">
          <el-form :model="clusterForm" label-width="110px">
            <el-form-item :label="t('k8s.field.clusterName')" required>
              <el-input v-model="clusterForm.name" :placeholder="t('k8s.ph.clusterName')" />
            </el-form-item>
            <el-form-item :label="t('k8s.field.apiUrl')" required>
              <el-input v-model="clusterForm.api_url" placeholder="https://<master>:6443" />
            </el-form-item>
            <el-form-item label="SA Token" required>
              <el-input v-model="clusterForm.token" type="textarea" :rows="3" show-password
                        :placeholder="t('k8s.ph.saToken')" />
            </el-form-item>
            <el-form-item :label="t('k8s.field.tlsVerify')">
              <el-switch v-model="clusterForm.verify_tls" :active-text="t('k8s.sw.verify')" :inactive-text="t('k8s.sw.skipSelfSigned')" />
            </el-form-item>
            <el-form-item :label="t('k8s.field.nsScope')">
              <el-input v-model="clusterForm.ns_scope" :placeholder="t('k8s.ph.nsScope')" />
            </el-form-item>
            <el-form-item :label="t('k8s.field.mgmtSsh')">
              <el-input v-model="clusterForm.ssh_host" :placeholder="t('k8s.ph.mgmtSsh')" />
            </el-form-item>
            <el-form-item :label="t('k8s.field.sshUser')">
              <el-input v-model="clusterForm.ssh_user" placeholder="root" />
            </el-form-item>
            <el-form-item :label="t('k8s.field.manageMode')">
              <el-switch v-model="clusterForm.read_only" :active-text="t('k8s.sw.readOnlyRec')" :inactive-text="t('k8s.status.manageable')" />
            </el-form-item>
            <div class="tip">
              {{ t('k8s.cluster.addTip') }}
            </div>
          </el-form>
          <template #footer>
            <el-button @click="clusterDialog = false">{{ t('k8s.actions.cancel') }}</el-button>
            <el-button type="primary" @click="saveCluster">{{ t('k8s.actions.connect') }}</el-button>
          </template>
        </el-dialog>

        <!-- 集群管理面配置（严格保存制：仅「保存」按钮 PATCH；@submit.prevent 堵回车隐式提交） -->
        <el-dialog v-model="clusterCfgDialog" :title="t('k8s.cluster.cfgTitle', { name: clusterCfgRow?.name })" width="540px">
          <el-form label-width="130px" @submit.prevent>
            <el-alert type="info" :closable="false" show-icon style="margin-bottom: 12px"
                      :title="t('k8s.cluster.cfgAlert')" />
            <el-form-item :label="t('k8s.field.mgmtSsh')">
              <el-input v-model="clusterCfg.ssh_host" :placeholder="t('k8s.ph.mgmtSsh2')" />
            </el-form-item>
            <el-form-item :label="t('k8s.field.sshUser')">
              <el-input v-model="clusterCfg.ssh_user" placeholder="root" />
            </el-form-item>
            <el-form-item :label="t('k8s.field.manageMode')">
              <el-switch v-model="clusterCfg.read_only" :active-text="t('k8s.sw.readOnlyRec')" :inactive-text="t('k8s.status.manageable')" />
            </el-form-item>
            <el-form-item :label="t('k8s.field.updateSaToken')">
              <el-input v-model="clusterCfg.token" type="textarea" :rows="2" :placeholder="t('k8s.ph.tokenKeep')" show-password />
            </el-form-item>
            <div class="tip">
              {{ t('k8s.cluster.cfgTip') }}
            </div>
          </el-form>
          <template #footer>
            <el-button @click="clusterCfgDialog = false">{{ t('k8s.actions.cancel') }}</el-button>
            <el-button type="primary" @click="saveClusterCfg">{{ t('k8s.actions.save') }}</el-button>
          </template>
        </el-dialog>

        <!-- 添加节点 -->
        <el-dialog v-model="nodeDialog" :title="t('k8s.node.addTitle')" width="520px" @open="loadMachineOptions">
          <el-form :model="nodeForm" label-width="110px">
            <el-form-item :label="t('k8s.field.pickFromMachines')">
              <el-select v-model="nodeForm.machine_id" clearable :placeholder="t('k8s.ph.machineSelect')"
                         style="width: 100%" @change="pickMachine(nodeForm)">
                <el-option v-for="m in machineOptions" :key="m.id"
                           :label="`${m.host_name || m.ip} · ${m.ip} · ${m.role}`" :value="m.id" />
              </el-select>
            </el-form-item>
            <el-form-item :label="t('k8s.field.targetIp')"><el-input v-model="nodeForm.ip" placeholder="10.x.x.x" /></el-form-item>
            <el-form-item :label="t('k8s.field.sshUser')"><el-input v-model="nodeForm.ssh_user" /></el-form-item>
            <el-form-item :label="t('k8s.field.sshPassword')"><el-input v-model="nodeForm.ssh_password" type="password" show-password :placeholder="t('k8s.ph.sshPasswordBlank')" /></el-form-item>
            <el-form-item :label="t('k8s.field.cluster')">
              <el-select v-model="nodeForm.cluster_name" clearable :placeholder="t('k8s.ph.defaultFirstCluster')" style="width: 100%">
                <el-option v-for="c in clusters" :key="c.id" :label="c.name" :value="c.name" />
              </el-select>
            </el-form-item>
            <div class="tip">{{ t('k8s.node.addTip') }}</div>
          </el-form>
          <template #footer>
            <el-button @click="nodeDialog = false">{{ t('k8s.actions.cancel') }}</el-button>
            <el-button type="primary" :loading="mgmtSubmitting" @click="submitNode">{{ t('k8s.actions.submit') }}</el-button>
          </template>
        </el-dialog>

        <!-- 创建集群（4 步向导） -->
        <el-dialog v-model="createDialog" :title="t('k8s.create.title')" width="720px"
                   @open="loadMachineOptions" :close-on-click-modal="false">
          <el-steps :active="createStep" align-center finish-status="success" class="wz-steps">
            <el-step :title="t('k8s.create.stepInfo')" /><el-step :title="t('k8s.create.stepNode')" />
            <el-step :title="t('k8s.create.stepAdv')" /><el-step :title="t('k8s.create.stepPrecheck')" />
          </el-steps>

          <el-form v-if="createStep === 0" :model="createForm" label-width="120px" class="wz-form">
            <el-form-item :label="t('k8s.field.clusterName')" required>
              <el-input v-model="createForm.cluster_name" :placeholder="t('k8s.ph.createClusterName')" />
            </el-form-item>
            <el-form-item :label="t('k8s.field.kubeVersion')" required>
              <el-input v-model="createForm.k8s_version" :placeholder="t('k8s.ph.k8sVersion')" />
            </el-form-item>
            <el-form-item :label="t('k8s.field.podCidr')">
              <el-input v-model="createForm.pod_cidr" :placeholder="t('k8s.ph.podCidr')" />
            </el-form-item>
            <el-form-item :label="t('k8s.field.svcCidr')">
              <el-input v-model="createForm.svc_cidr" :placeholder="t('k8s.ph.svcCidr')" />
            </el-form-item>
          </el-form>

          <el-form v-if="createStep === 1" :model="createForm" label-width="120px" class="wz-form">
            <el-form-item :label="t('k8s.field.pickFromMachines')">
              <el-select v-model="createForm.machine_id" clearable :placeholder="t('k8s.ph.machineSelectRec')"
                         style="width: 100%" @change="pickMachine(createForm)">
                <el-option v-for="m in machineOptions" :key="m.id"
                           :label="`${m.host_name || m.ip} · ${m.ip} · ${m.role}`" :value="m.id" />
              </el-select>
            </el-form-item>
            <el-form-item :label="t('k8s.field.masterIp')" required>
              <el-input v-model="createForm.ip" :placeholder="t('k8s.ph.masterIp')" />
            </el-form-item>
            <el-form-item :label="t('k8s.field.sshUser')"><el-input v-model="createForm.ssh_user" /></el-form-item>
            <el-form-item :label="t('k8s.field.sshPassword')">
              <el-input v-model="createForm.ssh_password" type="password" show-password
                        :placeholder="t('k8s.ph.sshPasswordRec')" />
            </el-form-item>
            <div class="tip">{{ t('k8s.create.multiNodeTip') }}</div>
          </el-form>

          <el-form v-if="createStep === 2" :model="createForm" label-width="120px" class="wz-form">
            <el-form-item :label="t('k8s.field.cni')">
              <el-radio-group v-model="createForm.cni">
                <el-radio value="flannel">{{ t('k8s.create.cniFlannel') }}</el-radio>
                <el-radio value="calico">{{ t('k8s.create.cniCalico') }}</el-radio>
              </el-radio-group>
            </el-form-item>
            <el-form-item :label="t('k8s.field.runtime')">
              <el-radio-group :model-value="'containerd'" disabled>
                <el-radio value="containerd">{{ t('k8s.create.runtimeContainerd') }}</el-radio>
              </el-radio-group>
            </el-form-item>
            <el-form-item :label="t('k8s.field.imageRegistry')">
              <div class="tip">{{ t('k8s.create.registryTip') }}</div>
            </el-form-item>
            <el-form-item :label="t('k8s.field.dataDirs')">
              <div class="tip">{{ t('k8s.create.dataDirsTip') }}</div>
            </el-form-item>
          </el-form>

          <div v-if="createStep === 3" class="wz-form">
            <el-descriptions :column="1" border size="small">
              <el-descriptions-item :label="t('k8s.field.clusterName')">{{ createForm.cluster_name }}</el-descriptions-item>
              <el-descriptions-item :label="t('k8s.field.versionRuntime')">v{{ createForm.k8s_version }} · containerd</el-descriptions-item>
              <el-descriptions-item :label="t('k8s.field.cniCidr')">{{ createForm.cni }} · {{ createForm.pod_cidr }} · {{ createForm.svc_cidr }}</el-descriptions-item>
              <el-descriptions-item :label="t('k8s.field.masterNode')">{{ t('k8s.create.masterNodeVal', { ip: createForm.ip, user: createForm.ssh_user }) }}</el-descriptions-item>
            </el-descriptions>
            <div class="wz-precheck">
              <el-button :loading="createPrechecking" type="primary" plain @click="runCreatePrecheck">{{ t('k8s.create.fullPrecheck') }}</el-button>
              <el-tag v-if="createPrecheck" :type="createPrecheck.ok ? 'success' : 'danger'" size="small" style="margin-left: 10px">
                {{ createPrecheck.ok ? t('k8s.create.precheckPass') : t('k8s.create.precheckFail') }}
              </el-tag>
              <span v-if="createPrecheck?.summary?.fail !== undefined" class="tip" style="margin-left: 8px">
                {{ t('k8s.create.precheckSummary', { pass: createPrecheck.summary.pass, warn: createPrecheck.summary.warn, fail: createPrecheck.summary.fail }) }}
              </span>
            </div>
            <div v-if="createPrecheck?.items?.length" class="precheck-list">
              <div v-for="it in createPrecheck.items" :key="it.key" class="precheck-row">
                <el-tag size="small" :type="{PASS:'success', WARN:'warning', FAIL:'danger'}[it.result]">{{ it.result }}</el-tag>
                <b class="pk">{{ it.key }}</b>
                <span class="tip">{{ it.detail }}</span>
              </div>
              <div class="tip">{{ createPrecheck.note }}</div>
            </div>
            <div class="tip" style="margin-top: 8px">
              {{ t('k8s.create.submitTip', { cni: createForm.cni }) }}
            </div>
          </div>

          <template #footer>
            <el-button @click="createDialog = false">{{ t('k8s.actions.cancel') }}</el-button>
            <el-button v-if="createStep > 0" @click="createPrev">{{ t('k8s.actions.prevStep') }}</el-button>
            <el-button v-if="createStep < 3" type="primary" @click="createNext">{{ t('k8s.actions.nextStep') }}</el-button>
            <el-button v-else type="primary" :loading="mgmtSubmitting"
                       :disabled="!createPrecheck?.ok" @click="submitCreate">{{ t('k8s.actions.submitDeploy') }}</el-button>
          </template>
        </el-dialog>

        <!-- 备份管理（备份集清单 + 发起备份 + 一键恢复） -->
        <el-dialog v-model="backupDialog" :title="t('k8s.backup.title')" width="640px" @open="loadBackupSets">
          <el-form :model="backupForm" label-width="90px">
            <el-form-item :label="t('k8s.field.cluster')">
              <el-select v-model="backupForm.cluster_name" clearable :placeholder="t('k8s.ph.defaultFirstCluster')" style="width: 280px" @change="loadBackupSets">
                <el-option v-for="c in clusters" :key="c.id" :label="c.name" :value="c.name" />
              </el-select>
              <el-button type="primary" style="margin-left: 12px" :loading="mgmtSubmitting" @click="submitBackup">{{ t('k8s.actions.startBackup') }}</el-button>
              <el-button @click="loadBackupSets">{{ t('k8s.actions.refreshList') }}</el-button>
            </el-form-item>
          </el-form>
          <el-table :data="backupSets" v-loading="backupSetsLoading" border size="default"
                    :empty-text="t('k8s.empty.noBackupSets')">
            <el-table-column prop="name" :label="t('k8s.table.backupSet')" min-width="180">
              <template #default="{ row }"><span class="mono">k8s-backup-{{ row.name }}</span></template>
            </el-table-column>
            <el-table-column prop="created_at" :label="t('k8s.table.time')" width="170" />
            <el-table-column prop="size" :label="t('k8s.table.size')" width="90" />
            <el-table-column prop="entries" :label="t('k8s.table.entries')" width="100" />
            <el-table-column :label="t('k8s.table.actions')" width="110">
              <template #default="{ row }">
                <el-button size="small" type="warning" plain @click="askRestore(row)">{{ t('k8s.op.restore') }}</el-button>
              </template>
            </el-table-column>
          </el-table>
          <div class="tip" style="margin-top: 10px">{{ t('k8s.backup.contentTip') }}</div>
          <div class="tip" style="margin-top: 8px">
            {{ t('k8s.backup.mgmtSshLabel') }}
            <template v-if="selCluster && selCluster.ssh_host">
              <el-tag size="small" type="success">{{ selCluster.ssh_host }}</el-tag>{{ t('k8s.backup.canBackup') }}
            </template>
            <template v-else>
              <el-tag size="small" type="warning">{{ t('k8s.status.notRegistered') }}</el-tag>
              {{ t('k8s.backup.mgmtSshHint') }}
            </template>
          </div>
        </el-dialog>

        <!-- 恢复确认（高危门） -->
        <el-dialog v-model="restoreDialog" :title="t('k8s.restore.title')" width="480px">
          <div class="warn-box">
            <b>{{ t('k8s.warn.redLabel') }}</b>{{ t('k8s.restore.warn1', { set: restoreForm.backup_set }) }}<br />
            {{ t('k8s.restore.warn2') }}
          </div>
          <el-form label-width="110px">
            <el-form-item :label="t('k8s.field.confirmClusterName')">
              <el-input v-model="restoreForm.confirm_name" :placeholder="restoreForm.cluster_name || t('k8s.field.clusterName')" />
            </el-form-item>
          </el-form>
          <template #footer>
            <el-button @click="restoreDialog = false">{{ t('k8s.actions.cancel') }}</el-button>
            <el-button type="danger" :disabled="restoreForm.confirm_name !== restoreForm.cluster_name || !restoreForm.cluster_name"
                       @click="submitRestore">{{ t('k8s.actions.confirmRestore') }}</el-button>
          </template>
        </el-dialog>

        <!-- 单控制面独立实验节点升级（受限；生产/多节点未开放） -->
        <el-dialog v-model="upgradeDialog" :title="t('k8s.upgrade.title')" width="560px">
          <el-form :model="upgradeForm" label-width="110px">
            <el-form-item :label="t('k8s.field.cluster')">
              <el-select v-model="upgradeForm.cluster_name" clearable :placeholder="t('k8s.ph.defaultFirstCluster')" style="width: 100%" @change="openUpgrade">
                <el-option v-for="c in clusters" :key="c.id" :label="c.name" :value="c.name" />
              </el-select>
            </el-form-item>
            <el-form-item :label="t('k8s.field.currentVersion')">
              <span class="mono">{{ upgradeInfo.current || t('k8s.upgrade.detecting') }}</span>
              <span class="tip" v-if="upgradeInfo.upgrade_path?.length">{{ t('k8s.upgrade.path', { list: upgradeInfo.upgrade_path.join(' / ') }) }}</span>
            </el-form-item>
            <el-form-item :label="t('k8s.field.targetVersion')">
              <el-input v-model="upgradeForm.target_version" :placeholder="t('k8s.ph.targetVersion')" style="width: 200px" />
              <el-button size="small" style="margin-left: 8px" :loading="prechecking" @click="runPrecheck">{{ t('k8s.actions.upgradePrecheck') }}</el-button>
            </el-form-item>
            <el-form-item v-if="precheck" :label="t('k8s.field.precheckResult')">
              <el-tag :type="precheck.passed ? 'success' : 'danger'" size="small">{{ precheck.passed ? t('k8s.upgrade.pass') : t('k8s.upgrade.fail') }}</el-tag>
              <div v-for="(f, i) in precheck.findings" :key="i" class="warn-line">
                <template v-if="f.note">
                  <span :style="{ color: f.instances?.length ? '#f56c6c' : '' }">· {{ f.note }}</span>
                  <div v-for="(inst, j) in (f.instances || [])" :key="j" class="mono tip">　{{ inst }}</div>
                </template>
              </div>
            </el-form-item>
            <div class="warn-box">
              {{ t('k8s.upgrade.warn1') }}<br />
              {{ t('k8s.upgrade.warn2') }}
            </div>
            <div class="tip" style="margin-top: 6px">
              {{ t('k8s.backup.mgmtSshLabel') }}
              <template v-if="selUpCluster && selUpCluster.ssh_host">
                <el-tag size="small" type="success">{{ selUpCluster.ssh_host }}</el-tag>
              </template>
              <template v-else>
                <el-tag size="small" type="warning">{{ t('k8s.status.notRegistered') }}</el-tag>
                {{ t('k8s.upgrade.mgmtSshHint') }}
              </template>
            </div>
            <el-form-item :label="t('k8s.field.confirmClusterName')">
              <el-input v-model="upgradeForm.confirm_name" :placeholder="upgradeForm.cluster_name || t('k8s.field.clusterName')" />
            </el-form-item>
          </el-form>
          <template #footer>
            <el-button @click="upgradeDialog = false">{{ t('k8s.actions.cancel') }}</el-button>
            <el-button type="warning" :loading="mgmtSubmitting"
                       :disabled="!upgradeForm.cluster_name || upgradeForm.confirm_name !== upgradeForm.cluster_name || (precheck && !precheck.passed)"
                       @click="submitUpgrade">{{ t('k8s.actions.confirmUpgrade') }}</el-button>
          </template>
        </el-dialog>

        <!-- 清空集群（高危） -->
        <el-dialog v-model="wipeDialog" :title="t('k8s.wipe.title')" width="520px">
          <div class="warn-box">
            <b>{{ t('k8s.wipe.warnBold') }}</b><br />
            {{ t('k8s.wipe.warnA') }}<b>{{ t('k8s.wipe.allNodes') }}</b>{{ t('k8s.wipe.warnB') }}<br />
            {{ t('k8s.wipe.warnC') }}
          </div>
          <el-form :model="wipeForm" label-width="110px">
            <el-form-item :label="t('k8s.field.cluster')">
              <el-select v-model="wipeForm.cluster_name" clearable :placeholder="t('k8s.ph.defaultFirstCluster')" style="width: 100%">
                <el-option v-for="c in clusters" :key="c.id" :label="c.name" :value="c.name" />
              </el-select>
            </el-form-item>
            <el-form-item :label="t('k8s.field.confirmClusterName')">
              <el-input v-model="wipeForm.confirm_name" :placeholder="wipeForm.cluster_name || t('k8s.field.clusterName')" />
            </el-form-item>
          </el-form>
          <template #footer>
            <el-button @click="wipeDialog = false">{{ t('k8s.actions.cancel') }}</el-button>
            <el-button type="danger" :loading="mgmtSubmitting"
                       :disabled="wipeForm.confirm_name !== wipeForm.cluster_name || !wipeForm.cluster_name"
                       @click="submitWipe">{{ t('k8s.actions.confirmWipe') }}</el-button>
          </template>
        </el-dialog>
      </section>

      <!-- ===== 资源清单 ===== -->
      <section v-if="tab === 'resources'" class="tab-page">
        <div class="toolbar">
          <el-select v-model="kind" style="width: 130px" @change="loadResources">
            <el-option v-for="k in KINDS" :key="k" :label="k" :value="k" />
          </el-select>
          <el-select v-model="namespace" :placeholder="t('k8s.ph.namespaceSelect')" clearable filterable style="width: 170px" @change="loadResources">
            <el-option v-for="n in nsOptions" :key="n" :label="n" :value="n" />
          </el-select>
          <el-input v-model="keyword" :placeholder="t('k8s.ph.searchName')" clearable style="width: 160px" @change="loadResources" />
          <el-switch v-model="includeDestroyed" :active-text="t('k8s.filter.includeDestroyed')" @change="loadResources" />
          <el-select v-model="resStatus" :placeholder="t('k8s.ph.statusFilter')" clearable style="width: 150px" @change="loadResources">
            <el-option v-for="st in statusOptions" :key="st" :label="st" :value="st" />
          </el-select>
          <el-button @click="loadResources">{{ t('k8s.actions.query') }}</el-button>
          <template v-if="canWrite">
            <el-button type="warning" plain @click="askBatch('restart')">{{ t('k8s.actions.batchRestart') }}</el-button>
            <el-button type="danger" plain @click="askBatch('delete')">{{ t('k8s.actions.batchDelete') }}</el-button>
          </template>
        </div>
        <el-table :data="resources" v-loading="resLoading" border size="small" v-table-resize
                  @selection-change="onSelectionChange"
                  :empty-text="t('k8s.empty.noResources', { kind })">
          <el-table-column v-if="canWrite" type="selection" width="42" />
          <el-table-column :label="t('k8s.table.cluster')" width="110">
            <template #default="{ row }">{{ row.cluster || clusterNameOf(row.cluster_id) || '—' }}</template>
          </el-table-column>
          <el-table-column prop="namespace" :label="t('k8s.word.namespace')" width="130" />
          <el-table-column prop="name" :label="t('k8s.table.name')" min-width="220" show-overflow-tooltip />
          <el-table-column :label="t('k8s.table.status')" width="120">
            <template #default="{ row }">
              <el-tag :type="row.is_active ? (row.ready === false ? 'warning' : 'success') : 'info'" size="small">
                {{ row.status || (row.is_active ? 'Active' : 'Destroyed') }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column :label="t('k8s.table.restarts')" width="70">
            <template #default="{ row }">{{ row.restarts ?? '—' }}</template>
          </el-table-column>
          <el-table-column label="IP" width="150">
            <template #default="{ row }">
              <span class="mono">{{ row.ip || '—' }}</span>
              <!-- 主机名册互链：Node 按 IP 去「机器管理」找同机（SSH 凭证/水位在机器库） -->
              <el-link v-if="row.kind === 'Node' && row.ip" type="primary" :underline="false"
                       @click="router.push({ path: '/machines', query: { keyword: row.ip } })">
                <el-icon><Connection /></el-icon>
              </el-link>
            </template>
          </el-table-column>
          <el-table-column v-if="kind === 'Node'" label="kubelet" width="120">
            <template #default="{ row }">{{ row.kubelet || '—' }}</template>
          </el-table-column>
          <el-table-column :label="t('k8s.table.nodeOrIngress')" min-width="140">
            <template #default="{ row }">{{ row.node || (row.hosts || []).join(', ') || row.cluster_ip || '—' }}</template>
          </el-table-column>
          <el-table-column :label="t('k8s.table.created')" width="150">
            <template #default="{ row }"><span class="tip">{{ row.created ? fmtTime(row.created).slice(0, 16) : '—' }}</span></template>
          </el-table-column>
          <el-table-column :label="t('k8s.table.relations')" min-width="170">
            <template #default="{ row }">
              <template v-if="row.kind === 'Ingress' && row.chain?.length">
                <div v-for="c in row.chain" :key="c.service" class="rel-line">
                  → <b>{{ c.service }}</b> <span class="tip">→ {{ c.pods.length }} pod</span>
                </div>
              </template>
              <template v-else-if="row.kind === 'Service' && row.matched_pods?.length">
                <span class="tip">{{ t('k8s.table.matchedPods', { n: row.matched_pods.length }) }}</span>
                <span class="mono">{{ row.matched_pods.slice(0, 3).join(', ') }}{{ row.matched_pods.length > 3 ? '…' : '' }}</span>
              </template>
              <span v-else class="tip">—</span>
            </template>
          </el-table-column>
          <el-table-column :label="t('k8s.table.actions')" width="180">
            <template #default="{ row }">
              <el-button size="small" text type="primary" @click="detail = row; drawerOpen = true">{{ t('k8s.actions.detail') }}</el-button>
              <el-button size="small" text @click="viewYaml(row)">yaml</el-button>
              <el-dropdown trigger="click" @command="(cmd) => rowOp(cmd, row)">
                <el-button size="small" text type="primary">
                  {{ t('k8s.actions.more') }}<el-icon class="more-ic"><ArrowDown /></el-icon>
                </el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <template v-if="canWrite">
                      <el-dropdown-item command="edit">{{ t('k8s.actions.editForm') }}</el-dropdown-item>
                      <el-dropdown-item v-if="canTerm && row.kind === 'Pod' && row.is_active" command="term">{{ t('k8s.actions.enterContainer') }}</el-dropdown-item>
                      <el-dropdown-item v-if="canDanger && row.kind === 'Secret'" command="secret">{{ t('k8s.actions.viewAudited') }}</el-dropdown-item>
                      <el-dropdown-item v-if="['Deployment', 'StatefulSet', 'DaemonSet'].includes(row.kind)" command="restart">{{ t('k8s.op.restart') }}</el-dropdown-item>
                      <template v-if="row.kind === 'Node'">
                        <el-dropdown-item v-if="canDanger" command="cordon">{{ t('k8s.op.cordon') }}</el-dropdown-item>
                        <el-dropdown-item v-if="canDanger" command="uncordon">{{ t('k8s.op.uncordon') }}</el-dropdown-item>
                        <el-dropdown-item v-if="canDanger" command="drain" divided class="danger-item">{{ t('k8s.op.drainDanger') }}</el-dropdown-item>
                      </template>
                      <template v-if="row.kind === 'CronJob'">
                        <el-dropdown-item command="trigger">{{ t('k8s.op.trigger') }}</el-dropdown-item>
                        <el-dropdown-item command="suspend">{{ t('k8s.op.suspend') }}</el-dropdown-item>
                        <el-dropdown-item command="resume">{{ t('k8s.op.resume') }}</el-dropdown-item>
                      </template>
                      <el-dropdown-item v-if="canDanger && !['Node', 'Namespace'].includes(row.kind)" command="delete" divided class="danger-item">{{ t('k8s.op.deleteDanger') }}</el-dropdown-item>
                    </template>
                    <el-dropdown-item v-else disabled>{{ t('k8s.actions.readOnlyNoOps') }}</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </template>
          </el-table-column>
        </el-table>
      </section>

      <!-- ===== 容器日志 ===== -->
      <section v-if="tab === 'logs'" class="tab-page">
        <div class="toolbar">
          <el-select v-model="logNs" :placeholder="t('k8s.ph.namespaceSelect')" clearable filterable style="width: 170px"
                     @change="onLogNsChange">
            <el-option v-for="n in nsOptions" :key="n" :label="n" :value="n" />
          </el-select>
          <el-select v-model="logPod" :placeholder="t('k8s.ph.podSelect')" clearable filterable style="width: 230px">
            <el-option v-for="p in podOptions" :key="p" :label="p" :value="p" />
          </el-select>
          <el-input v-model="logKeyword" :placeholder="t('k8s.ph.keyword')" clearable style="width: 170px" />
          <el-button type="primary" @click="loadLogs">{{ t('k8s.actions.query') }}</el-button>
          <span class="tip">{{ t('k8s.logs.tip') }}</span>
          <span class="retention-box">
            {{ t('k8s.logs.keepPrefix') }}
            <el-input-number v-model="retention" :min="1" :max="365" size="small" style="width: 90px" />
            {{ t('k8s.logs.keepSuffix') }}
            <el-button v-if="canWrite" size="small" @click="saveRetention">{{ t('k8s.actions.saveRetention') }}</el-button>
            <!-- 与 系统设置-系统参数 同一配置（同一 env 键），避免两处改来改去互相覆盖的误解 -->
            <span class="tip">{{ t('k8s.logs.retentionSameAsSettings') }}</span>
          </span>
        </div>
        <el-table :data="logs" v-loading="logLoading" border size="small" max-height="520"
                  :empty-text="t('k8s.empty.noLogs')">
          <el-table-column :label="t('k8s.table.time')" width="150">
            <template #default="{ row }">{{ fmtTime(row.ts) }}</template>
          </el-table-column>
          <el-table-column :label="t('k8s.table.podContainer')" width="220">
            <template #default="{ row }">
              <span class="mono">{{ row.pod }}</span>
              <span class="tip"> / {{ row.container }}</span>
            </template>
          </el-table-column>
          <el-table-column :label="t('k8s.table.stream')" width="70">
            <template #default="{ row }">
              <el-tag :type="row.stream === 'stderr' ? 'danger' : 'info'" size="small">{{ row.stream }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column :label="t('k8s.table.log')" min-width="380">
            <template #default="{ row }"><span class="mono log-line">{{ row.line }}</span></template>
          </el-table-column>
        </el-table>
      </section>

      <!-- ===== 失败分析 ===== -->
      <section v-if="tab === 'failures'" class="tab-page">
        <div class="toolbar">
          <el-radio-group v-model="failStatus" @change="loadFailures">
            <el-radio-button value="pending">{{ t('k8s.fail.pending') }}</el-radio-button>
            <el-radio-button value="ignored">{{ t('k8s.fail.ignored') }}</el-radio-button>
            <el-radio-button value="confirmed">{{ t('k8s.fail.confirmed') }}</el-radio-button>
            <el-radio-button value="">{{ t('k8s.filter.all') }}</el-radio-button>
          </el-radio-group>
          <template v-if="canWrite && failStatus === 'pending'">
            <el-input v-model="batchNote" :placeholder="t('k8s.ph.batchNote')" style="width: 200px" />
            <el-button size="default" :loading="batching" :disabled="!selection.length" @click="batch('ignore')">{{ t('k8s.actions.batchIgnore') }}</el-button>
            <el-button size="default" type="primary" :loading="batching" :disabled="!selection.length" @click="batch('confirm')">{{ t('k8s.actions.batchConfirm') }}</el-button>
          </template>
          <el-button @click="loadFailures">{{ t('k8s.actions.refresh') }}</el-button>
        </div>
        <el-table :data="failures" v-loading="failLoading" border size="small"
                  @selection-change="(rows) => selection = rows"
                  :empty-text="t('k8s.empty.noFailures')">
          <el-table-column v-if="canWrite && failStatus === 'pending'" type="selection" width="42" />
          <el-table-column :label="t('k8s.table.type')" width="150">
            <template #default="{ row }">
              <el-tag :type="FAIL_TAGS[row.failure_type] || 'warning'" size="small">{{ row.failure_type }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column :label="t('k8s.table.object')" width="220">
            <template #default="{ row }">
              <span class="mono">{{ row.namespace }}/{{ row.pod }}</span>
              <span class="tip"> / {{ row.container }}</span>
            </template>
          </el-table-column>
          <el-table-column :label="t('k8s.table.diagnosis')" min-width="360">
            <template #default="{ row }">
              <div class="diag-text">{{ row.diagnosis }}</div>
            </template>
          </el-table-column>
          <el-table-column :label="t('k8s.table.status')" width="110">
            <template #default="{ row }">
              <el-tag :type="row.status === 'pending' ? 'warning' : (row.status === 'confirmed' ? 'success' : 'info')" size="small">
                {{ FAIL_STATUS_LABELS[row.status] }}
              </el-tag>
              <div v-if="row.confirmed_by" class="tip">{{ row.confirmed_by }}</div>
            </template>
          </el-table-column>
          <el-table-column label="" width="180">
            <template #default="{ row }">
              <el-button size="default" type="primary" plain @click="diagFrom(row)">{{ t('k8s.actions.deepAnalysis') }}</el-button>
              <el-button size="default" text type="primary" @click="askAgent(row)">{{ t('k8s.actions.askAgent') }}</el-button>
            </template>
          </el-table-column>
        </el-table>
      </section>


    <!-- 任务详情（步骤+实时日志 2 秒刷新） -->
    <el-drawer v-model="taskDrawerVisible" :title="taskDetail ? t('k8s.tasks.drawerTitle', { id: taskDetail.id, title: taskDetail.title }) : t('k8s.tasks.drawerTitleFallback')" size="620px" @close="closeTaskDrawer">
      <template v-if="taskDetail">
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item :label="t('k8s.table.type')">{{ taskDetail.task_type }}</el-descriptions-item>
          <el-descriptions-item :label="t('k8s.table.status')">{{ taskStatusLabel(taskDetail.status) }}</el-descriptions-item>
          <el-descriptions-item :label="t('k8s.table.submitter')">{{ taskDetail.submitted_by || '—' }}</el-descriptions-item>
          <el-descriptions-item :label="t('k8s.table.duration')">{{ taskDetail.duration_s || '—' }}s</el-descriptions-item>
          <el-descriptions-item :label="t('k8s.table.submittedAt')">{{ taskDetail.created_at ? fmtTime(taskDetail.created_at) : '' }}</el-descriptions-item>
          <el-descriptions-item :label="t('k8s.field.retries')">{{ taskDetail.retries }}</el-descriptions-item>
        </el-descriptions>
        <div class="member-title">{{ t('k8s.tasks.steps') }}</div>
        <el-timeline>
          <el-timeline-item v-for="st in taskDetail.steps" :key="st.key"
                            :type="{succeeded:'success',failed:'danger',running:'primary',pending:'info'}[st.status]"
                            :timestamp="st.status">
            {{ st.label }}<span v-if="st.note" class="tip">{{ t('k8s.tasks.stNote', { note: st.note }) }}</span>
          </el-timeline-item>
        </el-timeline>
        <div class="member-title">{{ t('k8s.tasks.execLog') }}</div>
        <pre class="log-box">{{ taskLogs.map(l => l.line).join('\n') || t('k8s.empty.noLogLines') }}</pre>
        <div v-if="taskDetail.error" class="member-title">{{ t('k8s.tasks.failReason') }}</div>
        <div v-if="taskDetail.error" class="diag-text">{{ taskDetail.error }}</div>
      </template>
    </el-drawer>

    <!-- 资源详情 -->
    <el-drawer v-model="drawerOpen" :title="detail?.name || t('k8s.actions.detail')" size="420px">
      <template v-if="detail">
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item :label="t('k8s.table.type')">{{ detail.kind }}</el-descriptions-item>
          <el-descriptions-item :label="t('k8s.word.namespace')">{{ detail.namespace || '—' }}</el-descriptions-item>
          <el-descriptions-item :label="t('k8s.table.status')">{{ detail.status || '—' }}</el-descriptions-item>
          <el-descriptions-item v-if="detail.node" :label="t('k8s.word.node')">{{ detail.node }}</el-descriptions-item>
          <el-descriptions-item v-if="detail.restarts != null" :label="t('k8s.field.restartCount')">{{ detail.restarts }}</el-descriptions-item>
          <el-descriptions-item v-if="detail.hosts?.length" :label="t('k8s.field.hosts')">{{ detail.hosts.join(', ') }}</el-descriptions-item>
          <el-descriptions-item v-if="detail.cluster_ip" label="ClusterIP">{{ detail.cluster_ip }}</el-descriptions-item>
          <el-descriptions-item :label="t('k8s.field.createdAt')">{{ detail.observed_at ? fmtTime(detail.observed_at) : '' }}</el-descriptions-item>
        </el-descriptions>
        <!-- 关联链（Ingress→Service→Pod / Service→Pod） -->
        <template v-if="detail.chain?.length || detail.matched_pods?.length">
          <div class="member-title">{{ t('k8s.detail.relChain') }}</div>
          <div v-if="detail.chain?.length" class="rel-chain">
            <div v-for="c in detail.chain" :key="c.service" class="rel-node">
              <b>{{ detail.name }}</b>{{ t('k8s.detail.ingressSuffix') }}
              <div class="rel-arrow">→ Service <b>{{ c.service }}</b></div>
              <div class="rel-arrow2">
                {{ t('k8s.detail.toPod') }}
                <el-tag v-for="p in c.pods" :key="p" size="small" class="diff-chip"
                        @click="goLogs(detail.namespace, p)">{{ p }}</el-tag>
                <span v-if="!c.pods.length" class="tip">{{ t('k8s.detail.noMatchedPod') }}</span>
              </div>
            </div>
          </div>
          <div v-else-if="detail.matched_pods?.length" class="rel-chain">
            <div class="rel-node">
              <b>{{ detail.name }}</b>{{ t('k8s.detail.serviceSuffix') }}
              <div class="rel-arrow2">
                {{ t('k8s.detail.toPod') }}
                <el-tag v-for="p in detail.matched_pods" :key="p" size="small" class="diff-chip"
                        @click="goLogs(detail.namespace, p)">{{ p }}</el-tag>
              </div>
            </div>
          </div>
        </template>
        <div class="member-title">{{ t('k8s.detail.labels') }}</div>
        <div><el-tag v-for="(v, k) in (detail.labels || {})" :key="k" size="small" class="diff-chip">{{ k }}={{ v }}</el-tag></div>
        <template v-if="detail.containers?.length">
          <div class="member-title">{{ t('k8s.detail.containers') }}</div>
          <div v-for="c in detail.containers" :key="c.name" class="cont-row">
            <b>{{ c.name }}</b> · {{ c.ready ? t('k8s.detail.ready') : t('k8s.detail.notReady') }} · {{ t('k8s.table.restarts') }} {{ c.restarts }}
            <span v-if="c.last_reason" class="tip">{{ t('k8s.detail.lastExit', { reason: c.last_reason, code: c.exit_code }) }}</span>
          </div>
        </template>
      </template>
    </el-drawer>

    <!-- yaml 查看/编辑 -->
    <el-drawer v-model="yamlVisible" :title="`${yamlRow?.kind}/${yamlRow?.name} · yaml`" size="560px">
      <div v-if="yamlRow" class="yaml-wrap">
        <el-alert v-if="yamlError" type="error" :closable="false" show-icon :title="yamlError" />
        <template v-else>
          <div class="tip" v-if="!yamlEdit">{{ t('k8s.yaml.viewTip') }}</div>
          <div class="tip" v-else>{{ t('k8s.yaml.editTip') }}</div>
          <el-input v-model="yamlText" type="textarea" :rows="20" :readonly="!yamlEdit" class="yaml-text mono" />
        </template>
        <div class="yaml-foot">
          <template v-if="!yamlError">
            <template v-if="yamlEdit">
              <el-input v-model="opConfirmName" :placeholder="t('k8s.ph.confirmResourceName')" style="width: 220px" />
              <el-button type="primary" :disabled="opConfirmName !== yamlRow?.name" @click="saveYaml">{{ t('k8s.actions.saveApply') }}</el-button>
            </template>
            <el-button v-else type="warning" plain @click="yamlEdit = true; opConfirmName = ''">{{ t('k8s.actions.edit') }}</el-button>
          </template>
          <el-button @click="yamlVisible = false">{{ t('k8s.actions.close') }}</el-button>
        </div>
      </div>
    </el-drawer>

    <!-- 资源操作确认（高危门） -->
    <el-dialog v-model="opDialog" :title="t('k8s.op.title', { op: OP_LABELS[opAction], name: opRow?.name })" width="460px">
      <div class="warn-box" v-if="opAction === 'cordon' || opAction === 'uncordon'">
        {{ opAction === 'cordon' ? t('k8s.opwarn.cordon') : t('k8s.opwarn.uncordon') }}
      </div>
      <div class="warn-box" v-else-if="opAction === 'drain'">
        <b>{{ t('k8s.opwarn.drainBold') }}</b>{{ t('k8s.opwarn.drainRest') }}
      </div>
      <div class="warn-box" v-else-if="opAction === 'restart'">
        {{ t('k8s.opwarn.restart', { kind: opRow?.kind, name: opRow?.name }) }}
      </div>
      <div class="warn-box" v-else-if="['trigger', 'suspend', 'resume'].includes(opAction)">
        {{ opAction === 'trigger' ? t('k8s.opwarn.trigger', { name: opRow?.name })
          : opAction === 'suspend' ? t('k8s.opwarn.suspend', { name: opRow?.name })
          : t('k8s.opwarn.resume', { name: opRow?.name }) }}
      </div>
      <div class="warn-box" v-else>
        <b>{{ t('k8s.warn.redLabel') }}</b>{{ t('k8s.opwarn.delete', { kind: opRow?.kind, name: opRow?.name, nsPart: opRow?.namespace ? t('k8s.opwarn.inNs', { ns: opRow.namespace }) : '' }) }}
      </div>
      <el-form label-width="110px">
        <el-form-item :label="t('k8s.field.confirmResourceName')">
          <el-input v-model="opConfirmName" :placeholder="opRow?.name" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="opDialog = false">{{ t('k8s.actions.cancel') }}</el-button>
        <el-button :type="['delete', 'evict', 'drain'].includes(opAction) ? 'danger' : 'warning'"
                   :disabled="opConfirmName !== opRow?.name" @click="doOp">
          {{ t('k8s.op.confirmLabel', { op: OP_LABELS[opAction] }) }}
        </el-button>
      </template>
    </el-dialog>

    <!-- 批量操作确认（数量核对门） -->
    <el-dialog v-model="batchDialog" :title="t('k8s.batch.title', { action: batchActionWord(), n: batchSelection.length })" width="480px">
      <div class="warn-box" v-if="batchAction === 'delete'">
        <b>{{ t('k8s.warn.redLabel') }}</b>{{ t('k8s.batch.deleteWarn', { n: batchSelection.length }) }}
      </div>
      <div class="warn-box" v-else>
        {{ t('k8s.batch.restartWarn', { n: batchSelection.length }) }}
      </div>
      <el-table :data="batchSelection.slice(0, 6)" size="small" border>
        <el-table-column prop="kind" :label="t('k8s.table.type')" width="110" />
        <el-table-column prop="name" :label="t('k8s.table.name')" min-width="200" show-overflow-tooltip />
        <el-table-column prop="namespace" label="NS" width="110" />
      </el-table>
      <div v-if="batchSelection.length > 6" class="tip">{{ t('k8s.batch.moreCount', { n: batchSelection.length }) }}</div>
      <el-form label-width="130px" style="margin-top: 12px">
        <el-form-item :label="t('k8s.batch.confirmCountLabel', { n: batchSelection.length })">
          <el-input-number v-model="batchConfirmCount" :min="0" :max="999" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="batchDialog = false">{{ t('k8s.actions.cancel') }}</el-button>
        <el-button :type="batchAction === 'delete' ? 'danger' : 'warning'"
                   :disabled="batchConfirmCount !== batchSelection.length" @click="submitBatch">
          {{ t('k8s.actions.confirmExecute') }}
        </el-button>
      </template>
    </el-dialog>

    <!-- 图形化编辑（表单字段 → 服务端改 yaml apply） -->
    <el-dialog v-model="formEditVisible" :title="t('k8s.formEdit.title', { kind: formEditRow?.kind, name: formEditRow?.name })" width="600px">
      <el-form label-width="110px" class="form-edit">
        <el-form-item :label="t('k8s.field.replicas')" v-if="['Deployment', 'StatefulSet'].includes(formEditRow?.kind)">
          <el-input-number v-model="formEdit.replicas" :min="0" :max="1000" :placeholder="t('k8s.ph.leaveBlank')" style="width: 180px" />
          <span class="tip" style="margin-left: 10px">{{ t('k8s.formEdit.blankNoChange') }}</span>
        </el-form-item>
        <el-form-item :label="t('k8s.field.firstImage')" v-if="['Deployment', 'StatefulSet', 'DaemonSet'].includes(formEditRow?.kind)">
          <el-input v-model="formEdit.image" :placeholder="t('k8s.ph.imageBlank')" />
        </el-form-item>
        <el-form-item :label="t('k8s.field.resources')">
          <div class="res-grid">
            <span class="tip">{{ t('k8s.field.cpuRequest') }}</span><el-input v-model="formEdit.requests_cpu" :placeholder="t('k8s.ph.cpuReq')" />
            <span class="tip">{{ t('k8s.field.memRequest') }}</span><el-input v-model="formEdit.requests_mem" :placeholder="t('k8s.ph.memReq')" />
            <span class="tip">{{ t('k8s.field.cpuLimit') }}</span><el-input v-model="formEdit.limits_cpu" :placeholder="t('k8s.ph.cpuLim')" />
            <span class="tip">{{ t('k8s.field.memLimit') }}</span><el-input v-model="formEdit.limits_mem" :placeholder="t('k8s.ph.memLim')" />
          </div>
        </el-form-item>
        <el-form-item :label="t('k8s.field.labels')">
          <el-input v-model="formEdit.labelsText" type="textarea" :rows="4" :placeholder="t('k8s.ph.labels')" class="mono" />
        </el-form-item>
        <el-form-item :label="t('k8s.field.confirmResourceName')">
          <el-input v-model="formEdit.confirm_name" :placeholder="formEditRow?.name" />
        </el-form-item>
        <div class="tip">{{ t('k8s.formEdit.saveTip') }}</div>
      </el-form>
      <template #footer>
        <el-button @click="formEditVisible = false">{{ t('k8s.actions.cancel') }}</el-button>
        <el-button type="primary" :disabled="formEdit.confirm_name !== formEditRow?.name" @click="submitFormEdit">{{ t('k8s.actions.saveApply') }}</el-button>
      </template>
    </el-dialog>

    <!-- Helm 操作确认（高危门） -->
    <el-dialog v-model="helmDialog" :title="t('k8s.helm.title', { action: helmAction === 'upgrade' ? t('k8s.word.upgrade') : t('k8s.op.uninstall'), name: helmRow?.name })" width="460px">
      <div class="warn-box" v-if="helmAction === 'uninstall'">
        <b>{{ t('k8s.warn.redLabel') }}</b>{{ t('k8s.helm.uninstallWarn', { name: helmRow?.name, ns: helmRow?.namespace }) }}
      </div>
      <div class="warn-box" v-else>
        {{ t('k8s.helm.upgradeWarn', { name: helmRow?.name }) }}
      </div>
      <el-form label-width="120px">
        <el-form-item v-if="helmAction === 'upgrade'" :label="t('k8s.field.chartRef')">
          <el-input v-model="helmChart" :placeholder="t('k8s.ph.chartRef')" />
        </el-form-item>
        <el-form-item :label="t('k8s.field.confirmReleaseName')">
          <el-input v-model="helmConfirm" :placeholder="helmRow?.name" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="helmDialog = false">{{ t('k8s.actions.cancel') }}</el-button>
        <el-button :type="helmAction === 'uninstall' ? 'danger' : 'warning'"
                   :disabled="helmConfirm !== helmRow?.name" @click="submitHelm">
          {{ t('k8s.helm.confirmLabel', { action: helmAction === 'upgrade' ? t('k8s.word.upgrade') : t('k8s.op.uninstall') }) }}
        </el-button>
      </template>
    </el-dialog>

    <!-- Secret 揭示（高危门+审计） -->
    <el-dialog v-model="secretVisible" :title="t('k8s.secret.title', { ns: secretRow?.namespace, name: secretRow?.name })" width="560px">
      <div class="warn-box">
        <b>{{ t('k8s.warn.redLabel') }}</b>{{ t('k8s.secret.warn') }}
      </div>
      <el-form label-width="130px">
        <el-form-item :label="t('k8s.field.confirmSecretName')">
          <el-input v-model="secretConfirm" :placeholder="secretRow?.name" />
        </el-form-item>
      </el-form>
      <div v-if="secretData" class="yaml-wrap">
        <div class="tip">{{ t('k8s.secret.dataLabel') }}</div>
        <el-input v-model="secretData" type="textarea" :rows="8" readonly class="mono" />
      </div>
      <template #footer>
        <el-button @click="secretVisible = false">{{ t('k8s.actions.close') }}</el-button>
        <el-button type="danger" :loading="secretLoading"
                   :disabled="secretConfirm !== secretRow?.name" @click="revealSecret">{{ t('k8s.actions.revealSecret') }}</el-button>
      </template>
    </el-dialog>

    <!-- Pod exec 终端 -->
    <TermDialog v-model="termVisible" :title="termTitle" :ws-url="termUrl" />
  </div>
</template>

<style scoped>
/* ===== 版式基座：统一间距/字号/圆角 ===== */
.page { display: flex; flex-direction: column; gap: 18px; padding: 4px 2px; }

/* 总览条：卡片化 + 大数字层次 */
.ov-bar {
  display: flex; align-items: center; gap: 28px; padding: 16px 22px;
  border: 1px solid var(--el-border-color-lighter); border-radius: 12px;
  font-size: 14px; flex-wrap: wrap;
  background: linear-gradient(180deg, var(--el-fill-color-blank), var(--el-fill-color-lighter));
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.04);
}
.ov-bar span { font-size: 14px; }
.ov-link { cursor: pointer; transition: color .15s; }
.ov-link:hover { color: var(--el-color-primary); }
.ov-bar .fail { color: var(--el-color-danger); font-weight: 600; font-size: 14px; }

.toolbar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; }
.retention-box { display: flex; align-items: center; gap: 8px; margin-left: auto; font-size: 13px; }
.tip { font-size: 12.5px; color: var(--el-text-color-secondary); }
.mono { font-family: Consolas, monospace; font-size: 12.5px; }

/* 高危警示块：醒目但不刺眼 */
.warn-box {
  background: var(--el-color-danger-light-9); border: 1px solid var(--el-color-danger-light-5);
  border-left: 4px solid var(--el-color-danger); border-radius: 10px;
  padding: 12px 16px; font-size: 13px; line-height: 1.9; color: var(--el-color-danger); margin-bottom: 14px;
}
.warn-line { font-size: 12.5px; line-height: 1.8; }

.yaml-wrap { display: flex; flex-direction: column; gap: 10px; }
.precheck-list {
  margin-top: 12px; border: 1px solid var(--el-border-color-lighter); border-radius: 10px;
  padding: 10px 14px; max-height: 260px; overflow: auto;
  display: flex; flex-direction: column; gap: 6px;
}
.precheck-row { display: flex; align-items: center; gap: 10px; font-size: 12.5px; }
.precheck-row .pk { min-width: 80px; font-family: Consolas, monospace; }
.cluster-cards { margin-top: 4px; }
.cluster-card {
  border: 1px solid var(--el-border-color-lighter); border-radius: 12px;
  padding: 16px 18px; margin-bottom: 14px; cursor: pointer;
  background: var(--el-fill-color-blank); transition: box-shadow .2s, border-color .2s;
}
.cluster-card:hover { box-shadow: 0 6px 18px rgba(59, 130, 246, 0.14); border-color: var(--el-color-primary-light-5); }
.cc-head { display: flex; justify-content: space-between; align-items: center; }
.cc-name { font-size: 15px; }
.cc-api { font-size: 11.5px; color: var(--el-text-color-secondary); margin: 6px 0 10px; }
.cc-stats { display: flex; gap: 16px; font-size: 12.5px; color: var(--el-text-color-regular); margin-bottom: 10px; }
.cc-link { cursor: pointer; transition: color .15s; }
.cc-link:hover { color: var(--el-color-primary); }
.cc-stats b { font-size: 16px; margin-right: 2px; }
.cc-warn { color: var(--el-color-danger); }
.cc-foot { display: flex; justify-content: space-between; align-items: center; }
.cd-wrap { display: flex; flex-direction: column; gap: 16px; }
.cd-sum { margin-bottom: 4px; }
.cd-title { font-size: 14px; font-weight: 600; margin-top: 4px; }
.cd-ops { display: flex; gap: 10px; flex-wrap: wrap; }
.cd-node-bar { display: flex; align-items: center; gap: 12px; margin-bottom: 8px; flex-wrap: wrap; }
.cd-reg-link { display: block; font-size: 11px; margin-top: 2px; }
.more-ic { margin-left: 2px; font-size: 12px; }
.danger-item { color: var(--el-color-danger) !important; }
.yaml-text :deep(textarea) { font-family: Consolas, monospace; font-size: 12px; line-height: 1.6; }
.yaml-foot { display: flex; gap: 12px; align-items: center; }
.log-line { white-space: pre-wrap; word-break: break-all; }

/* 统计卡片：大数字 + 宽卡 */
.tab-page { display: flex; flex-direction: column; gap: 16px; }
.ov-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 16px; }
.ov-card {
  display: flex; align-items: center; gap: 14px;
  border: 1px solid var(--el-border-color-lighter); border-radius: 12px;
  padding: 18px 20px; background: var(--el-fill-color-blank); transition: box-shadow 0.2s;
}
.ov-card:hover { box-shadow: 0 6px 18px rgba(59, 130, 246, 0.12); }
.ov-card.clickable { cursor: pointer; }
.ov-card.clickable:hover { border-color: var(--el-color-primary-light-5); }
.ov-card .num { font-size: 28px; font-weight: 700; line-height: 1.15; }
.ov-card .num.ok { color: var(--el-color-success); }
.ov-card .num.warn { color: var(--el-color-warning); }
.ov-card .label { font-size: 12.5px; color: var(--el-text-color-secondary); margin-top: 2px; }
.ov-card .ic {
  width: 46px; height: 46px; border-radius: 12px; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center; font-size: 22px; color: #fff;
}
.ic-blue { background: linear-gradient(135deg, #60a5fa, #3b82f6); }
.ic-green { background: linear-gradient(135deg, #6ee7b7, #10b981); }
.ic-purple { background: linear-gradient(135deg, #c4b5fd, #8b5cf6); }
.ic-cyan { background: linear-gradient(135deg, #67e8f9, #06b6d4); }
.ic-orange { background: linear-gradient(135deg, #fdba74, #f97316); }
.ic-pink { background: linear-gradient(135deg, #f9a8d4, #ec4899); }

.log-box {
  background: var(--el-fill-color-light); padding: 14px 16px; border-radius: 10px;
  font-size: 12.5px; max-height: 380px; overflow: auto; white-space: pre-wrap;
  font-family: Consolas, monospace; line-height: 1.7;
}
.diag-text { white-space: pre-wrap; font-size: 13px; line-height: 1.7; }
.member-title { margin: 18px 0 10px; font-weight: 600; font-size: 14px; }
.cont-row { margin-bottom: 8px; font-size: 13.5px; }
.rel-chain { margin-bottom: 10px; }
.rel-node { padding: 10px 12px; border: 1px solid var(--el-border-color-lighter); border-radius: 10px; margin-bottom: 10px; }
.rel-arrow { margin: 6px 0 6px 14px; }
.rel-arrow2 { margin-left: 28px; }
.rel-line { margin-bottom: 3px; }
.diff-chip { margin: 0 4px 4px 0; cursor: pointer; }

/* 图形化编辑表单 */
.form-edit :deep(.el-form-item) { margin-bottom: 20px; }
.res-grid {
  display: grid; grid-template-columns: 90px 1fr 90px 1fr; gap: 10px 12px;
  align-items: center; width: 100%;
}

/* ===== Element 控件升档（按钮/表格/标签页/表单呼吸感） ===== */
.toolbar :deep(.el-button) { height: 36px; padding: 0 18px; font-size: 13.5px; border-radius: 8px; }
:deep(.el-tabs__item) { font-size: 15px; padding: 0 22px; height: 46px; line-height: 46px; }
:deep(.el-tabs__item.is-active) { font-weight: 600; }
:deep(.el-table) { font-size: 13px; border-radius: 10px; }
:deep(.el-table th.el-table__cell) { font-size: 13px; font-weight: 600; background: var(--el-fill-color-light); }
:deep(.el-table td.el-table__cell) { padding: 11px 0; }
:deep(.el-dialog) { border-radius: 14px; padding: 8px 10px; }
:deep(.el-dialog__title) { font-size: 17px; font-weight: 600; }
:deep(.el-dialog .el-form-item) { margin-bottom: 20px; }
:deep(.el-dialog .el-button) { height: 36px; padding: 0 20px; font-size: 13.5px; border-radius: 8px; }
:deep(.el-drawer__header) { font-size: 16px; font-weight: 600; margin-bottom: 12px; }
:deep(.el-input__inner) { border-radius: 8px; }
</style>
