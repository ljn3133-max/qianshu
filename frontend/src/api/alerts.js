import http from '@/api/http'

// 通知渠道（企微/钉钉机器人）——CRUD 唯一管理面在 系统设置→通知渠道
export const listChannels = () => http.get('/alerts/channels')
export const createChannel = (data) => http.post('/alerts/channels', data)
export const updateChannel = (id, data) => http.put(`/alerts/channels/${id}`, data)
export const deleteChannel = (id) => http.delete(`/alerts/channels/${id}`)
// 发送测试消息=真实对外推送（用户主动点击才发；全站唯一真发口在 系统设置→通知渠道）
export const testChannel = (id) => http.post(`/alerts/channels/${id}/test`, null, { timeout: 30000 })

// 告警规则
export const listRules = () => http.get('/alerts/rules')
export const createRule = (data) => http.post('/alerts/rules', data)
// 自动规则：平台盘点"有资产无规则"的缺口，并用近 N 天真实样本建议阈值。
// 分析只读；用户点选后只传 key，服务端重新分析再建（阈值不接受前端传入）
export const autoAnalyzeRules = (params) => http.get('/alerts/rules/auto-analyze', { params, timeout: 60000 })
export const autoCreateRules = (keys) => http.post('/alerts/rules/auto-create', { keys }, { timeout: 120000 })
export const updateRule = (id, data) => http.put(`/alerts/rules/${id}`, data)
export const deleteRule = (id) => http.delete(`/alerts/rules/${id}`)

// 规则试算：保存前用近 N 天历史数据回放该规则——会触发几次/什么时间/样本值，
// 帮调阈值不盲拍。只读历史数据零写库
export const dryRunRule = (data) => http.post('/alerts/rules/试算', data, { timeout: 120000 })

// 告警记录与评估
// 设为常态：把某机器某指标记为自身水位，之后只报显著偏离
export const acceptBaseline = (data) => http.post('/alerts/accept-baseline', data)

// 手动标记已恢复（处理完主动收口，不干等恢复线）
export const resolveRecord = (id) => http.post(`/alerts/records/${id}/resolve`)

// 确认/忽略：静默后续提醒，按账号优先级安排重申；不是值班「认领」
export const ackRecord = (id, data) => http.post(`/alerts/records/${id}/ack`, data)
export const clearRecordAck = (id) => http.delete(`/alerts/records/${id}/ack`)

export const listRecords = (params) => http.get('/alerts/records', { params })
export const evalStatus = () => http.get('/alerts/eval/status')
export const triggerEval = () => http.post('/alerts/eval/run', null, { timeout: 120000 })
