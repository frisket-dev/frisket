import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('apache-arrow', () => ({ tableFromIPC: vi.fn() }));
vi.mock('pluralize', () => ({
  default: Object.assign((word: string) => word, {
    addIrregularRule: vi.fn(),
    addUncountableRule: vi.fn(),
  }),
}));

import { ApiError, createProjectApi } from '../../src/api/real';

const realApi = createProjectApi('test-project');

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

afterEach(() => vi.unstubAllGlobals());

describe('OAuth connection generated HTTP contract', () => {
  it.each([
    [undefined, '/api/org/oauth/connections'],
    ['', '/api/org/oauth/connections?provider='],
    ['google/a b?', '/api/org/oauth/connections?provider=google%2Fa+b%3F'],
  ])('preserves provider query bytes for %p', async (provider, path) => {
    const calls: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        calls.push({ input, init });
        return jsonResponse({ connections: [{ id: 'google_1', provider: 'google' }] });
      }),
    );

    await expect(realApi.listOAuthConnections(provider)).resolves.toEqual([
      { id: 'google_1', provider: 'google' },
    ]);
    expect(calls).toEqual([{ input: path, init: { method: 'GET' } }]);
  });

  it('keeps the generic ApiError behavior for browser-session failures', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse({ detail: 'browser session required' }, 403),
      ),
    );
    await expect(realApi.listOAuthConnections()).rejects.toMatchObject({
      name: ApiError.name,
      status: 403,
      message: 'browser session required',
      code: undefined,
      details: undefined,
    });
  });
});
