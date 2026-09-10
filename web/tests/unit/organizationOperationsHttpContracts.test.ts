import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('apache-arrow', () => ({ tableFromIPC: vi.fn() }));
vi.mock('pluralize', () => ({
  default: Object.assign((word: string) => word, {
    addIrregularRule: vi.fn(),
    addUncountableRule: vi.fn(),
  }),
}));

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('organization operations generated HTTP port', () => {
  it('preserves exact paths and bodies, additive omissions, void mutations, and media mapping', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    const responses = [
      jsonResponse([
        { name: 'PUBLIC_ONLY', hint: '...only' },
        { name: 'HOSTED', hint: '...sted', created_at: '2026-08-13T00:00:00Z' },
      ]),
      jsonResponse({ name: 'CLIENT_SECRET', hint: '...cret' }),
      jsonResponse({ ok: true, deleted: true }),
      jsonResponse({ configured: true, connected: false, can_configure: true }),
    ];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return responses.shift()!;
    }));
    const controller = new AbortController();
    const options = {
      signal: controller.signal,
      headers: { Authorization: 'Bearer org', 'X-Trace-Id': 'org-operations' },
    };
    const { createOrganizationOperationsApi } = await import('../../src/api/organizationOperations');
    const api = createOrganizationOperationsApi((status, payload) =>
      Object.assign(new Error(`mapped-${status}`), { status, payload }),
    );

    await expect(api.listOrgEnvVars(options)).resolves.toEqual([
      { name: 'PUBLIC_ONLY', hint: '...only' },
      { name: 'HOSTED', hint: '...sted', created_at: '2026-08-13T00:00:00Z' },
    ]);
    await expect(api.setOrgEnvVar('CLIENT_SECRET', 'secret', options)).resolves.toBeUndefined();
    await expect(api.deleteOrgEnvVar('CLIENT/SECRET', options)).resolves.toBeUndefined();
    await expect(api.getMediaProxyStatus(options)).resolves.toEqual({
      configured: true,
      connected: false,
      canConfigure: true,
    });

    expect(requests.map(({ input, init }) => [String(input), init?.method])).toEqual([
      ['/api/org/env', 'GET'],
      ['/api/org/env', 'POST'],
      ['/api/org/env/CLIENT%2FSECRET', 'DELETE'],
      ['/api/org/media-proxy/status', 'GET'],
    ]);
    expect(requests.map(({ init }) => init?.body ?? null)).toEqual([
      null,
      JSON.stringify({ name: 'CLIENT_SECRET', value: 'secret' }),
      null,
      null,
    ]);
    for (const { init } of requests) {
      expect(init?.signal).toBe(controller.signal);
      const headers = new Headers(init?.headers);
      expect(headers.get('authorization')).toBe('Bearer org');
      expect(headers.get('x-trace-id')).toBe('org-operations');
    }
  });

  it('maps HTTP errors, preserves network identity, and keeps real.ts mutations void', async () => {
    const { createOrganizationOperationsApi } = await import('../../src/api/organizationOperations');
    const api = createOrganizationOperationsApi((status, payload) =>
      Object.assign(new Error(`mapped-${status}`), { status, payload }),
    );
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ detail: 'forbidden' }, 403)));
    await expect(api.listOrgEnvVars()).rejects.toMatchObject({
      message: 'mapped-403',
      status: 403,
      payload: { detail: 'forbidden' },
    });

    const network = new Error('network identity');
    vi.stubGlobal('fetch', vi.fn(async () => Promise.reject(network)));
    await expect(api.getMediaProxyStatus()).rejects.toBe(network);

    const responses = [
      jsonResponse({ name: 'CLIENT_SECRET', hint: '...cret' }),
      jsonResponse({ ok: true, deleted: true }),
      jsonResponse({ configured: false, connected: null, can_configure: false }),
    ];
    vi.stubGlobal('fetch', vi.fn(async () => responses.shift()!));
    const real = await import('../../src/api/real');
    await expect(real.setOrgEnvVar('CLIENT_SECRET', 'secret')).resolves.toBeUndefined();
    await expect(real.deleteOrgEnvVar('CLIENT_SECRET')).resolves.toBeUndefined();
    await expect(real.getMediaProxyStatus()).resolves.toEqual({
      configured: false,
      connected: null,
      canConfigure: false,
    });
  });
});
