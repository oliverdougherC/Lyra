import { fileURLToPath, URL } from 'node:url'

import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
      // Only the eager, layered entry loads the vendor CSS. Crepe's theme also imports
      // the bare specifier, which would otherwise add an unlayered copy on navigation.
      'katex/dist/katex.min.css': fileURLToPath(
        new URL('./src/styles/katex-already-loaded.css', import.meta.url),
      ),
      '@lyra/katex-vendor.css': fileURLToPath(
        new URL('./node_modules/katex/dist/katex.min.css', import.meta.url),
      ),
    },
  },
  server: {
    host: '127.0.0.1',
  },
  preview: {
    host: '127.0.0.1',
  },
})
