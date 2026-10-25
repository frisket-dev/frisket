import type { RunProgress } from './api/types';

export type RunProgressStatus = RunProgress['status'];

// ONE compatibility rule for legacy servers that reported a terminal run as
// `complete` even when it left failed rows behind. New servers publish
// `partial` directly and toRunProgressStatus preserves it. Three surfaces used
// to each decide for themselves whether to consult failedRows. Only the dock's
// job table did. The bottom-dock pill said
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

/** Terminal status as a person should read it. Preserve the server's public
 *  outcome; only repair the legacy complete-plus-failures combination. */
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
    case 'partial':
      return raw;
    case 'completed':
    case 'done':
      return 'complete';
    default:
      return typeof status === 'object' && status?.live ? 'running' : 'complete';
  }
}
