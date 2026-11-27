import http from '@/api/http'

// 成本管理（采集 / 看板）
export const collectStatus = () => http.get('/cost/collect/status')
export const triggerCollect = (data, timeout = 300000) => http.post('/cost/collect', data, { timeout })
export const listCostDaily = (params) => http.get('/cost/daily', { params })
export const costSummary = (params) => http.get('/cost/summary', { params })
export const costInsights = (params) => http.get('/cost/insights', { params })

// 实例维度日成本（看板下钻，基于 daily 聚合）
export const instanceCost = (params) => http.get('/cost/instances', { params })

// 异动检测与资源包监控；异动生命周期支持确认/忽略/重开
export const costAnomalies = (params) => http.get('/cost/anomalies', { params })
export const ackAnomalies = (data) => http.post('/cost/anomalies/ack', data)
export const resourcePacks = () => http.get('/cost/packs')
export const packsImpact = () => http.get('/cost/packs/impact')
export const cdnDomainTraffic = (params) => http.get('/cost/cdn-domains', { params })
// 采购建议：该买/该治/浪费——给结论不给数据点
// 昨日按量付费下钻：产品拆解 / 实例用量 / 资源包消耗归因
export const paygDrilldown = (params) => http.get('/cost/drilldown', { params })

export const purchaseAdvice = () => http.get('/cost/purchase-advice')
export const triggerDetect = (data) => http.post('/cost/detect', data, { timeout: 300000 })

export const cdnUrls = (params) => http.get('/cost/cdn-urls', { params })
