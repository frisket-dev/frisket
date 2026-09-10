import type { HttpContractOperationMap } from '../generated/openHttpContracts';
import { httpContract } from './httpContract';
import type {
  ProjectNetworkPolicy,
  ProjectRetentionPolicy,
  ProjectSettings,
} from './types';

export interface ProjectDataManagementOptions {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

export interface ProjectDataManagementApi {
  getProjectRetention(options?: ProjectDataManagementOptions): Promise<ProjectRetentionPolicy>;
  updateProjectRetention(
    input: Partial<ProjectRetentionPolicy>,
    options?: ProjectDataManagementOptions,
  ): Promise<ProjectRetentionPolicy>;
  getProjectNetworkPolicy(options?: ProjectDataManagementOptions): Promise<ProjectNetworkPolicy>;
  updateProjectNetworkPolicy(
    input: { mode: string },
    options?: ProjectDataManagementOptions,
  ): Promise<ProjectNetworkPolicy>;
  getProjectSettings(options?: ProjectDataManagementOptions): Promise<ProjectSettings>;
  updateProjectSettings(
    input: Partial<ProjectSettings>,
    options?: ProjectDataManagementOptions,
  ): Promise<ProjectSettings>;
  compactProject(options?: ProjectDataManagementOptions): Promise<Record<string, unknown>>;
}

type ContractErrorFactory = (status: number, payload: unknown) => Error;
type RetentionPatch = HttpContractOperationMap['tenant.update_project_retention.patch']['request'];
type SettingsPatch = HttpContractOperationMap['tenant.update_project_settings.patch']['request'];

export function createProjectDataManagementApi(
  errorFactory: ContractErrorFactory,
  projectId: string,
): ProjectDataManagementApi {
  return {
    getProjectRetention(options = {}) {
      return httpContract(
        'tenant.get_project_retention.get',
        {
          pathParams: { pid: projectId },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    updateProjectRetention(input, options = {}) {
      return httpContract(
        'tenant.update_project_retention.patch',
        {
          pathParams: { pid: projectId },
          query: {},
          body: input as RetentionPatch,
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    getProjectNetworkPolicy(options = {}) {
      return httpContract(
        'tenant.get_project_network.get',
        {
          pathParams: { pid: projectId },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    updateProjectNetworkPolicy(input, options = {}) {
      return httpContract(
        'outer.update_project_network.patch',
        {
          pathParams: { pid: projectId },
          query: {},
          body: { mode: input.mode },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    getProjectSettings(options = {}) {
      return httpContract(
        'tenant.get_project_settings.get',
        {
          pathParams: { pid: projectId },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    updateProjectSettings(input, options = {}) {
      return httpContract(
        'tenant.update_project_settings.patch',
        {
          pathParams: { pid: projectId },
          query: {},
          // The browser surface historically accepts a runtime Partial, which
          // can carry extension/read-only keys. Preserve it byte-for-byte;
          // httpContract removes only undefined JSON properties.
          body: input as SettingsPatch,
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    compactProject(options = {}) {
      return httpContract(
        'tenant.compact_project.post',
        {
          pathParams: { pid: projectId },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },
  };
}
