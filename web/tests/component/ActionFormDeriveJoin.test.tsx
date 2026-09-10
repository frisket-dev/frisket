// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it } from 'vitest';
import { joinApi, joinLeft, joinRight, renderJoinForm } from '../support/renderJoinForm';
import type { GeneratedActionDraft } from '../../src/api/types';

afterEach(cleanup);
beforeEach(() => joinApi.listSheets.mockReset().mockResolvedValue([joinLeft, joinRight]));
const draft: GeneratedActionDraft = {
  action_id: 'derive.join', scope: { kind: 'sheet_rows', sheet_id: 1, row_ids: [101] },
  params: { right: { sheet_id: 2 }, join_keys: [{ left_column: 'code', right_column: 'code' }],
    columns: [{ side: 'right', column: 'city' }, { side: 'left', column: 'state' }] },
  output_names: { code: 'id', city_left: 'Reported city', city_right: 'Official city',
    city_right_2: 'Copy', _merge: 'Match status' }, sheet_name: 'Saved join',
};
function openSaved(params = draft.params) {
  return renderJoinForm({ initialDraft: { ...structuredClone(draft), params },
    hasExactRowScopeInitializer: true, selectedRowIds: ['101'] });
}
async function ready() {
  await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
}
function choose(id: string, value: string) {
  fireEvent.change(screen.getByTestId(id), { target: { value } });
}
function deferred() {
  let resolve!: (value: typeof joinLeft[]) => void;
  const promise = new Promise<typeof joinLeft[]>((done) => { resolve = done; });
  return { promise, resolve };
}
function refresh() { fireEvent.click(screen.getByTestId('tabular-join-refresh-sheets')); }

it('retains absent saved right sheet until the sheet, key and projection are explicitly repaired', async () => {
  const view = openSaved({ ...draft.params, right: { sheet_id: 999 } });
  await waitFor(() => expect(screen.getByTestId('tabular-join-repair')).toBeVisible());
  expect(screen.getByTestId('field-join_right_sheet')).toHaveValue('');
  expect(screen.getByTestId('generated-action-run')).toBeDisabled();
  choose('field-join_right_sheet', '2');
  choose('field-join_key_right-0', 'code');
  expect(screen.getByTestId('generated-action-run')).toBeDisabled();
  choose('join-projection-column-0', 'city');
  await ready();
  fireEvent.click(screen.getByTestId('generated-action-run'));
  expect(view.onExecute.mock.calls[0][0]).toMatchObject({ scope: draft.scope,
    params: { right: { sheet_id: 2 }, columns: draft.params.columns } });
});

it('does not bind a deleted key to a same-ID column on another sheet or choose the first option', async () => {
  const view = openSaved(); await ready();
  joinApi.listSheets.mockResolvedValueOnce([{ ...joinLeft, columns: [
    ...joinLeft.columns, { ...joinLeft.columns[0], id: '21', name: 'Other sheet key' },
  ] }, { ...joinRight, columns: joinRight.columns.filter((c) => c.id !== '21') }]);
  refresh();
  await waitFor(() => expect(screen.getByTestId('field-join_key_right-0')).toHaveValue(''));
  expect(screen.getByTestId('tabular-join-repair')).toHaveTextContent('code');
  expect(screen.getByTestId('generated-action-run')).toBeDisabled();
  choose('field-join_key_right-0', 'zone'); await ready();
  fireEvent.click(screen.getByTestId('generated-action-run'));
  expect(view.onExecute.mock.calls[0][0].params.join_keys).toEqual([{ left_column: 'code', right_column: 'zone' }]);
});

it('does not turn a deleted last projection into all-columns until explicitly removed', async () => {
  const view = openSaved({ ...draft.params, columns: [{ side: 'right', column: 'city' }] }); await ready();
  joinApi.listSheets.mockResolvedValueOnce([joinLeft,
    { ...joinRight, columns: joinRight.columns.filter((c) => c.id !== '22') }]);
  refresh();
  await waitFor(() => expect(screen.getByTestId('join-projection-column-0')).toHaveValue(''));
  expect(screen.getByTestId('generated-action-run')).toBeDisabled();
  expect(screen.getByTestId('join-projection-order')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: 'Remove projection 1' })); await ready();
  fireEvent.click(screen.getByTestId('generated-action-run'));
  expect(view.onExecute.mock.calls[0][0].params.columns).toBeNull();
});

it('repairs repeated missing projections separately without reordering surviving occurrences', async () => {
  const view = openSaved({ ...draft.params, columns: [
    { side: 'right', column: 'city' }, { side: 'left', column: 'city' }, { side: 'right', column: 'city' },
  ] }); await ready();
  joinApi.listSheets.mockResolvedValueOnce([joinLeft,
    { ...joinRight, columns: joinRight.columns.map((c) => c.id === '22' ? { ...c, id: '99' } : c) }]);
  refresh();
  await waitFor(() => expect(screen.getByTestId('join-projection-column-0')).toHaveValue(''));
  choose('join-projection-column-0', 'city');
  expect(screen.getByTestId('generated-action-run')).toBeDisabled();
  choose('join-projection-column-2', 'zone'); await ready();
  fireEvent.click(screen.getByTestId('generated-action-run'));
  expect(view.onExecute.mock.calls[0][0].params.columns).toEqual([
    { side: 'right', column: 'city' }, { side: 'left', column: 'city' }, { side: 'right', column: 'zone' },
  ]);
});

it('keeps sheet and missing-column labels separate when their numeric IDs collide', async () => {
  const right = { ...joinRight, columns: joinRight.columns.map((c) => c.name === 'city' ? { ...c, id: '2' } : c) };
  joinApi.listSheets.mockResolvedValueOnce([joinLeft, right]); openSaved(); await ready();
  joinApi.listSheets.mockResolvedValueOnce([joinLeft, { ...right, name: 'Renamed sheet',
    columns: right.columns.filter((c) => c.id !== '2') }]); refresh();
  await waitFor(() => expect(screen.getByTestId('tabular-join-repair')).toHaveTextContent('city'));
  expect(screen.getByTestId('tabular-join-repair')).not.toHaveTextContent('Renamed sheet');
  expect(screen.getByTestId('field-join_right_sheet')).toHaveTextContent('Renamed sheet');
});

it('switches the whole right sheet fail-closed while preserving the primary selected-row scope', async () => {
  joinApi.listSheets.mockResolvedValueOnce([joinLeft, joinRight, { ...joinRight, id: '3', name: 'Third' }]);
  const view = openSaved(); await ready(); choose('field-join_right_sheet', '3');
  expect(screen.getByTestId('field-join_key_right-0')).toHaveValue('');
  expect(screen.getByTestId('join-projection-column-0')).toHaveValue('');
  expect(screen.getByTestId('field-join_key_left-0')).toHaveValue('code');
  expect(screen.queryByTestId('field-join_left_sheet')).not.toBeInTheDocument();
  choose('field-join_key_right-0', 'code'); choose('join-projection-column-0', 'zone'); await ready();
  fireEvent.click(screen.getByTestId('generated-action-run'));
  expect(view.onExecute.mock.calls[0][0]).toMatchObject({ scope: draft.scope, params: { right: { sheet_id: 3 } } });
});

it('exposes errors, clears them during retry and never replaces saved intent with defaults', async () => {
  joinApi.listSheets.mockRejectedValueOnce(new Error('Sheets unavailable'));
  const view = openSaved();
  await waitFor(() => expect(screen.getByText('Sheets unavailable', { selector: 'p' })).toBeVisible());
  expect(screen.getByTestId('generated-action-run')).toBeDisabled();
  const retry = deferred(); joinApi.listSheets.mockReturnValueOnce(retry.promise); refresh();
  expect(screen.queryByText('Sheets unavailable', { selector: 'p' })).not.toBeInTheDocument();
  expect(screen.getByTestId('field-join_key_left-0')).toBeDisabled();
  await act(async () => retry.resolve([joinLeft, joinRight])); await ready();
  fireEvent.click(screen.getByTestId('generated-action-run'));
  expect(view.onExecute.mock.calls[0][0]).toMatchObject(draft);
});

it('lets the newest refresh win without overwriting edited Params and names', async () => {
  const view = openSaved(); await ready(); choose('field-how', 'outer');
  const first = deferred(); const second = deferred();
  joinApi.listSheets.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
  refresh(); refresh();
  await act(async () => second.resolve([joinLeft, { ...joinRight, name: 'Newest' }])); await ready();
  await act(async () => first.resolve([joinLeft]));
  expect(screen.getByTestId('field-join_right_sheet')).toHaveValue('2');
  fireEvent.click(screen.getByTestId('generated-action-run'));
  expect(view.onExecute.mock.calls[0][0]).toMatchObject({ params: { how: 'outer' }, output_names: draft.output_names });
});

it('locks source controls on primary metadata revision and ignores the stale prop epoch', async () => {
  const view = openSaved(); await ready();
  const first = deferred(); const second = deferred();
  joinApi.listSheets.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
  view.rerender(view.rerenderForm({ sheet: { ...joinLeft, name: 'Revision A' } }));
  expect(screen.getByTestId('field-join_key_left-0')).toBeDisabled();
  expect(screen.getByTestId('generated-action-run')).toBeDisabled();
  view.rerender(view.rerenderForm({ sheet: { ...joinLeft, name: 'Revision B' } }));
  await act(async () => second.resolve([{ ...joinLeft, name: 'Revision B' }, joinRight])); await ready();
  await act(async () => first.resolve([joinLeft]));
  expect(screen.getByTestId('field-join_right_sheet')).toHaveValue('2');
  expect(screen.getByText('Left: Revision B. The selected rows come from this sheet.')).toBeVisible();
});
