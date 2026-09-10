// Unit tests of gridViewStore's own actions in isolation — no cross-store
// composition here. Cross-domain transaction-parity tests live in
// state/workspaceTransitions.test.ts, not this file.

import { describe, expect, it } from 'vitest';
import {
  createGridViewStore,
  selectInlineFilterColumn,
  selectInlineFilterOpen,
} from './gridViewStore';
import type { GridFilterSpec, GridSortSpec } from '../api/open';

describe('createGridViewStore — unit', () => {
  it('starts with the same defaults createWorkspaceUiState used for the grid slice', () => {
    const { store } = createGridViewStore();
    const s = store.get();
    expect(s.draft).toEqual({
      sortColumn: '',
      sortDirection: 'asc',
    });
    expect(s.applied).toEqual({ filter: null, sort: null, filterValueLabel: null });
    expect(s.sortPanelOpen).toBe(false);
    expect(s.columnOrderBySheet).toEqual({});
    expect(s.frozenColumnCountBySheet).toEqual({});
    expect(s.hiddenColumnsBySheet).toEqual({});
    expect(s.columnGroupSpecs).toEqual([]);
    expect(s.columnGroupsVersion).toBe(0);
    expect(s.rowHeight).toBe(34);
    expect(s.wrapText).toBe(false);
    expect(s.activeSavedViewId).toBeNull();
    expect(s.inlineFilter).toEqual({ sheetId: '', open: false, column: '' });
  });

  it('hydrates every persisted row-height choice and falls back for unsupported values', () => {
    try {
      for (const rowHeight of [26, 34, 48, 68, 96]) {
        localStorage.setItem('frisket:row-height', String(rowHeight));
        expect(createGridViewStore().store.get().rowHeight).toBe(rowHeight);
      }

      localStorage.setItem('frisket:row-height', '42');
      expect(createGridViewStore().store.get().rowHeight).toBe(34);
    } finally {
      localStorage.removeItem('frisket:row-height');
    }
  });

  it('applyFilter updates applied.filter, leaving sort untouched', () => {
    const { store, applySort, applyFilter } = createGridViewStore();
    applySort({ column: 'name', direction: 'desc', sort: [{ column: 'name', dir: 'desc' }] });
    const filter: GridFilterSpec = { status: { eq: 'open' } };
    applyFilter({ filter });

    const s = store.get();
    expect(s.applied.filter).toEqual(filter);
    // Sort from the earlier applySort call is untouched.
    expect(s.applied.sort).toEqual([{ column: 'name', dir: 'desc' }]);
    expect(s.draft.sortColumn).toBe('name');
  });

  it('applySort updates draft sort fields and applied.sort, leaving filter untouched', () => {
    const { store, applyFilter, applySort } = createGridViewStore();
    const filter: GridFilterSpec = { status: { eq: 'open' } };
    applyFilter({ filter });
    const sort: GridSortSpec = [{ column: 'name', dir: 'asc' }];
    applySort({ column: 'name', direction: 'asc', sort });

    const s = store.get();
    expect(s.applied.sort).toEqual(sort);
    expect(s.applied.filter).toEqual(filter);
  });

  it('clearFilter clears only the applied filter', () => {
    const { store, applyFilter, clearFilter } = createGridViewStore();
    applyFilter({ filter: { status: { between: { start: '1', end: '2' } } } });
    clearFilter();
    const s = store.get();
    expect(s.applied.filter).toBeNull();
  });

  // --- applied.filterValueLabel: the spelling carried alongside a filter whose
  // payload cannot hold one (an entity_eq FINGERPRINT selector holds a
  // comparison token nobody wrote). One producer — the Mentions panel's group
  // click, via useWorkspaceModel's applyGridEntityFilter — and it must be gone
  // the moment the filter it described is.
  const acmeGroupFilter: GridFilterSpec = {
    entities: { entity_eq: { type: 'organization', fingerprint: 'acme corp' } },
  };

  it('applyFilter carries filterValueLabel onto applied, and nulls it for an application without one', () => {
    const { store, applyFilter } = createGridViewStore();
    applyFilter({
      filter: acmeGroupFilter,
      filterValueLabel: 'Acme Corp.',
    });
    expect(store.get().applied.filterValueLabel).toBe('Acme Corp.');

    // A later filter that knows no spelling REPLACES the label rather than
    // leaving the previous group's spelling attached to a different filter.
    applyFilter({
      filter: { status: { eq: 'open' } },
    });
    expect(store.get().applied.filterValueLabel).toBeNull();
  });

  it('clearFilter drops filterValueLabel with the filter it described', () => {
    const { store, applyFilter, clearFilter } = createGridViewStore();
    applyFilter({
      filter: acmeGroupFilter,
      filterValueLabel: 'Acme Corp.',
    });
    clearFilter();
    expect(store.get().applied.filter).toBeNull();
    expect(store.get().applied.filterValueLabel).toBeNull();
  });

  it('applySavedViewGrid restores a filter spec with NO label — a saved view remembers no spelling', () => {
    const { store, applyFilter, applySavedViewGrid } = createGridViewStore();
    applyFilter({
      filter: acmeGroupFilter,
      filterValueLabel: 'Acme Corp.',
    });
    applySavedViewGrid({
      viewId: 1,
      sheetId: 'sheet-1',
      filter: acmeGroupFilter,
      sort: null,
      draftSortColumn: '',
      draftSortDirection: 'asc',
      columns: ['entities'],
      hiddenColumns: [],
      columnGroupSpecs: [],
    });
    // The SAME filter is applied again — but the label would now be a spelling
    // this restore never learned, so it is null and the chip names the type.
    expect(store.get().applied.filter).toEqual(acmeGroupFilter);
    expect(store.get().applied.filterValueLabel).toBeNull();
  });

  it('resetForSheetChange and resetForLensEntry both drop filterValueLabel', () => {
    for (const reset of ['resetForSheetChange', 'resetForLensEntry'] as const) {
      const handle = createGridViewStore();
      handle.applyFilter({
        filter: acmeGroupFilter,
        filterValueLabel: 'Acme Corp.',
      });
      handle[reset]();
      expect(handle.store.get().applied.filterValueLabel, reset).toBeNull();
    }
  });

  it('clearSort clears only applied.sort', () => {
    const { store, applySort, clearSort } = createGridViewStore();
    applySort({ column: 'name', direction: 'asc', sort: [{ column: 'name', dir: 'asc' }] });
    clearSort();
    expect(store.get().applied.sort).toBeNull();
    expect(store.get().draft.sortColumn).toBe('name'); // unchanged
  });

  it('applySavedViewGrid replaces sort draft and applied state, keys columnOrderBySheet by sheetId, and bumps columnGroupsVersion', () => {
    const { store, applySavedViewGrid } = createGridViewStore();
    applySavedViewGrid({
      viewId: 1,
      sheetId: 'sheet-1',
      filter: { status: { eq: 'open' } },
      sort: [{ column: 'name', dir: 'asc' }],
      draftSortColumn: 'name',
      draftSortDirection: 'asc',
      columns: ['name', 'status'],
      hiddenColumns: [],
      columnGroupSpecs: [],
    });
    let s = store.get();
    expect(s.applied.filter).toEqual({ status: { eq: 'open' } });
    expect(s.columnOrderBySheet).toEqual({ 'sheet-1': ['name', 'status'] });
    expect(s.columnGroupsVersion).toBe(1);

    // A second sheet's saved view keys in ALONGSIDE the first, not replacing it.
    applySavedViewGrid({
      viewId: 2,
      sheetId: 'sheet-2',
      filter: null,
      sort: null,
      draftSortColumn: '',
      draftSortDirection: 'asc',
      columns: ['id'],
      hiddenColumns: [],
      columnGroupSpecs: [],
    });
    s = store.get();
    expect(s.columnOrderBySheet).toEqual({ 'sheet-1': ['name', 'status'], 'sheet-2': ['id'] });
    expect(s.columnGroupsVersion).toBe(2);
  });

  it('keeps Active only while the saved definition is unchanged', () => {
    const {
      store,
      applySavedViewGrid,
      applyFilter,
      reconcileSavedViewHiddenColumns,
      setColumnGroupSpecs,
      setHiddenColumns,
    } =
      createGridViewStore();
    applySavedViewGrid({
      viewId: 7,
      sheetId: 'sheet-1',
      filter: { status: { eq: 'open' } },
      sort: null,
      draftSortColumn: '',
      draftSortDirection: 'asc',
      columns: ['name'],
      hiddenColumns: ['later'],
      columnGroupSpecs: [],
    });
    expect(store.get().activeSavedViewId).toBe(7);

    // Grid hydration can re-publish the same groups and must not erase the
    // active attribution; a real filter or visibility edit does.
    setColumnGroupSpecs([]);
    expect(store.get().activeSavedViewId).toBe(7);
    applyFilter({ filter: { status: { eq: 'open' } } });
    expect(store.get().activeSavedViewId).toBe(7);
    setHiddenColumns('sheet-1', ['later']);
    expect(store.get().activeSavedViewId).toBe(7);
    reconcileSavedViewHiddenColumns(7, 'sheet-1', ['later', 'new-column']);
    expect(store.get().hiddenColumnsBySheet['sheet-1']).toEqual(['later', 'new-column']);
    expect(store.get().activeSavedViewId).toBe(7);
    setHiddenColumns('sheet-1', []);
    expect(store.get().activeSavedViewId).toBeNull();
    reconcileSavedViewHiddenColumns(7, 'sheet-1', ['stale-column']);
    expect(store.get().hiddenColumnsBySheet['sheet-1']).toEqual([]);
  });

  it('resetForSheetChange resets the sort panel, applied state, and sort direction but not sort column', () => {
    const { store, applyFilter, applySort, setSortPanelOpen, resetForSheetChange } =
      createGridViewStore();
    applyFilter({ filter: { status: { between: { start: '1', end: '2' } } } });
    applySort({ column: 'name', direction: 'desc', sort: [{ column: 'name', dir: 'desc' }] });
    setSortPanelOpen(true);

    resetForSheetChange();

    const s = store.get();
    expect(s.sortPanelOpen).toBe(false);
    expect(s.applied).toEqual({ filter: null, sort: null, filterValueLabel: null });
    expect(s.draft.sortDirection).toBe('asc');
    expect(s.draft.sortColumn).toBe('name');
  });

  it('resetForLensEntry clears applied filter/sort and closes the sort panel, leaving draft untouched', () => {
    const { store, applyFilter, setSortPanelOpen, resetForLensEntry } =
      createGridViewStore();
    applyFilter({ filter: { status: { eq: 'open' } } });
    setSortPanelOpen(true);

    resetForLensEntry();

    const s = store.get();
    expect(s.applied).toEqual({ filter: null, sort: null, filterValueLabel: null });
    expect(s.sortPanelOpen).toBe(false);
    expect(s.draft.sortColumn).toBe(''); // draft untouched
  });

  it('setSortPanelOpen bails (reference-equal) on a no-op set', () => {
    const { store, setSortPanelOpen } = createGridViewStore();
    const before = store.get();
    setSortPanelOpen(false); // already false
    expect(store.get()).toBe(before); // createStore's Object.is bail — no new reference
    setSortPanelOpen(true);
    expect(store.get()).not.toBe(before);
  });

  it('toggleWrap bumps rowHeight to WRAP_ROW_HEIGHT when enabling wrap on a short row, and never shrinks it back on disable', () => {
    const { store, setRowHeight, toggleWrap } = createGridViewStore();
    setRowHeight(26); // 'Compact' — below WRAP_ROW_HEIGHT (68)
    toggleWrap();
    expect(store.get().wrapText).toBe(true);
    expect(store.get().rowHeight).toBe(68);

    toggleWrap(); // disable — row height is NOT restored to 26
    expect(store.get().wrapText).toBe(false);
    expect(store.get().rowHeight).toBe(68);
  });

  it('toggleWrap does not change rowHeight when it is already >= WRAP_ROW_HEIGHT', () => {
    const { store, setRowHeight, toggleWrap } = createGridViewStore();
    setRowHeight(96);
    toggleWrap();
    expect(store.get().rowHeight).toBe(96);
  });

  describe('inline filter row', () => {
    it('setInlineFilterColumn opens the row for the given sheet+column', () => {
      const { store, setInlineFilterColumn } = createGridViewStore();
      setInlineFilterColumn('s1', 'colA');
      expect(store.get().inlineFilter).toEqual({ sheetId: 's1', open: true, column: 'colA' });
    });

    it('closeInlineFilter closes the row but keeps the sheetId/column (the applied filter survives)', () => {
      const { store, setInlineFilterColumn, closeInlineFilter } = createGridViewStore();
      setInlineFilterColumn('s1', 'colA');
      closeInlineFilter();
      expect(store.get().inlineFilter).toEqual({ sheetId: 's1', open: false, column: 'colA' });
    });

    it('sheet-scoped derivation masks state retained across a sheet reset for another sheet', () => {
      const { store, setInlineFilterColumn, resetForSheetChange } = createGridViewStore();
      setInlineFilterColumn('s1', 'colA');
      resetForSheetChange();
      expect(selectInlineFilterOpen(store.get(), 's1')).toBe(true);
      expect(selectInlineFilterColumn(store.get(), 's1')).toBe('colA');
      expect(selectInlineFilterOpen(store.get(), 's2')).toBe(false);
      expect(selectInlineFilterColumn(store.get(), 's2')).toBe('');
    });
  });

  it('setColumnOrder/setFrozenColumnCount/setHiddenColumns key by sheetId without clobbering other sheets', () => {
    const { store, setColumnOrder, setFrozenColumnCount, setHiddenColumns } = createGridViewStore();
    setColumnOrder('sheet-1', ['a', 'b']);
    setFrozenColumnCount('sheet-1', 2);
    setHiddenColumns('sheet-1', ['b']);
    setColumnOrder('sheet-2', ['x']);

    const s = store.get();
    expect(s.columnOrderBySheet).toEqual({ 'sheet-1': ['a', 'b'], 'sheet-2': ['x'] });
    expect(s.frozenColumnCountBySheet).toEqual({ 'sheet-1': 2 });
    expect(s.hiddenColumnsBySheet).toEqual({ 'sheet-1': ['b'] });
  });

  it('setColumnGroupSpecs sets the specs WITHOUT bumping columnGroupsVersion (unlike applySavedViewGrid)', () => {
    const { store, setColumnGroupSpecs } = createGridViewStore();
    const specs = [{ run_id: 1, label: 'g', columns: ['a'], show_confidence: true, show_justification: true }];
    setColumnGroupSpecs(specs);
    expect(store.get().columnGroupSpecs).toEqual(specs);
    expect(store.get().columnGroupsVersion).toBe(0);
  });
});
