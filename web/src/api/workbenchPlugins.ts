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

export function createWorkbenchPluginsApi(
  errorFactory: ContractErrorFactory,
  projectId: string,
) {
  return {
    async getWorkbenchPluginSettings(
      pluginId: string,
      options: RequestOptions = {},
    ): Promise<WorkbenchPluginSettings> {
      return httpContract(
        'tenant.workbench_plugin_settings.get',
        {
          pathParams: { pid: projectId, plugin_id: pluginId },
          signal: options.signal,
          errorFactory,
        },
      );
    },

    async patchWorkbenchPluginSettings(
      pluginId: string,
      values: Record<string, unknown>,
      options: RequestOptions = {},
    ): Promise<WorkbenchPluginSettings> {
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

    /** Execute a trusted-local install plan through the backend plugin.load lifecycle. */
    async installLocalWorkbenchPlugin(
      input: WorkbenchPluginLocalInstallRequest,
      options: RequestOptions = {},
    ): Promise<WorkbenchPluginLocalInstallExecution> {
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

    /** Trust-gated workbench activation of manifest metadata from a plugin.load receipt. */
    async activateWorkbenchPlugin(
      input: WorkbenchPluginActivationRequest,
      options: RequestOptions = {},
    ): Promise<WorkbenchPluginActivation> {
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

    async activateWorkbenchPluginBackend(
      input: WorkbenchPluginBackendActivationRequest,
      options: RequestOptions = {},
    ): Promise<WorkbenchPluginBackendActivation> {
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

    async disableWorkbenchPlugin(
      pluginId: string,
      options: RequestOptions = {},
    ): Promise<WorkbenchPluginInstallStateChange> {
      return httpContract(
        'tenant.disable_workbench_plugin_route.post',
        {
          pathParams: { pid: projectId, plugin_id: pluginId },
          signal: options.signal,
          errorFactory,
        },
      );
    },

    async uninstallWorkbenchPlugin(
      pluginId: string,
      options: RequestOptions = {},
    ): Promise<WorkbenchPluginInstallStateChange> {
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
