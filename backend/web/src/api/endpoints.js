import http from './client'

// ---- 鉴权 ----
export const login = (payload) => http.post('/auth/login', payload).then((r) => r.data)
export const fetchMe = () => http.get('/auth/me').then((r) => r.data)
export const changePassword = (payload) =>
  http.post('/auth/change-password', payload).then((r) => r.data)

// ---- 用户（仅管理员）----
export const listUsers = (params) => http.get('/users', { params }).then((r) => r.data)
export const createUser = (payload) => http.post('/users', payload).then((r) => r.data)
export const updateUser = (id, params) =>
  http.patch(`/users/${id}`, null, { params }).then((r) => r.data)

// ---- 元信息 ----
export const listVendors = () => http.get('/meta/vendors').then((r) => r.data)
export const fetchStats = () => http.get('/meta/stats').then((r) => r.data)

// ---- 凭据 ----
export const listCredentials = () => http.get('/credentials').then((r) => r.data)
export const createCredential = (payload) => http.post('/credentials', payload).then((r) => r.data)
export const updateCredential = (id, payload) =>
  http.patch(`/credentials/${id}`, payload).then((r) => r.data)
export const deleteCredential = (id) => http.delete(`/credentials/${id}`).then((r) => r.data)

// ---- 设备 ----
export const listDevices = (params) => http.get('/devices', { params }).then((r) => r.data)
export const getDevice = (id) => http.get(`/devices/${id}`).then((r) => r.data)
export const createDevice = (payload) => http.post('/devices', payload).then((r) => r.data)
export const updateDevice = (id, payload) => http.patch(`/devices/${id}`, payload).then((r) => r.data)
export const deleteDevice = (id) => http.delete(`/devices/${id}`).then((r) => r.data)
export const importDevices = (rows) => http.post('/devices/import', rows).then((r) => r.data)
export const testDevice = (id) => http.post(`/devices/${id}/test`).then((r) => r.data)
export const backupDevice = (id, params) =>
  http.post(`/devices/${id}/backup`, null, { params }).then((r) => r.data)

// ---- 任务 ----
export const createBackup = (payload) => http.post('/backups', payload).then((r) => r.data)
export const listTasks = (params) => http.get('/backups', { params }).then((r) => r.data)
export const getTask = (id) => http.get(`/backups/${id}`).then((r) => r.data)
export const cancelTask = (id) => http.post(`/backups/${id}/cancel`).then((r) => r.data)
export const listRuns = (params) => http.get('/runs', { params }).then((r) => r.data)

// ---- 设备分组 ----
export const listGroups = () => http.get('/groups').then((r) => r.data)
export const createGroup = (payload) => http.post('/groups', payload).then((r) => r.data)
export const updateGroup = (id, payload) => http.patch(`/groups/${id}`, payload).then((r) => r.data)
export const deleteGroup = (id) => http.delete(`/groups/${id}`).then((r) => r.data)
export const setGroupDevices = (id, payload) =>
  http.put(`/groups/${id}/devices`, payload).then((r) => r.data)

// ---- 定时策略 ----
export const listSchedules = () => http.get('/schedules').then((r) => r.data)
export const createSchedule = (payload) => http.post('/schedules', payload).then((r) => r.data)
export const updateSchedule = (id, payload) =>
  http.patch(`/schedules/${id}`, payload).then((r) => r.data)
export const deleteSchedule = (id) => http.delete(`/schedules/${id}`).then((r) => r.data)
export const previewSchedule = (id, count = 5) =>
  http.get(`/schedules/${id}/preview`, { params: { count } }).then((r) => r.data)
export const runScheduleNow = (id) => http.post(`/schedules/${id}/run`).then((r) => r.data)

// ---- 通知渠道 ----
export const listChannels = () => http.get('/channels').then((r) => r.data)
export const createChannel = (payload) => http.post('/channels', payload).then((r) => r.data)
export const updateChannel = (id, payload) =>
  http.patch(`/channels/${id}`, payload).then((r) => r.data)
export const deleteChannel = (id) => http.delete(`/channels/${id}`).then((r) => r.data)
export const testChannel = (id) => http.post(`/channels/${id}/test`).then((r) => r.data)
export const fetchNotificationEvents = () => http.get('/notification-events').then((r) => r.data)
export const listNotificationLogs = (params) =>
  http.get('/notification-logs', { params }).then((r) => r.data)

// ---- 保留策略 ----
export const getRetention = () => http.get('/retention').then((r) => r.data)
export const updateRetention = (payload) => http.put('/retention', payload).then((r) => r.data)
export const runRetention = () => http.post('/retention/run').then((r) => r.data)

// ---- 版本与对比 ----
export const listSnapshots = (deviceId, params) =>
  http.get(`/devices/${deviceId}/snapshots`, { params }).then((r) => r.data)
export const getSnapshot = (id) => http.get(`/snapshots/${id}`).then((r) => r.data)
export const getSnapshotContent = (id, params) =>
  http.get(`/snapshots/${id}/content`, { params }).then((r) => r.data)
export const getDiff = (deviceId, params) =>
  http.get(`/devices/${deviceId}/diff`, { params }).then((r) => r.data)

//: 下载走浏览器原生下载（带 Authorization 头需要 blob，这里用一次性 URL 简单处理）
export async function downloadSnapshot(id, format, filename) {
  const response = await http.get(`/snapshots/${id}/content`, {
    params: { format, download: 1 },
    responseType: 'blob'
  })
  const url = window.URL.createObjectURL(new Blob([response.data]))
  const link = document.createElement('a')
  link.href = url
  link.download = filename || `config-${id}.cfg`
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
  window.URL.revokeObjectURL(url)
}
