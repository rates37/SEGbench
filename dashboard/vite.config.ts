import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { defineConfig } from 'vite'

// Relative base so the static build can be opened from file:// or served from any subpath.
export default defineConfig({
  base: './',
  plugins: [react(), tailwindcss()],
})
