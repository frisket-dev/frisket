// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { ReviewBundlePage } from '../../src/api/types';
import { createProjectApi } from '../../src/api/real';
import { WorkspaceStoresContext } from '../../src/bind/workspaceStoresContext';
import { ReviewQueue } from '../../src/components/ReviewQueue';
import { createWorkspaceStores, type WorkspaceStores } from '../../src/state/createWorkspaceStores';

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
      <ReviewQueue runId="9" onClose={vi.fn()} onChanged={vi.fn()} />
    </WorkspaceStoresContext.Provider>,
  );
  await waitFor(() => expect(getBundles).toHaveBeenCalledWith(0, 25, '9', false));

  fireEvent.click(screen.getByRole('checkbox', { name: 'Show reviewed' }));
  await waitFor(() => expect(getBundles).toHaveBeenLastCalledWith(0, 25, '9', true));
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
      <ReviewQueue runId="9" onClose={vi.fn()} onChanged={vi.fn()} />
    </WorkspaceStoresContext.Provider>,
  );
  await screen.findByTestId('review-field-pending');
  const note = screen.getByTestId('review-note-input');
  fireEvent.change(note, { target: { value: 'Keep this note' } });

  fireEvent.click(screen.getByRole('checkbox', { name: 'Show reviewed' }));
  await screen.findByTestId('review-field-reviewed');

  expect(getBundles).toHaveBeenLastCalledWith(0, 25, '9', true);
  expect(note).toHaveValue('Keep this note');
});
