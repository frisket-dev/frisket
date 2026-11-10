// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  byteBucket,
  configureProductTelemetry,
  durationBucket,
  sendProductTelemetry,
  setTelemetryPreference,
  setTelemetryWalkthrough,
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
    setTelemetryWalkthrough(null);
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

it('attaches bounded client facts without sending the user-agent or location', () => {
  localStorage.clear();
  configureProductTelemetry(true);
  setTelemetryPreference(true);
  vi.stubGlobal('navigator', { userAgent: 'Mozilla/5.0 (secret-device) Chrome/142.0 Electron/39.1', platform: 'MacIntel' });
  const fetch = vi.fn(() => Promise.resolve(new Response(null, { status: 204 })));
  vi.stubGlobal('fetch', fetch);
  sendProductTelemetry({ type: 'Project.opened', properties: {} }, 'example');
  const sent = JSON.parse(fetch.mock.calls[0]![1]!.body as string);
  expect(sent.client).toEqual({ platform: 'macos', browser: 'electron', browser_major: '39' });
  expect(JSON.stringify(sent)).not.toContain('secret-device');
  vi.unstubAllGlobals();
  configureProductTelemetry(false);
});


it('limits walkthrough context to its project and maps custom IDs to other', () => {
  localStorage.clear(); configureProductTelemetry(true); setTelemetryPreference(true);
  const fetch = vi.fn(() => Promise.resolve(new Response(null, { status: 204 })));
  vi.stubGlobal('fetch', fetch);
  setTelemetryWalkthrough({ projectId: 'p', walkthrough: 'private-guide-name', walkthroughState: 'active' });
  sendProductTelemetry({ type: 'Action.opened', properties: { action: 'map.extract' } }, 'p');
  sendProductTelemetry({ type: 'Action.opened', properties: { action: 'map.extract' } }, 'elsewhere');
  const bodies = fetch.mock.calls.map(call => JSON.parse(String((call as unknown as [string, RequestInit])[1].body)));
  expect(bodies[0].properties).toMatchObject({ walkthrough: 'other', walkthroughState: 'active' });
  expect(bodies[1].properties).toMatchObject({ walkthrough: 'none', walkthroughState: 'none' });
  expect(JSON.stringify(bodies)).not.toContain('private-guide-name');
  setTelemetryWalkthrough(null); configureProductTelemetry(false); vi.unstubAllGlobals();
});
