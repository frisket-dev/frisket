// Idempotent seeding: the read-only specs (search, review, ...) expect the
// demo projects from scripts/e2e/seed_demo.py. The seeder builds only the
// projects the workspace lacks, each on its own, and reports each one as
// complete, partial or missing; only complete ones are published to the
// persistent e2e workspace. With FRISKET_CACHE_MODE=replay_strict it builds
// them from fixture data with no provider call and no local model;
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
import { type BackendHealth, publishSeedToBaseWorkspace } from './seedPublish';

const here = path.dirname(fileURLToPath(import.meta.url));

// The seeder's report on one project (seed_demo.py --status-file).
type SeedOutcome = { state: 'complete' | 'partial' | 'missing' | 'present'; detail: string | null };
// project name -> why it is unusable after seeding, else null
type SeedVerdict = (name: string) => string | null;

async function readBackendHealth(request: APIRequestContext): Promise<BackendHealth | null> {
  const res = await request.get('/api/health');
  if (!res.ok()) return null;
  return (await res.json()) as BackendHealth;
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
async function seed(request: APIRequestContext): Promise<SeedVerdict> {
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
  await publishSeedToBaseWorkspace({
    publishable: new Set(complete),
    readHealth: () => readBackendHealth(request),
  });
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
    verdict = await seed(request);
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
