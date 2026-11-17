import { afterEach, describe, expect, it, vi } from 'vitest';

import { createMapPointsArrowApi } from '../../src/api/raw/mapPointsArrow';
import { subscribeMapPointsMeta } from '../../src/api/mapPointsMeta';

afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetAllMocks();
});

describe('map-points Arrow raw transport', () => {
  it('owns the exact map-points query and response metadata', async () => {
    const metadata = vi.fn();
    const unsubscribe = subscribeMapPointsMeta(metadata);
    const fetch = vi.fn(async () => new Response(new ArrayBuffer(3), {
      headers: {
        'X-Frisket-Map-Transient': '1',
        'X-Frisket-Map-Generation': 'gen-7',
        'X-Frisket-Map-Valid-Points': '2',
        'X-Frisket-Map-Schema': 'schema-7',
        'X-Frisket-Map-Backend': 'duckdb',
      },
    }));
    vi.stubGlobal('fetch', fetch);
    const api = createMapPointsArrowApi(
      (status, message) => Object.assign(new Error(message), { status }),
      'team project',
    );

    await expect(api.getMapPointsArrowBuffer('sheet/1', 'geo id', {
      bbox: [1, 2, 3, 4],
      attrs: ['score', 'label name'],
      filter: { op: 'and', children: [] },
      sort: [{ columnId: 'name', direction: 'asc' }],
    })).resolves.toHaveProperty('byteLength', 3);

    await api.getMapPointsArrowBuffer('sheet/1', 'geo id');
    expect(fetch.mock.calls.map(([input]) => String(input))).toEqual([
      '/api/projects/team%20project/sheets/sheet/1/map/points?column_id=geo+id&format=arrow&bbox=1%2C2%2C3%2C4&attrs=score%2Clabel+name&filter=%7B%22op%22%3A%22and%22%2C%22children%22%3A%5B%5D%7D&sort=%5B%7B%22columnId%22%3A%22name%22%2C%22direction%22%3A%22asc%22%7D%5D',
      '/api/projects/team%20project/sheets/sheet/1/map/points?column_id=geo+id&format=arrow',
    ]);
    expect(metadata).toHaveBeenCalledWith({
      sheetId: 'sheet/1',
      columnId: 'geo id',
      validPoints: 2,
      transient: true,
      generation: 'gen-7',
      schema: 'schema-7',
      backend: 'duckdb',
    });
    unsubscribe();
  });

  it('maps response errors but preserves network rejection identity', async () => {
    const errorFactory = vi.fn((status, message) =>
      Object.assign(new Error(`mapped:${message}`), { status }),
    );
    const api = createMapPointsArrowApi(errorFactory, 'team project');
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
      detail: { message: 'map column is unavailable' },
    }), { status: 422 })));
    await expect(api.getMapPointsArrowBuffer('s', 'c')).rejects.toMatchObject({
      status: 422,
      message: 'mapped:map column is unavailable',
    });
    expect(errorFactory).toHaveBeenCalledWith(422, 'map column is unavailable');

    const network = new Error('network identity');
    vi.stubGlobal('fetch', vi.fn(async () => Promise.reject(network)));
    await expect(api.getMapPointsArrowBuffer('s', 'c')).rejects.toBe(network);
  });
});
