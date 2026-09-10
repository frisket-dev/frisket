import { httpContract } from './httpContract';
import type { AttemptReceiptsPage } from './types';

export interface ProjectAttemptsOptions {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

export interface ProjectAttemptsApi {
  listAttemptReceipts(
    projectId: string,
    runId?: number | string | null,
    limit?: number,
    options?: ProjectAttemptsOptions,
  ): Promise<AttemptReceiptsPage>;
}

type ContractErrorFactory = (status: number, payload: unknown) => Error;
export function createProjectAttemptsApi(
  errorFactory: ContractErrorFactory,
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
          headers: options.headers,
          errorFactory,
        },
      );
    },
  };
}
