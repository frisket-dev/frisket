import { modelSetupErrorFactory, type ModelSetupScope } from './engineSetup';
import { createLocalProvidersApi } from './localProviders';
import { createTeamLocalModelsApi } from './teamLocalModels';

const workspace = createLocalProvidersApi(modelSetupErrorFactory);
const organization = createTeamLocalModelsApi(modelSetupErrorFactory);

export function readModelPull(scope: ModelSetupScope, id: number, options: { signal?: AbortSignal } = {}) {
  return scope === 'organization'
    ? organization.orgGetModelPull(id, options) : workspace.getModelPull(id, options);
}

export function cancelScopedModelPull(scope: ModelSetupScope, id: number, options: { signal?: AbortSignal } = {}) {
  return scope === 'organization'
    ? organization.orgCancelModelPull(id, options) : workspace.cancelModelPull(id, options);
}
