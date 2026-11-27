import { createPinia } from 'pinia'
import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import en from 'element-plus/es/locale/lang/en'
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'

import App from './App.vue'
import TableResize from './directives/tableResize'
import router from './router'
import { initTheme } from './composables/theme'
import { initLang, locale } from './i18n'
import './styles/index.css'

// Element Plus 内置文案（分页/日期等）随语言切换（setLocale 会整页刷新，启动时取一次即可）
const app = createApp(App)
app.use(createPinia())
app.use(router)
app.use(ElementPlus, { locale: locale.value === 'en' ? en : zhCn })
app.use(TableResize)   // 表格列宽拖拽（v-table-resize）
app.mount('#app')

initTheme() // 暗色主题：随时间自动 + 手动切换
initLang()  // 同步 html lang 属性
