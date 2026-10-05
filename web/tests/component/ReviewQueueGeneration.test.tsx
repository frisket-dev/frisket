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
    nextCursor: null,
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

const options = { order: 'shuffle' as const };
function mount(onClose = vi.fn(), reviewOptions = options) {
  return render(<WorkspaceStoresContext.Provider value={stores}>
    <ReviewSession runId="9" options={reviewOptions} onChanged={vi.fn()} onClose={onClose}
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
  await waitFor(() => expect(screen.getByTestId('review-page-status')).toHaveTextContent('2 / 2'));
  expect(decide).toHaveBeenCalledTimes(1);
  expect(decide).toHaveBeenCalledWith('9:3:5', 'accept', undefined);
  fireEvent.click(screen.getByRole('button', { name: 'Previous row' }));
  await screen.findByText('Row done');
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
  expect(screen.getByTestId('review-page-status')).toHaveTextContent('1 / 2');
  fireEvent.click(screen.getByRole('button', { name: 'Accept remaining' }));
  await waitFor(() => expect(screen.getByTestId('review-page-status')).toHaveTextContent('2 / 2'));
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
  expect(note).toBeDisabled();
  const button = screen.getByRole('button', { name: 'Close' });
  expect(button).toBeEnabled();
  fireEvent.click(button);
  expect(close).not.toHaveBeenCalled();
  finish();
  await waitFor(() => expect(close).toHaveBeenCalledOnce());
  expect(save).toHaveBeenCalledOnce();
});

it.each(['ArrowDown', 's', 'ArrowUp', 'w'])('%s selects another result without moving rows', async (key) => {
  vi.spyOn(stores.projectApi, 'getReviewBundles').mockResolvedValue(multiPage());
  mount();
  await screen.findByRole('button', { name: 'Select first review field' });
  fireEvent.keyDown(window, { key });
  expect(screen.getByRole('button', { name: 'Select second review field' })).toHaveAttribute('aria-pressed', 'true');
  expect(screen.getByTestId('review-page-status')).toHaveTextContent('1 / 2');
});

it.each([['ArrowLeft', 'accept'], ['a', 'accept'], ['ArrowRight', 'reject'], ['d', 'reject']])('%s records %s', async (key, decision) => {
  vi.spyOn(stores.projectApi, 'getReviewBundles').mockResolvedValue(multiPage());
  const decide = vi.spyOn(stores.projectApi, 'reviewItem').mockResolvedValue();
  vi.spyOn(stores.projectApi, 'getReviewCount').mockResolvedValue(1);
  mount();
  const note = await screen.findByTestId('review-note-input');
  fireEvent.keyDown(note, { key });
  fireEvent.keyDown(window, { key, repeat: true });
  fireEvent.keyDown(window, { key, isComposing: true });
  expect(decide).not.toHaveBeenCalled();
  fireEvent.keyDown(window, { key });
  await waitFor(() => expect(decide).toHaveBeenCalledWith('9:3:4', decision, undefined));
});

it('edits the field whose pencil was clicked and records a correction', async () => {
  vi.spyOn(stores.projectApi, 'getReviewBundles').mockResolvedValue(multiPage());
  const decide = vi.spyOn(stores.projectApi, 'reviewItem').mockResolvedValue();
  vi.spyOn(stores.projectApi, 'getReviewCount').mockResolvedValue(1);
  mount();
  fireEvent.click(await screen.findByRole('button', { name: 'Edit second', exact: true }));
  fireEvent.change(screen.getByRole('textbox', { name: 'Edit proposed second value' }), { target: { value: 'Fixed' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save edit' }));
  await waitFor(() => expect(screen.getByTestId('review-field-second')).toHaveTextContent('Fixed'));
  expect(decide).toHaveBeenCalledWith('9:3:5', 'edit', 'Fixed');
  expect(screen.getByRole('button', { name: 'Edit second', exact: true })).toHaveAttribute('aria-pressed', 'true');
  expect(screen.getByRole('button', { name: 'Accept second' })).toHaveAttribute('aria-pressed', 'false');
  expect(screen.getByTestId('review-field-second')).toHaveAttribute('data-review-decision', 'edit');
  expect(screen.queryByRole('button', { name: 'Reject and clear' })).not.toBeInTheDocument();
});

it('keeps historical decisions and notes available while explaining why edit is disabled', async () => {
  const value = page('replaced');
  value.bundles[0].fields[0].canEdit = false;
  vi.spyOn(stores.projectApi, 'getReviewBundles').mockResolvedValue(value);
  const decide = vi.spyOn(stores.projectApi, 'reviewItem').mockResolvedValue();
  vi.spyOn(stores.projectApi, 'getReviewCount').mockResolvedValue(1);
  mount();

  const edit = await screen.findByRole('button', { name: 'Edit replaced', exact: true });
  expect(edit).toBeDisabled();
  expect(edit).toHaveAttribute('title', expect.stringContaining('replaced or edited after the run'));
  expect(screen.getByText(/Accept, reject, clear, and notes are still available/)).toBeVisible();
  expect(screen.getByTestId('review-note-input')).toBeEnabled();

  const reject = screen.getByRole('button', { name: 'Reject replaced', exact: true });
  expect(reject).toBeEnabled();
  fireEvent.click(reject);
  await waitFor(() => expect(decide).toHaveBeenLastCalledWith('9:3:4', 'reject', undefined));
  fireEvent.click(reject);
  await waitFor(() => expect(decide).toHaveBeenLastCalledWith('9:3:4', 'clear', undefined));
  fireEvent.click(screen.getByRole('button', { name: 'Accept all' }));
  await waitFor(() => expect(decide).toHaveBeenLastCalledWith('9:3:4', 'accept', undefined));
});

it('allows edits only for plain text results through both buttons and keyboard shortcuts', async () => {
  const value = multiPage();
  value.bundles[0].fields[1] = {
    ...value.bundles[0].fields[1],
    columnType: 'json',
    value: JSON.stringify([{ text: 'Ada Lovelace', type: 'PERSON', start: 0 }]),
  };
  vi.spyOn(stores.projectApi, 'getReviewBundles').mockResolvedValue(value);
  mount();

  const structuredEdit = await screen.findByRole('button', { name: 'Edit second', exact: true });
  expect(structuredEdit).toBeDisabled();
  const structuredSelect = screen.getByRole('button', { name: 'Select second review field' });
  expect(structuredSelect.querySelector('table, a, img, audio, video')).toBeNull();
  expect(screen.getByTestId('review-field-second')).toContainElement(screen.getByTestId('entity-mini-table'));
  fireEvent.click(structuredSelect);
  fireEvent.keyDown(window, { key: 'e' });
  expect(screen.queryByTestId('review-edit-input')).toBeNull();

  fireEvent.click(screen.getByRole('button', { name: 'Select first review field' }));
  fireEvent.keyDown(window, { key: 'e' });
  expect(screen.getByRole('textbox', { name: 'Edit proposed first value' })).toBeVisible();
});

it('accepts the final row without navigating beyond it', async () => {
  const load = vi.spyOn(stores.projectApi, 'getReviewBundles').mockResolvedValue(page('last'));
  vi.spyOn(stores.projectApi, 'reviewItem').mockResolvedValue();
  vi.spyOn(stores.projectApi, 'getReviewCount').mockResolvedValue(0);
  mount();
  fireEvent.click(await screen.findByRole('button', { name: 'Accept all' }));
  await screen.findByText('Row done');
  expect(screen.getByTestId('review-page-status')).toHaveTextContent('1 / 1');
  expect(load).toHaveBeenCalledOnce();
});

it('waits for a complete bulk save before advancing across the page boundary', async () => {
  const first = page('first');
  Object.assign(first, { total: 2, limit: 1, hasMore: true });
  const next = page('next');
  Object.assign(next, {
    total: 2, limit: 1,
    bundles: [{ ...next.bundles[0], rowId: '4', id: 'bundle-next', fields: next.bundles[0].fields.map((field) => ({
      ...field, id: `9:4:${field.columnId}`, rowId: '4',
    })) }],
  });
  const load = vi.spyOn(stores.projectApi, 'getReviewBundles').mockResolvedValueOnce(first).mockResolvedValueOnce(next);
  let finish!: () => void;
  vi.spyOn(stores.projectApi, 'reviewItem').mockReturnValue(new Promise<void>((resolve) => { finish = resolve; }));
  vi.spyOn(stores.projectApi, 'getReviewCount').mockResolvedValue(1);
  mount();
  fireEvent.click(await screen.findByRole('button', { name: 'Accept all' }));
  expect(screen.getByTestId('review-page-status')).toHaveTextContent('1 / 2');
  expect(load).toHaveBeenCalledOnce();
  finish();
  await waitFor(() => expect(screen.getByTestId('review-page-status')).toHaveTextContent('2 / 2'));
  expect(load).toHaveBeenLastCalledWith(0, 25, '9', true, {
    fieldId: undefined, order: 'shuffle', excludeRowIds: ['3'],
  });
});

it('keeps random history stable, excludes shown rows, and refreshes decisions on Back and Forward', async () => {
  const first = page('first');
  Object.assign(first, { total: 2, limit: 1, hasMore: true });
  const second = page('second');
  Object.assign(second, {
    total: 2, limit: 1,
    bundles: [{ ...second.bundles[0], rowId: '4', id: 'bundle-second', fields: second.bundles[0].fields.map((field) => ({
      ...field, id: `9:4:${field.columnId}`, rowId: '4',
    })) }],
  });
  const refreshedFirst = structuredClone(first);
  refreshedFirst.hasMore = false;
  refreshedFirst.bundles[0].fields[0].reviewDecision = 'accept';
  refreshedFirst.bundles[0].fields[0].reviewState = 'verified';
  const refreshedSecond = structuredClone(second);
  refreshedSecond.hasMore = false;
  const load = vi.spyOn(stores.projectApi, 'getReviewBundles')
    .mockResolvedValueOnce(first)
    .mockResolvedValueOnce(second)
    .mockResolvedValueOnce(refreshedFirst)
    .mockResolvedValueOnce(refreshedSecond);
  mount();

  await waitFor(() => expect(screen.getByTestId('review-page-status')).toHaveTextContent('1 / 2'));
  fireEvent.click(screen.getAllByRole('button', { name: 'Next row', exact: true })[0]);
  await waitFor(() => expect(screen.getByTestId('review-page-status')).toHaveTextContent('2 / 2'));
  fireEvent.click(screen.getByRole('button', { name: 'Previous row' }));
  await waitFor(() => expect(screen.getByTestId('review-page-status')).toHaveTextContent('1 / 2'));
  expect(screen.getByRole('button', { name: 'Accept first' })).toHaveAttribute('aria-pressed', 'true');
  fireEvent.click(screen.getAllByRole('button', { name: 'Next row', exact: true })[0]);
  await waitFor(() => expect(screen.getByTestId('review-page-status')).toHaveTextContent('2 / 2'));

  expect(load.mock.calls).toEqual([
    [0, 25, '9', true, { fieldId: undefined, order: 'shuffle', excludeRowIds: [] }],
    [0, 25, '9', true, { fieldId: undefined, order: 'shuffle', excludeRowIds: ['3'] }],
    [0, 25, '9', true, { fieldId: undefined, order: 'shuffle', rowIds: ['3'] }],
    [0, 25, '9', true, { fieldId: undefined, order: 'shuffle', rowIds: ['4'] }],
  ]);
});

it('retries a failed random frontier request instead of reloading the displayed page', async () => {
  const first = page('first');
  Object.assign(first, { total: 2, limit: 1, hasMore: true });
  const second = page('second');
  Object.assign(second, {
    total: 2, limit: 1,
    bundles: [{ ...second.bundles[0], rowId: '4', id: 'bundle-second', fields: second.bundles[0].fields.map((field) => ({
      ...field, id: `9:4:${field.columnId}`, rowId: '4',
    })) }],
  });
  const load = vi.spyOn(stores.projectApi, 'getReviewBundles')
    .mockResolvedValueOnce(first)
    .mockRejectedValueOnce(new Error('offline'))
    .mockResolvedValueOnce(second);
  mount();

  await waitFor(() => expect(screen.getByTestId('review-page-status')).toHaveTextContent('1 / 2'));
  fireEvent.click(screen.getAllByRole('button', { name: 'Next row', exact: true })[0]);
  fireEvent.click(await screen.findByRole('button', { name: 'Retry' }));
  await waitFor(() => expect(screen.getByTestId('review-page-status')).toHaveTextContent('2 / 2'));

  expect(load.mock.calls).toEqual([
    [0, 25, '9', true, { fieldId: undefined, order: 'shuffle', excludeRowIds: [] }],
    [0, 25, '9', true, { fieldId: undefined, order: 'shuffle', excludeRowIds: ['3'] }],
    [0, 25, '9', true, { fieldId: undefined, order: 'shuffle', excludeRowIds: ['3'] }],
  ]);
});

it('uses row cursors for stable next and previous pages', async () => {
  const first = page('first');
  Object.assign(first, { total: 2, limit: 1, hasMore: true, nextOffset: null, nextCursor: 3 });
  const next = page('next');
  Object.assign(next, {
    offset: 0, total: 2, limit: 1, nextCursor: null,
    bundles: [{ ...next.bundles[0], rowId: '4', id: 'bundle-next', fields: next.bundles[0].fields.map((field) => ({
      ...field, id: `9:4:${field.columnId}`, rowId: '4',
    })) }],
  });
  const load = vi.spyOn(stores.projectApi, 'getReviewBundles')
    .mockResolvedValueOnce(first)
    .mockResolvedValueOnce(next)
    .mockResolvedValueOnce(first);
  mount(vi.fn(), { order: 'row' });

  await waitFor(() => expect(screen.getByTestId('review-page-status')).toHaveTextContent('1 / 2'));
  fireEvent.click(screen.getAllByRole('button', { name: 'Next row', exact: true })[0]);
  await waitFor(() => expect(screen.getByTestId('review-page-status')).toHaveTextContent('2 / 2'));
  fireEvent.click(screen.getByRole('button', { name: 'Previous row' }));
  await waitFor(() => expect(screen.getByTestId('review-page-status')).toHaveTextContent('1 / 2'));

  expect(load.mock.calls).toEqual([
    [0, 25, '9', true, { fieldId: undefined, order: 'row', cursor: undefined }],
    [0, 25, '9', true, { fieldId: undefined, order: 'row', cursor: 3 }],
    [0, 25, '9', true, { fieldId: undefined, order: 'row', cursor: undefined }],
  ]);
});

it('lets the reviewer close while the initial page is loading', async () => {
  vi.spyOn(stores.projectApi, 'getReviewBundles').mockReturnValue(new Promise(() => {}));
  const close = vi.fn();
  mount(close);
  fireEvent.click(screen.getByRole('button', { name: 'Close' }));
  await waitFor(() => expect(close).toHaveBeenCalledOnce());
});

it('resamples an empty history page after a run becomes available again', async () => {
  const first = { ...page('first'), total: 2, hasMore: true };
  const empty = { ...first, total: 0, hasMore: false, bundles: [] };
  const next = page('next');
  next.total = 2;
  next.bundles[0].rowId = '4';
  const load = vi.spyOn(stores.projectApi, 'getReviewBundles')
    .mockResolvedValueOnce(first)
    .mockResolvedValueOnce(empty)
    .mockResolvedValueOnce(first)
    .mockResolvedValueOnce(next);
  mount();

  await screen.findByTestId('review-card');
  fireEvent.click(screen.getAllByRole('button', { name: 'Next row', exact: true })[0]);
  await screen.findByTestId('review-empty');
  expect(screen.getByTestId('review-page-status')).toHaveTextContent('0 / 0');
  fireEvent.click(screen.getByRole('button', { name: 'Previous row' }));
  await screen.findByTestId('review-card');
  fireEvent.click(screen.getAllByRole('button', { name: 'Next row', exact: true })[0]);
  await waitFor(() => expect(screen.getByTestId('review-page-status')).toHaveTextContent('2 / 2'));
  expect(load).toHaveBeenLastCalledWith(0, 25, '9', true, {
    fieldId: undefined, order: 'shuffle', excludeRowIds: ['3'],
  });
});
