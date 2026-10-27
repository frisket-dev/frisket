// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import { GridCellKind } from '@glideapps/glide-data-grid';
import { activateSourceLink, openSourceLink, parseSourceLink } from '../../src/components/evidence/sourceLink';
import { buildCell } from '../../src/grid/cells';
import { parsePathname } from '../../src/routes';
import { routeToRouteState } from '../../src/core/route/locationMatchesRouteState';
import type { EvidenceViewerPayload, Row } from '../../src/api/types';
import { artifactRef, blobRef, evidenceArtifact, evidenceSpan } from '../support/evidenceFixtures';

function payload(): EvidenceViewerPayload {
  return {
    schema_version: 'frisket.evidence_viewer.v1',
    link: { id: 1, stable_id: 'link:one', role: 'source_provenance' },
    artifacts: [evidenceArtifact({
      media_type: 'application/pdf', page_count: 8,
      source_cell: { sheet_id: 2, row_id: 4, column_id: 6 },
      artifact_ref: artifactRef({ blob: blobRef('/api/original.pdf') }),
      spans: [evidenceSpan({ selector: { kind: 'page_range', page_start: 3, page_end: 5 } })],
    })],
  } as EvidenceViewerPayload;
}

describe('source links', () => {
  it('parses typed relative routes and preserves normal external URI behavior', () => {
    expect(parseSourceLink('/p/p1/evidence/link%3Aone?page=3')).toEqual({ projectId: 'p1', evidenceLinkId: 'link:one', page: 3 });
    for (const href of ['https://example.com/p/p1/evidence/id', '//evil/p/p1/evidence/id', '/p/p1/evidence/id?page=0', '/p/p1/evidence/id?page=2&page=3', '/p/p1/evidence/%ZZ']) {
      expect(parseSourceLink(href)).toBeNull();
    }
    expect(activateSourceLink('/p/other/evidence/link', 'p1')).toBe(false);
    const event = vi.fn();
    window.addEventListener('frisket:open-evidence', event);
    expect(activateSourceLink('/p/p1/evidence/link%3Aone?page=3', 'p1')).toBe(true);
    expect(event.mock.calls[0][0].detail).toEqual({ evidenceLinkId: 'link:one', sourceLink: true, page: 3 });
    window.removeEventListener('frisket:open-evidence', event);
    const route = parsePathname('/p/p1/evidence/link%3Aone');
    expect(route).toEqual({ kind: 'project', projectId: 'p1', sheetId: undefined, panel: { kind: 'evidence', evidenceLinkId: 'link:one' } });
    expect(routeToRouteState(route)).toBeNull();
  });

  it('reuses URI display labels without changing the stored/copied link', () => {
    const row = { id: '7', index: 0, cells: { '1': '/p/p1/evidence/link%3Aone?page=3' }, provenance: {}, sourceLinks: { '1': { label: 'Original.pdf · p 3' } } } satisfies Row;
    const cell = buildCell({ id: '1', name: 'source', type: 'link' }, row, { projectId: 'p1' });
    expect(cell.kind).toBe(GridCellKind.Uri);
    if (cell.kind !== GridCellKind.Uri) throw new Error('expected URI');
    expect(cell.displayData).toBe('Original.pdf · p 3');
    expect(cell.data).toBe(row.cells['1']);
    expect(cell.onClickUri).toBeTypeOf('function');
  });

  it.each(['visible', 'hidden', 'changed', 'changed_page'] as const)('opens %s originals or the retained artifact with one evidence read', async (state) => {
    const evidence = payload();
    const row = { id: '4', index: 0, cells: { '6': JSON.stringify({ blob: state === 'changed' ? 'new-blob' : 'blob-hash', mime: 'application/pdf', ...(state === 'changed_page' ? { page: 7 } : {}) }) }, provenance: {} } satisfies Row;
    const api = {
      getEvidenceViewer: vi.fn().mockResolvedValue(evidence),
      getSheetRows: vi.fn().mockResolvedValue({ rows: state === 'hidden' ? [] : [row] }),
    };
    const navigation = { isCurrent: () => true, openOriginal: vi.fn(), openEvidence: vi.fn() };
    await openSourceLink({ projectId: 'p1', evidenceLinkId: 'link:one', page: 3 }, api, navigation);
    expect(api.getEvidenceViewer).toHaveBeenCalledTimes(1);
    expect(api.getSheetRows).toHaveBeenCalledTimes(1);
    if (state === 'visible') expect(navigation.openOriginal).toHaveBeenCalledWith('2', '4', '6', 3);
    else expect(navigation.openEvidence).toHaveBeenCalledWith(evidence, 3);
  });

  it('does not navigate after another source supersedes the request', async () => {
    const api = { getEvidenceViewer: vi.fn().mockResolvedValue(payload()), getSheetRows: vi.fn() };
    const navigation = { isCurrent: () => false, openOriginal: vi.fn(), openEvidence: vi.fn() };
    await openSourceLink({ projectId: 'p1', evidenceLinkId: 'link:one' }, api, navigation);
    expect(api.getSheetRows).not.toHaveBeenCalled();
    expect(navigation.openEvidence).not.toHaveBeenCalled();
  });
});
