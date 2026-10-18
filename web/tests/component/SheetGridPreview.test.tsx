// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { CompactSelection, GridCellKind, type DataEditorProps } from '@glideapps/glide-data-grid';
import { SheetGrid } from '../../src/grid/SheetGrid';
import { createProjectApi } from '../../src/api/real';
import type { PreviewOverlayCell, PreviewOverlayColumn, SheetMeta } from '../../src/api/types';
import { columnDef, row } from '../support/domainFixtures';
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
vi.mock('../../src/grid/useRowCache', () => ({
  useRowCache: () => ({
    getRow: (index: number) => rows[index],
    rowCount: 2,
    version: 0,
    refresh: vi.fn(),
    onVisibleRowsChanged: visibleRowsChanged,
  }),
}));
const { render } = createWorkspaceTestHarness({ projectId: 'preview-grid', api: { projectApi: createProjectApi('preview-grid') } });
afterEach(() => { cleanup(); vi.restoreAllMocks(); visibleRowsChanged.mockClear(); });
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
    });
    expect(editor.props?.gridSelection.rows.toArray()).toEqual([0, 1]);
    expect(editor.props?.gridSelection.current).toBeUndefined();

    act(() => grid.onVisibleRegionChanged?.({ x: 0, y: 3, width: 1, height: 2 }));
    expect(visibleRowsChanged).not.toHaveBeenCalled();
  });
});
