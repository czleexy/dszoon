import axios from 'axios'
import { ElMessage } from 'element-plus'

// 生产：前端由后端同一个端口提供，用相对路径 /api/v1
// 开发：vite proxy 把 /api 转到 127.0.0.1:8000
export const API_BASE = import.meta.env.VITE_API_BASE || '/api/v1'

export const TOKEN_KEY = 'dszoon_token'

/**
 * 401 时的清理回调。
 *
 * 为什么不直接 import auth store：store → endpoints → client 会形成循环依赖。
 * 这里留一个注册点，由 main.js 在启动时注入（那里没有循环依赖问题）。
 * 只清 localStorage 是不够的——Pinia 里的 token 还在，路由守卫会以为"还登录着"，
 * 于是多绕一次请求才跳登录页。
 */
let onUnauthorized = null

export function setUnauthorizedHandler(handler) {
  onUnauthorized = handler
}

export const http = axios.create({
  baseURL: API_BASE,
  timeout: 60000
})

http.interceptors.request.use((config) => {
  const token = localStorage.getItem(TOKEN_KEY)
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

http.interceptors.response.use(
  (response) => response,
  (error) => {
    const status = error.response?.status
    const detail = error.response?.data?.detail
    if (status === 401) {
      localStorage.removeItem(TOKEN_KEY)
      onUnauthorized?.()
      // 避免在登录页本身弹出"未提供访问令牌"
      if (!window.location.hash.includes('/login')) {
        ElMessage.error(detail || '登录已过期，请重新登录')
        window.location.hash = '#/login'
      }
    } else if (status === 403) {
      ElMessage.error(detail || '没有权限执行该操作')
    } else if (status && status >= 400) {
      const extra = error.response?.data?.extra
      const extraText = Array.isArray(extra)
        ? extra.map((item) => `${(item.loc || []).slice(-1)[0]}: ${item.msg}`).join('；')
        : ''
      ElMessage.error(`${detail || '请求失败'}${extraText ? `（${extraText}）` : ''}`)
    } else {
      ElMessage.error(error.message || '网络错误')
    }
    return Promise.reject(error)
  }
)

export default http
