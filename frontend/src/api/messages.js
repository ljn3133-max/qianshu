import http from '@/api/http'

// 站内信（告警/巡检/系统通知收口）
export const inbox = (params) => http.get('/messages/inbox', { params })
export const markRead = (id) => http.post(`/messages/${id}/read`)
export const markAllRead = () => http.post('/messages/read-all')
