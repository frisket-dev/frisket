import { httpContract } from './httpContract';
import type { AdminOverview } from './types';

type ContractErrorFactory = (status: number, payload: unknown) => Error;
export interface AdminOverviewApi {
  getAdminOverview(): Promise<AdminOverview>;
}

export function createAdminOverviewApi(
  errorFactory: ContractErrorFactory,
): AdminOverviewApi {
  return {
    getAdminOverview() {
      return httpContract(
        'outer.admin_overview.get',
        {
          pathParams: {},
          query: {},
          errorFactory,
        },
      );
    },
  };
}
