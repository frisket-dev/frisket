// Idempotent seeding: the read-only specs (search, review, ...) expect the
// demo projects from scripts/e2e/seed_demo.py. Seeding makes real model calls
// (~$0.25), so it runs ONCE into the persistent e2e workspace and is skipped
// whenever the projects already exist.
import { execSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test as setup } from '@playwright/test';

const here = path.dirname(fileURLToPath(import.meta.url));
const shellQuote = (value: string) => `'${value.replace(/'/g, "'\\''")}'`;
const SEED_MARKER_PROJECT = 'People mentioned';

function sleep(ms: number) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

function workspaceHasSeed(workspace: string): boolean {
  if (!fs.existsSync(workspace)) return false;
  for (const entry of fs.readdirSync(workspace, { withFileTypes: true })) {
    if (!entry.isDirectory() || !entry.name.endsWith('.frisket')) continue;
    const manifestPath = path.join(workspace, entry.name, 'manifest.json');
    try {
      const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8')) as { name?: string };
      if (manifest.name === SEED_MARKER_PROJECT) return true;
    } catch {
      // Ignore partial project bundles; the seeder will repair an incomplete base.
    }
  }
  return false;
}

async function acquireSeedPublishLock(lockPath: string): Promise<() => void> {
  const staleAfterMs = 60 * 60 * 1000;
  for (let attempt = 0; attempt < 600; attempt += 1) {
    try {
      fs.mkdirSync(lockPath);
      return () => fs.rmSync(lockPath, { recursive: true, force: true });
    } catch (error) {
      if (!(error instanceof Error) || !('code' in error) || error.code !== 'EEXIST') {
        throw error;
      }
      try {
        const stat = fs.statSync(lockPath);
        if (Date.now() - stat.mtimeMs > staleAfterMs) {
          fs.rmSync(lockPath, { recursive: true, force: true });
          continue;
        }
      } catch {
        // The lock disappeared between mkdir attempts; try again.
      }
      await sleep(250);
    }
  }
  throw new Error(`Timed out waiting for e2e seed publish lock: ${lockPath}`);
}

async function publishSeedToBaseWorkspace() {
  const workspace = process.env.FRISKET_E2E_WS;
  const baseWorkspace = process.env.FRISKET_E2E_BASE_WS;
  if (process.env.FRISKET_E2E_ISOLATED !== '1' || !workspace || !baseWorkspace) return;
  if (path.resolve(workspace) === path.resolve(baseWorkspace)) return;
  fs.mkdirSync(baseWorkspace, { recursive: true });
  const release = await acquireSeedPublishLock(path.join(baseWorkspace, '.seed-publish.lock'));
  try {
    if (workspaceHasSeed(baseWorkspace)) return;
    fs.cpSync(workspace, baseWorkspace, {
      recursive: true,
      force: true,
      filter: (src) => {
        const basename = path.basename(src);
        return !basename.startsWith('.queue.db') && basename !== '.seed-publish.lock';
      },
    });
  } finally {
    release();
  }
}

setup('seed demo projects (once per workspace)', async ({ request }) => {
  setup.setTimeout(600_000); // first-ever seed: model runs + local transcription
  const res = await request.get('/api/projects');
  const projects = (await res.json()) as Array<{ name: string }>;
  // pin the LAST project the seeder creates, so a partially-failed seed
  // retries instead of being mistaken for done
  if (projects.some((p) => p.name === 'People mentioned')) return; // seeded
  const backendURL = process.env.FRISKET_E2E_BACKEND_URL ?? 'http://127.0.0.1:8000';
  execSync(`uv run python scripts/e2e/seed_demo.py ${shellQuote(backendURL)}`, {
    cwd: path.resolve(here, '../../..'),
    stdio: 'inherit',
    timeout: 600_000,
  });
  await publishSeedToBaseWorkspace();
});
