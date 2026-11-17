import { httpContract, type RequestOptions, type ContractErrorFactory } from './httpContract';
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

interface WorkbenchPluginsApi {
  getSettings(
    projectId: string,
    pluginId: string,
    options?: RequestOptions,
  ): Promise<WorkbenchPluginSettings>;
  patchSettings(
    projectId: string,
    pluginId: string,
    values: Record<string, unknown>,
    options?: RequestOptions,
  ): Promise<WorkbenchPluginSettings>;
  installLocal(
    projectId: string,
    input: WorkbenchPluginLocalInstallRequest,
    options?: RequestOptions,
  ): Promise<WorkbenchPluginLocalInstallExecution>;
  activate(
    projectId: string,
    input: WorkbenchPluginActivationRequest,
    options?: RequestOptions,
  ): Promise<WorkbenchPluginActivation>;
  activateBackend(
    projectId: string,
    input: WorkbenchPluginBackendActivationRequest,
    options?: RequestOptions,
  ): Promise<WorkbenchPluginBackendActivation>;
  disable(
    projectId: string,
    pluginId: string,
    options?: RequestOptions,
  ): Promise<WorkbenchPluginInstallStateChange>;
  uninstall(
    projectId: string,
    pluginId: string,
    options?: RequestOptions,
  ): Promise<WorkbenchPluginInstallStateChange>;
}

export function createWorkbenchPluginsApi(
  errorFactory: ContractErrorFactory,
): WorkbenchPluginsApi {
  return {
    getSettings(projectId, pluginId, options = {}) {
      return httpContract(
        'tenant.workbench_plugin_settings.get',
        {
          pathParams: { pid: projectId, plugin_id: pluginId },
          signal: options.signal,
          errorFactory,
        },
      );
    },

    patchSettings(projectId, pluginId, values, options = {}) {
      return httpContract(
        'tenant.patch_workbench_plugin_settings.patch',
        {
          pathParams: { pid: projectId, plugin_id: pluginId },
          body: { values: values as never },
          signal: options.signal,
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
          body: { source: source as never, arbitraryPackageLoadAllowed },
          signal: options.signal,
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
          body: {
            receiptId,
            trustAcknowledged,
            permissionsAccepted,
            arbitraryPackageLoadAllowed,
          },
          signal: options.signal,
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
          body: {
            trustAcknowledged,
            arbitraryPackageLoadAllowed,
            executableHandlersAllowed,
          },
          signal: options.signal,
          errorFactory,
        },
      );
    },

    disable(projectId, pluginId, options = {}) {
      return httpContract(
        'tenant.disable_workbench_plugin_route.post',
        {
          pathParams: { pid: projectId, plugin_id: pluginId },
          signal: options.signal,
          errorFactory,
        },
      );
    },

    uninstall(projectId, pluginId, options = {}) {
      return httpContract(
        'tenant.uninstall_workbench_plugin_route.post',
        {
          pathParams: { pid: projectId, plugin_id: pluginId },
          signal: options.signal,
          errorFactory,
        },
      );
    },
  };
}
