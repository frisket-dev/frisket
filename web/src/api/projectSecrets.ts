import type { HttpContractOperationMap } from '../generated/openHttpContracts';
import { httpContract } from './httpContract';
import type { ProjectSecrets } from './types';

export interface ProjectSecretsOptions {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

export interface ProjectSecretsApi {
  getProjectSecrets(
    projectId: string,
    options?: ProjectSecretsOptions,
  ): Promise<ProjectSecrets>;
  setProjectSecret(
    projectId: string,
    name: string,
    value: string,
    options?: ProjectSecretsOptions,
  ): Promise<ProjectSecrets>;
  deleteProjectSecret(
    projectId: string,
    name: string,
    options?: ProjectSecretsOptions,
  ): Promise<{ ok: boolean; deleted: boolean; name: string }>;
}

type ContractErrorFactory = (status: number, payload: unknown) => Error;
type SetRequest = HttpContractOperationMap['tenant.set_project_secret.post']['request'];

export function createProjectSecretsApi(
  errorFactory?: ContractErrorFactory,
): ProjectSecretsApi {
  return {
    getProjectSecrets(projectId, options = {}) {
      return httpContract(
        'tenant.get_project_secrets.get',
        {
          pathParams: { pid: projectId },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    setProjectSecret(projectId, name, value, options = {}) {
      const body = { name, value } as SetRequest;
      return httpContract(
        'tenant.set_project_secret.post',
        {
          pathParams: { pid: projectId },
          query: {},
          body,
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    deleteProjectSecret(projectId, name, options = {}) {
      return httpContract(
        'tenant.delete_project_secret.delete',
        {
          pathParams: { pid: projectId, name },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },
  };
}
