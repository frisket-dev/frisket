import { validPoint, validRange, type TemporalColumnType, type TimelinePointItem, type TimelineRangeItem } from './model';

/** Display-only envelope. Never accepted by parseTimelineValue or draft binding. */
export interface PreviewTemporalValue {
  schema_version: 'frisket.preview_temporal.v1';
  column_type: TemporalColumnType;
  timeline: { preview_clock_id?: string; artifact_stable_id?: string; fingerprint: string; duration_ms: number };
  item?: TimelinePointItem | TimelineRangeItem;
  items?: Array<TimelinePointItem | TimelineRangeItem>;
}

export function parsePreviewTemporalValue(value: unknown, expectedType?: string): PreviewTemporalValue | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const v = value as Partial<PreviewTemporalValue>;
  if (v.schema_version !== 'frisket.preview_temporal.v1'
    || !['timeline_point', 'timeline_points', 'timeline_range', 'timeline_ranges'].includes(v.column_type ?? '')
    || (expectedType !== undefined && v.column_type !== expectedType)) return null;
  const clock = v.timeline;
  if (!clock || !Number.isSafeInteger(clock.duration_ms) || clock.duration_ms <= 0
    || !/^sha256:[0-9a-f]{64}$/.test(clock.fingerprint ?? '')) return null;
  const scratch = typeof clock.preview_clock_id === 'string' && /^[0-9a-f]{32}$/.test(clock.preview_clock_id);
  const stored = typeof clock.artifact_stable_id === 'string' && /^source_artifact:[A-Za-z0-9-]+$/.test(clock.artifact_stable_id);
  if (scratch === stored || (scratch && clock.artifact_stable_id !== undefined)
    || (stored && clock.preview_clock_id !== undefined)) return null;
  const multiple = v.column_type === 'timeline_points' || v.column_type === 'timeline_ranges';
  if ((multiple && (v.item !== undefined || !Array.isArray(v.items)))
    || (!multiple && (v.items !== undefined || v.item === undefined))) return null;
  const items = multiple ? v.items! : [v.item!];
  const valid = v.column_type === 'timeline_point' || v.column_type === 'timeline_points' ? validPoint : validRange;
  if (!items.every((item) => valid(item, clock.duration_ms))) return null;
  return v as PreviewTemporalValue;
}
