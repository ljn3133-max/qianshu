import http from './http'

// 个人 API Token 明文只在创建响应里回显一次
export const listTokens = () => http.get('/users/me/tokens')
export const createToken = (data) => http.post('/users/me/tokens', data)
export const deleteToken = (id) => http.delete(`/users/me/tokens/${id}`)
