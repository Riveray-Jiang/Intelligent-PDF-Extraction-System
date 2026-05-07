import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.HITL_BACKEND_URL ?? 'http://127.0.0.1:8892',
        changeOrigin: true,
        secure: false,
      },
    },
  },
})
