import { defineConfig, devices } from '@playwright/test'

const port = Number(process.env.PLAYWRIGHT_FRONTEND_PORT ?? '4179')

export default defineConfig({
  testDir: './e2e',
  testIgnore: '**/acceptance/**',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['github'], ['html', { open: 'never' }]] : 'list',
  use: {
    ...devices['Desktop Chrome'],
    baseURL: `http://127.0.0.1:${port}`,
    trace: 'on-first-retry',
  },
  webServer: {
    command: `./node_modules/.bin/vite preview --host 127.0.0.1 --port ${port} --strictPort`,
    port,
    // Never attach the smoke suite to an unrelated listener. A developer may already
    // have Lyra (or another app) on a common port; strict ownership is part of the
    // desktop lifecycle contract, and tests should model it too.
    reuseExistingServer: false,
    timeout: 120_000,
  },
})
