import { modelSetupErrorFactory, type ModelSetupScope } from './engineSetup';
import { createLocalProvidersApi } from './localProviders';
import { createTeamLocalModelsApi } from './teamLocalModels';

const workspace = createLocalProvidersApi(modelSetupErrorFactory);
const organization = createTeamLocalModelsApi(modelSetupErrorFactory);

export function readModelPull(scope: ModelSetupScope, id: number, options: { signal?: AbortSignal } = {}) {
  return scope === 'organization'
    ? organization.organizationGetModelPull(id, options) : workspace.getModelPull(id, options);
}

export function cancelScopedModelPull(scope: ModelSetupScope, id: number, options: { signal?: AbortSignal } = {}) {
  return scope === 'organization'
    ? organization.organizationCancelModelPull(id, options) : workspace.cancelModelPull(id, options);
}
