import fs from 'node:fs'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

import { fileURLToPath, URL } from 'node:url'

// 访问域名白名单（2026-11-02 系统管理页可配置）：vite.allowed-hosts.json
// 为空/缺失 = 全放行（内网工具开箱可用）；配置后 = 仅白名单可访问。
// 平台「系统设置-访问域名」保存时写此文件并 touch 本配置——vite 监听 config
// 变化自动秒级重载，无需手动重启（真正的动态生效）。
const allowedHostsFile = fileURLToPath(new URL('./vite.allowed-hosts.json', import.meta.url))
let allowedHosts = true
try {
  const arr = JSON.parse(fs.readFileSync(allowedHostsFile, 'utf-8'))
  if (Array.isArray(arr) && arr.length) {
    allowedHosts = [...arr, 'localhost', '127.0.0.1']
  }
} catch { /* 文件不存在/为空 → 全放行 */ }

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    host: '0.0.0.0', // 允许局域网访问（本地开发）
    port: 5273,
    allowedHosts,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8810', changeOrigin: true },
      '/ws': { target: 'ws://127.0.0.1:8810', ws: true },   // WebShell 终端
    },
  },
})
