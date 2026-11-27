import http from '@/api/http'

// 巡检流水线：运行状态/历史/事实清单
export const inspectionStatus = () => http.get('/inspections/status')
export const triggerInspection = () => http.post('/inspections/run', {}, { timeout: 300000 })
export const listRuns = (params) => http.get('/inspections/runs', { params })
export const runDetail = (runId, params) => http.get(`/inspections/runs/${runId}`, { params })

// 巡检日报 + 推送设置
export const getReport = (params) => http.get('/inspections/report', { params })
export const generateReport = (data) => http.post('/inspections/report/generate', data, { timeout: 300000 })
export const getPushSettings = () => http.get('/inspections/push-settings')
export const updatePushSettings = (data) => http.put('/inspections/push-settings', data)
