import type {
  HttpContractOperationMap,
  HttpSelectorChoicesQuery,
  HttpSelectorChoicesResponse,
} from '../generated/openHttpContracts';
import { httpContract, type RequestOptions } from './httpContract';

export type { HttpSelectorChoicesQuery, HttpSelectorChoicesResponse } from '../generated/openHttpContracts';

/** Keep raw choices tied to the generated response rather than copying DTOs. */
export type SelectorChoice = HttpSelectorChoicesResponse['groups'][number]['choices'][number];
export type SelectorSubject = HttpSelectorChoicesQuery['subject'];

type SelectorChoicesRequest = HttpContractOperationMap['tenant.selector_choices.post']['request'];

function createSelectorChoicesApi() {
  return {
    getSelectorChoices(
      projectId: string,
      query: HttpSelectorChoicesQuery,
      options: RequestOptions = {},
    ): Promise<HttpSelectorChoicesResponse> {
      return httpContract(
        'tenant.selector_choices.post',
        {
          pathParams: { pid: projectId },
          body: query as SelectorChoicesRequest,
          signal: options.signal,
        },
      );
    },
  };
}

export const selectorChoicesApi = createSelectorChoicesApi();
