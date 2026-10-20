// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it } from 'vitest';
import type { GeneratedActionDraft } from '../../src/api/types';
import { joinApi, joinLeft, joinRight, renderJoinForm } from '../support/renderJoinForm';

afterEach(cleanup);
beforeEach(() => joinApi.listSheets.mockReset().mockResolvedValue([joinLeft, joinRight]));
const saved: GeneratedActionDraft = { action_id: 'derive.join',
  scope: { kind: 'sheet_rows', sheet_id: 1, row_ids: [101, 102] },
  sheet_name: 'Joined records', params: { right: { sheet_id: 2 },
    join_keys: [{ left_column: 'code', right_column: 'code' }], how: 'outer', indicator: true,
    columns: [{ side: 'right', column: 'city' }, { side: 'left', column: 'city' },
      { side: 'right', column: 'city' }], max_output_rows: 1234 },
  output_names: { code: 'id', city_left: 'Reported city', city_right: 'Official city',
    city_right_2: 'Review copy', _merge: 'Match status' } };
async function ready() {
  await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
}
function openSaved() {
  return renderJoinForm({ initialDraft: structuredClone(saved), selectedRowIds: ['101', '102'],
    hasExactRowScopeInitializer: true });
}

it('uses served generated metadata and preserves ordered repeated projections, names, cap and selected scope', async () => {
  const view = openSaved();
  expect(view.entry.ui_hints.form).toBe('generated');
  expect(view.entry.ui_hints.dynamic_outputs).toBe(true);
  await ready();
  fireEvent.click(screen.getByTestId('generated-action-run'));
  expect(view.onExecute).toHaveBeenCalledWith({ ...saved, idempotency_key: expect.any(String) }, 'run');
});

it('bulk suffix editing changes only host output names and leaves semantic Params unchanged', async () => {
  const view = openSaved();
  await ready();
  fireEvent.change(screen.getByTestId('field-join_left_suffix'), { target: { value: '_reported' } });
  fireEvent.click(screen.getByText('Apply left suffix'));
  await ready();
  fireEvent.click(screen.getByTestId('generated-action-run'));
  expect(view.onExecute.mock.calls[0][0]).toMatchObject({ params: saved.params,
    output_names: { city_left: 'city_reported', city_right: 'Official city', _merge: 'Match status' } });
});

it('clicking projection field headings never activates a selected-column remove button', async () => {
  const view = openSaved();
  await ready();
  fireEvent.click(screen.getByText('Left fields', { exact: true }));
  fireEvent.click(screen.getByText('Right fields', { exact: true }));
  await ready();
  fireEvent.click(screen.getByTestId('generated-action-run'));
  expect(view.onExecute.mock.calls[0][0].params).toEqual(saved.params);
});

it('keeps projection reorder, duplicate and remove controls bound to their own row', async () => {
  const view = openSaved();
  await ready();
  expect(screen.getByRole('button', { name: 'Move projection 1 up' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Move projection 3 down' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: 'Move projection 2 up' }));
  fireEvent.click(screen.getByRole('button', { name: 'Duplicate projection 1' }));
  expect(screen.getByLabelText('Projection 4 column')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Remove projection 2' }));
  expect(screen.queryByLabelText('Projection 4 column')).not.toBeInTheDocument();
  await ready();
  fireEvent.click(screen.getByTestId('generated-action-run'));
  expect(view.onExecute.mock.calls[0][0].params.columns).toEqual([
    { side: 'left', column: 'city' }, { side: 'right', column: 'city' }, { side: 'right', column: 'city' },
  ]);
});

it('blocks Run while source metadata is pending and enables it after resolution', async () => {
  let resolve!: (value: typeof joinLeft[]) => void;
  joinApi.listSheets.mockReturnValueOnce(new Promise((done) => { resolve = done; }));
  const view = openSaved();
  expect(screen.getByTestId('generated-action-run')).toBeDisabled();
  await act(async () => resolve([joinLeft, joinRight]));
  await ready();
  expect(view.onExecute).not.toHaveBeenCalled();
});

it('follows source renames by stable ID and refuses same-name replacements after refresh', async () => {
  const view = openSaved();
  await ready();
  const renamed = { ...joinRight, name: 'Renamed zones', columns: joinRight.columns.map((column) =>
    column.id === '22' ? { ...column, name: 'New city label' } : column) };
  joinApi.listSheets.mockResolvedValueOnce([joinLeft, renamed]);
  fireEvent.click(screen.getByTestId('tabular-join-refresh-sheets'));
  await ready();
  fireEvent.click(screen.getByTestId('generated-action-run'));
  expect(view.onExecute.mock.calls[0][0].params.columns).toEqual([
    { side: 'right', column: 'New city label' }, { side: 'left', column: 'city' },
    { side: 'right', column: 'New city label' },
  ]);
  const replaced = { ...renamed, columns: renamed.columns.map((column) => column.id === '22'
    ? { ...column, id: '99' } : column) };
  joinApi.listSheets.mockResolvedValueOnce([joinLeft, replaced]);
  fireEvent.click(screen.getByTestId('tabular-join-refresh-sheets'));
  await waitFor(() => expect(screen.getByTestId('tabular-join-repair')).toHaveTextContent('New city label'));
  expect(screen.getByTestId('generated-action-run')).toBeDisabled();
});
