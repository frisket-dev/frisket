// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  byteBucket,
  configureProductTelemetry,
  durationBucket,
  sendProductTelemetry,
  setTelemetryPreference,
  telemetryPreference,
} from '../../src/telemetry/productTelemetry';

const STORAGE_KEY = 'frisket:product-telemetry:v1';

describe('product telemetry boundary', () => {
  beforeEach(() => {
    localStorage.clear();
    configureProductTelemetry(true);
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-08-31T23:59:59Z'));
  });

  afterEach(() => {
    configureProductTelemetry(false);
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('does not send before the user chooses or after opt-out', () => {
    const fetch = vi.fn();
    vi.stubGlobal('fetch', fetch);

    sendProductTelemetry({ type: 'App.opened', properties: { platform: 'linux' } });
    expect(telemetryPreference()).toBe('unanswered');
    expect(fetch).not.toHaveBeenCalled();

    setTelemetryPreference(false);
    sendProductTelemetry({ type: 'App.opened', properties: { platform: 'linux' } });
    expect(fetch).not.toHaveBeenCalled();
  });

  it('rotates the pseudonym lazily when the UTC month changes', () => {
    const fetch = vi.fn(() => Promise.resolve(new Response(null, { status: 204 })));
    vi.stubGlobal('fetch', fetch);
    setTelemetryPreference(true);

    sendProductTelemetry({ type: 'App.opened', properties: { platform: 'linux' } });
    const august = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '{}') as {
      monthly_id: string;
      utc_month: string;
    };
    expect(august.utc_month).toBe('2026-08');
    expect(august.monthly_id).toMatch(/^[0-9a-f]{64}$/);

    vi.setSystemTime(new Date('2026-09-01T00:00:01Z'));
    sendProductTelemetry({ type: 'App.opened', properties: { platform: 'linux' } });
    const september = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '{}') as {
      monthly_id: string;
      utc_month: string;
    };
    expect(september.utc_month).toBe('2026-09');
    expect(september.monthly_id).not.toBe(august.monthly_id);
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it('uses the documented coarse boundaries', () => {
    expect(durationBucket(999)).toBe('lt_1s');
    expect(durationBucket(1_000)).toBe('1_10s');
    expect(byteBucket(0)).toBe('zero');
    expect(byteBucket(1024 ** 2)).toBe('1_100mb');
  });
});
