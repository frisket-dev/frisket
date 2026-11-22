import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('pluralize', () => ({
  default: Object.assign((word: string) => word, {
    addIrregularRule: vi.fn(),
    addUncountableRule: vi.fn(),
  }),
}));

import { createProjectApi } from '../../src/api/real';
import { jsonResponse } from '../support/httpContractTestHelpers';

const realApi = createProjectApi('test-project');

afterEach(() => vi.unstubAllGlobals());

describe('OAuth connection generated HTTP contract', () => {
  it.each([
    [undefined, '/api/organization/oauth/connections'],
    ['', '/api/organization/oauth/connections?provider='],
    ['google/a b?', '/api/organization/oauth/connections?provider=google%2Fa+b%3F'],
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
});
