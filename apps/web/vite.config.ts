import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Dev proxy: the SPA talks to the real FastAPI router, never to a mock server.
// SSE (/api/v1/events/stream) streams through because http-proxy does not buffer.
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      // Root-level system endpoints used by the /system page.
      '/health': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      '/version': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
