import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  test: {
    // Without this, vitest's default include glob also picks up
    // e2e/*.spec.ts — Playwright specs, whose test() API vitest doesn't
    // understand, so `npm run test` crashed trying to collect them.
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
  },
})
