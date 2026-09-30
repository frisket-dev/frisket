/** @vitest-environment jsdom */
import '@testing-library/jest-dom/vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { CellEvidencePayload, EvidenceViewerPayload, ReviewBundle, ReviewBundleField } from '../../src/api/types';
import { evidenceArtifact, evidenceSpan } from '../support/evidenceFixtures';

const projectApi = vi.hoisted(() => ({
  getCellEvidence: vi.fn(),
  getEvidenceViewer: vi.fn(),
}));

vi.mock('../../src/bind/useWorkspaceStores', () => ({
  useWorkspaceStores: () => ({ projectApi }),
}));
vi.mock('../../src/components/EvidenceViewer', () => ({
  ArtifactSource: ({ artifact, emphasizedSpanIds }: { artifact: { stable_id: string }; emphasizedSpanIds?: string[] }) => (
    <div data-testid="artifact-source" data-source-id={artifact.stable_id} data-emphasized={emphasizedSpanIds?.join(',') ?? ''} />
  ),
}));

import { ReviewSourcePreview } from '../../src/components/review/ReviewSourcePreview';

const fieldOne = reviewField('field-one', 'column-one');
const fieldTwo = reviewField('field-two', 'column-two');
const bundle: ReviewBundle = {
  id: 'bundle-1', runId: 'run-9', sheetId: 'sheet-1', sheetName: 'Sheet', rowId: 'row-3', rowIndex: 2,
  actionKind: 'enrich', actionName: 'Extract', model: 'model', confidence: null, source: {}, context: '',
  fields: [fieldOne, fieldTwo], evidence: [],
};

function reviewField(id: string, columnId: string): ReviewBundleField {
  return {
    id, runId: 'run-9', sheetId: 'sheet-1', rowId: 'row-3', columnId, columnName: id,
    columnType: 'text', value: `${id} value`, confidence: null, justification: '', reviewState: 'unreviewed', role: 'field', chore: false,
  };
}

function cellEvidence(linkIds: string[]): CellEvidencePayload {
  return {
    schema_version: 'frisket.cell_evidence.v1',
    current_value_ref: { kind: 'run_result', run_id: 'run-9' },
    links: linkIds.map((stable_id, index) => ({
      id: index + 1, stable_id, export_ref: stable_id, status: 'active', role: 'citation', evidence_kind: 'source',
      span_count: 1, artifact_count: 1, snippet: null, viewer_href: `/evidence/${stable_id}`,
    })),
  } as CellEvidencePayload;
}

function viewer(linkId: string, sourceId: string, spanId: string, title: string): EvidenceViewerPayload {
  return {
    schema_version: 'frisket.evidence_viewer.v1', warnings: [],
    link: {
      id: 1, stable_id: linkId, export_ref: linkId, subject_kind: 'cell', subject_ref: null,
      sheet_id: 1, row_id: 3, column_id: 1, run_id: 9, op_id: null, receipt_id: null, role: 'citation', status: 'active',
      confidence: null, pinned: false, producer: {}, stale_reason: null, stale_at: null,
      created_at: '2026-09-01T00:00:00Z', text_layer_hash_mismatch: false,
    },
    artifacts: [evidenceArtifact({
      stable_id: sourceId, title, media_type: 'text/markdown',
      spans: [evidenceSpan({ stable_id: spanId })],
      text_context: { text: 'A shared cited passage.', offset_unit: 'utf16_code_unit', ranges: [{ span_id: spanId, start: 2, end: 8 }] },
    })],
  };
}

describe('ReviewSourcePreview', () => {
  beforeEach(() => {
    projectApi.getCellEvidence.mockReset();
    projectApi.getEvidenceViewer.mockReset();
    class ResizeObserver {
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    vi.stubGlobal('ResizeObserver', ResizeObserver);
  });

  it('keeps cited sources mounted across cloned decision, edit and note updates, then reloads for another row', async () => {
    projectApi.getCellEvidence.mockImplementation((_rowId: string, columnId: string) => Promise.resolve(
      columnId === 'column-one' ? cellEvidence(['link:shared-one', 'link:one-only']) : cellEvidence(['link:shared-two', 'link:two-only']),
    ));
    projectApi.getEvidenceViewer.mockImplementation((linkId: string) => Promise.resolve({
      'link:shared-one': viewer(linkId, 'source:shared', 'span:one', 'Shared notes.md'),
      'link:shared-two': viewer(linkId, 'source:shared', 'span:two', 'Shared notes.md'),
      'link:one-only': viewer(linkId, 'source:one-only', 'span:one-only', 'One only.md'),
      'link:two-only': viewer(linkId, 'source:two-only', 'span:two-only', 'Two only.md'),
    }[linkId]));

    const { rerender } = render(<ReviewSourcePreview bundle={bundle} activeField={fieldOne} sourceEntries={[]} />);

    const sharedTab = await screen.findByRole('tab', { name: /shared notes\.md/i });
    fireEvent.click(sharedTab);
    expect(sharedTab).toHaveAttribute('aria-selected', 'true');
    await waitFor(() => expect(screen.getByTestId('artifact-source')).toHaveAttribute('data-emphasized', 'span:one'));
    const sourceRenderer = screen.getByTestId('artifact-source');

    const acceptedBundle = clonedBundle({ reviewDecision: 'accept', reviewState: 'verified' });
    rerender(<ReviewSourcePreview bundle={acceptedBundle} activeField={acceptedBundle.fields[0]} sourceEntries={[]} />);
    expect(screen.getByTestId('artifact-source')).toBe(sourceRenderer);

    const rejectedBundle = clonedBundle({ reviewDecision: 'reject', reviewState: 'rejected' });
    rerender(<ReviewSourcePreview bundle={rejectedBundle} activeField={rejectedBundle.fields[0]} sourceEntries={[]} />);
    expect(screen.getByTestId('artifact-source')).toBe(sourceRenderer);

    const editedBundle = clonedBundle({ value: 'Edited field one value' });
    rerender(<ReviewSourcePreview bundle={editedBundle} activeField={editedBundle.fields[0]} sourceEntries={[]} />);
    expect(screen.getByTestId('artifact-source')).toBe(sourceRenderer);

    const bundleWithSavedRowNote = clonedBundle({}, 'Saved note');
    rerender(<ReviewSourcePreview bundle={bundleWithSavedRowNote} activeField={bundleWithSavedRowNote.fields[0]} sourceEntries={[]} />);
    expect(screen.getByTestId('artifact-source')).toBe(sourceRenderer);
    expect(screen.getByTestId('artifact-source')).toHaveAttribute('data-source-id', 'source:shared');
    expect(projectApi.getCellEvidence).toHaveBeenCalledTimes(2);
    expect(projectApi.getEvidenceViewer).toHaveBeenCalledTimes(4);

    rerender(<ReviewSourcePreview bundle={bundleWithSavedRowNote} activeField={bundleWithSavedRowNote.fields[1]} sourceEntries={[]} />);

    await waitFor(() => expect(screen.getByRole('tab', { name: /shared notes\.md/i })).toHaveAttribute('aria-selected', 'true'));
    expect(screen.getByTestId('artifact-source')).toHaveAttribute('data-source-id', 'source:shared');
    expect(screen.getByTestId('artifact-source')).toHaveAttribute('data-emphasized', 'span:two');
    expect(screen.getByTestId('artifact-source')).toBe(sourceRenderer);
    expect(projectApi.getCellEvidence).toHaveBeenCalledTimes(2);
    expect(projectApi.getEvidenceViewer).toHaveBeenCalledTimes(4);

    rerender(<ReviewSourcePreview bundle={bundleWithSavedRowNote} activeField={bundleWithSavedRowNote.fields[0]} sourceEntries={[]} />);
    fireEvent.click(screen.getByRole('tab', { name: /one only/i }));
    rerender(<ReviewSourcePreview bundle={bundleWithSavedRowNote} activeField={bundleWithSavedRowNote.fields[1]} sourceEntries={[]} />);
    expect(screen.getByRole('tab', { name: /shared notes/i })).toHaveAttribute('aria-selected', 'true');
    rerender(<ReviewSourcePreview bundle={bundleWithSavedRowNote} activeField={bundleWithSavedRowNote.fields[0]} sourceEntries={[]} />);
    expect(screen.getByRole('tab', { name: /shared notes/i })).toHaveAttribute('aria-selected', 'true');

    const otherRowBundle = clonedBundle({ rowId: 'row-4' }, undefined, { id: 'bundle-2', rowId: 'row-4', rowIndex: 3 });
    rerender(<ReviewSourcePreview bundle={otherRowBundle} activeField={otherRowBundle.fields[0]} sourceEntries={[]} />);

    await waitFor(() => expect(projectApi.getCellEvidence).toHaveBeenCalledTimes(4));
    expect(projectApi.getCellEvidence).toHaveBeenCalledWith('row-4', 'column-one');
    expect(projectApi.getCellEvidence).toHaveBeenCalledWith('row-4', 'column-two');
    await waitFor(() => expect(projectApi.getEvidenceViewer).toHaveBeenCalledTimes(8));
  });
});

function clonedBundle(
  fieldUpdate: Partial<ReviewBundleField>,
  reviewNote?: string,
  bundleUpdate: Partial<ReviewBundle> = {},
): ReviewBundle {
  return {
    ...bundle,
    ...bundleUpdate,
    reviewNote,
    fields: bundle.fields.map((field) => ({ ...field, ...fieldUpdate })),
  };
}
