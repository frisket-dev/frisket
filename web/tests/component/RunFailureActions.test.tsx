// @vitest-environment jsdom
import { useEffect } from 'react';
import { act, cleanup, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import type { SheetMeta } from '../../src/api/types';
import { ConfirmationRequiredError } from '../../src/api/open';
import { RunFailureTriage } from '../../src/components/RunFailureTriage';
import { createProjectApi } from '../../src/api/real';
import { useWorkspaceActionController } from '../../src/workspace/useWorkspaceActionController';
import { useWorkspaceGridController } from '../../src/workspace/useWorkspaceGridController';
import { columnDef, row } from '../support/domainFixtures';
import { sheetMeta } from '../support/actionFormFixtures';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const api = createProjectApi('triage-sheets');
const { render, stores } = createWorkspaceTestHarness({ projectId: 'triage-sheets', api: { projectApi: api } });
const sheets: SheetMeta[] = [1, 2].map((id) => sheetMeta(
  [columnDef({ id: String(id * 11), name: 'Answer', type: 'text' })],
  { id: String(id), name: `Sheet ${id}`, rowCount: 1 },
));
let actions: ReturnType<typeof useWorkspaceActionController>;
let grid: ReturnType<typeof useWorkspaceGridController>;
const showError = vi.fn();
function Probe() {
  const currentActions = useWorkspaceActionController({ sheet: sheets[0], closeRoutePanel: vi.fn(), startRun: vi.fn(), showError });
  const currentGrid = useWorkspaceGridController({ project: { id: 'triage-sheets', name: 'Triage' }, sheet: sheets[0],
    chromeState: { setOpenSplit: vi.fn(), setDocumentView: vi.fn(), setPromotedViews: vi.fn(), promotedViews: [] },
    refreshHistory: vi.fn(), refreshSheets: vi.fn(), showError });
  useEffect(() => { actions = currentActions; grid = currentGrid; });
  return null;
}
function mount() {
  stores.projectData.store.set((state) => ({ ...state, sheets }));
  stores.route.navigate({ ...stores.route.baseRoute(), sheetId: '1' }, 'replace');
  render(<Probe />);
}
afterEach(() => { cleanup(); vi.restoreAllMocks(); showError.mockClear(); });

it('retries failures on the source sheet while a same-named column is selected elsewhere', async () => {
  const failed = { ...row({ '22': null }), id: '3', provenance: { '22': {
    runId: '9', model: '', actionName: '', cost: 0, confidence: null, justification: '',
    currentValueRef: { kind: 'run_result' as const, runId: '9', rowId: '3', columnId: '22', opId: null },
  } } };
  const getSheetData = vi.spyOn(api, 'getSheetData').mockResolvedValue({ rows: [failed], total: 1, columns: sheets[1].columns });
  const runAction = vi.spyOn(api, 'runAction').mockResolvedValue({ status: 'started', runId: '10' });
  vi.spyOn(stores.job, 'afterBackfill').mockImplementation(() => {});
  mount();
  let accepted = false;
  await act(async () => { accepted = await actions.retryFailedRows('2', '22', 'any'); });
  expect(accepted).toBe(true);
  expect(getSheetData).toHaveBeenCalledWith('2', 0, 1000, {
    selection: { scope: { kind: 'sheet', sheet_id: 2 }, filters: [{ column_id: 22, condition: { failed: 'any' } }], order: [], limit: null }, columnIds: ['22'], valueMode: 'bounded',
  });
  expect(runAction).toHaveBeenCalledWith(expect.objectContaining({
    action_id: 'run.retry', scope: { kind: 'sheet_rows', sheet_id: 2, row_ids: [3] }, params: { source_run_id: 9 },
  }));
  expect(stores.route.store.get().sheetId).toBe('1');
});

it('reports cancelled confirmation as unaccepted and permits a later request', async () => {
  const failed = { ...row({ '22': null }), id: '3', provenance: { '22': {
    runId: '9', model: '', actionName: '', cost: 0, confidence: null, justification: '',
    currentValueRef: { kind: 'run_result' as const, runId: '9', rowId: '3', columnId: '22', opId: null },
  } } };
  vi.spyOn(api, 'getSheetData').mockResolvedValue({ rows: [failed], total: 1, columns: sheets[1].columns });
  const runAction = vi.spyOn(api, 'runAction')
    .mockRejectedValueOnce(new ConfirmationRequiredError({ cost: 3, rows: 1, promise_set_hash: 'quote' }, 'Confirm retry', 'model_cost'))
    .mockResolvedValueOnce({ status: 'started', runId: '10' });
  vi.spyOn(stores.job, 'requestCostConfirmation').mockResolvedValue(false);
  vi.spyOn(stores.job, 'afterBackfill').mockImplementation(() => {});
  mount();
  await act(async () => { expect(await actions.retryFailedRows('2', '22', 'any')).toBe(false); });
  expect(runAction).toHaveBeenCalledTimes(1);
  await act(async () => { expect(await actions.retryFailedRows('2', '22', 'any')).toBe(true); });
  expect(showError).not.toHaveBeenCalled();
});

it('shows failures on the source sheet and binds the source column after route reset', () => {
  mount();
  act(() => grid.applyGridFailedFilter('2', '22', 'any'));
  expect(stores.route.store.get().sheetId).toBe('2');
  expect(stores.gridView.store.get().applied).toMatchObject({
    filter: { Answer: { failed: 'any' } }, filterColumnIds: { Answer: 22 },
  });
});


it('keeps Retry accepted when a later original execution fails admission', async () => {
  const failed = (id: string, runId: string) => ({ ...row({ '22': null }), id, provenance: { '22': {
    runId, model: '', actionName: '', cost: 0, confidence: null, justification: '',
    currentValueRef: { kind: 'run_result' as const, runId, rowId: id, columnId: '22', opId: null },
  } } });
  vi.spyOn(api, 'getSheetData').mockResolvedValue({
    rows: [failed('3', '9'), failed('4', '11')], total: 2, columns: sheets[1].columns,
  });
  const runAction = vi.spyOn(api, 'runAction')
    .mockResolvedValueOnce({ status: 'started', runId: '10' })
    .mockRejectedValueOnce(new Error('The second execution is unavailable'));
  const afterBackfill = vi.spyOn(stores.job, 'afterBackfill').mockImplementation(() => {});
  mount();
  render(<RunFailureTriage rowErrors={{ totalFailedRows: 2, groups: [{
    message: 'Failed', count: 2, code: 'model_error', outcome: 'model_error', terminal: false, rowIds: ['3', '4'],
  }] }} onRetryRows={(outcome) => actions.retryFailedRows('2', '22', outcome)} />);
  const button = screen.getByRole('button', { name: 'Retry' });
  fireEvent.click(button);
  await waitFor(() => expect(button.textContent).toBe('Retry requested'));
  expect(button.hasAttribute('disabled')).toBe(true);
  expect(afterBackfill).toHaveBeenCalledExactlyOnceWith('10');
  expect(showError).toHaveBeenCalledWith('The second execution is unavailable');
  fireEvent.click(button);
  expect(runAction).toHaveBeenCalledTimes(2);
});

it('keeps retry and filter bindings on the original column after rename and reuse of its old name', async () => {
  const renamed: SheetMeta = { ...sheets[1], columns: [
    { ...sheets[1].columns[0], name: 'Renamed answer' },
    columnDef({ id: '33', name: 'Answer', type: 'text' }),
  ] };
  const getSheetData = vi.spyOn(api, 'getSheetData').mockResolvedValue({ rows: [], total: 0, columns: renamed.columns });
  mount();
  act(() => {
    stores.projectData.store.set((state) => ({ ...state, sheets: [sheets[0], renamed] }));
    navigateToSource();
    stores.gridView.applyFilter({ filter: { 'Renamed answer': { failed: 'any' } }, filterColumnIds: { 'Renamed answer': 33 } });
    grid.applyGridFailedFilter('2', '22', 'any');
  });
  expect(stores.gridView.store.get().applied.filterColumnIds).toEqual({ 'Renamed answer': 22 });
  await act(async () => { await actions.retryFailedRows('2', '22', 'any'); });
  expect(getSheetData).toHaveBeenCalledWith('2', 0, 1000, {
    selection: { scope: { kind: 'sheet', sheet_id: 2 }, filters: [{ column_id: 22, condition: { failed: 'any' } }], order: [], limit: null },
    columnIds: ['22'], valueMode: 'bounded',
  });
});
function navigateToSource() { stores.route.navigate({ ...stores.route.baseRoute(), sheetId: '2' }, 'push'); }
