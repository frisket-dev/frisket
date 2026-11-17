// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { useEffect, type ReactElement } from 'react';
import { act, cleanup, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeAll, expect, it, vi } from 'vitest';
import type { Row, SheetMeta } from '../../src/api/types';
import { createProjectApi } from '../../src/api/real';
import { useReplayReviewController } from '../../src/workspace/useReplayReviewController';
import { columnDef, row } from '../support/domainFixtures';
import { installPopoverPolyfill } from '../support/domPolyfills';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const api = createProjectApi('replay-identity');
const { render, stores } = createWorkspaceTestHarness({ projectId: 'replay-identity', api: { projectApi: api } });
const sheets: SheetMeta[] = [1, 2].map((id) => ({ id: String(id), name: `Sheet ${id}`, rowCount: 1,
  columns: [columnDef({ id: String(id * 11), name: 'Answer', type: 'text', latestRunId: String(id * 100), replayPendingCount: 1 })],
}));
let latest: ReturnType<typeof useReplayReviewController>;
const showError = vi.fn();
function Probe({ sheet }: { sheet: SheetMeta }) {
  const controller = useReplayReviewController({ sheet, refreshHistory: vi.fn(), refreshSheets: vi.fn(), showError });
  useEffect(() => { latest = controller; });
  return <>{controller.replayColumnAnnotations.map((annotation) => <div key={annotation.key}>{annotation.message}</div>)}{controller.replayReviewPopover}</>;
}
function page(sheet: SheetMeta) {
  const columnId = sheet.columns[0].id;
  const data: Row = { ...row({ [columnId]: `Edited ${sheet.id}` }), id: String(Number(sheet.id) * 10),
    replayPending: { [columnId]: { freshValue: `Generated ${sheet.id}`, generatedValueHash: `hash-${sheet.id}`, runId: String(Number(sheet.id) * 100) } },
  };
  return { rows: [data], columns: sheet.columns, total: 1 };
}
function navigate(sheetId: string) { stores.route.navigate({ ...stores.route.baseRoute(), sheetId }, 'push'); }
function mount() { navigate('1'); return render(<Probe sheet={sheets[0]} />); }
function savedAction(name: 'onAccept' | 'onKeep') {
  return (latest.replayReviewPopover as ReactElement<Record<typeof name, () => void>>).props[name];
}
beforeAll(installPopoverPolyfill);
afterEach(() => { cleanup(); vi.restoreAllMocks(); showError.mockClear(); });

it('discards a deferred review load after navigating to another sheet', async () => {
  let finish!: (value: ReturnType<typeof page>) => void;
  const getSheetData = vi.spyOn(api, 'getSheetData').mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
  const view = mount();
  fireEvent.click(screen.getByTestId('replay-review-button'));
  expect(getSheetData).toHaveBeenCalledWith('1', 0, 500, { columnIds: ['11'], valueMode: 'bounded' });
  act(() => { navigate('2'); view.rerender(<Probe sheet={sheets[1]} />); });
  await act(async () => { finish(page(sheets[0])); });
  expect(screen.queryByTestId('replay-accept-popover')).toBeNull();
});

it.each(['onAccept', 'onKeep'] as const)('rejects a saved %s callback after sheet changes, including a return to the original sheet', async (action) => {
  vi.spyOn(api, 'getSheetData').mockResolvedValue(page(sheets[0]));
  const accept = vi.spyOn(api, 'acceptReplayValue').mockResolvedValue();
  const dismiss = vi.spyOn(api, 'dismissReplayPending').mockResolvedValue();
  const view = mount();
  fireEvent.click(screen.getByTestId('replay-review-button'));
  await screen.findByTestId('replay-accept-popover');
  const obsolete = savedAction(action);
  act(() => { navigate('2'); view.rerender(<Probe sheet={sheets[1]} />); obsolete(); });
  act(() => { navigate('1'); view.rerender(<Probe sheet={sheets[0]} />); obsolete(); });
  expect(accept).not.toHaveBeenCalled();
  expect(dismiss).not.toHaveBeenCalled();
  fireEvent.click(screen.getByTestId('replay-review-button'));
  await screen.findByTestId('replay-accept-popover');
  act(savedAction(action));
  await waitFor(() => expect(action === 'onAccept' ? accept : dismiss).toHaveBeenCalledWith('1', '11', '10', 'hash-1', '100'));
});

it('keeps a newer sheet review when an already admitted source-sheet action finishes', async () => {
  vi.spyOn(api, 'getSheetData').mockImplementation(async (sheetId) => page(sheets.find((sheet) => sheet.id === sheetId)!));
  let finish!: () => void;
  const accept = vi.spyOn(api, 'acceptReplayValue').mockImplementation(() => new Promise<void>((resolve) => { finish = resolve; }));
  const view = mount();
  fireEvent.click(screen.getByTestId('replay-review-button'));
  await screen.findByTestId('replay-accept-popover');
  act(savedAction('onAccept'));
  expect(accept).toHaveBeenCalledWith('1', '11', '10', 'hash-1', '100');
  act(() => { navigate('2'); view.rerender(<Probe sheet={sheets[1]} />); });
  fireEvent.click(screen.getByTestId('replay-review-button'));
  await waitFor(() => expect(screen.getByTestId('replay-newer-generated')).toHaveTextContent('Generated 2'));
  await act(async () => { finish(); });
  expect(screen.getByTestId('replay-newer-generated')).toHaveTextContent('Generated 2');
});

it('starts a fresh interactive review on the same sheet while an earlier action is still pending', async () => {
  vi.spyOn(api, 'getSheetData').mockResolvedValue(page(sheets[0]));
  let finish!: () => void;
  vi.spyOn(api, 'acceptReplayValue').mockImplementation(() => new Promise<void>((resolve) => { finish = resolve; }));
  mount();
  fireEvent.click(screen.getByTestId('replay-review-button'));
  await screen.findByTestId('replay-accept-popover');
  act(savedAction('onAccept'));
  expect(screen.getByTestId('replay-accept-button')).toBeDisabled();
  fireEvent.click(screen.getByTestId('replay-review-button'));
  await waitFor(() => expect(screen.getByTestId('replay-accept-button')).toBeEnabled());
  await act(async () => { finish(); });
  expect(screen.getByTestId('replay-accept-button')).toBeEnabled();
  expect(screen.getByTestId('replay-newer-generated')).toHaveTextContent('Generated 1');
});


function columnCommand(command: 'accept-all' | 'dismiss') {
  if (command === 'accept-all') fireEvent.click(screen.getByTestId('replay-accept-all-button'));
  else act(() => latest.replayColumnAnnotations[0].onDismiss?.());
}

it.each(['accept-all', 'dismiss'] as const)('closes the superseded row review before a failed %s command and permits a fresh review', async (command) => {
  vi.spyOn(api, 'getSheetData').mockResolvedValue(page(sheets[0]));
  let finishRow!: () => void;
  vi.spyOn(api, 'acceptReplayValue').mockImplementation(() => new Promise<void>((resolve) => { finishRow = resolve; }));
  const failed = vi.spyOn(api, command === 'accept-all' ? 'acceptReplayValuesInColumn' : 'dismissReplayPending')
    .mockRejectedValue(new Error('Column command failed'));
  mount();
  fireEvent.click(screen.getByTestId('replay-review-button'));
  await screen.findByTestId('replay-accept-popover');
  act(savedAction('onAccept'));
  expect(screen.getByTestId('replay-accept-button')).toBeDisabled();
  columnCommand(command);
  expect(screen.queryByTestId('replay-accept-popover')).toBeNull();
  await waitFor(() => expect(showError).toHaveBeenCalledWith('Column command failed'));
  expect(failed).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByTestId('replay-review-button'));
  await waitFor(() => expect(screen.getByTestId('replay-accept-button')).toBeEnabled());
  await act(async () => { finishRow(); });
  expect(screen.getByTestId('replay-accept-button')).toBeEnabled();
});

it.each(['accept-all', 'dismiss'] as const)('keeps a newer review and its busy state when a deferred %s command finishes', async (command) => {
  vi.spyOn(api, 'getSheetData').mockResolvedValue(page(sheets[0]));
  let finishColumn!: () => void;
  const columnApi = vi.spyOn(api, command === 'accept-all' ? 'acceptReplayValuesInColumn' : 'dismissReplayPending')
    .mockImplementation(() => new Promise<void>((resolve) => { finishColumn = resolve; }));
  let finishRow!: () => void;
  vi.spyOn(api, 'acceptReplayValue').mockImplementation(() => new Promise<void>((resolve) => { finishRow = resolve; }));
  mount();
  fireEvent.click(screen.getByTestId('replay-review-button'));
  await screen.findByTestId('replay-accept-popover');
  columnCommand(command);
  expect(screen.queryByTestId('replay-accept-popover')).toBeNull();
  await waitFor(() => expect(columnApi).toHaveBeenCalledOnce());
  fireEvent.click(screen.getByTestId('replay-review-button'));
  await screen.findByTestId('replay-accept-popover');
  act(savedAction('onAccept'));
  expect(screen.getByTestId('replay-accept-button')).toBeDisabled();
  await act(async () => { finishColumn(); });
  expect(screen.getByTestId('replay-accept-popover')).toBeInTheDocument();
  expect(screen.getByTestId('replay-accept-button')).toBeDisabled();
  await act(async () => { finishRow(); });
  expect(screen.getByTestId('replay-accept-button')).toBeEnabled();
});
