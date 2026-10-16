import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('apache-arrow', () => ({ tableFromIPC: vi.fn() }));
vi.mock('pluralize', () => ({
  default: Object.assign((word: string) => word, {
    addIrregularRule: vi.fn(),
    addUncountableRule: vi.fn(),
  }),
}));

import {
  createProjectSearchApi,
  type ProjectSearchRequestWire,
} from '../../src/api/projectSearch';

const request: ProjectSearchRequestWire = {
  scope: { kind: 'project' },
  query: { kind: 'text', text: 'a+b &/☃', lexical_syntax: 'fts' },
  mode: 'hybrid',
  group_by: 'source',
  limit: 50,
  ranking: { rerank: 'on' },
};

const response = {
  groups: [],
  coverage: {
    requested_mode: 'hybrid',
    effective_modes: ['lexical'],
    state: 'partial',
    searched_channels: ['text'],
    channels: [],
    is_top_k: true,
    truncated: false,
  },
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
  it('posts the typed request to the encoded project URL with caller headers and cancellation', async () => {
    const calls: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ input, init });
      return jsonResponse(response);
    }));
    const controller = new AbortController();
    const api = createProjectSearchApi(
      (status, payload) => new Error(`${status}:${String(payload)}`),
      'hostile /%25 ☃',
    );

    await expect(api.searchProject(request, {
      signal: controller.signal,
      headers: { Authorization: 'Bearer search', 'X-Trace-Id': 'search-contract' },
    })).resolves.toEqual(response);

    expect(String(calls[0].input)).toBe(
      '/api/projects/hostile%20%2F%2525%20%E2%98%83/search',
    );
    expect(calls[0].init?.method).toBe('POST');
    expect(JSON.parse(String(calls[0].init?.body))).toEqual(request);
    expect(calls[0].init?.signal).toBe(controller.signal);
    const headers = new Headers(calls[0].init?.headers);
    expect(headers.get('content-type')).toContain('application/json');
    expect(headers.get('authorization')).toBe('Bearer search');
    expect(headers.get('x-trace-id')).toBe('search-contract');
  });

  it('keeps generic error mapping, network rejection identity, and RealApi compatibility', async () => {
    const api = createProjectSearchApi(
      (status, payload) => Object.assign(new Error(`mapped-${status}`), { status, payload }),
      'project-1',
    );
    for (const status of [401, 403, 404, 422, 500]) {
      vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ detail: `error-${status}` }, status)));
      await expect(api.searchProject(request)).rejects.toMatchObject({
        message: `mapped-${status}`,
        status,
        payload: { detail: `error-${status}` },
      });
    }

    const network = new Error('network identity');
    vi.stubGlobal('fetch', vi.fn(async () => Promise.reject(network)));
    await expect(api.searchProject(request)).rejects.toBe(network);

    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(response)));
    const real = await import('../../src/api/real');
    await expect(real.searchProject('project-1', request)).resolves.toEqual(response);

    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ detail: 'unavailable' }, 500)));
    await expect(real.searchProject('project-1', request)).rejects.toMatchObject({
      name: 'ApiError',
      status: 500,
      message: 'unavailable',
      code: undefined,
      details: undefined,
    });
  });
});
