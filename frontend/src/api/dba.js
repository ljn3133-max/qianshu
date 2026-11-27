import http from '@/api/http'

// DBA 专项（RDS 慢SQL / SQL洞察明细 / 空间趋势 / 备份状态）
export const dbaInstances = () => http.get('/dba/instances')
export const slowlogTop = (params) => http.get('/dba/slowlogs', { params, timeout: 120000 })
export const slowlogRecords = (params) => http.get('/dba/slowlog-records', { params, timeout: 120000 })
export const insightRecords = (params) => http.get('/dba/insight/records', { params, timeout: 120000 })
export const dbaSpace = (params) => http.get('/dba/space', { params })
export const dbaBackups = (params) => http.get('/dba/backups', { params, timeout: 60000 })
export const redisInstances = () => http.get('/dba/redis/instances')
export const redisKeys = (params) => http.get('/dba/redis/keys', { params, timeout: 60000 })
export const spaceCollectStatus = () => http.get('/dba/collect/status')
export const triggerSpaceCollect = () => http.post('/dba/collect', {}, { timeout: 120000 })
