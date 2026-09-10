import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('apache-arrow', () => ({ tableFromIPC: vi.fn() }));
vi.mock('pluralize', () => ({
  default: Object.assign((word: string) => word, {
    addIrregularRule: vi.fn(),
    addUncountableRule: vi.fn(),
  }),
}));

import { ApiError, createProjectApi } from '../../src/api/real';
import { createProjectDataManagementApi } from '../../src/api/projectDataManagement';

const realApi = createProjectApi('test-project');

const retention = {
  schemaVersion: 'frisket.project_retention_policy.v1',
  default_evidence: 'compactable',
  pin_evidence_by_default: false,
  no_compact: false,
  supported_default_evidence: ['compactable', 'pinned', 'materialized'],
  preserved_extension: { source: 'server' },
};
const network = {
  schemaVersion: 'frisket.project_network_policy.v1',
  mode: 'inherit',
  effective: 'on',
  org_default: null,
};
const settings = {
  media_allow_private_hosts: false,
  media_allow_private_hosts_locked: false,
};
const compact = {
  blobs_removed: 1,
  bytes_freed: 2,
  db_bytes_before: 3,
  db_bytes_after: 4,
  db_bytes_reclaimed: 5,
  results_pruned: 6,
  skipped: false,
  reason: null,
  preserved_extension: 'open response field',
};

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('project data-management generated HTTP contracts', () => {
  it('preserves runtime method, path encoding, patch bodies, response DTOs, and options', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    const responses = [retention, retention, network, network, settings, settings, compact];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return jsonResponse(responses.shift());
    }));
    const controller = new AbortController();
    const options = {
      signal: controller.signal,
      headers: { Authorization: 'Bearer project', 'X-Trace-Id': 'data-management' },
    };
    const baseApi = createProjectDataManagementApi((status, payload) =>
      Object.assign(new Error(`mapped ${status}`), { status, payload }),
      'hostile /%25 ☃',
    );
    const api = createProjectDataManagementApi((status, payload) =>
      Object.assign(new Error(`mapped ${status}`), { status, payload }),
      'switched /%25 ☃',
    );
    await expect(baseApi.getProjectRetention(options)).resolves.toEqual(retention);
    await expect(api.updateProjectRetention({
      default_evidence: 'pinned',
      no_compact: undefined,
      preserved_extension: { retained: true },
    }, options)).resolves.toEqual(retention);
    await expect(api.getProjectNetworkPolicy(options)).resolves.toEqual(network);
    await expect(api.updateProjectNetworkPolicy({ mode: 'off' }, options)).resolves.toEqual(network);
    await expect(api.getProjectSettings(options)).resolves.toEqual(settings);
    await expect(api.updateProjectSettings({
      media_allow_private_hosts: true,
      media_allow_private_hosts_locked: false,
      preserved_extension: { retained: true },
    } as Partial<typeof settings> & Record<string, unknown>, options)).resolves.toEqual(settings);
    await expect(api.compactProject(options)).resolves.toEqual(compact);

    const base = '/api/projects/hostile%20%2F%2525%20%E2%98%83';
    const switched = '/api/projects/switched%20%2F%2525%20%E2%98%83';
    expect(requests.map(({ input, init }) => [String(input), init?.method, init?.body])).toEqual([
      [`${base}/retention`, 'GET', undefined],
      [`${switched}/retention`, 'PATCH', JSON.stringify({
        default_evidence: 'pinned',
        preserved_extension: { retained: true },
      })],
      [`${switched}/network`, 'GET', undefined],
      [`${switched}/network`, 'PATCH', JSON.stringify({ mode: 'off' })],
      [`${switched}/settings`, 'GET', undefined],
      [`${switched}/settings`, 'PATCH', JSON.stringify({
        media_allow_private_hosts: true,
        media_allow_private_hosts_locked: false,
        preserved_extension: { retained: true },
      })],
      [`${switched}/compact`, 'POST', undefined],
    ]);
    for (const request of requests) {
      expect(request.init?.signal).toBe(controller.signal);
      const headers = new Headers(request.init?.headers);
      expect(headers.get('authorization')).toBe('Bearer project');
      expect(headers.get('x-trace-id')).toBe('data-management');
    }
    for (const request of [requests[1], requests[3], requests[5]]) {
      expect(new Headers(request.init?.headers).get('content-type')).toBe('application/json');
    }
    for (const request of [requests[0], requests[2], requests[4], requests[6]]) {
      expect(new Headers(request.init?.headers).get('content-type')).toBeNull();
    }
  });

  it('keeps generic errors, non-JSON fallback, network identity, empty PATCHes, and RealApi methods stable', async () => {
    const api = createProjectDataManagementApi((status, payload) =>
      Object.assign(new Error(`mapped ${status}`), { status, payload }),
      'test-project',
    );

    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ detail: 'retention denied' }, 400)));
    await expect(api.getProjectRetention()).rejects.toMatchObject({
      message: 'mapped 400', status: 400, payload: { detail: 'retention denied' },
    });

    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse([
      { type: 'missing', loc: ['body', 'mode'], msg: 'Field required' },
    ], 422)));
    await expect(api.updateProjectNetworkPolicy({ mode: 'off' })).rejects.toMatchObject({
      message: 'mapped 422',
      status: 422,
      payload: [{ type: 'missing', loc: ['body', 'mode'], msg: 'Field required' }],
    });

    vi.stubGlobal('fetch', vi.fn(async () => new Response('service unavailable', {
      status: 503,
      statusText: 'Service Unavailable',
    })));
    await expect(api.getProjectSettings()).rejects.toMatchObject({
      message: 'mapped 503',
      status: 503,
      payload: { detail: 'Service Unavailable' },
    });

    const emptyRequests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      emptyRequests.push({ input, init });
      return jsonResponse(settings);
    }));
    await expect(api.updateProjectSettings({})).resolves.toEqual(settings);
    expect(emptyRequests[0]?.init?.body).toBe('{}');
    expect(new Headers(emptyRequests[0]?.init?.headers).get('content-type')).toBe('application/json');

    const networkFailure = new Error('network identity');
    vi.stubGlobal('fetch', vi.fn(async () => Promise.reject(networkFailure)));
    await expect(api.compactProject()).rejects.toBe(networkFailure);

    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(retention)));
    await expect(realApi.getProjectRetention()).resolves.toEqual(retention);

    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ detail: 'settings denied' }, 400)));
    await expect(realApi.getProjectSettings()).rejects.toMatchObject({
      name: 'ApiError',
      status: 400,
      message: 'settings denied',
      code: undefined,
      details: undefined,
    } satisfies Partial<ApiError>);
  });
});
