// @vitest-environment jsdom
// The sidebar's Rows controls driven through the real grid controller and
// grid view store: what a delayed search commit writes, and where.
import '@testing-library/jest-dom/vitest';
import { useEffect, useState } from 'react';
import { act, cleanup, fireEvent, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { SheetMeta, ViewSelection } from '../../src/api/types';
import { createProjectApi } from '../../src/api/real';
import { useCurrentViewSelection } from '../../src/bind/useCurrentViewSelection';
import { FilterRowsBinding } from '../../src/workspace/FilterSidebar';
import { useWorkspaceGridController } from '../../src/workspace/useWorkspaceGridController';
import { columnDef } from '../support/domainFixtures';
import { sheetMeta } from '../support/actionFormFixtures';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const api = createProjectApi('rows-controls');
const { render, stores } = createWorkspaceTestHarness({ projectId: 'rows-controls', api: { projectApi: api } });
const sheets: SheetMeta[] = [1, 2].map((id) => sheetMeta(
  [
    columnDef({ id: String(id * 10 + 1), name: 'title', type: 'text' }),
    columnDef({ id: String(id * 10 + 2), name: 'body', type: 'text' }),
    columnDef({ id: String(id * 10 + 3), name: 'size', type: 'number' }),
  ],
  { id: String(id), name: `Sheet ${id}`, rowCount: 3 },
));

let grid: ReturnType<typeof useWorkspaceGridController>;
let selection: ViewSelection | null = null;
let showRows!: (show: boolean) => void;
let showSheet!: (sheet: SheetMeta) => void;

function Probe() {
  const [sheet, setSheet] = useState(sheets[0]);
  const [rows, setRows] = useState(true);
  const currentGrid = useWorkspaceGridController({
    project: { id: 'rows-controls', name: 'Rows' }, sheet,
    chromeState: { setOpenSplit: vi.fn(), setDocumentView: vi.fn(), setPromotedViews: vi.fn(), promotedViews: [] },
    refreshHistory: vi.fn(), refreshSheets: vi.fn(), showError: vi.fn(),
  });
  const currentSelection = useCurrentViewSelection(sheet);
  useEffect(() => {
    grid = currentGrid;
    selection = currentSelection;
    showRows = setRows;
    showSheet = setSheet;
  }, [currentGrid, currentSelection]);
  return rows ? (
    <FilterRowsBinding
      sheet={sheet}
      searchOnlyColumns={new Set()}
      applyRowSearch={currentGrid.applyRowSearch}
      applyRowLimit={currentGrid.applyRowLimit}
      applyRowOrder={currentGrid.applyRowOrder}
    />
  ) : null;
}

function mount() {
  stores.projectData.store.set((state) => ({ ...state, sheets }));
  stores.route.navigate({ ...stores.route.baseRoute(), sheetId: '1' }, 'replace');
  render(<Probe />);
}

const searchBox = () => screen.getByTestId('sheet-filter-search');
const type = (text: string) => fireEvent.change(searchBox(), { target: { value: text } });

beforeEach(() => {
  vi.useFakeTimers();
  stores.gridView.resetForSheetChange();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

it('keeps a pending search when the Rows controls unmount for a tab switch on the same sheet', () => {
  mount();
  type('northstar');
  act(() => showRows(false));
  act(() => { vi.advanceTimersByTime(500); });
  expect(selection?.search).toEqual({ text: 'northstar' });
});

it('commits a pending search when the search box loses focus', () => {
  mount();
  type('northstar');
  fireEvent.blur(searchBox());
  expect(selection?.search).toEqual({ text: 'northstar' });
});

it('never writes a pending search from the previous sheet into the next one', () => {
  mount();
  type('northstar');
  act(() => {
    stores.route.navigate({ ...stores.route.baseRoute(), sheetId: '2' }, 'push');
    showSheet(sheets[1]);
  });
  act(() => { vi.advanceTimersByTime(500); });
  expect(selection?.scope).toEqual({ kind: 'sheet', sheet_id: 2 });
  expect(selection?.search).toBeUndefined();
  expect(stores.gridView.store.get().applied.filter).toBeNull();
});

it('a delayed search keeps a limit set while it was pending', () => {
  mount();
  type('acme');
  fireEvent.change(screen.getByTestId('sheet-filter-limit'), { target: { value: '5' } });
  expect(selection?.limit).toBe(5);
  act(() => { vi.advanceTimersByTime(500); });
  expect(selection?.search).toEqual({ text: 'acme' });
  expect(selection?.limit).toBe(5);
});

it('a search filters in row order: committing it drops any relevance order', () => {
  mount();
  act(() => { stores.gridView.setSelection({ ...selection!, order: [{ score: 'search', dir: 'desc' }] }); });
  type('budget');
  act(() => { vi.advanceTimersByTime(500); });
  expect(selection?.search).toEqual({ text: 'budget' });
  expect(selection?.order).toEqual([]);
});

it('a delayed search keeps a column sort chosen while it was pending', () => {
  mount();
  type('budget');
  act(() => { grid.applyRowOrder({ column: 'size', dir: 'desc' }); });
  act(() => { vi.advanceTimersByTime(500); });
  expect(selection?.search).toEqual({ text: 'budget' });
  expect(selection?.order).toEqual([{ column_id: 13, dir: 'desc' }]);
});

it('editing the search of a View keeps its other contains conditions', () => {
  mount();
  act(() => {
    grid.applyGridFilterSpec({ body: { contains: 'invoice' } });
    grid.applyRowLimit(null);
    stores.gridView.setSelection({ ...selection!, search: { text: 'Northstar' } });
  });
  expect(screen.getByTestId('sheet-filter-search')).toHaveValue('Northstar');
  type('Acme');
  act(() => { vi.advanceTimersByTime(500); });
  expect(selection?.search).toEqual({ text: 'Acme' });
  expect(selection?.filters).toEqual([{ column_id: 12, condition: { contains: 'invoice' } }]);
  type('');
  expect(selection?.search).toBeUndefined();
  expect(selection?.filters).toEqual([{ column_id: 12, condition: { contains: 'invoice' } }]);
});

it('moving a column search keeps the contains condition of another column', () => {
  mount();
  act(() => { grid.applyGridFilterSpec({ body: { contains: 'a' }, title: { contains: 'b' } }); });
  expect(screen.getByTestId('sheet-filter-search')).toHaveValue('a');
  fireEvent.change(screen.getByTestId('sheet-filter-scope'), { target: { value: 'all' } });
  expect(selection?.search).toEqual({ text: 'a' });
  expect(selection?.filters).toEqual([{ column_id: 11, condition: { contains: 'b' } }]);
});
