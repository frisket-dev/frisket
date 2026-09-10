import { afterEach, describe, expect, it, vi } from 'vitest';

import { createErrorIntakeApi } from '../../src/api/errorIntake';

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('error intake HTTP contracts', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('preserves diagnostic-bundle path, body, headers, signal, and domain response', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    const wire = {
      ok: true,
      report_id: 41,
      bundle: {
        message: 'user report',
        include_raw_values: false,
        recent_client_error_ids: [7],
        context: { trace_id: 'trace-1' },
      },
    };
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return jsonResponse(wire);
    }));
    const errorFactory = vi.fn((status: number, payload: unknown) => new Error(`${status}:${String(payload)}`));
    const api = createErrorIntakeApi(errorFactory);
    const controller = new AbortController();
    const headers = new Headers({ 'X-Trace-Id': 'bundle' });
    const body = {
      message: 'user report',
      route: '/p/demo',
      include_raw_values: false,
      context: { trace_id: 'trace-1' },
      recent_client_error_ids: [7],
    };

    const result = await api.createDiagnosticBundle(body, {
      signal: controller.signal,
      headers,
    });

    expect(result).toEqual(wire);
    expect(requests).toHaveLength(1);
    expect(String(requests[0]?.input)).toBe('/api/diagnostic-bundle');
    expect(requests[0]?.init?.method).toBe('POST');
    expect(requests[0]?.init?.body).toBe(JSON.stringify(body));
    expect(requests[0]?.init?.signal).toBe(controller.signal);
    expect(new Headers(requests[0]?.init?.headers).get('x-trace-id')).toBe('bundle');
    expect(new Headers(requests[0]?.init?.headers).get('content-type')).toBe('application/json');
    expect(errorFactory).not.toHaveBeenCalled();
  });

  it('routes non-2xx responses through the caller error factory', async () => {
    vi.stubGlobal('fetch', vi.fn(async () =>
      jsonResponse({ detail: 'diagnostic report rate limit exceeded' }, 429),
    ));
    const errorFactory = vi.fn(
      (status: number) => new Error(`intake-error-${status}`),
    );
    const api = createErrorIntakeApi(errorFactory);

    await expect(api.createDiagnosticBundle({ message: 'x' })).rejects.toThrow(
      'intake-error-429',
    );
    expect(errorFactory).toHaveBeenCalledWith(429, {
      detail: 'diagnostic report rate limit exceeded',
    });
  });

  it('keeps the edition-divergent bundle payload open', async () => {
    const hostedShapedWire = {
      ok: true,
      report_id: 9,
      bundle: {
        generated_at: '2026-08-11T00:00:00Z',
        user: { org_id: 1, user_id: 2 },
        queue: { jobs: [] },
        recent_errors: [],
      },
    };
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(hostedShapedWire)));
    const api = createErrorIntakeApi(() => new Error('unused'));

    const result = await api.createDiagnosticBundle({ message: 'x' });

    expect(result.bundle).toEqual(hostedShapedWire.bundle);
  });

  it('sends accepted client errors through the generated contract with unload-safe credentials', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    const body = {
      source: 'browser',
      severity: 'error',
      name: 'ErrorEvent',
      message: 'Unhandled browser error',
      stack: null,
      route: '/projects/demo',
      context: { project_id: 'demo' },
    };
    const hostedWire = { ok: true, id: 47 };
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return jsonResponse(hostedWire, 202);
    }));
    const api = createErrorIntakeApi(() => new Error('unused'));

    await expect(api.reportClientError(body, {
      credentials: 'same-origin',
      keepalive: true,
    })).resolves.toEqual(hostedWire);

    expect(requests).toHaveLength(1);
    expect(String(requests[0]?.input)).toBe('/api/client-errors');
    expect(requests[0]?.init).toMatchObject({
      method: 'POST',
      credentials: 'same-origin',
      keepalive: true,
      body: JSON.stringify(body),
    });
    expect(new Headers(requests[0]?.init?.headers).get('content-type')).toBe('application/json');
  });

});
