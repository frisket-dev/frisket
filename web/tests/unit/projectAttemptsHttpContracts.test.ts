import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('apache-arrow', () => ({ tableFromIPC: vi.fn() }));
vi.mock('pluralize', () => ({
  default: Object.assign((word: string) => word, {
    addIrregularRule: vi.fn(),
    addUncountableRule: vi.fn(),
  }),
}));


const page = {
  schema_version: 'frisket.attempt_receipts.v1' as const,
  order: 'created_at DESC' as const,
  offset: 0,
  limit: 7,
  total: 1,
  has_more: false,
  next_offset: null,
  run_id: 9,
  attempts: [{
    attempt_id: 'attempt-1',
    run_id: 9,
    seq: 1,
    state: 'effected',
    action_identity_hash: 'identity-hash',
    scope: [1, 2],
    target: {
      target_id: 'openai',
      transport: 'https',
      engine: 'gpt-5-mini',
      operator: 'openai',
      egress_class: 'model',
      region: null,
      credential_source: 'project_key',
    },
    consent: {
      id: 'consent-1',
      grant_basis: 'user_confirmation',
      actor: 'owner@example.com',
      granted_at: '2026-08-13T00:00:00Z',
      promise_set_hash: 'promise-hash',
    },
    cost_basis: { kind: 'priced', nested: ['heterogeneous', 1, true, null] },
    price_card_version: 'terms.v1',
    settlement: {
      price_card_version: 'terms.v1',
      terminal_status: 'completed',
      pricing_key: 'model.tokens',
      unit_rate: '0.1',
      quantity_unit: 'token',
      charge_authority: 'catalog',
      ceiling_mode: 'none',
      row_settlement_mode: 'all_metered',
      metered_quantity: '1',
      metered_unit: 'tokens',
      billable_quantity: '1',
      rated_charge_usd: '0.1',
      charged_quantity: '1',
      charge_usd: '0.1',
      absorbed_overage_usd: '0',
      rated_calls: 1,
      unmetered_calls: 0,
      consented_quantity: null,
      exceeds_consented: null,
    },
    evaluation: { decisions: [{ allowed: true }] },
    borne_by: { credentialed_providers: ['openai'] },
    created_at: '2026-08-13T00:00:00Z',
  }],
};

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('project attempts generated HTTP port', () => {
  it('preserves encoded project path, query bytes, rich receipt leaves, headers, and signal', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return jsonResponse(page);
    }));
    const controller = new AbortController();
    const { createProjectAttemptsApi } = await import('../../src/api/projectAttempts');
    const api = createProjectAttemptsApi((status, payload) =>
      Object.assign(new Error(`mapped-${status}`), { status, payload }),
    );

    await expect(api.listAttemptReceipts('hostile /%25 ☃', 9, 7, {
      signal: controller.signal,
      headers: { Authorization: 'Bearer receipts', 'X-Trace-Id': 'attempts' },
    })).resolves.toEqual(page);
    await api.listAttemptReceipts('hostile /%25 ☃', null, 25);

    expect(requests.map(({ input }) => String(input))).toEqual([
      '/api/projects/hostile%20%2F%2525%20%E2%98%83/actions/attempts?limit=7&run_id=9',
      '/api/projects/hostile%20%2F%2525%20%E2%98%83/actions/attempts?limit=25',
    ]);
    expect(requests[0].init?.method).toBe('GET');
    expect(requests[0].init?.body).toBeUndefined();
    expect(requests[0].init?.signal).toBe(controller.signal);
    const headers = new Headers(requests[0].init?.headers);
    expect(headers.get('authorization')).toBe('Bearer receipts');
    expect(headers.get('x-trace-id')).toBe('attempts');
  });

  it('maps declared errors, preserves network identity, and keeps the RealApi method stable', async () => {
    const { createProjectAttemptsApi } = await import('../../src/api/projectAttempts');
    const api = createProjectAttemptsApi((status, payload) =>
      Object.assign(new Error(`mapped-${status}`), { status, payload }),
    );
    for (const status of [401, 403, 404, 422, 500]) {
      vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ detail: `error-${status}` }, status)));
      await expect(api.listAttemptReceipts('receipts', null, 25)).rejects.toMatchObject({
        message: `mapped-${status}`,
        status,
        payload: { detail: `error-${status}` },
      });
    }

    const network = new Error('network identity');
    vi.stubGlobal('fetch', vi.fn(async () => Promise.reject(network)));
    await expect(api.listAttemptReceipts('receipts')).rejects.toBe(network);
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(page)));
    const real = await import('../../src/api/real');
    const projectApi = real.createProjectApi('hostile /%25 ☃');
    await expect(projectApi.listAttemptReceipts(9, 7)).resolves.toEqual(page);
  });
});
