import { randomUUID } from 'node:crypto';
import { rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { defineConfig, devices } from '@playwright/test';

import {
  durationWrappedServerCommand,
  dynamicPortPair,
  shellQuote,
} from './playwright.local-stack';

const WEB_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const ROOT = path.resolve(WEB_DIR, '..');
const { backendPort } = dynamicPortPair();
const baseURL = `http://127.0.0.1:${backendPort}`;
const runtime = path.join(os.tmpdir(), `frisket-cloud-connection-e2e-${process.pid}-${randomUUID()}`);
const workspace = path.join(runtime, 'workspace');
const staticDir = process.env.FRISKET_E2E_CLOUD_STATIC_DIR
  ?? path.join(WEB_DIR, 'dist', 'local');

process.once('exit', () => rmSync(runtime, { recursive: true, force: true }));

const python = process.env.FRISKET_TEST_PYTHON
  ? shellQuote(process.env.FRISKET_TEST_PYTHON)
  : 'uv run --no-sync python';
const backendCommand = [
  'cd', shellQuote(ROOT), '&&',
  `PYTHONPATH=${shellQuote(path.join(ROOT, 'src'))}`,
  'PYTHONDONTWRITEBYTECODE=1',
  'UV_NO_SYNC=1',
  python,
  shellQuote(path.join(ROOT, 'scripts/e2e/cloud_connection_app.py')),
  '--port', String(backendPort),
  '--workspace', shellQuote(workspace),
  '--static-dir', shellQuote(staticDir),
].join(' ');

// This project keeps the public runtime boundary intact: a real static build,
// browser API calls, encrypted profile storage, selector projection, worker,
// and result materialization. The Python fixture replaces only the external
// Cloud device/session/capability and paid-execution transports.
export default defineConfig({
  testDir: '../tests/e2e',
  testMatch: /cloud-connection\.spec\.ts/,
  outputDir: '../test-results/cloud-connection',
  workers: 1,
  fullyParallel: false,
  timeout: 120_000,
  expect: { timeout: 15_000 },
  retries: process.env.CI ? 1 : 0,
  reporter: [['list'], ['./playwright.checklog-reporter.mjs']],
  use: {
    baseURL,
    actionTimeout: 10_000,
    navigationTimeout: 20_000,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    ...devices['Desktop Chrome'],
    viewport: { width: 1600, height: 900 },
  },
  webServer: {
    command: durationWrappedServerCommand({
      phase: 'cloud_connection_backend_boot',
      name: 'cloud-connection-backend',
      url: `${baseURL}/api/health`,
      timeoutMs: 60_000,
      command: backendCommand,
    }),
    cwd: WEB_DIR,
    url: `${baseURL}/api/health`,
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
