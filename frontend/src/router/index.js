import { createRouter, createWebHistory } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

// 菜单骨架：后续里程碑逐个填充页面
// meta.group/title/menuTabs.label 为 i18n 键（layout.menu.*），菜单渲染处统一 t() 化
const routes = [
  { path: '/login', name: 'login', component: () => import('@/views/Login.vue'), meta: { public: true } },
  { path: '/change-password', name: 'change-password', component: () => import('@/views/ChangePassword.vue') },
  {
    path: '/',
    component: () => import('@/layout/MainLayout.vue'),
    redirect: '/dashboard',
    children: [
      { path: 'dashboard', name: 'dashboard', component: () => import('@/views/Dashboard.vue'), meta: { group: 'layout.menu.group.monitor', title: 'layout.menu.dashboard', icon: 'Odometer' } },
      { path: 'monitor', name: 'monitor', component: () => import('@/views/Monitor.vue'), meta: { group: 'layout.menu.group.monitor', title: 'layout.menu.monitor', icon: 'DataLine' } },
      { path: 'anomalies', name: 'anomalies', component: () => import('@/views/Anomalies.vue'), meta: { group: 'layout.menu.group.monitor', title: 'layout.menu.anomalies', icon: 'TrendCharts' } },
      { path: 'logs', name: 'logs', component: () => import('@/views/Logs.vue'), meta: { group: 'layout.menu.group.monitor', title: 'layout.menu.logs', icon: 'Document' } },
      { path: 'alerts', name: 'alerts', component: () => import('@/views/Alerts.vue'), meta: { group: 'layout.menu.group.response', title: 'layout.menu.alerts', icon: 'Bell' } },
      // 资产台账按属性归「成本与资产」：此前挂在「处置」下，与告警/值班等动作类菜单混在一起
      { path: 'ops', name: 'ops', component: () => import('@/views/Ops.vue'), meta: { group: 'layout.menu.group.cost_assets', title: 'layout.menu.ops', icon: 'Monitor' } },
      { path: 'changes', name: 'changes', component: () => import('@/views/Changes.vue'), meta: { group: 'layout.menu.group.monitor', title: 'layout.menu.changes', icon: 'Clock' } },
      { path: 'profiles', name: 'profiles', component: () => import('@/views/Profiles.vue'), meta: { group: 'layout.menu.group.monitor', title: 'layout.menu.profiles', icon: 'PieChart' } },
      { path: 'cost', name: 'cost', component: () => import('@/views/Cost.vue'), meta: { group: 'layout.menu.group.cost_assets', title: 'layout.menu.cost', icon: 'Coin' } },
      { path: 'expiry', name: 'expiry', component: () => import('@/views/Expiry.vue'), meta: { group: 'layout.menu.group.cost_assets', title: 'layout.menu.expiry', icon: 'Calendar' } },
      // AI 分析已并入千枢智能体，旧链接继续兼容。
      // 旧链接 /ai?q=... 保留转跳：补 session=new 让 Agent 按深链自动发送（等价旧页"进页即问"）
      { path: 'ai', redirect: (to) => ({ path: '/agent', query: { ...to.query, session: to.query.session || 'new' } }) },
      { path: 'agent', name: 'agent', component: () => import('@/views/Agent.vue'), meta: { group: 'layout.menu.group.ai', title: 'layout.menu.agent', icon: 'MagicStick' } },
      // 自定义工作流编辑器：步骤卡片编排，仅 admin 管理
      { path: 'workflow-editor', name: 'workflow-editor', component: () => import('@/views/WorkflowEditor.vue'), meta: { group: 'layout.menu.group.ai', title: 'layout.menu.workflow_editor', icon: 'Operation', roles: ['admin'] } },
      // 规则巡检属于监测域，不归入 AI 功能
      { path: 'inspections', name: 'inspections', component: () => import('@/views/Inspections.vue'), meta: { group: 'layout.menu.group.monitor', title: 'layout.menu.inspections', icon: 'Tickets' } },
      { path: 'links', redirect: '/topology?tab=links' },   // 旧链路拓扑地址兼容重定向到资产拓扑
      { path: 'topology', name: 'topology', component: () => import('@/views/Topology.vue'), meta: { group: 'layout.menu.group.cost_assets', title: 'layout.menu.topology', icon: 'Connection' } },
      { path: 'synth', name: 'synth', component: () => import('@/views/Synth.vue'), meta: { group: 'layout.menu.group.monitor', title: 'layout.menu.synth', icon: 'Aim' } },
      { path: 'k8s', name: 'k8s', component: () => import('@/views/K8s.vue'),
        meta: {
          group: 'layout.menu.group.containers', title: 'layout.menu.k8s', icon: 'Box',
          // 资源域多菜单直达（参考 k8smanager 可发现性）：每个条目=tab 锚点
          menuTabs: [
            { tab: 'overview', label: 'layout.menu.k8s.overview', icon: 'Odometer' },
            { tab: 'clusters', label: 'layout.menu.k8s.clusters', icon: 'Connection' },
            { tab: 'tasks', label: 'layout.menu.k8s.tasks', icon: 'Tickets' },
            { tab: 'resources', label: 'layout.menu.k8s.resources', icon: 'Grid' },
            { tab: 'stats', label: 'layout.menu.k8s.stats', icon: 'DataLine' },
            { tab: 'events', label: 'layout.menu.k8s.events', icon: 'Bell' },
            { tab: 'logs', label: 'layout.menu.k8s.logs', icon: 'Document' },
            { tab: 'failures', label: 'layout.menu.k8s.failures', icon: 'FirstAidKit' },
            { tab: 'audits', label: 'layout.menu.k8s.audits', icon: 'Memo' },
          ],
        } },
      { path: 'machines', name: 'machines', component: () => import('@/views/Machines.vue'), meta: { group: 'layout.menu.group.containers', title: 'layout.menu.machines', icon: 'Cpu' } },
      { path: 'oncall', name: 'oncall', component: () => import('@/views/Oncall.vue'), meta: { group: 'layout.menu.group.response', title: 'layout.menu.oncall', icon: 'Phone' } },
      { path: 'integrations', name: 'integrations', component: () => import('@/views/Integrations.vue'), meta: { group: 'layout.menu.group.system', title: 'layout.menu.integrations', icon: 'Link' } },
      { path: 'clouds', name: 'clouds', component: () => import('@/views/CloudAccounts.vue'), meta: { group: 'layout.menu.group.system', title: 'layout.menu.clouds', icon: 'Cloud' } },
      { path: 'diagnose', name: 'diagnose', component: () => import('@/views/Diagnose.vue'), meta: { group: 'layout.menu.group.ai', title: 'layout.menu.diagnose', icon: 'FirstAidKit' } },
      // 平台自愈问题收件箱：AI 先定位，R2/R3 需管理员按完整计划哈希授权
      { path: 'agent/issues', name: 'agent-issues', component: () => import('@/views/IssueInbox.vue'), meta: { group: 'layout.menu.group.ai', title: 'layout.menu.issues', icon: 'FirstAidKit' } },
      // DBA 使用独立图标，避免与成本管理的 Coin 图标混淆
      { path: 'dba', name: 'dba', component: () => import('@/views/Dba.vue'), meta: { group: 'layout.menu.group.response', title: 'layout.menu.dba', icon: 'DataBoard' } },
      { path: 'bootstrap', name: 'bootstrap', component: () => import('@/views/Bootstrap.vue'), meta: { group: 'layout.menu.group.system', title: 'layout.menu.bootstrap', icon: 'SetUp' } },
      { path: 'settings', name: 'settings', component: () => import('@/views/Settings.vue'), meta: { group: 'layout.menu.group.system', title: 'layout.menu.settings', icon: 'Setting' } },
    ],
  },
  // 404 catch-all：未知路径进 404 页而非空白/路由告警
  { path: '/:pathMatch(.*)*', name: 'not-found', component: () => import('@/views/NotFound.vue') },
]

const router = createRouter({ history: createWebHistory(), routes })

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if (!to.meta.public && !auth.isLoggedIn) return { name: 'login' }
  if (to.name === 'login' && auth.isLoggedIn) return { path: '/' }
  // 首次登录强制改密：默认密码必须换掉才能用系统（刷新后 user 为空则先补取）
  if (auth.isLoggedIn && !auth.user && to.name !== 'change-password') {
    try { await auth.fetchMe() } catch { /* 401 由 http 拦截器处理 */ }
  }
  if (auth.isLoggedIn && auth.user?.must_change_password && to.name !== 'change-password') {
    return { name: 'change-password' }
  }
  // 初始化未完成（无云账号且未完成向导）时强跳安装向导；
  // 完成后守卫放行、菜单隐藏，路由保留可直连（页面显示"已初始化"只读摘要）
  if (auth.isLoggedIn && to.name !== 'bootstrap' && to.name !== 'change-password') {
    const done = await auth.fetchInit()
    if (!done) return { name: 'bootstrap' }
  }
})

export default router
