import type { CellValue } from './dataTypes';

export interface GeoPoint {
  lat: number;
  lon: number;
}

export function parseGeoPointValue(v: CellValue): GeoPoint | null {
  if (v === null || v === '') return null;
  let parsed: unknown = v;
  if (typeof v === 'string') {
    try {
      parsed = JSON.parse(v) as unknown;
    } catch {
      return null;
    }
  }
  if (parsed === null || typeof parsed !== 'object') return null;
  const obj = parsed as { lat?: unknown; lon?: unknown };
  const lat = typeof obj.lat === 'number' ? obj.lat : Number(obj.lat);
  const lon = typeof obj.lon === 'number' ? obj.lon : Number(obj.lon);
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
  if (lat < -90 || lat > 90 || lon < -180 || lon > 180) return null;
  return { lat, lon };
}

export function formatGeoPoint(point: GeoPoint): string {
  return `${point.lat.toFixed(5)}, ${point.lon.toFixed(5)}`;
}
