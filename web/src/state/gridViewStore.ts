// Owns exactly the grid slice of WorkspaceUiState (workspace/workspaceState.ts):
// the applied filter/sort/selection, the Filter sidebar, per-sheet column
// layout maps, and the global wrap-text display pref. Everything else that transition also
// touches (selection, row/column drawers, child filter) stays on the legacy
// reducer and is composed at the bind layer (useWorkspaceModel.tsx), never
// imported here — state/ must not import sibling stores.
//
// createGridViewStore() is per-project, assembled by
// state/createWorkspaceStores.ts — never a module-level singleton.

import { createStore } from '../core/store/createStore';
import type { Store } from '../core/store/types';
import type {
  GridFilterSpec,
  GridSortSpec,
  ViewSelection,
  SheetMeta,
} from '../api/open';
import type { ColumnGroupViewSpec } from '../grid/SheetGrid';

interface GridViewAppliedState {
  scopeRowIds?: number[] | null;
  selection?: ViewSelection | null;
  filter: GridFilterSpec | null;
  sort: GridSortSpec | null;
  /** Bound when a criterion is applied; renames cannot silently retarget it. */
  filterColumnIds?: Record<string, number>;
  sortColumnIds?: Record<string, number>;
  /** The spelling the applied filter's VALUE is known by, when the payload
   *  itself cannot carry one. Exactly one filter kind needs this: an
   *  `entity_eq` fingerprint selector holds an internal comparison token
   *  nobody wrote, so the chip could otherwise only name the type — the same
   *  chip for every organization on the sheet. Written only by the Mentions
   *  panel's group click, which HAS the clicked group in hand; null everywhere
   *  else, including a saved-view restore (a saved spec remembers no
   *  spelling). Not a rendered chip: the chip text is derived from the filter
   *  plus this at render (workspace/gridColumnState.ts's gridFilterLabel), so
   *  the two can never disagree about the type or the column. */
  filterValueLabel: string | null;
}

export type FilterSidebarTab = 'filter' | 'views';

/** The sheet's Filter sidebar. `focusFacet` names the facet card opened from a
 *  chip or a column menu; `focusSearch` increments each time a caller asks for
 *  the search box to take focus; `searchScope` is the scope the search box
 *  applies to its next text ('all' or a column name). */
interface FilterSidebarState {
  open: boolean;
  tab: FilterSidebarTab;
  focusFacet: string | null;
  focusSearch: number;
  searchScope: string;
}

interface GridViewState {
  applied: GridViewAppliedState;
  sidebar: FilterSidebarState;
  columnOrderBySheet: Record<string, string[]>;
  frozenColumnCountBySheet: Record<string, number>;
  hiddenColumnsBySheet: Record<string, string[]>;
  columnGroupSpecs: ColumnGroupViewSpec[];
  columnGroupsVersion: number;
  wrapText: boolean;
  /** The Saved View definition currently represented by the grid.  This is
   * retained while edited so the UI can show Modified and explicitly save. */
  activeViewId: number | null;
  presentationEdited: boolean;
}

/** Wrap text is a per-browser preference; everything else starts empty. */
function createGridViewState(): GridViewState {
  return {
    applied: { filter: null, sort: null, filterValueLabel: null },
    sidebar: { open: false, tab: 'filter', focusFacet: null, focusSearch: 0, searchScope: 'all' },
    columnOrderBySheet: {},
    frozenColumnCountBySheet: {},
    hiddenColumnsBySheet: {},
    columnGroupSpecs: [],
    columnGroupsVersion: 0,
    wrapText: localStorage.getItem('frisket:wrap-text') === '1',
    activeViewId: null,
    presentationEdited: false,
  };
}

export interface GridViewFilterApplication {
  filter: GridFilterSpec;
  /** A source-owned column identity takes precedence over an earlier display-name binding. */
  filterColumnIds?: Record<string, number>;
  /** See GridViewAppliedState.filterValueLabel. Optional because exactly one
   *  of the five call sites that build this shape (useWorkspaceModel.tsx's
   *  applyGridEntityFilter, fed by the Mentions panel's group click) knows a
   *  spelling to carry; the rest omit it and the applied label is nulled. */
  filterValueLabel?: string | null;
}

export interface GridViewSortApplication {
  sort: GridSortSpec;
}

/** = the ViewGridState shape minus the fields
 *  that stayed on the legacy reducer (that action also clears selectedRows/
 *  rowDrawer/columnDrawer — composed at the bind layer, not here). */
export interface ViewApplication {
  scopeRowIds?: number[] | null;
  selection?: ViewSelection | null;
  viewId: number;
  sheetId: string;
  filter: GridFilterSpec | null;
  sort: GridSortSpec | null;
  columns: string[];
  hiddenColumns: string[];
  columnGroupSpecs: ColumnGroupViewSpec[];
}

export function createGridViewStore(currentSheet: () => SheetMeta | undefined = () => undefined): {
  store: Store<GridViewState>;
  applyFilter(spec: GridViewFilterApplication): void;
  applySort(spec: GridViewSortApplication): void;
  clearFilter(): void;
  clearSort(): void;
  applyViewGrid(grid: ViewApplication): void;
  clearActiveView(viewId?: number): void;
  setSelection(selection: ViewSelection | null): void;
  /** Resets the applied filter, sort and selection to their sheet-switch
   *  defaults. The non-grid fields a sheet switch resets (current sheet,
   *  selected rows, header menu) belong to other stores. */
  resetForSheetChange(): void;
  openSidebar(tab: FilterSidebarTab, focus?: { facet?: string; search?: boolean; scope?: string }): void;
  setSearchScope(scope: string): void;
  closeSidebar(): void;
  setSidebarTab(tab: FilterSidebarTab): void;
  toggleWrap(): void;
  setColumnOrder(sheetId: string, order: string[]): void;
  setFrozenColumnCount(sheetId: string, count: number): void;
  setHiddenColumns(sheetId: string, names: string[]): void;
  reconcileViewHiddenColumns(viewId: number, sheetId: string, names: string[]): void;
  setColumnGroupSpecs(specs: ColumnGroupViewSpec[]): void;
} {
  const store = createStore<GridViewState>(createGridViewState());
  const bindColumns = (names: string[], previous: Record<string, number> = {}) => {
    const columns = currentSheet()?.columns ?? [];
    return Object.fromEntries(names.flatMap((name) => {
      const id = previous[name] ?? columns.find((column) => column.name === name)?.id;
      return id === undefined ? [] : [[name, Number(id)]];
    }));
  };

  return {
    store,

    applyFilter(spec) {
      store.set((s) => ({
        ...s,
        applied: {
          ...s.applied,
          filter: spec.filter,
          filterColumnIds: bindColumns(Object.keys(spec.filter), spec.filterColumnIds ?? s.applied.filterColumnIds),
          filterValueLabel: spec.filterValueLabel ?? null,
        },
      }));
    },

    applySort(spec) {
      store.set((s) => ({
        ...s,
        applied: { ...s.applied, sort: spec.sort, sortColumnIds: bindColumns(spec.sort.map((item) => item.column), s.applied.sortColumnIds) },
      }));
    },

    clearFilter() {
      store.set((s) => ({
        ...s,
        applied: { ...s.applied, filter: null, filterColumnIds: {}, filterValueLabel: null },
      }));
    },

    clearSort() {
      store.set((s) => ({
        ...s,
        applied: { ...s.applied, sort: null, sortColumnIds: {} },
      }));
    },

    applyViewGrid(grid) {
      store.set((s) => ({
        ...s,
        // A view restores a filter SPEC and nothing else: the spelling
        // the user originally clicked was never persisted, so the chip falls
        // back to naming the type rather than carrying a stale one.
        applied: { filter: grid.filter, sort: grid.sort, filterColumnIds: bindColumns(Object.keys(grid.filter ?? {})), sortColumnIds: bindColumns((grid.sort ?? []).map((item) => item.column)), selection: grid.selection ?? null, filterValueLabel: null, ...(grid.scopeRowIds != null ? { scopeRowIds: grid.scopeRowIds } : {}) },
        columnOrderBySheet: { ...s.columnOrderBySheet, [grid.sheetId]: grid.columns },
        hiddenColumnsBySheet: { ...s.hiddenColumnsBySheet, [grid.sheetId]: grid.hiddenColumns },
        columnGroupSpecs: grid.columnGroupSpecs,
        columnGroupsVersion: s.columnGroupsVersion + 1,
        activeViewId: grid.viewId,
        presentationEdited: false,
      }));
    },

    setSelection(selection) {
      store.set((s) => ({ ...s, applied: { ...s.applied, selection } }));
    },

    clearActiveView(viewId) {
      store.set((s) => (
        s.activeViewId !== null && (viewId === undefined || s.activeViewId === viewId)
          ? { ...s, activeViewId: null }
          : s
      ));
    },

    resetForSheetChange() {
      store.set((s) => ({
        ...s,
        applied: { filter: null, sort: null, filterValueLabel: null },
        activeViewId: null,
        presentationEdited: false,
      }));
    },

    openSidebar(tab, focus = {}) {
      store.set((s) => ({
        ...s,
        sidebar: {
          open: true,
          tab,
          focusFacet: focus.facet ?? null,
          focusSearch: focus.search ? s.sidebar.focusSearch + 1 : s.sidebar.focusSearch,
          searchScope: focus.scope ?? s.sidebar.searchScope,
        },
      }));
    },
    setSearchScope(scope) {
      store.set((s) => (s.sidebar.searchScope === scope ? s : { ...s, sidebar: { ...s.sidebar, searchScope: scope } }));
    },
    closeSidebar() {
      store.set((s) => (s.sidebar.open ? { ...s, sidebar: { ...s.sidebar, open: false, focusFacet: null } } : s));
    },
    setSidebarTab(tab) {
      store.set((s) => (s.sidebar.tab === tab ? s : { ...s, sidebar: { ...s.sidebar, tab, focusFacet: null } }));
    },

    toggleWrap() {
      store.set((s) => ({ ...s, wrapText: !s.wrapText }));
    },

    setColumnOrder(sheetId, order) {
      store.set((s) => {
        const previous = s.columnOrderBySheet[sheetId] ?? [];
        if (JSON.stringify(previous) === JSON.stringify(order)) return s;
        return {
          ...s,
          columnOrderBySheet: { ...s.columnOrderBySheet, [sheetId]: order },
          presentationEdited: true,
        };
      });
    },
    setFrozenColumnCount(sheetId, count) {
      store.set((s) => ({
        ...s,
        frozenColumnCountBySheet: { ...s.frozenColumnCountBySheet, [sheetId]: count },
      }));
    },
    setHiddenColumns(sheetId, names) {
      store.set((s) => {
        // An absent entry means the sheet still reads its persisted list, so
        // an empty list is a change for it.
        const previous = s.hiddenColumnsBySheet[sheetId];
        if (previous && JSON.stringify(previous) === JSON.stringify(names)) return s;
        return {
          ...s,
          hiddenColumnsBySheet: { ...s.hiddenColumnsBySheet, [sheetId]: names },
          presentationEdited: true,
        };
      });
    },
    reconcileViewHiddenColumns(viewId, sheetId, names) {
      store.set((s) => {
        if (s.activeViewId !== viewId || s.presentationEdited) return s;
        const previous = s.hiddenColumnsBySheet[sheetId] ?? [];
        if (JSON.stringify(previous) === JSON.stringify(names)) return s;
        return {
          ...s,
          hiddenColumnsBySheet: { ...s.hiddenColumnsBySheet, [sheetId]: names },
        };
      });
    },
    setColumnGroupSpecs(specs) {
      store.set((s) => (
        JSON.stringify(s.columnGroupSpecs) === JSON.stringify(specs)
          ? s
          : { ...s, columnGroupSpecs: specs, presentationEdited: true }
      ));
    },
  };
}

export type GridViewStoreHandle = ReturnType<typeof createGridViewStore>;
