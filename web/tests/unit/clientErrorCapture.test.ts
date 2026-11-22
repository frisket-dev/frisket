// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const GENERIC_PADDED_TOKEN = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij123456==';

function jsonResponse(payload: unknown, status = 202): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

const flushPromises = async (): Promise<void> => {
  await new Promise((resolve) => setTimeout(resolve, 0));
};

function dispatchWindowError(message: string, stack?: string): void {
  const err = new Error(message);
  if (stack !== undefined) err.stack = stack;
  window.dispatchEvent(new ErrorEvent('error', { message: err.message, error: err }));
}

function postedReport(fetchMock: ReturnType<typeof vi.fn>, callIndex = 0) {
  const [, init] = fetchMock.mock.calls[callIndex] as [string, RequestInit];
  return JSON.parse(String(init.body)) as {
    message: string;
    stack: string | null;
    route: string;
    context: Record<string, unknown>;
  };
}

describe('client error capture', () => {
  const registeredListeners: Array<{
    type: string;
    listener: EventListenerOrEventListenerObject;
    options?: boolean | AddEventListenerOptions;
  }> = [];
  let originalAddEventListener: typeof window.addEventListener;

  const detachRegisteredListeners = () => {
    while (registeredListeners.length > 0) {
      const entry = registeredListeners.pop()!;
      window.removeEventListener(entry.type, entry.listener, entry.options);
    }
  };

  beforeEach(() => {
    originalAddEventListener = window.addEventListener.bind(window);
    vi.spyOn(window, 'addEventListener').mockImplementation((type, listener, options) => {
      registeredListeners.push({ type, listener, options });
      return originalAddEventListener(type, listener, options);
    });
  });

  afterEach(() => {
    detachRegisteredListeners();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    vi.resetModules();
    window.sessionStorage.clear();
    window.history.replaceState(null, '', '/');
    delete window.__FRISKET_CLIENT_ERROR_CAPTURE__;
    delete window.__FRISKET_RECENT_CLIENT_ERROR_IDS__;
  });

  const loadMountFresh = async () => {
    detachRegisteredListeners();
    vi.resetModules();
    await import('../../src/entries/mount');
  };

  it('is disabled by default on local builds', async () => {
    const fetch = vi.fn().mockResolvedValue(jsonResponse({ ok: true, id: 10 }));
    vi.stubGlobal('fetch', fetch);

    await loadMountFresh();
    dispatchWindowError('ignored default error');
    await flushPromises();

    expect(fetch).not.toHaveBeenCalled();
  });

  it('reports once through the generated client-error operation, redacts secrets, remembers accepted IDs, and swallows failures', async () => {
    window.__FRISKET_CLIENT_ERROR_CAPTURE__ = true;
    const fetch = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ ok: true, id: 47 }))
      .mockRejectedValueOnce(new Error('offline'));
    vi.stubGlobal('fetch', fetch);

    await loadMountFresh();

    window.history.replaceState(
      null,
      '',
      '/p/investigation/s/12?token=sk-secret-token&raw_cells=private-note',
    );
    dispatchWindowError(
      "Cannot read properties of undefined (reading 'toFixed')",
      `TypeError: bad\n  at http://localhost:5173/assets/app.js?token=sk-secret-token\n  opaque ${GENERIC_PADDED_TOKEN}`,
    );
    await flushPromises();

    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch).toHaveBeenCalledWith('/api/client-errors', expect.objectContaining({
      method: 'POST',
      credentials: 'same-origin',
      keepalive: true,
    }));
    const report = postedReport(fetch, 0);
    const serialized = JSON.stringify(report);
    expect(report.message).toContain('toFixed');
    expect(report.route).toBe('/p/investigation/s/12');
    expect(report.context.project_id).toBe('investigation');
    expect(report.context.sheet_id).toBe('12');
    expect(serialized).not.toContain('sk-secret-token');
    expect(serialized).not.toContain('private-note');
    expect(serialized).not.toContain(GENERIC_PADDED_TOKEN);
    expect(window.__FRISKET_RECENT_CLIENT_ERROR_IDS__).toEqual([47]);

    expect(() => dispatchWindowError('second browser error')).not.toThrow();
    await flushPromises();

    expect(fetch).toHaveBeenCalledTimes(2);
    expect(window.__FRISKET_RECENT_CLIENT_ERROR_IDS__).toEqual([47]);
  });

  it('survives malformed percent-encoded route segments without leaking secrets or URIError', async () => {
    window.__FRISKET_CLIENT_ERROR_CAPTURE__ = true;
    const fetch = vi.fn().mockResolvedValue(jsonResponse({ ok: true, id: 11 }));
    vi.stubGlobal('fetch', fetch);

    await loadMountFresh();

    const cases = [
      {
        url: '/p/%E0%A4%A/s/12?token=sk-secret-token&raw_cells=private-note',
        route: '/p/%E0%A4%A/s/12',
        projectId: undefined,
        sheetId: undefined,
      },
      {
        url: '/p/investigation/s/%E0%A4%A?token=sk-secret-token',
        route: '/p/investigation/s/%E0%A4%A',
        projectId: 'investigation',
        sheetId: undefined,
      },
      {
        url: '/p/investigation/s/12/%E0%A4%A?token=sk-secret-token',
        route: '/p/investigation/s/12/%E0%A4%A',
        projectId: 'investigation',
        sheetId: '12',
      },
    ];

    for (const [index, entry] of cases.entries()) {
      window.history.replaceState(null, '', entry.url);
      dispatchWindowError(`Malformed route crash ${index} token=sk-secret-token`);
      await flushPromises();
      const report = postedReport(fetch, index);
      const serialized = JSON.stringify(report);
      expect(report.route).toBe(entry.route);
      expect(report.context.route).toBe(entry.route);
      expect(report.context.project_id).toBe(entry.projectId);
      expect(report.context.sheet_id).toBe(entry.sheetId);
      expect(report.context.route_parse_error).toBe('malformed_percent_encoding');
      expect(serialized).not.toContain('URIError');
      expect(serialized).not.toContain('sk-secret-token');
      expect(serialized).not.toContain('private-note');
    }
  });

  it('supports session-scoped local query opt-in and opt-out', async () => {
    const fetch = vi.fn().mockResolvedValue(jsonResponse({ ok: true, id: 12 }));
    vi.stubGlobal('fetch', fetch);

    window.history.replaceState(null, '', '/?frisket_client_errors=1');
    await loadMountFresh();
    dispatchWindowError('opt-in error');
    await flushPromises();
    expect(fetch).toHaveBeenCalledTimes(1);

    window.history.replaceState(null, '', '/');
    await loadMountFresh();
    dispatchWindowError('persisted error');
    await flushPromises();
    expect(fetch).toHaveBeenCalledTimes(2);

    window.history.replaceState(null, '', '/?frisket_client_errors=maybe');
    await loadMountFresh();
    dispatchWindowError('ignored value keeps session opt-in');
    await flushPromises();
    expect(fetch).toHaveBeenCalledTimes(3);

    window.history.replaceState(null, '', '/?frisket_client_errors=0');
    await loadMountFresh();
    dispatchWindowError('opted-out error');
    await flushPromises();
    expect(fetch).toHaveBeenCalledTimes(3);
  });

  it('ignores manual query opt-in on non-local hostnames', async () => {
    const fetch = vi.fn().mockResolvedValue(jsonResponse({ ok: true, id: 13 }));
    vi.stubGlobal('fetch', fetch);
    const originalLocation = window.location;
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: {
        ...originalLocation,
        hostname: 'frisket.example',
        pathname: '/',
        search: '?frisket_client_errors=1',
      },
    });
    try {
      await loadMountFresh();
      dispatchWindowError('shared origin error');
      await flushPromises();
      expect(fetch).not.toHaveBeenCalled();
    } finally {
      Object.defineProperty(window, 'location', {
        configurable: true,
        value: originalLocation,
      });
    }
  });
});
