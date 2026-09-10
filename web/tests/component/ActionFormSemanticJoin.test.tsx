// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { GeneratedActionDraft } from '../../src/api/types';
import { joinApi, joinLeft, joinRight, renderJoinForm } from '../support/renderJoinForm';
import { installPopoverPolyfill } from '../support/domPolyfills';
installPopoverPolyfill();
afterEach(cleanup);
beforeEach(() => joinApi.listSheets.mockReset().mockResolvedValue([joinLeft, joinRight]));
const saved: GeneratedActionDraft = { action_id: 'join.semantic',
  scope: { kind: 'sheet_rows', sheet_id: 1, row_ids: [101, 102] }, sheet_name: 'Linked companies',
  params: { source: 'city', target: { sheet_id: 2, column: 'city' },
    carry: ['state'], match_threshold: 0.72, confident_threshold: 0.93 },
  output_names: { match_value: 'Best match', match_score: 'Similarity', matched_row_id: 'Matched row',
    source: 'Reported name', 'carry.state': 'Reported state' } };
function open(next: Partial<GeneratedActionDraft> = {}) {
  return renderJoinForm({ initialDraft: { ...structuredClone(saved), ...next },
    selectedRowIds: ['101', '102'], hasExactRowScopeInitializer: true }, 'join.semantic');
}
async function ready() { await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled()); }
function choose(id: string, value: string) { fireEvent.change(screen.getByTestId(id), { target: { value } }); }
function refresh() { fireEvent.click(screen.getByTestId('semantic-join-refresh-sheets')); }

it('starts a fresh column launch with no optional carry columns', async () => {
  const resolveParams = vi.fn(async ({ params }: { params: Record<string, unknown> }) => ({
    diagnostics: (params.carry as string[] | undefined)?.includes(String(params.source))
      ? { carry: { ok: false, message: 'Carry columns must exclude the source.' } } : {},
    logical_outputs: [
      { key: 'match_value', column_type: 'text' },
      { key: 'match_score', column_type: 'number' },
      { key: 'matched_row_id', column_type: 'integer' },
      { key: 'source', column_type: 'text' },
    ],
  }));
  const view = renderJoinForm({ initialSourceColumn: 'city', resolveParams }, 'join.semantic');
  await ready();
  fireEvent.click(screen.getByTestId('generated-action-run'));
  expect(view.onExecute.mock.calls[0][0].params).toMatchObject({ source: 'city', carry: [] });
});

it('uses the served generated contract with whole target, selected sources, independent child names and thresholds', async () => {
  const view = open(); await ready();
  expect(view.entry.ui_hints.form).toBe('generated');
  expect(view.entry.ui_hints.typed_action?.creates_sheet).toBe(true);
  expect(view.entry.required_capabilities).toContain('model:embed');
  expect(screen.getAllByTestId('field-source')).toHaveLength(1);
  expect(screen.getAllByTestId('field-semantic_target_column')).toHaveLength(1);
  expect(screen.getByTestId('field-output-source')).toHaveValue('Reported name');
  expect(screen.getByTestId('field-output-carry.state')).toHaveValue('Reported state');
  fireEvent.click(screen.getByTestId('generated-action-run'));
  expect(view.onExecute).toHaveBeenCalledWith({ ...saved, idempotency_key: expect.any(String) }, 'run');
  expect(view.onExecute.mock.calls[0][0]).not.toHaveProperty('confirmation');
});

it('preserves comma-bearing carry names as typed list items and excludes the selected source from choices', async () => {
  const primary = { ...joinLeft, columns: [...joinLeft.columns,
    { ...joinLeft.columns[2], id: '14', name: 'amount, usd' }] };
  joinApi.listSheets.mockResolvedValueOnce([primary, joinRight]);
  const view = renderJoinForm({ sheet: primary, initialDraft: { ...saved,
    params: { ...saved.params, carry: ['amount, usd'] }, output_names: undefined } }, 'join.semantic');
  await ready();
  const user = userEvent.setup();
  await user.click(screen.getByTestId('field-semantic_carry_columns'));
  const menu = within(screen.getByTestId('field-semantic_carry_columns-menu'));
  expect(menu.queryByRole('option', { name: /^city/, hidden: true })).not.toBeInTheDocument();
  expect(menu.getByRole('option', { name: /^amount, usd/, hidden: true })).toBeInTheDocument();
  await user.keyboard('{Escape}');
  fireEvent.click(screen.getByTestId('generated-action-run'));
  expect(view.onExecute.mock.calls[0][0].params.carry).toEqual(['amount, usd']);
});

it('follows target renames by ID and refuses a same-name replacement until explicitly reselected', async () => {
  const view = open(); await ready();
  const renamed = { ...joinRight, name: 'Current registry', columns: joinRight.columns.map((c) =>
    c.id === '22' ? { ...c, name: 'Organization name' } : c) };
  joinApi.listSheets.mockResolvedValueOnce([joinLeft, renamed]); refresh(); await ready();
  expect(screen.getByTestId('field-semantic_target_column')).toHaveValue('Organization name');
  joinApi.listSheets.mockResolvedValueOnce([joinLeft, { ...renamed, columns: renamed.columns.map((c) =>
    c.id === '22' ? { ...c, id: '99' } : c) }]); refresh();
  await waitFor(() => expect(screen.getByTestId('field-semantic_target_column')).toHaveValue(''));
  expect(screen.getByTestId('semantic-join-repair')).toHaveTextContent('Organization name');
  expect(screen.getByTestId('generated-action-run')).toBeDisabled();
  choose('field-semantic_target_column', 'Organization name'); await ready();
  fireEvent.click(screen.getByTestId('generated-action-run'));
  expect(view.onExecute.mock.calls[0][0].params.target).toEqual({ sheet_id: 2, column: 'Organization name' });
});

it('does not silently repair a deleted primary source or carry column by matching its old name', async () => {
  const view = open(); await ready();
  const primary = { ...joinLeft, columns: joinLeft.columns.map((c) =>
    c.id === '12' || c.id === '13' ? { ...c, id: c.id + '-replacement' } : c) };
  joinApi.listSheets.mockResolvedValueOnce([primary, joinRight]);
  view.rerender(view.rerenderForm({ sheet: primary }));
  await waitFor(() => expect(screen.getByTestId('field-source')).toHaveValue(''));
  expect(screen.getByTestId('semantic-join-repair')).toHaveTextContent('state');
  expect(screen.getByTestId('generated-action-run')).toBeDisabled();
  choose('field-source', 'city');
  expect(screen.getByTestId('generated-action-run')).toBeDisabled();
  const user = userEvent.setup();
  await user.click(screen.getByRole('button', { name: 'Remove' }));
  await user.click(screen.getByTestId('field-semantic_carry_columns'));
  await user.click(within(screen.getByTestId('field-semantic_carry_columns-menu')).getByRole('option', { name: /^state/, hidden: true }));
  await user.keyboard('{Escape}'); await ready();
  fireEvent.click(screen.getByTestId('generated-action-run'));
  expect(view.onExecute.mock.calls[0][0].params).toMatchObject({ source: 'city', carry: ['state'] });
});

it('keeps a missing target sheet as a repair and requires a deliberate target column after switching', async () => {
  const view = open({ params: { ...saved.params, target: { sheet_id: 999, column: 'city' } } });
  await waitFor(() => expect(screen.getByTestId('semantic-join-repair')).toBeVisible());
  expect(screen.getByTestId('field-semantic_target_sheet')).toHaveValue('');
  choose('field-semantic_target_sheet', '2');
  expect(screen.getByTestId('field-semantic_target_column')).toHaveValue('');
  expect(screen.getByTestId('generated-action-run')).toBeDisabled();
  choose('field-semantic_target_column', 'zone'); await ready();
  fireEvent.click(screen.getByTestId('generated-action-run'));
  expect(view.onExecute.mock.calls[0][0]).toMatchObject({ scope: saved.scope,
    params: { target: { sheet_id: 2, column: 'zone' }, carry: ['state'] } });
});

it('uses the host estimate and submits unchanged intent without manufacturing consent', async () => {
  const estimateAction = vi.fn(async () => ({ cost: 0.02, quoted_usd: 0.02, rows: 2, llm: true }));
  const view = renderJoinForm({ initialDraft: saved, selectedRowIds: ['101', '102'],
    hasExactRowScopeInitializer: true, estimateAction }, 'join.semantic');
  await ready(); await waitFor(() => expect(estimateAction).toHaveBeenCalled());
  expect(estimateAction.mock.calls[0][0]).toMatchObject(saved);
  fireEvent.click(screen.getByTestId('generated-action-run'));
  expect(view.onExecute.mock.calls[0][0]).not.toHaveProperty('confirmation');
  expect(view.onExecute.mock.calls[0][0]).not.toHaveProperty('confirmed');
});

it('blocks during metadata loading and does not revive an unmounted editor after its response arrives', async () => {
  let resolve!: (sheets: typeof joinLeft[]) => void;
  joinApi.listSheets.mockReturnValueOnce(new Promise((done) => { resolve = done; }));
  const view = open();
  expect(screen.getByTestId('generated-action-run')).toBeDisabled();
  view.unmount();
  await act(async () => resolve([joinLeft, joinRight]));
  expect(view.onExecute).not.toHaveBeenCalled();
});
