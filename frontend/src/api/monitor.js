import http from '@/api/http'

// 云监控直查，一次拉一个实例整组指标（约 3~6 个 metric）
export const instanceMetrics = (product, instanceId, range) =>
  http.get(`/monitor/${product}/${encodeURIComponent(instanceId)}`, {
    params: { range },
    timeout: 60000,
  })

// 历史趋势（读 VictoriaMetrics 本地采集数据）
export const instanceHistory = (product, instanceId, range) =>
  http.get(`/monitor/history/${product}/${encodeURIComponent(instanceId)}`, {
    params: { range },
    timeout: 60000,
  })

// 指标采集任务状态/手动触发（product：只采该产品，空=全部）
export const collectStatus = () => http.get('/monitor/collect/status')
export const triggerCollect = (product) =>
  http.post('/monitor/collect/run', null, {
    params: product ? { product } : {},
    timeout: 120000,
  })

// 指标配置（按产品；不传产品返回全部）
export const metricConfig = (product) =>
  http.get('/monitor/config', { params: product ? { product } : {} })
