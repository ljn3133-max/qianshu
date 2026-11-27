import http from '@/api/http'

// 故障诊断：发起诊断 / 历史 / 详情
// 采纳 AI 建议为经验案例；自动沉淀只进入待确认候选
export const acceptCase = (id) => http.post(`/diagnosis/cases/${id}/accept`)

export const runDiagnosis = (data) => http.post('/diagnosis/run', data, { timeout: 300000 })
export const listDiagSessions = (params) => http.get('/diagnosis/sessions', { params })
export const diagSessionDetail = (id) => http.get(`/diagnosis/sessions/${id}`)

// 案例库：沉淀 / 检索 / 详情 / 复盘编辑
export const createDiagCase = (data) => http.post('/diagnosis/cases', data)
export const listDiagCases = (params) => http.get('/diagnosis/cases', { params })
export const diagCaseDetail = (id) => http.get(`/diagnosis/cases/${id}`)
export const updateDiagCase = (id, data) => http.put(`/diagnosis/cases/${id}`, data)
