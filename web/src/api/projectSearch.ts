import type {
  HttpContractInvokeOptions,
  HttpContractSuccessResponse,
} from './httpContract';
import { httpContract } from './httpContract';

export type ProjectSearchRequestWire = NonNullable<
  HttpContractInvokeOptions<'tenant.search_ep.post'>['body']
>;
export type ProjectSearchResponseWire =
  HttpContractSuccessResponse<'tenant.search_ep.post'>;
export type ProjectSearchGroupWire = ProjectSearchResponseWire['groups'][number];
export type ProjectSearchHitWire = ProjectSearchGroupWire['primary'];
type ProjectSearchQueryWire = ProjectSearchRequestWire['query'];
type ProjectSearchSimilarQueryWire = Extract<
  ProjectSearchQueryWire,
  { kind: 'similar' }
>;
export type ProjectSearchAnchorWire = ProjectSearchSimilarQueryWire['anchor'];

export interface ProjectSearchOptions {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

export interface ProjectSearchApi {
  searchProject(
    request: ProjectSearchRequestWire,
    options?: ProjectSearchOptions,
  ): Promise<ProjectSearchResponseWire>;
}

type ContractErrorFactory = (status: number, payload: unknown) => Error;

export function createProjectSearchApi(
  errorFactory: ContractErrorFactory,
  projectId: string,
): ProjectSearchApi {
  return {
    searchProject(request, options = {}) {
      return httpContract(
        'tenant.search_ep.post',
        {
          pathParams: { pid: projectId },
          query: {},
          body: request,
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },
  };
}
