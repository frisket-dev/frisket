import { describe, expect, it } from 'vitest';

import {
  authReturnTo,
  rememberPendingSignIn,
  resumePendingSignIn,
} from '../../src/components/authReturnTo';

const location = (pathname: string, search = '', hash = '') => ({
  origin: 'https://app.frisket.dev', pathname, search, hash,
}) as Pick<Location, 'origin' | 'pathname' | 'search' | 'hash'>;

describe('device approval authentication continuation', () => {
  it('preserves the device path and consumes it after authenticated root load', () => {
    const storage = new Map<string, string>();
    const memory = {
      getItem: (key: string) => storage.get(key) ?? null,
      setItem: (key: string, value: string) => { storage.set(key, value); },
      removeItem: (key: string) => { storage.delete(key); },
    } as Storage;
    const path = authReturnTo(location('/device', '?user_code=KQ4F-7M2D'));
    rememberPendingSignIn(path, memory, 1_000);
    expect(resumePendingSignIn(location('/', ''), memory, 2_000)).toBe(path);
    expect(resumePendingSignIn(location('/', ''), memory, 2_000)).toBeNull();
  });

  it('rejects external, protocol-relative, and expired targets', () => {
    expect(authReturnTo(location('/sign-in', '?returnTo=%2Fdevice%3Fuser_code%3DA'))).toBe(
      '/device?user_code=A',
    );
    expect(authReturnTo(location('/sign-in', '?returnTo=https%3A%2F%2Fevil.test'))).toBe('/');
    expect(authReturnTo(location('/sign-in', '?returnTo=%2F%2Fevil.test'))).toBe('/');
    const values = new Map<string, string>();
    const memory = {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => { values.set(key, value); },
      removeItem: (key: string) => { values.delete(key); },
    } as Storage;
    rememberPendingSignIn('/device?user_code=A', memory, 1_000);
    expect(resumePendingSignIn(location('/'), memory, 1_000_000)).toBeNull();
  });
});


describe('shared sign-in destination', () => {
  function memoryStorage(): Storage {
    const values = new Map<string, string>();
    return {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => { values.set(key, value); },
      removeItem: (key: string) => { values.delete(key); },
    } as Storage;
  }

  it.each(['/admin/signup-requests', '/p/reporting/s/2?filter=pending#details'])('resumes %s once after a provider callback', (path) => {
    const memory = memoryStorage();
    rememberPendingSignIn(path, memory, 1_000);
    expect(resumePendingSignIn(location('/'), memory, 2_000)).toBe(path);
    expect(resumePendingSignIn(location('/'), memory, 2_000)).toBeNull();
  });

  it('does not overwrite the destination when an email callback loads sign-in', () => {
    const memory = memoryStorage();
    rememberPendingSignIn('/admin/signup-requests', memory, 1_000);
    rememberPendingSignIn(authReturnTo(location('/', '?auth_kind=magic-link&auth_token=example')), memory, 2_000);
    expect(resumePendingSignIn(location('/'), memory, 3_000)).toBe('/admin/signup-requests');
  });

  it('clears continuation on authenticated arrival without hijacking another page', () => {
    const memory = memoryStorage();
    rememberPendingSignIn('/admin/signup-requests', memory, 1_000);
    expect(resumePendingSignIn(location('/p/other'), memory, 2_000)).toBeNull();
    expect(resumePendingSignIn(location('/'), memory, 3_000)).toBeNull();
  });

  it.each(['https://evil.test', '//evil.test', '/\\evil.test', '/sign-in'])('does not save unsafe or looping destination %s', (path) => {
    const memory = memoryStorage();
    rememberPendingSignIn(path, memory, 1_000);
    expect(resumePendingSignIn(location('/'), memory, 2_000)).toBeNull();
  });

  it.each(['not-json', JSON.stringify({ path: '//evil.test', expiresAt: 10_000 })])('discards invalid saved continuation', (raw) => {
    const memory = memoryStorage();
    memory.setItem('frisket:pending-sign-in', raw);
    expect(resumePendingSignIn(location('/'), memory, 2_000)).toBeNull();
    expect(memory.getItem('frisket:pending-sign-in')).toBeNull();
  });

  it('allows sign-in when browser storage is blocked', () => {
    const memory = {
      getItem: () => { throw new Error('blocked'); },
      setItem: () => { throw new Error('blocked'); },
      removeItem: () => { throw new Error('blocked'); },
    } as unknown as Storage;
    expect(() => rememberPendingSignIn('/admin', memory)).not.toThrow();
    expect(resumePendingSignIn(location('/'), memory)).toBeNull();
  });
});
