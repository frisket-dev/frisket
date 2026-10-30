// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { CompactSelection, GridCellKind, type DataEditorProps } from '@glideapps/glide-data-grid';
import { SheetGrid } from '../../src/grid/SheetGrid';
import { createProjectApi } from '../../src/api/real';
import type { ColumnRun, PreviewOverlayCell, PreviewOverlayColumn, RunProgress, SheetMeta } from '../../src/api/types';
import { aiMeta, columnDef, row } from '../support/domainFixtures';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const editor = vi.hoisted(() => ({ props: null as DataEditorProps | null }));
vi.mock('@glideapps/glide-data-grid', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@glideapps/glide-data-grid')>();
  const { forwardRef } = await import('react');
  return {
    ...actual,
    DataEditor: forwardRef((_props: DataEditorProps, ref) => {
      void ref;
      editor.props = _props;
      return null;
    }),
  };
});
const rows = [row({ old: 'COMMITTED' }, { id: '1' }), row({ old: 'UNSAMPLED' }, { id: '2', index: 1 })];
const visibleRowsChanged = vi.hoisted(() => vi.fn());
const refreshRows = vi.hoisted(() => vi.fn());
vi.mock('../../src/grid/useRowCache', () => ({
  useRowCache: () => ({
    getRow: (index: number) => rows[index],
    rowCount: 2,
    version: 0,
    refresh: refreshRows,
    onVisibleRowsChanged: visibleRowsChanged,
  }),
}));
const projectApi = createProjectApi('preview-grid');
const { render } = createWorkspaceTestHarness({ projectId: 'preview-grid', api: { projectApi } });
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  visibleRowsChanged.mockClear();
  refreshRows.mockClear();
  localStorage.clear();
});
const sheet: SheetMeta = { id: '1', name: 'Videos', rowCount: 2, columns: [columnDef({ id: 'old', name: 'Cuts', type: 'text' })] };
const column: PreviewOverlayColumn = { name: 'Cuts', columnType: 'timeline_points', format: null, hidden: false, overwritesColumnId: 'old' };
const temporal: PreviewOverlayCell = { value: {
  schema_version: 'frisket.timeline_points.v1',
  timeline: { artifact_stable_id: 'source_artifact:preview-only', fingerprint: `sha256:${'a'.repeat(64)}`, duration_ms: 30_000 },
  items: [{ id: 'cut', at_ms: 12_500 }],
} };

async function mount(sample: PreviewOverlayCell, descriptor = column) {
  const open = vi.fn();
  const edit = vi.fn().mockResolvedValue(undefined);
  const facet = vi.fn();
  await act(async () => {
    render(<SheetGrid sheet={sheet} dataVersion={0} liveRun={null} rowHeight={32} wrapText={false}
      columnGroupsStorageScope="preview-grid" previewColumns={[descriptor]} previewCells={{ '1': { [descriptor.name]: sample } }}
      onRowOpen={open} onColumnOpen={vi.fn()} onCellEdit={edit} onFacetValueFilter={facet}
    />);
  });
  return { open, edit, facet, grid: editor.props! };
}

async function mountImportPlaceholders() {
  const open = vi.fn();
  const edit = vi.fn().mockResolvedValue(undefined);
  const selected = vi.fn();
  await act(async () => {
    render(<SheetGrid sheet={sheet} dataVersion={0} liveRun={null} rowHeight={32} wrapText={false}
      columnGroupsStorageScope="preview-grid" pendingImportRowCount={3}
      onRowOpen={open} onColumnOpen={vi.fn()} onCellEdit={edit} onSelectedRowsChange={selected}
    />);
  });
  return { open, edit, selected, grid: editor.props! };
}

describe('preview grid interactions', () => {
  it('soft-refreshes committed import rows without changing the hard cache version', async () => {
    const props = {
      sheet, dataVersion: 7, liveRun: null, rowHeight: 32, wrapText: false,
      columnGroupsStorageScope: 'preview-grid', pendingImportRowCount: 3,
      onRowOpen: vi.fn(), onColumnOpen: vi.fn(), onCellEdit: vi.fn(),
    } as const;
    const view = render(<SheetGrid {...props} importRowRefreshKey="table-1:running:10" />);
    expect(refreshRows).not.toHaveBeenCalled();

    view.rerender(<SheetGrid {...props} pendingImportRowCount={2} importRowRefreshKey="table-1:running:11" />);
    await waitFor(() => expect(refreshRows).toHaveBeenCalledTimes(1));
    view.rerender(<SheetGrid {...props} pendingImportRowCount={0} importRowRefreshKey="table-1:completed:11" />);
    await waitFor(() => expect(refreshRows).toHaveBeenCalledTimes(2));
  });

  it.each([null, 'old'])('uses sampled types and opens sampled detail for target %s', async (overwritesColumnId) => {
    const descriptor = { ...column, overwritesColumnId };
    const { grid, open, edit } = await mount(temporal, descriptor);
    const index = overwritesColumnId === null ? 1 : 0;
    expect(grid.getCellContent([index, 0])).toMatchObject({
      kind: GridCellKind.Custom, data: { kind: 'frisket-timeline', label: '0:12.500' }, readonly: true, allowOverlay: false,
    });
    act(() => grid.onCellActivated?.([index, 0]));
    expect(open).toHaveBeenCalledWith(rows[0], overwritesColumnId === null ? undefined : sheet.columns[0], { column: descriptor, cell: temporal });
    act(() => grid.onCellEdited?.([index, 0], { kind: GridCellKind.Text, data: 'changed', displayData: 'changed', allowOverlay: true }));
    expect(edit).not.toHaveBeenCalled();
  });

  it.each([{ value: null }, { value: null, error: 'Failed sample' }, { value: false }, { value: 0 }])('protects sampled %j but keeps absent rows normal', async (sample) => {
    const descriptor = { ...column, columnType: 'text' };
    const { grid, open, edit, facet } = await mount(sample, descriptor);
    const event = { preventDefault: vi.fn() } as unknown as Parameters<NonNullable<DataEditorProps['onCellClicked']>>[1];
    act(() => grid.onCellClicked?.([0, 0], event));
    expect(event.preventDefault).toHaveBeenCalled();
    expect(open).toHaveBeenLastCalledWith(rows[0], sheet.columns[0], { column: descriptor, cell: sample });
    expect(facet).not.toHaveBeenCalled();
    act(() => grid.onCellEdited?.([0, 0], { kind: GridCellKind.Text, data: 'changed', displayData: 'changed', allowOverlay: true }));
    expect(edit).not.toHaveBeenCalled();
    expect(grid.getCellContent([0, 1])).toMatchObject({ data: 'UNSAMPLED' });
    await act(async () => grid.onCellEdited?.([0, 1], { kind: GridCellKind.Text, data: 'normal edit', displayData: 'normal edit', allowOverlay: true }));
    expect(edit).toHaveBeenCalledWith(rows[1], sheet.columns[0], 'normal edit');
  });

  it('appends inert loading rows without exposing virtual indexes to selection', async () => {
    const { grid, open, edit, selected } = await mountImportPlaceholders();

    expect(grid.rows).toBe(5);
    expect(grid.getCellContent([0, 4])).toMatchObject({
      kind: GridCellKind.Loading,
      allowOverlay: false,
      skeletonWidth: 80,
    });
    act(() => grid.onCellActivated?.([0, 4]));
    act(() => grid.onCellEdited?.([0, 4], {
      kind: GridCellKind.Text,
      data: 'virtual edit',
      displayData: 'virtual edit',
      allowOverlay: true,
    }));
    expect(open).not.toHaveBeenCalled();
    expect(edit).not.toHaveBeenCalled();

    act(() => grid.onGridSelectionChange?.({
      columns: CompactSelection.empty(),
      rows: CompactSelection.fromSingleSelection([0, 5]),
      current: {
        cell: [0, 4],
        range: { x: 0, y: 0, width: 1, height: 5 },
        rangeStack: [],
      },
    }));
    expect(selected).toHaveBeenLastCalledWith({
      sheetId: '1',
      rowIds: ['1', '2'],
      rowIndexes: [0, 1],
      cachedAnchors: [{ index: 0, rowId: '1' }, { index: 1, rowId: '2' }],
    });
    expect(editor.props?.gridSelection.rows.toArray()).toEqual([0, 1]);
    expect(editor.props?.gridSelection.current).toBeUndefined();

    act(() => grid.onVisibleRegionChanged?.({ x: 0, y: 3, width: 1, height: 2 }));
    expect(visibleRowsChanged).not.toHaveBeenCalled();
  });

  it('shows background OCR pending state only for the child run row scope', async () => {
    const documentSheet: SheetMeta = {
      ...sheet,
      columns: [columnDef({ id: 'text', name: 'Text', type: 'text' })],
      annotatedTextColumnIds: ['text'],
    };
    const backgroundLiveRun: RunProgress = {
      runId: '42',
      actionKind: 'media.ocr',
      actionName: 'OCR PDFs',
      sheetId: sheet.id,
      targetColumnId: '',
      targetRowIds: ['2'],
      status: 'running',
      completedRows: 0,
      totalRows: 1,
      failedRows: 0,
      costSoFar: 0,
    };
    await act(async () => {
      render(<SheetGrid sheet={documentSheet} dataVersion={0} liveRun={null}
        backgroundLiveRun={backgroundLiveRun} rowHeight={32} wrapText={false}
        columnGroupsStorageScope="preview-grid"
        onRowOpen={vi.fn()} onColumnOpen={vi.fn()} onCellEdit={vi.fn()}
      />);
    });

    expect(editor.props!.getCellContent([0, 0])).toMatchObject({
      kind: GridCellKind.Text,
      data: '',
    });
    expect(editor.props!.getCellContent([0, 1])).toMatchObject({
      kind: GridCellKind.Custom,
      data: { label: 'Waiting for OCR' },
    });
  });

  it.each([
    ['map.summarize', 'Summarize', {}, 'Summarize'],
    ['media.transcribe', 'Transcribe', {}, 'Transcribe'],
    ['media.transcribe', 'Transcribe', { group_label: 'Interview transcript' }, 'Interview transcript'],
  ])('labels document text neutrally and %s with its public title or explicit label', async (actionKind, actionName, spec, expectedLabel) => {
    const ordinaryRun: ColumnRun = {
      runId: '43',
      actionKind,
      actionName,
      model: 'local',
      status: 'completed',
      spec,
      totalRows: 2,
      completedRows: 2,
      failedRows: 0,
      cost: 0,
      startedAt: null,
      finishedAt: null,
      durationMs: null,
      tokensIn: null,
      tokensOut: null,
      current: true,
      humanScore: { passed: 0, graded: 0 },
      judgeScores: [],
    };
    const getColumnRuns = vi.spyOn(projectApi, 'getColumnRuns').mockResolvedValue({
      columnName: 'Summary',
      offset: 0,
      limit: 20,
      totalRuns: 1,
      hasMore: false,
      nextOffset: null,
      currentRun: ordinaryRun,
      currentRunLoaded: true,
      latestRun: ordinaryRun,
      latestRunLoaded: true,
      mixedOrigins: false,
      runs: [ordinaryRun],
    });
    localStorage.setItem('frisket:column-groups:preview-grid:1', JSON.stringify({
      42: {
        label: 'media.read_pdf_text',
        showConfidence: true,
        showJustification: true,
      },
    }));
    const groupedSheet: SheetMeta = {
      ...sheet,
      documentTextColumnIds: ['text'],
      columns: [
        columnDef({ id: 'text', name: 'Text', ai: aiMeta(), latestRunId: '42' }),
        columnDef({ id: 'blocks', name: 'Search positions', ai: aiMeta(), latestRunId: '42' }),
        columnDef({ id: 'summary', name: 'Summary', ai: aiMeta({ actionName: 'AI run' }), latestRunId: '43' }),
        columnDef({ id: 'summary-confidence', name: 'summary_confidence', ai: aiMeta({ actionName: 'AI run' }), latestRunId: '43' }),
      ],
    };
    await act(async () => {
      render(<SheetGrid sheet={groupedSheet} dataVersion={0} liveRun={null}
        rowHeight={32} wrapText={false} columnGroupsStorageScope="preview-grid"
        onRowOpen={vi.fn()} onColumnOpen={vi.fn()} onCellEdit={vi.fn()}
      />);
    });

    const documentGroup = editor.props!.columns[0].group!;
    const ordinaryGroup = editor.props!.columns[2].group!;
    expect(editor.props!.getGroupDetails?.(documentGroup)).toMatchObject({
      name: 'Document text',
    });
    await waitFor(() => expect(getColumnRuns).toHaveBeenCalledTimes(1));
    expect(getColumnRuns).toHaveBeenCalledWith('summary');
    await waitFor(() => expect(editor.props!.getGroupDetails?.(ordinaryGroup)).toMatchObject({
      name: expectedLabel,
    }));
    expect(editor.props!.getGroupDetails?.(documentGroup)).toMatchObject({
      name: 'Document text',
    });
  });
});

it('renders projected expected values in the ordinary grid and routes edits to their source', async () => {
  const target = { ...sheet, columns: [columnDef({ id: '10', name: 'Answer', type: 'text', format: 'markdown' })] };
  const expected = { id: 'expected:4:20', link_id: 4, expected_column_id: 20, output_column_id: 10,
    logical_key: 'answer', name: 'answer', rule: 'exact' as const, hidden: false, source: 'import' as const, sourceColumn: columnDef({ id: '20', name: 'answer', type: 'text' }) };
  const cell = { value: 'verified', source_row_id: 50, source_column_id: 20, checked: true };
  const project = vi.fn().mockResolvedValue({ links: [], columns: [expected], rows: { '1': { [expected.id]: cell }, '2': { [expected.id]: { ...cell, source_row_id: null, value: null, checked: false } } } });
  const edit = vi.fn().mockResolvedValue(undefined);
  render(<SheetGrid sheet={target} dataVersion={0} liveRun={null} rowHeight={32} wrapText={false}
    columnGroupsStorageScope="expected-grid" onRowOpen={vi.fn()} onColumnOpen={vi.fn()} onCellEdit={vi.fn()}
    expectedColumns={[expected]} projectExpected={project} onExpectedEdit={edit} />);
  await waitFor(() => expect(editor.props?.getCellContent([1, 0])).toMatchObject({ kind: GridCellKind.Text, data: 'verified', readonly: false, allowOverlay: true }));
  expect(editor.props?.columns.map(item => item.id)).toEqual(['10', expected.id]);
  expect(editor.props?.columns[1]).toMatchObject({ title: 'Expected · answer', icon: 'typeLink', themeOverride: { bgHeader: '#dcefec', bgCell: '#ffffff' } });
  await act(async () => editor.props?.onCellEdited?.([1, 0], { kind: GridCellKind.Text, data: 'corrected', displayData: 'corrected', allowOverlay: true }));
  expect(edit).toHaveBeenCalledWith('1', expected, cell, 'corrected');
});
