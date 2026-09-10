import { defineConfig, devices } from '@playwright/test';
import { localStack } from './playwright.local-stack';

const stack = process.env.FRISKET_E2E_BASE_URL ? undefined : localStack();
const baseURL = process.env.FRISKET_E2E_BASE_URL ?? stack?.baseURL ?? 'http://127.0.0.1:5173';
const local = /^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?\b/.test(baseURL);

if (!local && process.env.FRISKET_E2E_CONFIRM_NONLOCAL !== '1') {
  throw new Error(
    'Refusing to run smoke e2e against a non-local FRISKET_E2E_BASE_URL without FRISKET_E2E_CONFIRM_NONLOCAL=1',
  );
}

export default defineConfig({
  testDir: '../tests/e2e',
  outputDir: '../test-results',
  testMatch: /deploy-smoke\.spec\.ts/,
  workers: 1,
  fullyParallel: false,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  retries: process.env.CI ? 1 : 0,
  reporter: [['list'], ['./playwright.checklog-reporter.mjs']],
  use: {
    baseURL,
    actionTimeout: 8_000,
    navigationTimeout: 20_000,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    ...devices['Desktop Chrome'],
  },
  webServer: local && stack ? stack.webServer : undefined,
});
