import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The API runs as a separate Python process (server.py). Proxying it under the
// same origin keeps the app free of base-URL configuration and CORS in the
// browser: in dev everything is same-origin, and a future build served by
// Python would be too.
export default defineConfig({
  plugins: [react()],
  build: {
    // Not the default `dist/`: that name belongs to the Python build, which
    // writes the workbench's wheel and sdist there. The compiled bundle is
    // force-included into the wheel under the same `web/` name, so the server
    // resolves it identically from a checkout and from an install.
    outDir: 'web',
    emptyOutDir: true,
  },
  server: {
    // Bind v4 explicitly: the default resolves 'localhost' to ::1 only, so the
    // 127.0.0.1 URL that every other part of this tool prints would refuse the
    // connection.
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.HIVELOOM_UI_API ?? 'http://127.0.0.1:8770',
        changeOrigin: true,
      },
    },
  },
})
