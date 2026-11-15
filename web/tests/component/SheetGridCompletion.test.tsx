// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { CompactSelection, GridCellKind, type DataEditorProps } from '@glideapps/glide-data-grid';
import { SheetGrid } from '../../src/grid/SheetGrid';
import { createProjectApi } from '../../src/api/real';
import type { Row, RunProgress, SheetMeta } from '../../src/api/types';
import { columnDef, row } from '../support/domainFixtures';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const editor = vi.hoisted(() => ({ props: null as DataEditorProps | null, mounts: 0 }));
vi.mock('@glideapps/glide-data-grid', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@glideapps/glide-data-grid')>();
  const { forwardRef, useEffect, useImperativeHandle } = await import('react');
  return {
    ...actual,
    DataEditor: forwardRef((props: DataEditorProps, ref) => {
      editor.props = props;
      useEffect(() => { editor.mounts += 1; }, []);
      useImperativeHandle(ref, () => ({ updateCells: vi.fn(), getBounds: vi.fn() }));
      return <div data-testid="canvas-scroller" />;
    }),
  };
});

const projectApi = createProjectApi('completion-grid');
const { render } = createWorkspaceTestHarness({ projectId: 'completion-grid', api: { projectApi } });
const sheet: SheetMeta = {
  id: '1', name: 'Results', rowCount: 1,
  columns: [columnDef({ id: 'answer', name: 'Answer', type: 'text' })],
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  editor.mounts = 0;
  localStorage.clear();
});

it('keeps streamed results, canvas, selection and scroll visible while completion revalidates', async () => {
  let publishFinal!: (page: { columns: typeof sheet.columns; total: number; rows: Row[] }) => void;
  const finalPage = new Promise<{ columns: typeof sheet.columns; total: number; rows: Row[] }>((resolve) => { publishFinal = resolve; });
  const getSheetData = vi.spyOn(projectApi, 'getSheetData')
    .mockResolvedValueOnce({ columns: sheet.columns, total: 1, rows: [row({ answer: 'Streamed result' })] })
    .mockImplementation(() => finalPage);
  const liveRun: RunProgress = {
    runId: '42', actionKind: 'map.summarize', actionName: 'Summarize', sheetId: sheet.id,
    targetColumnId: 'answer', status: 'running', completedRows: 1, totalRows: 1, failedRows: 0, costSoFar: 0,
  };
  const props = {
    sheet, rowHeight: 32, wrapText: false, columnGroupsStorageScope: 'completion-grid',
    onRowOpen: vi.fn(), onColumnOpen: vi.fn(), onCellEdit: vi.fn(),
  };
  const view = render(<SheetGrid {...props} dataVersion={0} liveRun={liveRun} />);
  await waitFor(() => expect(editor.props?.getCellContent([0, 0])).toMatchObject({ data: 'Streamed result' }));
  const host = view.getByTestId('grid');
  const scroller = view.getByTestId('canvas-scroller');
  scroller.scrollTop = 320;
  const selection = {
    columns: CompactSelection.empty(), rows: CompactSelection.empty(),
    current: { cell: [0, 0] as const, range: { x: 0, y: 0, width: 1, height: 1 }, rangeStack: [] },
  };
  act(() => editor.props?.onGridSelectionChange?.(selection));

  // This is the completion transition emitted by jobStore: terminal progress
  // plus projectData.invalidate(), while the final response is still pending.
  view.rerender(<SheetGrid {...props} dataVersion={1} liveRun={{ ...liveRun, status: 'complete' }} />);
  await waitFor(() => expect(getSheetData.mock.calls.length).toBeGreaterThan(1));
  expect(view.getByTestId('grid')).toBe(host);
  expect(view.queryByTestId('grid-data-loading')).toBeNull();
  expect(editor.props?.getCellContent([0, 0])).toMatchObject({ data: 'Streamed result' });
  expect(editor.props?.gridSelection.current?.cell).toEqual([0, 0]);
  expect(view.getByTestId('canvas-scroller')).toBe(scroller);
  expect(scroller.scrollTop).toBe(320);
  expect(editor.mounts).toBe(1);

  await act(async () => publishFinal({ columns: sheet.columns, total: 1, rows: [row({ answer: 'Final result' })] }));
  await waitFor(() => expect(editor.props?.getCellContent([0, 0])).toMatchObject({ data: 'Final result' }));
  expect(view.queryByTestId('grid-data-loading')).toBeNull();
  expect(editor.props?.gridSelection.current?.cell).toEqual([0, 0]);
  expect(editor.mounts).toBe(1);
});

it('retains visible values but blocks stale edits while a newly revealed column loads', async () => {
  const columns = [...sheet.columns, columnDef({ id: 'hidden', name: 'Hidden value', type: 'text' })];
  const projectedSheet = { ...sheet, columns };
  let publishProjection!: (page: { columns: typeof columns; total: number; rows: Row[] }) => void;
  const projection = new Promise<{ columns: typeof columns; total: number; rows: Row[] }>((resolve) => { publishProjection = resolve; });
  const initialRow = { ...row({ answer: 'Keep visible' }), loadedColumnIds: ['answer'] };
  const freshRow = { ...row({ answer: 'Keep visible', hidden: 'Actual saved value' }), loadedColumnIds: ['answer', 'hidden'] };
  const getSheetData = vi.spyOn(projectApi, 'getSheetData')
    .mockResolvedValueOnce({ columns, total: 1, rows: [initialRow] })
    .mockImplementation(() => projection);
  const edit = vi.fn().mockResolvedValue(undefined);
  const props = {
    sheet: projectedSheet, dataVersion: 0, liveRun: null, rowHeight: 32, wrapText: false,
    columnGroupsStorageScope: 'completion-grid', onRowOpen: vi.fn(), onColumnOpen: vi.fn(), onCellEdit: edit,
  };
  const view = render(<SheetGrid {...props} hiddenColumnNames={['Hidden value']} />);
  await waitFor(() => expect(editor.props?.getCellContent([0, 0])).toMatchObject({ data: 'Keep visible', allowOverlay: true }));
  const staleEditorCallback = editor.props!.onCellEdited!;
  const host = view.getByTestId('grid');
  view.rerender(<SheetGrid {...props} hiddenColumnNames={[]} />);
  await waitFor(() => expect(getSheetData).toHaveBeenCalledTimes(2));
  expect(view.getByTestId('grid')).toBe(host);
  expect(view.queryByTestId('grid-data-loading')).toBeNull();
  expect(editor.props?.getCellContent([0, 0])).toMatchObject({ data: 'Keep visible', allowOverlay: false });
  expect(editor.props?.getCellContent([1, 0])).toMatchObject({ kind: GridCellKind.Loading, allowOverlay: false });
  const changedCell = { kind: GridCellKind.Text, data: 'overwrite', displayData: 'overwrite', allowOverlay: true } as const;
  act(() => {
    editor.props?.onCellEdited?.([1, 0], changedCell);
    editor.props?.onCellEdited?.([0, 0], changedCell);
    // An overlay already open before the projection change cannot write either.
    staleEditorCallback([0, 0], changedCell);
  });
  expect(edit).not.toHaveBeenCalled();

  await act(async () => publishProjection({ columns, total: 1, rows: [freshRow] }));
  await waitFor(() => expect(editor.props?.getCellContent([1, 0])).toMatchObject({ data: 'Actual saved value', allowOverlay: true }));
  await act(async () => editor.props?.onCellEdited?.([1, 0], changedCell));
  expect(edit).toHaveBeenCalledOnce();
  expect(edit).toHaveBeenCalledWith(freshRow, columns[1], 'overwrite');
});
