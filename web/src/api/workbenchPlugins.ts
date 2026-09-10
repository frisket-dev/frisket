import { httpContract } from './httpContract';
import type {
  WorkbenchPluginActivation,
  WorkbenchPluginActivationRequest,
  WorkbenchPluginBackendActivation,
  WorkbenchPluginBackendActivationRequest,
  WorkbenchPluginInstallStateChange,
  WorkbenchPluginLocalInstallExecution,
  WorkbenchPluginLocalInstallRequest,
  WorkbenchPluginSettings,
} from './types';

export interface WorkbenchPluginsOptions {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

export interface WorkbenchPluginsApi {
  getSettings(
    projectId: string,
    pluginId: string,
    options?: WorkbenchPluginsOptions,
  ): Promise<WorkbenchPluginSettings>;
  patchSettings(
    projectId: string,
    pluginId: string,
    values: Record<string, unknown>,
    options?: WorkbenchPluginsOptions,
  ): Promise<WorkbenchPluginSettings>;
  installLocal(
    projectId: string,
    input: WorkbenchPluginLocalInstallRequest,
    options?: WorkbenchPluginsOptions,
  ): Promise<WorkbenchPluginLocalInstallExecution>;
  activate(
    projectId: string,
    input: WorkbenchPluginActivationRequest,
    options?: WorkbenchPluginsOptions,
  ): Promise<WorkbenchPluginActivation>;
  activateBackend(
    projectId: string,
    input: WorkbenchPluginBackendActivationRequest,
    options?: WorkbenchPluginsOptions,
  ): Promise<WorkbenchPluginBackendActivation>;
  disable(
    projectId: string,
    pluginId: string,
    options?: WorkbenchPluginsOptions,
  ): Promise<WorkbenchPluginInstallStateChange>;
  uninstall(
    projectId: string,
    pluginId: string,
    options?: WorkbenchPluginsOptions,
  ): Promise<WorkbenchPluginInstallStateChange>;
}

type ContractErrorFactory = (status: number, payload: unknown) => Error;
export function createWorkbenchPluginsApi(
  errorFactory: ContractErrorFactory,
): WorkbenchPluginsApi {
  return {
    getSettings(projectId, pluginId, options = {}) {
      return httpContract(
        'tenant.workbench_plugin_settings.get',
        {
          pathParams: { pid: projectId, plugin_id: pluginId },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    patchSettings(projectId, pluginId, values, options = {}) {
      return httpContract(
        'tenant.patch_workbench_plugin_settings.patch',
        {
          pathParams: { pid: projectId, plugin_id: pluginId },
          query: {},
          body: { values: values as never },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    installLocal(projectId, input, options = {}) {
      const { pluginId, source, arbitraryPackageLoadAllowed } = input;
      return httpContract(
        'tenant.workbench_plugin_local_install_route.post',
        {
          pathParams: { pid: projectId, plugin_id: pluginId },
          query: {},
          body: { source: source as never, arbitraryPackageLoadAllowed },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    activate(projectId, input, options = {}) {
      const {
        pluginId,
        receiptId,
        trustAcknowledged,
        permissionsAccepted,
        arbitraryPackageLoadAllowed,
      } = input;
      return httpContract(
        'tenant.activate_workbench_plugin.post',
        {
          pathParams: { pid: projectId, plugin_id: pluginId },
          query: {},
          body: {
            receiptId,
            trustAcknowledged,
            permissionsAccepted,
            arbitraryPackageLoadAllowed,
          },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    activateBackend(projectId, input, options = {}) {
      const {
        pluginId,
        trustAcknowledged,
        arbitraryPackageLoadAllowed,
        executableHandlersAllowed,
      } = input;
      return httpContract(
        'tenant.activate_workbench_plugin_backend.post',
        {
          pathParams: { pid: projectId, plugin_id: pluginId },
          query: {},
          body: {
            trustAcknowledged,
            arbitraryPackageLoadAllowed,
            executableHandlersAllowed,
          },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    disable(projectId, pluginId, options = {}) {
      return httpContract(
        'tenant.disable_workbench_plugin_route.post',
        {
          pathParams: { pid: projectId, plugin_id: pluginId },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    uninstall(projectId, pluginId, options = {}) {
      return httpContract(
        'tenant.uninstall_workbench_plugin_route.post',
        {
          pathParams: { pid: projectId, plugin_id: pluginId },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },
  };
}
