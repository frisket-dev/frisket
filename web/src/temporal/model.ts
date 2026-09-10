import type { CellValue, ColumnType } from '../api/types';

export type TemporalColumnType =
  | 'timeline_point'
  | 'timeline_points'
  | 'timeline_range'
  | 'timeline_ranges';

export interface TimelineAnchor {
  artifact_stable_id: string;
  fingerprint: string;
  duration_ms?: number | null;
}

export interface TimelinePointItem {
  id: string;
  at_ms: number;
  label?: string | null;
  metadata?: Record<string, unknown>;
}

export interface TimelineRangeItem {
  id: string;
  start_ms: number;
  end_ms: number;
  label?: string | null;
  metadata?: Record<string, unknown>;
}

export type TimelineValue =
  | {
      schema_version: 'frisket.timeline_point.v1';
      timeline: TimelineAnchor;
      item: TimelinePointItem;
    }
  | {
      schema_version: 'frisket.timeline_points.v1';
      timeline: TimelineAnchor;
      items: TimelinePointItem[];
    }
  | {
      schema_version: 'frisket.timeline_range.v1';
      timeline: TimelineAnchor;
      item: TimelineRangeItem;
    }
  | {
      schema_version: 'frisket.timeline_ranges.v1';
      timeline: TimelineAnchor;
      items: TimelineRangeItem[];
    };

export type TimelinePointsValue = Extract<
  TimelineValue,
  { schema_version: 'frisket.timeline_points.v1' }
>;

export type DraftTemporalSelection =
  | { kind: 'draft_range'; start_ms: number; end_ms: number; label?: string }
  | { kind: 'draft_points'; items: Array<{ at_ms: number; label?: string }> }
  | {
      kind: 'draft_ranges';
      items: Array<{ start_ms: number; end_ms: number; label?: string }>;
    }
  | { kind: 'column'; column: string }
  | {
    kind: 'typed_value';
    value: TimelineValue;
    origin_receipt_id?: string;
    draft_revision?: number;
    draft_hash?: string;
  };

export type TimeParseResult =
  | { ok: true; ms: number }
  | { ok: false; error: string };

const TEMPORAL_TYPES = new Set<TemporalColumnType>([
  'timeline_point',
  'timeline_points',
  'timeline_range',
  'timeline_ranges',
]);

const SCHEMA_FOR_TYPE: Record<TemporalColumnType, TimelineValue['schema_version']> = {
  timeline_point: 'frisket.timeline_point.v1',
  timeline_points: 'frisket.timeline_points.v1',
  timeline_range: 'frisket.timeline_range.v1',
  timeline_ranges: 'frisket.timeline_ranges.v1',
};

const TIME_INPUT_HELP = 'Use seconds (90 or 90.5), a timecode such as 01:30, or an explicit unit such as 90000ms.';

export function isTemporalColumnType(type: ColumnType): type is TemporalColumnType {
  return TEMPORAL_TYPES.has(type as TemporalColumnType);
}

function safeMilliseconds(value: number): boolean {
  return Number.isSafeInteger(value) && value >= 0;
}

function decimalMilliseconds(raw: string): number {
  if (!raw) return 0;
  return Number(raw.padEnd(3, '0'));
}

function scaledDecimalMilliseconds(
  raw: string,
  multiplier: number,
): TimeParseResult {
  const [wholeRaw, fractionRaw = ''] = raw.split('.');
  const whole = Number(wholeRaw);
  if (!Number.isSafeInteger(whole) || whole > Math.floor(Number.MAX_SAFE_INTEGER / multiplier)) {
    return { ok: false, error: 'That time is too large.' };
  }
  const scale = 10 ** fractionRaw.length;
  const fractionalNumerator = Number(fractionRaw || '0') * multiplier;
  if (!Number.isSafeInteger(fractionalNumerator) || fractionalNumerator % scale !== 0) {
    return { ok: false, error: 'That value does not resolve to a whole millisecond.' };
  }
  const ms = whole * multiplier + fractionalNumerator / scale;
  return safeMilliseconds(ms)
    ? { ok: true, ms }
    : { ok: false, error: 'That time is too large.' };
}

/** Parse friendly time input into canonical milliseconds.
 *
 * Reporter-facing bare numbers mean seconds everywhere in this suite. The
 * persisted value remains explicitly millisecond-typed, and imports that
 * already carry milliseconds can say `ms`.
 */
export function parseTimeInput(raw: string): TimeParseResult {
  const value = raw.trim();
  if (!value) return { ok: false, error: 'Enter a time.' };

  const hms = /^(\d+):([0-5]\d):([0-5]\d)(?:[.,](\d{1,3}))?$/.exec(value);
  if (hms) {
    const ms =
      Number(hms[1]) * 3_600_000 +
      Number(hms[2]) * 60_000 +
      Number(hms[3]) * 1_000 +
      decimalMilliseconds(hms[4] ?? '');
    return safeMilliseconds(ms)
      ? { ok: true, ms }
      : { ok: false, error: 'That time is too large.' };
  }

  const msTime = /^(\d+):([0-5]\d)(?:\.(\d{1,3}))?$/.exec(value);
  if (msTime) {
    const ms =
      Number(msTime[1]) * 60_000 +
      Number(msTime[2]) * 1_000 +
      decimalMilliseconds(msTime[3] ?? '');
    return safeMilliseconds(ms)
      ? { ok: true, ms }
      : { ok: false, error: 'That time is too large.' };
  }

  const withUnit = /^(\d+(?:\.\d{1,3})?)\s*(ms|s|m|h)?$/i.exec(value);
  if (withUnit) {
    const multipliers = { ms: 1, s: 1_000, m: 60_000, h: 3_600_000 } as const;
    const unit = (withUnit[2]?.toLowerCase() ?? 's') as keyof typeof multipliers;
    return scaledDecimalMilliseconds(withUnit[1], multipliers[unit]);
  }

  return { ok: false, error: TIME_INPUT_HELP };
}

export function formatTimecode(milliseconds: number): string {
  const ms = Math.max(0, Math.trunc(milliseconds));
  const hours = Math.floor(ms / 3_600_000);
  const minutes = Math.floor((ms % 3_600_000) / 60_000);
  const seconds = Math.floor((ms % 60_000) / 1_000);
  const remainder = ms % 1_000;
  const secondsPart = `${String(seconds).padStart(2, '0')}${
    remainder ? `.${String(remainder).padStart(3, '0')}` : ''
  }`;
  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, '0')}:${secondsPart}`;
  }
  return `${minutes}:${secondsPart}`;
}

function objectValue(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function parseCellJson(value: CellValue): unknown {
  if (typeof value !== 'string') return value;
  try {
    return JSON.parse(value) as unknown;
  } catch {
    return null;
  }
}

function validAnchor(value: unknown): value is TimelineAnchor {
  const anchor = objectValue(value);
  if (!anchor) return false;
  if (typeof anchor.artifact_stable_id !== 'string' || !anchor.artifact_stable_id.trim()) {
    return false;
  }
  if (typeof anchor.fingerprint !== 'string' || !anchor.fingerprint.trim()) return false;
  return anchor.duration_ms === undefined || anchor.duration_ms === null || (
    safeMilliseconds(anchor.duration_ms as number) && Number(anchor.duration_ms) > 0
  );
}

export function validPoint(value: unknown, duration?: number): value is TimelinePointItem {
  const point = objectValue(value);
  if (!point || typeof point.id !== 'string' || !point.id.trim()) return false;
  if (!safeMilliseconds(point.at_ms as number)) return false;
  if (duration !== undefined && Number(point.at_ms) > duration) return false;
  return point.label === undefined || point.label === null || typeof point.label === 'string';
}

export function validRange(value: unknown, duration?: number): value is TimelineRangeItem {
  const range = objectValue(value);
  if (!range || typeof range.id !== 'string' || !range.id.trim()) return false;
  if (!safeMilliseconds(range.start_ms as number) || !safeMilliseconds(range.end_ms as number)) {
    return false;
  }
  if (Number(range.start_ms) >= Number(range.end_ms)) return false;
  if (duration !== undefined && Number(range.end_ms) > duration) return false;
  return range.label === undefined || range.label === null || typeof range.label === 'string';
}

/** Strict-enough display parser. Persistence/action execution remains the
 * authoritative contextual validator because only the server can resolve an
 * artifact and verify its fingerprint. */
export function parseTimelineValue(
  value: CellValue,
  expectedType: TemporalColumnType,
): TimelineValue | null {
  const parsed = objectValue(parseCellJson(value));
  if (!parsed || parsed.schema_version !== SCHEMA_FOR_TYPE[expectedType]) return null;
  if (!validAnchor(parsed.timeline)) return null;
  const duration = parsed.timeline.duration_ms ?? undefined;

  if (expectedType === 'timeline_point') {
    if (!validPoint(parsed.item, duration)) return null;
  } else if (expectedType === 'timeline_points') {
    if (!Array.isArray(parsed.items) || !parsed.items.every((item) => validPoint(item, duration))) {
      return null;
    }
  } else if (expectedType === 'timeline_range') {
    if (!validRange(parsed.item, duration)) return null;
  } else if (
    !Array.isArray(parsed.items) ||
    !parsed.items.every((item) => validRange(item, duration))
  ) {
    return null;
  }

  const items = expectedType.endsWith('s') ? parsed.items : [parsed.item];
  const ids = (items as Array<{ id: string }>).map((item) => item.id);
  if (new Set(ids).size !== ids.length) return null;
  return parsed as unknown as TimelineValue;
}

export interface TemporalCellSummary {
  label: string;
  copyText: string;
  invalid: boolean;
}

export function summarizeTemporalCell(
  value: CellValue,
  type: TemporalColumnType,
): TemporalCellSummary {
  if (value === null || value === '') return { label: '', copyText: '', invalid: false };
  const parsed = parseTimelineValue(value, type);
  if (!parsed) {
    return { label: 'Invalid temporal value', copyText: String(value), invalid: true };
  }
  if (parsed.schema_version === 'frisket.timeline_point.v1') {
    const label = formatTimecode(parsed.item.at_ms);
    return { label, copyText: label, invalid: false };
  }
  if (parsed.schema_version === 'frisket.timeline_points.v1') {
    const times = parsed.items.map((item) => formatTimecode(item.at_ms));
    const label = times.length === 0
      ? 'No timestamps'
      : times.length === 1
        ? times[0]
        : `${times.length} timestamps`;
    return { label, copyText: times.join('\n'), invalid: false };
  }
  if (parsed.schema_version === 'frisket.timeline_range.v1') {
    const label = `${formatTimecode(parsed.item.start_ms)} – ${formatTimecode(parsed.item.end_ms)}`;
    return { label, copyText: label, invalid: false };
  }
  const ranges = parsed.items.map(
    (item) => `${formatTimecode(item.start_ms)} – ${formatTimecode(item.end_ms)}`,
  );
  const label = ranges.length === 0
    ? 'No time ranges'
    : ranges.length === 1
      ? ranges[0]
      : `${ranges.length} time ranges`;
  return { label, copyText: ranges.join('\n'), invalid: false };
}
