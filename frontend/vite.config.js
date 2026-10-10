import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Backend is proxied through the dev server so one URL (and one Cloudflare tunnel) serves both the
// dashboard and the API, with no CORS. Override with VITE_BACKEND_URL when the backend runs elsewhere,
// e.g. the hosted server: VITE_BACKEND_URL=https://cloudmorph.34-64-253-41.sslip.io
const backend = process.env.VITE_BACKEND_URL || 'http://127.0.0.1:8000'
// Optional: attach the API token on the proxy hop so a local dashboard can drive the hosted server without
// typing the token in the browser. Read from the environment only; never commit it.
const token = process.env.CLOUDMORPH_API_TOKEN
const route = { target: backend, changeOrigin: true, ...(token ? { headers: { 'X-API-Token': token } } : {}) }

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    allowedHosts: true, // *.trycloudflare.com hostnames are random; Vite would otherwise reject them
    proxy: {
      '/deploy': route,
      '/health': route,
      '/fleet': route,
      '/projects': route,
      '/webhook': { target: backend, changeOrigin: true }, // GitHub push webhook: signed, never gets our token
    },
  },
})
