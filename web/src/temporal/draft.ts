import type { CellValue } from '../api/types';
import {
  formatTimecode,
  parseTimeInput,
  parseTimelineValue,
  type TemporalColumnType,
  type TimelinePointItem,
  type TimelineRangeItem,
  type TimelineValue,
} from './model';

export interface ParsedPointDraft {
  id: string | number;
  at_ms: number;
  label?: string;
}

export interface ParsedRangeDraft {
  id: number;
  start_ms: number;
  end_ms: number;
  label?: string;
}

export type PointDraftResult =
  | { ok: true; items: ParsedPointDraft[] }
  | { ok: false; error: string };

export type RangeDraftResult =
  | { ok: true; items: ParsedRangeDraft[] }
  | { ok: false; error: string };

interface PastedRow {
  line: number;
  fields: string[];
}

type PastedRowsResult =
  | { ok: true; rows: PastedRow[] }
  | { ok: false; error: string };

function parseCsvFields(value: string, line: number): string[] | { error: string } {
  const fields: string[] = [];
  let field = '';
  let quoted = false;
  for (let index = 0; index < value.length; index += 1) {
    const character = value[index];
    if (character === '"') {
      if (quoted && value[index + 1] === '"') {
        field += '"';
        index += 1;
      } else {
        quoted = !quoted;
      }
    } else if (character === ',' && !quoted) {
      const srtDecimalComma = /^\d+:[0-5]\d:[0-5]\d$/.test(field.trim()) &&
        /^\d+(?:,|$)/.test(value.slice(index + 1));
      if (srtDecimalComma) {
        field += character;
      } else {
        fields.push(field.trim());
        field = '';
      }
    } else {
      field += character;
    }
  }
  if (quoted) return { error: `Line ${line}: close the quoted CSV field.` };
  fields.push(field.trim());
  return fields;
}

function pastedRows(value: string): PastedRowsResult {
  const rows: PastedRow[] = [];
  for (const [index, rawLine] of value.split(/\r?\n/).entries()) {
    if (!rawLine.trim()) continue;
    const line = index + 1;
    if (rawLine.includes('\t')) {
      rows.push({ line, fields: rawLine.split('\t').map((field) => field.trim()) });
      continue;
    }
    if (rawLine.includes(',')) {
      const fields = parseCsvFields(rawLine, line);
      if (!Array.isArray(fields)) return { ok: false, error: fields.error };
      rows.push({ line, fields });
      continue;
    }
    rows.push({ line, fields: [rawLine.trim()] });
  }
  return { ok: true, rows };
}

/** Parse the paste-oriented Split timestamp editor.
 *
 * Each non-empty line is one timestamp, optionally followed by a label in a
 * second CSV or TSV column. Keeping one observation per line avoids guessing
 * whether `00:10,00:20` means two cuts or one cut whose label looks like a
 * time. Quoted CSV labels may contain commas.
 */
export function parsePointDraftText(value: string): PointDraftResult {
  const parsedRows = pastedRows(value);
  if (!parsedRows.ok) return parsedRows;
  if (parsedRows.rows.length === 0) {
    return { ok: false, error: 'Add at least one timestamp.' };
  }
  const items: ParsedPointDraft[] = [];
  for (const row of parsedRows.rows) {
    if (row.fields.length > 2) {
      return {
        ok: false,
        error: `Line ${row.line}: use one timestamp and, optionally, one label column.`,
      };
    }
    const at = parseTimeInput(row.fields[0] ?? '');
    if (!at.ok) return { ok: false, error: `Line ${row.line}: ${at.error}` };
    const label = row.fields[1]?.trim() ?? '';
    items.push({
      id: row.line,
      at_ms: at.ms,
      ...(label ? { label } : {}),
    });
  }
  return { ok: true, items };
}

/** Parse the paste-oriented Split range editor.
 *
 * Each non-empty line is `start,end[,label]` CSV or
 * `start<TAB>end[<TAB>label]` TSV. CSV quoting is supported for labels.
 */
export function parseRangeDraftText(value: string): RangeDraftResult {
  const parsedRows = pastedRows(value);
  if (!parsedRows.ok) return parsedRows;
  if (parsedRows.rows.length === 0) {
    return { ok: false, error: 'Add at least one time range.' };
  }
  const items: ParsedRangeDraft[] = [];
  for (const row of parsedRows.rows) {
    if (row.fields.length < 2 || row.fields.length > 3) {
      return {
        ok: false,
        error: `Line ${row.line}: use start, end, and an optional label column.`,
      };
    }
    const start = parseTimeInput(row.fields[0] ?? '');
    const end = parseTimeInput(row.fields[1] ?? '');
    if (!start.ok) return { ok: false, error: `Line ${row.line} start: ${start.error}` };
    if (!end.ok) return { ok: false, error: `Line ${row.line} end: ${end.error}` };
    if (start.ms >= end.ms) {
      return { ok: false, error: `Line ${row.line}: end must be after start.` };
    }
    const label = row.fields[2]?.trim() ?? '';
    items.push({
      id: row.line,
      start_ms: start.ms,
      end_ms: end.ms,
      ...(label ? { label } : {}),
    });
  }
  return { ok: true, items };
}

export type EditableTimelineType = TemporalColumnType;

function draftLabel(value: string | null | undefined): string {
  return (value ?? '').replace(/\r?\n|\t/g, ' ').trim();
}

function csvLabel(value: string | null | undefined): string {
  const oneLine = draftLabel(value);
  if (!oneLine) return '';
  return /[",\t]/.test(oneLine)
    ? `"${oneLine.replace(/"/g, '""')}"`
    : oneLine;
}

export function formatPointDraftText(items: readonly TimelinePointItem[]): string {
  return items.map((item) => {
    const label = csvLabel(item.label);
    return `${formatTimecode(item.at_ms)}${label ? `,${label}` : ''}`;
  }).join('\n');
}

export function formatRangeDraftText(items: readonly TimelineRangeItem[]): string {
  return items.map((item) => {
    const label = csvLabel(item.label);
    return `${formatTimecode(item.start_ms)},${formatTimecode(item.end_ms)}${label ? `,${label}` : ''}`;
  }).join('\n');
}

/** Friendly row-drawer representation. The canonical JSON and source anchor
 * remain implementation details; reporters edit only the timestamp/range
 * items using the same CSV/TSV syntax as Split into segments. */
export function timelineItemsDraftText(
  value: CellValue,
  type: EditableTimelineType,
): string | null {
  const parsed = parseTimelineValue(value, type);
  if (!parsed) return null;
  if (parsed.schema_version === 'frisket.timeline_point.v1') {
    return formatPointDraftText([parsed.item]);
  }
  if (parsed.schema_version === 'frisket.timeline_points.v1') {
    return formatPointDraftText(parsed.items);
  }
  if (parsed.schema_version === 'frisket.timeline_range.v1') {
    return formatRangeDraftText([parsed.item]);
  }
  if (parsed.schema_version === 'frisket.timeline_ranges.v1') {
    return formatRangeDraftText(parsed.items);
  }
  return null;
}

export type ReplaceTimelineItemsResult =
  | { ok: true; value: TimelineValue }
  | { ok: false; error: string };

function itemKey(coordinates: readonly number[], label: string | null | undefined): string {
  return JSON.stringify([...coordinates, draftLabel(label)]);
}

function existingItemsByKey<Item>(
  items: readonly Item[],
  keyFor: (item: Item) => string,
): Map<string, Item[]> {
  const byKey = new Map<string, Item[]>();
  for (const item of items) {
    const key = keyFor(item);
    const matches = byKey.get(key) ?? [];
    matches.push(item);
    byKey.set(key, matches);
  }
  return byKey;
}

function manualIdFactory(existingIds: Iterable<string>): () => string {
  const used = new Set(existingIds);
  let next = 1;
  return () => {
    while (used.has(`manual-${next}`)) next += 1;
    const id = `manual-${next}`;
    used.add(id);
    next += 1;
    return id;
  };
}

/** Replace a temporal value's item(s). Schema and timeline anchor
 * are copied verbatim from the stored value, so a row edit cannot retarget a
 * timestamp to a different video/transcript. Unchanged observations retain
 * their stable id and detector metadata; only genuinely new lines receive a
 * manual id. */
export function replaceTimelineItemsFromDraft(
  value: CellValue,
  type: EditableTimelineType,
  draft: string,
): ReplaceTimelineItemsResult {
  const parsed = parseTimelineValue(value, type);
  if (!parsed) {
    return { ok: false, error: 'This temporal value has no valid source timeline.' };
  }
  const duration = parsed.timeline.duration_ms ?? undefined;
  if (type === 'timeline_point' || type === 'timeline_points') {
    const result = draft.trim()
      ? parsePointDraftText(draft)
      : type === 'timeline_points'
        ? { ok: true as const, items: [] }
        : { ok: false as const, error: 'Add exactly one timestamp.' };
    if (!result.ok) return result;
    if (type === 'timeline_point' && result.items.length !== 1) {
      return { ok: false, error: 'Use exactly one timestamp.' };
    }
    if (duration != null) {
      const outOfBounds = result.items.find((item) => item.at_ms > duration);
      if (outOfBounds) {
        return {
          ok: false,
          error: `${formatTimecode(outOfBounds.at_ms)} is after the source ends at ${formatTimecode(duration)}.`,
        };
      }
    }
    const existing = parsed.schema_version === 'frisket.timeline_point.v1'
      ? [parsed.item]
      : parsed.schema_version === 'frisket.timeline_points.v1'
        ? parsed.items
        : [];
    const byKey = existingItemsByKey(
      existing,
      (item) => itemKey([item.at_ms], item.label),
    );
    const newManualId = manualIdFactory(existing.map((item) => item.id));
    const items = result.items.map(({ at_ms, label }) => {
      const key = itemKey([at_ms], label);
      const unchanged = byKey.get(key)?.shift();
      return unchanged ?? {
        id: newManualId(),
        at_ms,
        ...(label ? { label } : {}),
      };
    });
    if (parsed.schema_version === 'frisket.timeline_point.v1') {
      return { ok: true, value: { ...parsed, item: items[0] } };
    }
    if (parsed.schema_version === 'frisket.timeline_points.v1') {
      return { ok: true, value: { ...parsed, items } };
    }
    return { ok: false, error: 'This temporal value has no valid source timeline.' };
  }

  const result = draft.trim()
    ? parseRangeDraftText(draft)
    : type === 'timeline_ranges'
      ? { ok: true as const, items: [] }
      : { ok: false as const, error: 'Add exactly one time range.' };
  if (!result.ok) return result;
  if (type === 'timeline_range' && result.items.length !== 1) {
    return { ok: false, error: 'Use exactly one time range.' };
  }
  if (duration != null) {
    const outOfBounds = result.items.find((item) => item.end_ms > duration);
    if (outOfBounds) {
      return {
        ok: false,
        error: `${formatTimecode(outOfBounds.end_ms)} is after the source ends at ${formatTimecode(duration)}.`,
      };
    }
  }
  const existing = parsed.schema_version === 'frisket.timeline_range.v1'
    ? [parsed.item]
    : parsed.schema_version === 'frisket.timeline_ranges.v1'
      ? parsed.items
      : [];
  const byKey = existingItemsByKey(
    existing,
    (item) => itemKey([item.start_ms, item.end_ms], item.label),
  );
  const newManualId = manualIdFactory(existing.map((item) => item.id));
  const items = result.items.map(({ start_ms, end_ms, label }) => {
    const key = itemKey([start_ms, end_ms], label);
    const unchanged = byKey.get(key)?.shift();
    return unchanged ?? {
      id: newManualId(),
      start_ms,
      end_ms,
      ...(label ? { label } : {}),
    };
  });
  if (parsed.schema_version === 'frisket.timeline_range.v1') {
    return { ok: true, value: { ...parsed, item: items[0] } };
  }
  if (parsed.schema_version === 'frisket.timeline_ranges.v1') {
    return { ok: true, value: { ...parsed, items } };
  }
  return { ok: false, error: 'This temporal value has no valid source timeline.' };
}
