// Atomic, race-free publishing of seeded e2e projects from an isolated run
// workspace into the persistent base cache ($FRISKET_E2E_BASE_WS).
import { randomUUID } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

export const SEED_PUBLISH_LOCK = '.seed-publish.lock';
export const SEED_PUBLISH_TMP_PREFIX = '.seed-publish-tmp-';

const DEFAULT_QUEUE_DRAIN_TIMEOUT_MS = 30_000;
const DEFAULT_QUEUE_POLL_INTERVAL_MS = 150;
const DEFAULT_MAX_COPY_ATTEMPTS = 5;
const DEFAULT_RETRY_DELAY_MS = 50;

export type BackendHealth = {
  ok?: boolean;
  queue?: {
    jobs?: Record<string, number>;
    queued?: {
      count?: number;
      no_live_worker?: boolean;
    };
  };
};

type WaitForQueueDrainOptions = {
  timeoutMs?: number;
  pollIntervalMs?: number;
  sleepFn?: (ms: number) => Promise<void>;
  now?: () => number;
  warn?: (message: string) => void;
};

type PublishSeedOptions = {
  publishable: ReadonlySet<string>;
  workspace?: string;
  baseWorkspace?: string;
  isolated?: boolean;
  readHealth?: () => Promise<BackendHealth | null>;
  queueDrainTimeoutMs?: number;
  queuePollIntervalMs?: number;
  maxCopyAttempts?: number;
  retryDelayMs?: number;
  copyEntry?: (source: string, staged: string) => void;
  sleepFn?: (ms: number) => Promise<void>;
  warn?: (message: string) => void;
};

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

function isErrorWithCode(error: unknown, code: string): boolean {
  return error instanceof Error && 'code' in error && (error as NodeJS.ErrnoException).code === code;
}

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
  return (
    name.startsWith('.queue.db') ||
    name === SEED_PUBLISH_LOCK ||
    name.startsWith(SEED_PUBLISH_TMP_PREFIX) ||
    name.startsWith('.frisket-e2e-') ||
    name.endsWith('.lock') ||
    name.endsWith('-shm')
  );
}

// Ephemeral advisory locks and SQLite WAL shared-memory index files carry no
// durable state (SQLite rebuilds -shm from -wal on open). Keep -wal and
// -journal so open WAL databases snapshot consistently.
function isEphemeralProjectFile(filePath: string): boolean {
  const name = path.basename(filePath);
  return name.endsWith('-shm') || name.endsWith('.lock');
}

function defaultCopyEntry(source: string, staged: string): void {
  fs.cpSync(source, staged, {
    recursive: true,
    filter: (srcPath) => srcPath === source || !isEphemeralProjectFile(srcPath),
  });
}

function activeQueueJobCount(health: BackendHealth): number {
  const jobs = health.queue?.jobs;
  if (!jobs) return 0;
  const queued = typeof jobs.queued === 'number' && Number.isFinite(jobs.queued) ? jobs.queued : 0;
  const running = typeof jobs.running === 'number' && Number.isFinite(jobs.running) ? jobs.running : 0;
  return Math.max(0, queued) + Math.max(0, running);
}

// Wait for background queue jobs (such as search.index) to finish and close
// their SQLite handles before snapshotting project directories on disk.
export async function waitForBackendQueueDrain(
  readHealth: () => Promise<BackendHealth | null>,
  options: WaitForQueueDrainOptions = {},
): Promise<{ drained: boolean; remaining: number }> {
  const timeoutMs = options.timeoutMs ?? DEFAULT_QUEUE_DRAIN_TIMEOUT_MS;
  const pollIntervalMs = options.pollIntervalMs ?? DEFAULT_QUEUE_POLL_INTERVAL_MS;
  const sleepFn = options.sleepFn ?? sleep;
  const now = options.now ?? Date.now;
  const warn = options.warn ?? console.warn;

  const deadline = now() + timeoutMs;
  let lastRemaining = -1;

  for (;;) {
    let health: BackendHealth | null;
    try {
      health = await readHealth();
    } catch {
      health = null;
    }

    if (health !== null) {
      const remaining = activeQueueJobCount(health);
      lastRemaining = remaining;
      if (remaining === 0) {
        return { drained: true, remaining: 0 };
      }
      const running = health.queue?.jobs?.running ?? 0;
      if (health.queue?.queued?.no_live_worker && running === 0) {
        warn(
          `[seed-publish] proceeding with ${remaining} queued background job(s); no live worker reported by /api/health`,
        );
        return { drained: false, remaining };
      }
    }

    const remainingMs = deadline - now();
    if (remainingMs <= 0) {
      warn(
        lastRemaining >= 0
          ? `[seed-publish] timed out after ${timeoutMs}ms waiting for ${lastRemaining} background job(s) to drain`
          : `[seed-publish] timed out after ${timeoutMs}ms waiting for /api/health before publishing`,
      );
      return { drained: false, remaining: lastRemaining };
    }

    await sleepFn(Math.min(pollIntervalMs, remainingMs));
  }
}

async function acquireSeedPublishLock(
  lockPath: string,
  sleepFn: (ms: number) => Promise<void> = sleep,
): Promise<() => void> {
  const staleAfterMs = 60 * 60 * 1000;
  for (let attempt = 0; attempt < 600; attempt += 1) {
    try {
      fs.mkdirSync(lockPath);
      return () => fs.rmSync(lockPath, { recursive: true, force: true });
    } catch (error) {
      if (!isErrorWithCode(error, 'EEXIST')) {
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
      await sleepFn(250);
    }
  }
  throw new Error(`Timed out waiting for e2e seed publish lock: ${lockPath}`);
}

function sweepOrphanedStagingDirs(baseWorkspace: string): void {
  for (const name of fs.readdirSync(baseWorkspace)) {
    if (!name.startsWith(SEED_PUBLISH_TMP_PREFIX)) continue;
    fs.rmSync(path.join(baseWorkspace, name), { recursive: true, force: true });
  }
}

// Stage into a sibling path inside baseWorkspace so renameSync is atomic and
// same-mount, and retry from a clean stage if a transient SQLite sidecar file
// disappears mid-copy (e.g. a WAL checkpoint unlinking -wal).
async function stageAndPublishEntry({
  source,
  target,
  baseWorkspace,
  copyEntry,
  maxCopyAttempts,
  retryDelayMs,
  sleepFn,
}: {
  source: string;
  target: string;
  baseWorkspace: string;
  copyEntry: (source: string, staged: string) => void;
  maxCopyAttempts: number;
  retryDelayMs: number;
  sleepFn: (ms: number) => Promise<void>;
}): Promise<boolean> {
  for (let attempt = 1; attempt <= maxCopyAttempts; attempt += 1) {
    const staged = path.join(baseWorkspace, `${SEED_PUBLISH_TMP_PREFIX}${process.pid}-${randomUUID()}`);
    try {
      copyEntry(source, staged);
      if (!fs.existsSync(staged)) return false;
      fs.renameSync(staged, target);
      return true;
    } catch (error) {
      fs.rmSync(staged, { recursive: true, force: true });
      if (isErrorWithCode(error, 'ENOENT')) {
        if (!fs.existsSync(source)) return false;
        if (attempt < maxCopyAttempts) {
          await sleepFn(retryDelayMs * attempt);
          continue;
        }
      }
      throw error;
    }
  }
  return false;
}

// Copy the completely built projects (and workspace files) the base workspace
// lacks, so the next isolated run starts from them; a partial project stays
// out so the next run builds it again. Existing base entries are kept.
export async function publishSeedToBaseWorkspace(options: PublishSeedOptions): Promise<void> {
  const workspace = options.workspace ?? process.env.FRISKET_E2E_WS;
  const baseWorkspace = options.baseWorkspace ?? process.env.FRISKET_E2E_BASE_WS;
  const isolated = options.isolated ?? process.env.FRISKET_E2E_ISOLATED === '1';
  if (!isolated || !workspace || !baseWorkspace) return;
  if (path.resolve(workspace) === path.resolve(baseWorkspace)) return;

  const sleepFn = options.sleepFn ?? sleep;
  const warn = options.warn ?? console.warn;
  const copyEntry = options.copyEntry ?? defaultCopyEntry;
  const maxCopyAttempts = Math.max(1, options.maxCopyAttempts ?? DEFAULT_MAX_COPY_ATTEMPTS);
  const retryDelayMs = options.retryDelayMs ?? DEFAULT_RETRY_DELAY_MS;

  if (options.readHealth) {
    await waitForBackendQueueDrain(options.readHealth, {
      timeoutMs: options.queueDrainTimeoutMs,
      pollIntervalMs: options.queuePollIntervalMs,
      sleepFn,
      warn,
    });
  }

  fs.mkdirSync(baseWorkspace, { recursive: true });
  const release = await acquireSeedPublishLock(path.join(baseWorkspace, SEED_PUBLISH_LOCK), sleepFn);
  try {
    sweepOrphanedStagingDirs(baseWorkspace);
    for (const entry of fs.readdirSync(workspace, { withFileTypes: true })) {
      if (isRunLocalFile(entry.name)) continue;
      const source = path.join(workspace, entry.name);
      const target = path.join(baseWorkspace, entry.name);
      if (fs.existsSync(target)) continue;
      if (entry.isDirectory() && !isPublishableProjectDir(source, options.publishable)) continue;
      await stageAndPublishEntry({
        source,
        target,
        baseWorkspace,
        copyEntry,
        maxCopyAttempts,
        retryDelayMs,
        sleepFn,
      });
    }
  } finally {
    release();
  }
}
