import axios from 'axios'
import { ElMessage } from 'element-plus'
import { t } from '@/i18n'

/**
 * 带 token 的二进制下载。
 *
 * 不走 http.js：它的响应拦截器只返回 resp.data，会丢掉 Content-Disposition /
 * X-Total-Rows 等下载元信息。这里手动复用同一套 token / 语言 / 账号参数口径。
 * 不使用 window.open（token 在 localStorage，不在 URL 里）。
 */
async function download(method, url, { params = {}, data = null, fallbackName, mime }) {
  const token = localStorage.getItem('qianshu.token')
  const finalParams = { ...params }
  const acc = localStorage.getItem('qianshu.account') || '0'
  const hasExplicit = Object.prototype.hasOwnProperty.call(finalParams, 'account_id')
  if (acc !== '0' && !hasExplicit) finalParams.account_id = Number(acc)

  let resp
  try {
    resp = await axios.request(url, {
      method,
      baseURL: '/api',
      params: finalParams,
      data: data || undefined,
      responseType: 'blob',
      timeout: 120000,   // 2 万行 + 归属解析，30s 默认值不够
      headers: {
        Authorization: token ? `Bearer ${token}` : '',
        'Accept-Language': localStorage.getItem('qianshu.lang') === 'en' ? 'en' : 'zh-CN',
      },
    })
  } catch (e) {
    let msg = ''
    try {
      const text = await e.response?.data?.text?.()
      msg = JSON.parse(text || '{}').detail || ''
    } catch { /* 非 JSON 错误体 */ }
    if (e.response?.status === 401) {
      localStorage.removeItem('qianshu.token')
      if (location.pathname !== '/login') location.href = '/login'
    }
    ElMessage.error(msg || t('common.export_failed'))
    return false
  }

  const disp = resp.headers['content-disposition'] || ''
  let filename = fallbackName
  const utf8 = /filename\*=UTF-8''([^;]+)/i.exec(disp)
  const plain = /filename="?([^";]+)"?/i.exec(disp)
  if (utf8) filename = decodeURIComponent(utf8[1])
  else if (plain) filename = plain[1]

  const blob = (resp.data instanceof Blob)
    ? resp.data
    : new Blob([resp.data], { type: mime })
  const a = document.createElement('a')
  const href = URL.createObjectURL(blob)
  a.href = href
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(href), 10000)

  const total = Number(resp.headers['x-total-rows'] || 0)
  const exported = Number(resp.headers['x-exported-rows'] || 0)
  if (total > exported) {
    ElMessage.warning(t('common.export_truncated', { total, exported }))
  } else if (exported) {
    ElMessage.success(t('common.export_done', { rows: exported }))
  }
  return true
}

export function exportCsv(url, params = {}, fallbackName = 'export.csv') {
  return download('GET', url, {
    params, fallbackName, mime: 'text/csv;charset=utf-8',
  })
}

export function exportXlsx(url, data = {}, fallbackName = 'export.xlsx') {
  return download('POST', url, {
    data, fallbackName, mime: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  })
}
