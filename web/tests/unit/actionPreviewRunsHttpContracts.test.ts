import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  createActionPreviewDomainApi,
  createActionPreviewRunsApi,
} from '../../src/api/actionPreviewRuns';

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

class MappedContractError extends Error {
  readonly status: number;
  readonly payload: unknown;

  constructor(status: number, payload: unknown) {
    super(`mapped preview error ${status}`);
    this.name = 'MappedContractError';
    this.status = status;
    this.payload = payload;
  }
}

afterEach(() => vi.unstubAllGlobals());

describe('action-preview lifecycle HTTP contracts', () => {
  it.each(['done', 'error', 'cancelled'] as const)('preserves accounting without a sample for %s', async (status) => {
    const accounting = { receipt_id: 'receipt-paid', status: status === 'done' ? 'completed' :
      status === 'error' ? 'failed' : 'cancelled', model_call_count: 1, cost_actual: null, elapsed_ms: 3200 };
    const responses = [jsonResponse({ schema_version: 'frisket.action_preview.v1',
      preview_id: 'paid', total: 1 }, 202), jsonResponse({
      schema_version: 'frisket.action_preview.v1', preview_id: 'paid', status,
      progress: { done: 0, total: 1 }, accounting,
    })];
    vi.stubGlobal('fetch', vi.fn(async () => responses.shift() as Response));
    const api = createActionPreviewDomainApi({
      errorFactory: (code, payload) => new MappedContractError(code, payload),
    }, 'project-one');
    await api.start({ action_id: 'media.ocr', scope: { kind: 'project' },
      params: {}, output_names: {}, idempotency_key: 'preview-paid' }, {});
    await expect(api.get('paid')).resolves.toMatchObject({ status, accounting });
  });
  it('keeps source-free table records ordered and unknown totals null', async () => {
    const responses = [
      jsonResponse({ schema_version: 'frisket.action_preview.v1', preview_id: 'table', total: null }, 202),
      jsonResponse({ schema_version: 'frisket.action_preview.v1', preview_id: 'table', status: 'done',
        progress: { done: 2, total: null }, result: { kind: 'table',
          columns: [{ name: 'name', column_type: 'text', format: null, hidden: false, overwrites_column_id: null }],
          rows: [{ name: { value: 'Second' } }, { name: { value: 'First' } }], sampled: 2, total: null,
          warnings: ['Page image unavailable; text retained.'],
        } }),
    ];
    vi.stubGlobal('fetch', vi.fn(async () => responses.shift() as Response));
    const api = createActionPreviewDomainApi({
      errorFactory: (status, payload) => new MappedContractError(status, payload),
    }, 'project-one');
    await expect(api.start({ action_id: 'import.files', scope: { kind: 'project' },
      params: {}, output_names: {}, sheet_name: 'Files', idempotency_key: 'sample' }, {}))
      .resolves.toEqual({ previewId: 'table', total: null });
    const result = await api.get('table');
    expect(result).toMatchObject({ kind: 'table', progress: { done: 2, total: null },
      rows: [{ name: { value: 'Second' } }, { name: { value: 'First' } }], sampled: 2, total: null,
      warnings: ['Page image unavailable; text retained.'] });
    expect(result).not.toHaveProperty('sheetId');
    expect(result).not.toHaveProperty('rowIds');
  });

  it('preserves lifecycle direct action, encoding, headers, and abort signal', async () => {
    const projectId = 'project /%?☃';
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    const responses = [
      jsonResponse({
        schema_version: 'frisket.action_preview.v1',
        preview_id: 'preview/a',
        total: 3,
      }, 202),
      jsonResponse({
        schema_version: 'frisket.action_preview.v1',
        preview_id: 'preview/a',
        status: 'running',
        progress: { done: 0, total: 3 },
      }),
      new Response(null, { status: 204 }),
    ];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return responses.shift() as Response;
    }));
    const signal = new AbortController().signal;
    const api = createActionPreviewRunsApi(
      (status, payload) => new MappedContractError(status, payload),
      projectId,
    );
    const options = { signal, headers: { Authorization: 'Bearer preview' } };

    await expect(api.start({
      kind: 'map.classify',
      params: { model: 'x', labels: ['yes', 'no'] },
      extension: { selected: true },
    }, options)).resolves.toMatchObject({ preview_id: 'preview/a', total: 3 });
    await expect(api.get('preview/a', options)).resolves.toMatchObject({ status: 'running' });
    await expect(api.cancel('preview/a', options)).resolves.toBeUndefined();

    expect(requests.map((request) => request.input)).toEqual([
      '/api/projects/project%20%2F%25%3F%E2%98%83/actions/v1/preview',
      '/api/projects/project%20%2F%25%3F%E2%98%83/actions/v1/preview/preview%2Fa',
      '/api/projects/project%20%2F%25%3F%E2%98%83/actions/v1/preview/preview%2Fa',
    ]);
    expect(requests.map((request) => request.init?.method)).toEqual(['POST', 'GET', 'DELETE']);
    expect(JSON.parse(String(requests[0]?.init?.body))).toEqual({
      kind: 'map.classify',
      params: { model: 'x', labels: ['yes', 'no'] },
      extension: { selected: true },
    });
    for (const request of requests) {
      expect(request.init?.signal).toBe(signal);
      expect(new Headers(request.init?.headers).get('authorization')).toBe('Bearer preview');
    }
    expect(new Headers(requests[0]?.init?.headers).get('content-type')).toBe('application/json');
  });

  it.each([400, 402, 404])('maps lifecycle HTTP %i through the provided error factory', async (status) => {
    const errorFactory = vi.fn(
      (actualStatus: number, payload: unknown) =>
        new MappedContractError(actualStatus, payload),
    );
    const payload = {
      schema_version: 'frisket.action_preview.v1',
      error: { code: 'preview_not_found', message: `error-${status}` },
    };
    const responses = [
      jsonResponse({
        schema_version: 'frisket.action_preview.v1',
        preview_id: 'missing',
        total: 1,
      }, 202),
      jsonResponse(payload, status),
    ];
    vi.stubGlobal('fetch', vi.fn(async () => responses.shift() as Response));
    const api = createActionPreviewRunsApi(errorFactory, 'project/one');

    await api.start({ kind: 'map.classify', params: {} });
    await expect(api.get('missing')).rejects.toMatchObject({
      name: 'MappedContractError',
      status,
      payload,
    });
    expect(errorFactory).toHaveBeenCalledWith(status, payload);
  });

  it('forwards abort identity without replacing the browser rejection', async () => {
    const controller = new AbortController();
    let forwardedSignal: AbortSignal | null | undefined;
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if ((init?.method ?? 'GET') === 'POST') {
        return Promise.resolve(jsonResponse({
          schema_version: 'frisket.action_preview.v1',
          preview_id: 'preview-1',
          total: 1,
        }, 202));
      }
      forwardedSignal = init?.signal;
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => reject(init.signal?.reason), {
          once: true,
        });
      });
    }));
    const api = createActionPreviewRunsApi(
      (status, payload) => new MappedContractError(status, payload),
      'project/one',
    );
    await api.start({ kind: 'map.classify', params: {} });
    const pending = api.get('preview-1', { signal: controller.signal });
    const abortError = new DOMException('preview cancelled', 'AbortError');
    controller.abort(abortError);

    await expect(pending).rejects.toBe(abortError);
    expect(forwardedSignal).toBe(controller.signal);
  });
});
