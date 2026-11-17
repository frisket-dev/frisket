import { httpContract, type RequestOptions } from './httpContract';
import type { SpendReport } from './types';

interface SpendApi {
  getSpend(options?: RequestOptions): Promise<SpendReport>;
}

export function createSpendApi(): SpendApi {
  return {
    getSpend(options = {}) {
      return httpContract(
        'tenant.spend.get',
        {
          signal: options.signal,
        },
      );
    },
  };
}
