import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('pluralize', () => ({
  default: Object.assign((word: string) => word, {
    addIrregularRule: vi.fn(),
    addUncountableRule: vi.fn(),
  }),
}));

import { createActionEstimateValidationApi } from '../../src/api/actionEstimateValidation';
import type { GeneratedActionRequest } from '../../src/api/types';
import { jsonResponse } from '../support/httpContractTestHelpers';

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
    pattern: { ok: false, code: 'invalid_pattern', message: 'unbalanced parenthesis', path: ['expression'], position: 6 },
    template: { ok: true },
  },
  logical_outputs: [{ key: 'extracted', column_type: 'text' }],
};

const estimateRequest: GeneratedActionRequest = {
  action_id: 'map.classify',
  scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [9, 3] },
  params: { model: 'x' },
  output_names: {},
  idempotency_key: 'web-map.classify:test',
};

const validateRequest = {
  action_id: 'map.regex_extract',
  scope: { kind: 'sheet_rows' as const, sheet_id: 7 },
  params: { pattern: 'x' },
};

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
    const api = createActionEstimateValidationApi('project/one');

    const estimate = await api.estimateAction(estimateRequest);
    const validation = await api.resolveActionParams(validateRequest);

    expect(requests.map((request) => request.input)).toEqual([
      '/api/projects/project%2Fone/actions/v1/estimate',
      '/api/projects/project%2Fone/actions/v1/validate-params',
    ]);
    expect(requests.map((request) => request.init?.method)).toEqual(['POST', 'POST']);
    expect(requests.map((request) => JSON.parse(String(request.init?.body)))).toEqual([
      { action: estimateRequest },
      { action: validateRequest },
    ]);
    for (const request of requests) {
      expect([...new Headers(request.init?.headers).entries()]).toEqual([
        ['content-type', 'application/json'],
      ]);
    }
    expect(validation.diagnostics.template).toEqual({ ok: true });
    expect(estimate).toEqual({
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

  it.each([
    ['carries its message', 'backfill is running', 'backfill is running'],
    ['falls back to the HTTP status for a blank message', '', 'Request failed (HTTP 409)'],
  ])('maps a bare v1 ActionError that %s', async (_label, message, expectedMessage) => {
    const payload = {
      schema_version: 'frisket.action_error.v1',
      code: 'backfill_in_progress',
      message,
      details: { sheet_id: 7 },
    };
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(payload, 409)));
    const api = createActionEstimateValidationApi('project-1');

    for (const request of [
      api.estimateAction(estimateRequest),
      api.resolveActionParams(validateRequest),
    ]) {
      await expect(request).rejects.toMatchObject({
        name: 'ApiError',
        status: 409,
        message: expectedMessage,
        code: 'backfill_in_progress',
        details: { sheet_id: 7 },
      });
    }
  });

  it('posts a registered action estimate directly', async () => {
    const requests: unknown[] = [];
    vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      requests.push(JSON.parse(String(init?.body)));
      return jsonResponse(estimateWire);
    }));
    const api = createActionEstimateValidationApi('project/one');
    const request: GeneratedActionRequest = {
      action_id: 'map.ask',
      scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [9, 3] },
      params: { source: ['body'], model: 'openai/gpt-5-mini', question: 'What changed?' },
      output_names: { answer: 'finding' },
      idempotency_key: 'web-map.ask:test',
    };

    await api.estimateAction(request);

    expect(requests).toEqual([{ action: request }]);
  });

  it('resolves dynamic outputs from the registered action request', async () => {
    const fetch = vi.fn<typeof globalThis.fetch>(async () => jsonResponse(validationWire));
    vi.stubGlobal('fetch', fetch);
    const api = createActionEstimateValidationApi('project/one');

    await expect(api.resolveActionParams({
      action_id: 'map.regex_extract',
      scope: { kind: 'sheet_rows', sheet_id: 7 },
      params: { input_columns: ['source'], pattern: '.+' },
    })).resolves.toEqual({
      diagnostics: {
        pattern: {
          ok: false, code: 'invalid_pattern', message: 'unbalanced parenthesis', path: ['expression'], position: 6,
        },
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
    const api = createActionEstimateValidationApi('project/one');
    await expect(api.resolveActionParams({
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

  it('carries runtime setup beside valid outputs instead of dropping it at the domain projection', async () => {
    const setup = { kind: 'engine_setup', scope: 'workspace', setup_ref: 'engine-setup:chromium.local@1',
      can_start: true, can_mutate: true, blocked_by_operation: null };
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ ...validationWire,
      diagnostics: {}, runtime_setup: setup, logical_outputs: [{ key: 'screenshot', column_type: 'file' }],
    })));
    const api = createActionEstimateValidationApi('project/one');
    await expect(api.resolveActionParams({ action_id: 'web.capture_screenshot',
      scope: { kind: 'sheet_rows', sheet_id: 7 }, params: { source: 'url' },
    })).resolves.toEqual({ diagnostics: {}, runtime_setup: setup,
      logical_outputs: [{ key: 'screenshot', column_type: 'file' }] });
  });

});
