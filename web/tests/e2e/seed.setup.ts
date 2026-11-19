// Idempotent seeding: the read-only specs (search, review, ...) expect the
// demo projects from scripts/e2e/seed_demo.py. The seeder builds only the
// projects the workspace lacks, each on its own, and reports each one as
// complete, partial or missing; only complete ones are published to the
// persistent e2e workspace. With FRISKET_CACHE_MODE=replay_strict it builds
// them from fixture replies with no provider call ("Council audio" also needs
// the pinned Whisper base model, scripts/e2e/fetch_whisper_model.py);
// otherwise it makes real model and search calls (~$0.25). Either way the
// setup then opens each seeded project and records which ones are unusable
// (absent, partially built, or created by an incompatible build), so the specs
// that need them fail at once with that reason while every other spec still
// runs.
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test as setup, type APIRequestContext } from '@playwright/test';
import { testPython } from '../support/testPython';
import { SEED_REMEDY, SEEDED_PROJECTS, type SeedStatus, writeSeedStatus } from './seedProjects';

const here = path.dirname(fileURLToPath(import.meta.url));

function sleep(ms: number) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

// The seeder's report on one project (seed_demo.py --status-file).
type SeedOutcome = { state: 'complete' | 'partial' | 'missing' | 'present'; detail: string | null };
// project name -> why it is unusable after seeding, else null
type SeedVerdict = (name: string) => string | null;

function isPublishableProjectDir(dir: string, publishable: ReadonlySet<string>): boolean {
  if (!dir.endsWith('.frisket')) return false;
  try {
    const manifest = JSON.parse(fs.readFileSync(path.join(dir, 'manifest.json'), 'utf8')) as {
      name?: string;
    };
    return publishable.has(manifest.name ?? '');
  } catch {
    return false;
  }
}

function isRunLocalFile(name: string): boolean {
  return name.startsWith('.queue.db') || name === '.seed-publish.lock' || name.startsWith('.frisket-e2e-');
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

// Copy the completely built projects (and workspace files) the base workspace
// lacks, so the next isolated run starts from them; a partial project stays
// out so the next run builds it again. Existing base entries are kept.
async function publishSeedToBaseWorkspace(publishable: ReadonlySet<string>) {
  const workspace = process.env.FRISKET_E2E_WS;
  const baseWorkspace = process.env.FRISKET_E2E_BASE_WS;
  if (process.env.FRISKET_E2E_ISOLATED !== '1' || !workspace || !baseWorkspace) return;
  if (path.resolve(workspace) === path.resolve(baseWorkspace)) return;
  fs.mkdirSync(baseWorkspace, { recursive: true });
  const release = await acquireSeedPublishLock(path.join(baseWorkspace, '.seed-publish.lock'));
  try {
    for (const entry of fs.readdirSync(workspace, { withFileTypes: true })) {
      const source = path.join(workspace, entry.name);
      const target = path.join(baseWorkspace, entry.name);
      if (fs.existsSync(target) || isRunLocalFile(entry.name)) continue;
      if (entry.isDirectory() && !isPublishableProjectDir(source, publishable)) continue;
      fs.cpSync(source, target, { recursive: true });
    }
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

function outcomeFailure(outcome: SeedOutcome | undefined): string | null {
  if (outcome === undefined) return 'absent: the seeder did not report it';
  if (outcome.state === 'partial') return `partially built: ${outcome.detail ?? 'a run did not complete'}`;
  if (outcome.state === 'missing') return `absent: building it failed (${outcome.detail ?? 'no detail'})`;
  return null;
}

// Runs the seeder for the projects the workspace lacks; returns why each
// project is still unusable afterwards.
async function seed(): Promise<SeedVerdict> {
  const backendURL = process.env.FRISKET_E2E_BACKEND_URL ?? 'http://127.0.0.1:8000';
  const keyless = process.env.FRISKET_CACHE_MODE === 'replay_strict';
  const workspace = process.env.FRISKET_E2E_WS;
  if (keyless && !workspace) return () => 'absent, and keyless seeding needs FRISKET_E2E_WS';
  const reportDir = fs.mkdtempSync(path.join(os.tmpdir(), 'frisket-seed-'));
  const statusFile = path.join(reportDir, 'outcomes.json');
  const args = ['scripts/e2e/seed_demo.py', backendURL, '--status-file', statusFile];
  if (keyless && workspace) args.push('--keyless', workspace);
  let outcomes: Record<string, SeedOutcome>;
  try {
    execFileSync(testPython(), args, {
      cwd: path.resolve(here, '../../..'),
      stdio: 'inherit',
      timeout: 600_000,
    });
    outcomes = JSON.parse(fs.readFileSync(statusFile, 'utf8')) as Record<string, SeedOutcome>;
  } catch (error) {
    const why = `seeding failed (${error instanceof Error ? error.message.split('\n')[0] : String(error)})`;
    return () => why;
  } finally {
    fs.rmSync(reportDir, { recursive: true, force: true });
  }
  const complete = Object.entries(outcomes)
    .filter(([, outcome]) => outcome.state === 'complete')
    .map(([name]) => name);
  await publishSeedToBaseWorkspace(new Set(complete));
  return (name) => outcomeFailure(outcomes[name]);
}

async function openFailure(
  request: APIRequestContext,
  id: string | undefined,
  whyUnusable: string | null,
): Promise<string | null> {
  if (whyUnusable) return whyUnusable;
  if (id === undefined) return 'absent from the seeded workspace';
  const res = await request.get(`/api/projects/${encodeURIComponent(id)}/sheets`);
  if (res.ok()) return null;
  const body = (await res.json().catch(() => null)) as { detail?: unknown } | null;
  const detail = typeof body?.detail === 'string' ? body.detail : res.statusText();
  return `opening it answers HTTP ${res.status()}: ${detail}`;
}

setup('seed demo projects (once per workspace)', async ({ request }) => {
  setup.setTimeout(600_000); // first-ever seed: model runs + local transcription
  let ids = await projectIds(request);
  let verdict: SeedVerdict = () => null;
  if (SEEDED_PROJECTS.some((name) => !ids.has(name))) {
    verdict = await seed();
    ids = await projectIds(request);
  }
  const status: SeedStatus = {};
  for (const name of SEEDED_PROJECTS) {
    status[name] = await openFailure(request, ids.get(name), verdict(name));
  }
  writeSeedStatus(status);
  // The known-failures ratchet (scripts/ci/e2e_known_failures.py) reads this
  // to decide whether tests listed under the `seed` reason must fail.
  setup.info().annotations.push({ type: 'seed-status', description: JSON.stringify(status) });
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
  }
});
