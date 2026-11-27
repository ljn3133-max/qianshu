import http from '@/api/http'

// 云账号管理：账号台账 + 连通测试 + 批量同步
export const listCloudAccounts = () => http.get('/clouds/accounts')
// 唯一接入任务：向导、智能体与云账号后台读取同一状态
export const listOnboardingRuns = () => http.get('/clouds/onboarding-runs')
export const getAccountOnboarding = (id) => http.get(`/clouds/accounts/${id}/onboarding`)
// 按账号 × 能力 × 范围重试，运行中的范围由服务端复用，不新建重复任务
export const retryOnboardingCapabilities = (id, scopes) =>
  http.post(`/clouds/accounts/${id}/onboarding/retry`, { scopes })
// 账号级 SLS 完整目录与派生语义；原始日志不经过这些接口
export const getSlsDiscovery = (id) => http.get(`/clouds/accounts/${id}/sls-discovery`)
export const startSlsDiscovery = (id) => http.post(`/clouds/accounts/${id}/sls-discovery`)
export const skipSlsTargets = (id) => http.post(`/clouds/accounts/${id}/sls-discovery/skip`)
// 厂商能力清单：前端分组/禁用/能力标注的唯一事实源
export const listVendors = () => http.get('/clouds/vendors')
export const createCloudAccount = (data) => http.post('/clouds/accounts', data)
export const updateCloudAccount = (id, data) => http.put(`/clouds/accounts/${id}`, data)
export const deleteCloudAccount = (id) => http.delete(`/clouds/accounts/${id}`)
// 未保存账号连通测试（首登向导/新建对话框「测试可用性」）
export const testCloudAccountUnsaved = (data) => http.post('/clouds/accounts/test', data)
// 已保存账号连通测试（结果回写 status/last_test_at）
export const testCloudAccount = (id) => http.post(`/clouds/accounts/${id}/test`)
// 批量同步（替换"同步阿里云"）：全量遍历 region 实测约 1 分钟，放宽超时
export const syncCloudAssets = (params) =>
  http.post('/clouds/sync', null, { params, timeout: 180000 })
