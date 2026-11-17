import { httpContract, type RequestOptions } from './httpContract';
import type { AttemptReceiptsPage } from './types';

interface ProjectAttemptsApi {
  listAttemptReceipts(
    projectId: string,
    runId?: number | string | null,
    limit?: number,
    options?: RequestOptions,
  ): Promise<AttemptReceiptsPage>;
}

export function createProjectAttemptsApi(
): ProjectAttemptsApi {
  return {
    listAttemptReceipts(projectId, runId, limit = 25, options = {}) {
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
