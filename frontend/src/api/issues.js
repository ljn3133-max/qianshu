import http from '@/api/http'

// 平台自愈问题收件箱：只读列表/详情 + 受控动作（诊断/重试/协助/授权/拒绝/回滚）
export const issueMeta = () => http.get('/issues/meta')
export const listIssues = (params) => http.get('/issues', { params })
export const issueDetail = (id) => http.get(`/issues/${id}`)
export const diagnoseIssue = (id) => http.post(`/issues/${id}/diagnose`)
export const retryIssue = (id) => http.post(`/issues/${id}/retry`)
export const assistIssue = (id, data) => http.post(`/issues/${id}/assist`, data)
export const ignoreIssue = (id) => http.post(`/issues/${id}/ignore`)
export const approvePlan = (id, planId, data) => http.post(`/issues/${id}/plans/${planId}/approve`, data)
export const rejectPlan = (id, planId, data) => http.post(`/issues/${id}/plans/${planId}/reject`, data)
export const rollbackPlan = (id, planId) => http.post(`/issues/${id}/plans/${planId}/rollback`)
export const resetBreaker = () => http.post('/issues/system/breaker/reset')
