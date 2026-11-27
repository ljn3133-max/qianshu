import http from '@/api/http'

// 云账号管理是唯一凭证入口，此处只保留旧凭证只读摘要；
// 旧写接口已 410 弃用，维护走 /clouds/accounts
export const listCredentials = () => http.get('/credentials')

// 用户管理（仅 admin；后端 /api/users 已按角色兜底）
export const listUsers = () => http.get('/users')
export const createUser = (data) => http.post('/users', data)
export const updateUser = (id, data) => http.put(`/users/${id}`, data)
export const deleteUser = (id) => http.delete(`/users/${id}`)
// 重置登录密码：服务端生成一次性临时密码，只在响应里回显一次
export const resetUserPassword = (id) => http.post(`/users/${id}/reset-password`)
