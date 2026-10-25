// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  CompactSelection,
  type DataEditorProps,
  type GroupHeaderClickedEventArgs,
  type GridMouseEventArgs,
} from '@glideapps/glide-data-grid';
import type { ActionCatalogEntry, SheetMeta } from '../../src/api/types';
import { createProjectApi } from '../../src/api/real';
import { SheetGrid } from '../../src/grid/SheetGrid';
import {
  actionComparisonKind,
  supportsRowOverlayComparison,
} from '../../src/grid/actionComparisonCatalog';
import type {
  ActionColumnMenuEntry,
  ActionRunEntryDetails,
} from '../../src/grid/actionRunEntry';
import { GridColumnHeaderMenu } from '../../src/workspace/gridMenus';
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

const gridRows = [row({ first: 'A', second: 'B' }, { id: '9' })];
vi.mock('../../src/grid/useRowCache', () => ({
  useRowCache: () => ({
    getRow: (index: number) => gridRows[index],
    rowCount: gridRows.length,
    version: 0,
    refresh: vi.fn(),
    onVisibleRowsChanged: vi.fn(),
  }),
}));

const { render } = createWorkspaceTestHarness({
  projectId: 'entry-tests',
  api: { projectApi: createProjectApi('entry-tests') },
});

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.clearAllMocks();
});

function catalogWithComparison(comparison: unknown) {
  return { ui_hints: { comparison } } as Pick<ActionCatalogEntry, 'ui_hints'>;
}

describe('action comparison catalog gate', () => {
  it('accepts only the catalog row_overlay declaration', () => {
    expect(actionComparisonKind(catalogWithComparison({ kind: 'row_overlay' }))).toBe('row_overlay');
    expect(supportsRowOverlayComparison(catalogWithComparison({ kind: 'row_overlay' }))).toBe(true);
    expect(actionComparisonKind(catalogWithComparison({ kind: 'row-overlay' }))).toBeNull();
    expect(actionComparisonKind(catalogWithComparison('row_overlay'))).toBeNull();
    expect(actionComparisonKind(null)).toBeNull();
  });
});

const anchor = { sheetId: 'sheet-1', runId: '42', columnId: 'out-1' };
const menuEntry: ActionColumnMenuEntry = {
  anchor,
  actionTitle: 'Extract structured fields',
  comparisonKind: 'row_overlay',
};

function renderColumnMenu(entry: ActionColumnMenuEntry | null) {
  const compare = vi.fn();
  render(
    <GridColumnHeaderMenu
      state={{
        column: columnDef({ id: 'out-1', name: 'plaintiff', ai: aiMeta(), latestRunId: '42' }),
        columnIndex: 0,
        bounds: { x: 10, y: 10, width: 100, height: 30 },
      }}
      activeSort={null}
      activeFilter={null}
      onSort={vi.fn()}
      onClearSort={vi.fn()}
      onFilter={vi.fn()}
      onClearFilter={vi.fn()}
      onOpenFilterSidebar={vi.fn()}
      onAdvancedSort={vi.fn()}
      columnActions={[]}
      onColumnAction={vi.fn()}
      actionRunEntry={entry}
      onCompareActionRun={compare}
      frozenColumnCount={0}
      onFreezeColumns={vi.fn()}
      onUnfreezeColumns={vi.fn()}
      onOpenSettings={vi.fn()}
      onOpenMap={vi.fn()}
      mapAvailable={false}
      mapAvailabilityStatus="unavailable"
      mapAvailabilityReason=""
      onHideColumn={vi.fn()}
      adjacentHiddenColumns={[]}
      onUnhideColumns={vi.fn()}
      onInsertColumnLeft={vi.fn()}
      onInsertColumnRight={vi.fn()}
      onExportColumnTables={vi.fn()}
      isRowTitleColumn={false}
      onSetRowTitleColumn={vi.fn()}
    />,
  );
  return { compare };
}

describe('generated column menu entry', () => {
  it('shows only Compare and emits the exact whole-family run anchor', () => {
    const callbacks = renderColumnMenu(menuEntry);
    expect(screen.getByText('Extract structured fields')).toBeInTheDocument();
    expect(screen.queryByText('map.extract')).not.toBeInTheDocument();
    expect(screen.queryByText(/output columns?/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('header-menu-compare-action-run'));
    expect(callbacks.compare).toHaveBeenCalledWith(anchor);
    expect(screen.queryByText('Edit settings…')).not.toBeInTheDocument();
    expect(screen.queryByText('Earlier runs')).not.toBeInTheDocument();
  });

  it('does not invent action-run controls without a catalog-gated entry', () => {
    renderColumnMenu(null);
    expect(screen.queryByTestId('header-menu-action-run-section')).not.toBeInTheDocument();
  });

  it('keeps the whole-family entry available without verbose output metadata', () => {
    renderColumnMenu(menuEntry);
    expect(screen.getByTestId('header-menu-compare-action-run')).toBeEnabled();
  });
});

const details: ActionRunEntryDetails = {
  actionTitle: 'Extract structured fields',
  comparisonKind: 'row_overlay',
  sourceLabels: ['Court filing text'],
  modelLabel: 'Qwen 3 4B',
  routeLabel: 'on this computer',
  outputLabels: ['plaintiff', 'defendant'],
  rowCount: 240,
  ranAt: new Date().toISOString(),
  errorCount: 1,
};

describe('group-header provenance entry', () => {
  it('loads on menu activation, not hover, and compares from a complete run anchor', async () => {
    const load = vi.fn().mockResolvedValue(details);
    const compare = vi.fn();
    const sheet: SheetMeta = {
      id: 'sheet-1',
      name: 'Court filings',
      rowCount: 1,
      documentTextColumnIds: ['out-1'],
      columns: [
        columnDef({ id: 'out-1', name: 'plaintiff', ai: aiMeta(), latestRunId: '42' }),
        columnDef({ id: 'out-2', name: 'defendant', ai: aiMeta(), latestRunId: '42' }),
      ],
    };
    render(
      <SheetGrid
        sheet={sheet}
        dataVersion={0}
        liveRun={null}
        rowHeight={32}
        wrapText={false}
        columnGroupsStorageScope="entry-tests"
        onRowOpen={vi.fn()}
        onColumnOpen={vi.fn()}
        onCellEdit={vi.fn()}
        loadActionRunEntry={load}
        onCompareActionRun={compare}
      />,
    );
    const group = editor.props!.columns[0].group!;
    const hover = {
      kind: 'group-header',
      group,
      location: [0, -2],
      bounds: { x: 100, y: 0, width: 280, height: 28 },
      shiftKey: false,
      ctrlKey: false,
      metaKey: false,
      isTouch: false,
      isEdge: false,
      button: 0,
      buttons: 0,
      scrollEdge: [0, 0],
      localEventX: 1,
      localEventY: 1,
    } as GridMouseEventArgs;

    await act(async () => editor.props!.onItemHovered?.(hover));
    expect(load).not.toHaveBeenCalled();
    const activate = {
      bounds: hover.bounds,
      preventDefault: vi.fn(),
    } as unknown as GroupHeaderClickedEventArgs;
    await act(async () => editor.props!.onGroupHeaderClicked?.(0, activate));
    await waitFor(() => expect(load).toHaveBeenCalledWith(anchor));
    await waitFor(() => expect(screen.getByText('Extract structured fields')).toBeInTheDocument());
    expect(screen.getByText('Court filing text')).toBeInTheDocument();
    expect(screen.getByText('Qwen 3 4B · on this computer')).toBeInTheDocument();

    await act(async () => editor.props!.onGroupHeaderClicked?.(0, activate));
    expect(load).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByTestId('action-run-entry-compare'));
    expect(compare).toHaveBeenCalledWith(anchor);

    act(() => editor.props!.onGridSelectionChange?.({
      columns: CompactSelection.empty(),
      rows: CompactSelection.fromSingleSelection(0),
    }));
    expect(screen.getByRole('button', { name: 'Compare on 1 row…' })).toBeInTheDocument();
  });

  it('retries a transient run-entry failure on the next menu activation', async () => {
    const load = vi.fn()
      .mockRejectedValueOnce(new Error('temporary'))
      .mockResolvedValueOnce(details);
    const sheet: SheetMeta = {
      id: 'sheet-1',
      name: 'Court filings',
      rowCount: 1,
      columns: [
        columnDef({ id: 'out-1', name: 'plaintiff', ai: aiMeta(), latestRunId: '42' }),
        columnDef({ id: 'out-2', name: 'defendant', ai: aiMeta(), latestRunId: '42' }),
      ],
    };
    render(
      <SheetGrid
        sheet={sheet}
        dataVersion={0}
        liveRun={null}
        rowHeight={32}
        wrapText={false}
        columnGroupsStorageScope="entry-tests-retry"
        onRowOpen={vi.fn()}
        onColumnOpen={vi.fn()}
        onCellEdit={vi.fn()}
        loadActionRunEntry={load}
      />,
    );
    const activate = {
      bounds: { x: 100, y: 0, width: 280, height: 28 },
      preventDefault: vi.fn(),
    } as unknown as GroupHeaderClickedEventArgs;

    await act(async () => editor.props!.onGroupHeaderClicked?.(0, activate));
    await waitFor(() => expect(screen.getByText('Run details are unavailable.')).toBeInTheDocument());
    await act(async () => editor.props!.onGroupHeaderClicked?.(0, activate));
    await waitFor(() => expect(load).toHaveBeenCalledTimes(2));
    await act(async () => editor.props!.onGroupHeaderClicked?.(0, activate));
    expect(await screen.findByText('Extract structured fields')).toBeInTheDocument();
  });
});
