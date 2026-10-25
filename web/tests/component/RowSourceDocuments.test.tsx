// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { installPopoverPolyfill } from '../support/domPolyfills';

const mocks = vi.hoisted(() => ({
  getRowSourceDocuments: vi.fn(),
}));
const projectApi = { getRowSourceDocuments: mocks.getRowSourceDocuments };

vi.mock('../../src/bind/useWorkspaceStores', () => ({
  useWorkspaceStores: () => ({
    projectApi,
  }),
}));

import { RowSourceDocuments } from '../../src/components/rowInspection/RowSourceDocuments';

beforeAll(() => installPopoverPolyfill());
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const document = (
  sheetId: number,
  rowId: number,
  columnId: number,
  name: string,
  page: number | null = null,
) => ({
  sheet_id: sheetId,
  sheet_name: name,
  row_id: rowId,
  row_position: rowId,
  column_id: columnId,
  column_name: 'document',
  column_type: 'file' as const,
  filename: `${name.toLowerCase().replace(' documents', '')}.pdf`,
  page,
});

describe('row source documents', () => {
  it('offers every discovered original in the shared menu and opens the chosen document', async () => {
    mocks.getRowSourceDocuments.mockResolvedValue({
      schema_version: 'frisket.row_source_documents.v1',
      sheet_id: 9,
      row_id: 90,
      ancestry_found: true,
      lineage_stale: false,
      unavailable_source_count: 0,
      truncated: false,
      evidence_links: [],
      documents: [document(1, 11, 101, 'Left documents'), document(2, 22, 202, 'Right documents', 3)],
    });
    const onOpenDocument = vi.fn();
    render(<RowSourceDocuments rowId="90" onOpenDocument={onOpenDocument} />);

    const trigger = await screen.findByTestId('row-open-source-documents');
    expect(trigger).toHaveTextContent('Open source documents (2)');
    fireEvent.click(trigger);
    const choice = await screen.findByRole('menuitem', { name: 'right.pdf · Page 3 · Right documents · Row 22' });
    fireEvent.click(choice);
    expect(onOpenDocument).toHaveBeenCalledWith('2', '22', '202', 3);
  });

  it('keeps explicit evidence and document fallback visible together', async () => {
    mocks.getRowSourceDocuments.mockResolvedValue({
      schema_version: 'frisket.row_source_documents.v1',
      sheet_id: 9,
      row_id: 90,
      ancestry_found: true,
      lineage_stale: false,
      unavailable_source_count: 1,
      truncated: true,
      evidence_links: [{
        id: 4,
        stable_id: 'evidence_link:4',
        export_ref: 'evidence_link:4',
        status: 'active',
        role: 'primary_support',
        evidence_kind: 'text',
        span_count: 1,
        artifact_count: 1,
        snippet: 'exact quoted text',
        viewer_href: '/viewer',
      }],
      documents: [document(1, 11, 101, 'Documents')],
    });
    render(<RowSourceDocuments rowId="90" onOpenDocument={() => {}} />);

    expect(await screen.findByText('Open evidence')).toBeInTheDocument();
    expect(screen.getByTestId('row-open-source-document')).toBeInTheDocument();
    expect(screen.getByTestId('row-source-documents-unavailable')).toHaveTextContent('1 source row is unavailable');
    expect(screen.getByTestId('row-source-documents-truncated')).toBeInTheDocument();
  });

  it('distinguishes no ancestry from ancestry with no readable source', async () => {
    mocks.getRowSourceDocuments.mockResolvedValueOnce({
      schema_version: 'frisket.row_source_documents.v1', sheet_id: 1, row_id: 1,
      ancestry_found: false, lineage_stale: false, unavailable_source_count: 0, truncated: false,
      evidence_links: [], documents: [],
    });
    const view = render(<RowSourceDocuments rowId="1" onOpenDocument={() => {}} />);
    await waitFor(() => expect(mocks.getRowSourceDocuments).toHaveBeenCalled());
    expect(screen.queryByTestId('row-source-documents')).not.toBeInTheDocument();

    mocks.getRowSourceDocuments.mockResolvedValueOnce({
      schema_version: 'frisket.row_source_documents.v1', sheet_id: 2, row_id: 2,
      ancestry_found: true, lineage_stale: false, unavailable_source_count: 0, truncated: false,
      evidence_links: [], documents: [],
    });
    view.rerender(<RowSourceDocuments rowId="2" onOpenDocument={() => {}} />);
    expect(await screen.findByTestId('row-source-documents-empty')).toHaveTextContent('No readable source document found');
  });

  it('warns when stale source lineage may be incomplete without hiding valid survivors', async () => {
    mocks.getRowSourceDocuments.mockResolvedValue({
      schema_version: 'frisket.row_source_documents.v1', sheet_id: 9, row_id: 90,
      ancestry_found: true, lineage_stale: true, unavailable_source_count: 0, truncated: false,
      evidence_links: [], documents: [document(2, 22, 202, 'Right documents')],
    });
    render(<RowSourceDocuments rowId="90" onOpenDocument={() => {}} />);
    expect(await screen.findByTestId('row-source-documents-stale')).toHaveTextContent(
      'some source links may be missing',
    );
    expect(screen.getByTestId('row-open-source-document')).toBeInTheDocument();
  });

  it('aborts a superseded row request', async () => {
    let firstSignal: AbortSignal | undefined;
    mocks.getRowSourceDocuments
      .mockImplementationOnce((_rowId, options) => {
        firstSignal = options?.signal;
        return new Promise(() => {});
      })
      .mockResolvedValueOnce({
        schema_version: 'frisket.row_source_documents.v1', sheet_id: 2, row_id: 2,
        ancestry_found: false, lineage_stale: false, unavailable_source_count: 0, truncated: false,
        evidence_links: [], documents: [],
      });
    const view = render(<RowSourceDocuments rowId="1" onOpenDocument={() => {}} />);
    expect(firstSignal?.aborted).toBe(false);
    view.rerender(<RowSourceDocuments rowId="2" onOpenDocument={() => {}} />);
    expect(firstSignal?.aborted).toBe(true);
    await waitFor(() => expect(mocks.getRowSourceDocuments).toHaveBeenCalledTimes(2));
  });

});
