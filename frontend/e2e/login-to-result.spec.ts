import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { expect, test } from '@playwright/test'

// Runs the real vertical slice end to end against the live dev Compose
// stack: login -> create consultation with consent -> upload the fixture
// WAV -> watch real pipeline progress over SSE -> land on the result view.
// Uses the seeded doctor account (go/cmd/seed) and the same fixture
// scripts/e2e_smoke.py already uses for its own real-stack smoke test.

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const FIXTURE_WAV = path.resolve(__dirname, '../../scripts/fixtures/sample_consultation.wav')

const DOCTOR_EMAIL = 'doctor@coda.dev'
const DOCTOR_PASSWORD = 'coda-dev-password'

test('login through result view', async ({ page }) => {
  await page.goto('/')

  // Unauthenticated -> redirected to /login (ProtectedRoute).
  await expect(page).toHaveURL(/\/login$/)
  await page.getByLabel('Email').fill(DOCTOR_EMAIL)
  await page.getByLabel('Password').fill(DOCTOR_PASSWORD)
  await page.getByRole('button', { name: 'Log in' }).click()

  await expect(page).toHaveURL('/')
  await expect(page.getByRole('heading', { name: 'Consultations' })).toBeVisible()

  await page.getByRole('link', { name: 'New consultation' }).click()
  await expect(page.getByRole('heading', { name: 'New consultation' })).toBeVisible()

  // The consent checkbox gates the submit button — required before any
  // upload is reachable, per the task's explicit ask.
  const submit = page.getByRole('button', { name: 'Create and continue to upload' })
  await expect(submit).toBeDisabled()
  await page.getByRole('checkbox').check()
  await expect(submit).toBeEnabled()
  await submit.click()

  await expect(page).toHaveURL(/\/consultations\/[0-9a-f-]+\/upload$/)
  await expect(page.getByRole('heading', { name: 'Upload audio' })).toBeVisible()

  await page.locator('input[type="file"]').setInputFiles(FIXTURE_WAV)

  // Real upload progress, then real pipeline processing — this is a live
  // stack, not a mock, so this genuinely takes a while.
  await expect(page).toHaveURL(/\/consultations\/[0-9a-f-]+\/jobs\/[0-9a-f-]+$/, {
    timeout: 60_000,
  })
  await expect(page.getByRole('heading', { name: 'Processing' })).toBeVisible()

  // The job status page redirects to /result once the real pipeline
  // reaches a result-ready state (awaiting_review or later) — driven by
  // the real SSE stream, real ASR/diarization/extraction/summary.
  await expect(page).toHaveURL(/\/consultations\/[0-9a-f-]+\/result$/, { timeout: 4 * 60_000 })

  await expect(page.getByRole('heading', { name: 'Transcript' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Clinical note' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Summary' })).toBeVisible()
  await expect(page.getByText('Chief Complaint')).toBeVisible()
  await expect(page.getByText('Treatment Plan & Advice')).toBeVisible()
})
