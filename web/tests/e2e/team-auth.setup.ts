// Real single-org team admin session, seeded once per run before the
// team-only specs (see scripts/e2e/e2e_team_seed.py for why: two of them,
// admin-users and audit-log-viewer, navigate straight to /admin without
// mocking /api/me themselves, so they need a real authenticated cookie
// against the real frisket.team.asgi:app backend playwright.team-stack.ts
// booted).
import { execSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test as setup } from '@playwright/test';

const here = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(here, '../../..');
const shellQuote = (value: string) => `'${value.replace(/'/g, "'\\''")}'`;

setup('seed a real single-org team admin session', async () => {
  setup.setTimeout(60_000);
  const storageStatePath = process.env.FRISKET_TEAM_STORAGE_STATE;
  const baseURL = process.env.FRISKET_TEAM_BACKEND_URL;
  if (!storageStatePath || !baseURL) {
    throw new Error(
      'team-auth.setup.ts requires FRISKET_TEAM_STORAGE_STATE and FRISKET_TEAM_BACKEND_URL ' +
        '(set by playwright.team-stack.ts teamStack())',
    );
  }
  execSync(
    'uv run python scripts/e2e/e2e_team_seed.py ' +
      `--storage-state ${shellQuote(storageStatePath)} --base-url ${shellQuote(baseURL)}`,
    { cwd: ROOT, stdio: 'inherit', timeout: 60_000 },
  );
});
