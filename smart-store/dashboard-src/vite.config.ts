import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { resolve } from 'path';

// FastAPI serves the dashboard at `/dashboard/` (StaticFiles mount in app/main.py).
// We build with `base: '/dashboard/'` so the asset URLs resolve correctly when
// loaded from that path. The vite outDir overwrites the existing dashboard/
// folder — emptyOutDir ensures the previous vanilla index.html + app.js are
// removed.
export default defineConfig({
  plugins: [react()],
  base: '/dashboard/',
  build: {
    outDir: resolve(__dirname, '../dashboard'),
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    port: 5173,
    proxy: {
      '/stores':  { target: 'http://localhost:8000', changeOrigin: true },
      '/health':  { target: 'http://localhost:8000', changeOrigin: true },
      '/events':  { target: 'http://localhost:8000', changeOrigin: true },
      '/ws':      { target: 'ws://localhost:8000', ws: true },
    },
  },
});
