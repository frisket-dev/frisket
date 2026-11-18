// Idempotent seeding: the read-only specs (search, review, ...) expect the
// demo projects from scripts/e2e/seed_demo.py. Seeding makes real model calls
// (~$0.25), so it runs ONCE into the persistent e2e workspace and is skipped
// whenever the projects already exist. Either way the setup then opens each
// seeded project and records which ones are unusable (absent, or created by an
// incompatible build), so the specs that need them fail at once with that
// reason while every other spec still runs.
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test as setup, type APIRequestContext } from '@playwright/test';
import { testPython } from '../support/testPython';
import { SEED_REMEDY, SEEDED_PROJECTS, type SeedStatus, writeSeedStatus } from './seedProjects';

const here = path.dirname(fileURLToPath(import.meta.url));
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

async function projectIds(request: APIRequestContext): Promise<Map<string, string>> {
  const res = await request.get('/api/projects');
  if (!res.ok()) throw new Error(`GET /api/projects answered HTTP ${res.status()}`);
  const ids = new Map<string, string>();
  for (const project of (await res.json()) as Array<{ id: string; name: string }>) {
    if (!ids.has(project.name)) ids.set(project.name, project.id);
  }
  return ids;
}

// Runs the paid seeder; returns why the projects are missing when it cannot.
async function seed(): Promise<string | null> {
  if (process.env.FRISKET_CACHE_MODE === 'replay_strict') {
    return 'absent, and seeding makes live model calls, which FRISKET_CACHE_MODE=replay_strict forbids';
  }
  const backendURL = process.env.FRISKET_E2E_BACKEND_URL ?? 'http://127.0.0.1:8000';
  try {
    execFileSync(testPython(), ['scripts/e2e/seed_demo.py', backendURL], {
      cwd: path.resolve(here, '../../..'),
      stdio: 'inherit',
      timeout: 600_000,
    });
  } catch (error) {
    return `seeding failed (${error instanceof Error ? error.message.split('\n')[0] : String(error)})`;
  }
  await publishSeedToBaseWorkspace();
  return null;
}

async function openFailure(
  request: APIRequestContext,
  id: string | undefined,
  whyAbsent: string | null,
): Promise<string | null> {
  if (id === undefined) return whyAbsent ?? 'absent from the seeded workspace';
  const res = await request.get(`/api/projects/${encodeURIComponent(id)}/sheets`);
  if (res.ok()) return null;
  const body = (await res.json().catch(() => null)) as { detail?: unknown } | null;
  const detail = typeof body?.detail === 'string' ? body.detail : res.statusText();
  return `opening it answers HTTP ${res.status()}: ${detail}`;
}

setup('seed demo projects (once per workspace)', async ({ request }) => {
  setup.setTimeout(600_000); // first-ever seed: model runs + local transcription
  let ids = await projectIds(request);
  let whyAbsent: string | null = null;
  // pin the LAST project the seeder creates, so a partially-failed seed
  // retries instead of being mistaken for done
  if (!ids.has(SEED_MARKER_PROJECT)) {
    whyAbsent = await seed();
    ids = await projectIds(request);
  }
  const status: SeedStatus = {};
  for (const name of SEEDED_PROJECTS) {
    status[name] = await openFailure(request, ids.get(name), whyAbsent);
  }
  writeSeedStatus(status);
  const byReason = new Map<string, string[]>();
  for (const name of SEEDED_PROJECTS) {
    const reason = status[name];
    if (reason) byReason.set(reason, [...(byReason.get(reason) ?? []), name]);
  }
  const unusable = [...byReason.values()].flat();
  if (unusable.length) {
    const reasons = [...byReason].map(([reason, names]) => `${names.map((n) => `"${n}"`).join(', ')}: ${reason}`);
    const message =
      `${unusable.length} of ${SEEDED_PROJECTS.length} seeded e2e projects are unusable, and the specs ` +
      `that open them will fail at once. ${reasons.join('. ')}. To restore them: ${SEED_REMEDY}.`;
    console.warn(message);
    setup.info().annotations.push({ type: 'seed', description: message });
  }
});
