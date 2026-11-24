// Unit tests of gridViewStore's own actions in isolation — no cross-store
// composition here. Cross-domain transaction-parity tests live in
// state/workspaceTransitions.test.ts, not this file.

import { describe, expect, it } from 'vitest';
import { createGridViewStore } from './gridViewStore';
import type { GridFilterSpec, GridSortSpec } from '../api/open';

describe('createGridViewStore — unit', () => {
  it('starts with the same defaults createWorkspaceUiState used for the grid slice', () => {
    const { store } = createGridViewStore();
    const s = store.get();
    expect(s.applied).toEqual({ filter: null, sort: null, filterValueLabel: null });
    expect(s.sidebar).toEqual({ open: false, tab: 'filter', focusFacet: null, focusSearch: 0, searchScope: 'all' });
    expect(s.columnOrderBySheet).toEqual({});
    expect(s.frozenColumnCountBySheet).toEqual({});
    expect(s.hiddenColumnsBySheet).toEqual({});
    expect(s.columnGroupSpecs).toEqual([]);
    expect(s.columnGroupsVersion).toBe(0);
    expect(s.wrapText).toBe(false);
    expect(s.activeViewId).toBeNull();
  });

  it('hydrates the persisted wrap preference', () => {
    try {
      localStorage.setItem('frisket:wrap-text', '1');
      expect(createGridViewStore().store.get().wrapText).toBe(true);
    } finally {
      localStorage.removeItem('frisket:wrap-text');
    }
  });

  it('applyFilter updates applied.filter, leaving sort untouched', () => {
    const { store, applySort, applyFilter } = createGridViewStore();
    applySort({ sort: [{ column: 'name', dir: 'desc' }] });
    const filter: GridFilterSpec = { status: { eq: 'open' } };
    applyFilter({ filter });

    const s = store.get();
    expect(s.applied.filter).toEqual(filter);
    // Sort from the earlier applySort call is untouched.
    expect(s.applied.sort).toEqual([{ column: 'name', dir: 'desc' }]);
  });

  it('applySort updates applied.sort, leaving filter untouched', () => {
    const { store, applyFilter, applySort } = createGridViewStore();
    const filter: GridFilterSpec = { status: { eq: 'open' } };
    applyFilter({ filter });
    const sort: GridSortSpec = [{ column: 'name', dir: 'asc' }];
    applySort({ sort });

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

  it('applyViewGrid restores a filter spec with NO label — a view remembers no spelling', () => {
    const { store, applyFilter, applyViewGrid } = createGridViewStore();
    applyFilter({
      filter: acmeGroupFilter,
      filterValueLabel: 'Acme Corp.',
    });
    applyViewGrid({
      viewId: 1,
      sheetId: 'sheet-1',
      filter: acmeGroupFilter,
      sort: null,
      columns: ['entities'],
      hiddenColumns: [],
      columnGroupSpecs: [],
    });
    // The SAME filter is applied again — but the label would now be a spelling
    // this restore never learned, so it is null and the chip names the type.
    expect(store.get().applied.filter).toEqual(acmeGroupFilter);
    expect(store.get().applied.filterValueLabel).toBeNull();
  });

  it('resetForSheetChange drops filterValueLabel', () => {
    for (const reset of ['resetForSheetChange'] as const) {
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
    applySort({ sort: [{ column: 'name', dir: 'asc' }] });
    clearSort();
    expect(store.get().applied.sort).toBeNull();
  });

  it('applyViewGrid replaces applied state, keys columnOrderBySheet by sheetId, and bumps columnGroupsVersion', () => {
    const { store, applyViewGrid } = createGridViewStore();
    applyViewGrid({
      viewId: 1,
      sheetId: 'sheet-1',
      filter: { status: { eq: 'open' } },
      sort: [{ column: 'name', dir: 'asc' }],
      columns: ['name', 'status'],
      hiddenColumns: [],
      columnGroupSpecs: [],
    });
    let s = store.get();
    expect(s.applied.filter).toEqual({ status: { eq: 'open' } });
    expect(s.columnOrderBySheet).toEqual({ 'sheet-1': ['name', 'status'] });
    expect(s.columnGroupsVersion).toBe(1);

    // A second sheet's view keys in ALONGSIDE the first, not replacing it.
    applyViewGrid({
      viewId: 2,
      sheetId: 'sheet-2',
      filter: null,
      sort: null,
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
      applyViewGrid,
      applyFilter,
      reconcileViewHiddenColumns,
      setColumnGroupSpecs,
      setHiddenColumns,
    } =
      createGridViewStore();
    applyViewGrid({
      viewId: 7,
      sheetId: 'sheet-1',
      filter: { status: { eq: 'open' } },
      sort: null,
      columns: ['name'],
      hiddenColumns: ['later'],
      columnGroupSpecs: [],
    });
    expect(store.get().activeViewId).toBe(7);

    // Grid hydration can re-publish the same groups and must not erase the
    // active attribution; local changes remain attached as Modified.
    setColumnGroupSpecs([]);
    expect(store.get().activeViewId).toBe(7);
    applyFilter({ filter: { status: { eq: 'open' } } });
    expect(store.get().activeViewId).toBe(7);
    setHiddenColumns('sheet-1', ['later']);
    expect(store.get().activeViewId).toBe(7);
    reconcileViewHiddenColumns(7, 'sheet-1', ['later', 'new-column']);
    expect(store.get().hiddenColumnsBySheet['sheet-1']).toEqual(['later', 'new-column']);
    expect(store.get().activeViewId).toBe(7);
    setHiddenColumns('sheet-1', []);
    expect(store.get().activeViewId).toBe(7);
    expect(store.get().presentationEdited).toBe(true);
    reconcileViewHiddenColumns(7, 'sheet-1', ['stale-column']);
    expect(store.get().hiddenColumnsBySheet['sheet-1']).toEqual([]);
  });

  it('resetForSheetChange clears the applied state but keeps the sidebar open', () => {
    const { store, applyFilter, applySort, openSidebar, resetForSheetChange } =
      createGridViewStore();
    applyFilter({ filter: { status: { between: { start: '1', end: '2' } } } });
    applySort({ sort: [{ column: 'name', dir: 'desc' }] });
    openSidebar('filter');

    resetForSheetChange();

    const s = store.get();
    expect(s.applied).toEqual({ filter: null, sort: null, filterValueLabel: null });
    expect(s.sidebar.open).toBe(true);
  });

  it('toggleWrap flips the wrap preference', () => {
    const { store, toggleWrap } = createGridViewStore();
    toggleWrap();
    expect(store.get().wrapText).toBe(true);
    toggleWrap();
    expect(store.get().wrapText).toBe(false);
  });

  describe('filter sidebar', () => {
    it('openSidebar focuses a facet, requests search focus and carries a scope', () => {
      const { store, openSidebar } = createGridViewStore();
      openSidebar('filter', { facet: 'filename' });
      expect(store.get().sidebar).toMatchObject({ open: true, tab: 'filter', focusFacet: 'filename', focusSearch: 0 });
      openSidebar('filter', { search: true, scope: 'ocr_text' });
      expect(store.get().sidebar).toMatchObject({ focusFacet: null, focusSearch: 1, searchScope: 'ocr_text' });
    });

    it('setSidebarTab clears the focused facet and bails on a no-op', () => {
      const { store, openSidebar, setSidebarTab } = createGridViewStore();
      openSidebar('filter', { facet: 'size' });
      setSidebarTab('views');
      expect(store.get().sidebar).toMatchObject({ tab: 'views', focusFacet: null });
      const before = store.get();
      setSidebarTab('views');
      expect(store.get()).toBe(before);
    });

    it('closeSidebar closes and bails when already closed', () => {
      const { store, openSidebar, closeSidebar } = createGridViewStore();
      const initial = store.get();
      closeSidebar();
      expect(store.get()).toBe(initial);
      openSidebar('views');
      closeSidebar();
      expect(store.get().sidebar).toMatchObject({ open: false, tab: 'views' });
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

  it('setHiddenColumns records an empty list for a sheet that has no entry yet', () => {
    const { store, setHiddenColumns } = createGridViewStore();
    setHiddenColumns('sheet-1', []);
    expect(store.get().hiddenColumnsBySheet).toEqual({ 'sheet-1': [] });
  });

  it('setColumnGroupSpecs sets the specs WITHOUT bumping columnGroupsVersion (unlike applyViewGrid)', () => {
    const { store, setColumnGroupSpecs } = createGridViewStore();
    const specs = [{ run_id: 1, label: 'g', columns: ['a'], show_confidence: true, show_justification: true }];
    setColumnGroupSpecs(specs);
    expect(store.get().columnGroupSpecs).toEqual(specs);
    expect(store.get().columnGroupsVersion).toBe(0);
  });
});
