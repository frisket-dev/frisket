import type { HttpContractSuccessResponse } from './httpContract';
import { httpContract } from './httpContract';

export interface ProjectSearchOptions {
  limit?: number;
  mode?: string;
  rerank?: boolean | string;
  signal?: AbortSignal;
  headers?: HeadersInit;
}

export type ProjectSearchHitWire =
  HttpContractSuccessResponse<'tenant.search_ep.get'>[number];

export interface ProjectSearchApi {
  searchProject(
    q: string,
    options?: ProjectSearchOptions | number,
  ): Promise<ProjectSearchHitWire[]>;
}

type ContractErrorFactory = (status: number, payload: unknown) => Error;

export function createProjectSearchApi(
  errorFactory: ContractErrorFactory,
  projectId: string,
): ProjectSearchApi {
  return {
    searchProject(q, options = {}) {
      const rawOptions = typeof options === 'number' ? { limit: options } : options;
      const rerank = rawOptions.rerank;
      return httpContract(
        'tenant.search_ep.get',
        {
          pathParams: { pid: projectId },
          query: {
            q,
            limit: rawOptions.limit ?? 50,
            mode: rawOptions.mode ?? 'keyword',
            rerank: rerank === true ? 'on' : rerank === false || rerank === undefined ? 'off' : rerank,
          },
          signal: rawOptions.signal,
          headers: rawOptions.headers,
          errorFactory,
        },
      );
    },
  };
}
