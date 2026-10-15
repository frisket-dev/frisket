import { defineConfig, devices } from '@playwright/test';
import { returningUserStorageState } from './playwright.returning-user';

const baseURL = 'http://localhost:5173';

// Config for `npm run gallery` — a single sequential walk through every major
// view that saves labeled full-page screenshots to screenshots/gallery/.
// Same live stack as the e2e suite (see tests/README.md).
export default defineConfig({
  testDir: '../tests',
  testMatch: 'gallery.ts',
  outputDir: '../test-results',
  workers: 1,
  timeout: 600_000,
  expect: { timeout: 20_000 },
  retries: 0,
  reporter: [['list'], ['./playwright.checklog-reporter.mjs']],
  use: {
    baseURL,
    storageState: returningUserStorageState(baseURL),
    viewport: { width: 1440, height: 900 },
    trace: 'off',
    screenshot: 'off',
    launchOptions: {
      args: ['--autoplay-policy=no-user-gesture-required'],
    },
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } }],
  webServer: {
    command: 'npm run dev',
    url: 'http://localhost:5173',
    reuseExistingServer: true,
    timeout: 30_000,
  },
});
