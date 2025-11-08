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
    minify: 'esbuild',
    cssMinify: true,
    sourcemap: false,
    lib: {
      entry: path.resolve(__dirname, 'app.js'),
      formats: ['es'],
      name: 'viewer'
    },
    rollupOptions: {
      output: {
        entryFileNames: 'app-[hash].js',
        chunkFileNames: 'chunks/[name]-[hash].js',
        assetFileNames: 'assets/[name]-[hash][extname]',
        },
    },
  },
  esbuild: {
    keepNames: false
  }
})
