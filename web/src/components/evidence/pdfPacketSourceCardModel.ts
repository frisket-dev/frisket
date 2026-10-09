import type { EvidenceArtifact } from '../../api/open';

export interface PdfPacketSourceCardData {
  filename: string;
  pageCount: number;
  pageStart: number;
  pageEnd: number;
  otherDocumentCount: number;
  blobUrl: string;
  sourceSheetId: number | null;
}

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function integer(value: unknown): number | null {
  const parsed = typeof value === 'number' ? value : Number(value);
  return Number.isInteger(parsed) ? parsed : null;
}

export function pdfPacketSourceCardData(
  artifact: EvidenceArtifact,
): PdfPacketSourceCardData | null {
  const external = record(artifact.external_ref);
  const blob = artifact.artifact_ref.blob;
  if (external?.kind !== 'pdf_packet_split_source' || !blob) return null;
  const pageRange = artifact.spans.find((span) => record(span.selector)?.kind === 'page_range');
  const selector = record(pageRange?.selector);
  const pageStart = integer(selector?.page_start);
  const pageEnd = integer(selector?.page_end);
  const pageCount = integer(artifact.page_count);
  const siblingCount = integer(external.sibling_count);
  if (
    pageStart === null
    || pageEnd === null
    || pageCount === null
    || siblingCount === null
    || pageStart < 1
    || pageEnd < pageStart
    || pageEnd > pageCount
    || siblingCount < 0
  ) return null;
  return {
    filename: artifact.filename || blob.filename || 'Source packet.pdf',
    pageCount,
    pageStart,
    pageEnd,
    otherDocumentCount: Math.max(0, siblingCount - 1),
    blobUrl: blob.url,
    sourceSheetId: artifact.source_cell?.sheet_id ?? null,
  };
}
