import { defineConfig, devices } from '@playwright/test';
import { localStack } from './playwright.local-stack';
import { returningUserStorageState } from './playwright.returning-user';

// Golden-project suite (goldens #1–2): LIVE model
// calls through the real UI, run on a weekly schedule by
// .github/workflows/goldens.yml and by hand via `npm run e2e:goldens`.
//
// GEMINI_API_KEY must be present in the environment that boots the backend
// (locally: `set -a; source .secrets/frisket.env; set +a`); the global setup
// refuses the run before any test starts when it is missing.
const stack = localStack();

export default defineConfig({
  testDir: '../tests/e2e',
  outputDir: '../test-results',
  testMatch: /golden-(tariff|ntsb)\.spec\.ts/,
  globalSetup: './goldens.global-setup.ts',
  // Sequential: DDG politeness plus honest, attributable spend per step.
  workers: 1,
  fullyParallel: false,
  // The tariff chain is one long test: 6-row live search + two live model
  // runs. Bounded well under the workflow's job timeout.
  timeout: 600_000,
  expect: { timeout: 15_000 },
  // No retries: a golden re-run burns real money and a flaky-green golden is
  // a dead alarm. Red means investigate.
  retries: 0,
  reporter: [['list'], ['./playwright.checklog-reporter.mjs']],
  use: {
    baseURL: stack.baseURL,
    storageState: returningUserStorageState(stack.baseURL),
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
    trace: 'retain-on-failure',
    screenshot: 'on',
    ...devices['Desktop Chrome'],
    viewport: { width: 1600, height: 900 },
  },
  webServer: stack.webServer,
});
