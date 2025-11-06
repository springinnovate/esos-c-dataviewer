import { defineConfig } from 'vite'
import path from 'node:path'

export default defineConfig({
  root: '.',
  base: '/static/',
  build: {
    manifest: 'manifest.json',
    outDir: 'dist',
    assetsDir: '',
    target: 'esnext',
    cssCodeSplit: true,
    minify: true,
    cssMinify: true,
    sourcemap: false,
    rollupOptions: {
      input: {
        app: path.resolve(__dirname, 'app.js')
      },
      output: {
        manualChunks: {
          leaflet: ['leaflet', 'proj4', 'proj4leaflet']
        }
      }
    }
  },
  esbuild: {
    keepNames: false
  }
})
