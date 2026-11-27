import axios from 'axios'
import { ElMessage } from 'element-plus'
import { t } from '@/i18n'

const http = axios.create({ baseURL: '/api', timeout: 30000 })

http.interceptors.request.use((config) => {
  const token = localStorage.getItem('qianshu.token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  // 后端下发文案（教程/工作流名等）按 Accept-Language 返回对应语言
  config.headers['Accept-Language'] = localStorage.getItem('qianshu.lang') === 'en' ? 'en' : 'zh-CN'
  // 多账号：GET 请求自动带 account_id（0=全部聚合）
  const acc = localStorage.getItem('qianshu.account') || '0'
  // 显式传了 account_id（含 0=本页全部账号）就尊重调用方，不能再被顶栏覆盖：
  // 此前用 !params.account_id 判断，0 是 falsy，页面自己指定的「全部账号」会被改掉。
  const hasExplicit = config.params && Object.prototype.hasOwnProperty.call(config.params, 'account_id')
  if (config.method === 'get' && acc !== '0' && !hasExplicit) {
    config.params = { ...(config.params || {}), account_id: Number(acc) }
  }
  return config
})

http.interceptors.response.use(
  (resp) => resp.data,
  (err) => {
    const detail = err.response?.data?.detail
    if (err.response?.status === 401) {
      localStorage.removeItem('qianshu.token')
      if (location.pathname !== '/login') location.href = '/login'
    } else if (err.response?.status === 403 && detail?.code === 'NEED_CHANGE_PASSWORD') {
      ElMessage.warning(detail.message || t('login.need_change_password'))
      if (location.pathname !== '/change-password') location.href = '/change-password'
    } else {
      ElMessage.error(typeof detail === 'string' ? detail : (detail?.message || t('common.request_failed', { msg: err.message })))
    }
    return Promise.reject(err)
  }
)

export default http
