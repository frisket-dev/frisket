import { httpContract } from './httpContract';
import type {
  ModelPullDto,
  ModelPullStartResult,
  LocalEndpointCatalog,
} from './types';

export interface TeamLocalModelsOptions {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

export interface TeamLocalModelsApi {
  listOrgLocalEndpoints(options?: TeamLocalModelsOptions): Promise<LocalEndpointCatalog>;
  orgStartArtifactPull(
    ref: string,
    unpinnedAcknowledged?: boolean,
    options?: TeamLocalModelsOptions,
  ): Promise<ModelPullStartResult>;
  orgListModelPulls(options?: TeamLocalModelsOptions): Promise<{ pulls: ModelPullDto[] }>;
  orgGetModelPull(id: number, options?: TeamLocalModelsOptions): Promise<ModelPullDto>;
  orgCancelModelPull(id: number, options?: TeamLocalModelsOptions): Promise<ModelPullDto>;
}

type ContractErrorFactory = (status: number, payload: unknown) => Error;
export function createTeamLocalModelsApi(
  errorFactory: ContractErrorFactory,
): TeamLocalModelsApi {
  return {
    listOrgLocalEndpoints(options = {}) {
      return httpContract(
        'outer.list_org_local_endpoints.get',
        {
          pathParams: {},
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    orgStartArtifactPull(ref, unpinnedAcknowledged = false, options = {}) {
      return httpContract(
        'outer.post_org_models_pull.post',
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

    orgListModelPulls(options = {}) {
      return httpContract(
        'outer.list_org_model_pulls.get',
        {
          pathParams: {},
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    orgGetModelPull(id, options = {}) {
      return httpContract(
        'outer.get_org_model_pull.get',
        {
          pathParams: { pull_id: id },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },

    orgCancelModelPull(id, options = {}) {
      return httpContract(
        'outer.cancel_org_model_pull.post',
        {
          pathParams: { pull_id: id },
          query: {},
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
      );
    },
  };
}
