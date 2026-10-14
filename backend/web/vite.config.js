import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 构建产物落到 web/dist，后端 app/main.py 会自动挂载这个目录（同一个端口提供前后端）。
// 开发时用 proxy 把 /api 转到本地 uvicorn，避免跨域配置。
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true
      }
    }
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    chunkSizeWarningLimit: 1500
  }
})
