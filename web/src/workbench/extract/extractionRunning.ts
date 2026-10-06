import type { JobState } from '../../state/jobStore';
import { isActiveRunStatus, isRunActionBlockedStatus, isTerminalActionJobStatus } from '../../runStatusModel';

export function isExtractionRunning(state: Pick<JobState, 'run' | 'actionJobs'>): boolean {
  // Queued acceptance clears the optimistic run before refreshing its job list.
  // Loading or a failed refresh means that list cannot establish an idle state.
  if (state.actionJobs.loading || state.actionJobs.error !== null) return true;
  if (state.run?.actionKind === 'media.extract_document' && isRunActionBlockedStatus(state.run.status)) return true;
  return state.actionJobs.jobs.some((job) => job.actionKind === 'media.extract_document'
    && (job.progress ? isActiveRunStatus(job.progress.status) : !isTerminalActionJobStatus(job.status)));
}
