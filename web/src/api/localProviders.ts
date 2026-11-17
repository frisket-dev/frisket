import { observeSetup, trackSetupSubmission } from '../telemetry/setupTelemetry';
import { httpContract, type ContractErrorFactory, type RequestOptions } from "./httpContract";
import type {
  LocalProviderCatalog,
  LocalProviderEntry,
  LocalEndpointDiscoveryResponse,
  ModelPullDto,
  ModelPullStartResult,
  ProviderValidateResult,
} from "./types";

export interface LocalEndpointInput {
  display_name: string;
  origin: string;
  inference_token?: string | null;
  provisioning_token?: string | null;
  edge_auth?: boolean;
  pull_enabled?: boolean;
}

type AtLeastOne<T, Keys extends keyof T = keyof T> = Keys extends keyof T
  ? Required<Pick<T, Keys>> & Partial<Omit<T, Keys>>
  : never;

export type LocalEndpointPatch = AtLeastOne<Omit<LocalEndpointInput, "origin">>;

export function createLocalProvidersApi(errorFactory?: ContractErrorFactory) {
  return {
    listProviders(projectId?: string, options: RequestOptions = {}): Promise<LocalProviderCatalog> {
      return httpContract(
        "tenant.list_providers.get",
        {
          query: { project_id: projectId },
          signal: options.signal,
          errorFactory,
        },
      );
    },

    providerStatus(provider: string, options: RequestOptions = {}): Promise<LocalProviderEntry> {
      return httpContract(
        "tenant.provider_status.get",
        {
          pathParams: { provider },
          signal: options.signal,
          errorFactory,
        },
      );
    },

    setProviderKey(
      provider: string,
      key: string,
      validationToken?: string | null,
      options: RequestOptions = {},
    ): Promise<LocalProviderCatalog> {
      return httpContract(
        "tenant.set_provider_key.put",
        {
          pathParams: { provider },
          body: { key, validation_token: validationToken ?? null },
          signal: options.signal,
          errorFactory,
        },
      );
    },

    deleteProviderKey(
      provider: string,
      options: RequestOptions = {},
    ): Promise<LocalProviderCatalog> {
      return httpContract(
        "tenant.delete_provider_key.delete",
        {
          pathParams: { provider },
          signal: options.signal,
          errorFactory,
        },
      );
    },

    createLocalEndpoint(
      input: LocalEndpointInput,
      options: RequestOptions = {},
    ): Promise<LocalProviderCatalog> {
      return httpContract(
        "tenant.create_local_endpoint.post",
        {
          body: { ...input },
          signal: options.signal,
          errorFactory,
        },
      );
    },

    discoverLocalEndpoints(options: RequestOptions = {}): Promise<LocalEndpointDiscoveryResponse> {
      return httpContract(
        "tenant.discover_local_endpoints.post",
        {
          signal: options.signal,
          errorFactory,
        },
      );
    },

    updateLocalEndpoint(
      endpointId: string,
      patch: LocalEndpointPatch,
      options: RequestOptions = {},
    ): Promise<LocalProviderCatalog> {
      return httpContract(
        "tenant.update_local_endpoint.patch",
        {
          pathParams: { endpoint_id: endpointId },
          body: { ...patch },
          signal: options.signal,
          errorFactory,
        },
      );
    },

    deleteLocalEndpoint(
      endpointId: string,
      options: RequestOptions = {},
    ): Promise<LocalProviderCatalog> {
      return httpContract(
        "tenant.delete_local_endpoint.delete",
        {
          pathParams: { endpoint_id: endpointId },
          signal: options.signal,
          errorFactory,
        },
      );
    },

    listModelPulls(options: RequestOptions = {}): Promise<{ pulls: ModelPullDto[] }> {
      return httpContract(
        "tenant.list_model_pulls.get",
        {
          signal: options.signal,
          errorFactory,
        },
      );
    },

    getModelPull(id: number, options: RequestOptions = {}): Promise<ModelPullDto> {
      return httpContract(
        "tenant.get_model_pull.get",
        {
          pathParams: { pull_id: id },
          signal: options.signal,
          errorFactory,
        },
      ).then((pull) => observeSetup('workspace', pull));
    },

    cancelModelPull(id: number, options: RequestOptions = {}): Promise<ModelPullDto> {
      return httpContract(
        "tenant.cancel_model_pull.post",
        {
          pathParams: { pull_id: id },
          signal: options.signal,
          errorFactory,
        },
      );
    },

    startArtifactPull(
      ref: string,
      unpinnedAcknowledged = false,
      options: RequestOptions = {},
    ): Promise<ModelPullStartResult> {
      return trackSetupSubmission('workspace', 'model', httpContract(
        "tenant.pull_artifact.post",
        {
          body: { ref, unpinned_acknowledged: unpinnedAcknowledged },
          signal: options.signal,
          errorFactory,
        },
      ));
    },

    uninstallArtifact(ref: string, options: RequestOptions = {}): Promise<ModelPullDto> {
      return httpContract(
        "tenant.uninstall_artifact.post",
        {
          body: { ref },
          signal: options.signal,
          errorFactory,
        },
      );
    },

    validateProviderKey(
      provider: string,
      key?: string,
      options: RequestOptions = {},
    ): Promise<ProviderValidateResult> {
      return httpContract(
        "tenant.validate_provider_key.post",
        {
          pathParams: { provider },
          body: key ? { key } : {},
          signal: options.signal,
          errorFactory,
        },
      );
    },
  };
}
