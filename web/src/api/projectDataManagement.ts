import type { HttpContractOperationMap } from '../generated/openHttpContracts';
import { httpContract, type RequestOptions } from './httpContract';
import type {
  ProjectNetworkPolicy,
  ProjectRetentionPolicy,
  ProjectSettings,
  ProjectSettingsPatch,
} from './types';

interface ProjectDataManagementApi {
  getProjectRetention(options?: RequestOptions): Promise<ProjectRetentionPolicy>;
  updateProjectRetention(
    input: Partial<ProjectRetentionPolicy>,
    options?: RequestOptions,
  ): Promise<ProjectRetentionPolicy>;
  getProjectNetworkPolicy(options?: RequestOptions): Promise<ProjectNetworkPolicy>;
  updateProjectNetworkPolicy(
    input: { mode: string },
    options?: RequestOptions,
  ): Promise<ProjectNetworkPolicy>;
  getProjectSettings(options?: RequestOptions): Promise<ProjectSettings>;
  updateProjectSettings(
    input: ProjectSettingsPatch,
    options?: RequestOptions,
  ): Promise<ProjectSettings>;
  compactProject(options?: RequestOptions): Promise<Record<string, unknown>>;
}

type RetentionPatch = HttpContractOperationMap['tenant.update_project_retention.patch']['request'];
type SettingsPatch = HttpContractOperationMap['tenant.update_project_settings.patch']['request'];

export function createProjectDataManagementApi(
  projectId: string,
): ProjectDataManagementApi {
  return {
    getProjectRetention(options = {}) {
      return httpContract(
        'tenant.get_project_retention.get',
        {
          pathParams: { pid: projectId },
          signal: options.signal,
        },
      );
    },

    updateProjectRetention(input, options = {}) {
      return httpContract(
        'tenant.update_project_retention.patch',
        {
          pathParams: { pid: projectId },
          body: input as RetentionPatch,
          signal: options.signal,
        },
      );
    },

    getProjectNetworkPolicy(options = {}) {
      return httpContract(
        'tenant.get_project_network.get',
        {
          pathParams: { pid: projectId },
          signal: options.signal,
        },
      );
    },

    updateProjectNetworkPolicy(input, options = {}) {
      return httpContract(
        'outer.update_project_network.patch',
        {
          pathParams: { pid: projectId },
          body: { mode: input.mode },
          signal: options.signal,
        },
      );
    },

    getProjectSettings(options = {}) {
      return httpContract(
        'tenant.get_project_settings.get',
        {
          pathParams: { pid: projectId },
          signal: options.signal,
        },
      );
    },

    updateProjectSettings(input, options = {}) {
      return httpContract(
        'tenant.update_project_settings.patch',
        {
          pathParams: { pid: projectId },
          // The browser surface historically accepts a runtime Partial, which
          // can carry extension/read-only keys. Preserve it byte-for-byte;
          // httpContract removes only undefined JSON properties.
          body: input as SettingsPatch,
          signal: options.signal,
        },
      );
    },

    compactProject(options = {}) {
      return httpContract(
        'tenant.compact_project.post',
        {
          pathParams: { pid: projectId },
          signal: options.signal,
        },
      );
    },
  };
}
