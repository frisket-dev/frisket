import { afterEach, describe, expect, it, vi } from 'vitest';
import { createProjectApi } from '../api/real';
import type { GeneratedActionRequest } from '../api/types';

afterEach(() => vi.unstubAllGlobals());

function request(): GeneratedActionRequest {
  return { action_id: 'map.api_call', scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [2, 9] },
    params: { request: { method: 'PATCH', url: 'https://example.test/{{customer_id}}',
      headers: [['Authorization', 'Bearer {{secret.TOKEN}}'], ['X:Mode', ' exact ']],
      query_params: [['filter:type', ' query ']], form_body: [['inactive', 'value']],
      cookies: [['session', '{{customer_id}}']], body_mode: 'json',
      body: '{"value":"{{customer_id}}"}', content_type: null, timeout: 30,
      max_requests_per_second: 0.5, follow_redirects: false } },
    output_names: { api_result: 'Response' }, idempotency_key: 'api-call-exact-key' };
}
function transport(status = 'completed') {
  const posted: unknown[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input, init) => {
    expect(String(input)).toBe('/api/projects/request-wire/actions/v1/run');
    posted.push(JSON.parse(init.body));
    return new Response(JSON.stringify({
      schema_version: 'frisket.action_result.v1',
      action: { kind: 'map.api_call', action_id: 'api-invocation' },
      status, run_id: status === 'completed' ? 123 : null, receipt_id: null, outputs: [],
      errors: status === 'needs_confirmation' ? [{
        code: 'confirmation_required', message: 'Confirm the unknown-cost network request.',
        field: 'confirmation', details: { reason: 'network', promise_set_hash: 'host-promise' },
      }] : [],
    }), { status: status === 'needs_confirmation' ? 402 : 200,
      headers: { 'Content-Type': 'application/json' } });
  }));
  return posted;
}

describe('typed API Call HTTP consumer', () => {
  it('sends canonical Params intact without a launcher translator or sheet reread', async () => {
    const posted = transport();
    const original = request();
    await createProjectApi('request-wire').runAction(original);
    expect(posted).toEqual([original]);
  });

  it('preserves sparse Params and explicit nulls without normalizing a second request model', async () => {
    const posted = transport();
    const original = request();
    original.params = { request: { url: 'https://example.test', content_type: null } };
    await createProjectApi('request-wire').runAction(original);
    expect(posted).toEqual([original]);
  });

  it('keeps unknown-cost refusal and retries with host consent outside Params', async () => {
    const original = request();
    const posted = transport('needs_confirmation');
    await expect(createProjectApi('request-wire').runAction(original)).rejects.toThrow();
    expect(posted).toEqual([original]);
    const retryPosted = transport();
    const confirmed = { ...original, confirmation: 'host-promise' };
    await createProjectApi('request-wire').runAction(confirmed);
    expect(retryPosted).toEqual([confirmed]);
    expect(confirmed.params).toEqual(original.params);
    expect(confirmed.idempotency_key).toBe(original.idempotency_key);
  });
});
