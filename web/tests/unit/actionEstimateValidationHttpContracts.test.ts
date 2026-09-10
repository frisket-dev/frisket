import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('apache-arrow', () => ({ tableFromIPC: vi.fn() }));
vi.mock('pluralize', () => ({
  default: Object.assign((word: string) => word, {
    addIrregularRule: vi.fn(),
    addUncountableRule: vi.fn(),
  }),
}));

import {
  createActionEstimateValidationDomainApi,
  createActionEstimateValidationApi,
  runEstimateFromV1Wire,
} from '../../src/api/actionEstimateValidation';
import type { GeneratedActionRequest } from '../../src/api/types';

const estimateWire = {
  schema_version: 'frisket.action_estimate_result.v1' as const,
  action: { kind: 'map.classify' },
  project_id: 'project/one',
  estimate: {
    rows: 3,
    cost: 0.125,
    pricing_key: 'openai.gpt-4o-mini.token',
    engine: 'openai/gpt-4o-mini',
    remote_capability: 'model:complete',
    requires_confirmation: true,
    avg_input_tokens: 42,
    audio_seconds: 10.5,
    billing_label: 'Frisket credits',
    venue_label: 'Hosted provider',
    cost_source: 'estimated',
    warning: 'sampled quote',
    billed_cost: 250000,
    policy_id: 'tenant-v1',
    claims: [{ field: 'network', display: 'Sends text to the provider' }],
    promise_set_hash: 'promise-set',
  },
};

const validationWire = {
  schema_version: 'frisket.action_param_validation_result.v1' as const,
  action: { kind: 'map.regex_extract' },
  project_id: 'project/one',
  diagnostics: {
    pattern: { ok: false, message: 'unbalanced parenthesis', position: 6 },
    template: { ok: true },
  },
  logical_outputs: [{ key: 'extracted', column_type: 'text' }],
};

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
    super(`mapped preflight error ${status}`);
    this.name = 'MappedContractError';
    this.status = status;
    this.payload = payload;
  }
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('action estimate and parameter-validation HTTP contracts', () => {
  it('preserves wrapper, path, query, and domain facts for both browser POSTs', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    const responses = [estimateWire, validationWire];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return jsonResponse(responses.shift());
    }));
    const signal = new AbortController().signal;
    const api = createActionEstimateValidationApi(
      (status, payload) => new MappedContractError(status, payload),
      'project/one',
    );

    const estimate = await api.estimate(
      { kind: 'map.classify', params: { model: 'x' } },
      { signal, headers: { Authorization: 'Bearer preflight' } },
    );
    const validation = await api.validateParams(
      { kind: 'map.regex_extract', params: { pattern: 'x' } },
      { signal, headers: { Authorization: 'Bearer preflight' } },
    );

    expect(requests.map((request) => request.input)).toEqual([
      '/api/projects/project%2Fone/actions/v1/estimate',
      '/api/projects/project%2Fone/actions/v1/validate-params',
    ]);
    expect(requests.map((request) => request.init?.method)).toEqual(['POST', 'POST']);
    expect(requests.map((request) => JSON.parse(String(request.init?.body)))).toEqual([
      { action: { kind: 'map.classify', params: { model: 'x' } } },
      { action: { kind: 'map.regex_extract', params: { pattern: 'x' } } },
    ]);
    for (const request of requests) {
      expect(request.init?.signal).toBe(signal);
      expect(new Headers(request.init?.headers).get('authorization')).toBe('Bearer preflight');
      expect(new Headers(request.init?.headers).get('content-type')).toBe('application/json');
    }
    expect(validation.diagnostics.template).toEqual({ ok: true });
    expect(runEstimateFromV1Wire(estimate.estimate)).toEqual({
      rows: 3,
      cost: 0.125,
      pricing_key: 'openai.gpt-4o-mini.token',
      engine: 'openai/gpt-4o-mini',
      remote_capability: 'model:complete',
      requires_confirmation: true,
      avg_input_tokens: 42,
      audio_seconds: 10.5,
      billing_label: 'Frisket credits',
      venue_label: 'Hosted provider',
      cost_source: 'estimated',
      warning: 'sampled quote',
      billed_cost: 250000,
      policy_id: 'tenant-v1',
      claims: [{ field: 'network', display: 'Sends text to the provider' }],
      promise_set_hash: 'promise-set',
    });
  });

  it.each([400, 401, 403, 404, 422, 500, 418])(
    'maps HTTP %i through the provided error factory',
    async (status) => {
      const errorFactory = vi.fn(
        (actualStatus: number, payload: unknown) =>
          new MappedContractError(actualStatus, payload),
      );
      vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ detail: `error-${status}` }, status)));
      const api = createActionEstimateValidationApi(errorFactory, 'project-1');

      await expect(api.estimate({ kind: 'map.classify' })).rejects.toMatchObject({
        name: 'MappedContractError',
        status,
        payload: { detail: `error-${status}` },
      });
      expect(errorFactory).toHaveBeenCalledWith(status, { detail: `error-${status}` });
    },
  );

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
    const api = createActionEstimateValidationApi(
      (status, payload) => new MappedContractError(status, payload),
      'project-1',
    );
    const pending = api.validateParams({ kind: 'map.regex_extract' }, {
      signal: controller.signal,
    });
    const abortError = new DOMException('preflight cancelled', 'AbortError');
    controller.abort(abortError);

    await expect(pending).rejects.toBe(abortError);
    expect(forwardedSignal).toBe(controller.signal);
  });

  it('posts a registered action estimate directly', async () => {
    const requests: unknown[] = [];
    vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      requests.push(JSON.parse(String(init?.body)));
      return jsonResponse(estimateWire);
    }));
    const api = createActionEstimateValidationDomainApi({
      errorFactory: (status, payload) => new MappedContractError(status, payload),
      projectId: 'project/one',
    });
    const request: GeneratedActionRequest = {
      action_id: 'map.ask',
      scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [9, 3] },
      params: { source: ['body'], model: 'openai/gpt-5-mini', question: 'What changed?' },
      output_names: { answer: 'finding' },
      idempotency_key: 'web-map.ask:test',
    };

    await api.estimate(request);

    expect(requests).toEqual([{ action: request }]);
  });

  it('resolves dynamic outputs from the registered action request', async () => {
    const fetch = vi.fn(async () => jsonResponse(validationWire));
    vi.stubGlobal('fetch', fetch);
    const api = createActionEstimateValidationDomainApi({
      errorFactory: (status, payload) => new MappedContractError(status, payload),
      projectId: 'project/one',
    });

    await expect(api.resolveParams({
      action_id: 'map.regex_extract',
      scope: { kind: 'sheet_rows', sheet_id: 7 },
      params: { input_columns: ['source'], pattern: '.+' },
    })).resolves.toEqual({
      diagnostics: {
        pattern: { ok: false, message: 'unbalanced parenthesis', position: 6 },
        template: { ok: true },
      },
      logical_outputs: [{ key: 'extracted', column_type: 'text' }],
    });
    expect(JSON.parse(String(fetch.mock.calls[0]?.[1]?.body))).toEqual({
      action: {
        action_id: 'map.regex_extract',
        scope: { kind: 'sheet_rows', sheet_id: 7 },
        params: { input_columns: ['source'], pattern: '.+' },
      },
    });
  });

  it.each([true, false, null])('preserves prepared destination and output policy (%s)', async (createsSheet) => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({
      ...validationWire, diagnostics: {}, creates_sheet: createsSheet,
      logical_outputs: [
        { key: 'page', column_type: 'file', existing_column_policy: 'compatible' },
        { key: 'ordinary', column_type: 'text', existing_column_policy: null },
      ],
    })));
    const api = createActionEstimateValidationDomainApi({
      errorFactory: (status, payload) => new MappedContractError(status, payload),
      projectId: 'project/one',
    });
    await expect(api.resolveParams({
      action_id: 'web.capture_page', scope: { kind: 'sheet_rows', sheet_id: 7 },
      params: { source: 'url' },
    })).resolves.toEqual({
      diagnostics: {}, ...(createsSheet === null ? {} : { creates_sheet: createsSheet }),
      logical_outputs: [
        { key: 'page', column_type: 'file', existing_column_policy: 'compatible' },
        { key: 'ordinary', column_type: 'text' },
      ],
    });
  });

});
