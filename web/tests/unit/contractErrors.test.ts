import { describe, expect, it } from 'vitest';

import {
  ApiError,
  ConfirmationRequiredError,
  errorFromContract,
} from '../../src/api/contractErrors';

const claims = [
  { field: 'model', display: 'gpt-4o-mini' },
  { field: 'rows', display: '12 rows' },
];

function confirmation(status: number, payload: unknown): ConfirmationRequiredError {
  const error = errorFromContract(status, payload);
  expect(error).toBeInstanceOf(ConfirmationRequiredError);
  return error as ConfirmationRequiredError;
}

describe('errorFromContract cost gate', () => {
  it('carries every action-result 402 fact by value', () => {
    const error = confirmation(402, {
      schema_version: 'frisket.action_result.v1',
      status: 'needs_confirmation',
      errors: [{
        code: 'needs_confirmation',
        field: 'params.confirmed',
        message: 'Confirm $0.42 for 12 rows',
        details: {
          estimate: { cost: 0.42, rows: 12, pricing_key: 'openai:gpt-4o-mini' },
          claims: [...claims, { field: 'dropped' }, 'not-a-claim'],
          promise_set_hash: 'sha256:promise',
          reason: 'model_cost',
        },
      }],
    });

    expect(error.name).toBe('ConfirmationRequiredError');
    expect(error.message).toBe('Confirm $0.42 for 12 rows');
    expect(error.reason).toBe('model_cost');
    expect(error.estimate).toEqual({
      cost: 0.42,
      rows: 12,
      pricing_key: 'openai:gpt-4o-mini',
      claims,
      promise_set_hash: 'sha256:promise',
    });
  });

  it('reads the action-result estimate from the confirm error detail and row-count gates', () => {
    expect(confirmation(402, {
      schema_version: 'frisket.action_result.v1',
      status: 'needs_confirmation',
      errors: [{ field: 'params.confirmed', message: 'Confirm', detail: { estimate: 0.5 } }],
    }).estimate).toEqual({ cost: 0.5, rows: 0 });

    expect(confirmation(402, {
      schema_version: 'frisket.action_result.v1',
      status: 'needs_confirmation',
      detail: { estimate: { cost: 1.25, rows: 3 } },
      errors: [{ field: 'params.confirmed', message: 'Confirm' }],
    }).estimate).toEqual({ cost: 1.25, rows: 3 });

    const rowsGate = confirmation(402, {
      schema_version: 'frisket.action_result.v1',
      status: 'needs_confirmation',
      errors: [{ field: 'params.confirmed', message: 'Fan-out', details: { estimated_rows: 9000, reason: 'destructive' } }],
    });
    expect(rowsGate.estimate).toEqual({ cost: null, rows: 9000 });
    expect(rowsGate.reason).toBe('destructive');
  });

  it('carries every preview-envelope 402 fact by value', () => {
    const error = confirmation(402, {
      schema_version: 'frisket.action_preview.v1',
      error: {
        schema_version: 'frisket.action_error.v1',
        code: 'needs_confirmation',
        message: 'Preview costs $0.10',
        details: {
          estimate: { cost: 0.1, rows: 5 },
          claims,
          promise_set_hash: 'sha256:preview',
          reason: 'remote_egress',
        },
      },
    });

    expect(error.message).toBe('Preview costs $0.10');
    expect(error.reason).toBe('remote_egress');
    expect(error.estimate).toEqual({
      cost: 0.1,
      rows: 5,
      claims,
      promise_set_hash: 'sha256:preview',
    });
  });
});

describe('errorFromContract envelope shapes', () => {
  it.each([
    ['plain-string detail', 404, { detail: 'project not found' },
      { status: 404, message: 'project not found', code: undefined, details: undefined }],
    ['coded detail with extras', 400, { detail: { code: 'missing_provider_key', message: 'Add a key', provider: 'openai' } },
      { status: 400, message: 'Add a key', code: 'missing_provider_key', details: { provider: 'openai' } }],
    ['coded detail without extras', 409, { detail: { code: 'conflict', message: 'Busy' } },
      { status: 409, message: 'Busy', code: 'conflict', details: undefined }],
    ['non-message detail', 422, { detail: [{ loc: ['body'], msg: 'bad' }] },
      { status: 422, message: '[{"loc":["body"],"msg":"bad"}]', code: undefined, details: undefined }],
    ['bare action error', 400, { schema_version: 'frisket.action_error.v1', code: 'invalid_param', message: 'Bad param', details: { field: 'x' } },
      { status: 400, message: 'Bad param', code: 'invalid_param', details: { field: 'x' } }],
    ['camel-cased bare action error', 404, { schemaVersion: 'frisket.action_error.v1', code: 'cell_not_found', message: 'Missing' },
      { status: 404, message: 'Missing', code: 'cell_not_found', details: undefined }],
    ['preview envelope', 404, { error: { code: 'preview_not_found', message: 'Gone' } },
      { status: 404, message: 'Gone', code: 'preview_not_found', details: undefined }],
    ['action-result refusal', 409, { schema_version: 'frisket.action_result.v1', status: 'failed', errors: [{ code: 'stale', message: 'Stale', details: { op: 3 } }] },
      { status: 409, message: 'Stale', code: 'stale', details: { op: 3 } }],
    ['top-level message', 500, { message: 'internal' },
      { status: 500, message: 'internal', code: undefined, details: undefined }],
    ['empty object', 503, {},
      { status: 503, message: 'Request failed (HTTP 503)', code: undefined, details: undefined }],
    ['non-object payload', 502, 'Bad Gateway',
      { status: 502, message: 'Request failed (HTTP 502)', code: undefined, details: undefined }],
  ])('maps a %s', (_label, status, payload, expected) => {
    const error = errorFromContract(status, payload);
    expect(error).toBeInstanceOf(ApiError);
    expect({
      status: (error as ApiError).status,
      message: error.message,
      code: (error as ApiError).code,
      details: (error as ApiError).details,
    }).toEqual(expected);
  });
});
