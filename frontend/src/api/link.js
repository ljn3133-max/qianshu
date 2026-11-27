import http from '@/api/http'

// 链路拓扑：草稿生成 / 域名健康汇总 / 边列表
export const linkStatus = () => http.get('/links/status')
export const generateDraft = () => http.post('/links/draft/generate', {}, { timeout: 300000 })
export const listHosts = (params) => http.get('/links/hosts', { params })
export const listEdges = (params) => http.get('/links', { params })

// 逐边确认 + 业务映射（biz_identifiers）
export const setEdgesStatus = (data) => http.post('/links/edges/status', data)
export const listBusinesses = () => http.get('/links/businesses')
export const generateBizDraft = () => http.post('/links/businesses/draft', {}, { timeout: 120000 })
export const confirmBusiness = (id) => http.post(`/links/businesses/${id}/confirm`, {})
export const createBusiness = (data) => http.post('/links/businesses', data)
export const updateBusiness = (id, data) => http.put(`/links/businesses/${id}`, data)
export const deleteBusiness = (id) => http.delete(`/links/businesses/${id}`)
export const addIdentifier = (id, data) => http.post(`/links/businesses/${id}/identifiers`, data)
export const deleteIdentifier = (id, identId) => http.delete(`/links/businesses/${id}/identifiers/${identId}`)
export const businessMap = (id) => http.get(`/links/businesses/${id}/map`)
