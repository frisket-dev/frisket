import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const WORKSPACE_PREFIX = 'frisket-e2e-';
const ACTIVE_MARKER = '.frisket-e2e-active.json';
// Failed interactive runs stay inspectable for a working session, not days;
// FRISKET_E2E_PRUNE_HOURS overrides.
export const DEFAULT_PRUNE_HOURS = 6;
// Backstop for burst load (many concurrent oracle checks can exceed disk
// before anything is stale); FRISKET_E2E_MAX_TOTAL_BYTES overrides.
export const DEFAULT_MAX_TOTAL_BYTES = 10 * 1024 ** 3;

// Fixed-ports runs reuse the persistent base workspace (~/.frisket/e2e-ws) and
// leak one `<uniqueName>.frisket` project dir per created project — the isolated
// $TMPDIR prune above never touches them. Keep the last N runs' leaked projects
// inspectable, prune older ones; FRISKET_E2E_BASE_KEEP_RUNS overrides.
export const DEFAULT_BASE_KEEP_RUNS = 20;
const BASE_RUN_LEDGER = '.frisket-e2e-base-runs.json';
// Auto-generated project-dir names only. tests/e2e/helpers.ts `uniqueName()` =
// `${prefix}-${Date.now().toString(36)}-${0..9999}`; the verify skill uses an
// epoch-ms suffix. Clean-named seed/fixture projects (Local stories, People
// mentioned, Column stats e2e, …) match neither, so they are never pruned.
const LEAKED_PROJECT_SUFFIX_RE = /-[a-z0-9]{6,}-\d{1,5}$/;
const EPOCH_SUFFIX_RE = /-\d{12,}$/;

function isLeakedProjectDir(name) {
  if (!name.endsWith('.frisket')) return false;
  const base = name.slice(0, -'.frisket'.length);
  return LEAKED_PROJECT_SUFFIX_RE.test(base) || EPOCH_SUFFIX_RE.test(base);
}

function falsey(value) {
  return value != null && ['0', 'false', 'no', 'off', 'never'].includes(String(value).trim().toLowerCase());
}

function cleanupMode(env = process.env) {
  if (falsey(env.FRISKET_E2E_CLEANUP) || env.FRISKET_KEEP_E2E_WS === '1') {
    return 'never';
  }
  return String(env.FRISKET_E2E_CLEANUP || 'passed').trim().toLowerCase();
}

function positiveNumber(value, fallback) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function realpathIfExists(target) {
  try {
    return fs.realpathSync(target);
  } catch {
    return path.resolve(target);
  }
}

export function activeMarkerPath(workspace) {
  return path.join(workspace, ACTIVE_MARKER);
}

function isSafeE2EWorkspace(workspace, tmpDir = os.tmpdir()) {
  if (!workspace) return false;
  const resolved = path.resolve(workspace);
  if (!path.basename(resolved).startsWith(WORKSPACE_PREFIX)) return false;
  return realpathIfExists(path.dirname(resolved)) === realpathIfExists(tmpDir);
}

export function prepareE2EWorkspace({ workspace, runId, env = process.env, tmpDir = os.tmpdir() }) {
  if (env.FRISKET_E2E_ISOLATED !== '1' || !isSafeE2EWorkspace(workspace, tmpDir)) return;
  fs.mkdirSync(workspace, { recursive: true });
  fs.writeFileSync(
    activeMarkerPath(workspace),
    `${JSON.stringify({
      pid: process.pid,
      run_id: runId,
      started_at: new Date().toISOString(),
    })}\n`,
    'utf8',
  );
}

function workspaceHasLiveOwner(workspace) {
  try {
    const marker = JSON.parse(fs.readFileSync(activeMarkerPath(workspace), 'utf8'));
    const pid = Number(marker.pid);
    if (!Number.isInteger(pid) || pid <= 0) return false;
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error && error.code === 'EPERM';
  }
}

export function removeActiveMarker(workspace) {
  if (!workspace) return;
  fs.rmSync(activeMarkerPath(workspace), { force: true });
}

function shouldCleanupE2EWorkspace({ status, env = process.env }) {
  const mode = cleanupMode(env);
  if (mode === 'never' || mode === 'keep') return false;
  if (['1', 'true', 'yes', 'on', 'always'].includes(mode)) return true;
  return status === 'passed';
}

export function cleanupCurrentE2EWorkspace({
  status,
  env = process.env,
  tmpDir = os.tmpdir(),
  log = console.warn,
} = {}) {
  const workspace = env.FRISKET_E2E_WS;
  if (env.FRISKET_E2E_ISOLATED !== '1') return { removed: false, reason: 'not-isolated' };
  if (!shouldCleanupE2EWorkspace({ status, env })) return { removed: false, reason: 'kept' };
  if (!isSafeE2EWorkspace(workspace, tmpDir)) return { removed: false, reason: 'unsafe-path' };
  try {
    fs.rmSync(workspace, { recursive: true, force: true });
    return { removed: true, reason: 'removed' };
  } catch (error) {
    log(`[e2e-cleanup] unable to remove ${workspace}: ${error.message}`);
    return { removed: false, reason: 'error' };
  }
}

function directorySizeBytes(dir) {
  let total = 0;
  let entries;
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return total;
  }
  for (const entry of entries) {
    const target = path.join(dir, entry.name);
    // Per-entry try/catch: a concurrently-removed entry is skipped, never fatal.
    try {
      if (entry.isDirectory()) {
        total += directorySizeBytes(target);
      } else if (entry.isFile()) {
        total += fs.lstatSync(target).size;
      }
    } catch {
      continue;
    }
  }
  return total;
}

// Prunes oldest-first (by mtime) regardless of age once the combined size of
// all $TMPDIR/frisket-e2e-* workspaces exceeds FRISKET_E2E_MAX_TOTAL_BYTES,
// until back under the cap. The current workspace and live-owner workspaces
// are never removed (they still count toward the total), and only paths
// satisfying isSafeE2EWorkspace are touched.
export function enforceE2EWorkspaceSizeCap({
  env = process.env,
  tmpDir = os.tmpdir(),
  currentWorkspace = env.FRISKET_E2E_WS,
  log = console.warn,
} = {}) {
  const maxTotalBytes = positiveNumber(
    env.FRISKET_E2E_MAX_TOTAL_BYTES,
    DEFAULT_MAX_TOTAL_BYTES,
  );
  let names;
  try {
    names = fs.readdirSync(tmpDir);
  } catch {
    return [];
  }

  let totalBytes = 0;
  const candidates = [];
  for (const name of names) {
    if (!name.startsWith(WORKSPACE_PREFIX)) continue;
    const workspace = path.join(tmpDir, name);
    if (!isSafeE2EWorkspace(workspace, tmpDir)) continue;
    let stat;
    try {
      stat = fs.statSync(workspace);
    } catch {
      continue;
    }
    if (!stat.isDirectory()) continue;
    const bytes = directorySizeBytes(workspace);
    totalBytes += bytes;
    if (path.resolve(workspace) === path.resolve(currentWorkspace || '')) continue;
    if (workspaceHasLiveOwner(workspace)) continue;
    candidates.push({ workspace, bytes, mtimeMs: stat.mtimeMs });
  }
  if (totalBytes <= maxTotalBytes) return [];

  candidates.sort((a, b) => a.mtimeMs - b.mtimeMs);
  const removed = [];
  for (const candidate of candidates) {
    if (totalBytes <= maxTotalBytes) break;
    try {
      fs.rmSync(candidate.workspace, { recursive: true, force: true });
      removed.push(candidate.workspace);
      totalBytes -= candidate.bytes;
    } catch (error) {
      log(`[e2e-cleanup] unable to prune ${candidate.workspace}: ${error.message}`);
    }
  }
  return removed;
}

export function pruneStaleE2EWorkspaces({
  env = process.env,
  tmpDir = os.tmpdir(),
  currentWorkspace = env.FRISKET_E2E_WS,
  now = Date.now(),
  log = console.warn,
} = {}) {
  if (falsey(env.FRISKET_E2E_PRUNE)) return [];
  const maxAgeMs =
    positiveNumber(env.FRISKET_E2E_PRUNE_HOURS, DEFAULT_PRUNE_HOURS) * 60 * 60 * 1000;
  let names;
  try {
    names = fs.readdirSync(tmpDir);
  } catch {
    return [];
  }

  const removed = [];
  for (const name of names) {
    if (!name.startsWith(WORKSPACE_PREFIX)) continue;
    const workspace = path.join(tmpDir, name);
    if (path.resolve(workspace) === path.resolve(currentWorkspace || '')) continue;
    if (!isSafeE2EWorkspace(workspace, tmpDir) || workspaceHasLiveOwner(workspace)) continue;
    try {
      const stat = fs.statSync(workspace);
      if (!stat.isDirectory() || now - stat.mtimeMs < maxAgeMs) continue;
      fs.rmSync(workspace, { recursive: true, force: true });
      removed.push(workspace);
    } catch (error) {
      log(`[e2e-cleanup] unable to prune ${workspace}: ${error.message}`);
    }
  }
  removed.push(...enforceE2EWorkspaceSizeCap({ env, tmpDir, currentWorkspace, log }));
  return removed;
}

// Prunes leaked project dirs from the persistent BASE workspace, keeping only
// those created by the last `keepRuns` runs. A JSON ledger records each run's
// start time; the cutoff is the start of the oldest run still inside the window,
// and any leaked project older than that is removed. Only auto-generated project
// names (isLeakedProjectDir) are ever considered, so seed/fixture projects and
// non-project files (saved_recipes.json, the ledger, markers) are always kept.
export function pruneStaleBaseWorkspaceProjects({
  workspace,
  env = process.env,
  runId = env.FRISKET_E2E_RUN_ID,
  now = Date.now(),
  log = console.warn,
} = {}) {
  if (!workspace || falsey(env.FRISKET_E2E_PRUNE) || env.FRISKET_KEEP_E2E_WS === '1') return [];
  const keepRuns = Math.max(
    1,
    Math.floor(positiveNumber(env.FRISKET_E2E_BASE_KEEP_RUNS, DEFAULT_BASE_KEEP_RUNS)),
  );
  let entries;
  try {
    entries = fs.readdirSync(workspace, { withFileTypes: true });
  } catch {
    return [];
  }

  // Load, append the current run to, and trim the run ledger.
  const ledgerPath = path.join(workspace, BASE_RUN_LEDGER);
  let ledger = [];
  try {
    const parsed = JSON.parse(fs.readFileSync(ledgerPath, 'utf8'));
    if (Array.isArray(parsed)) {
      ledger = parsed.filter((entry) => entry && Number.isFinite(entry.at));
    }
  } catch {
    ledger = [];
  }
  if (runId && !ledger.some((entry) => entry.runId === runId)) {
    ledger.push({ runId, at: now });
  }
  ledger.sort((a, b) => a.at - b.at);
  // The oldest run still inside the last-`keepRuns` window; artifacts from runs
  // older than it (mtime below its start time) are stale.
  const cutoff = ledger.length > keepRuns ? ledger[ledger.length - keepRuns].at : -Infinity;
  const trimmed = ledger.slice(Math.max(0, ledger.length - keepRuns));
  try {
    fs.writeFileSync(ledgerPath, `${JSON.stringify(trimmed)}\n`, 'utf8');
  } catch (error) {
    log(`[e2e-cleanup] unable to write base run ledger: ${error.message}`);
  }
  if (!Number.isFinite(cutoff)) return [];

  const removed = [];
  for (const entry of entries) {
    if (!entry.isDirectory() || !isLeakedProjectDir(entry.name)) continue;
    const dir = path.join(workspace, entry.name);
    try {
      const stat = fs.statSync(dir);
      if (stat.mtimeMs >= cutoff) continue;
      fs.rmSync(dir, { recursive: true, force: true });
      removed.push(dir);
    } catch (error) {
      log(`[e2e-cleanup] unable to prune ${dir}: ${error.message}`);
    }
  }
  return removed;
}
