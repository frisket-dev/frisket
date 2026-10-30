// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeAll, expect, it, vi } from 'vitest';
import { JoinExpectedAnswersDialog, type ExpectedLinkDraft } from '../../src/components/expectedAnswers/JoinExpectedAnswersDialog';
import { installDialogPolyfill } from '../support/domPolyfills';

beforeAll(installDialogPolyfill);
afterEach(cleanup);
const sheets = [{ id: 1, name: 'Target', columns: [{ id: 11, name: 'id' }, { id: 12, name: 'answer' }] }, { id: 2, name: 'Expected', columns: [{ id: 21, name: 'id' }, { id: 22, name: 'answer' }] }];
const actions = [{ id: 'ask', label: 'Ask', targetSheetId: 1, outputs: [{ id: 12, name: 'answer', logicalKey: 'answer' }] }];
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
  await waitFor(() => expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ fields: [expect.objectContaining({ expected_column_id: 22, output_column_id: 12, rule: 'exact' })] })));
});
