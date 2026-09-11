import { afterEach, describe, expect, it, vi } from 'vitest';

import { listWalkthroughs } from '../../src/api/walkthroughs';

afterEach(() => vi.unstubAllGlobals());

describe('walkthrough generated HTTP contract', () => {
  it('fetches the backend-authored catalog without request data', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return new Response(JSON.stringify({
        walkthroughs: [{ id: 'regex-extract', badges: ['Regex', 'Joins'] }],
      }));
    }));

    await expect(listWalkthroughs()).resolves.toEqual({
      walkthroughs: [{ id: 'regex-extract', badges: ['Regex', 'Joins'] }],
    });
    expect(requests).toEqual([
      { input: '/api/walkthroughs', init: { method: 'GET' } },
    ]);
  });
});
