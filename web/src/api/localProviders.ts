import { httpContract } from "./httpContract";
import type {
  LocalProviderCatalog,
  LocalProviderEntry,
  LocalEndpointDiscoveryResponse,
  ModelPullDto,
  ModelPullStartResult,
  ProviderValidateResult,
} from "./types";

export interface LocalProvidersOptions {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

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

export interface LocalProvidersApi {
  listProviders(
    projectId?: string,
    options?: LocalProvidersOptions,
  ): Promise<LocalProviderCatalog>;
  providerStatus(
    provider: string,
    options?: LocalProvidersOptions,
  ): Promise<LocalProviderEntry>;
  setProviderKey(
    provider: string,
    key: string,
    validationToken?: string | null,
    options?: LocalProvidersOptions,
  ): Promise<LocalProviderCatalog>;
  deleteProviderKey(
    provider: string,
    options?: LocalProvidersOptions,
  ): Promise<LocalProviderCatalog>;
  createLocalEndpoint(
    input: LocalEndpointInput,
    options?: LocalProvidersOptions,
  ): Promise<LocalProviderCatalog>;
  discoverLocalEndpoints(
    options?: LocalProvidersOptions,
  ): Promise<LocalEndpointDiscoveryResponse>;
  updateLocalEndpoint(
    endpointId: string,
    patch: LocalEndpointPatch,
    options?: LocalProvidersOptions,
  ): Promise<LocalProviderCatalog>;
  deleteLocalEndpoint(
    endpointId: string,
    options?: LocalProvidersOptions,
  ): Promise<LocalProviderCatalog>;
  listModelPulls(
    options?: LocalProvidersOptions,
  ): Promise<{ pulls: ModelPullDto[] }>;
  getModelPull(
    id: number,
    options?: LocalProvidersOptions,
  ): Promise<ModelPullDto>;
  cancelModelPull(
    id: number,
    options?: LocalProvidersOptions,
  ): Promise<ModelPullDto>;
  startArtifactPull(
    ref: string,
    unpinnedAcknowledged?: boolean,
    options?: LocalProvidersOptions,
  ): Promise<ModelPullStartResult>;
  uninstallArtifact(
    ref: string,
    options?: LocalProvidersOptions,
  ): Promise<ModelPullDto>;
  validateProviderKey(
    provider: string,
    key?: string,
    options?: LocalProvidersOptions,
  ): Promise<ProviderValidateResult>;
}

type ContractErrorFactory = (status: number, payload: unknown) => Error;
export function createLocalProvidersApi(
  errorFactory: ContractErrorFactory,
): LocalProvidersApi {
  return {
    listProviders(projectId, options = {}) {
      return httpContract(
        "tenant.list_providers.get",
        {
          pathParams: {},
          query: { project_id: projectId },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    providerStatus(provider, options = {}) {
      return httpContract(
        "tenant.provider_status.get",
        {
          pathParams: { provider },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    setProviderKey(provider, key, validationToken, options = {}) {
      return httpContract(
        "tenant.set_provider_key.put",
        {
          pathParams: { provider },
          query: {},
          body: { key, validation_token: validationToken ?? null },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    deleteProviderKey(provider, options = {}) {
      return httpContract(
        "tenant.delete_provider_key.delete",
        {
          pathParams: { provider },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    createLocalEndpoint(input, options = {}) {
      return httpContract(
        "tenant.create_local_endpoint.post",
        {
          pathParams: {},
          query: {},
          body: { ...input },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    discoverLocalEndpoints(options = {}) {
      return httpContract(
        "tenant.discover_local_endpoints.post",
        {
          pathParams: {},
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    updateLocalEndpoint(endpointId, patch, options = {}) {
      return httpContract(
        "tenant.update_local_endpoint.patch",
        {
          pathParams: { endpoint_id: endpointId },
          query: {},
          body: { ...patch },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    deleteLocalEndpoint(endpointId, options = {}) {
      return httpContract(
        "tenant.delete_local_endpoint.delete",
        {
          pathParams: { endpoint_id: endpointId },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    listModelPulls(options = {}) {
      return httpContract(
        "tenant.list_model_pulls.get",
        {
          pathParams: {},
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    getModelPull(id, options = {}) {
      return httpContract(
        "tenant.get_model_pull.get",
        {
          pathParams: { pull_id: id },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    cancelModelPull(id, options = {}) {
      return httpContract(
        "tenant.cancel_model_pull.post",
        {
          pathParams: { pull_id: id },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    startArtifactPull(ref, unpinnedAcknowledged = false, options = {}) {
      return httpContract(
        "tenant.pull_artifact.post",
        {
          pathParams: {},
          query: {},
          body: { ref, unpinned_acknowledged: unpinnedAcknowledged },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    uninstallArtifact(ref, options = {}) {
      return httpContract(
        "tenant.uninstall_artifact.post",
        {
          pathParams: {},
          query: {},
          body: { ref },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    validateProviderKey(provider, key, options = {}) {
      return httpContract(
        "tenant.validate_provider_key.post",
        {
          pathParams: { provider },
          query: {},
          body: key ? { key } : {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },
  };
}
