import http from '@/api/http'

// 多源日志：源清单 / 目标清单 / 统一查询
export const logSources = () => http.get('/logs/sources', { timeout: 30000 })

export const logTargets = (source) =>
  http.get('/logs/targets', { params: { source }, timeout: 60000 })

export const logSearch = (params) =>
  http.get('/logs/search', { params, timeout: 90000 })

// SLS project/logstore 列表（告警规则配置与 SLS 源目标选择共用）
export const slsProjects = () => http.get('/logs/projects')

export const slsLogstores = (project, source = '') =>
  http.get('/logs/logstores', { params: { project, source }, timeout: 60000 })

// 常用查询（保存/列表/删除；source/target 为多源字段，旧行 source 为空=SLS）
export const listSavedQueries = () => http.get('/logs/saved')
export const createSavedQuery = (data) => http.post('/logs/saved', data)
export const deleteSavedQuery = (id) => http.delete(`/logs/saved/${id}`)
