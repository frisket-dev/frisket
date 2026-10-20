import path from 'node:path';
import { shellQuote } from './playwright.local-stack';

/** A fresh real Team edition, without normal E2E seeding, uv sync, or inherited keys. */
export function teamServerCommands(options: {
  workspace: string; python: string; baseURL: string; backendURL: string;
}) {
  const { workspace, python, baseURL, backendURL } = options;
  const root = path.resolve('..');
  const storageState = path.join(workspace, 'inventory-team-session.json');
  const env = {
    PATH: process.env.PATH ?? '/usr/bin:/bin', HOME: workspace,
    PYTHONPATH: path.join(root, 'src'), PYTHONNOUSERSITE: '1',
    FRISKET_TELEMETRY: 'off',
    FRISKET_TEAM_DATABASE_URL: `sqlite:///${path.join(workspace, 'inventory-team.db')}`,
    FRISKET_RUN_QUEUE_DATABASE_URL: `sqlite:///${path.join(workspace, 'inventory-team-queue.db')}`,
    FRISKET_DATA_DIR: path.join(workspace, 'inventory-team-data'),
    FRISKET_SECRETS_KEY_FILE: path.join(workspace, 'inventory-team.key'),
    FRISKET_ORGANIZATION_NAME: 'Inventory team',
    FRISKET_ADMIN_EMAILS: 'owner@example.test',
    FRISKET_BASE_URL: backendURL,
  };
  const environment = Object.entries(env).map(([key, value]) => `${key}=${shellQuote(value)}`).join(' ');
  return {
    backend: `env -i ${environment} ${shellQuote(python)} ${shellQuote(path.resolve('tests/inventory/server.py'))} --team ${new URL(backendURL).port}`,
    frontend: `FRISKET_EDITION=team FRISKET_BACKEND_URL=${shellQuote(backendURL)} npm run dev -- --host 127.0.0.1 --port ${new URL(baseURL).port} --strictPort`,
    seed: {
      command: python,
      args: [path.join(root, 'scripts/e2e/e2e_team_seed.py'), '--storage-state', storageState, '--base-url', baseURL],
      env, storageState,
    },
  };
}
