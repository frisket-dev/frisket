import { afterEach, describe, expect, it, vi } from 'vitest';

import { requestMagicLink, signOut } from '../../src/api/browserAuth';

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe('generated browser-auth HTTP contracts', () => {
  it('trims a typed request-link body and preserves the legacy status error', async () => {
    const fetch = vi.fn(async () => new Response(null, { status: 502 }));
    vi.stubGlobal('fetch', fetch);

    await expect(requestMagicLink('  person@example.test  ')).rejects.toThrow(
      'server returned 502',
    );
    const [path, init] = fetch.mock.calls[0] ?? [];
    expect(path).toBe('/auth/request-link');
    expect(init).toMatchObject({
      method: 'POST',
      body: JSON.stringify({ email: 'person@example.test' }),
    });
    expect(new Headers(init?.headers).get('Content-Type')).toBe('application/json');
  });

  it('consumes the typed logout response but preserves best-effort logout', async () => {
    const fetch = vi.fn(async () => new Response('{"detail":"unexpected"}', { status: 500 }));
    vi.stubGlobal('fetch', fetch);
    localStorage.setItem('frisket:product-telemetry:v1', JSON.stringify({
      schema_version: 1,
      state: 'enabled',
      utc_month: '2026-09',
      monthly_id: 'a'.repeat(64),
    }));

    await expect(signOut()).resolves.toBeUndefined();
    expect(fetch).toHaveBeenCalledWith('/auth/logout', { method: 'POST' });
    expect(JSON.parse(localStorage.getItem('frisket:product-telemetry:v1') ?? 'null')).toEqual({
      schema_version: 1,
      state: 'enabled',
    });
  });
});
