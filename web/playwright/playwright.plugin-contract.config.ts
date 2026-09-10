import { defineConfig, devices } from '@playwright/test';
import { localStack } from './playwright.local-stack';

const configuredWorkers = Number(process.env.FRISKET_PLUGIN_CONTRACT_WORKERS ?? '1');
const workers = Number.isFinite(configuredWorkers) && configuredWorkers > 0 ? configuredWorkers : 1;
const stack = localStack();

export default defineConfig({
  testDir: '../tests/plugin-contract',
  outputDir: '../test-results',
  workers,
  fullyParallel: false,
  timeout: 90_000,
  expect: { timeout: 10_000 },
  retries: 0,
  reporter: [['list'], ['./playwright.checklog-reporter.mjs']],
  use: {
    baseURL: stack.baseURL,
    actionTimeout: 8_000,
    navigationTimeout: 20_000,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    launchOptions: {
      args: [
        '--autoplay-policy=no-user-gesture-required',
        '--enable-unsafe-swiftshader',
        '--ignore-gpu-blocklist',
      ],
    },
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer: stack.webServer,
});
