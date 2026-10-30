import path from 'node:path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig({
  // Shown in the profile menu ("Build ...") so you can tell whether a rebuild is live.
  define: {
    __BUILD_TIME__: JSON.stringify(new Date().toISOString()),
  },
  plugins: [
    react(),
    tailwindcss(),
    {
      // Dev server only: keep browser dark-mode extensions from re-theming the
      // app while its own light/dark themes are being reviewed. Not in dist.
      name: 'dev-darkreader-lock',
      apply: 'serve',
      transformIndexHtml: () => [{ tag: 'meta', attrs: { name: 'darkreader-lock' }, injectTo: 'head' }],
    },
  ],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    proxy: {
      '/api': {
        // Override with VITE_API_TARGET to point the dev server at a mock API.
        target: process.env.VITE_API_TARGET ?? 'http://127.0.0.1:8080',
        changeOrigin: true,
        ws: true,
      },
    },
  },
})
