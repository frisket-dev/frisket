import type {
  HttpContractInvokeOptions,
  HttpContractSuccessResponse,
} from './httpContract';
import { httpContract, type RequestOptions } from './httpContract';

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

export function createProjectSearchApi(projectId: string) {
  return {
    searchProject(
      request: ProjectSearchRequestWire,
      options: RequestOptions = {},
    ): Promise<ProjectSearchResponseWire> {
      return httpContract(
        'tenant.search_ep.post',
        {
          pathParams: { pid: projectId },
          body: request,
          signal: options.signal,
        },
      );
    },
  };
}
