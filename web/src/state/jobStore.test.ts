// @vitest-environment jsdom

// Pins jobStore's poll-epoch behavior directly (not only in
// core/jobs/engine.test.ts): stale poll response dropped (epoch mismatch), abort
// on cancel/unmount, no overlapping polls (next scheduled only after previous
// resolves).

import { afterEach, describe, expect, it, vi } from 'vitest';
import { createJobStore, type JobRunDeps } from './jobStore';
import { ApiError, ConfirmationRequiredError } from '../api/open';
import { createProjectApi } from '../api/real';
import type {
  ActionJob,
  ActionCatalogPayload,
  CopilotProposal,
  DeriveCompositeRequest,
  RegisteredActionRequest,
  RunActionLaunchResult,
  RunProgress,
} from '../api/open';
import type { ProjectApiPort } from '../api/ports';

const api = createProjectApi('test-project');

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
} {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function noopDeps(): JobRunDeps {
  return {
    invalidateProjectData: vi.fn(),
    refreshHistory: vi.fn(),
    refreshReviewCount: vi.fn(),
    refreshSheets: vi.fn(),
    showError: vi.fn(),
  };
}

function runProgress(overrides: Partial<RunProgress> = {}): RunProgress {
  return {
    runId: 'run-a',
    actionName: 'Classify',
    actionKind: 'map.classify',
    sheetId: 'sheet-1',
    targetColumnId: 'col-1',
    status: 'running',
    completedRows: 0,
    totalRows: 10,
    failedRows: 0,
    costSoFar: 0,
    ...overrides,
  };
}

function runRequest(overrides: Partial<RegisteredActionRequest> & {
  actionKind?: string;
  targetColumnId?: string;
} = {}): RegisteredActionRequest {
  const { actionKind, targetColumnId, ...registered } = overrides;
  return {
    action_id: actionKind ?? 'map.classify',
    scope: { kind: 'sheet_rows', sheet_id: 1 },
    params: { instruction: 'p', model: 'm' },
    output_names: { result: targetColumnId ?? 'col-1' },
    idempotency_key: `test-${actionKind ?? 'map.classify'}-${targetColumnId ?? 'col-1'}`,
    ...registered,
  };
}

async function flushMicrotasks(): Promise<void> {
  for (let index = 0; index < 8; index += 1) await Promise.resolve();
}

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

function actionJob(overrides: Partial<ActionJob> = {}): ActionJob {
  return {
    schemaVersion: '1',
    projectId: 'project-a',
    jobId: 1,
    kind: 'action',
    runId: null,
    receiptId: null,
    status: 'running',
    actionKind: 'map.classify',
    actionName: 'Classify',
    attempts: 1,
    maxAttempts: 3,
    lease: {
      lockedBy: null,
      lockedAt: null,
      leaseExpiresAt: null,
      leaseExpired: false,
    },
    timing: {
      createdAt: '2026-08-08T00:00:00Z',
      startedAt: '2026-08-08T00:00:01Z',
      finishedAt: null,
    },
    error: null,
    ...overrides,
  };
}

function projectPort(overrides: Partial<ProjectApiPort> = {}): ProjectApiPort {
  return new Proxy(api as ProjectApiPort, {
    get(target, property) {
      if (Object.prototype.hasOwnProperty.call(overrides, property)) {
        return overrides[property as keyof ProjectApiPort];
      }
      const value = Reflect.get(target, property, target) as unknown;
      return typeof value === 'function' ? value.bind(target) : value;
    },
  });
}

function setDocumentHidden(hidden: boolean): void {
  Object.defineProperty(document, 'hidden', { configurable: true, value: hidden });
}

describe('createJobStore — defaults (parity with the pre-migration hook state)', () => {
  it('starts idle with public job facts and no completed cluster result', () => {
    const jobs = createJobStore('test-project', projectPort());
    expect(Object.keys(jobs.store.get()).sort()).toEqual([
      'actionJobs',
      'completedClusterReceiptId',
      'costGate',
      'outputColumnCollision',
      'run',
    ]);
    expect(Object.keys(jobs).sort()).toEqual([
      'afterBackfill',
      'cancelCostGate',
      'cancelCurrentRun',
      'cancelOutputColumnCollision',
      'confirmCostGate',
      'confirmOutputColumnCollision',
      'dismissCompletedClusterResult',
      'dispose',
      'liveActionJobs',
      'refresh',
      'requestCostConfirmation',
      'start',
      'startProposal',
      'startRun',
      'store',
    ]);
    expect(jobs.store.get()).toEqual({
      completedClusterReceiptId: null,
      run: null,
      actionJobs: { error: null, jobs: [], loading: false },
      costGate: null,
      // The SAME confirm/cancel-pair
      // shape costGate uses, for a server-side output_column_exists 409.
      outputColumnCollision: null,
    });
  });
});

describe('createJobStore — owned scheduler lifecycle (WEB-03-4B red contract)', () => {
  it('is side-effect-free until start, then owns one deferred refresh and fixed run cadence', async () => {
    vi.useFakeTimers();
    const listActionJobs = vi
      .spyOn(api, 'listActionJobs')
      .mockResolvedValue({ jobs: [] } as never);
    const getRunProgress = vi
      .spyOn(api, 'getRunProgress')
      .mockResolvedValue(runProgress({ status: 'running' }));
    vi.spyOn(api, 'runAction').mockResolvedValue({ runId: 'run-a' });

    const jobs = createJobStore('test-project', projectPort());
    const deps = noopDeps();

    expect(listActionJobs).not.toHaveBeenCalled();
    jobs.start(deps);
    expect(listActionJobs).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(0);
    expect(listActionJobs).toHaveBeenCalledTimes(1);

    jobs.startRun(
      runRequest(),
      { id: 'sheet-1', rowCount: 5 } as never,
    );
    await Promise.resolve();
    await Promise.resolve();

    expect(getRunProgress).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(299);
    expect(getRunProgress).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(getRunProgress).toHaveBeenCalledTimes(1);

    jobs.start(deps);
    await vi.advanceTimersByTimeAsync(300);
    expect(getRunProgress).toHaveBeenCalledTimes(2);
    expect(listActionJobs).toHaveBeenCalledTimes(2);
  });

  it('dispose resolves pending consent false and fences a delayed launch completion', async () => {
    const launch = deferred<RunActionLaunchResult>();
    vi.spyOn(api, 'runAction').mockReturnValueOnce(launch.promise);
    vi.spyOn(api, 'listActionJobs').mockResolvedValue({ jobs: [] } as never);
    const jobs = createJobStore('test-project', projectPort());
    const deps = { ...noopDeps(), onLaunchAccepted: vi.fn() };
    jobs.start(deps);

    const confirmation = jobs.requestCostConfirmation(
      { cost: 1, rows: 10 },
      'confirm?',
    );
    jobs.startRun(
      runRequest(),
      { id: 'sheet-1', rowCount: 5 } as never,
    );
    const beforeDispose = jobs.store.get().run;

    jobs.dispose();
    await expect(confirmation).resolves.toBe(false);
    expect(jobs.store.get().costGate).toBeNull();

    launch.resolve({ runId: 'run-a' });
    await Promise.resolve();
    await Promise.resolve();

    expect(jobs.store.get().run).toBe(beforeDispose);
    expect(deps.onLaunchAccepted).not.toHaveBeenCalled();
    expect(deps.refreshSheets).not.toHaveBeenCalled();
  });
});

describe('createJobStore — complete WEB-03-4B scheduler/resource red contract', () => {
  it('refreshes collaborators on repeated start and restarts one fresh generation after dispose', async () => {
    vi.useFakeTimers();
    const listActionJobs = vi.fn().mockResolvedValue({ jobs: [] });
    const jobs = createJobStore('project-a', projectPort({ listActionJobs }));
    const firstDeps = noopDeps();
    const latestDeps = noopDeps();

    jobs.start(firstDeps);
    jobs.start(latestDeps);
    await vi.advanceTimersByTimeAsync(0);
    expect(listActionJobs).toHaveBeenCalledTimes(1);

    jobs.dispose();
    jobs.start(latestDeps);
    await vi.advanceTimersByTimeAsync(0);
    expect(listActionJobs).toHaveBeenCalledTimes(2);
  });

  it('owns immediate-plus-fixed queued cadence and target replacement does not reset phase or lead twice', async () => {
    vi.useFakeTimers();
    setDocumentHidden(false);
    const runAction = vi
      .fn()
      .mockResolvedValueOnce({ runId: null, jobId: 1, receiptId: null, status: 'queued' })
      .mockResolvedValueOnce({ runId: null, jobId: 2, receiptId: null, status: 'queued' });
    const getActionJob = vi.fn().mockResolvedValue(actionJob());
    const listActionJobs = vi.fn().mockResolvedValue({ jobs: [] });
    const jobs = createJobStore(
      'project-a',
      projectPort({ runAction, getActionJob, listActionJobs }),
    );
    jobs.start(noopDeps());
    await vi.advanceTimersByTimeAsync(0);
    listActionJobs.mockClear();

    const req = runRequest();
    jobs.startRun(req, { id: 'sheet-1', rowCount: 1 } as never);
    await Promise.resolve();
    await Promise.resolve();
    expect(getActionJob).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(400);
    jobs.startRun(runRequest({ targetColumnId: 'col-2' }), { id: 'sheet-1', rowCount: 1 } as never);
    await Promise.resolve();
    await Promise.resolve();
    expect(getActionJob).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(349);
    expect(getActionJob).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(getActionJob).toHaveBeenCalledTimes(2);
  });

  it('skips hidden fixed ticks and catches up each active lane once without shifting fixed phase', async () => {
    vi.useFakeTimers();
    setDocumentHidden(false);
    const runAction = vi.fn().mockResolvedValue({ runId: 'run-a' });
    const getRunProgress = vi.fn().mockResolvedValue(runProgress());
    const listActionJobs = vi.fn().mockResolvedValue({ jobs: [] });
    const jobs = createJobStore(
      'project-a',
      projectPort({ runAction, getRunProgress, listActionJobs }),
    );
    jobs.start(noopDeps());
    await vi.advanceTimersByTimeAsync(0);
    jobs.startRun(runRequest(), { id: 'sheet-1', rowCount: 1 } as never);
    await Promise.resolve();
    await Promise.resolve();

    setDocumentHidden(true);
    await vi.advanceTimersByTimeAsync(600);
    expect(getRunProgress).not.toHaveBeenCalled();

    setDocumentHidden(false);
    document.dispatchEvent(new Event('visibilitychange'));
    await Promise.resolve();
    expect(getRunProgress).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(299);
    expect(getRunProgress).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(getRunProgress).toHaveBeenCalledTimes(2);
  });

  it('lets idle queued and dock lanes catch up while the run lane is in flight', async () => {
    vi.useFakeTimers();
    setDocumentHidden(false);
    const pendingRun = deferred<RunProgress>();
    const listActionJobs = vi.fn().mockResolvedValue({ jobs: [] });
    const getActionJob = vi.fn().mockResolvedValue(actionJob({ status: 'running' }));
    const getRunProgress = vi.fn().mockReturnValue(pendingRun.promise);
    const runAction = vi.fn()
      .mockResolvedValueOnce({ runId: null, jobId: 1, receiptId: null, status: 'queued' })
      .mockResolvedValueOnce({ runId: 'run-a' });
    const jobs = createJobStore('project-a', projectPort({
      getActionJob,
      getRunProgress,
      listActionJobs,
      runAction,
    }));
    const sheet = { id: 'sheet-1', rowCount: 1 } as never;
    jobs.start(noopDeps());
    await vi.advanceTimersByTimeAsync(0);
    jobs.startRun(runRequest(), sheet);
    await flushMicrotasks();
    jobs.startRun(runRequest({ targetColumnId: 'run-target' }), sheet);
    await flushMicrotasks();
    jobs.liveActionJobs.start();
    await flushMicrotasks();
    expect(getActionJob).toHaveBeenCalledTimes(1);
    listActionJobs.mockClear();

    await vi.advanceTimersByTimeAsync(300);
    expect(getRunProgress).toHaveBeenCalledTimes(1);
    setDocumentHidden(true);
    setDocumentHidden(false);
    document.dispatchEvent(new Event('visibilitychange'));
    await flushMicrotasks();

    expect(getRunProgress).toHaveBeenCalledTimes(1);
    expect(getActionJob).toHaveBeenCalledTimes(2);
    expect(listActionJobs).toHaveBeenCalledTimes(2);
  });

  it('owns visible settle-relative dock polling, silent dock LKG, and exact dock-wins merge order', async () => {
    vi.useFakeTimers();
    setDocumentHidden(false);
    const baseOlder = actionJob({ jobId: 1, actionName: 'base', timing: {
      createdAt: '2026-08-08T00:00:00Z', startedAt: null, finishedAt: null,
    } });
    const dockWinner = actionJob({ jobId: 1, actionName: 'dock', timing: {
      createdAt: '2026-08-08T00:00:00Z', startedAt: '2026-08-08T00:00:01Z', finishedAt: null,
    } });
    const dockNewest = actionJob({ jobId: 2, actionName: 'newest', timing: {
      createdAt: '2026-08-08T00:00:02Z', startedAt: null, finishedAt: null,
    } });
    const listActionJobs = vi
      .fn()
      .mockResolvedValueOnce({ jobs: [baseOlder] })
      .mockResolvedValueOnce({ jobs: [dockWinner, dockNewest] })
      .mockRejectedValueOnce(new Error('transient dock failure'))
      .mockResolvedValue({ jobs: [dockWinner, dockNewest] });
    const jobs = createJobStore('project-a', projectPort({ listActionJobs }));
    jobs.start(noopDeps());
    await vi.advanceTimersByTimeAsync(0);

    jobs.liveActionJobs.start();
    await flushMicrotasks();
    expect(jobs.store.get().actionJobs.jobs.map((job) => [job.jobId, job.actionName])).toEqual([
      [2, 'newest'],
      [1, 'dock'],
    ]);
    expect(jobs.store.get().actionJobs.error).toBeNull();

    await vi.advanceTimersByTimeAsync(2_000);
    expect(listActionJobs).toHaveBeenCalledTimes(3);
    expect(jobs.store.get().actionJobs.jobs.map((job) => job.actionName)).toEqual(['newest', 'dock']);
    expect(jobs.store.get().actionJobs.error).toBeNull();
  });

  it('suppresses a hidden dock leading request and catches up once on refocus', async () => {
    vi.useFakeTimers();
    setDocumentHidden(true);
    const listActionJobs = vi.fn().mockResolvedValue({ jobs: [] });
    const jobs = createJobStore('project-a', projectPort({ listActionJobs }));
    jobs.start(noopDeps());
    await vi.advanceTimersByTimeAsync(0);
    listActionJobs.mockClear();

    jobs.liveActionJobs.start();
    await vi.advanceTimersByTimeAsync(6_000);
    expect(listActionJobs).not.toHaveBeenCalled();

    setDocumentHidden(false);
    document.dispatchEvent(new Event('visibilitychange'));
    await Promise.resolve();
    expect(listActionJobs).toHaveBeenCalledTimes(1);
  });

  it('keeps terminal progress caches project-local, evicts on reactivation, and clears on dispose', async () => {
    vi.useFakeTimers();
    setDocumentHidden(false);
    const terminal = actionJob({ jobId: 1, runId: 'same-run', status: 'completed' });
    const active = actionJob({ jobId: 1, runId: 'same-run', status: 'running' });
    const listA = vi.fn()
      .mockResolvedValueOnce({ jobs: [] })
      .mockResolvedValueOnce({ jobs: [terminal] })
      .mockResolvedValueOnce({ jobs: [terminal] })
      .mockResolvedValueOnce({ jobs: [active] })
      .mockResolvedValue({ jobs: [terminal] });
    const listB = vi.fn()
      .mockResolvedValueOnce({ jobs: [] })
      .mockResolvedValue({ jobs: [terminal] });
    const progressA = vi.fn().mockResolvedValue(runProgress({ runId: 'same-run', status: 'complete' }));
    const progressB = vi.fn().mockResolvedValue(runProgress({ runId: 'same-run', status: 'complete' }));
    const a = createJobStore('project-a', projectPort({ listActionJobs: listA, getRunProgress: progressA }));
    const b = createJobStore('project-b', projectPort({ listActionJobs: listB, getRunProgress: progressB }));
    a.start(noopDeps());
    await vi.advanceTimersByTimeAsync(0);
    a.liveActionJobs.start();
    await flushMicrotasks();
    expect(progressA).toHaveBeenCalledTimes(1);

    // Project B begins only after A has cached the same run id; a module-global
    // cache would incorrectly suppress B's own immutable-project request.
    b.start(noopDeps());
    await vi.advanceTimersByTimeAsync(0);
    b.liveActionJobs.start();
    await flushMicrotasks();
    expect(progressB).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(2_000);
    expect(progressA).toHaveBeenCalledTimes(1);
    expect(progressB).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(2_000);
    expect(progressA).toHaveBeenCalledTimes(2);
    a.dispose();
    a.start(noopDeps());
    await vi.advanceTimersByTimeAsync(0);
    a.liveActionJobs.start();
    await Promise.resolve();
    await Promise.resolve();
    expect(progressA).toHaveBeenCalledTimes(3);
  });

  it('caches a terminal progress failure as null instead of refetching it', async () => {
    vi.useFakeTimers();
    const terminal = actionJob({ jobId: 1, runId: 'failed-progress', status: 'completed' });
    const getRunProgress = vi.fn().mockRejectedValue(new Error('progress unavailable'));
    const jobs = createJobStore('project-a', projectPort({
      listActionJobs: vi.fn().mockResolvedValue({ jobs: [terminal] }),
      getRunProgress,
    }));
    jobs.start(noopDeps());
    await vi.advanceTimersByTimeAsync(0);
    expect(jobs.store.get().actionJobs.jobs[0]?.progress).toBeNull();

    await jobs.refresh();

    expect(getRunProgress).toHaveBeenCalledTimes(1);
    expect(jobs.store.get().actionJobs.jobs[0]?.progress).toBeNull();
  });

  it('fences stale ordinary-list settlement across dispose/restart without clearing successor loading', async () => {
    vi.useFakeTimers();
    const oldList = deferred<{ jobs: ActionJob[] }>();
    const newList = deferred<{ jobs: ActionJob[] }>();
    const listActionJobs = vi
      .fn()
      .mockReturnValueOnce(oldList.promise)
      .mockReturnValueOnce(newList.promise);
    const jobs = createJobStore('project-a', projectPort({ listActionJobs }));
    jobs.start(noopDeps());
    await vi.advanceTimersByTimeAsync(0);
    jobs.dispose();
    jobs.start(noopDeps());
    await vi.advanceTimersByTimeAsync(0);

    oldList.resolve({ jobs: [actionJob({ jobId: 1, actionName: 'stale' })] });
    await Promise.resolve();
    await Promise.resolve();
    expect(jobs.store.get().actionJobs.jobs).toEqual([]);
    expect(jobs.store.get().actionJobs.loading).toBe(true);

    newList.resolve({ jobs: [actionJob({ jobId: 2, actionName: 'current' })] });
    await flushMicrotasks();
    expect(jobs.store.get().actionJobs.jobs.map((job) => job.jobId)).toEqual([2]);
    expect(jobs.store.get().actionJobs.loading).toBe(false);
  });

  it('uses resource generation even when a hostile controller factory reuses slot identity', async () => {
    vi.useFakeTimers();
    const sharedController = new AbortController();
    class ReusedAbortController {
      constructor() {
        return sharedController;
      }
    }
    vi.stubGlobal('AbortController', ReusedAbortController);
    try {
      const oldList = deferred<{ jobs: ActionJob[] }>();
      const newList = deferred<{ jobs: ActionJob[] }>();
      const listActionJobs = vi.fn()
        .mockReturnValueOnce(oldList.promise)
        .mockReturnValueOnce(newList.promise);
      const jobs = createJobStore('project-a', projectPort({ listActionJobs }));
      jobs.start(noopDeps());
      await vi.advanceTimersByTimeAsync(0);
      jobs.dispose();
      jobs.start(noopDeps());
      await vi.advanceTimersByTimeAsync(0);

      oldList.resolve({ jobs: [actionJob({ jobId: 1, actionName: 'stale' })] });
      await flushMicrotasks();

      expect(jobs.store.get().actionJobs.jobs).toEqual([]);
      expect(jobs.store.get().actionJobs.loading).toBe(true);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('does not dispatch an ordinary list retired by its loading publication', async () => {
    vi.useFakeTimers();
    const listActionJobs = vi.fn().mockResolvedValue({ jobs: [] });
    const jobs = createJobStore('project-a', projectPort({ listActionJobs }));
    jobs.start(noopDeps());
    let restarted = false;
    const unsubscribe = jobs.store.subscribe(() => {
      if (restarted || !jobs.store.get().actionJobs.loading) return;
      restarted = true;
      jobs.dispose();
      jobs.start(noopDeps());
    });

    await jobs.refresh();
    expect(restarted).toBe(true);
    expect(listActionJobs).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(0);
    unsubscribe();
    expect(listActionJobs).toHaveBeenCalledTimes(1);
    expect(listActionJobs.mock.calls[0][2]).toEqual({
      projectId: 'project-a',
      signal: expect.any(AbortSignal),
    });
  });

  it('forwards immutable project plus signal and clears collision/consent without confirming on dispose', async () => {
    const runAction = vi.fn().mockRejectedValue(
      new ApiError(409, 'collision', 'output_column_exists', { columns: ['result'] }),
    );
    const jobs = createJobStore('project-a', projectPort({ runAction }));
    jobs.start(noopDeps());
    const confirmation = jobs.requestCostConfirmation({ cost: 1, rows: 1 }, 'confirm');
    jobs.startRun(runRequest({ actionKind: 'map.extract', targetColumnId: 'result' }), {
      id: 'sheet-1', rowCount: 1,
    } as never);
    await Promise.resolve();
    await Promise.resolve();
    expect(runAction).toHaveBeenCalledWith(expect.anything(), {
      projectId: 'project-a',
      signal: expect.any(AbortSignal),
    });
    expect(jobs.store.get().outputColumnCollision).not.toBeNull();

    jobs.dispose();
    await expect(confirmation).resolves.toBe(false);
    expect(jobs.store.get().costGate).toBeNull();
    expect(jobs.store.get().outputColumnCollision).toBeNull();
    expect(runAction).toHaveBeenCalledTimes(1);
  });
});

describe('createJobStore — complete 4B cadence, fencing, and transport pins', () => {
  it('arms the dock lane 2s after settlement, never on a fixed request phase', async () => {
    vi.useFakeTimers();
    setDocumentHidden(false);
    const firstDock = deferred<{ jobs: ActionJob[] }>();
    const listActionJobs = vi.fn()
      .mockResolvedValueOnce({ jobs: [] })
      .mockReturnValueOnce(firstDock.promise)
      .mockResolvedValue({ jobs: [] });
    const jobs = createJobStore('project-a', projectPort({ listActionJobs }));
    jobs.start(noopDeps());
    await vi.advanceTimersByTimeAsync(0);

    jobs.liveActionJobs.start();
    expect(listActionJobs).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(listActionJobs).toHaveBeenCalledTimes(2);

    firstDock.resolve({ jobs: [] });
    await flushMicrotasks();
    await vi.advanceTimersByTimeAsync(1_999);
    expect(listActionJobs).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(listActionJobs).toHaveBeenCalledTimes(3);
  });

  it('does not overlap a live-dock request during repeated visible catch-ups', async () => {
    vi.useFakeTimers();
    setDocumentHidden(false);
    const pendingDock = deferred<{ jobs: ActionJob[] }>();
    const listActionJobs = vi.fn()
      .mockResolvedValueOnce({ jobs: [] })
      .mockReturnValueOnce(pendingDock.promise)
      .mockResolvedValue({ jobs: [] });
    const jobs = createJobStore('project-a', projectPort({ listActionJobs }));
    jobs.start(noopDeps());
    await vi.advanceTimersByTimeAsync(0);
    jobs.liveActionJobs.start();
    expect(listActionJobs).toHaveBeenCalledTimes(2);

    document.dispatchEvent(new Event('visibilitychange'));
    document.dispatchEvent(new Event('visibilitychange'));
    await flushMicrotasks();
    expect(listActionJobs).toHaveBeenCalledTimes(2);

    pendingDock.resolve({ jobs: [] });
    await flushMicrotasks();
    document.dispatchEvent(new Event('visibilitychange'));
    await flushMicrotasks();
    expect(listActionJobs).toHaveBeenCalledTimes(3);
  });

  it('keeps both fixed lanes active, hidden-aware, and independently non-overlapping', async () => {
    vi.useFakeTimers();
    setDocumentHidden(false);
    const queuedRequest = deferred<ActionJob>();
    const runRequestResult = deferred<RunProgress>();
    const runAction = vi.fn()
      .mockResolvedValueOnce({ runId: null, jobId: 1, receiptId: null, status: 'queued' })
      .mockResolvedValueOnce({ runId: 'run-a' });
    const getActionJob = vi.fn().mockReturnValue(queuedRequest.promise);
    const getRunProgress = vi.fn().mockReturnValue(runRequestResult.promise);
    const jobs = createJobStore('project-a', projectPort({
      runAction,
      getActionJob,
      getRunProgress,
      listActionJobs: vi.fn().mockResolvedValue({ jobs: [] }),
    }));
    jobs.start(noopDeps());
    await vi.advanceTimersByTimeAsync(0);
    jobs.startRun(runRequest(), { id: 'sheet-1', rowCount: 1 } as never);
    await flushMicrotasks();
    jobs.startRun(runRequest({ targetColumnId: 'col-2' }), {
      id: 'sheet-1', rowCount: 1,
    } as never);
    await flushMicrotasks();

    expect(getActionJob).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1_500);
    expect(getActionJob).toHaveBeenCalledTimes(1);
    expect(getRunProgress).toHaveBeenCalledTimes(1);

    setDocumentHidden(true);
    queuedRequest.resolve(actionJob({ status: 'running' }));
    runRequestResult.resolve(runProgress({ status: 'running' }));
    await flushMicrotasks();
    await vi.advanceTimersByTimeAsync(1_500);
    expect(getActionJob).toHaveBeenCalledTimes(1);
    expect(getRunProgress).toHaveBeenCalledTimes(1);

    setDocumentHidden(false);
    document.dispatchEvent(new Event('visibilitychange'));
    await flushMicrotasks();
    expect(getActionJob).toHaveBeenCalledTimes(2);
    expect(getRunProgress).toHaveBeenCalledTimes(2);
  });

  it('does not let an old run-slot finally clear a restarted generation successor slot', async () => {
    vi.useFakeTimers();
    setDocumentHidden(false);
    const oldProgress = deferred<RunProgress>();
    const successorProgress = deferred<RunProgress>();
    const getRunProgress = vi.fn()
      .mockReturnValueOnce(oldProgress.promise)
      .mockReturnValueOnce(successorProgress.promise);
    const runAction = vi.fn()
      .mockResolvedValueOnce({ runId: 'run-old' })
      .mockResolvedValueOnce({ runId: 'run-successor' });
    const jobs = createJobStore('project-a', projectPort({
      runAction,
      getRunProgress,
      listActionJobs: vi.fn().mockResolvedValue({ jobs: [] }),
    }));
    jobs.start(noopDeps());
    await vi.advanceTimersByTimeAsync(0);
    jobs.startRun(runRequest(), { id: 'sheet-1', rowCount: 1 } as never);
    await flushMicrotasks();
    await vi.advanceTimersByTimeAsync(300);
    expect(getRunProgress).toHaveBeenCalledTimes(1);

    jobs.dispose();
    jobs.start(noopDeps());
    await vi.advanceTimersByTimeAsync(0);
    jobs.startRun(runRequest({ targetColumnId: 'successor' }), {
      id: 'sheet-1', rowCount: 1,
    } as never);
    await flushMicrotasks();
    await vi.advanceTimersByTimeAsync(300);
    expect(getRunProgress).toHaveBeenCalledTimes(2);

    oldProgress.resolve(runProgress({ runId: 'run-old', status: 'running' }));
    await flushMicrotasks();
    await vi.advanceTimersByTimeAsync(300);
    expect(getRunProgress).toHaveBeenCalledTimes(2);
  });

  it('retires pending run and queued slots on target replacement without delaying successor phase', async () => {
    vi.useFakeTimers();
    setDocumentHidden(false);
    const queuedOld = deferred<ActionJob>();
    const queuedNew = deferred<ActionJob>();
    const runOld = deferred<RunProgress>();
    const runNew = deferred<RunProgress>();
    const getActionJob = vi.fn()
      .mockReturnValueOnce(queuedOld.promise)
      .mockReturnValueOnce(queuedNew.promise);
    const getRunProgress = vi.fn()
      .mockReturnValueOnce(runOld.promise)
      .mockReturnValueOnce(runNew.promise);
    const runAction = vi.fn()
      .mockResolvedValueOnce({ runId: null, jobId: 1, receiptId: null, status: 'queued' })
      .mockResolvedValueOnce({ runId: null, jobId: 2, receiptId: null, status: 'queued' })
      .mockResolvedValueOnce({ runId: 'run-old' })
      .mockResolvedValueOnce({ runId: 'run-new' });
    const jobs = createJobStore('project-a', projectPort({
      getActionJob,
      getRunProgress,
      runAction,
      listActionJobs: vi.fn().mockResolvedValue({ jobs: [] }),
    }));
    jobs.start(noopDeps());
    await vi.advanceTimersByTimeAsync(0);

    jobs.startRun(runRequest(), { id: 'sheet-1', rowCount: 1 } as never);
    await flushMicrotasks();
    await vi.advanceTimersByTimeAsync(400);
    jobs.startRun(runRequest({ targetColumnId: 'queued-new' }), {
      id: 'sheet-1', rowCount: 1,
    } as never);
    await flushMicrotasks();
    await vi.advanceTimersByTimeAsync(350);
    expect(getActionJob).toHaveBeenCalledTimes(2);
    queuedOld.resolve(actionJob({ jobId: 1, status: 'running' }));
    await flushMicrotasks();
    await vi.advanceTimersByTimeAsync(750);
    expect(getActionJob).toHaveBeenCalledTimes(2);

    jobs.startRun(runRequest({ targetColumnId: 'run-old' }), {
      id: 'sheet-1', rowCount: 1,
    } as never);
    await flushMicrotasks();
    await vi.advanceTimersByTimeAsync(300);
    jobs.startRun(runRequest({ targetColumnId: 'run-new' }), {
      id: 'sheet-1', rowCount: 1,
    } as never);
    await flushMicrotasks();
    await vi.advanceTimersByTimeAsync(300);
    expect(getRunProgress).toHaveBeenCalledTimes(2);
    runOld.resolve(runProgress({ runId: 'run-old', status: 'running' }));
    await flushMicrotasks();
    await vi.advanceTimersByTimeAsync(300);
    expect(getRunProgress).toHaveBeenCalledTimes(2);
  });

  it('does not let terminal-target abort listeners clobber synchronous successors', async () => {
    vi.useFakeTimers();
    setDocumentHidden(false);
    const sheet = { id: 'sheet-1', rowCount: 1 } as never;

    const terminalRun = deferred<RunProgress>();
    const runSignals: AbortSignal[] = [];
    const getRunProgress = vi.fn().mockImplementation(((_runId, options) => {
      runSignals.push(options!.signal!);
      return runSignals.length === 1
        ? terminalRun.promise
        : Promise.resolve(runProgress({ runId: 'run-successor', status: 'running' }));
    }) as ProjectApiPort['getRunProgress']);
    const synchronousRunSuccessor = {
      then(onFulfilled: (value: RunActionLaunchResult) => void) {
        onFulfilled({ runId: 'run-successor' });
        return Promise.resolve();
      },
    } as Promise<RunActionLaunchResult>;
    const runAction = vi.fn()
      .mockResolvedValueOnce({ runId: 'run-terminal' })
      .mockReturnValueOnce(synchronousRunSuccessor);
    const runJobs = createJobStore('project-a', projectPort({
      getRunProgress,
      listActionJobs: vi.fn().mockResolvedValue({ jobs: [] }),
      runAction,
    }));
    runJobs.start(noopDeps());
    await vi.advanceTimersByTimeAsync(0);
    runJobs.startRun(runRequest(), sheet);
    await flushMicrotasks();
    await vi.advanceTimersByTimeAsync(300);
    runSignals[0].addEventListener('abort', () => {
      runJobs.startRun(runRequest({ targetColumnId: 'run-successor' }), sheet);
    }, { once: true });

    terminalRun.resolve(runProgress({ runId: 'run-terminal', status: 'complete' }));
    await flushMicrotasks();
    await vi.advanceTimersByTimeAsync(300);
    expect(getRunProgress).toHaveBeenCalledTimes(2);
    expect(getRunProgress.mock.calls[1][0]).toBe('run-successor');

    const terminalQueued = deferred<ActionJob>();
    const queuedSignals: AbortSignal[] = [];
    const getActionJob = vi.fn().mockImplementation(((_jobId, options) => {
      queuedSignals.push(options!.signal!);
      return queuedSignals.length === 1
        ? terminalQueued.promise
        : Promise.resolve(actionJob({ jobId: 2, status: 'running' }));
    }) as NonNullable<ProjectApiPort['getActionJob']>);
    const synchronousQueuedSuccessor = {
      then(onFulfilled: (value: RunActionLaunchResult) => void) {
        onFulfilled({ runId: null, jobId: 2, receiptId: null, status: 'queued' });
        return Promise.resolve();
      },
    } as Promise<RunActionLaunchResult>;
    const queuedAction = vi.fn()
      .mockResolvedValueOnce({ runId: null, jobId: 1, receiptId: null, status: 'queued' })
      .mockReturnValueOnce(synchronousQueuedSuccessor);
    const queuedJobs = createJobStore('project-a', projectPort({
      getActionJob,
      listActionJobs: vi.fn().mockResolvedValue({ jobs: [] }),
      runAction: queuedAction,
    }));
    queuedJobs.start(noopDeps());
    await vi.advanceTimersByTimeAsync(0);
    queuedJobs.startRun(runRequest(), sheet);
    await flushMicrotasks();
    queuedSignals[0].addEventListener('abort', () => {
      queuedJobs.startRun(runRequest({ targetColumnId: 'queued-successor' }), sheet);
    }, { once: true });

    terminalQueued.resolve(actionJob({ jobId: 1, status: 'completed' }));
    await flushMicrotasks();
    expect(getActionJob).toHaveBeenCalledTimes(2);
    expect(getActionJob.mock.calls[1][0]).toBe(2);
  });

  it('keeps synchronous run and queued successors owned after replacement abort re-entry', async () => {
    vi.useFakeTimers();
    setDocumentHidden(false);
    const sheet = { id: 'sheet-1', rowCount: 1 } as never;
    const synchronousLaunch = (result: RunActionLaunchResult) => ({
      then(onFulfilled: (value: RunActionLaunchResult) => void) {
        onFulfilled(result);
        return Promise.resolve();
      },
    }) as Promise<RunActionLaunchResult>;

    const pendingRunA = deferred<RunProgress>();
    const runPollIds: string[] = [];
    const runPollSignals: AbortSignal[] = [];
    const getRunProgress = vi.fn().mockImplementation(((runId, options) => {
      runPollIds.push(runId);
      runPollSignals.push(options!.signal!);
      return runPollIds.length === 1
        ? pendingRunA.promise
        : Promise.resolve(runProgress({ runId, status: 'running' }));
    }) as ProjectApiPort['getRunProgress']);
    const runAction = vi.fn()
      .mockResolvedValueOnce({ runId: 'run-a' })
      .mockResolvedValueOnce({ runId: 'run-outer-b' })
      .mockReturnValueOnce(synchronousLaunch({ runId: 'run-reentrant-c' }))
      .mockResolvedValueOnce({ runId: 'run-d' });
    const runJobs = createJobStore('project-a', projectPort({
      getRunProgress,
      listActionJobs: vi.fn().mockResolvedValue({ jobs: [] }),
      runAction,
    }));
    runJobs.start(noopDeps());
    await vi.advanceTimersByTimeAsync(0);
    runJobs.startRun(runRequest(), sheet);
    await flushMicrotasks();
    await vi.advanceTimersByTimeAsync(300);
    runPollSignals[0].addEventListener('abort', () => {
      runJobs.startRun(runRequest({ targetColumnId: 'run-c' }), sheet);
    }, { once: true });

    runJobs.startRun(runRequest({ targetColumnId: 'run-b' }), sheet);
    await flushMicrotasks();
    await vi.advanceTimersByTimeAsync(300);
    expect(runPollIds).toEqual(['run-a', 'run-reentrant-c']);
    const runCSignal = runPollSignals[1];
    expect(runCSignal.aborted).toBe(false);

    runJobs.startRun(runRequest({ targetColumnId: 'run-d' }), sheet);
    await flushMicrotasks();
    expect(runCSignal.aborted).toBe(true);
    await vi.advanceTimersByTimeAsync(300);
    expect(runPollIds).toEqual(['run-a', 'run-reentrant-c', 'run-d']);
    const runDSignal = runPollSignals[2];
    expect(runDSignal.aborted).toBe(false);
    runJobs.dispose();
    expect(runDSignal.aborted).toBe(true);
    pendingRunA.resolve(runProgress({ runId: 'run-a', status: 'complete' }));
    await flushMicrotasks();

    const pendingQueuedA = deferred<ActionJob>();
    const queuedPollIds: number[] = [];
    const queuedPollSignals: AbortSignal[] = [];
    const getActionJob = vi.fn().mockImplementation(((jobId, options) => {
      const numericJobId = Number(jobId);
      queuedPollIds.push(numericJobId);
      queuedPollSignals.push(options!.signal!);
      return queuedPollIds.length === 1
        ? pendingQueuedA.promise
        : Promise.resolve(actionJob({ jobId: numericJobId, status: 'running' }));
    }) as NonNullable<ProjectApiPort['getActionJob']>);
    const queuedAction = vi.fn()
      .mockResolvedValueOnce({ runId: null, jobId: 1, receiptId: null, status: 'queued' })
      .mockResolvedValueOnce({ runId: null, jobId: 2, receiptId: null, status: 'queued' })
      .mockReturnValueOnce(synchronousLaunch({
        runId: null, jobId: 3, receiptId: null, status: 'queued',
      }))
      .mockResolvedValueOnce({ runId: null, jobId: 4, receiptId: null, status: 'queued' });
    const queuedJobs = createJobStore('project-a', projectPort({
      getActionJob,
      listActionJobs: vi.fn().mockResolvedValue({ jobs: [] }),
      runAction: queuedAction,
    }));
    queuedJobs.start(noopDeps());
    await vi.advanceTimersByTimeAsync(0);
    queuedJobs.startRun(runRequest(), sheet);
    await flushMicrotasks();
    queuedPollSignals[0].addEventListener('abort', () => {
      queuedJobs.startRun(runRequest({ targetColumnId: 'queued-c' }), sheet);
    }, { once: true });

    queuedJobs.startRun(runRequest({ targetColumnId: 'queued-b' }), sheet);
    await flushMicrotasks();
    await vi.advanceTimersByTimeAsync(750);
    expect(queuedPollIds).toEqual([1, 3]);
    const queuedCSignal = queuedPollSignals[1];
    expect(queuedCSignal.aborted).toBe(false);

    queuedJobs.startRun(runRequest({ targetColumnId: 'queued-d' }), sheet);
    await flushMicrotasks();
    expect(queuedCSignal.aborted).toBe(true);
    await vi.advanceTimersByTimeAsync(750);
    expect(queuedPollIds).toEqual([1, 3, 4]);
    const queuedDSignal = queuedPollSignals[2];
    expect(queuedDSignal.aborted).toBe(false);
    queuedJobs.dispose();
    expect(queuedDSignal.aborted).toBe(true);
    pendingQueuedA.resolve(actionJob({ jobId: 1, status: 'completed' }));
    await flushMicrotasks();
  });

  it('shares one concurrent terminal progress fetch, sorts base-only rows, and caches the result', async () => {
    vi.useFakeTimers();
    const progressResult = deferred<RunProgress>();
    const older = actionJob({
      jobId: 1,
      runId: 'terminal-run',
      status: 'completed',
      timing: { createdAt: '2026-08-08T00:00:00Z', startedAt: null, finishedAt: null },
    });
    const newer = actionJob({
      jobId: 2,
      runId: null,
      timing: { createdAt: '2026-08-08T00:00:02Z', startedAt: null, finishedAt: null },
    });
    const listActionJobs = vi.fn()
      .mockResolvedValueOnce({ jobs: [] })
      .mockResolvedValue({ jobs: [older, newer] });
    const getRunProgress = vi.fn().mockReturnValue(progressResult.promise);
    const jobs = createJobStore('project-a', projectPort({ listActionJobs, getRunProgress }));
    jobs.start(noopDeps());
    await vi.advanceTimersByTimeAsync(0);

    const first = jobs.refresh();
    const second = jobs.refresh();
    await flushMicrotasks();
    expect(getRunProgress).toHaveBeenCalledTimes(1);
    progressResult.resolve(runProgress({ runId: 'terminal-run', status: 'complete' }));
    await Promise.all([first, second]);
    expect(jobs.store.get().actionJobs.jobs.map((job) => job.jobId)).toEqual([2, 1]);

    await jobs.refresh();
    expect(getRunProgress).toHaveBeenCalledTimes(1);
  });

  it('dock-only dispose clears its snapshot but preserves base state and terminal cache', async () => {
    vi.useFakeTimers();
    setDocumentHidden(false);
    const base = actionJob({ jobId: 1, actionName: 'base' });
    const dock = actionJob({ jobId: 1, actionName: 'dock-winner' });
    const terminal = actionJob({ jobId: 2, runId: 'terminal-run', status: 'completed' });
    const listActionJobs = vi.fn()
      .mockResolvedValueOnce({ jobs: [base] })
      .mockResolvedValueOnce({ jobs: [dock, terminal] })
      .mockResolvedValueOnce({ jobs: [base, terminal] });
    const getRunProgress = vi.fn().mockResolvedValue(
      runProgress({ runId: 'terminal-run', status: 'complete' }),
    );
    const jobs = createJobStore('project-a', projectPort({ listActionJobs, getRunProgress }));
    jobs.start(noopDeps());
    await vi.advanceTimersByTimeAsync(0);
    jobs.liveActionJobs.start();
    await flushMicrotasks();
    expect(jobs.store.get().actionJobs.jobs.find((job) => job.jobId === 1)?.actionName)
      .toBe('dock-winner');

    jobs.liveActionJobs.dispose();
    expect(jobs.store.get().actionJobs).toEqual({ error: null, jobs: [base], loading: false });
    await jobs.refresh();
    expect(getRunProgress).toHaveBeenCalledTimes(1);
  });

  it('does not let a restarted dock join or publish retired terminal progress', async () => {
    vi.useFakeTimers();
    setDocumentHidden(false);
    const retiredProgress = deferred<RunProgress>();
    const terminal = actionJob({ jobId: 9, runId: 'terminal-run', status: 'completed' });
    const listActionJobs = vi.fn()
      .mockResolvedValueOnce({ jobs: [] })
      .mockResolvedValue({ jobs: [terminal] });
    const getRunProgress = vi.fn()
      .mockReturnValueOnce(retiredProgress.promise)
      .mockResolvedValueOnce(runProgress({
        runId: 'terminal-run', actionName: 'successor', status: 'complete',
      }));
    const jobs = createJobStore('project-a', projectPort({ listActionJobs, getRunProgress }));
    jobs.start(noopDeps());
    await vi.advanceTimersByTimeAsync(0);
    jobs.liveActionJobs.start();
    await flushMicrotasks();
    expect(getRunProgress).toHaveBeenCalledTimes(1);

    jobs.liveActionJobs.dispose();
    jobs.liveActionJobs.start();
    await flushMicrotasks();
    expect(getRunProgress).toHaveBeenCalledTimes(2);
    expect(jobs.store.get().actionJobs.jobs[0]?.progress?.actionName).toBe('successor');

    retiredProgress.resolve(runProgress({
      runId: 'terminal-run', actionName: 'retired', status: 'complete',
    }));
    await flushMicrotasks();
    expect(jobs.store.get().actionJobs.jobs[0]?.progress?.actionName).toBe('successor');
  });

  it('retries a base terminal join when its dock-owned progress is retired', async () => {
    vi.useFakeTimers();
    setDocumentHidden(false);
    const retiredDockProgress = deferred<RunProgress>();
    const terminal = actionJob({ jobId: 9, runId: 'terminal-run', status: 'completed' });
    const listActionJobs = vi.fn()
      .mockResolvedValueOnce({ jobs: [] })
      .mockResolvedValue({ jobs: [terminal] });
    const progressSignals: AbortSignal[] = [];
    const getRunProgress = vi.fn().mockImplementation(((_runId, options) => {
      progressSignals.push(options!.signal!);
      return progressSignals.length === 1
        ? retiredDockProgress.promise
        : Promise.resolve(runProgress({
            runId: 'terminal-run', actionName: 'base-retry', status: 'complete',
          }));
    }) as ProjectApiPort['getRunProgress']);
    const jobs = createJobStore('project-a', projectPort({ listActionJobs, getRunProgress }));
    jobs.start(noopDeps());
    await vi.advanceTimersByTimeAsync(0);
    jobs.liveActionJobs.start();
    await flushMicrotasks();
    expect(getRunProgress).toHaveBeenCalledTimes(1);

    const baseRefresh = jobs.refresh();
    await flushMicrotasks();
    expect(getRunProgress).toHaveBeenCalledTimes(1);
    jobs.liveActionJobs.dispose();
    expect(progressSignals[0].aborted).toBe(true);

    retiredDockProgress.resolve(runProgress({
      runId: 'terminal-run', actionName: 'retired-dock', status: 'complete',
    }));
    await baseRefresh;

    expect(getRunProgress).toHaveBeenCalledTimes(2);
    expect(progressSignals[1]).not.toBe(progressSignals[0]);
    expect(progressSignals[1].aborted).toBe(false);
    expect(jobs.store.get().actionJobs.jobs).toHaveLength(1);
    expect(jobs.store.get().actionJobs.jobs[0]?.progress?.actionName).toBe('base-retry');
  });

  it('joins a replacement dock terminal fetch instead of overwriting it during retry', async () => {
    vi.useFakeTimers();
    setDocumentHidden(false);
    const retiredDockProgress = deferred<RunProgress>();
    const successorDockProgress = deferred<RunProgress>();
    const terminal = actionJob({ jobId: 9, runId: 'terminal-run', status: 'completed' });
    const listActionJobs = vi.fn()
      .mockResolvedValueOnce({ jobs: [] })
      .mockResolvedValue({ jobs: [terminal] });
    const getRunProgress = vi.fn().mockImplementation((() => {
      if (getRunProgress.mock.calls.length === 1) return retiredDockProgress.promise;
      if (getRunProgress.mock.calls.length === 2) return successorDockProgress.promise;
      return Promise.resolve(runProgress({
        runId: 'terminal-run', actionName: 'unexpected-base-fetch', status: 'complete',
      }));
    }) as ProjectApiPort['getRunProgress']);
    const jobs = createJobStore('project-a', projectPort({ listActionJobs, getRunProgress }));
    jobs.start(noopDeps());
    await vi.advanceTimersByTimeAsync(0);
    jobs.liveActionJobs.start();
    await flushMicrotasks();
    expect(getRunProgress).toHaveBeenCalledTimes(1);

    const baseRefresh = jobs.refresh();
    await flushMicrotasks();
    expect(getRunProgress).toHaveBeenCalledTimes(1);

    jobs.liveActionJobs.dispose();
    jobs.liveActionJobs.start();
    await flushMicrotasks();
    expect(getRunProgress).toHaveBeenCalledTimes(2);

    retiredDockProgress.resolve(runProgress({
      runId: 'terminal-run', actionName: 'retired-dock', status: 'complete',
    }));
    await flushMicrotasks();
    expect(getRunProgress).toHaveBeenCalledTimes(2);

    successorDockProgress.resolve(runProgress({
      runId: 'terminal-run', actionName: 'successor-dock', status: 'complete',
    }));
    await baseRefresh;
    jobs.liveActionJobs.dispose();

    expect(getRunProgress).toHaveBeenCalledTimes(2);
    expect(jobs.store.get().actionJobs.jobs).toHaveLength(1);
    expect(jobs.store.get().actionJobs.jobs[0]?.progress?.actionName).toBe('successor-dock');
  });

  it('does not reinstall targets when replacement abort listeners dispose the resource', async () => {
    vi.useFakeTimers();
    setDocumentHidden(false);
    const queuedOld = deferred<ActionJob>();
    const queuedSignals: AbortSignal[] = [];
    const getActionJob = vi.fn().mockImplementation(((_id, options) => {
      queuedSignals.push(options!.signal!);
      return queuedSignals.length === 1
        ? queuedOld.promise
        : Promise.resolve(actionJob({ jobId: 3, status: 'running' }));
    }) as NonNullable<ProjectApiPort['getActionJob']>);
    const queuedRunAction = vi.fn()
      .mockResolvedValueOnce({ runId: null, jobId: 1, receiptId: null, status: 'queued' })
      .mockResolvedValueOnce({ runId: null, jobId: 2, receiptId: null, status: 'queued' })
      .mockResolvedValueOnce({ runId: null, jobId: 3, receiptId: null, status: 'queued' });
    const queued = createJobStore('project-a', projectPort({
      getActionJob,
      runAction: queuedRunAction,
      listActionJobs: vi.fn().mockResolvedValue({ jobs: [] }),
    }));
    const deps = noopDeps();
    queued.start(deps);
    await vi.advanceTimersByTimeAsync(0);
    queued.startRun(runRequest(), { id: 'sheet-1', rowCount: 1 } as never);
    await flushMicrotasks();
    queuedSignals[0].addEventListener('abort', () => queued.dispose(), { once: true });
    queued.startRun(runRequest({ targetColumnId: 'queued-replacement' }), {
      id: 'sheet-1', rowCount: 1,
    } as never);
    await flushMicrotasks();
    queued.start(deps);
    await vi.advanceTimersByTimeAsync(0);
    queued.startRun(runRequest({ targetColumnId: 'queued-after-restart' }), {
      id: 'sheet-1', rowCount: 1,
    } as never);
    await flushMicrotasks();
    expect(getActionJob).toHaveBeenCalledTimes(2);

    const runOld = deferred<RunProgress>();
    const runSignals: AbortSignal[] = [];
    const getRunProgress = vi.fn().mockImplementation(((_id, options) => {
      runSignals.push(options!.signal!);
      return runSignals.length === 1
        ? runOld.promise
        : Promise.resolve(runProgress({ runId: 'run-3', status: 'running' }));
    }) as ProjectApiPort['getRunProgress']);
    const directRunAction = vi.fn()
      .mockResolvedValueOnce({ runId: 'run-1' })
      .mockResolvedValueOnce({ runId: 'run-2' })
      .mockResolvedValueOnce({ runId: 'run-3' });
    const direct = createJobStore('project-a', projectPort({
      getRunProgress,
      runAction: directRunAction,
      listActionJobs: vi.fn().mockResolvedValue({ jobs: [] }),
    }));
    direct.start(deps);
    await vi.advanceTimersByTimeAsync(0);
    direct.startRun(runRequest(), { id: 'sheet-1', rowCount: 1 } as never);
    await flushMicrotasks();
    await vi.advanceTimersByTimeAsync(300);
    runSignals[0].addEventListener('abort', () => direct.dispose(), { once: true });
    direct.startRun(runRequest({ targetColumnId: 'run-replacement' }), {
      id: 'sheet-1', rowCount: 1,
    } as never);
    await flushMicrotasks();
    direct.start(deps);
    await vi.advanceTimersByTimeAsync(0);
    direct.startRun(runRequest({ targetColumnId: 'run-after-restart' }), {
      id: 'sheet-1', rowCount: 1,
    } as never);
    await flushMicrotasks();
    await vi.advanceTimersByTimeAsync(300);
    expect(getRunProgress).toHaveBeenCalledTimes(2);
  });

  it('retains exactly two queued-terminal list refreshes and collaborator order', async () => {
    vi.useFakeTimers();
    setDocumentHidden(false);
    const terminalJob = deferred<ActionJob>();
    const events: string[] = [];
    const listActionJobs = vi.fn(async () => {
      events.push('list');
      return { schemaVersion: '1', projectId: 'project-a', jobs: [] };
    });
    const deps: JobRunDeps = {
      invalidateProjectData: vi.fn(() => { events.push('invalidate'); }),
      refreshSheets: vi.fn(() => { events.push('sheets'); }),
      refreshHistory: vi.fn(() => { events.push('history'); }),
      refreshReviewCount: vi.fn(() => { events.push('review'); }),
      showError: vi.fn(() => { events.push('error'); }),
    };
    const jobs = createJobStore('project-a', projectPort({
      listActionJobs,
      getActionJob: vi.fn().mockReturnValue(terminalJob.promise),
      runAction: vi.fn().mockResolvedValue({
        runId: null, jobId: 7, receiptId: null, status: 'queued',
      }),
    }));
    jobs.start(deps);
    await vi.advanceTimersByTimeAsync(0);
    jobs.startRun(runRequest(), { id: 'sheet-1', rowCount: 1 } as never);
    await flushMicrotasks();
    listActionJobs.mockClear();
    events.length = 0;

    terminalJob.resolve(actionJob({ jobId: 7, status: 'failed', error: 'terminal error' }));
    await flushMicrotasks();
    expect(listActionJobs).toHaveBeenCalledTimes(2);
    expect(events).toEqual([
      'list',
      'invalidate',
      'sheets',
      'history',
      'review',
      'list',
      'error',
    ]);
  });

  it('rechecks queued identity after refresh loading publication replaces the target', async () => {
    vi.useFakeTimers();
    setDocumentHidden(false);
    const terminal = deferred<ActionJob>();
    const getActionJob = vi
      .fn()
      .mockReturnValueOnce(terminal.promise)
      .mockResolvedValue(actionJob({ jobId: 2, status: 'running' }));
    const successor = {
      then(onFulfilled: (value: RunActionLaunchResult) => void) {
        onFulfilled({ runId: null, jobId: 2, receiptId: null, status: 'queued' });
        return Promise.resolve();
      },
    } as Promise<RunActionLaunchResult>;
    const runAction = vi
      .fn()
      .mockResolvedValueOnce({ runId: null, jobId: 1, receiptId: null, status: 'queued' })
      .mockReturnValueOnce(successor);
    const jobs = createJobStore('project-a', projectPort({
      getActionJob,
      listActionJobs: vi.fn().mockResolvedValue({
        schemaVersion: '1', projectId: 'project-a', jobs: [],
      }),
      runAction,
    }));
    jobs.start(noopDeps());
    await vi.advanceTimersByTimeAsync(0);
    jobs.startRun(runRequest(), { id: 'sheet-1', rowCount: 1 } as never);
    await flushMicrotasks();

    let replaced = false;
    const unsubscribe = jobs.store.subscribe(() => {
      if (replaced || !jobs.store.get().actionJobs.loading) return;
      replaced = true;
      jobs.startRun(runRequest({ targetColumnId: 'successor' }), {
        id: 'sheet-1', rowCount: 1,
      } as never);
    });
    terminal.resolve(actionJob({ jobId: 1, status: 'done' }));
    await flushMicrotasks();
    unsubscribe();

    expect(replaced).toBe(true);
    await vi.advanceTimersByTimeAsync(750);
    expect(getActionJob).toHaveBeenCalledTimes(2);
    expect(getActionJob.mock.calls[1][0]).toBe(2);
  });

  it('checks the queued epoch after job settlement before issuing a receipt request', async () => {
    vi.useFakeTimers();
    const jobResult = deferred<ActionJob>();
    const getReceipt = vi.fn().mockResolvedValue({ status: 'running' });
    const deps = noopDeps();
    const jobs = createJobStore('project-a', projectPort({
      listActionJobs: vi.fn().mockResolvedValue({ jobs: [] }),
      getActionJob: vi.fn().mockReturnValue(jobResult.promise),
      getReceipt,
      runAction: vi.fn().mockResolvedValue({
        runId: null, jobId: 7, receiptId: 'receipt-7', status: 'queued',
      }),
    }));
    jobs.start(deps);
    await vi.advanceTimersByTimeAsync(0);
    jobs.startRun(runRequest(), { id: 'sheet-1', rowCount: 1 } as never);
    await flushMicrotasks();
    jobs.dispose();
    jobs.start(noopDeps());
    jobResult.resolve(actionJob({ jobId: 7, status: 'running' }));
    await flushMicrotasks();

    expect(getReceipt).not.toHaveBeenCalled();
    expect(deps.invalidateProjectData).not.toHaveBeenCalled();
  });

  it('drops stale cancel and backfill continuations after dispose/restart', async () => {
    vi.useFakeTimers();
    const cancelResult = deferred<RunProgress>();
    const backfillResult = deferred<RunProgress>();
    const cancelRun = vi.fn().mockReturnValue(cancelResult.promise);
    const getRunProgress = vi.fn().mockReturnValue(backfillResult.promise);
    const deps = noopDeps();
    const jobs = createJobStore('project-a', projectPort({
      cancelRun,
      getRunProgress,
      listActionJobs: vi.fn().mockResolvedValue({ jobs: [] }),
    }));
    jobs.start(deps);
    await vi.advanceTimersByTimeAsync(0);
    jobs.cancelCurrentRun('run-a');
    jobs.afterBackfill('run-a');
    vi.mocked(deps.invalidateProjectData).mockClear();
    vi.mocked(deps.refreshSheets).mockClear();
    vi.mocked(deps.refreshHistory).mockClear();
    vi.mocked(deps.refreshReviewCount).mockClear();
    jobs.dispose();
    jobs.start(noopDeps());

    cancelResult.resolve(runProgress({ runId: 'cancelled', status: 'cancelled' }));
    backfillResult.resolve(runProgress({ runId: 'backfilled', status: 'running' }));
    await flushMicrotasks();
    expect(jobs.store.get().run).toBeNull();
    expect(deps.invalidateProjectData).not.toHaveBeenCalled();
    expect(deps.refreshSheets).not.toHaveBeenCalled();
    expect(deps.refreshHistory).not.toHaveBeenCalled();
    expect(deps.refreshReviewCount).not.toHaveBeenCalled();
    expect(deps.showError).not.toHaveBeenCalled();
  });

  it('passes immutable project and exact request signals through every job transport call', async () => {
    vi.useFakeTimers();
    setDocumentHidden(false);
    const listActionJobs = vi.fn()
      .mockResolvedValueOnce({ jobs: [actionJob({
        jobId: 1, runId: 'base-run', status: 'completed',
      })] })
      .mockResolvedValue({ jobs: [] });
    const getRunProgress = vi.fn().mockResolvedValue(runProgress({ status: 'complete' }));
    const getActionJob = vi.fn().mockResolvedValue(actionJob({ jobId: 7, status: 'running' }));
    const getReceipt = vi.fn().mockResolvedValue({ status: 'running' });
    const cancelRun = vi.fn().mockResolvedValue(runProgress({ status: 'cancelled' }));
    const runAction = vi.fn().mockResolvedValue({
      runId: null, jobId: 7, receiptId: 'receipt-7', status: 'queued',
    });
    const jobs = createJobStore('project-a', projectPort({
      listActionJobs,
      getRunProgress,
      getActionJob,
      getReceipt,
      cancelRun,
      runAction,
    }));
    jobs.start(noopDeps());
    await vi.advanceTimersByTimeAsync(0);

    const baseOptions = listActionJobs.mock.calls[0]?.[2];
    const baseProgressOptions = getRunProgress.mock.calls.find(([id]) => id === 'base-run')?.[1];
    expect(baseOptions).toEqual({ projectId: 'project-a', signal: expect.any(AbortSignal) });
    expect(baseProgressOptions?.signal).toBe(baseOptions?.signal);

    jobs.startRun(runRequest(), { id: 'sheet-1', rowCount: 1 } as never);
    await flushMicrotasks();
    const queuedOptions = getActionJob.mock.calls[0]?.[1];
    const receiptOptions = getReceipt.mock.calls[0]?.[1];
    expect(runAction.mock.calls[0]?.[1]).toEqual({
      projectId: 'project-a', signal: expect.any(AbortSignal),
    });
    expect(queuedOptions).toEqual({ projectId: 'project-a', signal: expect.any(AbortSignal) });
    expect(receiptOptions?.signal).toBe(queuedOptions?.signal);

    jobs.cancelCurrentRun('cancel-run');
    jobs.afterBackfill('backfill-run');
    await flushMicrotasks();
    expect(cancelRun.mock.calls[0]?.[1]).toEqual({
      projectId: 'project-a', signal: expect.any(AbortSignal),
    });
    const backfillOptions = getRunProgress.mock.calls.find(([id]) => id === 'backfill-run')?.[1];
    expect(backfillOptions).toEqual({ projectId: 'project-a', signal: expect.any(AbortSignal) });
    expect(listActionJobs.mock.calls.every(([, , options]) => (
      options?.projectId === 'project-a' && options.signal instanceof AbortSignal
    ))).toBe(true);
  });

  it('keeps base LKG/error policy and lets the last valid settlement win without single-flight', async () => {
    vi.useFakeTimers();
    const first = deferred<{ jobs: ActionJob[] }>();
    const second = deferred<{ jobs: ActionJob[] }>();
    const third = deferred<{ jobs: ActionJob[] }>();
    const fourth = deferred<{ jobs: ActionJob[] }>();
    const listActionJobs = vi.fn()
      .mockResolvedValueOnce({ jobs: [actionJob({ jobId: 1, actionName: 'initial' })] })
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise)
      .mockReturnValueOnce(third.promise)
      .mockReturnValueOnce(fourth.promise);
    const jobs = createJobStore('project-a', projectPort({ listActionJobs }));
    jobs.start(noopDeps());
    await vi.advanceTimersByTimeAsync(0);

    const firstRefresh = jobs.refresh();
    const secondRefresh = jobs.refresh();
    expect(listActionJobs).toHaveBeenCalledTimes(3);
    first.resolve({ jobs: [actionJob({ jobId: 2, actionName: 'first-success' })] });
    await firstRefresh;
    second.reject(new Error('last settlement failed'));
    await secondRefresh;
    expect(jobs.store.get().actionJobs).toEqual({
      error: 'last settlement failed',
      jobs: [expect.objectContaining({ jobId: 2 })],
      loading: false,
    });

    const thirdRefresh = jobs.refresh();
    const fourthRefresh = jobs.refresh();
    fourth.resolve({ jobs: [actionJob({ jobId: 4, actionName: 'settled-earlier' })] });
    await fourthRefresh;
    third.resolve({ jobs: [actionJob({ jobId: 3, actionName: 'settled-last' })] });
    await thirdRefresh;
    expect(jobs.store.get().actionJobs).toEqual({
      error: null,
      jobs: [expect.objectContaining({ jobId: 3 })],
      loading: false,
    });
  });

  it('dock dispose preserves nondefault base loading/error while republishing base-only', async () => {
    vi.useFakeTimers();
    setDocumentHidden(false);
    const pendingBase = deferred<{ jobs: ActionJob[] }>();
    const base = actionJob({ jobId: 1, actionName: 'base' });
    const dock = actionJob({ jobId: 1, actionName: 'dock' });
    const listActionJobs = vi.fn()
      .mockResolvedValueOnce({ jobs: [base] })
      .mockResolvedValueOnce({ jobs: [dock] })
      .mockRejectedValueOnce(new Error('base error'))
      .mockReturnValueOnce(pendingBase.promise);
    const jobs = createJobStore('project-a', projectPort({ listActionJobs }));
    jobs.start(noopDeps());
    await vi.advanceTimersByTimeAsync(0);
    jobs.liveActionJobs.start();
    await flushMicrotasks();
    await jobs.refresh();
    void jobs.refresh();
    expect(jobs.store.get().actionJobs).toMatchObject({
      error: 'base error', loading: true,
    });

    jobs.liveActionJobs.dispose();
    expect(jobs.store.get().actionJobs).toEqual({
      error: 'base error', jobs: [base], loading: true,
    });
    jobs.dispose();
  });

  it('drops a delayed receipt settlement and all of its terminal follow-ups after restart', async () => {
    vi.useFakeTimers();
    const receiptResult = deferred<{ status: string }>();
    const listActionJobs = vi.fn().mockResolvedValue({ jobs: [] });
    const deps = noopDeps();
    const jobs = createJobStore('project-a', projectPort({
      listActionJobs,
      getActionJob: vi.fn().mockResolvedValue(actionJob({ jobId: 8, status: 'running' })),
      getReceipt: vi.fn().mockReturnValue(receiptResult.promise as never),
      runAction: vi.fn().mockResolvedValue({
        runId: null, jobId: 8, receiptId: 'receipt-8', status: 'queued',
      }),
    }));
    jobs.start(deps);
    await vi.advanceTimersByTimeAsync(0);
    jobs.startRun(runRequest(), { id: 'sheet-1', rowCount: 1 } as never);
    await flushMicrotasks();
    jobs.dispose();
    jobs.start(noopDeps());
    listActionJobs.mockClear();
    receiptResult.resolve({ status: 'completed' });
    await flushMicrotasks();

    expect(listActionJobs).not.toHaveBeenCalled();
    expect(deps.invalidateProjectData).not.toHaveBeenCalled();
    expect(deps.showError).not.toHaveBeenCalled();
  });

  it('supersedes same-generation cancel and backfill slots without stale publication', async () => {
    vi.useFakeTimers();
    const cancelOld = deferred<RunProgress>();
    const cancelNew = deferred<RunProgress>();
    const backfillOld = deferred<RunProgress>();
    const backfillNew = deferred<RunProgress>();
    const cancelRun = vi.fn()
      .mockReturnValueOnce(cancelOld.promise)
      .mockReturnValueOnce(cancelNew.promise);
    const getRunProgress = vi.fn()
      .mockReturnValueOnce(backfillOld.promise)
      .mockReturnValueOnce(backfillNew.promise);
    const deps = noopDeps();
    const jobs = createJobStore('project-a', projectPort({
      cancelRun,
      getRunProgress,
      listActionJobs: vi.fn().mockResolvedValue({ jobs: [] }),
    }));
    jobs.start(deps);
    await vi.advanceTimersByTimeAsync(0);
    jobs.cancelCurrentRun('cancel-old');
    jobs.cancelCurrentRun('cancel-new');
    jobs.afterBackfill('backfill-old');
    jobs.afterBackfill('backfill-new');
    vi.mocked(deps.invalidateProjectData).mockClear();
    vi.mocked(deps.refreshSheets).mockClear();
    vi.mocked(deps.refreshHistory).mockClear();
    vi.mocked(deps.refreshReviewCount).mockClear();

    cancelOld.resolve(runProgress({ runId: 'cancel-old', status: 'cancelled' }));
    backfillOld.resolve(runProgress({ runId: 'backfill-old' }));
    await flushMicrotasks();
    expect(jobs.store.get().run).toBeNull();
    expect(deps.invalidateProjectData).not.toHaveBeenCalled();

    cancelNew.resolve(runProgress({ runId: 'cancel-new', status: 'cancelled' }));
    await flushMicrotasks();
    expect(jobs.store.get().run?.runId).toBe('cancel-new');
    expect(deps.invalidateProjectData).toHaveBeenCalledTimes(1);
    backfillNew.resolve(runProgress({ runId: 'backfill-new' }));
    await flushMicrotasks();
    expect(jobs.store.get().run?.runId).toBe('backfill-new');
  });

  it('uses the latest repeated-start collaborator bundle at terminal settlement', async () => {
    vi.useFakeTimers();
    const firstDeps = noopDeps();
    const latestDeps = noopDeps();
    const jobs = createJobStore('project-a', projectPort({
      listActionJobs: vi.fn().mockResolvedValue({ jobs: [] }),
      runAction: vi.fn().mockResolvedValue({ runId: 'run-a' }),
      getRunProgress: vi.fn().mockResolvedValue(runProgress({ status: 'complete' })),
    }));
    jobs.start(firstDeps);
    jobs.start(latestDeps);
    await vi.advanceTimersByTimeAsync(0);
    jobs.startRun(runRequest(), { id: 'sheet-1', rowCount: 1 } as never);
    await flushMicrotasks();
    vi.mocked(latestDeps.refreshSheets).mockClear();
    await vi.advanceTimersByTimeAsync(300);

    expect(firstDeps.invalidateProjectData).not.toHaveBeenCalled();
    expect(firstDeps.refreshSheets).not.toHaveBeenCalled();
    expect(latestDeps.invalidateProjectData).toHaveBeenCalledTimes(1);
    expect(latestDeps.refreshSheets).toHaveBeenCalledTimes(1);
  });

  it('pre-fences synchronous resource and dock starts re-entered by disposal abort', async () => {
    vi.useFakeTimers();
    const pendingList = deferred<{ jobs: ActionJob[] }>();
    const listActionJobs = vi.fn()
      .mockReturnValueOnce(pendingList.promise)
      .mockResolvedValue({ jobs: [] });
    const jobs = createJobStore('project-a', projectPort({ listActionJobs }));
    const reentrantDeps = noopDeps();
    jobs.start(noopDeps());
    await vi.advanceTimersByTimeAsync(0);
    const signal = listActionJobs.mock.calls[0]?.[2]?.signal;
    expect(signal).toBeInstanceOf(AbortSignal);
    signal!.addEventListener('abort', () => {
      jobs.start(reentrantDeps);
      jobs.liveActionJobs.start();
      void jobs.refresh();
    }, { once: true });

    jobs.dispose();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(listActionJobs).toHaveBeenCalledTimes(1);
    jobs.start(reentrantDeps);
    await vi.advanceTimersByTimeAsync(0);
    expect(listActionJobs).toHaveBeenCalledTimes(2);

    jobs.liveActionJobs.start();
    const dockSignal = listActionJobs.mock.calls[2]?.[2]?.signal;
    expect(dockSignal).toBeInstanceOf(AbortSignal);
    dockSignal!.addEventListener('abort', () => jobs.liveActionJobs.start(), { once: true });
    jobs.liveActionJobs.dispose();
    await flushMicrotasks();
    expect(listActionJobs).toHaveBeenCalledTimes(3);
    jobs.liveActionJobs.start();
    expect(listActionJobs).toHaveBeenCalledTimes(4);
  });
});

describe('createJobStore — action-launch generation (WEB-03-4A)', () => {
  const request = runRequest();
  const proposal = {
    kind: 'map',
    title: 'Generation-fenced proposal',
    spec: {
      action_id: 'map.template',
      scope: { kind: 'sheet_rows', sheet_id: 1 },
      output_names: { rendered: 'result' },
      params: { template: { text: '{{source}}' } },
    },
  } as CopilotProposal;

  it('aborts and identity-fences a superseded launch even when the old fake ignores its signal', async () => {
    const jobs = createJobStore('test-project', projectPort());
    const first = deferred<RunActionLaunchResult>();
    const second = deferred<RunActionLaunchResult>();
    const signals: Array<AbortSignal | undefined> = [];
    const runAction = vi.spyOn(api, 'runAction').mockImplementation(((_req, options) => {
      signals.push(options?.signal);
      return signals.length === 1 ? first.promise : second.promise;
    }) as typeof api.runAction);
    vi.spyOn(api, 'listActionJobs').mockResolvedValue({ jobs: [] } as never);
    const deps = { ...noopDeps(), onLaunchAccepted: vi.fn() };
    jobs.start(deps);

    jobs.startRun(request, { id: 'sheet-1', rowCount: 5 } as never);
    jobs.startRun({ ...request, targetColumnId: 'col-2' }, {
      id: 'sheet-1', rowCount: 5,
    } as never);

    expect(runAction).toHaveBeenCalledTimes(2);
    expect(signals[0]).toBeDefined();
    expect(signals[0]?.aborted).toBe(true);
    expect(signals[1]?.aborted).toBe(false);

    first.resolve({ runId: 'run-a' });
    await Promise.resolve();
    await Promise.resolve();
    expect(jobs.store.get().run?.runId).not.toBe('run-a');
    expect(deps.onLaunchAccepted).not.toHaveBeenCalled();
    expect(deps.refreshSheets).not.toHaveBeenCalled();

    second.resolve({ runId: 'run-b' });
    await Promise.resolve();
    await Promise.resolve();
    expect(jobs.store.get().run?.runId).toBe('run-b');
    expect(deps.onLaunchAccepted).toHaveBeenCalledTimes(1);
  });

  it('does not clobber a newer launch started synchronously by the displaced abort listener', async () => {
    const jobs = createJobStore('test-project', projectPort());
    const first = deferred<RunActionLaunchResult>();
    const reentrant = deferred<RunActionLaunchResult>();
    const signals: AbortSignal[] = [];
    const runAction = vi.spyOn(api, 'runAction').mockImplementation(((_req, options) => {
      signals.push(options!.signal!);
      return signals.length === 1 ? first.promise : reentrant.promise;
    }) as typeof api.runAction);
    vi.spyOn(api, 'listActionJobs').mockResolvedValue({ jobs: [] } as never);
    const deps = { ...noopDeps(), onLaunchAccepted: vi.fn() };
    const sheet = { id: 'sheet-1', rowCount: 5 } as never;
    jobs.start(deps);

    jobs.startRun(request, sheet);
    signals[0].addEventListener('abort', () => {
      jobs.startRun(runRequest({ targetColumnId: 'col-reentrant' }), sheet);
    }, { once: true });

    jobs.startRun(runRequest({ targetColumnId: 'col-outer' }), sheet);

    expect(runAction).toHaveBeenCalledTimes(2);
    expect((runAction.mock.calls[1][0] as RegisteredActionRequest).output_names.result).toBe('col-reentrant');
    expect(signals[0].aborted).toBe(true);
    expect(signals[1].aborted).toBe(false);
    expect(jobs.store.get().run?.targetColumnId).toBe('col-reentrant');

    first.resolve({ runId: 'run-old' });
    reentrant.resolve({ runId: 'run-reentrant' });
    await Promise.resolve();
    await Promise.resolve();

    expect(jobs.store.get().run?.runId).toBe('run-reentrant');
    expect(deps.onLaunchAccepted).toHaveBeenCalledTimes(1);
  });

  it('retires an outer launch whose displaced abort listener disposes the store', async () => {
    const jobs = createJobStore('test-project', projectPort());
    const first = deferred<RunActionLaunchResult>();
    const restarted = deferred<RunActionLaunchResult>();
    const signals: AbortSignal[] = [];
    const runAction = vi.spyOn(api, 'runAction').mockImplementation(((_req, options) => {
      signals.push(options!.signal!);
      return signals.length === 1 ? first.promise : restarted.promise;
    }) as typeof api.runAction);
    vi.spyOn(api, 'listActionJobs').mockResolvedValue({ jobs: [] } as never);
    const deps = { ...noopDeps(), onLaunchAccepted: vi.fn() };
    const sheet = { id: 'sheet-1', rowCount: 5 } as never;
    jobs.start(deps);

    jobs.startRun(request, sheet);
    const beforeDispose = jobs.store.get().run;
    signals[0].addEventListener('abort', () => jobs.dispose(), { once: true });

    jobs.startRun({ ...request, targetColumnId: 'col-spanned-dispose' }, sheet);

    expect(runAction).toHaveBeenCalledTimes(1);
    expect(signals[0].aborted).toBe(true);
    expect(jobs.store.get().run).toBe(beforeDispose);

    jobs.start(deps);
    jobs.startRun(runRequest({ targetColumnId: 'col-after-dispose' }), sheet);
    expect(runAction).toHaveBeenCalledTimes(2);
    expect((runAction.mock.calls[1][0] as RegisteredActionRequest).output_names.result).toBe('col-after-dispose');
    expect(signals[1].aborted).toBe(false);

    first.resolve({ runId: 'run-retired' });
    restarted.resolve({ runId: 'run-after-dispose' });
    await Promise.resolve();
    await Promise.resolve();

    expect(jobs.store.get().run?.runId).toBe('run-after-dispose');
    expect(deps.onLaunchAccepted).toHaveBeenCalledTimes(1);
  });

  it('dispose aborts a launch and a late fake completion cannot publish or invoke callbacks', async () => {
    const jobs = createJobStore('test-project', projectPort());
    const launch = deferred<RunActionLaunchResult>();
    let signal: AbortSignal | undefined;
    vi.spyOn(api, 'runAction').mockImplementation(((_req, options) => {
      signal = options?.signal;
      return launch.promise;
    }) as typeof api.runAction);
    vi.spyOn(api, 'listActionJobs').mockResolvedValue({ jobs: [] } as never);
    const deps = { ...noopDeps(), onLaunchAccepted: vi.fn() };
    jobs.start(deps);

    jobs.startRun(request, { id: 'sheet-1', rowCount: 5 } as never);
    const beforeDispose = jobs.store.get().run;
    jobs.dispose();

    expect(signal).toBeDefined();
    expect(signal?.aborted).toBe(true);
    launch.resolve({ runId: 'late-run' });
    await Promise.resolve();
    await Promise.resolve();

    expect(jobs.store.get().run).toBe(beforeDispose);
    expect(deps.onLaunchAccepted).not.toHaveBeenCalled();
    expect(deps.refreshSheets).not.toHaveBeenCalled();
    expect(deps.showError).not.toHaveBeenCalled();
  });

  it('dispose keeps its launch guard through recursive disposal in an abort listener', async () => {
    const jobs = createJobStore('test-project', projectPort());
    const launch = deferred<RunActionLaunchResult>();
    let signal: AbortSignal | undefined;
    const runAction = vi.spyOn(api, 'runAction').mockImplementation(((_req, options) => {
      signal = options?.signal;
      return launch.promise;
    }) as typeof api.runAction);
    const deps = { ...noopDeps(), onLaunchAccepted: vi.fn() };
    const sheet = { id: 'sheet-1', rowCount: 5 } as never;
    jobs.start(deps);

    jobs.startRun(request, sheet);
    signal!.addEventListener('abort', () => {
      jobs.dispose();
      jobs.startRun({ ...request, targetColumnId: 'col-reentrant-dispose' }, sheet);
    }, { once: true });
    const beforeDispose = jobs.store.get().run;

    jobs.dispose();

    expect(signal?.aborted).toBe(true);
    expect(runAction).toHaveBeenCalledTimes(1);
    expect(jobs.store.get().run).toBe(beforeDispose);
    launch.resolve({ runId: 'late-disposed-run' });
    await Promise.resolve();
    await Promise.resolve();
    expect(jobs.store.get().run).toBe(beforeDispose);
    expect(deps.onLaunchAccepted).not.toHaveBeenCalled();
    expect(deps.showError).not.toHaveBeenCalled();
  });

  it('pre-fences a direct proposal entrypoint re-entered during disposal', async () => {
    const runProposal = vi.fn<ProjectApiPort['runProposal']>().mockResolvedValue({ run_id: 81 });
    const jobs = createJobStore('test-project', projectPort({ runProposal }));
    const launch = deferred<RunActionLaunchResult>();
    let signal: AbortSignal | undefined;
    vi.spyOn(api, 'runAction').mockImplementation(((_req, options) => {
      signal = options?.signal;
      return launch.promise;
    }) as typeof api.runAction);
    const deps = { ...noopDeps(), onLaunchAccepted: vi.fn() };
    const proposalDeps = { refreshSheets: vi.fn(), showError: vi.fn() };
    let proposalResult: Promise<boolean> | null = null;
    jobs.start({ ...deps, ...proposalDeps });

    jobs.startRun(request, { id: 'sheet-1', rowCount: 5 } as never);
    signal!.addEventListener('abort', () => {
      proposalResult = jobs.startProposal(proposal);
    }, { once: true });

    jobs.dispose();

    expect(runProposal).not.toHaveBeenCalled();
    await expect(proposalResult).resolves.toBe(false);
    expect(proposalDeps.refreshSheets).not.toHaveBeenCalled();
    expect(proposalDeps.showError).not.toHaveBeenCalled();
  });

  it('pre-fences a confirmed proposal entrypoint re-entered during disposal', async () => {
    const runProposal = vi.fn<ProjectApiPort['runProposal']>()
      .mockRejectedValueOnce(new ConfirmationRequiredError(
        { cost: 1, rows: 2, promise_set_hash: 'proposal-generation-hash' },
        'Confirm proposal generation.',
      ))
      .mockResolvedValueOnce({ run_id: 82 });
    const jobs = createJobStore('test-project', projectPort({ runProposal }));
    const proposalDeps = { refreshSheets: vi.fn(), showError: vi.fn() };
    jobs.start({ ...noopDeps(), ...proposalDeps });

    await expect(jobs.startProposal(proposal)).resolves.toBe(false);
    expect(runProposal).toHaveBeenCalledTimes(1);
    expect(jobs.store.get().costGate).not.toBeNull();

    const launch = deferred<RunActionLaunchResult>();
    let signal: AbortSignal | undefined;
    vi.spyOn(api, 'runAction').mockImplementation(((_req, options) => {
      signal = options?.signal;
      return launch.promise;
    }) as typeof api.runAction);
    jobs.startRun(request, { id: 'sheet-1', rowCount: 5 } as never);
    signal!.addEventListener('abort', () => jobs.confirmCostGate(), { once: true });

    jobs.dispose();

    expect(runProposal).toHaveBeenCalledTimes(1);
    expect(jobs.store.get().costGate).toBeNull();
    expect(proposalDeps.refreshSheets).not.toHaveBeenCalled();
    expect(proposalDeps.showError).not.toHaveBeenCalled();
  });

  it('does not publish a run cost gate when the prior gate cancel disposes its launch', async () => {
    const jobs = createJobStore('test-project', projectPort());
    const priorCancel = vi.fn(() => jobs.dispose());
    jobs.store.set((state) => ({
      ...state,
      costGate: {
        estimate: { cost: 1, rows: 1 },
        message: 'Prior gate',
        onConfirm: vi.fn(),
        onCancel: priorCancel,
      },
    }));
    vi.spyOn(api, 'runAction').mockRejectedValue(new ConfirmationRequiredError(
      { cost: 2, rows: 2 },
      'Replacement gate',
    ));
    jobs.start(noopDeps());

    jobs.startRun(request, { id: 'sheet-1', rowCount: 5 } as never);
    await Promise.resolve();
    await Promise.resolve();

    expect(priorCancel).toHaveBeenCalledTimes(1);
    expect(jobs.store.get().costGate).toBeNull();
  });

  it('does not publish a proposal cost gate when the prior gate cancel disposes its launch', async () => {
    const runProposal = vi.fn<ProjectApiPort['runProposal']>().mockRejectedValue(
      new ConfirmationRequiredError({ cost: 2, rows: 2 }, 'Replacement proposal gate'),
    );
    const jobs = createJobStore('test-project', projectPort({ runProposal }));
    const priorCancel = vi.fn(() => jobs.dispose());
    jobs.store.set((state) => ({
      ...state,
      costGate: {
        estimate: { cost: 1, rows: 1 },
        message: 'Prior proposal gate',
        onConfirm: vi.fn(),
        onCancel: priorCancel,
      },
    }));
    jobs.start(noopDeps());

    await expect(jobs.startProposal(proposal)).resolves.toBe(false);

    expect(priorCancel).toHaveBeenCalledTimes(1);
    expect(jobs.store.get().costGate).toBeNull();
  });

  it('keeps a nested consent request and resolves the displaced outer request false', async () => {
    const jobs = createJobStore('test-project', projectPort());
    let nestedConsent: Promise<boolean> | undefined;
    jobs.store.set((state) => ({
      ...state,
      costGate: {
        estimate: { cost: 1, rows: 1 },
        message: 'Prior gate',
        onConfirm: vi.fn(),
        onCancel: () => {
          nestedConsent = jobs.requestCostConfirmation(
            { cost: 3, rows: 3 },
            'Nested gate C',
          );
        },
      },
    }));

    const outerConsent = jobs.requestCostConfirmation(
      { cost: 2, rows: 2 },
      'Outer gate B',
    );

    expect(jobs.store.get().costGate?.message).toBe('Nested gate C');
    await expect(outerConsent).resolves.toBe(false);
    jobs.confirmCostGate();
    await expect(nestedConsent).resolves.toBe(true);
  });

  it('does not let a run gate overwrite consent published by prior cancellation', async () => {
    const jobs = createJobStore('test-project', projectPort());
    let nestedConsent: Promise<boolean> | undefined;
    jobs.store.set((state) => ({
      ...state,
      costGate: {
        estimate: { cost: 1, rows: 1 },
        message: 'Prior run gate',
        onConfirm: vi.fn(),
        onCancel: () => {
          nestedConsent = jobs.requestCostConfirmation(
            { cost: 3, rows: 3 },
            'Nested run consent C',
          );
        },
      },
    }));
    vi.spyOn(api, 'runAction').mockRejectedValue(new ConfirmationRequiredError(
      { cost: 2, rows: 2 },
      'Outer run gate B',
    ));
    jobs.start(noopDeps());

    jobs.startRun(request, { id: 'sheet-1', rowCount: 5 } as never);
    await flushMicrotasks();

    expect(jobs.store.get().costGate?.message).toBe('Nested run consent C');
    jobs.confirmCostGate();
    await expect(nestedConsent).resolves.toBe(true);
  });

  it('does not let a proposal gate overwrite consent published by prior cancellation', async () => {
    const runProposal = vi.fn<ProjectApiPort['runProposal']>().mockRejectedValue(
      new ConfirmationRequiredError({ cost: 2, rows: 2 }, 'Outer proposal gate B'),
    );
    const jobs = createJobStore('test-project', projectPort({ runProposal }));
    let nestedConsent: Promise<boolean> | undefined;
    jobs.store.set((state) => ({
      ...state,
      costGate: {
        estimate: { cost: 1, rows: 1 },
        message: 'Prior proposal gate',
        onConfirm: vi.fn(),
        onCancel: () => {
          nestedConsent = jobs.requestCostConfirmation(
            { cost: 3, rows: 3 },
            'Nested proposal consent C',
          );
        },
      },
    }));
    jobs.start(noopDeps());

    await expect(jobs.startProposal(proposal)).resolves.toBe(false);

    expect(jobs.store.get().costGate?.message).toBe('Nested proposal consent C');
    jobs.confirmCostGate();
    await expect(nestedConsent).resolves.toBe(true);
  });

  it('does not confirm or strand re-entrant consent while disposal cancels a gate', async () => {
    const jobs = createJobStore('test-project', projectPort());
    const confirm = vi.fn();
    let reenteredConsent: Promise<boolean> | undefined;
    jobs.store.set((state) => ({
      ...state,
      costGate: {
        estimate: { cost: 1, rows: 1 },
        message: 'Dispose gate',
        onConfirm: confirm,
        onCancel: () => {
          jobs.confirmCostGate();
          reenteredConsent = jobs.requestCostConfirmation(
            { cost: 2, rows: 2 },
            'Must not survive disposal',
          );
        },
      },
    }));
    jobs.start(noopDeps());

    jobs.dispose();

    expect(confirm).not.toHaveBeenCalled();
    await expect(reenteredConsent).resolves.toBe(false);
    expect(jobs.store.get().costGate).toBeNull();
  });

  it('does not publish a collision when the prior collision cancel disposes its launch', async () => {
    const jobs = createJobStore('test-project', projectPort());
    const priorCancel = vi.fn(() => jobs.dispose());
    jobs.store.set((state) => ({
      ...state,
      outputColumnCollision: {
        columns: ['prior'],
        message: 'Prior collision',
        onConfirm: vi.fn(),
        onCancel: priorCancel,
      },
    }));
    vi.spyOn(api, 'runAction').mockRejectedValue(new ApiError(
      409,
      'Replacement collision',
      'output_column_exists',
      { columns: ['result'] },
    ));
    jobs.start(noopDeps());

    jobs.startRun(request, { id: 'sheet-1', rowCount: 5 } as never);
    await Promise.resolve();
    await Promise.resolve();

    expect(priorCancel).toHaveBeenCalledTimes(1);
    expect(jobs.store.get().outputColumnCollision).toBeNull();
  });

  it('clears a prior collision before its cancel callback re-enters the public command', async () => {
    const jobs = createJobStore('test-project', projectPort());
    const priorCancel = vi.fn(() => {
      jobs.cancelOutputColumnCollision();
      jobs.store.set((state) => ({
        ...state,
        outputColumnCollision: {
          columns: ['nested'],
          message: 'Nested collision C',
          onConfirm: vi.fn(),
          onCancel: vi.fn(),
        },
      }));
    });
    jobs.store.set((state) => ({
      ...state,
      outputColumnCollision: {
        columns: ['prior'],
        message: 'Prior collision',
        onConfirm: vi.fn(),
        onCancel: priorCancel,
      },
    }));
    vi.spyOn(api, 'runAction').mockRejectedValue(new ApiError(
      409,
      'Replacement collision',
      'output_column_exists',
      { columns: ['replacement'] },
    ));
    jobs.start(noopDeps());

    jobs.startRun(request, { id: 'sheet-1', rowCount: 5 } as never);
    await flushMicrotasks();

    expect(priorCancel).toHaveBeenCalledTimes(1);
    expect(jobs.store.get().outputColumnCollision).toMatchObject({
      columns: ['nested'],
      message: 'Nested collision C',
    });
  });

  it('clears a collision before its cancel callback disposes and restarts the resource', () => {
    const jobs = createJobStore('test-project', projectPort());
    const deps = noopDeps();
    const cancel = vi.fn(() => {
      jobs.dispose();
      jobs.start(deps);
    });
    jobs.start(deps);
    jobs.store.set((state) => ({
      ...state,
      actionJobs: {
        error: 'must be discarded',
        jobs: [actionJob()],
        loading: true,
      },
      outputColumnCollision: {
        columns: ['result'],
        message: 'Collision',
        onConfirm: vi.fn(),
        onCancel: cancel,
      },
    }));

    jobs.cancelOutputColumnCollision();

    expect(cancel).toHaveBeenCalledTimes(1);
    expect(jobs.store.get().outputColumnCollision).toBeNull();
    expect(jobs.store.get().actionJobs).toEqual({ error: null, jobs: [], loading: false });
  });

  it('skips a cost-gate confirm callback when clearing the gate disposes the store', async () => {
    const jobs = createJobStore('test-project', projectPort());
    const runAction = vi.spyOn(api, 'runAction')
      .mockRejectedValueOnce(new ConfirmationRequiredError(
        { cost: 2, rows: 2 },
        'Confirm replacement.',
      ))
      .mockResolvedValueOnce({ runId: 'must-not-launch' });
    jobs.start(noopDeps());
    jobs.startRun(request, { id: 'sheet-1', rowCount: 5 } as never);
    await Promise.resolve();
    await Promise.resolve();
    expect(jobs.store.get().costGate).not.toBeNull();

    let disposed = false;
    const unsubscribe = jobs.store.subscribe(() => {
      if (!disposed && jobs.store.get().costGate === null) {
        disposed = true;
        jobs.dispose();
      }
    });
    jobs.confirmCostGate();
    unsubscribe();

    expect(disposed).toBe(true);
    expect(runAction).toHaveBeenCalledTimes(1);
  });

  it('resolves a promise consent false when its confirm clear synchronously disposes', async () => {
    const jobs = createJobStore('test-project', projectPort());
    jobs.start(noopDeps());
    const resolution = vi.fn();
    void jobs.requestCostConfirmation({ cost: 1, rows: 1 }, 'confirm?').then(resolution);

    let disposed = false;
    const unsubscribe = jobs.store.subscribe(() => {
      if (!disposed && jobs.store.get().costGate === null) {
        disposed = true;
        jobs.dispose();
      }
    });
    jobs.confirmCostGate();
    unsubscribe();
    await flushMicrotasks();

    expect(disposed).toBe(true);
    expect(resolution).toHaveBeenCalledOnce();
    expect(resolution).toHaveBeenCalledWith(false);
  });

  it('preserves a successor consent installed by the confirm clear publication', async () => {
    const jobs = createJobStore('test-project', projectPort());
    const outer = jobs.requestCostConfirmation({ cost: 1, rows: 1 }, 'outer');
    let successor: Promise<boolean> | null = null;
    let installed = false;
    const unsubscribe = jobs.store.subscribe(() => {
      if (!installed && jobs.store.get().costGate === null) {
        installed = true;
        successor = jobs.requestCostConfirmation({ cost: 2, rows: 2 }, 'successor');
      }
    });

    jobs.confirmCostGate();
    await expect(outer).resolves.toBe(false);
    expect(jobs.store.get().costGate?.message).toBe('successor');

    unsubscribe();
    jobs.confirmCostGate();
    await expect(successor).resolves.toBe(true);
  });

  it('skips a collision confirm callback when clearing it disposes the store', async () => {
    const jobs = createJobStore('test-project', projectPort());
    const runAction = vi.spyOn(api, 'runAction')
      .mockRejectedValueOnce(new ApiError(
        409,
        'Collision',
        'output_column_exists',
        { columns: ['result'] },
      ))
      .mockResolvedValueOnce({ runId: 'must-not-launch' });
    jobs.start(noopDeps());
    jobs.startRun(request, { id: 'sheet-1', rowCount: 5 } as never);
    await Promise.resolve();
    await Promise.resolve();
    expect(jobs.store.get().outputColumnCollision).not.toBeNull();

    let disposed = false;
    const unsubscribe = jobs.store.subscribe(() => {
      if (!disposed && jobs.store.get().outputColumnCollision === null) {
        disposed = true;
        jobs.dispose();
      }
    });
    jobs.confirmOutputColumnCollision();
    unsubscribe();

    expect(disposed).toBe(true);
    expect(runAction).toHaveBeenCalledTimes(1);
  });

  it('preserves a successor collision installed by the confirm clear publication', () => {
    const jobs = createJobStore('test-project', projectPort());
    const outerConfirm = vi.fn();
    const successorConfirm = vi.fn();
    const successor = {
      columns: ['successor'],
      message: 'successor',
      onConfirm: successorConfirm,
      onCancel: vi.fn(),
    };
    jobs.store.set((state) => ({
      ...state,
      outputColumnCollision: {
        columns: ['outer'],
        message: 'outer',
        onConfirm: outerConfirm,
        onCancel: vi.fn(),
      },
    }));
    let installed = false;
    const unsubscribe = jobs.store.subscribe(() => {
      if (!installed && jobs.store.get().outputColumnCollision === null) {
        installed = true;
        jobs.store.set((state) => ({ ...state, outputColumnCollision: successor }));
      }
    });

    jobs.confirmOutputColumnCollision();

    expect(outerConfirm).not.toHaveBeenCalled();
    expect(jobs.store.get().outputColumnCollision).toBe(successor);
    unsubscribe();
    jobs.confirmOutputColumnCollision();
    expect(successorConfirm).toHaveBeenCalledOnce();
  });

  it('does not run a stale cost-gate confirmation after disposal', async () => {
    const jobs = createJobStore('test-project', projectPort());
    const runAction = vi.spyOn(api, 'runAction')
      .mockRejectedValueOnce(new ConfirmationRequiredError(
        { cost: 2, rows: 2 },
        'Confirm stale gate.',
      ))
      .mockResolvedValueOnce({ runId: 'must-not-launch' });
    jobs.start(noopDeps());
    jobs.startRun(request, { id: 'sheet-1', rowCount: 5 } as never);
    await Promise.resolve();
    await Promise.resolve();
    expect(jobs.store.get().costGate).not.toBeNull();

    jobs.dispose();
    jobs.confirmCostGate();

    expect(runAction).toHaveBeenCalledTimes(1);
    expect(jobs.store.get().costGate).toBeNull();
  });

  it('does not run a stale proposal confirmation after disposal', async () => {
    const runProposal = vi.fn<ProjectApiPort['runProposal']>()
      .mockRejectedValueOnce(new ConfirmationRequiredError(
        { cost: 2, rows: 2 },
        'Confirm stale proposal.',
      ))
      .mockResolvedValueOnce({ run_id: 83 });
    const jobs = createJobStore('test-project', projectPort({ runProposal }));
    jobs.start(noopDeps());
    await expect(jobs.startProposal(proposal)).resolves.toBe(false);
    expect(jobs.store.get().costGate).not.toBeNull();

    jobs.dispose();
    jobs.confirmCostGate();

    expect(runProposal).toHaveBeenCalledTimes(1);
    expect(jobs.store.get().costGate).toBeNull();
  });

  it('does not run a stale collision confirmation after disposal', async () => {
    const jobs = createJobStore('test-project', projectPort());
    const runAction = vi.spyOn(api, 'runAction')
      .mockRejectedValueOnce(new ApiError(
        409,
        'Stale collision',
        'output_column_exists',
        { columns: ['result'] },
      ))
      .mockResolvedValueOnce({ runId: 'must-not-launch' });
    jobs.start(noopDeps());
    jobs.startRun(request, { id: 'sheet-1', rowCount: 5 } as never);
    await Promise.resolve();
    await Promise.resolve();
    expect(jobs.store.get().outputColumnCollision).not.toBeNull();

    jobs.dispose();
    jobs.confirmOutputColumnCollision();

    expect(runAction).toHaveBeenCalledTimes(1);
    expect(jobs.store.get().outputColumnCollision).toBeNull();
  });

  it('dispose aborts a typed proposal dispatch and fences its completion', async () => {
    const jobs = createJobStore('job-store-retired-proposal', projectPort());
    const runAction = vi.spyOn(api, 'runAction').mockResolvedValue({ runId: 'proposal-run' });
    const deps = {
      refreshSheets: vi.fn(),
      showError: vi.fn(),
    };
    const proposal = {
      kind: 'map',
      title: 'Retired proposal',
      spec: {
        action_id: 'map.template',
        scope: { kind: 'sheet_rows', sheet_id: 1 },
        output_names: { rendered: 'result' },
        params: { template: { text: '{{source}}' } },
      },
    } as CopilotProposal;

    jobs.start({ ...noopDeps(), ...deps });
    const launched = jobs.startProposal(proposal);
    jobs.dispose();

    await expect(launched).resolves.toBe(false);
    expect(runAction).toHaveBeenCalledOnce();
    expect(runAction.mock.calls[0]?.[1]?.signal?.aborted).toBe(true);
    expect(deps.refreshSheets).not.toHaveBeenCalled();
    expect(deps.showError).not.toHaveBeenCalled();
  });
});

describe('createJobStore — startRun / cost gate (parity with the pre-migration hook)', () => {
  it('opens a synchronously materialized sheet after its inventory refresh', async () => {
    const jobs = createJobStore('test-project', projectPort());
    const refresh = deferred<void>();
    const deps = {
      ...noopDeps(),
      refreshSheets: vi.fn(() => refresh.promise),
      onMaterializedSheetCreated: vi.fn(),
    };
    vi.spyOn(api, 'runAction').mockResolvedValueOnce({
      runId: null,
      status: 'completed',
      outputSheetId: '42',
    });
    vi.spyOn(api, 'listActionJobs').mockResolvedValue({ jobs: [] } as never);
    jobs.start(deps);

    jobs.startRun({
      action_id: 'derive.join',
      scope: { kind: 'sheet_rows', sheet_id: 11, row_ids: [101, 108] },
      params: { right: { sheet_id: 22 }, join_keys: [{ left_column: 'id', right_column: 'id' }] },
      sheet_name: 'Joined',
      output_names: {},
      idempotency_key: 'materialized-join',
    }, {
      id: '11', rowCount: 5,
    } as never);
    await flushMicrotasks();
    expect(deps.onMaterializedSheetCreated).not.toHaveBeenCalled();

    refresh.resolve();
    await flushMicrotasks();
    expect(deps.onMaterializedSheetCreated).toHaveBeenCalledWith('42');
  });

  it('startRun sets an optimistic run, then owns the run lane for a runId result', async () => {
    const jobs = createJobStore('test-project', projectPort());
    const deps = noopDeps();
    const launch: RunActionLaunchResult = { runId: 'run-a' };
    vi.spyOn(api, 'runAction').mockResolvedValueOnce(launch);
    vi.spyOn(api, 'listActionJobs').mockResolvedValue({ jobs: [] } as never);
    jobs.start(deps);

    jobs.startRun(
      runRequest(),
      { id: 'sheet-1', rowCount: 5 } as never,
    );

    expect(jobs.store.get().run?.status).toBe('queued');
    expect(jobs.store.get().run?.totalRows).toBe(5);

    // Flush the microtask queue for runAction's .then chain.
    await Promise.resolve();
    await Promise.resolve();
    expect(jobs.store.get().run?.runId).toBe('run-a');
  });

  it('a direct runless temporal launch polls its action job and receipt, not a run id', async () => {
    const jobs = createJobStore('test-project', projectPort());
    const deps = { ...noopDeps(), onLaunchAccepted: vi.fn() };
    vi.spyOn(api, 'runAction').mockResolvedValueOnce({
      runId: null,
      jobId: 917,
      receiptId: 'receipt-temporal-segments',
      status: 'queued',
    });
    vi.spyOn(api, 'listActionJobs').mockResolvedValue({ jobs: [] } as never);
    const getActionJob = vi.spyOn(api, 'getActionJob').mockResolvedValue(
      actionJob({ jobId: 917, status: 'running' }),
    );
    vi.spyOn(api, 'getReceipt').mockResolvedValue({ status: 'running' } as never);
    jobs.start(deps);

    jobs.startRun(
      {
        action_id: 'derive.transcript_segments',
        scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [101] },
        params: { source: 'transcript', selection: { kind: 'column', column: 'topics' } },
        sheet_name: 'Transcript segments',
        output_names: { transcript: 'Excerpt' },
        idempotency_key: 'queued-transcript-segments',
      },
      { id: '7', rowCount: 5 } as never,
    );

    await Promise.resolve();
    await Promise.resolve();
    expect(jobs.store.get().run).toBeNull();
    expect(getActionJob).toHaveBeenCalledWith(917, {
      projectId: 'test-project', signal: expect.any(AbortSignal),
    });
    expect(deps.onLaunchAccepted).toHaveBeenCalledTimes(1);
  });

  // THE cost gate wire, end to end, since the panel stopped keeping a second
  // opinion: launch is always attempted unconfirmed, and the modal exists only
  // because a 402 came back. Cut any link here — the ConfirmationRequiredError
  // branch, the envelope passed to costGate, the confirmed retry — and this
  // fails. Before the migration a panel-side threshold could open the modal
  // without any of it.
  it('a 402 opens the gate on the server envelope and retries with its confirmation hash', async () => {
    const jobs = createJobStore('test-project', projectPort());
    const deps = noopDeps();
    const req = runRequest();
    const runAction = vi.spyOn(api, 'runAction')
      .mockRejectedValueOnce(new ConfirmationRequiredError(
        { cost: 4.2, rows: 250, promise_set_hash: 'ps-1' },
        'estimated cost $4.20 exceeds the $1.00 gate — confirm to run it anyway.',
        'model_cost',
      ))
      .mockResolvedValueOnce({ runId: 'run-a' } as RunActionLaunchResult);
    jobs.start(deps);

    jobs.startRun(req, { id: 'sheet-1', rowCount: 250 } as never);
    await Promise.resolve();
    await Promise.resolve();

    // The launch went out UNCONFIRMED — the panel never pre-confirms.
    expect(runAction.mock.calls[0][0]).not.toHaveProperty('confirmation');
    // Nothing about the dialog is the client's: the estimate and the sentence
    // are the ones the server sent.
    expect(jobs.store.get().costGate?.estimate).toEqual({
      cost: 4.2,
      rows: 250,
      promise_set_hash: 'ps-1',
    });
    expect(jobs.store.get().costGate?.message).toContain('confirm to run it anyway');
    expect(jobs.store.get().run).toBeNull();

    jobs.confirmCostGate();
    await Promise.resolve();
    await Promise.resolve();
    expect(runAction).toHaveBeenCalledTimes(2);
    // R3a claims gate: the retry echoes the exact hash the 402 carried.
    expect(runAction.mock.calls[1][0]).toEqual({ ...req, confirmation: 'ps-1' });
    const initialSignal = runAction.mock.calls[0][1]?.signal;
    const confirmedSignal = runAction.mock.calls[1][1]?.signal;
    expect(initialSignal).toBeDefined();
    expect(confirmedSignal).toBeDefined();
    expect(initialSignal).not.toBe(confirmedSignal);
    expect(confirmedSignal?.aborted).toBe(false);
    expect(jobs.store.get().costGate).toBeNull();
  });

  it('retries a registered request with only the exact server confirmation hash', async () => {
    const req: RegisteredActionRequest = {
      action_id: 'map.template',
      scope: { kind: 'sheet_rows', sheet_id: 7 },
      params: { template: { text: '{{name}}' } },
      output_names: { rendered: 'label' },
      idempotency_key: 'web-map.template:submission-one',
    };
    const runAction = vi.spyOn(api, 'runAction')
      .mockRejectedValueOnce(new ConfirmationRequiredError(
        { cost: 1, rows: 2, promise_set_hash: 'exact-promise-set' },
        'Confirm this action.',
      ))
      .mockResolvedValueOnce({ runId: 'run-registered' });
    const jobs = createJobStore('test-project', projectPort());
    jobs.start(noopDeps());

    jobs.startRun(req, { id: '7', rowCount: 2 } as never);
    await flushMicrotasks();
    jobs.confirmCostGate();
    await flushMicrotasks();

    expect(runAction.mock.calls[0][0]).toEqual(req);
    expect(runAction.mock.calls[1][0]).toEqual({
      ...req,
      confirmation: 'exact-promise-set',
    });
  });

  it('refuses to fabricate registered-action authority when a 402 omits its hash', async () => {
    const req: RegisteredActionRequest = {
      action_id: 'map.template',
      scope: { kind: 'sheet_rows', sheet_id: 7 },
      params: { template: { text: '{{name}}' } },
      output_names: { rendered: 'label' },
      idempotency_key: 'web-map.template:submission-two',
    };
    const runAction = vi.spyOn(api, 'runAction').mockRejectedValueOnce(
      new ConfirmationRequiredError({ cost: 1, rows: 2 }, 'Malformed gate.'),
    );
    const deps = noopDeps();
    const jobs = createJobStore('test-project', projectPort());
    jobs.start(deps);

    jobs.startRun(req, { id: '7', rowCount: 2 } as never);
    await flushMicrotasks();
    jobs.confirmCostGate();

    expect(runAction).toHaveBeenCalledTimes(1);
    expect(deps.showError).toHaveBeenCalledWith(
      'The server did not provide the confirmation hash for this action.',
    );
  });

  it('a stale-hash re-402 reopens the gate with the new server envelope', async () => {
    const jobs = createJobStore('test-project', projectPort());
    const deps = noopDeps();
    const req = runRequest();
    const runAction = vi.spyOn(api, 'runAction')
      .mockRejectedValueOnce(new ConfirmationRequiredError(
        { cost: 4.2, rows: 250, promise_set_hash: 'ps-1' },
        'Confirm the first quote.',
        'model_cost',
      ))
      // The route, price, or row scope changed while the first modal was
      // open. The server correctly refuses the stale echo and returns the
      // claims/quote the user must actually approve.
      .mockRejectedValueOnce(new ConfirmationRequiredError(
        { cost: 8.4, rows: 500, promise_set_hash: 'ps-2' },
        'The quote changed; confirm the current scope.',
        'model_cost',
      ))
      .mockResolvedValueOnce({ runId: 'run-a' } as RunActionLaunchResult);
    jobs.start(deps);

    jobs.startRun(req, { id: 'sheet-1', rowCount: 250 } as never);
    await Promise.resolve();
    await Promise.resolve();
    expect(jobs.store.get().costGate?.estimate.promise_set_hash).toBe('ps-1');

    jobs.confirmCostGate();
    await Promise.resolve();
    await Promise.resolve();

    expect(runAction.mock.calls[1][0]).toEqual({ ...req, confirmation: 'ps-1' });
    expect(jobs.store.get().costGate?.estimate).toEqual({
      cost: 8.4,
      rows: 500,
      promise_set_hash: 'ps-2',
    });
    expect(jobs.store.get().costGate?.message).toBe(
      'The quote changed; confirm the current scope.',
    );
    expect(deps.showError).not.toHaveBeenCalled();

    jobs.confirmCostGate();
    await Promise.resolve();
    await Promise.resolve();
    expect(runAction.mock.calls[2][0]).toEqual({ ...req, confirmation: 'ps-2' });
    expect(jobs.store.get().costGate).toBeNull();
  });

  it('Copilot proposal confirm echoes the 402 hash and a stale echo refreshes the modal', async () => {
    const deps = noopDeps();
    const proposal: CopilotProposal = {
      kind: 'map',
      title: 'Classify reporting risk',
      spec: {
        action_id: 'map.classify',
        scope: { kind: 'sheet_rows', sheet_id: 1 },
        output_names: { risk: 'risk' },
        params: {
          source: ['story'],
          engine: 'llm',
          fields: [{ name: 'risk', type: 'category', labels: ['low', 'high'] }],
          context: 'Classify the reporting risk.',
          model: 'anthropic/claude-haiku-4-5',
        },
      },
    };
    vi.spyOn(api, 'listActionCatalog').mockResolvedValue({
      schema_version: 'frisket.action_catalog.v2',
      actions: [{
        kind: 'map.classify',
        authoring_contract_version: 1,
        title: 'Classify rows',
        description: 'Classify rows with a model.',
        input_schema: {
          type: 'object',
          additionalProperties: false,
          properties: {
            confirmed: { type: 'boolean', default: false },
            consented_promise_set_hash: { default: null },
            sheet_id: { type: 'integer' },
            input_columns: { type: 'array', items: { type: 'string' }, default: [] },
            input_template: { default: null },
            model: { type: 'string' },
            context: { type: 'string', default: '' },
            fields: { type: 'array', items: { type: 'object' } },
            include_justification: { type: 'boolean', default: false },
            include_confidence: { type: 'boolean', default: false },
            row_ids: { default: null },
          },
        },
        output_schema: {},
        errors: [],
        side_effects: [],
        required_capabilities: ['project:write', 'model:complete'],
        required_credentials: [],
        cost_policy: { kind: 'model_metered' },
        idempotency: { supported: true },
        retry_policy: { supported: true },
        execution_mode: 'per_row',
        async_mode: 'sync',
        writes_project: true,
        examples: [],
        ui_hints: {
          form: 'map.classify',
          form_params: [],
          primary_fields: ['sheet_id', 'input_columns', 'fields', 'model'],
        },
        receipt_policy: 'writes_receipt',
      }],
      action_schema: {},
      error_schema: {},
      result_schema: {},
      receipt_schema: {},
      validation_result_schema: {},
    } satisfies ActionCatalogPayload);
    const runProposal = vi.fn<ProjectApiPort['runProposal']>()
      .mockRejectedValueOnce(new ConfirmationRequiredError(
        { cost: 2.1, rows: 25, promise_set_hash: 'proposal-ps-1' },
        'Confirm the proposal quote.',
        'model_cost',
      ))
      .mockRejectedValueOnce(new ConfirmationRequiredError(
        { cost: 3.2, rows: 40, promise_set_hash: 'proposal-ps-2' },
        'The proposal scope changed; confirm the current quote.',
        'model_cost',
      ))
      .mockResolvedValueOnce({ run_id: 77 });
    const jobs = createJobStore(
      'job-store-confirmed-proposal',
      projectPort({ runProposal }),
    );
    jobs.start(deps);

    await expect(jobs.startProposal(proposal)).resolves.toBe(false);
    expect(runProposal.mock.calls[0]?.[1]).toBe(false);
    expect(jobs.store.get().costGate?.estimate.promise_set_hash).toBe('proposal-ps-1');

    jobs.confirmCostGate();
    await vi.waitFor(() => {
      expect(jobs.store.get().costGate?.estimate.promise_set_hash).toBe('proposal-ps-2');
    });

    expect(runProposal.mock.calls[1]?.slice(1, 3)).toEqual([true, 'proposal-ps-1']);
    expect(jobs.store.get().costGate?.estimate).toEqual({
      cost: 3.2,
      rows: 40,
      promise_set_hash: 'proposal-ps-2',
    });
    expect(jobs.store.get().costGate?.message).toBe(
      'The proposal scope changed; confirm the current quote.',
    );
    expect(deps.showError).not.toHaveBeenCalled();

    jobs.confirmCostGate();
    await vi.waitFor(() => expect(runProposal).toHaveBeenCalledTimes(3));

    expect(runProposal.mock.calls[2]?.slice(1, 3)).toEqual([true, 'proposal-ps-2']);
    const proposalSignals = runProposal.mock.calls.map((call) => call[3]?.signal);
    expect(proposalSignals.every(Boolean)).toBe(true);
    expect(new Set(proposalSignals).size).toBe(3);
    expect(proposalSignals[2]?.aborted).toBe(false);
    expect(jobs.store.get().costGate).toBeNull();
  });

  it('requestCostConfirmation resolves true on confirmCostGate, false on cancelCostGate', async () => {
    const jobs = createJobStore('test-project', projectPort());
    const p1 = jobs.requestCostConfirmation({ cost: 1, rows: 10 }, 'confirm?');
    expect(jobs.store.get().costGate?.message).toBe('confirm?');
    jobs.confirmCostGate();
    await expect(p1).resolves.toBe(true);
    expect(jobs.store.get().costGate).toBeNull();

    const p2 = jobs.requestCostConfirmation({ cost: 1, rows: 10 }, 'confirm again?');
    jobs.cancelCostGate();
    await expect(p2).resolves.toBe(false);
    expect(jobs.store.get().costGate).toBeNull();
  });
});

describe('createJobStore — canonical extraction composite consent', () => {
  const request: DeriveCompositeRequest = {
    intent: 'derive_from_extraction', itemField: 'items', sheet_name: 'Findings',
    extraction: {
      action_id: 'map.extract', scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [2, 4] },
      params: { source: ['story'], fields: [{ name: 'items', type: 'list' }] },
      output_names: { items: 'findings' }, idempotency_key: 'composite-extraction',
    },
  };

  it.each(['quoted-promise', undefined])('requires the exact quote hash on retry (%s)', async (hash) => {
    const jobs = createJobStore('test-project', projectPort());
    const deps = noopDeps();
    const runAction = vi.spyOn(api, 'runAction')
      .mockRejectedValueOnce(new ConfirmationRequiredError(
        { cost: 4.2, rows: 2, ...(hash ? { promise_set_hash: hash } : {}) },
        'Confirm the extraction.',
        'model_cost',
      ))
      .mockResolvedValueOnce({ runId: '88' } as RunActionLaunchResult);
    jobs.start(deps);
    jobs.startRun(request, { id: '7', rowCount: 2 } as never);
    await Promise.resolve();
    await Promise.resolve();
    expect(jobs.store.get().costGate?.estimate.cost).toBe(4.2);
    jobs.confirmCostGate();
    await Promise.resolve();
    await Promise.resolve();
    if (hash) {
      expect(runAction).toHaveBeenCalledTimes(2);
      expect(runAction.mock.calls[1][0]).toEqual({ ...request, confirmation: hash });
      expect(runAction.mock.calls[1][0]).not.toHaveProperty('confirmed');
    } else {
      expect(runAction).toHaveBeenCalledTimes(1);
      expect(deps.showError).toHaveBeenCalledWith('The server did not provide the confirmation hash for this action.');
    }
    expect(request).not.toHaveProperty('confirmation');
    jobs.dispose();
  });

  it('carries explicit replacement intent without changing the extraction identity', async () => {
    const jobs = createJobStore('test-project', projectPort());
    const deps = noopDeps();
    const runAction = vi.spyOn(api, 'runAction')
      .mockRejectedValueOnce(new ApiError(409, 'collision', 'output_column_exists', { columns: ['findings'] }))
      .mockResolvedValueOnce({ runId: '88' } as RunActionLaunchResult);
    jobs.start(deps);
    jobs.startRun(request, { id: '7', rowCount: 2 } as never);
    await Promise.resolve();
    await Promise.resolve();
    expect(jobs.store.get().outputColumnCollision?.columns).toEqual(['findings']);
    jobs.confirmOutputColumnCollision();
    await Promise.resolve();
    await Promise.resolve();
    expect(runAction.mock.calls[1][0]).toEqual({ ...request, replace_existing: true });
    expect(request).not.toHaveProperty('replace_existing');
    jobs.dispose();
  });
});

describe('createJobStore — startRun / output-column collision replacement', () => {
  const req = runRequest({ actionKind: 'map.extract', targetColumnId: 'officials' });

  it('a 409 output_column_exists ApiError sets outputColumnCollision instead of showError', async () => {
    const jobs = createJobStore('test-project', projectPort());
    const deps = noopDeps();
    vi.spyOn(api, 'runAction').mockRejectedValueOnce(
      new ApiError(409, "'map.extract' outputs would overwrite existing columns", 'output_column_exists', {
        columns: ['officials'],
      }),
    );
    jobs.start(deps);

    jobs.startRun(req, { id: 'sheet-1', rowCount: 5 } as never);
    await Promise.resolve();
    await Promise.resolve();

    expect(deps.showError).not.toHaveBeenCalled();
    expect(jobs.store.get().run).toBeNull();
    expect(jobs.store.get().outputColumnCollision?.columns).toEqual(['officials']);
  });

  it('confirming resubmits the SAME request with overwrite:true', async () => {
    const jobs = createJobStore('test-project', projectPort());
    const deps = noopDeps();
    vi.spyOn(api, 'runAction')
      .mockRejectedValueOnce(
        new ApiError(409, 'collision', 'output_column_exists', { columns: ['officials'] }),
      )
      .mockResolvedValueOnce({ runId: 'run-b' } as RunActionLaunchResult);
    vi.spyOn(api, 'listActionJobs').mockResolvedValue({ jobs: [] } as never);
    jobs.start(deps);

    jobs.startRun(req, { id: 'sheet-1', rowCount: 5 } as never);
    await Promise.resolve();
    await Promise.resolve();
    expect(jobs.store.get().outputColumnCollision).not.toBeNull();

    jobs.confirmOutputColumnCollision();
    expect(jobs.store.get().outputColumnCollision).toBeNull();

    await Promise.resolve();
    await Promise.resolve();
    expect(api.runAction).toHaveBeenLastCalledWith(
      { ...req, replace_existing: true },
      { projectId: 'test-project', signal: expect.any(AbortSignal) },
    );
    expect(jobs.store.get().run?.runId).toBe('run-b');
  });

  it('a rejected overwrite:true retry terminates instead of reopening the modal', async () => {
    const jobs = createJobStore('test-project', projectPort());
    const deps = noopDeps();
    vi.spyOn(api, 'runAction')
      .mockRejectedValueOnce(
        new ApiError(409, 'collision', 'output_column_exists', { columns: ['officials'] }),
      )
      .mockRejectedValueOnce(
        new ApiError(
          409,
          'protected output column',
          'output_column_exists',
          { columns: ['officials'] },
        ),
      );
    jobs.start(deps);

    jobs.startRun(req, { id: 'sheet-1', rowCount: 5 } as never);
    await Promise.resolve();
    await Promise.resolve();
    expect(jobs.store.get().outputColumnCollision).not.toBeNull();

    jobs.confirmOutputColumnCollision();
    await vi.waitFor(() => expect(deps.showError).toHaveBeenCalledTimes(1));

    expect(api.runAction).toHaveBeenCalledTimes(2);
    expect(api.runAction).toHaveBeenLastCalledWith(
      { ...req, replace_existing: true },
      { projectId: 'test-project', signal: expect.any(AbortSignal) },
    );
    expect(jobs.store.get().run).toBeNull();
    expect(jobs.store.get().outputColumnCollision).toBeNull();
    expect(deps.showError).toHaveBeenCalledWith(
      expect.objectContaining({ message: expect.stringMatching(/replacement was rejected/i) }),
    );
  });

  it('cancelling clears the collision state without resubmitting', async () => {
    const jobs = createJobStore('test-project', projectPort());
    const deps = noopDeps();
    vi.spyOn(api, 'runAction').mockRejectedValueOnce(
      new ApiError(409, 'collision', 'output_column_exists', { columns: ['officials'] }),
    );
    jobs.start(deps);

    jobs.startRun(req, { id: 'sheet-1', rowCount: 5 } as never);
    await Promise.resolve();
    await Promise.resolve();

    jobs.cancelOutputColumnCollision();
    expect(jobs.store.get().outputColumnCollision).toBeNull();
    expect(api.runAction).toHaveBeenCalledTimes(1);
  });

  const registeredReq: RegisteredActionRequest = {
    action_id: 'map.template',
    scope: { kind: 'sheet_rows', sheet_id: 7 },
    params: { template: { text: '{{name}}' } },
    output_names: { rendered: 'generated_text' },
    idempotency_key: 'web-map.template:replace-one',
  };

  it('cancels a registered collision without persisting or retrying replacement consent', async () => {
    const jobs = createJobStore('test-project', projectPort());
    const deps = noopDeps();
    vi.spyOn(api, 'runAction').mockRejectedValueOnce(
      new ApiError(409, 'collision', 'output_column_exists', { columns: ['generated_text'] }),
    );
    jobs.start(deps);

    jobs.startRun(registeredReq, { id: '7', rowCount: 5 } as never);
    await flushMicrotasks();
    expect(jobs.store.get().outputColumnCollision?.columns).toEqual(['generated_text']);
    jobs.cancelOutputColumnCollision();

    expect(api.runAction).toHaveBeenCalledTimes(1);
    expect(jobs.store.get().outputColumnCollision).toBeNull();
  });

  it('confirms one registered replacement retry and terminates if the server rejects it', async () => {
    const jobs = createJobStore('test-project', projectPort());
    const deps = noopDeps();
    vi.spyOn(api, 'runAction')
      .mockRejectedValueOnce(
        new ApiError(409, 'collision', 'output_column_exists', { columns: ['generated_text'] }),
      )
      .mockRejectedValueOnce(
        new ApiError(409, 'protected output', 'output_column_exists', {
          columns: ['generated_text'],
        }),
      );
    jobs.start(deps);

    jobs.startRun(registeredReq, { id: '7', rowCount: 5 } as never);
    await flushMicrotasks();
    jobs.confirmOutputColumnCollision();
    await vi.waitFor(() => expect(deps.showError).toHaveBeenCalledTimes(1));

    expect(api.runAction).toHaveBeenLastCalledWith(
      { ...registeredReq, replace_existing: true },
      { projectId: 'test-project', signal: expect.any(AbortSignal) },
    );
    expect(jobs.store.get().outputColumnCollision).toBeNull();
    expect(deps.showError).toHaveBeenCalledWith(
      expect.objectContaining({ message: expect.stringMatching(/replacement was rejected/i) }),
    );
  });
});
