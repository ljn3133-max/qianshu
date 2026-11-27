import http from '@/api/http'

// 统一变更时间线（排障第一问"最近改了什么"）
export const listChanges = (params) => http.get('/changes', { params })
export const syncChanges = (days, accountId = 0) => http.post('/changes/sync', null, {
  params: { days, account_id: accountId || 0 }, timeout: 60000,
})
export const listChangeSources = () => http.get('/changes/sources')
