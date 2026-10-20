import { defineConfig } from '@playwright/test';
import { mkdtempSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { dynamicPortPair, shellQuote } from './playwright.local-stack';
import { returningUserStorageState } from './playwright.returning-user';
import { teamServerCommands } from './inventory-team';

// No standard E2E seed, cached workspace, credentials, or paid model runs.
if (process.env.TEST_WORKER_INDEX === undefined) {
  const workspace = mkdtempSync(path.join(os.tmpdir(), 'frisket-inventory-'));
  process.once('exit', () => rmSync(workspace, { recursive: true, force: true }));
  const { frontendPort, backendPort } = dynamicPortPair();
  const { frontendPort: storyPort } = dynamicPortPair();
  const teamPorts = dynamicPortPair();
  Object.assign(process.env, {
    FRISKET_INVENTORY_WORKSPACE: workspace,
    FRISKET_INVENTORY_APP_URL: `http://127.0.0.1:${frontendPort}`,
    FRISKET_INVENTORY_BACKEND_URL: `http://127.0.0.1:${backendPort}`,
    FRISKET_INVENTORY_STORY_URL: `http://127.0.0.1:${storyPort}`,
    FRISKET_INVENTORY_TEAM_URL: `http://127.0.0.1:${teamPorts.frontendPort}`,
    FRISKET_INVENTORY_TEAM_BACKEND_URL: `http://127.0.0.1:${teamPorts.backendPort}`,
  });
}
const workspace = process.env.FRISKET_INVENTORY_WORKSPACE!;
const baseURL = process.env.FRISKET_INVENTORY_APP_URL!;
const backendURL = process.env.FRISKET_INVENTORY_BACKEND_URL!;
const storyURL = process.env.FRISKET_INVENTORY_STORY_URL!;
const frontendPort = new URL(baseURL).port;
const backendPort = new URL(backendURL).port;
const storyPort = new URL(storyURL).port;
const python = process.env.FRISKET_INVENTORY_PYTHON ?? path.resolve('../.venv/bin/python');
const teamURL = process.env.FRISKET_INVENTORY_TEAM_URL!;
const teamBackendURL = process.env.FRISKET_INVENTORY_TEAM_BACKEND_URL!;
const team = teamServerCommands({ workspace, python, baseURL: teamURL, backendURL: teamBackendURL });

export default defineConfig({
  testDir: '../tests/inventory',
  globalSetup: '../tests/inventory/setup.ts',
  outputDir: '../test-results/inventory',
  workers: 1,
  retries: 0,
  timeout: 120_000,
  expect: { timeout: 15_000 },
  reporter: 'list',
  use: {
    actionTimeout: 15_000,
    baseURL,
    browserName: 'chromium',
    viewport: { width: 1440, height: 1000 },
    storageState: { cookies: [], origins: [
      ...returningUserStorageState(baseURL).origins, ...returningUserStorageState(teamURL).origins,
    ] },
    serviceWorkers: 'block',
    trace: 'retain-on-failure',
  },
  webServer: [
    { command: team.backend, url: `${teamBackendURL}/api/health`, timeout: 90_000, reuseExistingServer: false },
    { command: team.frontend, url: teamURL, timeout: 60_000, reuseExistingServer: false },
    {
      command: `env -i PATH=${shellQuote(process.env.PATH ?? '/usr/bin:/bin')} HOME=${shellQuote(workspace)} FRISKET_TELEMETRY=off PYTHONPATH=${shellQuote(path.resolve('../src'))} ${shellQuote(python)} ${shellQuote(path.resolve('tests/inventory/server.py'))} ${shellQuote(path.join(workspace, 'projects'))} ${backendPort} --yes --no-open`,
      url: `${backendURL}/api/health`, timeout: 90_000, reuseExistingServer: false,
    },
    {
      command: `FRISKET_EDITION=local FRISKET_BACKEND_URL=${shellQuote(backendURL)} npm run dev -- --host 127.0.0.1 --port ${frontendPort} --strictPort`,
      url: baseURL, timeout: 60_000, reuseExistingServer: false,
    },
    {
      command: `${shellQuote(python)} -m http.server ${storyPort} --bind 127.0.0.1 --directory ${shellQuote(path.resolve('storybook-static'))}`,
      url: storyURL, timeout: 30_000, reuseExistingServer: false,
    },
  ],
});
