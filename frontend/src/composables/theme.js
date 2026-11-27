/**
 * 主题管理：亮/暗双主题。
 * 规则（已确认的需求）：
 *  - 默认跟随时间自动切换：18:00 ~ 次日 06:00 暗色，其余亮色；
 *  - 用户手动切换后，当天内尊重手动选择（存 localStorage）；
 *  - 手动选择的有效期为当天，次日零点后回到自动模式（避免"手动一次永远不自动"）。
 */
import { ref } from 'vue'

const STORAGE_KEY = 'qianshu.theme.preference'

export const themeMode = ref('auto') // auto / light / dark
export const isDark = ref(false)

function apply() {
  const hour = new Date().getHours()
  const darkByTime = hour >= 18 || hour < 6
  isDark.value = themeMode.value === 'auto' ? darkByTime : themeMode.value === 'dark'
  document.documentElement.classList.toggle('dark', isDark.value)
}

function loadPreference() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null')
    if (saved && saved.date === new Date().toDateString() && saved.mode !== 'auto') {
      themeMode.value = saved.mode
    } else {
      themeMode.value = 'auto'
    }
  } catch {
    themeMode.value = 'auto'
  }
}

export function setTheme(mode /* 'auto' | 'light' | 'dark' */) {
  themeMode.value = mode
  if (mode === 'auto') {
    localStorage.removeItem(STORAGE_KEY)
  } else {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ mode, date: new Date().toDateString() })
    )
  }
  apply()
}

export function toggleTheme() {
  setTheme(isDark.value ? 'light' : 'dark')
}

export function initTheme() {
  loadPreference()
  apply()
  // 定期检查（跨天/跨时段自动切换）
  setInterval(apply, 60 * 1000)
}
