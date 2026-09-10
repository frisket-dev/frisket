import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('apache-arrow', () => ({ tableFromIPC: vi.fn() }));
vi.mock('pluralize', () => ({
  default: Object.assign((word: string) => word, {
    addIrregularRule: vi.fn(),
    addUncountableRule: vi.fn(),
  }),
}));

import { ApiError, createProjectApi } from '../../src/api/real';
import { createRuntimeProjectionsApi } from '../../src/api/runtimeProjections';

const realApi = createProjectApi('test-project');

const status = {
  schemaVersion: 'frisket.runtime_projection_status.v1',
  status: 'ready',
  freshness: { state: 'fresh', generation: 'gen-1', transient: false },
  outputs: { artifactRefs: [], metrics: { rows: 3 } },
  warnings: [],
};

const build = {
  schemaVersion: 'frisket.runtime_projection_build_plan.v1',
  status: 'accepted',
  build: { operation: 'rebuild', idempotencyKey: 'build-1' },
  outputs: { artifactRefs: [], metrics: {} },
  warnings: [],
};

function jsonResponse(payload: unknown, statusCode = 200): Response {
  return new Response(JSON.stringify(payload), {
    status: statusCode,
    headers: { 'Content-Type': 'application/json' },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('runtime projection generated HTTP contracts', () => {
  it('uses exact paths, bodies, one encoding pass, headers, and abort options', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return jsonResponse(requests.length === 1 ? status : build);
    }));
    const controller = new AbortController();
    const api = createRuntimeProjectionsApi(
      (statusCode, payload) => Object.assign(
        new Error(`mapped-${statusCode}`),
        { status: statusCode, payload },
      ),
      'hostile /%25 ☃',
    );
    const options = {
      signal: controller.signal,
      headers: { Authorization: 'Bearer projection', 'X-Trace-Id': 'runtime-projection' },
    };
    await expect(api.status({
      projectionKind: 'timeline',
      target: { sheetId: 'sheet/1', extension: { kept: true } },
      params: { window: 14 },
    }, options)).resolves.toEqual(status);
    await expect(api.build({
      projectionKind: 'timeline',
      target: { sheetId: 'sheet/1' },
      params: { window: 30 },
      mode: 'rebuild',
    }, options)).resolves.toEqual(build);

    const base = '/api/projects/hostile%20%2F%2525%20%E2%98%83/projections/runtime';
    expect(requests.map(({ input, init }) => [String(input), init?.method, init?.body])).toEqual([
      [`${base}/status`, 'POST', JSON.stringify({
        projectionKind: 'timeline',
        target: { sheetId: 'sheet/1', extension: { kept: true } },
        params: { window: 14 },
      })],
      [`${base}/build`, 'POST', JSON.stringify({
        projectionKind: 'timeline',
        target: { sheetId: 'sheet/1' },
        params: { window: 30 },
        mode: 'rebuild',
      })],
    ]);
    for (const request of requests) {
      expect(request.init?.signal).toBe(controller.signal);
      const headers = new Headers(request.init?.headers);
      expect(headers.get('authorization')).toBe('Bearer projection');
      expect(headers.get('x-trace-id')).toBe('runtime-projection');
      expect(headers.get('content-type')).toBe('application/json');
    }
  });

  it('preserves error, non-JSON, network, and abort identities without retries', async () => {
    const errorFactory = vi.fn((statusCode: number, payload: unknown) =>
      Object.assign(new Error(`mapped-${statusCode}`), { status: statusCode, payload }),
    );
    const api = createRuntimeProjectionsApi(errorFactory, 'test-project');

    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({
      detail: { code: 'invalid_runtime_projection_request', message: 'invalid', field: 'mode' },
    }, 400)));
    await expect(api.build({
      projectionKind: 'timeline', target: { sheetId: 's' }, mode: 'refresh',
    })).rejects.toMatchObject({ message: 'mapped-400', status: 400 });
    expect(errorFactory).toHaveBeenCalledWith(400, {
      detail: { code: 'invalid_runtime_projection_request', message: 'invalid', field: 'mode' },
    });

    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse([
      { type: 'missing', loc: ['body', 'projectionKind'], msg: 'Field required' },
    ], 422)));
    await expect(api.status({ projectionKind: 'timeline', target: { sheetId: 's' } }))
      .rejects.toMatchObject({ message: 'mapped-422', status: 422 });

    vi.stubGlobal('fetch', vi.fn(async () => new Response('upstream unavailable', {
      status: 502,
      statusText: 'Bad Gateway',
    })));
    await expect(api.status({ projectionKind: 'timeline', target: { sheetId: 's' } }))
      .rejects.toMatchObject({ message: 'mapped-502', status: 502, payload: { detail: 'Bad Gateway' } });

    const network = new Error('network identity');
    vi.stubGlobal('fetch', vi.fn(async () => Promise.reject(network)));
    await expect(api.status({ projectionKind: 'timeline', target: { sheetId: 's' } })).rejects.toBe(network);

    const aborted = new DOMException('cancelled', 'AbortError');
    const fetch = vi.fn(async () => Promise.reject(aborted));
    vi.stubGlobal('fetch', fetch);
    await expect(api.status({ projectionKind: 'timeline', target: { sheetId: 's' } })).rejects.toBe(aborted);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('uses the scoped RealApi factory without exposing producer detail extras', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({
      detail: {
        code: 'unsupported_runtime_projection',
        message: 'No trusted projection runtime binding exists',
        projection_kind: 'timeline',
        field: 'projectionKind',
      },
    }, 404)));

    await expect(realApi.getRuntimeProjectionStatus({
      projectionKind: 'timeline', target: { sheetId: 's' },
    })).rejects.toMatchObject({
      name: 'ApiError',
      status: 404,
      message: 'No trusted projection runtime binding exists',
      code: 'unsupported_runtime_projection',
      details: undefined,
    } satisfies Partial<ApiError>);

    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(status)));
    await expect(realApi.getRuntimeProjectionStatus({
      projectionKind: 'timeline', target: { sheetId: 's' },
    })).resolves.toEqual(status);
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(build)));
    await expect(realApi.buildRuntimeProjection({
      projectionKind: 'timeline', target: { sheetId: 's' }, mode: 'rebuild',
    })).resolves.toEqual(build);
  });
});
