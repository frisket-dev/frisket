// Thin React composer for the project-owned job resource. The store owns all
// scheduling and state; this hook only keeps its call-time collaborators fresh
// and binds the current sheet to launch commands.

import { useCallback, useEffect, useMemo } from 'react';
import { useJobsHandle } from './useJobsHandle';
import type {
  CopilotProposal,
  ActionExecutionRequest,
  SheetMeta,
} from '../api/open';
import type { JobRunDeps } from '../state/jobStore';

interface UseRunControllerArgs extends JobRunDeps {
  sheet: SheetMeta | null | undefined;
}

export interface RunController {
  startProposal(proposal: CopilotProposal, confirmed?: boolean): Promise<boolean>;
  startRun(req: ActionExecutionRequest): void;
}

export function useRunController({
  invalidateProjectData,
  refreshHistory,
  refreshReviewCount,
  refreshSheets,
  sheet,
  showError,
  onLaunchAccepted,
  onMaterializedSheetCreated,
}: UseRunControllerArgs): RunController {
  const jobs = useJobsHandle();
  const deps: JobRunDeps = useMemo(
    () => ({
      invalidateProjectData,
      refreshHistory,
      refreshReviewCount,
      refreshSheets,
      showError,
      onLaunchAccepted,
      onMaterializedSheetCreated,
    }),
    [
      invalidateProjectData,
      refreshHistory,
      refreshReviewCount,
      refreshSheets,
      showError,
      onLaunchAccepted,
      onMaterializedSheetCreated,
    ],
  );

  useEffect(() => {
    jobs.start(deps);
  }, [deps, jobs]);

  const startRun = useCallback(
    (req: ActionExecutionRequest) => jobs.startRun(req, sheet),
    [jobs, sheet],
  );
  const startProposal = useCallback(
    (proposal: CopilotProposal, confirmed = false) => jobs.startProposal(proposal, confirmed),
    [jobs],
  );

  return { startProposal, startRun };
}
