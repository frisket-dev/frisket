import type { HttpContractOperationMap } from '../generated/openHttpContracts';
import { httpContract } from './httpContract';
import type {
  ProjectProviderKeys,
  ProviderValidateResult,
} from './types';

export interface ProjectProviderKeysOptions {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

export interface ProjectProviderKeysApi {
  getProjectProviderKeys(
    projectId: string,
    options?: ProjectProviderKeysOptions,
  ): Promise<ProjectProviderKeys>;
  setProjectProviderKey(
    projectId: string,
    provider: string,
    key: string,
    spendCapUsd?: number | null,
    validationToken?: string | null,
    options?: ProjectProviderKeysOptions,
  ): Promise<ProjectProviderKeys>;
  validateProjectProviderKey(
    projectId: string,
    provider: string,
    key?: string,
    options?: ProjectProviderKeysOptions,
  ): Promise<ProviderValidateResult>;
  deleteProjectProviderKey(
    projectId: string,
    provider: string,
    options?: ProjectProviderKeysOptions,
  ): Promise<{ ok: boolean; deleted: boolean; provider: string }>;
}

type ContractErrorFactory = (status: number, payload: unknown) => Error;
type SetRequest = HttpContractOperationMap['tenant.set_project_provider_key.post']['request'];
type ValidateRequest = HttpContractOperationMap['tenant.validate_project_provider_key.post']['request'];

export function createProjectProviderKeysApi(
  errorFactory?: ContractErrorFactory,
): ProjectProviderKeysApi {
  return {
    getProjectProviderKeys(projectId, options = {}) {
      return httpContract(
        'tenant.get_project_provider_keys.get',
        {
          pathParams: { pid: projectId },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    setProjectProviderKey(projectId, provider, key, spendCapUsd, validationToken, options = {}) {
      const body = {
        provider,
        key,
        ...(spendCapUsd != null ? { spend_cap_usd: spendCapUsd } : {}),
        ...(validationToken != null ? { validation_token: validationToken } : {}),
      } as SetRequest;
      return httpContract(
        'tenant.set_project_provider_key.post',
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

    validateProjectProviderKey(projectId, provider, key, options = {}) {
      const body = {
        provider,
        ...(key ? { key } : {}),
      } as ValidateRequest;
      return httpContract(
        'tenant.validate_project_provider_key.post',
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

    deleteProjectProviderKey(projectId, provider, options = {}) {
      return httpContract(
        'tenant.delete_project_provider_key.delete',
        {
          pathParams: { pid: projectId, provider },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },
  };
}
