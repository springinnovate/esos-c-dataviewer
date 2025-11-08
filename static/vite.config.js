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
    minify: false, //'esbuild',
    cssMinify: false, //true,
    sourcemap: true, //false,
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
    keepNames: true//false
  }
})
