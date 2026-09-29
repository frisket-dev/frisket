import type { EvidenceArtifact, EvidenceSpan, EvidenceViewerPayload } from '../../api/types';

export interface ReviewCitationPayload { fieldId: string; payload: EvidenceViewerPayload; }
interface SourceMember { fieldId: string; artifact: EvidenceArtifact; spans: EvidenceSpan[]; }
export interface ReviewCitationSource {
  id: string; artifact: EvidenceArtifact; fieldIds: string[]; members: SourceMember[];
  kind: 'MD' | 'PDF' | 'AUDIO' | 'FILE'; title: string;
}

/** Groups full-fidelity citations by persisted source-artifact identity. */
export function reviewCitationSources(payloads: readonly ReviewCitationPayload[]): ReviewCitationSource[] {
  const bySource = new Map<string, SourceMember[]>();
  for (const { fieldId, payload } of payloads) {
    if (payload.link.role === 'source_provenance') continue;
    for (const artifact of payload.artifacts) {
      const spans = citedSpans(artifact);
      if (spans.length === 0) continue;
      const members = bySource.get(artifact.stable_id) ?? [];
      members.push({ fieldId, artifact, spans });
      bySource.set(artifact.stable_id, members);
    }
  }
  return [...bySource].map(([id, members]) => ({
    id, artifact: mergeArtifacts(members), fieldIds: [...new Set(members.map((member) => member.fieldId))],
    members, kind: sourceKind(members[0].artifact.media_type), title: sourceTitle(members[0].artifact),
  }));
}

export function sourcesForField(sources: readonly ReviewCitationSource[], fieldId: string | undefined): ReviewCitationSource[] {
  return fieldId === undefined ? [] : sources.filter((source) => source.fieldIds.includes(fieldId));
}

export function sourceLocation(source: ReviewCitationSource, fieldId: string | undefined): string | null {
  const span = (source.members.find((item) => item.fieldId === fieldId) ?? source.members[0])?.spans[0];
  if (!span) return null;
  const selector = record(span.selector);
  const page = finite(selector?.page_start);
  if (page !== null) return `p. ${page}`;
  const startMs = finite(selector?.start_ms);
  if (startMs !== null) return formatMs(startMs);
  // UTF-16 offsets are exact, but the public payload carries no paragraph locator.
  return null;
}

function citedSpans(artifact: EvidenceArtifact): EvidenceSpan[] {
  const active = artifact.spans.filter((span) => span.status === 'active');
  const required = active.filter((span) => span.required);
  return required.length > 0 ? required : active;
}

function mergeArtifacts(members: readonly SourceMember[]): EvidenceArtifact {
  const first = members[0].artifact;
  const spanIds = new Set(members.flatMap((member) => member.spans.map((span) => span.stable_id)));
  const spans = uniqueBy(members.flatMap((member) => member.spans), (span) => span.stable_id);
  const pages = uniqueBy(members.flatMap((member) => member.artifact.pages), (page) => String(page.page)).map((page) => ({
    ...page,
    regions: uniqueBy(members.flatMap((member) => member.artifact.pages)
      .filter((candidate) => candidate.page === page.page).flatMap((candidate) => candidate.regions),
    (region) => region.stable_id).filter((region) => spanIds.has(region.stable_id)),
  }));
  const textContext = mergeTextContext(members, spanIds);
  return {
    ...first, spans, pages,
    runs: uniqueBy(members.flatMap((member) => member.artifact.runs), (run) => `${run.index}:${run.span_ids.join(':')}`),
    ...(textContext === null ? {} : { text_context: textContext }),
  };
}

function mergeTextContext(members: readonly SourceMember[], spanIds: ReadonlySet<string>) {
  const first = members.map((member) => member.artifact.text_context).find((context) => context !== null && context !== undefined);
  if (!first) return null;
  const ranges = uniqueBy(members.flatMap((member) => member.artifact.text_context?.text === first.text
    ? member.artifact.text_context.ranges.filter((range) => spanIds.has(range.span_id)) : []),
  (range) => `${range.span_id}:${range.start}:${range.end}`);
  return { ...first, ranges };
}

function uniqueBy<T>(items: readonly T[], key: (item: T) => string): T[] {
  const seen = new Set<string>();
  return items.filter((item) => { const value = key(item); if (seen.has(value)) return false; seen.add(value); return true; });
}
function sourceKind(mediaType: string): ReviewCitationSource['kind'] {
  const type = mediaType.split(';')[0].trim();
  if (type === 'application/pdf') return 'PDF';
  if (type.startsWith('audio/') || type.startsWith('video/')) return 'AUDIO';
  if (type === 'text/markdown' || type === 'application/vnd.frisket.row+json') return 'MD';
  return 'FILE';
}
function sourceTitle(artifact: EvidenceArtifact): string {
  return artifact.title || artifact.filename || artifact.artifact_ref.blob?.filename || artifact.media_type;
}
function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null;
}
function finite(value: unknown): number | null { const number = typeof value === 'number' ? value : Number(value); return Number.isFinite(number) ? number : null; }
function formatMs(ms: number): string { const seconds = Math.max(0, Math.round(ms / 1000)); return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`; }
