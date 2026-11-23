import type { HttpContractOperationMap } from '../generated/openHttpContracts';
import { httpContract, type RequestOptions } from './httpContract';
import type {
  ProjectRetentionPolicy,
  ProjectSettings,
  ProjectSettingsPatch,
} from './types';

type RetentionPatch = HttpContractOperationMap['tenant.update_project_retention.patch']['request'];
type SettingsPatch = HttpContractOperationMap['tenant.update_project_settings.patch']['request'];

export function createProjectDataManagementApi(projectId: string) {
  return {
    getProjectRetention(options: RequestOptions = {}): Promise<ProjectRetentionPolicy> {
      return httpContract(
        'tenant.get_project_retention.get',
        {
          pathParams: { pid: projectId },
          signal: options.signal,
        },
      );
    },

    updateProjectRetention(
      input: Partial<ProjectRetentionPolicy>,
      options: RequestOptions = {},
    ): Promise<ProjectRetentionPolicy> {
      return httpContract(
        'tenant.update_project_retention.patch',
        {
          pathParams: { pid: projectId },
          body: input as RetentionPatch,
          signal: options.signal,
        },
      );
    },

    getProjectSettings(options: RequestOptions = {}): Promise<ProjectSettings> {
      return httpContract(
        'tenant.get_project_settings.get',
        {
          pathParams: { pid: projectId },
          signal: options.signal,
        },
      );
    },

    updateProjectSettings(
      input: ProjectSettingsPatch,
      options: RequestOptions = {},
    ): Promise<ProjectSettings> {
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

    compactProject(options: RequestOptions = {}): Promise<Record<string, unknown>> {
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
