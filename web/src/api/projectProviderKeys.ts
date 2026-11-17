import type { HttpContractOperationMap } from '../generated/openHttpContracts';
import { httpContract, type ContractErrorFactory, type RequestOptions } from './httpContract';
import type {
  ProjectProviderKeys,
  ProviderValidateResult,
} from './types';

interface ProjectProviderKeysApi {
  getProjectProviderKeys(
    projectId: string,
    options?: RequestOptions,
  ): Promise<ProjectProviderKeys>;
  setProjectProviderKey(
    projectId: string,
    provider: string,
    key: string,
    spendCapUsd?: number | null,
    validationToken?: string | null,
    options?: RequestOptions,
  ): Promise<ProjectProviderKeys>;
  validateProjectProviderKey(
    projectId: string,
    provider: string,
    key?: string,
    options?: RequestOptions,
  ): Promise<ProviderValidateResult>;
  deleteProjectProviderKey(
    projectId: string,
    provider: string,
    options?: RequestOptions,
  ): Promise<{ ok: boolean; deleted: boolean; provider: string }>;
}

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
          signal: options.signal,
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
          body,
          signal: options.signal,
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
          body,
          signal: options.signal,
          errorFactory,
        },
      );
    },

    deleteProjectProviderKey(projectId, provider, options = {}) {
      return httpContract(
        'tenant.delete_project_provider_key.delete',
        {
          pathParams: { pid: projectId, provider },
          signal: options.signal,
          errorFactory,
        },
      );
    },
  };
}
