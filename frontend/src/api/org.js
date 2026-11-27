import http from '@/api/http'

// 组织架构：部门树 + 部门权限 + Namespace 授权
export const listDepartments = () => http.get('/org/departments')
export const createDepartment = (data) => http.post('/org/departments', data)
export const updateDepartment = (id, data) => http.put(`/org/departments/${id}`, data)
export const deleteDepartment = (id) => http.delete(`/org/departments/${id}`)
export const reorderDepartments = (data) => http.post('/org/departments/reorder', data)

export const getDeptPermissions = (id) => http.get(`/org/departments/${id}/permissions`)
export const setDeptPermission = (id, data) => http.put(`/org/departments/${id}/permissions`, data)

export const setUserDepartment = (uid, departmentId) =>
  http.put(`/org/users/${uid}/department`, { department_id: departmentId })

export const listNamespaceGrants = (params) => http.get('/org/namespace-grants', { params })
export const upsertNamespaceGrant = (data) => http.post('/org/namespace-grants', data)
export const deleteNamespaceGrant = (id) => http.delete(`/org/namespace-grants/${id}`)
