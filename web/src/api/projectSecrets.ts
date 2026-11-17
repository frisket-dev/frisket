import type { HttpContractOperationMap } from '../generated/openHttpContracts';
import { httpContract, type RequestOptions } from './httpContract';
import type { ProjectSecrets } from './types';

interface ProjectSecretsApi {
  getProjectSecrets(
    projectId: string,
    options?: RequestOptions,
  ): Promise<ProjectSecrets>;
  setProjectSecret(
    projectId: string,
    name: string,
    value: string,
    options?: RequestOptions,
  ): Promise<ProjectSecrets>;
  deleteProjectSecret(
    projectId: string,
    name: string,
    options?: RequestOptions,
  ): Promise<{ ok: boolean; deleted: boolean; name: string }>;
}

type SetRequest = HttpContractOperationMap['tenant.set_project_secret.post']['request'];

export function createProjectSecretsApi(
): ProjectSecretsApi {
  return {
    getProjectSecrets(projectId, options = {}) {
      return httpContract(
        'tenant.get_project_secrets.get',
        {
          pathParams: { pid: projectId },
          signal: options.signal,
        },
      );
    },

    setProjectSecret(projectId, name, value, options = {}) {
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

    deleteProjectSecret(projectId, name, options = {}) {
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
