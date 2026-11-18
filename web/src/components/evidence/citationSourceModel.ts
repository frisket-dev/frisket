import type { EvidenceArtifact, EvidenceSpan } from '../../api/open';

// Shared evidence selectors and display coordinates; rendering stays with each source owner.
export function finiteNumber(value: unknown): number | null {
  const n = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(n) ? n : null;
}

export function jsonRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

export function selectorNumber(span: EvidenceSpan | undefined, field: string): number | null {
  return finiteNumber(span === undefined ? undefined : jsonRecord(span.selector)?.[field]);
}

export function formatMs(ms: number): string {
  const totalSeconds = Math.max(0, Math.round(ms / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${String(seconds).padStart(2, '0')}`;
}

export function pageAnchorId(artifactStableId: string, page: number): string {
  return `evidence-page-${artifactStableId}-${page}`;
}

const TEXT_RENDERABLE_MEDIA_TYPES = new Set([
  'text/plain',
  'text/markdown',
  'application/vnd.frisket.row+json',
]);

export function isTextRenderableMediaType(mediaType: string): boolean {
  return TEXT_RENDERABLE_MEDIA_TYPES.has(mediaType.split(';')[0].trim());
}

export function textCitedSpans(artifact: EvidenceArtifact): EvidenceSpan[] {
  const spans: EvidenceSpan[] = [];
  for (const span of artifact.spans) {
    if (span.span_kind !== 'text' && span.span_kind !== 'whole') continue;
    const text = span.quote || span.snippet;
    if (!text) continue;
    spans.push(span);
  }
  return spans;
}
