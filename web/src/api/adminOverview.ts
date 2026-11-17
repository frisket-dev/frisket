import { httpContract } from './httpContract';
import type { AdminOverview } from './types';

interface AdminOverviewApi {
  getAdminOverview(): Promise<AdminOverview>;
}

export function createAdminOverviewApi(
): AdminOverviewApi {
  return {
    getAdminOverview() {
      return httpContract(
        'outer.admin_overview.get',
        {
        },
      );
    },
  };
}
