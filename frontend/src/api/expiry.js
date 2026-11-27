import http from '@/api/http'

// 到期总账：域名/证书/包年资产/资源包到期合并视图
export const expiryItems = (params) => http.get('/expiry/items', { params })

// 外部注册域名因本账号 API 查不到，需人工登记到期信息
export const registerExternalDomain = (data) => http.post('/expiry/domains', data)
export const removeExternalDomain = (domain) => http.delete(`/expiry/domains/${encodeURIComponent(domain)}`)

export const ignoreExpiry = (data) => http.post('/expiry/ignores', data)
export const unignoreExpiry = (key) => http.delete(`/expiry/ignores/${encodeURIComponent(key)}`)
