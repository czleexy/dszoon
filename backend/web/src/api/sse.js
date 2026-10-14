import { API_BASE, TOKEN_KEY } from './client'

/**
 * 用 fetch 手工解析 SSE。
 *
 * 为什么不用原生 EventSource：它**不支持自定义请求头**，而我们的接口需要
 * `Authorization: Bearer`。用 fetch + ReadableStream 自己切分 SSE 帧即可，
 * 既保留了实时进度，也不用为了兼容 EventSource 而在后端开一个"令牌放 query"
 * 的口子（那会把令牌写进访问日志）。
 *
 * @returns 取消函数
 */
export function streamTask(taskId, { onEvent, onError, onDone } = {}) {
  const controller = new AbortController()
  const token = localStorage.getItem(TOKEN_KEY)

  fetch(`${API_BASE}/backups/${taskId}/stream`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    signal: controller.signal
  })
    .then(async (response) => {
      if (!response.ok) {
        throw new Error(`SSE 连接失败：HTTP ${response.status}`)
      }
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })

        let index
        // SSE 以空行分隔事件
        while ((index = buffer.indexOf('\n\n')) >= 0) {
          const chunk = buffer.slice(0, index)
          buffer = buffer.slice(index + 2)
          const isDone = /^event:\s*done/m.test(chunk)
          const payload = chunk
            .split('\n')
            .filter((line) => line.startsWith('data:'))
            .map((line) => line.slice(5).trim())
            .join('')
          if (payload && payload !== '{}') {
            try {
              onEvent?.(JSON.parse(payload))
            } catch {
              // 心跳或非 JSON 帧，忽略
            }
          }
          if (isDone) {
            onDone?.()
            return
          }
        }
      }
      onDone?.()
    })
    .catch((error) => {
      if (error.name !== 'AbortError') {
        onError?.(error)
      }
    })

  return () => controller.abort()
}
