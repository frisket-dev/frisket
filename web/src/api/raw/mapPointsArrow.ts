// Named raw transport for the map-points Arrow IPC endpoint.
//
// This is deliberately not a generic fetch wrapper: the route, query shape,
// Arrow decoder, response metadata, and error translation belong together.

import { tableFromIPC } from 'apache-arrow';

import { emitMapPointsMeta } from '../mapPointsMeta';
import type { MapPointsOptions, MapPointsResult } from '../types';

export type MapPointsErrorFactory = (status: number, message: string) => Error;

export interface MapPointsArrowApi {
  getMapPoints(
    sheetId: string,
    columnId: string,
    options?: MapPointsOptions | null,
  ): Promise<MapPointsResult>;
  getMapPointsArrowBuffer(
    sheetId: string,
    columnId: string,
    options?: MapPointsOptions | null,
  ): Promise<ArrayBuffer>;
}

interface DecodedMapPointsArrow {
  count: number;
  positions: Float32Array;
  rowIds: string[];
  attributes: Record<string, Array<number | string | null>>;
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

function decodeMapPointsArrow(
  buf: ArrayBuffer,
  errorFactory: MapPointsErrorFactory,
): DecodedMapPointsArrow {
  let table;
  try {
    table = tableFromIPC(new Uint8Array(buf));
  } catch (err) {
    throw errorFactory(
      500,
      `invalid Arrow map-points payload: ${err instanceof Error ? err.message : String(err)}`,
    );
  }
  const rowIdVector = table.getChild('row_id');
  const lonVector = table.getChild('lon');
  const latVector = table.getChild('lat');
  if (!rowIdVector || !lonVector || !latVector) {
    throw errorFactory(500, 'invalid Arrow map-points payload: missing columns');
  }
  const count = table.numRows;
  const rowIdValues = rowIdVector.toArray() as ArrayLike<bigint | number | null>;
  const lonValues = lonVector.toArray() as ArrayLike<number | null>;
  const latValues = latVector.toArray() as ArrayLike<number | null>;
  if (
    rowIdValues.length < count ||
    lonValues.length < count ||
    latValues.length < count
  ) {
    throw errorFactory(500, 'invalid Arrow map-points payload: short columns');
  }
  const positions = new Float32Array(count * 2);
  const rowIds: string[] = [];
  for (let i = 0; i < count; i++) {
    const rowId = rowIdValues[i];
    const lon = lonValues[i];
    const lat = latValues[i];
    if (rowId == null || lon == null || lat == null) {
      throw errorFactory(500, 'invalid Arrow map-points payload: null values');
    }
    positions[i * 2] = Number(lon);
    positions[i * 2 + 1] = Number(lat);
    rowIds.push(rowId.toString());
  }
  const attributes: Record<string, Array<number | string | null>> = {};
  for (const field of table.schema.fields) {
    if (!field.name.startsWith('attr:')) continue;
    const columnId = field.name.slice('attr:'.length);
    const raw = table.getChild(field.name)?.toArray() as
      | ArrayLike<number | string | bigint | null>
      | undefined;
    if (!raw) continue;
    const values: Array<number | string | null> = [];
    for (let i = 0; i < count; i++) {
      const value = raw[i];
      values.push(
        value == null ? null : typeof value === 'bigint' ? Number(value) : value,
      );
    }
    attributes[columnId] = values;
  }
  return { count, positions, rowIds, attributes };
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
    async getMapPoints(sheetId, columnId, options = {}) {
      const res = await fetchMapPointsResponse(sheetId, columnId, options);
      const decoded = decodeMapPointsArrow(await res.arrayBuffer(), errorFactory);
      return {
        count: decoded.count,
        positions: decoded.positions,
        rowIds: decoded.rowIds,
        attributes: decoded.attributes,
        transient: res.headers.get('X-Frisket-Map-Transient') === '1',
        generation: res.headers.get('X-Frisket-Map-Generation') ?? '',
        validPoints: Number(res.headers.get('X-Frisket-Map-Valid-Points') ?? decoded.count),
        schema: res.headers.get('X-Frisket-Map-Schema') ?? '',
        backend: res.headers.get('X-Frisket-Map-Backend') ?? '',
      };
    },

    async getMapPointsArrowBuffer(sheetId, columnId, options = {}) {
      const res = await fetchMapPointsResponse(sheetId, columnId, options);
      return res.arrayBuffer();
    },
  };
}
