import { TIMELINE_SCHEMA_VERSIONS } from '../temporal/model';
import type { CellValue } from './types';

/** A JSON object: not null and not an array. */
export function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/** SQLite CURRENT_TIMESTAMP ("YYYY-MM-DD HH:MM:SS", UTC) → ISO 8601. Values
 * that are already ISO pass through unchanged. */
export function sqliteTimestampToIso(timestamp: string): string {
  return timestamp.includes('T') ? timestamp : `${timestamp.replace(' ', 'T')}Z`;
}

/** Display label for an action kind (`map.extract` → "Map Extract") when the
 * server supplies no action name. */
export function actionDisplayNameFromActionKind(actionKind: string): string {
  const normalized = actionKind.replace(/[._-]+/g, ' ').trim();
  if (!normalized) return 'AI run';
  return normalized
    .split(/\s+/)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
}

/** Backend cell values are real JSON. Structured renderers still receive JSON
 * strings; source-bound temporal values keep their typed object shape. */
export function cellValueFromWire(value: unknown): CellValue {
  if (value === null || value === undefined) return null;
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
    return value;
  }
  if (
    isRecord(value)
    && typeof value.schema_version === 'string'
    && TIMELINE_SCHEMA_VERSIONS.has(value.schema_version)
  ) {
    return value;
  }
  return JSON.stringify(value);
}
