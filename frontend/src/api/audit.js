import http from '@/api/http'

// 审计日志（admin）：全操作留痕的只读查询
export const listAuditLogs = (params) => http.get('/audit/logs', { params })
