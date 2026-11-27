import http from '@/api/http'

export const listAssets = (params) => http.get('/assets', { params })
export const getAsset = (id) => http.get(`/assets/${id}`)
// 资产监控级别：core=全量监控（指标告警+画像+巡检）/ aux=基础监控（只看关机+5xx外溢）
export const setAssetTier = (instanceId, tier) => http.post('/assets/tier', { instance_id: instanceId, tier })
// 批量关键度：勾选清单或按当前筛选条件整页设置
export const setAssetsTierBatch = (data) => http.post('/assets/tier/batch', data)
export const assetStats = () => http.get('/assets/stats')
// 资产导出的资产类型/列目录（唯一事实源在后端；前端只渲染勾选框）
export const assetExportSchema = () => http.get('/assets/export/schema')
// 资产底账·多云口径：按已接入云账号动态聚合，未接入账号不出现
export const cloudOverview = () => http.get('/assets/cloud-overview')
// 全量同步要遍历所有 region，实测约 1 分钟，单独放宽超时
export const syncAssets = (product) =>
  http.post('/assets/sync', null, { params: { product }, timeout: 180000 })

// 电源操作（危险操作：confirm_value 必须逐字符等于实例 ID）
// ECS 与 VMware 虚拟机共用同一端点与电源操作门禁
export const assetAction = (assetId, action, confirmValue) =>
  http.post(`/assets/${assetId}/actions`, { action, confirm_value: confirmValue })
