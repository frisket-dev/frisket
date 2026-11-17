import { httpContract, type RequestOptions } from './httpContract';
import type { AttemptReceiptsPage } from './types';

export function createProjectAttemptsApi(projectId: string) {
  return {
    /** Attempt receipts for this project, newest first. `runId` narrows to one
     *  run; omitting it is the ONLY way to reach a compaction-orphaned attempt,
     *  whose `run_id` is null by design. */
    async listAttemptReceipts(
      runId?: number | string | null,
      limit = 25,
      options: RequestOptions = {},
    ): Promise<AttemptReceiptsPage> {
      return httpContract(
        'tenant.project_attempts.get',
        {
          pathParams: { pid: projectId },
          query: {
            limit,
            run_id: runId != null && runId !== '' ? Number(runId) : undefined,
          },
          signal: options.signal,
        },
      );
    },
  };
}
