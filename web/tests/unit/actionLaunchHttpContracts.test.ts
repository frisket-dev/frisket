import { afterEach, describe, expect, it, vi } from 'vitest';

import { createActionLaunchApi } from '../../src/api/actionLaunch';

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

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

class MappedContractError extends Error {
  readonly status: number;
  readonly payload: unknown;

  constructor(status: number, payload: unknown) {
    super(`mapped action-launch error ${status}`);
    this.name = 'MappedContractError';
    this.status = status;
    this.payload = payload;
  }
}

afterEach(() => vi.unstubAllGlobals());

describe('action-launch HTTP contract', () => {
  it('preserves action-run direct recursive JSON, encoding, headers, and abort signal', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return jsonResponse(actionResult('completed'));
    }));
    const signal = new AbortController().signal;
    const api = createActionLaunchApi(
      (status, payload) => new MappedContractError(status, payload),
      'project/one',
    );

    await expect(api.launch({
      kind: 'map.classify',
      params: { labels: ['yes', 'no'], nested: { retained: true } },
      extension: { producer_owned: ['open', 1] },
    }, {
      signal,
      headers: { Authorization: 'Bearer action' },
    })).resolves.toMatchObject({ status: 200, result: { status: 'completed' } });

    expect(requests).toHaveLength(1);
    expect(requests[0]?.input).toBe('/api/projects/project%2Fone/actions/v1/run');
    expect(requests[0]?.init?.method).toBe('POST');
    expect(JSON.parse(String(requests[0]?.init?.body))).toEqual({
      kind: 'map.classify',
      params: { labels: ['yes', 'no'], nested: { retained: true } },
      extension: { producer_owned: ['open', 1] },
    });
    expect(requests[0]?.init?.signal).toBe(signal);
    expect(new Headers(requests[0]?.init?.headers).get('authorization')).toBe('Bearer action');
    expect(new Headers(requests[0]?.init?.headers).get('content-type')).toBe('application/json');
  });

  it.each([
    [400, 'failed'],
    [402, 'needs_confirmation'],
    [409, 'failed'],
    [500, 'failed'],
  ])('returns HTTP %i ActionResult outcomes as values', async (status, resultStatus) => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(actionResult(resultStatus), status)));
    const api = createActionLaunchApi(
      (actualStatus, payload) => new MappedContractError(actualStatus, payload),
      'project-1',
    );

    await expect(api.launch({ kind: 'map.classify' })).resolves.toMatchObject({
      status,
      result: { schema_version: 'frisket.action_result.v1', status: resultStatus },
    });
  });

  it.each([402, 503])('keeps flat hosted HTTP %i failures as ordinary mapped errors', async (status) => {
    const payload = { detail: `hosted-${status}` };
    const errorFactory = vi.fn(
      (actualStatus: number, actualPayload: unknown) =>
        new MappedContractError(actualStatus, actualPayload),
    );
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(payload, status)));
    const api = createActionLaunchApi(errorFactory, 'project-1');

    await expect(api.launch({ kind: 'map.classify' })).rejects.toMatchObject({
      name: 'MappedContractError',
      status,
      payload,
    });
    expect(errorFactory).toHaveBeenCalledWith(status, payload);
  });

  it('forwards abort identity without replacing the browser rejection', async () => {
    const controller = new AbortController();
    let forwardedSignal: AbortSignal | null | undefined;
    vi.stubGlobal('fetch', vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      forwardedSignal = init?.signal;
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => reject(init.signal?.reason), {
          once: true,
        });
      });
    }));
    const api = createActionLaunchApi(
      (status, payload) => new MappedContractError(status, payload),
      'project-1',
    );
    const pending = api.launch({ kind: 'map.classify' }, { signal: controller.signal });
    const abortError = new DOMException('action cancelled', 'AbortError');
    controller.abort(abortError);

    await expect(pending).rejects.toBe(abortError);
    expect(forwardedSignal).toBe(controller.signal);
  });
});
