<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useRoute, useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'
import { isDark, setTheme, themeMode, toggleTheme } from '@/composables/theme'
import { t } from '@/i18n'
import * as Icons from '@element-plus/icons-vue'
import Watermark from '@/components/Watermark.vue'
import CopilotFloat from '@/components/CopilotFloat.vue'
import LangSwitch from '@/components/LangSwitch.vue'
const { Bell } = Icons
import MarkdownIt from 'markdown-it'
import http from '@/api/http'
import { inbox, markAllRead, markRead } from '@/api/messages'
import { fmtTime } from '@/utils/format'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()

// 路由是菜单的唯一事实源：总览常显，其余入口按标准 el-sub-menu 分组。
// 容器平台是独立管理域，不归入监控；组名使用 i18n 键。
// 排序偏好保存 i18n 键，读取时兼容旧版中文组名。
const MENU_GROUP_ORDER = [
  'layout.menu.group.monitor', 'layout.menu.group.containers', 'layout.menu.group.response',
  'layout.menu.group.cost_assets', 'layout.menu.group.ai', 'layout.menu.group.system',
]

const menuItems = computed(() => {
  const root = router.options.routes.find((r) => r.path === '/')
  const home = []
  const groups = {}
  for (const c of (root?.children || [])) {
    if (!c.meta?.title) continue
    // meta.roles 缺省表示所有角色可见；不匹配的入口不渲染。
    if (c.meta.roles && !c.meta.roles.includes(auth.role)) continue
    // 初始化完成后隐藏向导菜单；保留路由以查看已初始化摘要。
    if (c.path === 'bootstrap' && auth.initDone) continue
    const g = c.meta.group || ''
    // 资源域多菜单直达（menuTabs）：一个路由展开为多条 tab 锚点菜单
    const tabs = c.meta.menuTabs
    const items = tabs
      ? tabs.map((tb) => ({ path: `/${c.path}?tab=${tb.tab}`, title: tb.label, icon: tb.icon || c.meta.icon }))
      : [{ path: '/' + c.path, title: c.meta.title, icon: c.meta.icon }]
    if (!g || c.path === 'dashboard') {
      home.push(...items)      // 总览永远在最上面、不折叠
      continue
    }
    ;(groups[g] ||= []).push(...items)
  }
  // 用户级菜单排序：localStorage 偏好，缺省使用默认顺序
  let order = []
  try { order = JSON.parse(localStorage.getItem('qianshu.menu.order') || '[]') } catch { order = [] }
  order = order.map(normGroupKey)   // 兼容旧版存中文组名的偏好
  const names = [...MENU_GROUP_ORDER.filter((g) => groups[g])]
  names.sort((a, b) => {
    const ia = order.indexOf(a); const ib = order.indexOf(b)
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib)
  })
  return {
    home,
    groups: names.map((g) => ({ name: g, items: groups[g] })),
  }
})

// 旧版排序偏好存中文组名，现存 i18n 键——读取时映射回键（中文界面下 t(键)=旧值，直接对上）
function normGroupKey(x) {
  if (MENU_GROUP_ORDER.includes(x)) return x
  return MENU_GROUP_ORDER.find((k) => t(k) === x) || x
}

// ---- 菜单排序弹窗 ----
const menuSortVisible = ref(false)
const menuSortList = ref([])

function openMenuSort() {
  menuSortList.value = menuItems.value.groups.map((g) => g.name)
  menuSortVisible.value = true
}

function moveGroup(i, d) {
  const j = i + d
  if (j < 0 || j >= menuSortList.value.length) return
  const arr = [...menuSortList.value]
  ;[arr[i], arr[j]] = [arr[j], arr[i]]
  menuSortList.value = arr
}

function saveMenuOrder() {
  localStorage.setItem('qianshu.menu.order', JSON.stringify(menuSortList.value))
  menuSortVisible.value = false
  ElMessage.success(t('layout.menu_sort.saved'))
}

/* ---------------- 站内信铃铛 ---------------- */
const md = new MarkdownIt({ html: false, breaks: true })
const messages = ref([])
const msgLoading = ref(false)
const msgDrawer = ref(false)
const msgDetail = ref(null)
const unread = ref(0)
let msgTimer = null

async function loadMessages() {
  msgLoading.value = true
  try {
    const data = await inbox({ page: 1, page_size: 10 })
    messages.value = data.items || []   // 缺 items 时使用空列表，保证模板长度计算安全
    unread.value = data.unread
  } catch {
    /* 辅助消息加载失败时不阻断主界面 */
  } finally {
    msgLoading.value = false   // 失败也必须结束加载状态
  }
}

// 消息只跳转到目标页面实际支持的入口；records/anomalies 页签可直接消费，
// observe 统一落到画像页，不携带目标页面无法消费的记录参数。
const MSG_LINKS = {
  alert: '/alerts?tab=records', cost: '/cost?tab=anomalies', inspection: '/inspections',
  diagnosis: '/diagnose', digest: '/', recheck: '/alerts?tab=records', observe: '/profiles',
  agent_issue: '/agent/issues',
}

async function onMsgClick(m) {
  msgDetail.value = m
  msgDrawer.value = true
  if (!m.read) {
    try {
      await markRead(m.id)
      m.read = true
      unread.value = Math.max(0, unread.value - 1)
    } catch { /* ignore */ }
  }
}

function goMsgTarget() {
  const m = msgDetail.value
  msgDrawer.value = false
  // 平台自愈问题单带 ref_id：点站内信直达该问题详情，而不是只落在收件箱首页。
  const base = MSG_LINKS[m?.msg_type] || '/'
  const target = m?.msg_type === 'agent_issue' && m?.ref_id ? `${base}?id=${m.ref_id}` : base
  router.push(target)
}

function msgSummary(m) {
  const first = (m.content_md || '').split('\n').find((l) => l.trim())
  return first ? first.replace(/[#>*`]/g, '').slice(0, 80) : t('layout.msg.click_hint')
}

async function onReadAll() {
  const r = await markAllRead()
  if (r.marked) ElMessage.success(t('layout.msg.marked_read', { n: r.marked }))
  loadMessages()
}

const fmtMsgTime = (iso) => (iso ? fmtTime(iso).slice(5, 16) : '')   // 复用全局时间格式

onMounted(() => {
  loadAccounts()
  if (!auth.user) auth.fetchMe().catch(() => {})
  auth.fetchInit().catch(() => {})   // 初始化状态（向导菜单显隐用，公开接口）
  loadMessages()
  msgTimer = setInterval(loadMessages, 60000)
})
onUnmounted(() => clearInterval(msgTimer))

// 多账号切换器的状态必须在 script setup 顶层定义，供挂载流程使用。
const accounts = ref([])
const currentAccount = ref(localStorage.getItem('qianshu.account') || '0')

async function loadAccounts() {
  try {
    // 顶栏切换的是 cloud_accounts 数据面账号，不是 /settings/accounts 的分组行；
    // 排除分组镜像与停用账号，使过滤条件与实际采集账号一致。
    const r = await http.get('/clouds/accounts')
    accounts.value = (r.items || [])
      .filter((a) => !a.bridge_group && a.enabled !== false)   // 排除分组镜像行与停用账号
      .map((a) => ({ id: a.id, name: a.vendor_label ? `${a.name}（${a.vendor_label}）` : a.name }))
    // 当前选中账号已不存在/停用：回到「全部账号」，避免页面数据被一个不存在的过滤条件清空
    if (currentAccount.value !== '0' && !accounts.value.some((a) => String(a.id) === String(currentAccount.value))) {
      currentAccount.value = '0'
      localStorage.setItem('qianshu.account', '0')
    }
  } catch { /* 静默 */ }
}
function onAccountChange(v) {
  localStorage.setItem('qianshu.account', String(v))
  location.reload()   // 账号切换后重新加载所有数据面，避免跨账号状态残留
}

async function onLogout() {
  await auth.logout()
  ElMessage.success(t('layout.logout.done'))
  router.push('/login')
}
</script>

<template>
  <el-container class="layout">
    <el-aside width="210px" class="aside">
      <div class="logo"><img src="/logo.png" :alt="t('layout.brand')" class="logo-img" /><span>{{ t('layout.brand') }}</span></div>
      <el-menu router :default-active="route.fullPath" class="menu">
        <el-menu-item v-for="item in menuItems.home" :key="item.path" :index="item.path">
          <el-icon class="mi"><component :is="Icons[item.icon] || Icons.Menu" /></el-icon>
          <span>{{ t(item.title) }}</span>
        </el-menu-item>
        <el-sub-menu v-for="g in menuItems.groups" :key="g.name" :index="g.name">
          <template #title>
            <el-icon class="mi"><component :is="Icons.Menu" /></el-icon>
            <span>{{ t(g.name) }}</span>
          </template>
          <el-menu-item v-for="item in g.items" :key="item.path" :index="item.path">
            <el-icon class="mi"><component :is="Icons[item.icon] || Icons.Menu" /></el-icon>
            <span>{{ t(item.title) }}</span>
          </el-menu-item>
        </el-sub-menu>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header class="header">
        <div class="header-title">{{ route.meta.title ? t(route.meta.title) : '' }}</div>
        <div class="header-right">
          <el-select :model-value="Number(currentAccount)" size="small" class="acc-select"
                     @change="onAccountChange">
            <el-option :label="t('layout.account.all')" :value="0" />
            <el-option v-for="a in accounts" :key="a.id" :label="a.name" :value="a.id" />
          </el-select>
          <el-popover placement="bottom-end" :width="380" trigger="click" @show="loadMessages">
            <template #reference>
              <el-badge :value="unread" :hidden="!unread" :max="99" class="bell-badge">
                <el-icon :size="18" class="bell-icon"><Bell /></el-icon>
              </el-badge>
            </template>
            <div class="msg-box">
              <div class="msg-head">
                <span>{{ t('layout.msg.title', { n: unread }) }}</span>
                <el-button v-if="unread" link type="primary" size="small" @click="onReadAll">{{ t('layout.msg.mark_all') }}</el-button>
              </div>
              <div v-if="msgLoading" class="msg-empty">{{ t('layout.msg.loading') }}</div>
              <div v-else-if="!messages.length" class="msg-empty">{{ t('layout.msg.empty') }}</div>
              <div
                v-for="m in messages"
                :key="m.id"
                class="msg-item"
                :class="{ unread: !m.read }"
                @click="onMsgClick(m)"
              >
                <div class="msg-title">
                  <span class="dot" :class="m.level" />
                  {{ m.title }}
                  <el-tag v-if="!m.read" size="small" type="danger" effect="plain">{{ t('layout.msg.unread') }}</el-tag>
                </div>
                <div class="msg-brief">{{ msgSummary(m) }}</div>
                <div class="msg-time">{{ fmtMsgTime(m.created_at) }} · {{ m.msg_type }}</div>
              </div>
              <!-- 站内信统一收口在顶栏铃铛；无消息时也保留入口说明 -->
              <div class="msg-hint">{{ t('layout.msg.hint') }}</div>
            </div>
          </el-popover>
          <el-switch
            :model-value="isDark"
            :active-text="t('layout.theme.dark')"
            :inactive-text="t('layout.theme.light')"
            inline-prompt
            @change="toggleTheme()"
          />
          <LangSwitch />
          <el-dropdown>
            <span class="user-chip">
              {{ auth.user?.display_name || auth.user?.username || '...' }}
              ({{ auth.user?.role || '' }})
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item @click="openMenuSort">{{ t('layout.menu_sort.open') }}</el-dropdown-item>
                <el-dropdown-item @click="setTheme('auto')">{{ t('layout.theme.auto') }}</el-dropdown-item>
                <el-dropdown-item disabled>{{ t('agent.copilot.shortcut_tip', { keys: '⌘K / Ctrl+K' }) }}</el-dropdown-item>
                <!-- 用户自助改密入口；管理员代设在 Settings-用户 -->
                <el-dropdown-item divided @click="router.push('/change-password')">{{ t('layout.change_password') }}</el-dropdown-item>
                <el-dropdown-item divided @click="onLogout">{{ t('layout.logout') }}</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>
      <el-dialog v-model="menuSortVisible" :title="t('layout.menu_sort.title')" width="380px">
        <div v-for="(g, i) in menuSortList" :key="g" class="ms-row">
          <span>{{ t(g) }}</span>
          <span>
            <el-button size="small" text :disabled="i === 0" @click="moveGroup(i, -1)">{{ t('layout.menu_sort.up') }}</el-button>
            <el-button size="small" text :disabled="i === menuSortList.length - 1" @click="moveGroup(i, 1)">{{ t('layout.menu_sort.down') }}</el-button>
          </span>
        </div>
        <template #footer>
          <el-button @click="menuSortVisible = false">{{ t('layout.cancel') }}</el-button>
          <el-button type="primary" @click="saveMenuOrder">{{ t('layout.save') }}</el-button>
        </template>
      </el-dialog>
      <el-main class="main">
        <router-view />
      <Watermark />
      </el-main>
      <CopilotFloat />
    </el-container>
  </el-container>
    <el-drawer v-model="msgDrawer" size="560px" :title="msgDetail?.title || t('layout.msg.detail')">
      <template v-if="msgDetail">
        <div class="msg-meta">
          <el-tag :type="msgDetail.level === 'critical' ? 'danger' : msgDetail.level === 'warning' ? 'warning' : 'info'" size="small">
            {{ msgDetail.level }}
          </el-tag>
          <span class="msg-time">{{ fmtMsgTime(msgDetail.created_at) }} · {{ msgDetail.msg_type }}</span>
        </div>
        <!-- eslint-disable-next-line vue/no-v-html -->
        <div class="msg-content markdown-body" v-html="md.render(msgDetail.content_md || t('layout.msg.no_content'))" />
        <div class="msg-actions">
          <el-button type="primary" @click="goMsgTarget()">{{ t('layout.msg.go_target') }}</el-button>
          <el-button @click="msgDrawer = false">{{ t('layout.close') }}</el-button>
        </div>
      </template>
    </el-drawer>
</template>

<style scoped>
.ms-row { display: flex; justify-content: space-between; align-items: center; padding: 8px 4px; border-bottom: 1px solid var(--el-border-color-lighter); }
.msg-brief {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin: 2px 0;
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}
.msg-meta { display: flex; gap: 8px; align-items: center; margin-bottom: 10px; }
.msg-content { font-size: 13px; line-height: 1.8; }
.msg-actions { margin-top: 16px; display: flex; gap: 8px; }

.layout {
  height: 100%;
}
.aside {
  border-right: 1px solid var(--el-border-color-light);
  background: linear-gradient(180deg, #eef2f9 0%, #e4ebf5 100%);
}
.mi { margin-right: 8px; font-size: 16px; vertical-align: -2px; }
.logo {
  height: 60px;
  display: flex;
  align-items: center;
  justify-content: flex-start;
  gap: 10px;
  padding: 0 18px;
  font-size: 20px;
  font-weight: 700;
  letter-spacing: 2px;
  color: var(--el-text-color-primary);
}
.logo-img {
  width: 32px;
  height: 32px;
  border-radius: 6px;
}
.menu {
  border-right: none;
  background: transparent;
  padding: 6px 10px;
}
.menu :deep(.el-menu-item),
.menu :deep(.el-sub-menu__title) {
  height: 42px;
  line-height: 42px;
  border-radius: 8px;
  margin: 3px 0;
  color: #42506a;
  font-size: 13.5px;
}
.menu :deep(.el-menu-item:hover),
.menu :deep(.el-sub-menu__title:hover) {
  background: rgba(255, 255, 255, 0.7);
  color: var(--el-color-primary);
}
.menu :deep(.el-menu-item.is-active) {
  background: var(--el-color-primary);
  color: #fff;
  font-weight: 600;
  box-shadow: 0 3px 10px rgba(59, 130, 246, 0.30);
}
.menu :deep(.el-menu-item.is-active .mi) { color: #fff; }
.menu :deep(.el-sub-menu .el-menu-item) { padding-left: 42px !important; min-width: 0; }
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid var(--el-border-color-light);
  background: #fff;
  height: 60px;
  padding: 0 22px;
}
.header-title {
  font-size: 19px;
  font-weight: 700;
  color: var(--el-text-color-primary);
  letter-spacing: 0.5px;
}
.acc-select { width: 150px; margin-right: 8px; }
.header-right {
  display: flex;
  align-items: center;
  gap: 16px;
}
.bell-badge {
  cursor: pointer;
  margin-right: 4px;
}
.bell-icon {
  color: var(--el-text-color-primary);
}
.msg-box {
  margin: -12px;
}
.msg-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 12px;
  border-bottom: 1px solid var(--el-border-color-lighter);
  font-weight: 600;
  font-size: 13px;
}
.msg-empty {
  padding: 24px 0;
  text-align: center;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.msg-item {
  padding: 8px 12px;
  cursor: pointer;
  border-bottom: 1px solid var(--el-border-color-extra-light);
}
.msg-item:hover {
  background: var(--el-fill-color-light);
}
.msg-title {
  font-size: 13px;
  display: flex;
  align-items: center;
  gap: 6px;
}
.msg-item.unread .msg-title {
  font-weight: 600;
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.dot.critical {
  background: var(--el-color-danger);
}
.dot.warning {
  background: var(--el-color-warning);
}
.dot.info {
  background: var(--el-color-info);
}
.msg-time {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-top: 2px;
}
.msg-hint {
  padding: 8px 12px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--el-text-color-secondary);
  background: var(--el-fill-color-lighter);
}
.user-chip {
  cursor: pointer;
  font-size: 14px;
}
.main {
  background: var(--el-bg-color-page);
}
</style>
