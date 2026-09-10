import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  cancelRunContract,
  getActionJobContract,
  getReceiptContract,
  getRunProgressContract,
  getRunRowsContract,
  listActionJobsContract,
} from '../../src/api/httpContractRoutes';

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function contractError(status: number, payload: unknown): Error {
  return new Error(`unexpected contract error ${status}: ${JSON.stringify(payload)}`);
}

class MappedContractError extends Error {
  readonly status: number;
  readonly payload: unknown;

  constructor(status: number, payload: unknown) {
    super(`mapped contract error ${status}`);
    this.name = 'MappedContractError';
    this.status = status;
    this.payload = payload;
  }
}

async function expectMapped(request: Promise<unknown>, status: number): Promise<void> {
  await expect(request).rejects.toMatchObject({
    name: 'MappedContractError',
    status,
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('run and receipt HTTP contracts', () => {
  it.each([null, false, 0, '', { matched: 2 }])('maps receipt presentation and domain value %j', async (value) => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({
      schema_version: 'frisket.receipt.v1',
      receipt_id: 'receipt-1',
      project_id: 'project-1',
      action_id: 'action-1',
      action_kind: 'map.template',
      run_id: 7,
      op_ids: [3],
      idempotency_key: 'template@sha256:v1',
      params_hash: 'sha256:v1',
      status: 'completed',
      inputs: [],
      outputs: [],
      value,
      provider_use: [],
      evidence: [],
      errors: [],
    })));

    const receipt = await getReceiptContract('project-1', 'receipt-1', contractError);
    expect(receipt).toMatchObject({
      receiptId: 'receipt-1',
      actionId: 'action-1',
      actionKind: 'map.template',
      runId: '7',
      value,
    });
    expect(Object.keys(receipt).sort()).toEqual([
      'actionId', 'actionKind', 'errors', 'evidence', 'idempotencyKey', 'inputs',
      'opIds', 'outputs', 'paramsHash', 'projectId', 'providerUse', 'receiptId',
      'runId', 'schemaVersion', 'status', 'value',
    ]);
  });

  it('accepts each route declared status, including middleware and framework statuses', async () => {
    const responses = [
      jsonResponse({ detail: 'run not found' }, 404),
      jsonResponse({ detail: 'unsupported row status filter' }, 400),
      jsonResponse({ detail: 'editor role required' }, 403),
      jsonResponse({ detail: 'run cannot be cancelled' }, 409),
      jsonResponse({ detail: 'invalid stored receipt' }, 500),
      jsonResponse({ detail: 'invalid limit' }, 422),
      jsonResponse({ detail: 'invalid job id' }, 422),
    ];
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return responses.shift()!;
    }));
    const errorFactory = vi.fn(
      (status: number, payload: unknown) => new MappedContractError(status, payload),
    );
    const controller = new AbortController();
    const options = {
      signal: controller.signal,
      headers: new Headers({
        Authorization: 'Bearer token',
        'Content-Type': 'application/vnd.frisket+json',
        'X-Trace-Id': 'trace-runs',
      }),
    };

    await expectMapped(
      getRunProgressContract('project/one', 17, errorFactory, options),
      404,
    );
    await expectMapped(
      getRunRowsContract(
        'project/one',
        17,
        { offset: 0, limit: 500, status: 'error' },
        errorFactory,
        options,
      ),
      400,
    );
    await expectMapped(cancelRunContract('project/one', 17, errorFactory, options), 403);
    await expectMapped(cancelRunContract('project/one', 17, errorFactory, options), 409);
    await expectMapped(
      getReceiptContract('project/one', 'receipt/one', errorFactory, options),
      500,
    );
    await expectMapped(
      listActionJobsContract('project/one', { status: null, limit: 50 }, errorFactory, options),
      422,
    );
    await expectMapped(getActionJobContract('project/one', 23, errorFactory, options), 422);

    expect(requests.map((request) => request.input)).toEqual([
      '/api/projects/project%2Fone/actions/runs/17/status',
      '/api/projects/project%2Fone/actions/runs/17/rows?offset=0&limit=500&status=error',
      '/api/projects/project%2Fone/actions/runs/17/cancel',
      '/api/projects/project%2Fone/actions/runs/17/cancel',
      '/api/projects/project%2Fone/actions/v1/receipts/receipt%2Fone',
      '/api/projects/project%2Fone/actions/jobs?limit=50',
      '/api/projects/project%2Fone/actions/jobs/23',
    ]);
    expect(errorFactory).toHaveBeenCalledTimes(7);
    for (const request of requests) {
      expect(request.init?.signal).toBe(controller.signal);
      const headers = new Headers(request.init?.headers);
      expect(headers.get('authorization')).toBe('Bearer token');
      expect(headers.get('content-type')).toBe('application/vnd.frisket+json');
      expect(headers.get('x-trace-id')).toBe('trace-runs');
    }
  });

  it('maps an additional non-2xx status', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ detail: 'teapot' }, 418)));
    const errorFactory = vi.fn(
      (status: number, payload: unknown) => new MappedContractError(status, payload),
    );

    await expect(
      getReceiptContract('project-1', 'receipt-1', errorFactory),
    ).rejects.toMatchObject({
      name: 'MappedContractError',
      status: 418,
      payload: { detail: 'teapot' },
    });
    expect(errorFactory).toHaveBeenCalledOnce();
    expect(errorFactory).toHaveBeenCalledWith(418, { detail: 'teapot' });
  });

  it('forwards the exact signal and preserves abort rejection', async () => {
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

    const request = getActionJobContract('project-1', 23, contractError, {
      signal: controller.signal,
    });
    const abortError = new DOMException('cancelled', 'AbortError');
    controller.abort(abortError);

    await expect(request).rejects.toBe(abortError);
    expect(forwardedSignal).toBe(controller.signal);
  });
});
