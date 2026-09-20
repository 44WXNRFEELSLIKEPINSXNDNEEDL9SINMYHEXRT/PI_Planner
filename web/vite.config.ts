import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Прокси /api -> локальный бэкенд: фронт не знает про порт и не требует CORS.
// На демо сервер отдаёт собранный dist сам, прокси нужен только в dev-режиме.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000' },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
