import { afterEach, describe, expect, it, vi } from 'vitest';
import { jsonResponse } from '../support/httpContractTestHelpers';

vi.mock('pluralize', () => ({
  default: Object.assign((word: string) => word, {
    addIrregularRule: vi.fn(),
    addUncountableRule: vi.fn(),
  }),
}));

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
    const options = { signal: controller.signal };
    const { createOrganizationOperationsApi } = await import('../../src/api/organizationOperations');
    const api = createOrganizationOperationsApi();

    await expect(api.listOrganizationEnvVars(options)).resolves.toEqual([
      { name: 'PUBLIC_ONLY', hint: '...only' },
      { name: 'HOSTED', hint: '...sted', created_at: '2026-08-13T00:00:00Z' },
    ]);
    await expect(api.setOrganizationEnvVar('CLIENT_SECRET', 'secret', options)).resolves.toBeUndefined();
    await expect(api.deleteOrganizationEnvVar('CLIENT/SECRET', options)).resolves.toBeUndefined();
    await expect(api.getMediaProxyStatus(options)).resolves.toEqual({
      configured: true,
      connected: false,
      canConfigure: true,
    });

    expect(requests.map(({ input, init }) => [String(input), init?.method])).toEqual([
      ['/api/organization/env', 'GET'],
      ['/api/organization/env', 'POST'],
      ['/api/organization/env/CLIENT%2FSECRET', 'DELETE'],
      ['/api/organization/media-proxy/status', 'GET'],
    ]);
    expect(requests.map(({ init }) => init?.body ?? null)).toEqual([
      null,
      JSON.stringify({ name: 'CLIENT_SECRET', value: 'secret' }),
      null,
      null,
    ]);
    for (const { init } of requests) {
      expect(init?.signal).toBe(controller.signal);
    }
  });

  it('maps HTTP errors, preserves network identity, and keeps real.ts mutations void', async () => {
    const { createOrganizationOperationsApi } = await import('../../src/api/organizationOperations');
    const api = createOrganizationOperationsApi();
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ detail: 'forbidden' }, 403)));
    await expect(api.listOrganizationEnvVars()).rejects.toMatchObject({
      name: 'ApiError',
      status: 403,
      message: 'forbidden',
      code: undefined,
      details: undefined,
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
    await expect(real.setOrganizationEnvVar('CLIENT_SECRET', 'secret')).resolves.toBeUndefined();
    await expect(real.deleteOrganizationEnvVar('CLIENT_SECRET')).resolves.toBeUndefined();
    await expect(real.getMediaProxyStatus()).resolves.toEqual({
      configured: false,
      connected: null,
      canConfigure: false,
    });
  });
});
