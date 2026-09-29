// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { ReviewBundlePage } from '../../src/api/types';
import { createProjectApi } from '../../src/api/real';
import { WorkspaceStoresContext } from '../../src/bind/workspaceStoresContext';
import { ReviewSession } from '../../src/components/review/ReviewSession';
import { createWorkspaceStores, type WorkspaceStores } from '../../src/state/createWorkspaceStores';

vi.mock('../../src/media/pdfjsSetup', () => ({
  pdfjsLib: {
    getDocument: vi.fn(() => ({ promise: new Promise(() => {}), destroy: () => Promise.resolve() })),
    TextLayer: class {},
  },
}));

vi.mock('../../src/components/EvidenceViewer', () => ({
  EvidenceViewer: ({ evidenceLinkId, onClose }: { evidenceLinkId: string; onClose(): void }) => (
    <section data-testid="evidence-viewer" data-link-id={evidenceLinkId}>
      <button type="button" onClick={onClose}>Dismiss citation</button>
    </section>
  ),
}));

let stores: WorkspaceStores;

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

function page(name: string): ReviewBundlePage {
  return {
    schemaVersion: 'frisket.review_bundles_page.v1',
    offset: 0,
    limit: 25,
    total: 1,
    hasMore: false,
    nextOffset: null,
    bundles: [{
      id: `bundle-${name}`,
      runId: '9',
      sheetId: '2',
      sheetName: 'Stories',
      rowId: '3',
      rowIndex: 2,
      actionKind: 'map.classify',
      actionName: 'Classify',
      model: 'test-model',
      confidence: 0.4,
      source: { story: 'Example' },
      context: 'story: Example',
      fields: [{
        id: '9:3:4', runId: '9', sheetId: '2', rowId: '3', columnId: '4',
        columnName: name, columnType: 'text', value: 'result', confidence: 0.4,
        justification: '', reviewDecision: name === 'reviewed' ? 'accept' : null,
        note: null, reviewState: name === 'reviewed' ? 'verified' : 'unreviewed',
        role: 'field', chore: name !== 'reviewed',
      }],
      evidence: [],
    }],
  };
}

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', class {
    observe() {}
    disconnect() {}
  });
  stores = createWorkspaceStores('review-generation', createProjectApi('review-generation'));
});

afterEach(() => {
  cleanup();
  stores.dispose();
  vi.restoreAllMocks();
});

it('keeps the latest Show reviewed response when the pending response arrives late', async () => {
  const pending = deferred<ReviewBundlePage>();
  const reviewed = deferred<ReviewBundlePage>();
  const getBundles = vi.spyOn(stores.projectApi, 'getReviewBundles')
    .mockReturnValueOnce(pending.promise)
    .mockReturnValueOnce(reviewed.promise);

  render(
    <WorkspaceStoresContext.Provider value={stores}>
      <ReviewSession runId="9" options={{ order: 'confidence' }} onChanged={vi.fn()} />
    </WorkspaceStoresContext.Provider>,
  );
  await waitFor(() => expect(getBundles).toHaveBeenCalledWith(0, 25, '9', false, { order: 'confidence' }));

  fireEvent.click(screen.getByRole('checkbox', { name: 'Show reviewed' }));
  await waitFor(() => expect(getBundles).toHaveBeenLastCalledWith(0, 25, '9', true, { order: 'confidence' }));
  await act(async () => { reviewed.resolve(page('reviewed')); });
  expect(await screen.findByTestId('review-field-reviewed')).toBeVisible();

  await act(async () => { pending.resolve(page('pending')); });
  expect(screen.getByTestId('review-field-reviewed')).toBeVisible();
  expect(screen.queryByTestId('review-field-pending')).not.toBeInTheDocument();
});

it('keeps an in-progress note when a refreshed page still contains its selected field', async () => {
  const getBundles = vi.spyOn(stores.projectApi, 'getReviewBundles')
    .mockResolvedValueOnce(page('pending'))
    .mockResolvedValueOnce(page('reviewed'));

  render(
    <WorkspaceStoresContext.Provider value={stores}>
      <ReviewSession runId="9" options={{ order: 'confidence' }} onChanged={vi.fn()} />
    </WorkspaceStoresContext.Provider>,
  );
  await screen.findByTestId('review-field-pending');
  const note = screen.getByTestId('review-note-input');
  fireEvent.change(note, { target: { value: 'Keep this note' } });

  fireEvent.click(screen.getByRole('checkbox', { name: 'Show reviewed' }));
  await screen.findByTestId('review-field-reviewed');

  expect(getBundles).toHaveBeenLastCalledWith(0, 25, '9', true, { order: 'confidence' });
  expect(note).toHaveValue('Keep this note');
});

it('does not attach a stale or manually-overlaid citation to the reviewed result', async () => {
  vi.spyOn(stores.projectApi, 'getReviewBundles').mockResolvedValue({
    ...page('pending'),
    bundles: [{
      ...page('pending').bundles[0],
      source: {
        filing: JSON.stringify({
          blob: 'a'.repeat(64),
          filename: 'filing.pdf',
          mime: 'application/pdf',
        }),
      },
    }],
  });
  const getCellEvidence = vi.spyOn(stores.projectApi, 'getCellEvidence').mockResolvedValue({
    schema_version: 'frisket.cell_evidence.v1',
    sheet_id: 2,
    row_id: 3,
    column_id: 4,
    current_value_ref: { kind: 'manual_edit', run_id: null },
    links: [{
      id: 91,
      stable_id: 'unrelated-active-link',
      export_ref: 'evidence:91',
      status: 'active',
      role: 'citation',
      evidence_kind: 'document',
      span_count: 1,
      artifact_count: 1,
      snippet: 'An unrelated current value',
      viewer_href: '/evidence/91',
    }],
    stale_count: 0,
  });

  render(
    <WorkspaceStoresContext.Provider value={stores}>
      <ReviewSession runId="9" options={{ order: 'confidence' }} onChanged={vi.fn()} />
    </WorkspaceStoresContext.Provider>,
  );

  expect(await screen.findByTestId('review-source-pdf-source')).toBeVisible();
  expect(getCellEvidence).toHaveBeenCalledWith('3', '4');
  expect(screen.queryByTestId('evidence-viewer')).not.toBeInTheDocument();
  expect(screen.getByTestId('document-reader')).toHaveAttribute('data-media-kind', 'pdf');
});

it('prefers a supporting citation from the active reviewed run and keeps Review open when its pane closes', async () => {
  vi.spyOn(stores.projectApi, 'getReviewBundles').mockResolvedValue(page('pending'));
  vi.spyOn(stores.projectApi, 'getCellEvidence').mockResolvedValue({
    schema_version: 'frisket.cell_evidence.v1',
    sheet_id: 2,
    row_id: 3,
    column_id: 4,
    current_value_ref: { kind: 'run_result', run_id: 9 },
    links: [
      {
        id: 11,
        stable_id: 'provenance-link',
        export_ref: 'evidence:11',
        status: 'active',
        role: 'source_provenance',
        evidence_kind: 'document',
        span_count: 0,
        artifact_count: 1,
        snippet: null,
        viewer_href: '/evidence/11',
      },
      {
        id: 12,
        stable_id: 'citation-link',
        export_ref: 'evidence:12',
        status: 'active',
        role: 'citation',
        evidence_kind: 'document',
        span_count: 1,
        artifact_count: 1,
        snippet: 'The supporting passage',
        viewer_href: '/evidence/12',
      },
    ],
    stale_count: 0,
  });

  render(
    <WorkspaceStoresContext.Provider value={stores}>
      <ReviewSession runId="9" options={{ order: 'confidence' }} onChanged={vi.fn()} />
    </WorkspaceStoresContext.Provider>,
  );

  expect(await screen.findByTestId('evidence-viewer')).toHaveAttribute('data-link-id', 'citation-link');
  fireEvent.click(screen.getByRole('button', { name: 'Dismiss citation' }));
  expect(screen.getByTestId('review-card')).toBeVisible();
});
