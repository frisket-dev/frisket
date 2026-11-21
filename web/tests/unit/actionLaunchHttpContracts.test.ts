import { afterEach, describe, expect, it, vi } from 'vitest';

import { createActionLaunchApi } from '../../src/api/actionLaunch';
import { ApiError } from '../../src/api/contractErrors';
import { jsonResponse } from '../support/httpContractTestHelpers';

function actionResult(status: string) {
  return {
    schema_version: 'frisket.action_result.v1' as const,
    action: { kind: 'map.classify', action_id: 'action-1' },
    status,
    project_id: 'project/one',
    run_id: null,
    receipt_id: null,
    errors: [],
  };
}

afterEach(() => vi.unstubAllGlobals());

describe('action-launch HTTP contract', () => {
  it('preserves action-run direct recursive JSON, encoding, content type, and abort signal', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return jsonResponse(actionResult('completed'));
    }));
    const signal = new AbortController().signal;
    const api = createActionLaunchApi('project/one');

    await expect(api.launch({
      kind: 'map.classify',
      params: { labels: ['yes', 'no'], nested: { retained: true } },
      extension: { producer_owned: ['open', 1] },
    }, { signal })).resolves.toMatchObject({ status: 200, result: { status: 'completed' } });

    expect(requests).toHaveLength(1);
    expect(requests[0]?.input).toBe('/api/projects/project%2Fone/actions/v1/run');
    expect(requests[0]?.init?.method).toBe('POST');
    expect(JSON.parse(String(requests[0]?.init?.body))).toEqual({
      kind: 'map.classify',
      params: { labels: ['yes', 'no'], nested: { retained: true } },
      extension: { producer_owned: ['open', 1] },
    });
    expect(requests[0]?.init?.signal).toBe(signal);
    expect([...new Headers(requests[0]?.init?.headers).entries()]).toEqual([
      ['content-type', 'application/json'],
    ]);
  });

  it.each([
    [400, 'failed'],
    [402, 'needs_confirmation'],
    [409, 'failed'],
    [500, 'failed'],
  ])('returns HTTP %i ActionResult outcomes as values', async (status, resultStatus) => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(actionResult(resultStatus), status)));
    const api = createActionLaunchApi('project-1');

    await expect(api.launch({ kind: 'map.classify' })).resolves.toMatchObject({
      status,
      result: { schema_version: 'frisket.action_result.v1', status: resultStatus },
    });
  });

  it.each([402, 503])('keeps flat hosted HTTP %i failures as ordinary ApiErrors', async (status) => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ detail: `hosted-${status}` }, status)));
    const api = createActionLaunchApi('project-1');

    const rejection = api.launch({ kind: 'map.classify' });
    await expect(rejection).rejects.toBeInstanceOf(ApiError);
    await expect(rejection).rejects.toMatchObject({
      name: 'ApiError',
      status,
      message: `hosted-${status}`,
      code: undefined,
      details: undefined,
    });
  });
});
