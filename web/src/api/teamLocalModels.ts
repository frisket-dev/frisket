import { observeSetup, trackSetupSubmission } from '../telemetry/setupTelemetry';
import { httpContract, type ContractErrorFactory, type RequestOptions } from './httpContract';
import type {
  ModelPullDto,
  ModelPullStartResult,
  LocalEndpointCatalog,
} from './types';

export function createTeamLocalModelsApi(errorFactory?: ContractErrorFactory) {
  return {
    listOrgLocalEndpoints(options: RequestOptions = {}): Promise<LocalEndpointCatalog> {
      return httpContract(
        'outer.list_org_local_endpoints.get',
        {
          signal: options.signal,
          errorFactory,
        },
      );
    },

    orgStartArtifactPull(
      ref: string,
      unpinnedAcknowledged = false,
      options: RequestOptions = {},
    ): Promise<ModelPullStartResult> {
      return trackSetupSubmission('organization', 'model', httpContract(
        'outer.post_org_models_pull.post',
        {
          body: { ref, unpinned_acknowledged: unpinnedAcknowledged },
          signal: options.signal,
          errorFactory,
        },
      ));
    },

    orgListModelPulls(options: RequestOptions = {}): Promise<{ pulls: ModelPullDto[] }> {
      return httpContract(
        'outer.list_org_model_pulls.get',
        {
          signal: options.signal,
          errorFactory,
        },
      );
    },

    orgGetModelPull(id: number, options: RequestOptions = {}): Promise<ModelPullDto> {
      return httpContract(
        'outer.get_org_model_pull.get',
        {
          pathParams: { pull_id: id },
          signal: options.signal,
          errorFactory,
        },
      ).then((pull) => observeSetup('organization', pull));
    },

    orgCancelModelPull(id: number, options: RequestOptions = {}): Promise<ModelPullDto> {
      return httpContract(
        'outer.cancel_org_model_pull.post',
        {
          pathParams: { pull_id: id },
          signal: options.signal,
          errorFactory,
        },
      );
    },
  };
}
