import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  googleOAuthStartUrl,
  oidcSignInUrl,
} from '../../src/api/raw/browserAuth';
import {
  readDocumentTextResource,
  readEvidenceTextResource,
} from '../../src/api/raw/blobText';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('named browser auth operations', () => {
  it('returns browser-navigation URLs for sign-in and connected-account OAuth', () => {
    expect(oidcSignInUrl('work sso')).toBe('/auth/oidc/work%20sso');
    expect(googleOAuthStartUrl()).toBe('/api/org/oauth/google/start');
  });

});

describe('named blob text operations', () => {
  it('returns evidence text and rejects a failed resource for the quote fallback', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('source text', { status: 200 })));
    await expect(readEvidenceTextResource('/blobs/source.txt')).resolves.toBe('source text');

    vi.stubGlobal('fetch', vi.fn(async () => new Response(null, { status: 404 })));
    await expect(readEvidenceTextResource('/blobs/missing.txt')).rejects.toThrow('404');
  });

  it('passes the document viewer\'s exact abort signal through and retains its human error', async () => {
    const controller = new AbortController();
    const fetch = vi.fn(async () => new Response(null, { status: 403 }));
    vi.stubGlobal('fetch', fetch);

    await expect(
      readDocumentTextResource('/blobs/private.txt', { signal: controller.signal }),
    ).rejects.toThrow('Could not load this file (403).');
    expect(fetch).toHaveBeenCalledWith('/blobs/private.txt', { signal: controller.signal });
  });
});
