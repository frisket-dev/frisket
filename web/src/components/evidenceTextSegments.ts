export interface TextSegment {
  text: string;
  highlighted: boolean;
  start: number;
  end: number;
}

export function textSegments(sourceText: string, ranges: readonly { start: number; end: number }[]): TextSegment[] {
  if (sourceText.length === 0) return [];
  const ordered = ranges
    .filter((range) => Number.isInteger(range.start) && Number.isInteger(range.end)
      && range.start >= 0 && range.start < range.end && range.end <= sourceText.length)
    .slice()
    .sort((a, b) => a.start - b.start || b.end - a.end);
  const merged: Array<{ start: number; end: number }> = [];
  for (const range of ordered) {
    const last = merged[merged.length - 1];
    // Overlaps are one visual mark. Adjacent, separately cited passages stay
    // separate so the renderer does not erase their boundary.
    if (last && range.start < last.end) {
      last.end = Math.max(last.end, range.end);
    } else {
      merged.push({ start: range.start, end: range.end });
    }
  }
  if (merged.length === 0) return [{ text: sourceText, highlighted: false, start: 0, end: sourceText.length }];
  const segments: TextSegment[] = [];
  let cursor = 0;
  for (const range of merged) {
    if (range.start > cursor) {
      segments.push({ text: sourceText.slice(cursor, range.start), highlighted: false, start: cursor, end: range.start });
    }
    segments.push({ text: sourceText.slice(range.start, range.end), highlighted: true, start: range.start, end: range.end });
    cursor = range.end;
  }
  if (cursor < sourceText.length) {
    segments.push({ text: sourceText.slice(cursor), highlighted: false, start: cursor, end: sourceText.length });
  }
  return segments;
}

