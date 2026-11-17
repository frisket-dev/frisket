import { httpContract, type RequestOptions } from './httpContract';
import type { SpendReport } from './types';

export function createSpendApi() {
  return {
    getSpend(options: RequestOptions = {}): Promise<SpendReport> {
      return httpContract(
        'tenant.spend.get',
        {
          signal: options.signal,
        },
      );
    },
  };
}
