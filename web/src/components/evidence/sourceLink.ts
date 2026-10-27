import type { EvidenceViewerPayload } from '../../api/open';
import type { ProjectApiPort } from '../../api/ports';
import { resolveMediaValue } from '../../media/resolveMediaValue';
import { jsonRecord, selectorNumber } from './evidenceSourceModel';

export interface SourceLink {
  projectId: string;
  evidenceLinkId: string;
  page?: number;
}

/** Source values are ordinary links; only this exact local route is internal. */
export function parseSourceLink(value: string): SourceLink | null {
  if (!value.startsWith('/p/')) return null;
  const match = /^\/p\/([^/?#]+)\/evidence\/([^/?#]+)(?:\?([^#]*))?$/.exec(value);
  if (!match) return null;
  try {
    const projectId = decodeURIComponent(match[1]);
    const evidenceLinkId = decodeURIComponent(match[2]);
    if (!projectId || !evidenceLinkId || [...projectId + evidenceLinkId]
      .some((character) => character === '/' || character.charCodeAt(0) < 32)) return null;
    const query = new URLSearchParams(match[3]);
    const rawPage = query.get('page');
    if ([...query.keys()].some((key) => key !== 'page') || query.getAll('page').length > 1) return null;
    const page = rawPage === null ? undefined : Number(rawPage);
    if (page !== undefined && (!/^[1-9]\d*$/.test(rawPage!) || !Number.isSafeInteger(page))) return null;
    return { projectId, evidenceLinkId, ...(page === undefined ? {} : { page }) };
  } catch {
    return null;
  }
}

export function activateSourceLink(value: string, projectId: string): boolean {
  const source = parseSourceLink(value);
  if (!source || source.projectId !== projectId) return false;
  window.dispatchEvent(new CustomEvent('frisket:open-evidence', {
    detail: { evidenceLinkId: source.evidenceLinkId, sourceLink: true, page: source.page },
  }));
  return true;
}

/** Use the pinned source artifact, not the child row identified by the link. */
export function sourceLinkArtifact(payload: EvidenceViewerPayload, page?: number) {
  if (payload.link.role !== 'source_provenance') return null;
  return payload.artifacts.find((artifact) => artifact.media_type === 'application/pdf'
    && artifact.artifact_ref.blob && (page === undefined || artifact.page_count === null || page <= artifact.page_count)) ?? null;
}

export async function openSourceLink(
  source: SourceLink,
  api: Pick<ProjectApiPort, 'getEvidenceViewer' | 'getSheetRows'>,
  navigation: {
    isCurrent(): boolean;
    openOriginal(sheetId: string, rowId: string, columnId: string, page?: number): void;
    openEvidence(payload: EvidenceViewerPayload, page?: number): void;
  },
): Promise<void> {
  const payload = await api.getEvidenceViewer(source.evidenceLinkId);
  if (!navigation.isCurrent()) return;
  const artifact = sourceLinkArtifact(payload, source.page);
  const historical = jsonRecord(jsonRecord(artifact?.external_ref)?.original_source_cell);
  const cell = artifact?.source_cell ?? (historical ? {
    sheet_id: Number(historical.sheet_id), row_id: Number(historical.row_id), column_id: Number(historical.column_id),
  } : null);
  const page = source.page ?? (artifact?.spans[0] ? selectorNumber(artifact.spans[0], 'page_start') ?? undefined : undefined);
  if (cell?.sheet_id && cell.row_id && cell.column_id) {
    try {
      const result = await api.getSheetRows(String(cell.sheet_id), 0, 1, {
        rowIds: [cell.row_id], columnIds: [String(cell.column_id)],
      });
      const row = result.rows.find((candidate) => candidate.id === String(cell.row_id));
      const media = row && resolveMediaValue(row.cells[String(cell.column_id)], source.projectId);
      let selectedPage: unknown;
      try { selectedPage = JSON.parse(String(row?.cells[String(cell.column_id)] ?? 'null'))?.page; } catch { /* ordinary non-file values fall back */ }
      if (!navigation.isCurrent()) return;
      if (media && media.blobHash === artifact?.artifact_ref.blob?.hash
        && (selectedPage === undefined || selectedPage === null || selectedPage === page)) {
        navigation.openOriginal(String(cell.sheet_id), String(cell.row_id), String(cell.column_id), page);
        return;
      }
    } catch {
      // A hidden/deleted row cannot invalidate its retained artifact.
    }
  }
  if (navigation.isCurrent()) navigation.openEvidence(payload, page);
}
