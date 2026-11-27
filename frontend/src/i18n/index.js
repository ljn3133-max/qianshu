/**
 * 轻量 i18n：零新依赖，避免为 vue-i18n 增加联网装包要求。
 * 契约（所有抽文案的改动方遵守）：
 *   import { t, locale, setLocale } from '@/i18n'
 *   模板：{{ t('alerts.title') }}；脚本：t('alerts.title')；插值：t('x.y', { n: 3 })
 *   目录：src/i18n/locales/zh-CN/<ns>.js 与 en-US/<ns>.js 各 default 导出扁平点号键对象
 *   规则：每个键两边都要有；zh 值=原文逐字（中文模式零变化）；组件内不留硬编码中文文案
 *   （代码注释可以留中文——注释不是 UI）
 * 切换：setLocale('en') 落 localStorage 后整页刷新（与账号切换同模式），
 *   保证后端下发文案（教程/工作流名等带 lang 参数）同步换语言。
 */
import { ref } from 'vue'

const NS = [
  'common', 'layout', 'login', 'dashboard', 'monitor', 'logs', 'alerts', 'ops',
  'changes', 'anomalies', 'profiles', 'cost', 'expiry', 'topology', 'synth',
  'oncall', 'k8s', 'machines', 'inspections', 'diagnose', 'dba',
  'integrations', 'clouds', 'bootstrap', 'settings', 'agent', 'workflow', 'issues',
]

const zhModules = import.meta.glob('./locales/zh-CN/*.js', { eager: true })
const enModules = import.meta.glob('./locales/en-US/*.js', { eager: true })

function merge(mods) {
  const out = {}
  for (const [path, mod] of Object.entries(mods)) {
    const ns = path.split('/').pop().replace('.js', '')
    for (const [k, v] of Object.entries(mod.default || {})) out[k] = v
    void ns
  }
  return out
}

const catalogs = {
  zh: merge(zhModules),
  en: merge(enModules),
}

export const locale = ref(localStorage.getItem('qianshu.lang') === 'en' ? 'en' : 'zh')

export function t(key, params) {
  let s = catalogs[locale.value]?.[key] ?? catalogs.zh[key] ?? key
  if (params) {
    // 函数式 replacer：参数值含 $&/$`/$' 时不会被替换引擎误解释
    for (const [k, v] of Object.entries(params)) s = s.replaceAll(`{${k}}`, () => String(v))
  }
  return s
}

export function setLocale(l) {
  locale.value = l === 'en' ? 'en' : 'zh'
  localStorage.setItem('qianshu.lang', locale.value)
  document.documentElement.setAttribute('lang', locale.value === 'en' ? 'en' : 'zh-CN')
  location.reload()   // 后端下发文案带 lang 请求头，刷新保证全站同语言
}

export function initLang() {
  document.documentElement.setAttribute('lang', locale.value === 'en' ? 'en' : 'zh-CN')
}

export { NS }
