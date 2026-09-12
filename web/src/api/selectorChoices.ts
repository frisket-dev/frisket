import type {
  HttpContractOperationMap,
  HttpSelectorChoicesQuery,
  HttpSelectorChoicesResponse,
} from '../generated/openHttpContracts';
import { httpContract } from './httpContract';

/** Keep raw choices tied to the generated response rather than copying DTOs. */
export type SelectorChoice = HttpSelectorChoicesResponse['groups'][number]['choices'][number];
export type SelectorSubject = HttpSelectorChoicesQuery['subject'];

export interface SelectorChoicesOptions {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

export interface SelectorChoicesApi {
  getSelectorChoices(
    projectId: string,
    query: HttpSelectorChoicesQuery,
    options?: SelectorChoicesOptions,
  ): Promise<HttpSelectorChoicesResponse>;
}

type SelectorChoicesRequest = HttpContractOperationMap['tenant.selector_choices.post']['request'];

export function createSelectorChoicesApi(): SelectorChoicesApi {
  return {
    getSelectorChoices(projectId, query, options = {}) {
      return httpContract(
        'tenant.selector_choices.post',
        {
          pathParams: { pid: projectId },
          query: {},
          body: query as SelectorChoicesRequest,
          signal: options.signal,
          headers: options.headers,
        },
      );
    },
  };
}

export const selectorChoicesApi = createSelectorChoicesApi();
