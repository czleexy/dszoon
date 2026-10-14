/** 时间与状态展示工具。 */

/**
 * 后端统一输出 UTC 的 ISO8601（带 Z），这里转成本地时间显示。
 * 之所以要显式处理：库里存的是朴素 UTC，前端如果不当成本地时间就会差 8 小时。
 */
export function formatTime(value) {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  const pad = (n) => String(n).padStart(2, '0')
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  )
}

export function formatDuration(ms) {
  if (ms === null || ms === undefined) return '-'
  if (ms < 1000) return `${ms} ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`
  return `${Math.floor(ms / 60000)} 分 ${Math.round((ms % 60000) / 1000)} 秒`
}

export function formatBytes(bytes) {
  if (!bytes) return '0 B'
  const units = ['B', 'KB', 'MB']
  let value = bytes
  let index = 0
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024
    index += 1
  }
  return `${value.toFixed(index === 0 ? 0 : 1)} ${units[index]}`
}

//: 采集状态 → 展示标签
export const RUN_STATUS = {
  success: { label: '成功', type: 'success' },
  unchanged: { label: '无变更', type: 'info' },
  auth_failed: { label: '认证失败', type: 'danger' },
  unreachable: { label: '不可达', type: 'danger' },
  timeout: { label: '超时', type: 'warning' },
  session_closed: { label: '连接中断', type: 'warning' },
  integrity_error: { label: '内容不可信', type: 'danger' },
  unsupported: { label: '不支持', type: 'info' },
  error: { label: '错误', type: 'danger' }
}

export const TASK_STATUS = {
  queued: { label: '排队中', type: 'info' },
  running: { label: '运行中', type: 'primary' },
  success: { label: '全部成功', type: 'success' },
  partial: { label: '部分成功', type: 'warning' },
  failed: { label: '失败', type: 'danger' },
  cancelled: { label: '已取消', type: 'info' }
}

export function runStatusLabel(status) {
  return RUN_STATUS[status]?.label || status
}

export function runStatusType(status) {
  return RUN_STATUS[status]?.type || 'info'
}

export function taskStatusLabel(status) {
  return TASK_STATUS[status]?.label || status
}

export function taskStatusType(status) {
  return TASK_STATUS[status]?.type || 'info'
}
