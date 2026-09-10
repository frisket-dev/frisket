import { isActiveRunStatus } from '../runStatusModel';
import type { RunProgress } from '../api/open';
import type { WorkbenchPluginRuntimeIndex, WorkbenchPluginRuntimePlugin } from '../api/types';
import type { DockActionJob } from './WorkbenchBottomDock';

// Run-queue derivations shared by the bottom dock (Errors chip + live summary)
// and the workspace region that feeds it. Kept out of WorkbenchBottomDock.tsx so
// that file stays component-only (react-refresh/only-export-components).

// Merge the ordinary and active-dock snapshots by job identity. The dock is
// fresher and wins conflicts; base-only rows remain. Every projection is
// newest-first, including a base-only projection.
export function mergeDockJobs(
  base: DockActionJob[],
  polled: DockActionJob[] | null,
): DockActionJob[] {
  const byId = new Map<number, DockActionJob>();
  for (const job of base) byId.set(job.jobId, job);
  for (const job of polled ?? []) byId.set(job.jobId, job);
  return Array.from(byId.values()).sort((a, b) => {
    const at = a.timing.startedAt ?? a.timing.createdAt ?? '';
    const bt = b.timing.startedAt ?? b.timing.createdAt ?? '';
    return bt.localeCompare(at);
  });
}

export function isErrorJob(job: DockActionJob): boolean {
  return (
    Boolean(job.error) ||
    job.status === 'failed' ||
    job.lease.leaseExpired ||
    (job.progress?.failedRows ?? 0) > 0
  );
}

// A failed/partial DIRECT run never becomes a job-queue row (it lives only in
// the project job resource's `run` state), so the Errors dock — fed exclusively
// by the job queue — would miss it. Synthesize a job-shaped row from the active
// RunProgress so a direct-run failure is honestly surfaced with its message.
function syntheticErrorJobFromRun(run: RunProgress): DockActionJob {
  const numericRunId = Number(run.runId);
  return {
    schemaVersion: '1',
    // Negative sentinel: never collides with a real (positive) job id.
    jobId: -(Number.isFinite(numericRunId) ? Math.abs(numericRunId) : 1),
    projectId: '',
    kind: 'action',
    runId: run.runId,
    receiptId: null,
    status: run.status === 'failed' ? 'failed' : run.status,
    actionKind: run.actionKind,
    actionName: run.actionName,
    attempts: 1,
    maxAttempts: 1,
    lease: { lockedBy: null, lockedAt: null, leaseExpiresAt: null, leaseExpired: false },
    timing: { createdAt: null, startedAt: null, finishedAt: null },
    // Prefer the run-level message; else dockJobErrorMessage derives a
    // "N failed rows" string from the attached progress.
    error:
      run.error?.trim() ||
      (run.status === 'failed' ? `${run.actionName} failed — see run details` : null),
    progress: run,
  };
}

// The Errors dock's row list: every error job in the queue PLUS a failed/partial
// active run that has not surfaced as a job row (deduped by runId). This is the
// symmetric partner of deriveDockRunSummary's "active run absent from job list".
export function deriveDockErrorJobs(
  jobs: DockActionJob[],
  run: RunProgress | null,
): DockActionJob[] {
  const base = jobs.filter(isErrorJob);
  if (run && !jobs.some((job) => job.runId != null && job.runId === run.runId)) {
    const synthetic = syntheticErrorJobFromRun(run);
    if (isErrorJob(synthetic)) return [synthetic, ...base];
  }
  return base;
}

// The Plugins bottom-dock tab retired to a Settings-only manager, so plugin
// load/runtime failures need a new route to stay visible — the Errors dock
// tab. The runtime index already carries honest failure state per plugin
// (installState/activation 'failed', installFailure, disabledReason —
// web/src/api/types.ts's WorkbenchPluginRuntimePlugin); this just surfaces
// what was already fetched and, until now, unrendered anywhere. Health (not
// failure) surfaces separately via Diagnostics (the "N installed, M active,
// K failed" probe) — this function only emits FAILED plugins as error-dock
// rows.
function isFailedRuntimePlugin(plugin: WorkbenchPluginRuntimePlugin): boolean {
  return plugin.installState === 'failed' || plugin.activation === 'failed';
}

function pluginFailureMessage(plugin: WorkbenchPluginRuntimePlugin): string {
  if (plugin.installFailure?.message) return plugin.installFailure.message;
  if (plugin.disabledReason) return plugin.disabledReason;
  if (plugin.activation === 'failed') return `${plugin.pluginId} failed to activate.`;
  return `${plugin.pluginId} failed to install.`;
}

// A deterministic negative job-id SEED derived from the plugin id, offset
// well below syntheticErrorJobFromRun's `-(numericRunId)` range so the two
// synthetic-row families can never collide within the same merged list
// (App.tsx's errorJobs concatenates both). NOT collision-free on its own —
// a 32-bit polynomial hash has ordinary collisions (e.g. 'demo.1n' and
// 'demo.30' both hash to the same bucket; see
// web/tests/unit/dockJobSummary.test.ts's regression case). This is only
// ever called through derivePluginErrorJobs below, which de-duplicates the
// seed against every id already assigned in the same batch — jobId is the
// row's Map key both in WorkbenchListDetailPanel selection/React-key
// identity and in WorkbenchBottomDock.tsx's mergeDockJobs (a
// Map<number, DockActionJob> keyed only by jobId), so an un-de-duplicated
// collision would silently drop one of the two failed plugins from the
// Errors tab.
function pluginErrorJobIdSeed(pluginId: string): number {
  let hash = 0;
  for (let index = 0; index < pluginId.length; index += 1) {
    hash = (Math.imul(hash, 31) + pluginId.charCodeAt(index)) | 0;
  }
  return -1_000_000_000 - Math.abs(hash % 1_000_000_000);
}

function syntheticErrorJobFromPlugin(
  plugin: WorkbenchPluginRuntimePlugin,
  projectId: string,
  jobId: number,
): DockActionJob {
  return {
    schemaVersion: '1',
    jobId,
    projectId,
    kind: 'plugin',
    runId: null,
    receiptId: plugin.receiptId ?? null,
    status: 'failed',
    actionKind: 'plugin',
    actionName: `Plugin: ${plugin.pluginId}`,
    attempts: 1,
    maxAttempts: 1,
    lease: { lockedBy: null, lockedAt: null, leaseExpiresAt: null, leaseExpired: false },
    timing: { createdAt: null, startedAt: null, finishedAt: null },
    error: pluginFailureMessage(plugin),
    resultSummary: {
      pluginId: plugin.pluginId,
      installState: plugin.installState,
      activation: plugin.activation,
      ...(plugin.installFailure?.code ? { failureCode: plugin.installFailure.code } : {}),
    },
  };
}

export function derivePluginErrorJobs(
  runtimeIndex: WorkbenchPluginRuntimeIndex | null,
): DockActionJob[] {
  if (!runtimeIndex) return [];
  // Guarantee collision-free ids within this batch even when two plugin ids
  // hash to the same seed (this is not a tail risk at this scale, it is a
  // reproducible collision — 'demo.1n' vs 'demo.30'): linear-probe to the
  // next lower integer until the seed is free. Deterministic given a stable
  // plugin-list order (the backend's response order), so row identity stays
  // stable across re-renders/polls as long as the failed-plugin set itself
  // doesn't reorder.
  const usedJobIds = new Set<number>();
  const rows: DockActionJob[] = [];
  for (const plugin of runtimeIndex.plugins) {
    if (!isFailedRuntimePlugin(plugin)) continue;
    let jobId = pluginErrorJobIdSeed(plugin.pluginId);
    while (usedJobIds.has(jobId)) jobId -= 1;
    usedJobIds.add(jobId);
    rows.push(syntheticErrorJobFromPlugin(plugin, runtimeIndex.projectId, jobId));
  }
  return rows;
}

// A dock job still doing work: prefer the live run-progress status, else fall
// back to the queue status. Feeds the dock's live run summary alongside the
// active RunProgress the status bar shows (they share the same job store).
function isActiveDockJob(job: DockActionJob): boolean {
  const progressStatus = job.progress?.status;
  if (progressStatus) return isActiveRunStatus(progressStatus);
  return job.status === 'queued' || job.status === 'running';
}

export interface DockRunSummary {
  /** Number of jobs/runs still executing (0 = nothing running). */
  runningCount: number;
  /** True when there is finished run history but nothing running. */
  anyComplete: boolean;
  /** True when something in that finished history did NOT finish cleanly.
   *  The pill read a flat "all complete" over a job the dock's own table
   *  called "complete with errors" — this carries the errored state up so the
   *  summary cannot contradict the rows it summarizes. */
  anyErrored: boolean;
  /** Aggregate 0–100 progress, or null for an indeterminate bar. */
  percent: number | null;
}

// Derive the live summary + tri-state label from the project job resource:
// the active RunProgress (what the status bar shows) plus the queued
// job list, deduped by runId. Never fabricated — a project with no runs reads
// 'idle'. Percent aggregates row totals across EVERY active source of
// progress (each active job's attached RunProgress + the active run when it
// has not yet surfaced in the job list); null when no active source knows its
// totals (indeterminate bar).
export function deriveDockRunSummary(
  jobs: DockActionJob[],
  run: RunProgress | null,
): DockRunSummary {
  const activeJobs = jobs.filter(isActiveDockJob);
  const runActive = run ? isActiveRunStatus(run.status) : false;
  const runInJobList = runActive && activeJobs.some((job) => job.runId === run?.runId);
  const runningCount = activeJobs.length + (runActive && !runInJobList ? 1 : 0);

  let completed = 0;
  let total = 0;
  for (const job of activeJobs) {
    if (job.progress && job.progress.totalRows > 0) {
      completed += job.progress.completedRows;
      total += job.progress.totalRows;
    }
  }
  if (runActive && !runInJobList && run && run.totalRows > 0) {
    completed += run.completedRows;
    total += run.totalRows;
  }

  const anyComplete = runningCount === 0 && (run !== null || jobs.length > 0);
  // isErrorJob is the dock's existing "this row is a problem" predicate (the
  // Errors chip already counts with it), so the pill and the Errors tab agree
  // by construction. The active run contributes too: a direct run that failed
  // never becomes a job row.
  const anyErrored = anyComplete && (
    jobs.some(isErrorJob)
    || (run !== null && (run.status === 'failed' || run.failedRows > 0))
  );
  const percent = runningCount > 0 && total > 0 ? Math.min(100, (100 * completed) / total) : null;
  return { runningCount, anyComplete, anyErrored, percent };
}
