import type { CellValue, Row, SheetMeta } from '../api/types';
import type { ResolvedMediaValue } from '../media/resolveMediaValue';

export interface TimedTranscriptSegment {
  index: number;
  start: number;
  end: number;
  text: string;
  speaker: string | null;
}

export interface TimedTranscriptDocument {
  row: Row;
  sheet: Pick<SheetMeta, 'columns'>;
  media: ResolvedMediaValue;
  kind: 'audio' | 'video';
  title: string;
  videoClassName?: string;
}

interface TimedTranscriptValue {
  text: string | null;
  segments: CellValue | undefined;
}

/** Resolve the conventional transcribe output pair from a document row once,
 *  here at the renderer boundary. Callers pass the document object; they do
 *  not need to know that the stored transcript currently spans a semantic
 *  text column plus its `{name}_segments` companion column. */
export function timedTranscriptValue(document: TimedTranscriptDocument): TimedTranscriptValue | null {
  const transcriptColumn = document.sheet.columns.find(
    (column) => column.type === 'timestamped_transcript',
  );
  if (!transcriptColumn) return null;
  const segmentsColumn = document.sheet.columns.find((column) => (
    column.name === `${transcriptColumn.name}_segments` && column.type === 'json'
  ));
  const rawText = document.row.cells[String(transcriptColumn.id)];
  return {
    text: typeof rawText === 'string' && rawText.trim() ? rawText : null,
    segments: segmentsColumn
      ? document.row.cells[String(segmentsColumn.id)]
      : undefined,
  };
}

export function hasTimedTranscript(document: TimedTranscriptDocument): boolean {
  const value = timedTranscriptValue(document);
  return value !== null && (value.text !== null || value.segments !== undefined);
}

function finiteNumber(value: unknown): number | null {
  const parsed = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
}

export function parseTimedTranscriptSegments(value: CellValue | undefined): TimedTranscriptSegment[] {
  if (value === undefined || value === null) return [];
  let parsed: unknown = value;
  if (typeof value === 'string') {
    try {
      parsed = JSON.parse(value);
    } catch {
      return [];
    }
  }
  if (!Array.isArray(parsed)) return [];
  return parsed.flatMap((item, index) => {
    if (item === null || typeof item !== 'object' || Array.isArray(item)) return [];
    const record = item as Record<string, unknown>;
    const start = finiteNumber(record.start);
    const end = finiteNumber(record.end);
    const text = typeof record.text === 'string' ? record.text.trim() : '';
    if (start === null || end === null || end < start || text.length === 0) return [];
    return [{
      index: finiteNumber(record.segment_index) ?? index,
      start,
      end,
      text,
      speaker: typeof record.speaker === 'string' && record.speaker.trim()
        ? record.speaker.trim()
        : null,
    }];
  }).sort((left, right) => left.start - right.start || left.index - right.index);
}
