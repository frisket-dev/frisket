// Hardening contract: a six-hour default TTL plus the
// FRISKET_E2E_MAX_TOTAL_BYTES size guard. The baseline
// passed/failed/live-owner/safety behavior is pinned by
// web/tests/e2e-workspace-cleanup.test.mjs.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import {
  DEFAULT_BASE_KEEP_RUNS,
  DEFAULT_MAX_TOTAL_BYTES,
  DEFAULT_PRUNE_HOURS,
  activeMarkerPath,
  enforceE2EWorkspaceSizeCap,
  pruneStaleBaseWorkspaceProjects,
  pruneStaleE2EWorkspaces,
} from '../../e2e-workspace-cleanup.mjs';

function tempRoot() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'frisket-cleanup-hardening-'));
}

function makeWorkspace(root, name, { bytes = 0, ageHours = 0, livePid = null } = {}) {
  const workspace = path.join(root, name);
  fs.mkdirSync(workspace, { recursive: true });
  if (bytes > 0) {
    fs.writeFileSync(path.join(workspace, 'payload.bin'), Buffer.alloc(bytes));
  }
  if (livePid != null) {
    fs.writeFileSync(activeMarkerPath(workspace), `${JSON.stringify({ pid: livePid })}\n`);
  }
  const when = new Date(Date.now() - ageHours * 60 * 60 * 1000);
  fs.utimesSync(workspace, when, when);
  return workspace;
}

test('prune defaults are 6 hours TTL and a 10GB size cap', () => {
  assert.equal(DEFAULT_PRUNE_HOURS, 6);
  assert.equal(DEFAULT_MAX_TOTAL_BYTES, 10 * 1024 ** 3);
});

test('age pass removes an over-age workspace and keeps a fresh one at the 6h default', () => {
  const root = tempRoot();
  const overAge = makeWorkspace(root, 'frisket-e2e-over-age', { ageHours: 7 });
  const fresh = makeWorkspace(root, 'frisket-e2e-fresh', { ageHours: 1 });

  const removed = pruneStaleE2EWorkspaces({ tmpDir: root, env: {}, now: Date.now() });

  assert.deepEqual(removed, [overAge]);
  assert.equal(fs.existsSync(overAge), false);
  assert.equal(fs.existsSync(fresh), true);
  fs.rmSync(root, { recursive: true, force: true });
});

test('size guard prunes oldest-first past the cap, keeping current + live-owner workspaces', () => {
  const root = tempRoot();
  // Everything is fresh (< 6h) so the age pass removes nothing; the two
  // OLDEST directories are protected (live owner, current run), so the guard
  // must skip them and remove the oldest unprotected candidate — and stop
  // there, since one removal gets back under the cap.
  const live = makeWorkspace(root, 'frisket-e2e-live', {
    bytes: 10000,
    ageHours: 5,
    livePid: process.pid,
  });
  const current = makeWorkspace(root, 'frisket-e2e-current', { bytes: 10000, ageHours: 4 });
  const oldest = makeWorkspace(root, 'frisket-e2e-oldest', { bytes: 10000, ageHours: 3 });
  const newest = makeWorkspace(root, 'frisket-e2e-newest', { bytes: 10000, ageHours: 1 });

  const removed = pruneStaleE2EWorkspaces({
    tmpDir: root,
    env: { FRISKET_E2E_MAX_TOTAL_BYTES: '35000' },
    currentWorkspace: current,
    now: Date.now(),
  });

  assert.deepEqual(removed, [oldest]);
  assert.equal(fs.existsSync(oldest), false);
  assert.equal(fs.existsSync(newest), true);
  assert.equal(fs.existsSync(live), true);
  assert.equal(fs.existsSync(current), true);
  fs.rmSync(root, { recursive: true, force: true });
});

test('size guard removes candidates oldest-first until under the cap', () => {
  const root = tempRoot();
  const oldest = makeWorkspace(root, 'frisket-e2e-a', { bytes: 10000, ageHours: 3 });
  const middle = makeWorkspace(root, 'frisket-e2e-b', { bytes: 10000, ageHours: 2 });
  const newest = makeWorkspace(root, 'frisket-e2e-c', { bytes: 10000, ageHours: 1 });

  const removed = enforceE2EWorkspaceSizeCap({
    tmpDir: root,
    env: { FRISKET_E2E_MAX_TOTAL_BYTES: '15000' },
  });

  assert.deepEqual(removed, [oldest, middle]);
  assert.equal(fs.existsSync(newest), true);
  fs.rmSync(root, { recursive: true, force: true });
});

test('size guard is inert while workspaces total under the cap', () => {
  const root = tempRoot();
  const workspace = makeWorkspace(root, 'frisket-e2e-small', { bytes: 1000, ageHours: 3 });

  const removed = enforceE2EWorkspaceSizeCap({
    tmpDir: root,
    env: { FRISKET_E2E_MAX_TOTAL_BYTES: '50000' },
  });

  assert.deepEqual(removed, []);
  assert.equal(fs.existsSync(workspace), true);
  fs.rmSync(root, { recursive: true, force: true });
});

// --- base workspace run-count prune -----------------------------------------

const HOUR = 60 * 60 * 1000;

function makeProjectDir(workspace, name, atMs) {
  const dir = path.join(workspace, name);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, 'manifest.json'), '{"name":"x"}');
  const when = new Date(atMs);
  fs.utimesSync(dir, when, when);
  return dir;
}

test('base keep-runs default is 20', () => {
  assert.equal(DEFAULT_BASE_KEEP_RUNS, 20);
});

test('base prune keeps the last N runs of leaked projects and never touches seeds', () => {
  const root = tempRoot();
  const t0 = Date.now() - 5 * HOUR; // run r0
  const t1 = Date.now() - 3 * HOUR; // run r1
  const t2 = Date.now() - 1 * HOUR; // current run r2
  fs.writeFileSync(
    path.join(root, '.frisket-e2e-base-runs.json'),
    JSON.stringify([{ runId: 'r0', at: t0 }, { runId: 'r1', at: t1 }]),
  );

  // A run's projects are created after its setup timestamp, so their mtime sits
  // a margin above the run's ledger time (well clear of fs timestamp rounding).
  const oldLeak = makeProjectDir(root, 'dataset-export-mq0abcd1-11.frisket', t0 + 60_000);
  const recentLeak = makeProjectDir(root, 'row-focus-mq1efgh2-22.frisket', t1 + 60_000);
  const seed = makeProjectDir(root, 'local-stories.frisket', t0 + 60_000); // clean name, old
  const recipes = path.join(root, 'saved_recipes.json');
  fs.writeFileSync(recipes, '[]');

  // keepRuns=2 → window is {r1, r2}; r0's artifacts are stale.
  const removed = pruneStaleBaseWorkspaceProjects({
    workspace: root,
    env: { FRISKET_E2E_BASE_KEEP_RUNS: '2' },
    runId: 'r2',
    now: t2,
  });

  assert.deepEqual(removed, [oldLeak]);
  assert.equal(fs.existsSync(oldLeak), false);
  assert.equal(fs.existsSync(recentLeak), true); // inside the window
  assert.equal(fs.existsSync(seed), true); // seed name never matches leak pattern
  assert.equal(fs.existsSync(recipes), true); // non-project file untouched

  // Ledger was appended + trimmed to the last keepRuns entries.
  const ledger = JSON.parse(fs.readFileSync(path.join(root, '.frisket-e2e-base-runs.json'), 'utf8'));
  assert.deepEqual(ledger.map((e) => e.runId), ['r1', 'r2']);
  fs.rmSync(root, { recursive: true, force: true });
});

test('base prune removes nothing until more runs than the window have accrued', () => {
  const root = tempRoot();
  const t0 = Date.now() - 5 * HOUR;
  const leak = makeProjectDir(root, 'imports-mq0abcd1-3.frisket', t0);

  const removed = pruneStaleBaseWorkspaceProjects({
    workspace: root,
    env: { FRISKET_E2E_BASE_KEEP_RUNS: '5' },
    runId: 'r1',
    now: Date.now(),
  });

  assert.deepEqual(removed, []);
  assert.equal(fs.existsSync(leak), true);
  fs.rmSync(root, { recursive: true, force: true });
});

test('base prune is disabled by FRISKET_E2E_PRUNE=0 and FRISKET_KEEP_E2E_WS=1', () => {
  const root = tempRoot();
  fs.writeFileSync(
    path.join(root, '.frisket-e2e-base-runs.json'),
    JSON.stringify([{ runId: 'r0', at: Date.now() - 5 * HOUR }, { runId: 'r1', at: Date.now() - 3 * HOUR }]),
  );
  const leak = makeProjectDir(root, 'export-ui-mq0abcd1-9.frisket', Date.now() - 5 * HOUR);

  for (const env of [{ FRISKET_E2E_PRUNE: '0' }, { FRISKET_KEEP_E2E_WS: '1' }]) {
    const removed = pruneStaleBaseWorkspaceProjects({
      workspace: root,
      env: { ...env, FRISKET_E2E_BASE_KEEP_RUNS: '1' },
      runId: 'r2',
      now: Date.now(),
    });
    assert.deepEqual(removed, []);
    assert.equal(fs.existsSync(leak), true);
  }
  fs.rmSync(root, { recursive: true, force: true });
});
