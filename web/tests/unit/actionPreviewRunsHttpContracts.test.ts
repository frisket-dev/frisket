import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  createActionPreviewApi,
  createActionPreviewRunsApi,
} from '../../src/api/actionPreviewRuns';
import { ApiError, ConfirmationRequiredError } from '../../src/api/contractErrors';
import { jsonResponse } from '../support/httpContractTestHelpers';

afterEach(() => vi.unstubAllGlobals());

describe('action-preview lifecycle HTTP contracts', () => {
  it('carries the running zero-row preparation hint into the preview domain result', async () => {
    const responses = [
      jsonResponse({
        schema_version: 'frisket.action_preview.v1', preview_id: 'preparing', total: 10,
      }, 202),
      jsonResponse({
        schema_version: 'frisket.action_preview.v1', preview_id: 'preparing', status: 'running',
        progress: {
          done: 0,
          total: 10,
          preparation: {
            message: 'Preparing the language model if this worker needs it, then translating…',
          },
        },
      }),
    ];
    vi.stubGlobal('fetch', vi.fn(async () => responses.shift() as Response));
    const api = createActionPreviewApi('project-one');
    await api.startPreview({ action_id: 'map.translate', scope: { kind: 'sheet_rows', sheet_id: 1 },
      params: {}, output_names: {}, idempotency_key: 'preview-preparing' });

    await expect(api.getPreview('preparing')).resolves.toMatchObject({
      status: 'running',
      progress: {
        done: 0,
        preparation: {
          message: 'Preparing the language model if this worker needs it, then translating…',
        },
      },
    });
  });

  it.each(['done', 'error', 'cancelled'] as const)('preserves accounting without a sample for %s', async (status) => {
    const accounting = { receipt_id: 'receipt-paid', status: status === 'done' ? 'completed' :
      status === 'error' ? 'failed' : 'cancelled', model_call_count: 1, cost_actual: null, elapsed_ms: 3200 };
    const responses = [jsonResponse({ schema_version: 'frisket.action_preview.v1',
      preview_id: 'paid', total: 1 }, 202), jsonResponse({
      schema_version: 'frisket.action_preview.v1', preview_id: 'paid', status,
      progress: { done: 0, total: 1 }, accounting,
    })];
    vi.stubGlobal('fetch', vi.fn(async () => responses.shift() as Response));
    const api = createActionPreviewApi('project-one');
    await api.startPreview({ action_id: 'media.ocr', scope: { kind: 'project' },
      params: {}, output_names: {}, idempotency_key: 'preview-paid' });
    await expect(api.getPreview('paid')).resolves.toMatchObject({ status, accounting });
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
    const api = createActionPreviewApi('project-one');
    await expect(api.startPreview({ action_id: 'import.files', scope: { kind: 'project' },
      params: {}, output_names: {}, sheet_name: 'Files', idempotency_key: 'sample' }))
      .resolves.toEqual({ previewId: 'table', total: null });
    const result = await api.getPreview('table');
    expect(result).toMatchObject({ kind: 'table', progress: { done: 2, total: null },
      rows: [{ name: { value: 'Second' } }, { name: { value: 'First' } }], sampled: 2, total: null,
      warnings: ['Page image unavailable; text retained.'] });
    expect(result).not.toHaveProperty('sheetId');
    expect(result).not.toHaveProperty('rowIds');
  });

  it('preserves lifecycle direct action, encoding, content type, and abort signal', async () => {
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
    const api = createActionPreviewRunsApi(projectId);
    const options = { signal };

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
    }
    expect([...new Headers(requests[0]?.init?.headers).entries()]).toEqual([
      ['content-type', 'application/json'],
    ]);
    expect(requests[1]?.init?.headers).toBeUndefined();
    expect(requests[2]?.init?.headers).toBeUndefined();
  });

  it.each([
    [400, undefined],
    [404, undefined],
    [409, { preview_id: 'missing' }],
  ] as const)('maps lifecycle HTTP %i preview envelopes to ApiError with details %j', async (status, details) => {
    const responses = [
      jsonResponse({
        schema_version: 'frisket.action_preview.v1',
        preview_id: 'missing',
        total: 1,
      }, 202),
      jsonResponse({
        schema_version: 'frisket.action_preview.v1',
        error: {
          code: 'preview_not_found',
          message: `error-${status}`,
          ...(details === undefined ? {} : { details }),
        },
      }, status),
    ];
    vi.stubGlobal('fetch', vi.fn(async () => responses.shift() as Response));
    const api = createActionPreviewRunsApi('project/one');

    await api.start({ kind: 'map.classify', params: {} });
    const rejection = api.get('missing');
    await expect(rejection).rejects.toBeInstanceOf(ApiError);
    await expect(rejection).rejects.toMatchObject({
      name: 'ApiError',
      status,
      message: `error-${status}`,
      code: 'preview_not_found',
      details,
    });
  });

  it('maps a lifecycle HTTP 402 preview envelope to a consent error carrying every quoted fact', async () => {
    const estimate = { cost: 0.42, rows: 7, model: 'gpt-x', pricing_version: 'v3' };
    const claims = [{ field: 'cost', display: '$0.42' }];
    const responses = [
      jsonResponse({
        schema_version: 'frisket.action_preview.v1',
        preview_id: 'quoted',
        total: 7,
      }, 202),
      jsonResponse({
        schema_version: 'frisket.action_preview.v1',
        error: {
          code: 'needs_confirmation',
          message: 'Confirm the quoted preview cost.',
          details: {
            estimate,
            claims: [...claims, { field: 'dropped' }],
            promise_set_hash: 'promise-hash-1',
            reason: 'model_cost',
          },
        },
      }, 402),
    ];
    vi.stubGlobal('fetch', vi.fn(async () => responses.shift() as Response));
    const api = createActionPreviewRunsApi('project/one');

    await api.start({ kind: 'map.classify', params: {} });
    const rejection = api.get('quoted');
    await expect(rejection).rejects.toBeInstanceOf(ConfirmationRequiredError);
    await expect(rejection).rejects.toMatchObject({
      name: 'ConfirmationRequiredError',
      message: 'Confirm the quoted preview cost.',
      reason: 'model_cost',
      estimate: { ...estimate, claims, promise_set_hash: 'promise-hash-1' },
    });
  });

  it('maps a lifecycle HTTP 402 preview envelope without details to an empty-quote consent error', async () => {
    const responses = [
      jsonResponse({
        schema_version: 'frisket.action_preview.v1',
        preview_id: 'unquoted',
        total: 1,
      }, 202),
      jsonResponse({
        schema_version: 'frisket.action_preview.v1',
        error: { code: 'preview_not_found', message: 'error-402' },
      }, 402),
    ];
    vi.stubGlobal('fetch', vi.fn(async () => responses.shift() as Response));
    const api = createActionPreviewRunsApi('project/one');

    await api.start({ kind: 'map.classify', params: {} });
    const rejection = api.get('unquoted');
    await expect(rejection).rejects.toBeInstanceOf(ConfirmationRequiredError);
    await expect(rejection).rejects.toMatchObject({
      message: 'error-402',
      reason: null,
      estimate: { cost: null, rows: 0 },
    });
  });
});
