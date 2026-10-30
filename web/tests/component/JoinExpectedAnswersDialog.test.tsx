// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeAll, expect, it, vi } from 'vitest';
import { JoinExpectedAnswersDialog, type ExpectedLinkDraft } from '../../src/components/expectedAnswers/JoinExpectedAnswersDialog';
import { installDialogPolyfill } from '../support/domPolyfills';

beforeAll(installDialogPolyfill);
afterEach(cleanup);
const sheets = [{ id: 1, name: 'Target', columns: [{ id: 11, name: 'id' }, { id: 12, name: 'answer' }] }, { id: 2, name: 'Expected', columns: [{ id: 21, name: 'id' }, { id: 22, name: 'answer' }] }];
const actions = [{ id: 'run1', actionId: 'ask', label: 'Ask', targetSheetId: 1, outputs: [{ id: 12, name: 'answer', logicalKey: 'answer' }] }];
const preview = { matched: 2, not_found: 0, ambiguous_expected: 0, ambiguous_target: 0, unmatched_expected_row_ids: [] };
const link: ExpectedLinkDraft = { action_id: 'ask', target_sheet_id: 1, expected_sheet_id: 2, keys: [{ expected_column_id: 21, target_column_id: 11 }], fields: [], hidden: false };

it('blocks ambiguous matching in either direction and defaults an exact-name field to Exact', async () => {
  const onPreview = vi.fn().mockResolvedValueOnce({ ...preview, ambiguous_target: 1 }).mockResolvedValue(preview);
  const onSave = vi.fn().mockResolvedValue(undefined);
  render(<JoinExpectedAnswersDialog sheets={sheets} actions={actions} initialLink={link} initialStep="keys" onPreview={onPreview} onSave={onSave} onImport={vi.fn()} onClose={vi.fn()} />);
  await screen.findByText(/1 match more than one/);
  expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
  fireEvent.change(screen.getByLabelText('Target key 1'), { target: { value: '12' } });
  await waitFor(() => expect(screen.getByRole('button', { name: 'Next' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: 'Next' }));
  expect(screen.getByLabelText('Role for answer')).toHaveValue('12');
  expect(screen.getByLabelText('Match rule for answer')).toHaveValue('exact');
  await waitFor(() => expect(screen.getByRole('button', { name: 'Join' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: 'Join' }));
  await waitFor(() => expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ fields: [expect.objectContaining({ expected_column_id: 22, output_column_id: 12, rule: 'exact' })] }), []));
});

it('edits the output family belonging to an existing link when two runs share an action', async () => {
  const second = { id: 'run2', actionId: 'ask', label: 'Ask · second run', targetSheetId: 1, outputs: [{ id: 13, name: 'other answer', logicalKey: 'other' }] };
  const existing: ExpectedLinkDraft = { ...link, fields: [{ expected_column_id: 22, output_column_id: 13, logical_key: 'other', rule: 'hand', hidden: false, source: 'import' }] };
  render(<JoinExpectedAnswersDialog sheets={sheets} actions={[...actions, second]} initialLink={existing} initialStep="columns" onPreview={vi.fn().mockResolvedValue(preview)} onSave={vi.fn()} onImport={vi.fn()} onClose={vi.fn()} />);
  expect(screen.getByLabelText('Role for answer')).toHaveValue('13');
  expect(screen.getByLabelText('Match rule for answer')).toHaveValue('hand');
  expect(screen.getByRole('option', { name: 'Expected · other answer' })).toBeInTheDocument();
});

it('submits only deliberately changed match rules when editing an existing link', async () => {
  const extendedSheets = [sheets[0], { ...sheets[1], columns: [...sheets[1].columns, { id: 23, name: 'other' }] }];
  const extendedActions = [{ ...actions[0], outputs: [...actions[0].outputs, { id: 13, name: 'other', logicalKey: 'other' }] }];
  const existing: ExpectedLinkDraft = { ...link, fields: [
    { expected_column_id: 22, output_column_id: 12, logical_key: 'answer', rule: 'exact', hidden: false, source: 'import' },
    { expected_column_id: 23, output_column_id: 13, logical_key: 'other', rule: 'hand', hidden: false, source: 'import' },
  ] };
  const onSave = vi.fn().mockResolvedValue(undefined);
  render(<JoinExpectedAnswersDialog sheets={extendedSheets} actions={extendedActions} initialLink={existing} initialStep="columns" onPreview={vi.fn().mockResolvedValue(preview)} onSave={onSave} onImport={vi.fn()} onClose={vi.fn()} />);
  fireEvent.change(screen.getByLabelText('Match rule for answer'), { target: { value: 'number' } });
  await waitFor(() => expect(screen.getByRole('button', { name: 'Join' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: 'Join' }));
  await waitFor(() => expect(onSave).toHaveBeenCalledWith(expect.anything(), [{ expected_column_id: 22, rule: 'number' }]));
});
