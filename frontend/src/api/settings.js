import http from '@/api/http'

// 云账号管理是唯一凭证入口，此处只保留旧凭证只读摘要；
// 旧写接口已 410 弃用，维护走 /clouds/accounts
export const listCredentials = () => http.get('/credentials')

// 用户管理
export const listUsers = () => http.get('/users')
export const createUser = (data) => http.post('/users', data)
export const updateUser = (id, data) => http.put(`/users/${id}`, data)
export const deleteUser = (id) => http.delete(`/users/${id}`)
