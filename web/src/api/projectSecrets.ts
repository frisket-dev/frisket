import type { HttpContractOperationMap } from '../generated/openHttpContracts';
import { httpContract, type RequestOptions } from './httpContract';
import type { ProjectSecrets } from './types';

type SetRequest = HttpContractOperationMap['tenant.set_project_secret.post']['request'];

export function createProjectSecretsApi(projectId: string) {
  return {
    async getProjectSecrets(options: RequestOptions = {}): Promise<ProjectSecrets> {
      return httpContract(
        'tenant.get_project_secrets.get',
        {
          pathParams: { pid: projectId },
          signal: options.signal,
        },
      );
    },

    async setProjectSecret(
      name: string,
      value: string,
      options: RequestOptions = {},
    ): Promise<ProjectSecrets> {
      const body = { name, value } as SetRequest;
      return httpContract(
        'tenant.set_project_secret.post',
        {
          pathParams: { pid: projectId },
          body,
          signal: options.signal,
        },
      );
    },

    async deleteProjectSecret(
      name: string,
      options: RequestOptions = {},
    ): Promise<{ ok: boolean; deleted: boolean; name: string }> {
      return httpContract(
        'tenant.delete_project_secret.delete',
        {
          pathParams: { pid: projectId, name },
          signal: options.signal,
        },
      );
    },
  };
}
