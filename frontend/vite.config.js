import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import vuetify from 'vite-plugin-vuetify'
import { fileURLToPath, URL } from 'node:url'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [
    vue(),
    vuetify({ autoImport: true }),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    // Allow Cloudflare quick-tunnel hosts so remote reviewers can open the
    // app (vite otherwise 403s unknown Host headers). Remove for production
    // builds — this only affects the dev server.
    allowedHosts: ['.trycloudflare.com', 'localhost', '127.0.0.1'],
  },
})
