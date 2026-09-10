// Owns exactly the grid slice of WorkspaceUiState (workspace/workspaceState.ts):
// the filter/sort draft+applied split, per-sheet column layout maps, and the
// global row-height/wrap-text display prefs. Everything else that transition also
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
  GridSortDirection,
  GridSortSpec,
} from '../api/open';
import type { ColumnGroupViewSpec } from '../grid/SheetGrid';
import {
  ROW_HEIGHTS,
  WRAP_ROW_HEIGHT,
} from '../workspace/workspaceState';

export interface GridViewDraftState {
  sortColumn: string;
  sortDirection: GridSortDirection;
}

export interface GridViewAppliedState {
  filter: GridFilterSpec | null;
  sort: GridSortSpec | null;
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

export interface InlineFilterState {
  sheetId: string;
  open: boolean;
  column: string;
}

export interface GridViewState {
  draft: GridViewDraftState;
  applied: GridViewAppliedState;
  sortPanelOpen: boolean;
  columnOrderBySheet: Record<string, string[]>;
  frozenColumnCountBySheet: Record<string, number>;
  hiddenColumnsBySheet: Record<string, string[]>;
  columnGroupSpecs: ColumnGroupViewSpec[];
  columnGroupsVersion: number;
  rowHeight: number;
  wrapText: boolean;
  /** The Saved View definition currently represented by the grid.  This is
   * cleared only by a real definition edit, never by display-only changes. */
  activeSavedViewId: number | null;
  /** Survives sheet switches by design-of-record: no reset path clears it.
   *  The sheetId guard in selectInlineFilterOpen/selectInlineFilterColumn masks
   *  the retained state while another sheet is active. */
  inlineFilter: InlineFilterState;
}

export function selectInlineFilterOpen(state: GridViewState, sheetId: string | undefined): boolean {
  return state.inlineFilter.sheetId === sheetId && state.inlineFilter.open;
}

export function selectInlineFilterColumn(state: GridViewState, sheetId: string | undefined): string {
  return state.inlineFilter.sheetId === sheetId ? state.inlineFilter.column : '';
}

/** Matches createWorkspaceUiState()'s rowHeight/wrapText read exactly —
 *  same localStorage keys, same fallback. Every other field starts at the
 *  same default that reducer used. */
function createGridViewState(): GridViewState {
  const storedRowHeight = Number(localStorage.getItem('frisket:row-height'));
  return {
    draft: {
      sortColumn: '',
      sortDirection: 'asc',
    },
    applied: { filter: null, sort: null, filterValueLabel: null },
    sortPanelOpen: false,
    columnOrderBySheet: {},
    frozenColumnCountBySheet: {},
    hiddenColumnsBySheet: {},
    columnGroupSpecs: [],
    columnGroupsVersion: 0,
    rowHeight: ROW_HEIGHTS.some((h) => h.value === storedRowHeight) ? storedRowHeight : 34,
    wrapText: localStorage.getItem('frisket:wrap-text') === '1',
    activeSavedViewId: null,
    inlineFilter: { sheetId: '', open: false, column: '' },
  };
}

export interface GridViewFilterApplication {
  filter: GridFilterSpec;
  /** See GridViewAppliedState.filterValueLabel. Optional because exactly one
   *  of the five call sites that build this shape (useWorkspaceModel.tsx's
   *  applyGridEntityFilter, fed by the Mentions panel's group click) knows a
   *  spelling to carry; the rest omit it and the applied label is nulled. */
  filterValueLabel?: string | null;
}

export interface GridViewSortApplication {
  column: string;
  direction: GridSortDirection;
  sort: GridSortSpec;
}

/** = the SavedViewGridState shape minus the fields
 *  that stayed on the legacy reducer (that action also clears selectedRows/
 *  rowDrawer/columnDrawer — composed at the bind layer, not here). */
export interface GridViewSavedViewApplication {
  viewId: number;
  sheetId: string;
  filter: GridFilterSpec | null;
  sort: GridSortSpec | null;
  draftSortColumn: string;
  draftSortDirection: GridSortDirection;
  columns: string[];
  hiddenColumns: string[];
  columnGroupSpecs: ColumnGroupViewSpec[];
}

export function createGridViewStore(): {
  store: Store<GridViewState>;
  applyFilter(spec: GridViewFilterApplication): void;
  applySort(spec: GridViewSortApplication): void;
  clearFilter(): void;
  clearSort(): void;
  applySavedViewGrid(grid: GridViewSavedViewApplication): void;
  clearActiveSavedView(viewId?: number): void;
  /** = the grid-owned fields of the 'selectSheet' reducer case:
   *  sortPanelOpen/
   *  activeGridFilter/activeGridSort/
   *  draftSortDirection reset to their sheet-switch defaults. The non-grid
   *  fields that same case resets (currentSheetId, selectedRows, headerMenu)
   *  stay on the legacy reducer. */
  resetForSheetChange(): void;
  /** = the grid-owned fields of applyLens's entry patch: entering a lens
   *  view clears the column filter/sort and closes the sort panel. The
   *  non-grid field that same patch sets (headerMenu) stays legacy. */
  resetForLensEntry(): void;
  setSortPanelOpen(open: boolean): void;
  setDraftSortColumn(column: string): void;
  setDraftSortDirection(direction: GridSortDirection): void;
  setRowHeight(n: number): void;
  toggleWrap(): void;
  setInlineFilterColumn(sheetId: string, column: string): void;
  closeInlineFilter(): void;
  setColumnOrder(sheetId: string, order: string[]): void;
  setFrozenColumnCount(sheetId: string, count: number): void;
  setHiddenColumns(sheetId: string, names: string[]): void;
  reconcileSavedViewHiddenColumns(viewId: number, sheetId: string, names: string[]): void;
  setColumnGroupSpecs(specs: ColumnGroupViewSpec[]): void;
} {
  const store = createStore<GridViewState>(createGridViewState());

  return {
    store,

    applyFilter(spec) {
      store.set((s) => ({
        ...s,
        applied: {
          ...s.applied,
          filter: spec.filter,
          filterValueLabel: spec.filterValueLabel ?? null,
        },
        activeSavedViewId: JSON.stringify(s.applied.filter) === JSON.stringify(spec.filter)
          ? s.activeSavedViewId
          : null,
      }));
    },

    applySort(spec) {
      store.set((s) => ({
        ...s,
        draft: { ...s.draft, sortColumn: spec.column, sortDirection: spec.direction },
        applied: { ...s.applied, sort: spec.sort },
        activeSavedViewId: JSON.stringify(s.applied.sort) === JSON.stringify(spec.sort)
          ? s.activeSavedViewId
          : null,
      }));
    },

    clearFilter() {
      store.set((s) => ({
        ...s,
        applied: { ...s.applied, filter: null, filterValueLabel: null },
        activeSavedViewId: s.applied.filter === null ? s.activeSavedViewId : null,
      }));
    },

    clearSort() {
      store.set((s) => ({
        ...s,
        applied: { ...s.applied, sort: null },
        activeSavedViewId: s.applied.sort === null ? s.activeSavedViewId : null,
      }));
    },

    applySavedViewGrid(grid) {
      store.set((s) => ({
        ...s,
        draft: {
          sortColumn: grid.draftSortColumn,
          sortDirection: grid.draftSortDirection,
        },
        // A saved view restores a filter SPEC and nothing else: the spelling
        // the user originally clicked was never persisted, so the chip falls
        // back to naming the type rather than carrying a stale one.
        applied: { filter: grid.filter, sort: grid.sort, filterValueLabel: null },
        columnOrderBySheet: { ...s.columnOrderBySheet, [grid.sheetId]: grid.columns },
        hiddenColumnsBySheet: { ...s.hiddenColumnsBySheet, [grid.sheetId]: grid.hiddenColumns },
        columnGroupSpecs: grid.columnGroupSpecs,
        columnGroupsVersion: s.columnGroupsVersion + 1,
        activeSavedViewId: grid.viewId,
      }));
    },

    clearActiveSavedView(viewId) {
      store.set((s) => (
        s.activeSavedViewId !== null && (viewId === undefined || s.activeSavedViewId === viewId)
          ? { ...s, activeSavedViewId: null }
          : s
      ));
    },

    resetForSheetChange() {
      store.set((s) => ({
        ...s,
        sortPanelOpen: false,
        applied: { filter: null, sort: null, filterValueLabel: null },
        draft: { ...s.draft, sortDirection: 'asc' },
        activeSavedViewId: null,
      }));
    },

    resetForLensEntry() {
      store.set((s) => ({
        ...s,
        applied: { filter: null, sort: null, filterValueLabel: null },
        sortPanelOpen: false,
        activeSavedViewId: null,
      }));
    },

    setSortPanelOpen(open) {
      store.set((s) => (s.sortPanelOpen === open ? s : { ...s, sortPanelOpen: open }));
    },
    setDraftSortColumn(column) {
      store.set((s) => ({ ...s, draft: { ...s.draft, sortColumn: column } }));
    },
    setDraftSortDirection(direction) {
      store.set((s) => ({ ...s, draft: { ...s.draft, sortDirection: direction } }));
    },

    setRowHeight(n) {
      store.set((s) => ({ ...s, rowHeight: n }));
    },
    toggleWrap() {
      store.set((s) => {
        const nextWrapText = !s.wrapText;
        const nextRowHeight =
          nextWrapText && s.rowHeight < WRAP_ROW_HEIGHT ? WRAP_ROW_HEIGHT : s.rowHeight;
        return { ...s, wrapText: nextWrapText, rowHeight: nextRowHeight };
      });
    },

    setInlineFilterColumn(sheetId, column) {
      store.set((s) => ({ ...s, inlineFilter: { sheetId, open: true, column } }));
    },
    closeInlineFilter() {
      store.set((s) => ({ ...s, inlineFilter: { ...s.inlineFilter, open: false } }));
    },

    setColumnOrder(sheetId, order) {
      store.set((s) => {
        const previous = s.columnOrderBySheet[sheetId] ?? [];
        if (JSON.stringify(previous) === JSON.stringify(order)) return s;
        return {
          ...s,
          columnOrderBySheet: { ...s.columnOrderBySheet, [sheetId]: order },
          activeSavedViewId: null,
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
        const previous = s.hiddenColumnsBySheet[sheetId] ?? [];
        if (JSON.stringify(previous) === JSON.stringify(names)) return s;
        return {
          ...s,
          hiddenColumnsBySheet: { ...s.hiddenColumnsBySheet, [sheetId]: names },
          activeSavedViewId: null,
        };
      });
    },
    reconcileSavedViewHiddenColumns(viewId, sheetId, names) {
      store.set((s) => {
        if (s.activeSavedViewId !== viewId) return s;
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
          : { ...s, columnGroupSpecs: specs, activeSavedViewId: null }
      ));
    },
  };
}

export type GridViewStoreHandle = ReturnType<typeof createGridViewStore>;
