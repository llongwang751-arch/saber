import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 开发期：Vite dev server 跑 :5174，把后端接口代理到 127.0.0.1:8090（同源，免 CORS）。
// 生产期：npm run build 产出 dist/，由 FastAPI 静态托管（与 Vue 版同一挂载约定）。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8090', changeOrigin: true },
      '/healthz': { target: 'http://127.0.0.1:8090', changeOrigin: true },
      '/readyz': { target: 'http://127.0.0.1:8090', changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})
