import { describe, expect, it } from 'vitest';

import {
  authReturnTo,
  rememberPendingDeviceApproval,
  resumePendingDeviceApproval,
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
    rememberPendingDeviceApproval(path, memory, 1_000);
    expect(resumePendingDeviceApproval(location('/', ''), memory, 2_000)).toBe(path);
    expect(resumePendingDeviceApproval(location('/', ''), memory, 2_000)).toBeNull();
  });

  it('rejects external, protocol-relative, expired, and non-device targets', () => {
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
    rememberPendingDeviceApproval('/settings', memory, 1_000);
    expect(resumePendingDeviceApproval(location('/'), memory, 2_000)).toBeNull();
    rememberPendingDeviceApproval('/device?user_code=A', memory, 1_000);
    expect(resumePendingDeviceApproval(location('/'), memory, 1_000_000)).toBeNull();
  });
});
