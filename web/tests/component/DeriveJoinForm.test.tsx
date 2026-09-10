// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, it } from 'vitest';
import { joinApi, joinLeft, joinRight, renderJoinForm } from '../support/renderJoinForm';
import { installPopoverPolyfill } from '../support/domPolyfills';
installPopoverPolyfill();
afterEach(cleanup);
beforeEach(() => joinApi.listSheets.mockReset().mockResolvedValue([joinLeft, joinRight]));
function open(columns: null | Array<{ side: 'left' | 'right'; column: string }> = null) {
  return renderJoinForm({ initialDraft: { action_id: 'derive.join',
    scope: { kind: 'sheet_rows', sheet_id: 1 }, sheet_name: 'Joined', params: {
      right: { sheet_id: 2 }, join_keys: [{ left_column: 'code', right_column: 'code' }], columns,
    } } });
}
async function ready() { await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled()); }
function choose(id: string, value: string) { fireEvent.change(screen.getByTestId(id), { target: { value } }); }

it('uses labelled panel selectors and keeps names in the generated host rather than Params', async () => {
  const view = open(); await ready();
  expect(screen.getByTestId('field-how')).toHaveAttribute('data-native-select-escape', 'panel-select-trigger');
  await userEvent.selectOptions(screen.getByLabelText('Key pair 1 left column'), 'state');
  choose('field-how', 'outer');
  fireEvent.click(screen.getByTestId('field-join_indicator'));
  fireEvent.change(screen.getByTestId('field-max_output_rows'), { target: { value: '4567' } });
  fireEvent.change(screen.getByTestId('field-output-_merge'), { target: { value: 'Provenance' } });
  fireEvent.click(screen.getByText('Apply right suffix')); await ready();
  fireEvent.click(screen.getByTestId('generated-action-run'));
  expect(view.onExecute.mock.calls[0][0]).toMatchObject({ sheet_name: 'Joined',
    params: { how: 'outer', indicator: true, max_output_rows: 4567,
      join_keys: [{ left_column: 'state', right_column: 'code' }] },
    output_names: { _merge: 'Provenance' } });
  expect(view.onExecute.mock.calls[0][0].params).not.toHaveProperty('suffixes');
  expect(view.onExecute.mock.calls[0][0].params).not.toHaveProperty('output_name');
});

it('preserves arbitrary ordered projections when duplicating, moving, replacing and removing occurrences', async () => {
  const view = open([{ side: 'right', column: 'zone' }, { side: 'left', column: 'city' }]); await ready();
  fireEvent.click(screen.getByLabelText('Duplicate projection 1'));
  fireEvent.click(screen.getByLabelText('Move projection 3 up'));
  choose('join-projection-column-2', 'city');
  fireEvent.click(screen.getByLabelText('Remove projection 1')); await ready();
  fireEvent.click(screen.getByTestId('generated-action-run'));
  expect(view.onExecute.mock.calls[0][0].params.columns).toEqual([
    { side: 'left', column: 'city' }, { side: 'right', column: 'city' },
  ]);
});

it('adds complete key pairs up to the admitted cap and removes only the selected pair', async () => {
  const view = open(); await ready();
  for (let i = 0; i < 7; i++) fireEvent.click(screen.getByTestId('join-key-pair-add'));
  expect(screen.getByTestId('join-key-pair-add')).toBeDisabled();
  choose('field-join_key_left-1', 'state');
  fireEvent.click(screen.getByTestId('join-key-pair-remove-0')); await ready();
  fireEvent.click(screen.getByTestId('generated-action-run'));
  const keys = view.onExecute.mock.calls[0][0].params.join_keys;
  expect(keys).toHaveLength(7);
  expect(keys[0]).toEqual({ left_column: 'state', right_column: 'code' });
});

it('switches from all to explicit picks and back only after the last explicit removal', async () => {
  const view = open(); await ready();
  const user = userEvent.setup();
  await user.click(screen.getByTestId('field-join_right_columns'));
  await user.click(within(screen.getByTestId('field-join_right_columns-menu')).getByRole('option', { name: /^zone/, hidden: true }));
  expect(screen.getByTestId('join-projection-column-0')).toHaveValue('zone');
  await user.keyboard('{Escape}');
  fireEvent.click(screen.getByLabelText('Remove projection 1')); await ready();
  fireEvent.click(screen.getByTestId('generated-action-run'));
  expect(view.onExecute.mock.calls[0][0].params.columns).toBeNull();
});

it.each([
  ['field-join_right_sheet', 'field-join_right_sheet-menu', 'select'],
  ['field-join_left_columns', 'field-join_left_columns-menu', 'picker'],
])('makes an already-open %s popup inert during refresh and after failure', async (id, menuId, kind) => {
  open(); await ready();
  if (kind === 'select') fireEvent.mouseDown(screen.getByTestId(id));
  else await userEvent.click(screen.getByTestId(id));
  expect(screen.getByTestId(menuId)).toBeInTheDocument();
  let reject!: (error: Error) => void;
  joinApi.listSheets.mockReturnValueOnce(new Promise((_, fail) => { reject = fail; }));
  fireEvent.click(screen.getByTestId('tabular-join-refresh-sheets'));
  expect(screen.queryByTestId(menuId)).not.toBeInTheDocument();
  expect(screen.getByTestId('generated-action-run')).toBeDisabled();
  await act(async () => reject(new Error('Refresh failed')));
  expect(screen.queryByTestId(menuId)).not.toBeInTheDocument();
  expect(screen.getByTestId('generated-action-run')).toBeDisabled();
});

it('defaults only an unbound fresh right reference and keeps a saved missing identity for repair', async () => {
  renderJoinForm();
  await waitFor(() => expect(screen.getByTestId('field-join_right_sheet')).toHaveValue('2'));
  expect(screen.getByTestId('field-join_key_right-0')).toHaveValue('code');
  expect(screen.getByTestId('field-join_right_sheet')).not.toHaveTextContent('Saved sheet unavailable');
});
