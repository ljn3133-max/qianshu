import { defineStore } from 'pinia'

import http from '@/api/http'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    token: localStorage.getItem('qianshu.token') || '',
    user: null,
    initDone: null,   // 初始化是否完成；null=尚未探测
  }),
  getters: {
    isLoggedIn: (s) => !!s.token,
    role: (s) => s.user?.role || '',
  },
  actions: {
    async login(username, password, extra = {}) {
      const data = await http.post('/auth/login', { username, password, ...extra })
      this.token = data.token
      this.user = data.user
      localStorage.setItem('qianshu.token', data.token)
    },
    // 初始化状态（公开接口 /bootstrap/status）：路由守卫强跳 + 菜单隐藏共用。
    // force=true 强制重取（向导完成后刷新）；后端不可达按未初始化处理（向导页有未就绪空态）
    async fetchInit(force = false) {
      if (!force && this.initDone !== null) return this.initDone
      try {
        const s = await http.get('/bootstrap/status')
        this.initDone = !!s.init_done
      } catch {
        this.initDone = false
      }
      return this.initDone
    },
    hasCode(code) {
      const codes = this.user?.permission_codes
      if (!codes || !codes.length) {
        // 未下发时按角色兜底（admin/operator 有写权限）
        return this.user?.role === 'admin' || this.user?.role === 'operator'
      }
      return codes.includes(code)
    },
    async fetchMe() {
      this.user = await http.get('/auth/me')
    },
    async logout() {
      try {
        await http.post('/auth/logout')
      } catch {
        /* token 失效时忽略 */
      }
      this.token = ''
      this.user = null
      this.initDone = null
      localStorage.removeItem('qianshu.token')
    },
  },
})
