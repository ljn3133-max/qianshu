import http from '@/api/http'

// 首页今日待办聚合
export const dashboardSummary = () => http.get('/dashboard/summary')
