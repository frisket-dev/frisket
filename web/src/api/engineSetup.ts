import { httpContract } from './httpContract';
import type { ModelPullStartResult } from './types';

export type ModelSetupScope = 'workspace' | 'organization';

export class ModelSetupRequestError extends Error {
  readonly status: number;
  readonly activePullId: number | null;
  constructor(status: number, activePullId: number | null = null) {
    super('Model setup request failed');
    this.status = status;
    this.activePullId = activePullId;
  }
}

/** Recover only a row identity from an error; fetch its typed projection next. */
export function modelSetupErrorFactory(status: number, payload: unknown): ModelSetupRequestError {
  if (status === 409 && typeof payload === 'object' && payload !== null && 'detail' in payload) {
    const detail = payload.detail;
    if (typeof detail === 'object' && detail !== null && 'code' in detail && detail.code === 'pull_busy' && 'active' in detail) {
      const active = detail.active;
      if (typeof active === 'object' && active !== null && 'id' in active && typeof active.id === 'number' && Number.isInteger(active.id) && active.id > 0) {
        return new ModelSetupRequestError(status, active.id);
      }
    }
  }
  return new ModelSetupRequestError(status);
}

export function startEngineSetup(
  scope: ModelSetupScope,
  setupRef: string,
  signal?: AbortSignal,
): Promise<ModelPullStartResult> {
  const options = { pathParams: {}, query: {}, body: { setup_ref: setupRef }, signal, errorFactory: modelSetupErrorFactory };
  return scope === 'organization'
    ? httpContract('outer.setup_org_model_engine.post', options)
    : httpContract('tenant.setup_model_engine.post', options);
}
