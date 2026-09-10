import {
  httpContract,
  type HttpContractSuccessResponse,
} from '../httpContract';
import type {
  WorkbenchPluginInstallFailure,
  WorkbenchPluginRuntimeIndex,
  WorkbenchPluginSettingDescriptor,
} from '../types';

type WorkbenchRuntimeIndexWire =
  HttpContractSuccessResponse<'tenant.workbench_plugins.get'>;
type ContractErrorFactory = (status: number, payload: unknown) => Error;

export interface WorkbenchContractOptions {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function mapWorkbenchSetting(value: unknown): WorkbenchPluginSettingDescriptor | null {
  if (!isRecord(value)) return null;
  if (typeof value.id !== 'string' || typeof value.title !== 'string' || typeof value.type !== 'string') {
    return null;
  }
  return {
    id: value.id,
    title: value.title,
    type: value.type,
    default: value.default,
    enum: Array.isArray(value.enum) ? value.enum : null,
    min: typeof value.min === 'number' ? value.min : null,
    max: typeof value.max === 'number' ? value.max : null,
    description: typeof value.description === 'string' ? value.description : null,
  };
}

function mapInstallFailure(value: unknown): WorkbenchPluginInstallFailure | null {
  if (!isRecord(value) || typeof value.code !== 'string' || typeof value.message !== 'string') {
    return null;
  }
  return {
    ...value,
    code: value.code,
    message: value.message,
    retryable: typeof value.retryable === 'boolean' ? value.retryable : undefined,
    rollbackAction: typeof value.rollbackAction === 'string' ? value.rollbackAction : undefined,
    ref: typeof value.ref === 'string' ? value.ref : undefined,
  };
}

function mapWorkbenchRuntimeIndex(
  wire: WorkbenchRuntimeIndexWire,
): WorkbenchPluginRuntimeIndex {
  return {
    schemaVersion: wire.schemaVersion,
    projectId: wire.projectId,
    arbitraryPackageLoadAllowed: wire.arbitraryPackageLoadAllowed,
    receiptScanLimit: wire.receiptScanLimit,
    skippedInvalidReceipts: wire.skippedInvalidReceipts,
    skippedInvalidManifestRefs: wire.skippedInvalidManifestRefs,
    loadedPluginCount: wire.loadedPluginCount,
    plugins: wire.plugins.map((plugin) => {
      const settings = (plugin.settings ?? [])
        .map(mapWorkbenchSetting)
        .filter((setting): setting is WorkbenchPluginSettingDescriptor => setting !== null);
      return {
        schemaVersion: plugin.schemaVersion,
        pluginId: plugin.pluginId,
        version: plugin.version,
        installState: plugin.installState,
        activation: plugin.activation,
        runtimeSource: plugin.runtimeSource,
        receiptId: plugin.receiptId,
        manifestSha256: plugin.manifestSha256,
        packageSha256: plugin.packageSha256,
        byteCount: plugin.byteCount,
        source: {
          ...plugin.source,
          kind: typeof plugin.source.kind === 'string' ? plugin.source.kind : 'unknown',
        },
        contributionSummary: plugin.contributionSummary,
        frontendComponentBindings: plugin.frontendComponentBindings.map((binding) => ({
          schemaVersion: binding.schemaVersion,
          contributionId: binding.contributionId,
          moduleKey: binding.moduleKey,
          componentKey: binding.componentKey,
          ...(binding.modulePath ? { modulePath: binding.modulePath } : {}),
          ...(binding.moduleUrl ? { moduleUrl: binding.moduleUrl } : {}),
        })),
        requires: plugin.requires,
        arbitraryPackageLoadAllowed: plugin.arbitraryPackageLoadAllowed,
        registryActivated: plugin.registryActivated,
        installStateSchemaVersion: plugin.installStateSchemaVersion,
        disabledReason: plugin.disabledReason,
        installFailure: mapInstallFailure(plugin.installFailure),
        ...(plugin.workbenchDescriptorPackage ? {
          workbenchDescriptorPackage: plugin.workbenchDescriptorPackage,
        } : {}),
        ...(plugin.workbenchDescriptorManifests ? {
          workbenchDescriptorManifests: plugin.workbenchDescriptorManifests,
        } : {}),
        ...(settings.length ? { settings } : {}),
      };
    }),
    firstParty: {
      schemaVersion: wire.firstParty.schemaVersion,
      descriptors: wire.firstParty.descriptors,
    },
  };
}

export function getWorkbenchPluginRuntimeIndexContract(
  projectId: string,
  errorFactory: ContractErrorFactory,
  options: WorkbenchContractOptions = {},
): Promise<WorkbenchPluginRuntimeIndex> {
  return httpContract('tenant.workbench_plugins.get', {
    pathParams: { pid: projectId },
    query: {},
    signal: options.signal,
    headers: options.headers,
    errorFactory,
  }, mapWorkbenchRuntimeIndex);
}
