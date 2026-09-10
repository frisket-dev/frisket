import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('apache-arrow', () => ({ tableFromIPC: vi.fn() }));
vi.mock('pluralize', () => ({
  default: Object.assign((word: string) => word, {
    addIrregularRule: vi.fn(),
    addUncountableRule: vi.fn(),
  }),
}));

import { createProjectSearchApi } from '../../src/api/projectSearch';

const hit = {
  sheet_id: 3,
  row_id: 7,
  column_id: 11,
  column_name: 'body',
  ai_generated: false,
  snip: '<b>budget</b>',
  score: 0.9,
  semantic: true,
  rerank_score: 0.8,
  producer_extension: { preserved: true },
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

describe('project-search generated HTTP contract', () => {
  it('sends exact browser URL bytes, defaults, overloads, headers, and abort signal', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return jsonResponse([hit]);
    }));
    const controller = new AbortController();
    const api = createProjectSearchApi(
      (status, payload) => new Error(`${status}:${String(payload)}`),
      'hostile /%25 ☃',
    );
    await expect(api.searchProject('a+b &/☃')).resolves.toEqual([hit]);
    await api.searchProject('numeric', 7);
    await api.searchProject('loose', {
      mode: 'lexical',
      rerank: 'rerank-variant',
      signal: controller.signal,
      headers: { Authorization: 'Bearer search', 'X-Trace-Id': 'search-contract' },
    });

    expect(requests.map(({ input }) => String(input))).toEqual([
      '/api/projects/hostile%20%2F%2525%20%E2%98%83/search?q=a%2Bb+%26%2F%E2%98%83&limit=50&mode=keyword&rerank=off',
      '/api/projects/hostile%20%2F%2525%20%E2%98%83/search?q=numeric&limit=7&mode=keyword&rerank=off',
      '/api/projects/hostile%20%2F%2525%20%E2%98%83/search?q=loose&limit=50&mode=lexical&rerank=rerank-variant',
    ]);
    for (const request of requests) {
      expect(request.init?.method).toBe('GET');
      expect(request.init?.body).toBeUndefined();
      expect(new Headers(request.init?.headers).get('content-type')).toBeNull();
    }
    expect(requests[2].init?.signal).toBe(controller.signal);
    const headers = new Headers(requests[2].init?.headers);
    expect(headers.get('authorization')).toBe('Bearer search');
    expect(headers.get('x-trace-id')).toBe('search-contract');
  });

  it('keeps generic error mapping, network rejection identity, and RealApi compatibility', async () => {
    const api = createProjectSearchApi(
      (status, payload) => Object.assign(new Error(`mapped-${status}`), { status, payload }),
      'hostile /%25 ☃',
    );
    for (const status of [401, 403, 404, 422, 500]) {
      vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ detail: `error-${status}` }, status)));
      await expect(api.searchProject('budget')).rejects.toMatchObject({
        message: `mapped-${status}`,
        status,
        payload: { detail: `error-${status}` },
      });
    }

    const network = new Error('network identity');
    vi.stubGlobal('fetch', vi.fn(async () => Promise.reject(network)));
    await expect(api.searchProject('budget')).rejects.toBe(network);

    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse([hit])));
    const real = await import('../../src/api/real');
    await expect(real.searchProject('hostile /%25 ☃', 'budget', {
      mode: 'semantic', rerank: true,
    })).resolves.toEqual([hit]);

    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ detail: 'unavailable' }, 500)));
    await expect(real.searchProject('hostile /%25 ☃', 'budget')).rejects.toMatchObject({
      name: 'ApiError',
      status: 500,
      message: 'unavailable',
      code: undefined,
      details: undefined,
    });
  });
});
