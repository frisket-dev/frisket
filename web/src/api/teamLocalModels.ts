import { observeSetup, trackSetupSubmission } from '../telemetry/setupTelemetry';
import { httpContract, type ContractErrorFactory, type RequestOptions } from './httpContract';
import type {
  ModelPullDto,
  ModelPullStartResult,
  LocalEndpointCatalog,
} from './types';

export function createTeamLocalModelsApi(errorFactory?: ContractErrorFactory) {
  return {
    listOrganizationLocalEndpoints(options: RequestOptions = {}): Promise<LocalEndpointCatalog> {
      return httpContract(
        'outer.list_organization_local_endpoints.get',
        {
          signal: options.signal,
          errorFactory,
        },
      );
    },

    organizationStartArtifactPull(
      ref: string,
      unpinnedAcknowledged = false,
      options: RequestOptions = {},
    ): Promise<ModelPullStartResult> {
      return trackSetupSubmission('organization', 'model', httpContract(
        'outer.post_organization_models_pull.post',
        {
          body: { ref, unpinned_acknowledged: unpinnedAcknowledged },
          signal: options.signal,
          errorFactory,
        },
      ));
    },

    organizationListModelPulls(options: RequestOptions = {}): Promise<{ pulls: ModelPullDto[] }> {
      return httpContract(
        'outer.list_organization_model_pulls.get',
        {
          signal: options.signal,
          errorFactory,
        },
      );
    },

    organizationGetModelPull(id: number, options: RequestOptions = {}): Promise<ModelPullDto> {
      return httpContract(
        'outer.get_organization_model_pull.get',
        {
          pathParams: { pull_id: id },
          signal: options.signal,
          errorFactory,
        },
      ).then((pull) => observeSetup('organization', pull));
    },

    organizationCancelModelPull(id: number, options: RequestOptions = {}): Promise<ModelPullDto> {
      return httpContract(
        'outer.cancel_organization_model_pull.post',
        {
          pathParams: { pull_id: id },
          signal: options.signal,
          errorFactory,
        },
      );
    },
  };
}
