import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  testMatch: 'update.spec.mjs',
  workers: 1,
  retries: 0,
  captureGitInfo: { commit: false, diff: false },
  timeout: 600_000,
  expect: { timeout: 20_000 },
  reporter: [['list'], ['html', { open: 'never', outputFolder: 'playwright-update-report' }]],
  outputDir: 'update-test-results',
});
