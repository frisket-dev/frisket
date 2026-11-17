import { httpContract } from './httpContract';
import type { AdminOverview } from './types';

export function createAdminOverviewApi() {
  return {
    getAdminOverview(): Promise<AdminOverview> {
      return httpContract(
        'outer.admin_overview.get',
        {
        },
      );
    },
  };
}
