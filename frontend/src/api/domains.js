import http from '@/api/http'

// 域名登记：域名→证书→SLB/CDN→业务 一处收口
export const domainList = (params) => http.get('/domains', { params })
export const domainDetail = (domain) => http.get(`/domains/${encodeURIComponent(domain)}/detail`)
export const updateDomain = (domain, data) => http.put(`/domains/${encodeURIComponent(domain)}`, data)
export const backfillDomains = () => http.post('/domains/backfill', {})
