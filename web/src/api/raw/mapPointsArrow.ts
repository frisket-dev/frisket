// Named raw transport for the map-points Arrow IPC endpoint.
//
// This is deliberately not a generic fetch wrapper: the route, query shape,
// response metadata, and error translation belong together.

import { emitMapPointsMeta } from '../mapPointsMeta';
import type { MapPointsOptions } from '../types';

type MapPointsErrorFactory = (status: number, message: string) => Error;

interface MapPointsArrowApi {
  getMapPointsArrowBuffer(
    sheetId: string,
    columnId: string,
    options?: MapPointsOptions | null,
  ): Promise<ArrayBuffer>;
}

function firstNonEmptyString(...values: Array<string | null | undefined>): string {
  for (const value of values) {
    if (typeof value === 'string' && value.trim().length > 0) return value;
  }
  return '';
}

function httpFailureFallback(status: number): string {
  return status > 0 ? `Request failed (HTTP ${status})` : 'Request failed';
}

function sheetViewParams(options: MapPointsOptions | null = {}): URLSearchParams {
  const opts = options ?? {};
  const params = new URLSearchParams();
  if (opts.filter && Object.keys(opts.filter).length > 0) {
    params.set('filter', JSON.stringify(opts.filter));
  }
  if (opts.sort && opts.sort.length > 0) {
    params.set('sort', JSON.stringify(opts.sort));
  }
  return params;
}

export function createMapPointsArrowApi(
  errorFactory: MapPointsErrorFactory,
  projectId: string,
): MapPointsArrowApi {
  async function fetchMapPointsResponse(
    sheetId: string,
    columnId: string,
    options: MapPointsOptions | null,
  ): Promise<Response> {
    const opts = options ?? {};
    const params = new URLSearchParams({ column_id: columnId, format: 'arrow' });
    if (opts.bbox) params.set('bbox', opts.bbox.join(','));
    if (opts.attrs && opts.attrs.length > 0) params.set('attrs', opts.attrs.join(','));
    sheetViewParams({ filter: opts.filter, sort: opts.sort }).forEach((value, key) =>
      params.set(key, value),
    );
    const res = await fetch(
      `/api/projects/${encodeURIComponent(projectId)}/sheets/${sheetId}/map/points?${params.toString()}`,
    );
    if (!res.ok) {
      let detail: unknown = res.statusText;
      try {
        const body = (await res.json()) as { detail?: unknown; message?: unknown };
        const value = body?.detail ?? body?.message ?? res.statusText;
        detail = (value as { message?: unknown })?.message ?? value;
      } catch {
        // non-JSON error body
      }
      throw errorFactory(
        res.status,
        firstNonEmptyString(
          typeof detail === 'string' ? detail : JSON.stringify(detail),
          httpFailureFallback(res.status),
        ),
      );
    }
    emitMapPointsMeta({
      sheetId,
      columnId,
      validPoints: Number(res.headers.get('X-Frisket-Map-Valid-Points') ?? 0),
      transient: res.headers.get('X-Frisket-Map-Transient') === '1',
      generation: res.headers.get('X-Frisket-Map-Generation') ?? '',
      schema: res.headers.get('X-Frisket-Map-Schema') ?? '',
      backend: res.headers.get('X-Frisket-Map-Backend') ?? '',
    });
    return res;
  }

  return {
    async getMapPointsArrowBuffer(sheetId, columnId, options = {}) {
      const res = await fetchMapPointsResponse(sheetId, columnId, options);
      return res.arrayBuffer();
    },
  };
}
