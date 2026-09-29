// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { ReviewBundlePage } from '../../src/api/types';
import { createProjectApi } from '../../src/api/real';
import { WorkspaceStoresContext } from '../../src/bind/workspaceStoresContext';
import { ReviewSession } from '../../src/components/review/ReviewSession';
import { createWorkspaceStores, type WorkspaceStores } from '../../src/state/createWorkspaceStores';

vi.mock('../../src/components/review/ReviewSourcePreview', () => ({ ReviewSourcePreview: () => <aside>Source viewer</aside> }));

let stores: WorkspaceStores;

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

const options = { order: 'shuffle' as const, seed: 4 };
function mount(onClose = vi.fn()) {
  return render(<WorkspaceStoresContext.Provider value={stores}>
    <ReviewSession runId="9" options={options} onChanged={vi.fn()} onClose={onClose}
      renderToolbar={(navigation, leave) => <>{navigation}<button onClick={() => leave(onClose)}>Close</button></>} />
  </WorkspaceStoresContext.Provider>);
}
function multiPage() {
  const value = page('first');
  const row = value.bundles[0];
  row.fields.push({ ...row.fields[0], id: '9:3:5', columnId: '5', columnName: 'second' });
  value.bundles.push({ ...row, id: 'next-row', rowId: '4', fields: row.fields.map((f) => ({ ...f, id: `9:4:${f.columnId}`, rowId: '4' })) });
  value.total = 2;
  return value;
}

it('keeps decided rows visible, advances fields, and toggles a decision without changing the value', async () => {
  vi.spyOn(stores.projectApi, 'getReviewBundles').mockResolvedValue(multiPage());
  const decide = vi.spyOn(stores.projectApi, 'reviewItem').mockResolvedValue();
  vi.spyOn(stores.projectApi, 'getReviewCount').mockResolvedValue(1);
  mount();
  fireEvent.click(await screen.findByRole('button', { name: 'Accept first' }));
  await waitFor(() => expect(screen.getByTestId('review-field-first')).toHaveAttribute('data-review-state', 'verified'));
  expect(screen.getByRole('button', { name: 'Select second review field' })).toHaveAttribute('aria-pressed', 'true');
  fireEvent.click(screen.getByRole('button', { name: 'Accept first' }));
  await waitFor(() => expect(decide).toHaveBeenLastCalledWith('9:3:4', 'clear', undefined));
  expect(screen.getByTestId('review-field-first')).toHaveTextContent('result');
  expect(screen.getByTestId('review-page-status')).toHaveTextContent('1 / 2');
});

it('accepts remaining fields without replacing a rejection, then resets decisions', async () => {
  const value = multiPage();
  value.bundles[0].fields[0] = { ...value.bundles[0].fields[0], value: 'corrected value', reviewState: 'rejected', reviewDecision: 'reject', chore: false };
  vi.spyOn(stores.projectApi, 'getReviewBundles').mockResolvedValue(value);
  const decide = vi.spyOn(stores.projectApi, 'reviewItem').mockResolvedValue();
  vi.spyOn(stores.projectApi, 'getReviewCount').mockResolvedValue(1);
  mount();
  fireEvent.click(await screen.findByRole('button', { name: 'Accept remaining' }));
  await screen.findByText('Row done');
  expect(decide).toHaveBeenCalledTimes(1);
  expect(decide).toHaveBeenCalledWith('9:3:5', 'accept', undefined);
  fireEvent.click(screen.getByRole('button', { name: 'Reset' }));
  await waitFor(() => expect(decide).toHaveBeenCalledTimes(3));
  expect(screen.getByTestId('review-field-first')).toHaveTextContent('corrected value');
  expect(screen.getByTestId('review-field-first')).toHaveAttribute('data-review-state', 'unreviewed');
});

it('saves a row note on navigation, preserves it across fields, and blocks leaving after a failed save', async () => {
  vi.spyOn(stores.projectApi, 'getReviewBundles').mockResolvedValue(multiPage());
  const save = vi.spyOn(stores.projectApi, 'setReviewNote').mockRejectedValueOnce(new Error('offline')).mockResolvedValue();
  const close = vi.fn();
  mount(close);
  const note = await screen.findByTestId('review-note-input');
  fireEvent.change(note, { target: { value: 'Keep this note' } });
  fireEvent.click(screen.getByRole('button', { name: 'Select second review field' }));
  expect(note).toHaveValue('Keep this note');
  fireEvent.click(screen.getByRole('button', { name: 'Close' }));
  await screen.findByRole('alert');
  expect(close).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Close' }));
  await waitFor(() => expect(close).toHaveBeenCalledOnce());
  expect(save).toHaveBeenLastCalledWith('9', '3', 'Keep this note');
});

it('ignores typing and browser modifiers while J/K navigate fields, not rows', async () => {
  vi.spyOn(stores.projectApi, 'getReviewBundles').mockResolvedValue(multiPage());
  const decide = vi.spyOn(stores.projectApi, 'reviewItem').mockResolvedValue();
  mount();
  const note = await screen.findByTestId('review-note-input');
  fireEvent.keyDown(note, { key: 'a' });
  fireEvent.keyDown(window, { key: 'a', ctrlKey: true });
  expect(decide).not.toHaveBeenCalled();
  fireEvent.keyDown(window, { key: 'j' });
  expect(screen.getByRole('button', { name: 'Select second review field' })).toHaveAttribute('aria-pressed', 'true');
  expect(screen.getByTestId('review-page-status')).toHaveTextContent('1 / 2');
});

it('keeps a partial bulk save visible and does not resend it when accepting remaining again', async () => {
  vi.spyOn(stores.projectApi, 'getReviewBundles').mockResolvedValue(multiPage());
  const decide = vi.spyOn(stores.projectApi, 'reviewItem').mockResolvedValueOnce().mockRejectedValueOnce(new Error('offline')).mockResolvedValue();
  vi.spyOn(stores.projectApi, 'getReviewCount').mockResolvedValue(1);
  mount();
  fireEvent.click(await screen.findByRole('button', { name: 'Accept all' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('1 decisions saved');
  expect(screen.getByTestId('review-field-first')).toHaveAttribute('data-review-state', 'verified');
  fireEvent.click(screen.getByRole('button', { name: 'Accept remaining' }));
  await screen.findByText('Row done');
  expect(decide.mock.calls.map((call) => call[0])).toEqual(['9:3:4', '9:3:5', '9:3:5']);
});

it('waits for an in-flight blur save before closing and sends the note only once', async () => {
  vi.spyOn(stores.projectApi, 'getReviewBundles').mockResolvedValue(multiPage());
  let finish!: () => void;
  const pending = new Promise<void>((resolve) => { finish = resolve; });
  const save = vi.spyOn(stores.projectApi, 'setReviewNote').mockReturnValue(pending);
  const close = vi.fn();
  mount(close);
  const note = await screen.findByTestId('review-note-input');
  fireEvent.change(note, { target: { value: 'Row note' } });
  fireEvent.blur(note);
  const button = screen.getByRole('button', { name: 'Close' });
  expect(button).toBeEnabled();
  fireEvent.click(button);
  expect(close).not.toHaveBeenCalled();
  finish();
  await waitFor(() => expect(close).toHaveBeenCalledOnce());
  expect(save).toHaveBeenCalledOnce();
});
