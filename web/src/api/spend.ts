import { httpContract } from './httpContract';
import type { SpendReport } from './types';

interface SpendOptions {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

interface SpendApi {
  getSpend(options?: SpendOptions): Promise<SpendReport>;
}

type ContractErrorFactory = (status: number, payload: unknown) => Error;
export function createSpendApi(errorFactory: ContractErrorFactory): SpendApi {
  return {
    getSpend(options = {}) {
      return httpContract(
        'tenant.spend.get',
        {
          pathParams: {},
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },
  };
}
