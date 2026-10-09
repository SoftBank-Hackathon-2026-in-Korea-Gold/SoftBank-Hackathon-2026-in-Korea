import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Backend is proxied through the dev server so one URL (and one Cloudflare tunnel) serves both the
// dashboard and the API, with no CORS. Override with VITE_BACKEND_URL when the backend runs elsewhere.
const backend = process.env.VITE_BACKEND_URL || 'http://127.0.0.1:8000'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    allowedHosts: true, // *.trycloudflare.com hostnames are random; Vite would otherwise reject them
    proxy: {
      '/deploy': { target: backend, changeOrigin: true },
      '/health': { target: backend, changeOrigin: true },
    },
  },
})
