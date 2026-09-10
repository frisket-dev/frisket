// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';

function jsonResponse(payload: unknown, status = 202): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

const flushPromises = async (): Promise<void> => {
  await new Promise((resolve) => setTimeout(resolve, 0));
};

describe('client error capture', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.resetModules();
    delete window.__FRISKET_CLIENT_ERROR_CAPTURE__;
    delete window.__FRISKET_RECENT_CLIENT_ERROR_IDS__;
  });

  it('reports once through the generated client-error operation, remembers accepted IDs, and swallows failures', async () => {
    window.__FRISKET_CLIENT_ERROR_CAPTURE__ = true;
    const fetch = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ ok: true, id: 47 }))
      .mockRejectedValueOnce(new Error('offline'));
    vi.stubGlobal('fetch', fetch);

    await import('../../src/entries/mount');

    window.dispatchEvent(new ErrorEvent('error', {
      message: 'first browser error',
    }));
    await flushPromises();

    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch).toHaveBeenCalledWith('/api/client-errors', expect.objectContaining({
      method: 'POST',
      credentials: 'same-origin',
      keepalive: true,
    }));
    expect(window.__FRISKET_RECENT_CLIENT_ERROR_IDS__).toEqual([47]);

    expect(() => window.dispatchEvent(new ErrorEvent('error', {
      message: 'second browser error',
    }))).not.toThrow();
    await flushPromises();

    expect(fetch).toHaveBeenCalledTimes(2);
    expect(window.__FRISKET_RECENT_CLIENT_ERROR_IDS__).toEqual([47]);
  });
});
