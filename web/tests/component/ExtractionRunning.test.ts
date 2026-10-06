// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { createProjectApi } from '../../src/api/real';
import type { ActionJob, RunActionLaunchResult } from '../../src/api/types';
import { createJobStore, type JobStoreHandle } from '../../src/state/jobStore';
import { isExtractionRunning } from '../../src/workbench/extract/extractionRunning';

const api = createProjectApi('extraction-acceptance');
let jobs: JobStoreHandle | null = null;
const job = (status: string, actionKind = 'media.extract_document'): ActionJob => ({
  schemaVersion: '1', projectId: 'extraction-acceptance', jobId: 1, kind: 'action', runId: null, receiptId: null,
  status, actionKind, actionName: 'Extract document', attempts: 1, maxAttempts: 3,
  lease: { lockedBy: null, lockedAt: null, leaseExpiresAt: null, leaseExpired: false },
  timing: { createdAt: '2026-10-06T00:00:00Z', startedAt: null, finishedAt: null }, error: null,
});
const page = (items: ActionJob[]) => ({ schemaVersion: '1', projectId: 'extraction-acceptance', jobs: items });
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
async function settle() { for (let index = 0; index < 8; index++) await Promise.resolve(); }
afterEach(() => { jobs?.dispose(); jobs = null; vi.useRealTimers(); vi.restoreAllMocks(); });

async function start() {
  vi.useFakeTimers();
  const launch = deferred<RunActionLaunchResult>();
  const listing = deferred<ReturnType<typeof page>>();
  const progress = deferred<ActionJob>();
  const list = vi.spyOn(api, 'listActionJobs').mockResolvedValueOnce(page([])).mockReturnValueOnce(listing.promise);
  vi.spyOn(api, 'runAction').mockReturnValueOnce(launch.promise);
  vi.spyOn(api, 'getActionJob').mockReturnValueOnce(progress.promise);
  jobs = createJobStore('extraction-acceptance', api);
  jobs.start({ invalidateProjectData: vi.fn(), refreshHistory: vi.fn(), refreshReviewCount: vi.fn(), refreshSheets: vi.fn(), showError: vi.fn() });
  await vi.advanceTimersByTimeAsync(0);
  expect(isExtractionRunning(jobs.store.get())).toBe(false);
  jobs.startRun({ action_id: 'media.extract_document', scope: { kind: 'sheet_rows', sheet_id: 1 },
    params: { source: 'Document' }, output_names: {}, sheet_name: 'Layout 1 results', idempotency_key: 'extract-acceptance' },
  { id: '1', name: 'Documents', rowCount: 3, columns: [] });
  return { launch, listing, progress, list, handle: jobs };
}

it('keeps extraction busy through optimistic launch, accepted loading, the queued job, and terminal refresh', async () => {
  const { launch, listing, progress, list, handle } = await start();
  expect(handle.store.get().run?.actionKind).toBe('media.extract_document');
  expect(isExtractionRunning(handle.store.get())).toBe(true);
  launch.resolve({ runId: null, jobId: 1, receiptId: null, status: 'queued' });
  await settle();
  expect(handle.store.get().run).toBeNull();
  expect(handle.store.get().actionJobs.loading).toBe(true);
  expect(isExtractionRunning(handle.store.get())).toBe(true);
  listing.resolve(page([job('running')]));
  await settle();
  expect(handle.store.get().actionJobs.loading).toBe(false);
  expect(isExtractionRunning(handle.store.get())).toBe(true);
  list.mockResolvedValue(page([job('done')]));
  progress.resolve(job('done'));
  await settle();
  expect(handle.store.get().actionJobs.loading).toBe(false);
  expect(isExtractionRunning(handle.store.get())).toBe(false);
});

it('keeps a failed accepted-job listing busy until the ordinary queued poll refreshes it', async () => {
  const { launch, listing, progress, list, handle } = await start();
  launch.resolve({ runId: null, jobId: 1, receiptId: null, status: 'queued' });
  await settle();
  listing.reject(new Error('Job list temporarily unavailable'));
  await settle();
  expect(handle.store.get().run).toBeNull();
  expect(handle.store.get().actionJobs.loading).toBe(false);
  expect(handle.store.get().actionJobs.error).toBe('Job list temporarily unavailable');
  expect(isExtractionRunning(handle.store.get())).toBe(true);
  list.mockResolvedValue(page([job('done')]));
  progress.resolve(job('done'));
  await settle();
  expect(handle.store.get().actionJobs.error).toBeNull();
  expect(isExtractionRunning(handle.store.get())).toBe(false);
});

it('does not block extraction for an unrelated active job or a finished extraction job', () => {
  const state = { run: null, actionJobs: { loading: false, error: null, jobs: [job('running', 'map.classify'), job('failed')] } };
  expect(isExtractionRunning(state)).toBe(false);
});
