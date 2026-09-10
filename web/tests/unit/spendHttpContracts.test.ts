import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('apache-arrow', () => ({ tableFromIPC: vi.fn() }));
vi.mock('pluralize', () => ({
  default: Object.assign((word: string) => word, {
    addIrregularRule: vi.fn(),
    addUncountableRule: vi.fn(),
  }),
}));

import { ApiError, getSpend } from '../../src/api/real';
import { createSpendApi } from '../../src/api/spend';

const report = {
  rows: [{
    project: 'project',
    model: null,
    month: null,
    runs: 2,
    rows: 3,
    cost: 0.123456789,
  }],
  total_cost: 0.123456789,
  has_unknown_costs: true,
  unknown_cost_models: ['unpriced'],
};

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

afterEach(() => vi.unstubAllGlobals());

describe('spend generated HTTP contract', () => {
  it('preserves report floats and forwards options without a request body', async () => {
    const controller = new AbortController();
    const fetch = vi.fn(async () => jsonResponse(report));
    vi.stubGlobal('fetch', fetch);
    const api = createSpendApi((status, payload) =>
      Object.assign(new Error(`mapped-${status}`), { status, payload }),
    );

    await expect(api.getSpend({
      signal: controller.signal,
      headers: { Authorization: 'Bearer spend' },
    })).resolves.toEqual(report);
    expect(fetch).toHaveBeenCalledWith('/api/spend', {
      method: 'GET',
      signal: controller.signal,
      headers: { Authorization: 'Bearer spend' },
    });
  });

  it('keeps ApiError and network identity through the real delegate', async () => {
    vi.stubGlobal('fetch', vi.fn(async () =>
      jsonResponse({ detail: 'only an org owner or admin can view org spend' }, 403),
    ));
    await expect(getSpend()).rejects.toMatchObject({
      name: ApiError.name,
      status: 403,
      message: 'only an org owner or admin can view org spend',
      code: undefined,
      details: undefined,
    });

    const network = new Error('network identity');
    vi.stubGlobal('fetch', vi.fn(async () => Promise.reject(network)));
    await expect(getSpend()).rejects.toBe(network);
  });
});
