import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { resolve } from 'node:path'

const backendURL = process.env.FRISKET_BACKEND_URL ?? 'http://127.0.0.1:8000'
const edition = process.env.FRISKET_EDITION ?? 'local'
const editionEntryMarker = '/src/entries/__frisket_edition__.tsx'

if (edition !== 'local' && edition !== 'team') {
  throw new Error(`Unsupported FRISKET_EDITION: ${edition}`)
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    react(),
    {
      name: 'frisket-edition-launcher',
      transformIndexHtml: {
        order: 'pre',
        handler(html) {
          if (!html.includes(editionEntryMarker)) {
            throw new Error('Missing Frisket edition entry marker')
          }
          return html.replace(editionEntryMarker, `/src/entries/${edition}.tsx`)
        },
      },
    },
  ],
  build: {
    outDir: `dist/${edition}`,
    emptyOutDir: true,
    manifest: true,
    rollupOptions: { input: resolve(__dirname, 'index.html') },
  },
  server: {
    proxy: {
      // Dev proxy to the frisket FastAPI server.
      '/api': backendURL,
    },
  },
})
