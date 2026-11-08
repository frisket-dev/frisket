import { beforeEach, expect, it, vi } from 'vitest';
import { startEngineSetup } from '../../src/api/engineSetup';
import { createLocalProvidersApi } from '../../src/api/localProviders';
import type { ModelPullDto } from '../../src/api/types';

const mocks = vi.hoisted(() => ({ http: vi.fn(), telemetry: vi.fn() }));
vi.mock('../../src/api/httpContract', () => ({ httpContract: mocks.http }));
vi.mock('../../src/telemetry/productTelemetry', async (original) => ({
  ...await original<typeof import('../../src/telemetry/productTelemetry')>(), sendProductTelemetry: mocks.telemetry,
}));
const providers = createLocalProvidersApi(() => new Error('request failed'));
const pull = (id: number, status: ModelPullDto['status']): ModelPullDto => ({
  id, status, schemaVersion: 'frisket.model_pull.v4', model: 'private/model/reference', display_name: 'Private model name',
  operation_kind: 'engine_setup', phase: 'provisioning', total_bytes: null, completed_bytes: null,
  capabilities: { cancel: true, retry: false, remove: false }, error: null,
  resolved_digest: null, resolved_size: null, created_at: '', started_at: null, finished_at: null,
  cancel_requested: false, endpoint_id: null, endpoint_origin: null, initiated_by: null, artifact: null,
});
beforeEach(() => { vi.clearAllMocks(); });

it('records setup admission and one completion across repeated progress reads, without model identifiers', async () => {
  mocks.http.mockResolvedValueOnce({ pull: pull(91, 'running'), deduplicated: false });
  await startEngineSetup('workspace', 'private/model/reference');
  expect(mocks.telemetry).toHaveBeenLastCalledWith({ type: 'Setup.submitted', properties: {
    setupKind: 'engine', result: 'queued', failureCategory: 'none',
  } });
  mocks.http.mockResolvedValue(pull(91, 'done'));
  await providers.getModelPull(91);
  await providers.getModelPull(91);
  expect(mocks.telemetry).toHaveBeenCalledTimes(2);
  expect(mocks.telemetry).toHaveBeenLastCalledWith({ type: 'Setup.observed', properties: {
    setupKind: 'engine', observedDuration: 'lt_1s', result: 'success', failureCategory: 'none',
  } });
  expect(JSON.stringify(mocks.telemetry.mock.calls)).not.toContain('private/');
});

it('ignores completed historical pulls that this browser did not start', async () => {
  mocks.http.mockResolvedValue(pull(92, 'done'));
  await providers.getModelPull(92);
  expect(mocks.telemetry).not.toHaveBeenCalled();
});

it('reports setup request failure and preserves the original rejection', async () => {
  const error = Object.assign(new Error('private endpoint'), { status: 403 });
  mocks.http.mockRejectedValueOnce(error);
  await expect(startEngineSetup('workspace', 'private/model/reference')).rejects.toBe(error);
  expect(mocks.telemetry).toHaveBeenCalledWith({ type: 'Setup.submitted', properties: {
    setupKind: 'engine', result: 'failed', failureCategory: 'permission',
  } });
});

it('reports a model download failure observed after successful admission', async () => {
  mocks.http.mockResolvedValueOnce({ pull: pull(93, 'pending'), deduplicated: false });
  await providers.startArtifactPull('private/model/reference');
  mocks.http.mockResolvedValue(pull(93, 'failed'));
  await providers.getModelPull(93);
  expect(mocks.telemetry).toHaveBeenLastCalledWith({ type: 'Setup.observed', properties: {
    setupKind: 'model', observedDuration: 'lt_1s', result: 'failed', failureCategory: 'unknown',
  } });
});

it.each([['failed', 'failed'], ['cancelled', 'cancelled'], ['done', 'success']] as const)(
  'records an immediately %s setup response truthfully', async (status, expected) => {
    mocks.http.mockResolvedValueOnce({ pull: pull(99, status), deduplicated: false });
    await startEngineSetup('workspace', 'private/model/reference');
    expect(mocks.telemetry).toHaveBeenNthCalledWith(1, { type: 'Setup.submitted', properties: {
      setupKind: 'engine', result: expected, failureCategory: status === 'failed' ? 'unknown' : 'none',
    } });
  },
);
