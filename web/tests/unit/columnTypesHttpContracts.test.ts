import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('apache-arrow', () => ({ tableFromIPC: vi.fn() }));
vi.mock('pluralize', () => ({
  default: Object.assign((word: string) => word, {
    addIrregularRule: vi.fn(),
    addUncountableRule: vi.fn(),
  }),
}));

import {
  createColumnTypesApi,
  createColumnTypesDomainApi,
} from '../../src/api/columnTypes';

const columnTypes = [{
  name: 'plugin_stars',
  core: false,
  plugin: 'stars',
  presentation: { renderer: 'stars', align: 'center', userSelectable: true },
  has_validator: true,
  has_parser: false,
  description: 'A plugin-provided rating.',
}];

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('column type generated HTTP contracts', () => {
  it('preserves global/project URL bytes for the catalog reads', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return jsonResponse(columnTypes);
    }));
    const controller = new AbortController();
    const api = createColumnTypesApi((status) => new Error(`column types ${status}`));
    const options = {
      signal: controller.signal,
      headers: { Authorization: 'Bearer column-types', 'X-Trace-Id': 'columns' },
    };

    await expect(api.listColumnTypes(options)).resolves.toEqual(columnTypes);
    await expect(api.listProjectColumnTypes('project /☃', options)).resolves.toEqual(columnTypes);

    expect(requests.map(({ input, init }) => [String(input), init?.method, init?.body])).toEqual([
      ['/api/column-types', 'GET', undefined],
      ['/api/projects/project%20%2F%E2%98%83/column-types', 'GET', undefined],
    ]);
    expect(globalThis.fetch).toHaveBeenCalledTimes(2);
    for (const { init } of requests) {
      expect(init?.signal).toBe(controller.signal);
      const headers = new Headers(init?.headers);
      expect(headers.get('authorization')).toBe('Bearer column-types');
      expect(headers.get('x-trace-id')).toBe('columns');
      expect(headers.get('content-type')).toBeNull();
    }
  });

  it('owns synchronous project/global fallback, exact-once hostile encoding, and mapping', async () => {
    const requests: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      requests.push(String(input));
      return jsonResponse(columnTypes);
    }));
    const projectApi = createColumnTypesDomainApi(
      (status) => new Error(`column types ${status}`),
      'project /☃',
    );
    const globalApi = createColumnTypesDomainApi(
      (status) => new Error(`column types ${status}`),
      null,
    );
    await expect(projectApi.listColumnTypes()).resolves.toEqual([{
      name: 'plugin_stars',
      core: false,
      plugin: 'stars',
      presentation: { renderer: 'stars', align: 'center', userSelectable: true },
      hasValidator: true,
      hasParser: false,
      description: 'A plugin-provided rating.',
    }]);
    await expect(globalApi.listColumnTypes()).resolves.toHaveLength(1);
    expect(requests).toEqual([
      '/api/projects/project%20%2F%E2%98%83/column-types',
      '/api/column-types',
    ]);
  });

  it('never falls back to the global catalog after a project request failure', async () => {
    const requests: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      requests.push(String(input));
      return jsonResponse({ detail: 'column types unavailable' }, 500);
    }));
    const api = createColumnTypesDomainApi(
      (status) => new Error(`column types ${status}`),
      'project /☃',
    );
    await expect(api.listColumnTypes()).rejects.toThrow('column types 500');
    expect(requests).toEqual(['/api/projects/project%20%2F%E2%98%83/column-types']);
  });
});
