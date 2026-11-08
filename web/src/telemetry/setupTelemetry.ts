import type { ModelPullDto, ModelPullStartResult } from '../api/types';
import type { ModelSetupScope } from '../api/engineSetup';
import { durationBucket, failureCategory, sendProductTelemetry } from './productTelemetry';

type SetupKind = 'engine' | 'model';
// Only observe operations started here, not historical pulls loaded in Settings.
const pending = new Map<string, { setupKind: SetupKind; startedAt: number }>();
const key = (scope: ModelSetupScope, id: number) => `${scope}:${id}`;

export async function trackSetupSubmission(
  scope: ModelSetupScope,
  setupKind: SetupKind,
  request: Promise<ModelPullStartResult>,
): Promise<ModelPullStartResult> {
  const startedAt = performance.now();
  try {
    const response = await request;
    const pull = response.pull;
    if (pull.status === 'uninstalled') return response;
    sendProductTelemetry({ type: 'Setup.submitted', properties: {
      setupKind, result: pull.status === 'done' ? 'success' : pull.status === 'failed' ? 'failed' : pull.status === 'cancelled' ? 'cancelled' : 'queued',
      failureCategory: pull.status === 'failed' ? 'unknown' : 'none',
    } });
    const id = key(scope, pull.id);
    if (!pending.has(id)) pending.set(id, { setupKind, startedAt });
    observeSetup(scope, pull);
    return response;
  } catch (error) {
    sendProductTelemetry({ type: 'Setup.submitted', properties: {
      setupKind, result: 'failed', failureCategory: failureCategory(error),
    } });
    throw error;
  }
}

export function observeSetup(scope: ModelSetupScope, pull: ModelPullDto): ModelPullDto {
  const id = key(scope, pull.id);
  const observation = pending.get(id);
  if (!observation || pull.status === 'pending' || pull.status === 'running') return pull;
  pending.delete(id);
  // Uninstalls aren't setup outcomes.
  if (pull.status === 'uninstalled') return pull;
  sendProductTelemetry({ type: 'Setup.observed', properties: {
    setupKind: observation.setupKind,
    observedDuration: durationBucket(performance.now() - observation.startedAt),
    result: pull.status === 'done' ? 'success' : pull.status === 'cancelled' ? 'cancelled' : 'failed',
    failureCategory: pull.status === 'failed' ? 'unknown' : 'none',
  } });
  return pull;
}
