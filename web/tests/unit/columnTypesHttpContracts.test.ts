import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('pluralize', () => ({
  default: Object.assign((word: string) => word, {
    addIrregularRule: vi.fn(),
    addUncountableRule: vi.fn(),
  }),
}));

import { createColumnTypesApi } from '../../src/api/columnTypes';
import { ApiError } from '../../src/api/contractErrors';
import { jsonResponse } from '../support/httpContractTestHelpers';

const columnTypes = [{
  name: 'plugin_stars',
  core: false,
  plugin: 'stars',
  presentation: { renderer: 'stars', align: 'center', userSelectable: true },
  has_validator: true,
  has_parser: false,
  description: 'A plugin-provided rating.',
}];

const expectedColumnTypes = [{
  name: 'plugin_stars',
  core: false,
  plugin: 'stars',
  presentation: { renderer: 'stars', align: 'center', userSelectable: true },
  hasValidator: true,
  hasParser: false,
  description: 'A plugin-provided rating.',
}];

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
    await expect(createColumnTypesApi().listColumnTypes()).resolves.toEqual(expectedColumnTypes);
    await expect(createColumnTypesApi('project /☃').listColumnTypes())
      .resolves.toEqual(expectedColumnTypes);

    expect(requests.map(({ input, init }) => [String(input), init?.method, init?.body])).toEqual([
      ['/api/column-types', 'GET', undefined],
      ['/api/projects/project%20%2F%E2%98%83/column-types', 'GET', undefined],
    ]);
    expect(globalThis.fetch).toHaveBeenCalledTimes(2);
    for (const { init } of requests) {
      expect(init?.headers).toBeUndefined();
    }
  });

  it('owns synchronous project/global fallback, exact-once hostile encoding, and mapping', async () => {
    const requests: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      requests.push(String(input));
      return jsonResponse(columnTypes);
    }));
    const projectApi = createColumnTypesApi('project /☃');
    const globalApi = createColumnTypesApi(null);
    await expect(projectApi.listColumnTypes()).resolves.toEqual(expectedColumnTypes);
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
    const api = createColumnTypesApi('project /☃');
    const request = api.listColumnTypes();
    await expect(request).rejects.toBeInstanceOf(ApiError);
    await expect(request).rejects.toMatchObject({
      name: 'ApiError',
      status: 500,
      message: 'column types unavailable',
      code: undefined,
      details: undefined,
    });
    expect(requests).toEqual(['/api/projects/project%20%2F%E2%98%83/column-types']);
  });
});
