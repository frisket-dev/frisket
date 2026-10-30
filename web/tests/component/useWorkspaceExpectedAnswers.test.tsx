// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import type { GridApiPort } from '../../src/api/ports';
import type { ActionCatalogEntry, SheetMeta } from '../../src/api/types';
import { useWorkspaceExpectedAnswers } from '../../src/workspace/useWorkspaceExpectedAnswers';

const calls = vi.hoisted(() => ({ getLinks: vi.fn(), previewLink: vi.fn(), getContext: vi.fn() }));
vi.mock('../../src/api/evaluation', () => ({ createEvaluationApi: () => ({ getLinks: calls.getLinks, previewLink: calls.previewLink }) }));
vi.mock('../../src/api/actionComparison', () => ({ createActionComparisonApi: () => ({ getContext: calls.getContext }) }));
afterEach(() => { cleanup(); vi.resetAllMocks(); });
const sheets: SheetMeta[] = [
  { id: '1', name: 'Current', rowCount: 1, columns: [{ id: '11', name: 'answer', type: 'text', latestRunId: '101' }] },
  { id: '2', name: 'Other', rowCount: 1, columns: [{ id: '22', name: 'answer', type: 'text', latestRunId: '202' }] },
];
const showError = vi.fn();
function prepare() {
  calls.getLinks.mockResolvedValue({ links: [{ id: 3, action_id: 'map.ask', target_sheet_id: 1, expected_sheet_id: 2, target_sheet_name: 'Current', expected_sheet_name: 'Other', keys: [], fields: [], hidden: false }] });
  calls.previewLink.mockResolvedValue({ matched: 1 });
  calls.getContext.mockImplementation(async ({ source }) => ({ available: true, draft: { action_id: 'map.ask' }, sheet: { id: source.run_id === 101 ? 1 : 2, name: 'sheet' }, output_columns: [] }));
  const getSheetData = vi.fn(async (id: string) => ({ rows: [{ id: id === '1' ? '10' : '20' }] }));
  return { getSheetData } as unknown as GridApiPort;
}
it('reads only current-sheet runs until the join is opened', async () => {
  const api = prepare();
  const { result } = renderHook(() => useWorkspaceExpectedAnswers('p', sheets[0], sheets, api, [], [], 0, showError));
  await waitFor(() => expect(calls.getContext).toHaveBeenCalledTimes(1));
  expect(api.getSheetData).toHaveBeenCalledTimes(1);
  expect(api.getSheetData).toHaveBeenCalledWith('1', 0, 1, expect.anything());
  act(() => result.current.openJoin());
  await waitFor(() => expect(calls.getContext).toHaveBeenCalledTimes(2));
  expect(api.getSheetData).toHaveBeenCalledWith('2', 0, 1, expect.anything());
  expect(api.getSheetData).toHaveBeenCalledTimes(2);
});
it('does not reload links, match counts or run contexts for unrelated data refreshes', async () => {
  const api = prepare();
  const { rerender } = renderHook(({ version, current, all }) => useWorkspaceExpectedAnswers('p', current, all, api, [], [], version, showError),
    { initialProps: { version: 0, current: sheets[0], all: sheets } });
  await waitFor(() => expect(calls.previewLink).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(calls.getContext).toHaveBeenCalledTimes(1));
  rerender({ version: 1, current: { ...sheets[0], columns: [...sheets[0].columns] }, all: sheets.map(item => ({ ...item, columns: [...item.columns] })) });
  await act(async () => { await Promise.resolve(); });
  expect(calls.getLinks).toHaveBeenCalledTimes(1);
  expect(calls.previewLink).toHaveBeenCalledTimes(1);
  expect(calls.getContext).toHaveBeenCalledTimes(1);
});

it('offers the existing Translate engine comparison only when its catalog action exists', async () => {
  const api = prepare();
  const entries = [{ kind: 'map.translate', title: 'Translate', ui_hints: {} }] as ActionCatalogEntry[];
  const { result } = renderHook(() => useWorkspaceExpectedAnswers('p', sheets[0], sheets, api, entries, [], 0, showError));
  expect(result.current.actionChoices).toEqual([expect.objectContaining({ kind: 'map.translate', group: 'rows' })]);
  expect(result.current.actionChoices).not.toEqual(expect.arrayContaining([expect.objectContaining({ kind: 'media.to_markdown' })]));
});
