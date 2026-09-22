import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The API runs separately (`make api`). Proxying keeps the browser on one
// origin, so there is no CORS config to carry into Fargate later.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { '/api': { target: 'http://localhost:8000', rewrite: p => p.replace(/^\/api/, '') } } },
})
