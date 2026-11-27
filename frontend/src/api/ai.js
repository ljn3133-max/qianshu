import http from '@/api/http'

// AI 模型配置
export const listAiSettings = () => http.get('/ai/settings')
export const createAiSetting = (data) => http.post('/ai/settings', data)
export const updateAiSetting = (id, data) => http.put(`/ai/settings/${id}`, data)
export const deleteAiSetting = (id) => http.delete(`/ai/settings/${id}`)
export const testAiSetting = (id) => http.post(`/ai/settings/${id}/test`, {}, { timeout: 180000 })
export const aiUsageSummary = (params) => http.get('/ai/usage/summary', { params })
export const aiUsageLogs = (params) => http.get('/ai/usage/logs', { params })

// 场景×模型矩阵
export const listAiScenarios = () => http.get('/ai/scenarios')
export const updateAiScenario = (scenario, data) => http.put(`/ai/scenarios/${scenario}`, data)

// 自然语言单次问答的前端入口已并入千枢智能体；
// 后端 /ai/ask 接口保留供外部集成/兼容旧调用，不再有前端 API 封装。
