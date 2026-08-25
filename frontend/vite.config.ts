import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  test: {
    // jsdom 而非 happy-dom：EventSource 与 aria 查询在 jsdom 上行为更接近真实浏览器，
    // 而这两者正是本单元要测的重点。
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    css: false,
  },
  server: {
    port: 5173,
    // 后端 API 与 SSE 走同源代理，避免前端处理 CORS。
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
