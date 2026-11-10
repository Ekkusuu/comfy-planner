import { defineConfig } from 'vite'
import { svelte } from '@sveltejs/vite-plugin-svelte'

// Dev: `npm run dev` proxies /api (including the SSE stream) to the FastAPI app.
export default defineConfig({
  plugins: [svelte()],
  server: { port: 5173, proxy: { '/api': 'http://127.0.0.1:8765' } },
  build: { outDir: 'dist', emptyOutDir: true },
})
