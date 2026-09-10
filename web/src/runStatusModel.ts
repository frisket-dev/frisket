import type { RunProgress } from './api/types';

export type RunProgressStatus = RunProgress['status'];

// ONE rule for "it finished, but not cleanly". The backend's own `partial`
// status is flattened to 'complete' by toRunProgressStatus below, so
// `failedRows > 0` on a complete run is the only signal left that anything
// went wrong — and three surfaces used to each decide for themselves whether
// to consult it. Only the dock's job table did. The bottom-dock pill said
// "all complete" and the status bar said "complete · 4 rows · 3 failed",
// asserting success and contradicting it in the same line, for the same job
// the detail pane called "complete with errors".
//
// Every surface that renders a terminal run status goes through here.
export function runCompletedWithErrors(
  status: RunProgressStatus | string | null | undefined,
  failedRows: number | null | undefined,
): boolean {
  return status === 'complete' && (failedRows ?? 0) > 0;
}

/** Terminal status as a person should read it: 'complete with errors' where a
 *  complete run left failed rows behind, otherwise the status itself. */
export function runStatusLabel(
  status: RunProgressStatus | string | null | undefined,
  failedRows: number | null | undefined,
): string {
  return runCompletedWithErrors(status, failedRows) ? 'complete with errors' : String(status);
}

const TERMINAL_ACTION_JOB_STATUSES = ['done', 'complete', 'completed', 'failed', 'cancelled'];

export function isTerminalActionJobStatus(status: string | null | undefined): boolean {
  return TERMINAL_ACTION_JOB_STATUSES.includes(status ?? '');
}

export type ActiveRunStatus = Extract<
  RunProgressStatus,
  'queued' | 'running' | 'stalled' | 'orphaned'
>;

export interface WireRunStatusLike {
  status?: string | null;
  live?: boolean | null;
}

const ACTIVE_RUN_STATUSES = [
  'queued',
  'running',
  'stalled',
  'orphaned',
] as const satisfies readonly ActiveRunStatus[];

const RUN_ACTION_BLOCKING_STATUSES = [
  'queued',
  'running',
] as const satisfies readonly RunProgressStatus[];

export function isActiveRunStatus(
  status: RunProgressStatus | string | null | undefined,
): status is ActiveRunStatus {
  return (ACTIVE_RUN_STATUSES as readonly string[]).includes(String(status));
}

export function isRunActionBlockedStatus(
  status: RunProgressStatus | string | null | undefined,
): boolean {
  return (RUN_ACTION_BLOCKING_STATUSES as readonly string[]).includes(String(status));
}

export function isStreamingRunStatus(
  status: RunProgressStatus | string | null | undefined,
): boolean {
  return status === 'running';
}

export function toRunProgressStatus(
  status: WireRunStatusLike | string | null | undefined,
): RunProgressStatus {
  const raw = typeof status === 'string' ? status : status?.status;
  switch (raw) {
    case 'queued':
    case 'running':
    case 'stalled':
    case 'orphaned':
    case 'failed':
    case 'cancelled':
    case 'complete':
      return raw;
    case 'completed':
    case 'done':
    case 'partial':
      return 'complete';
    default:
      return typeof status === 'object' && status?.live ? 'running' : 'complete';
  }
}
