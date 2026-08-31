import { defineConfig } from '@playwright/test'

// Runs against the real dev Compose stack (frontend:5173, go-api:8080, and
// the real asr-service/nlp-service workers) — not a mocked backend. A full
// run drives real ASR, diarization, role classification, single-pass
// extraction, and summary generation, so it is genuinely slow and spends
// real Groq quota, unlike a typical component test.
export default defineConfig({
  testDir: './e2e',
  timeout: 5 * 60 * 1000, // real model inference, not a mock — generous budget
  expect: { timeout: 15 * 1000 },
  fullyParallel: false,
  retries: 0,
  workers: 1,
  reporter: 'list',
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://localhost:5173',
    trace: 'retain-on-failure',
  },
})
