import http from '@/api/http'

// 监控与提醒策略：模块提醒矩阵 + 数据源优先级/去向的唯一配置面。
// 账号增删仍在云账号管理（/clouds）；这里只改策略列，不触发重新同步。
export const getPolicyOverview = () => http.get('/policy/overview')
export const updateModulePolicy = (modules) => http.put('/policy/modules', { modules })
export const updateAccountPolicy = (id, data) => http.put(`/policy/accounts/${id}`, data)
export const bulkSetPriority = (ids, priority) =>
  http.post('/policy/accounts/bulk', { ids, priority })
