import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';

import {
  type BackendHealth,
  SEED_PUBLISH_LOCK,
  SEED_PUBLISH_TMP_PREFIX,
  publishSeedToBaseWorkspace,
  waitForBackendQueueDrain,
} from '../e2e/seedPublish';

const tempRoots: string[] = [];

function makeTempRoot(): { root: string; workspace: string; baseWorkspace: string } {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'frisket-seed-publish-test-'));
  tempRoots.push(root);
  const workspace = path.join(root, 'run');
  const baseWorkspace = path.join(root, 'base');
  fs.mkdirSync(workspace, { recursive: true });
  fs.mkdirSync(baseWorkspace, { recursive: true });
  return { root, workspace, baseWorkspace };
}

function writeProject(
  dir: string,
  slug: string,
  name: string,
  extraFiles: Record<string, string> = {},
): string {
  const projectDir = path.join(dir, `${slug}.frisket`);
  fs.mkdirSync(projectDir, { recursive: true });
  fs.writeFileSync(path.join(projectDir, 'manifest.json'), JSON.stringify({ name }));
  fs.writeFileSync(path.join(projectDir, 'project.db'), `db:${name}`);
  for (const [relativePath, content] of Object.entries(extraFiles)) {
    const filePath = path.join(projectDir, relativePath);
    fs.mkdirSync(path.dirname(filePath), { recursive: true });
    fs.writeFileSync(filePath, content);
  }
  return projectDir;
}

afterEach(() => {
  for (const root of tempRoots.splice(0)) {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

describe('waitForBackendQueueDrain', () => {
  it('waits until queued and running job counts both reach zero', async () => {
    const snapshots: BackendHealth[] = [
      { ok: true, queue: { jobs: { queued: 1, running: 0, done: 3 } } },
      { ok: true, queue: { jobs: { queued: 0, running: 1, done: 3 } } },
      { ok: true, queue: { jobs: { queued: 0, running: 0, done: 4 } } },
    ];
    let index = 0;
    const sleeps: number[] = [];
    const warnings: string[] = [];

    const result = await waitForBackendQueueDrain(
      async () => snapshots[Math.min(index++, snapshots.length - 1)],
      {
        timeoutMs: 5_000,
        pollIntervalMs: 100,
        sleepFn: async (ms) => {
          sleeps.push(ms);
        },
        warn: (message) => warnings.push(message),
      },
    );

    expect(result).toEqual({ drained: true, remaining: 0 });
    expect(sleeps).toEqual([100, 100]);
    expect(warnings).toEqual([]);
  });

  it('treats health payloads without a queue subtree as immediately drained', async () => {
    const result = await waitForBackendQueueDrain(async () => ({ ok: true }), {
      timeoutMs: 1_000,
      pollIntervalMs: 50,
    });
    expect(result).toEqual({ drained: true, remaining: 0 });
  });

  it('short-circuits with a warning when queued jobs have no live worker', async () => {
    const warnings: string[] = [];
    const sleeps: number[] = [];

    const result = await waitForBackendQueueDrain(
      async () => ({
        ok: true,
        queue: {
          jobs: { queued: 2, running: 0 },
          queued: { count: 2, no_live_worker: true },
        },
      }),
      {
        timeoutMs: 5_000,
        pollIntervalMs: 100,
        sleepFn: async (ms) => {
          sleeps.push(ms);
        },
        warn: (message) => warnings.push(message),
      },
    );

    expect(result).toEqual({ drained: false, remaining: 2 });
    expect(sleeps).toEqual([]);
    expect(warnings).toHaveLength(1);
    expect(warnings[0]).toContain('no live worker');
  });

  it('times out gracefully with a warning when jobs stay active past the deadline', async () => {
    let clock = 1_000;
    const warnings: string[] = [];

    const result = await waitForBackendQueueDrain(
      async () => ({ ok: true, queue: { jobs: { queued: 0, running: 1 } } }),
      {
        timeoutMs: 250,
        pollIntervalMs: 100,
        now: () => clock,
        sleepFn: async (ms) => {
          clock += ms;
        },
        warn: (message) => warnings.push(message),
      },
    );

    expect(result).toEqual({ drained: false, remaining: 1 });
    expect(clock).toBe(1_250);
    expect(warnings).toHaveLength(1);
    expect(warnings[0]).toContain('timed out');
  });

  it('recovers from transient health fetch errors and drains once healthy', async () => {
    let calls = 0;
    const result = await waitForBackendQueueDrain(
      async () => {
        calls += 1;
        if (calls === 1) throw new Error('ECONNRESET');
        if (calls === 2) return null;
        return { ok: true, queue: { jobs: { queued: 0, running: 0 } } };
      },
      {
        timeoutMs: 1_000,
        pollIntervalMs: 10,
        sleepFn: async () => {},
      },
    );

    expect(result).toEqual({ drained: true, remaining: 0 });
    expect(calls).toBe(3);
  });
});

describe('publishSeedToBaseWorkspace', () => {
  it('publishes complete projects atomically, skips run-local and ephemeral files, and keeps existing base entries', async () => {
    const { workspace, baseWorkspace } = makeTempRoot();
    writeProject(workspace, 'people-mentioned', 'People mentioned', {
      'project.db-wal': 'wal-data',
      'project.db-shm': 'shm-ephemeral',
      '.manifest.lock': '',
      'project.retrieval.db': 'retrieval-data',
      'project.retrieval.db.init.lock': '',
    });
    writeProject(workspace, 'partial-project', 'Partial project');
    writeProject(workspace, 'existing-project', 'Local stories', {
      'marker.txt': 'from-run',
    });
    writeProject(baseWorkspace, 'existing-project', 'Local stories', {
      'marker.txt': 'from-base',
    });
    fs.writeFileSync(path.join(workspace, 'saved_recipes.json'), '[]');
    fs.writeFileSync(path.join(workspace, '.queue.db'), 'queue');
    fs.writeFileSync(path.join(workspace, '.queue.db-wal'), 'queue-wal');
    fs.writeFileSync(path.join(workspace, '.frisket-e2e-active.json'), '{}');
    fs.writeFileSync(path.join(workspace, '.saved_recipes.lock'), '');
    fs.mkdirSync(path.join(workspace, `${SEED_PUBLISH_TMP_PREFIX}orphan-in-ws`));
    fs.mkdirSync(path.join(baseWorkspace, `${SEED_PUBLISH_TMP_PREFIX}orphan-in-base`));

    await publishSeedToBaseWorkspace({
      publishable: new Set(['People mentioned', 'Local stories']),
      workspace,
      baseWorkspace,
      isolated: true,
    });

    const baseEntries = fs.readdirSync(baseWorkspace).sort();
    expect(baseEntries).toEqual([
      'existing-project.frisket',
      'people-mentioned.frisket',
      'saved_recipes.json',
    ]);
    expect(
      fs.readdirSync(path.join(baseWorkspace, 'people-mentioned.frisket')).sort(),
    ).toEqual(['manifest.json', 'project.db', 'project.db-wal', 'project.retrieval.db']);
    expect(
      fs.readFileSync(path.join(baseWorkspace, 'existing-project.frisket', 'marker.txt'), 'utf8'),
    ).toBe('from-base');
    expect(fs.existsSync(path.join(baseWorkspace, SEED_PUBLISH_LOCK))).toBe(false);
  });

  it('cleans up the staging directory and leaves baseWorkspace untouched when copying fails', async () => {
    const { workspace, baseWorkspace } = makeTempRoot();
    writeProject(workspace, 'people-mentioned', 'People mentioned');

    await expect(
      publishSeedToBaseWorkspace({
        publishable: new Set(['People mentioned']),
        workspace,
        baseWorkspace,
        isolated: true,
        copyEntry: (_source, staged) => {
          fs.mkdirSync(staged, { recursive: true });
          fs.writeFileSync(path.join(staged, 'partial.db'), 'half-written');
          const err = new Error('simulated disk error') as NodeJS.ErrnoException;
          err.code = 'EIO';
          throw err;
        },
      }),
    ).rejects.toThrow('simulated disk error');

    expect(fs.readdirSync(baseWorkspace)).toEqual([]);
  });

  it('retries cleanly when a transient SQLite WAL file vanishes mid-copy', async () => {
    const { workspace, baseWorkspace } = makeTempRoot();
    const projectDir = writeProject(workspace, 'people-mentioned', 'People mentioned', {
      'project.retrieval.db': 'v1-before-checkpoint',
      'project.retrieval.db-wal': 'pending-wal-frames',
    });

    let attempts = 0;
    const observedStagingPaths: string[] = [];
    const sleeps: number[] = [];

    await publishSeedToBaseWorkspace({
      publishable: new Set(['People mentioned']),
      workspace,
      baseWorkspace,
      isolated: true,
      maxCopyAttempts: 4,
      retryDelayMs: 25,
      sleepFn: async (ms) => {
        sleeps.push(ms);
      },
      copyEntry: (source, staged) => {
        attempts += 1;
        observedStagingPaths.push(staged);
        fs.cpSync(source, staged, {
          recursive: true,
          filter: (srcPath) => {
            // Simulate search.index closing SQLite and checkpointing/unlinking
            // the WAL file right as cpSync reaches it on the first attempt.
            if (attempts === 1 && srcPath.endsWith('project.retrieval.db-wal')) {
              fs.writeFileSync(
                path.join(projectDir, 'project.retrieval.db'),
                'v2-checkpointed',
              );
              fs.unlinkSync(srcPath);
            }
            return true;
          },
        });
      },
    });

    expect(attempts).toBe(2);
    expect(sleeps).toEqual([25]);
    // Both staging directories must be gone (first cleaned on retry, second renamed).
    for (const staged of observedStagingPaths) {
      expect(fs.existsSync(staged)).toBe(false);
    }
    expect(fs.readdirSync(baseWorkspace)).toEqual(['people-mentioned.frisket']);
    expect(
      fs.readFileSync(
        path.join(baseWorkspace, 'people-mentioned.frisket', 'project.retrieval.db'),
        'utf8',
      ),
    ).toBe('v2-checkpointed');
    expect(
      fs.existsSync(
        path.join(baseWorkspace, 'people-mentioned.frisket', 'project.retrieval.db-wal'),
      ),
    ).toBe(false);
  });

  it('cleans up and rethrows when ENOENT persists past maxCopyAttempts', async () => {
    const { workspace, baseWorkspace } = makeTempRoot();
    writeProject(workspace, 'people-mentioned', 'People mentioned');
    let attempts = 0;

    await expect(
      publishSeedToBaseWorkspace({
        publishable: new Set(['People mentioned']),
        workspace,
        baseWorkspace,
        isolated: true,
        maxCopyAttempts: 3,
        retryDelayMs: 10,
        sleepFn: async () => {},
        copyEntry: (_source, staged) => {
          attempts += 1;
          fs.mkdirSync(staged, { recursive: true });
          const err = new Error('vanished child file') as NodeJS.ErrnoException;
          err.code = 'ENOENT';
          throw err;
        },
      }),
    ).rejects.toThrow('vanished child file');

    expect(attempts).toBe(3);
    expect(fs.readdirSync(baseWorkspace)).toEqual([]);
  });

  it('skips cleanly when a transient top-level workspace file vanishes before copy', async () => {
    const { workspace, baseWorkspace } = makeTempRoot();
    const transientFile = path.join(workspace, 'transient.json');
    fs.writeFileSync(transientFile, '{}');
    let attempts = 0;

    await publishSeedToBaseWorkspace({
      publishable: new Set(),
      workspace,
      baseWorkspace,
      isolated: true,
      copyEntry: (source, staged) => {
        attempts += 1;
        fs.unlinkSync(source);
        fs.cpSync(source, staged, { recursive: true });
      },
    });

    expect(attempts).toBe(1);
    expect(fs.readdirSync(baseWorkspace)).toEqual([]);
  });

  it('waits for backend queue drain before snapshotting project directories', async () => {
    const { workspace, baseWorkspace } = makeTempRoot();
    const projectDir = writeProject(workspace, 'people-mentioned', 'People mentioned', {
      'project.retrieval.db': 'indexing-in-progress',
      'project.retrieval.db-wal': 'live-wal',
    });

    let pollCount = 0;
    await publishSeedToBaseWorkspace({
      publishable: new Set(['People mentioned']),
      workspace,
      baseWorkspace,
      isolated: true,
      queuePollIntervalMs: 10,
      sleepFn: async () => {},
      readHealth: async () => {
        pollCount += 1;
        if (pollCount === 1) {
          return { ok: true, queue: { jobs: { queued: 0, running: 1 } } };
        }
        // Background search.index completes and closes its WAL database.
        fs.writeFileSync(path.join(projectDir, 'project.retrieval.db'), 'indexed-final');
        fs.rmSync(path.join(projectDir, 'project.retrieval.db-wal'), { force: true });
        return { ok: true, queue: { jobs: { queued: 0, running: 0 } } };
      },
    });

    expect(pollCount).toBe(2);
    expect(
      fs.readFileSync(
        path.join(baseWorkspace, 'people-mentioned.frisket', 'project.retrieval.db'),
        'utf8',
      ),
    ).toBe('indexed-final');
    expect(
      fs.existsSync(
        path.join(baseWorkspace, 'people-mentioned.frisket', 'project.retrieval.db-wal'),
      ),
    ).toBe(false);
  });

  it('reclaims a stale publish lock older than one hour', async () => {
    const { workspace, baseWorkspace } = makeTempRoot();
    writeProject(workspace, 'people-mentioned', 'People mentioned');
    const lockPath = path.join(baseWorkspace, SEED_PUBLISH_LOCK);
    fs.mkdirSync(lockPath);
    const twoHoursAgo = new Date(Date.now() - 2 * 60 * 60 * 1000);
    fs.utimesSync(lockPath, twoHoursAgo, twoHoursAgo);

    await publishSeedToBaseWorkspace({
      publishable: new Set(['People mentioned']),
      workspace,
      baseWorkspace,
      isolated: true,
    });

    expect(fs.readdirSync(baseWorkspace)).toEqual(['people-mentioned.frisket']);
  });

  it('is a no-op when not isolated or when workspace equals baseWorkspace', async () => {
    const { workspace, baseWorkspace } = makeTempRoot();
    writeProject(workspace, 'people-mentioned', 'People mentioned');
    let healthChecks = 0;

    await publishSeedToBaseWorkspace({
      publishable: new Set(['People mentioned']),
      workspace,
      baseWorkspace,
      isolated: false,
      readHealth: async () => {
        healthChecks += 1;
        return { ok: true };
      },
    });

    await publishSeedToBaseWorkspace({
      publishable: new Set(['People mentioned']),
      workspace,
      baseWorkspace: workspace,
      isolated: true,
      readHealth: async () => {
        healthChecks += 1;
        return { ok: true };
      },
    });

    expect(healthChecks).toBe(0);
    expect(fs.readdirSync(baseWorkspace)).toEqual([]);
  });
});
