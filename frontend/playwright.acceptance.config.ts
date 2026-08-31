/**
 * Playwright config for real-stack acceptance tests.
 *
 * Unlike the smoke tests in playwright.config.ts, these tests do NOT intercept
 * /api/** traffic.  The global setup starts the real FastAPI backend (with
 * deterministic embedding fixtures), a fake tutor endpoint, and the production
 * Vite frontend. Every browser request flows through the real application
 * stack, exercising the full composition.
 *
 * Browser-support contract:
 *   Chromium — required CI gate on every PR (ci.yml "acceptance" lane).
 *   WebKit   — scheduled weekly on macOS (webkit-acceptance.yml), opt-in
 *              locally via ACCEPTANCE_WEBKIT=1. Not a merge blocker.
 *
 * Run:  pnpm test:acceptance
 * Local: ../scripts/run-acceptance.sh
 */

import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './e2e/acceptance',
  fullyParallel: false, // serial within each file for stateful journeys
  workers: 1, // shared backend state (settings, tutor fixture mode) requires serial files
  forbidOnly: !!process.env.CI,
  retries: 0, // no retries — flakes must be fixed, not hidden
  timeout: 60_000,
  expect: { timeout: 15_000 },

  reporter: process.env.CI
    ? [['github'], ['html', { open: 'never', outputFolder: '../acceptance-report' }]]
    : 'list',

  globalSetup: './e2e/acceptance/global-setup.ts',
  globalTeardown: './e2e/acceptance/global-teardown.ts',

  use: {
    ...devices['Desktop Chrome'],
    baseURL: `http://127.0.0.1:${process.env.ACCEPTANCE_FRONTEND_PORT ?? '3000'}`,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
    // Do NOT set up any route interception — the whole point is real traffic
  },

  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
    ...(process.env.ACCEPTANCE_WEBKIT
      ? [
          {
            name: 'webkit',
            use: { ...devices['Desktop Safari'] },
          },
        ]
      : []),
  ],
})
