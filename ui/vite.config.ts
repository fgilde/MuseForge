import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 3000,
    // Defaults to a local `python launch.py`. Point it elsewhere to develop
    // against a running container, which publishes on 7861:
    //   MUSEFORGE_API=http://127.0.0.1:7861 npm run dev
    proxy: {
      '/api': process.env.MUSEFORGE_API || 'http://127.0.0.1:7860',
      '/classic': process.env.MUSEFORGE_API || 'http://127.0.0.1:7860',
    },
  },
  // Strip console.* and debugger statements from the production bundle.
  // Dev mode (npm run dev) is unaffected — esbuild `drop` only runs at
  // build time.
  esbuild: {
    drop: ['console', 'debugger'],
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
