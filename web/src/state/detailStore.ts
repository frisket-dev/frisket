// Owns rowDrawer/rowDrawerPreview/columnDrawer/columnSettingsId/headerMenu
// /childFilter/proposalInspect. Does NOT own route panel
// identity (routeStore) or selectedColumnId (selectionStore) — several call sites
// write selectedColumnId ALONGSIDE these fields in one transition; the bind layer
// composes both stores' calls together — state/ must not import a sibling store.
//
// createDetailStore() is per-project, assembled by state/createWorkspaceStores.ts
// — never a module-level singleton.

import { createStore } from '../core/store/createStore';
import type { Store } from '../core/store/types';
import type { ColumnDef, PreviewCellDetail, Row } from '../api/open';
import type {
  ChildFilter,
  HeaderMenuState,
  ProposalInspectState,
} from '../workspace/workspaceState';

export interface DetailState {
  rowDrawer: Row | null;
  /** The clicked in-memory sample; null means ordinary committed-row details. */
  rowDrawerPreview: PreviewCellDetail | null;
  columnDrawer: ColumnDef | null;
  columnSettingsId: string;
  childFilter: ChildFilter | null;
  headerMenu: HeaderMenuState | null;
  proposalInspect: ProposalInspectState | null;
}

export function createDetailState(): DetailState {
  return {
    rowDrawer: null,
    rowDrawerPreview: null,
    columnDrawer: null,
    columnSettingsId: '',
    childFilter: null,
    headerMenu: null,
    proposalInspect: null,
  };
}

export function createDetailStore(): {
  store: Store<DetailState>;
  closeAll(): void;
  syncColumn(column: ColumnDef | null): void;
  syncRow(): void;
  loadRow(row: Row | null): void;
  openRow(row: Row, preview?: PreviewCellDetail | null): void;
  openColumn(column: ColumnDef): void;
  closeDrawers(opts?: { clearChildFilter?: boolean; clearColumnSettings?: boolean }): void;
  /** Clears rowDrawer ONLY, leaving columnDrawer/childFilter/headerMenu
   *  untouched — a narrower clear than closeDrawers(), matching two
   *  call sites that need only rowDrawer cleared
   *  (afterHistoryChange's post-undo/redo invalidation; the evidence-viewer
   *  "open companion" affordance in App.tsx). */
  clearRowDrawer(): void;
  setChildFilter(childFilter: ChildFilter | null): void;
  mergeColumnUpdate(column: ColumnDef): void;
  clearForGridTransition(): void;
  clearForSavedView(): void;
  openHeaderMenu(headerMenu: HeaderMenuState): void;
  closeHeaderMenu(): void;
  setProposalInspect(proposal: ProposalInspectState | null): void;
} {
  const store = createStore<DetailState>(createDetailState());

  return {
    store,

    /** selectedColumnId:null is
     *  selectionStore's — composed at the bind-layer call site. */
    closeAll() {
      store.set((s) => ({
        ...s,
        rowDrawer: null,
        rowDrawerPreview: null,
        columnDrawer: null,
        columnSettingsId: '',
      }));
    },

    syncColumn(column) {
      store.set((s) => ({
        ...s,
        rowDrawer: null,
        rowDrawerPreview: null,
        columnDrawer: column,
        columnSettingsId: column?.id ?? '',
      }));
    },

    syncRow() {
      store.set((s) => ({ ...s, columnDrawer: null, columnSettingsId: '' }));
    },

    loadRow(row) {
      store.set((s) => ({ ...s, rowDrawer: row, rowDrawerPreview: null }));
    },

    openRow(row, preview) {
      store.set((s) => ({
        ...s,
        columnDrawer: null,
        columnSettingsId: '',
        rowDrawer: row,
        rowDrawerPreview: preview ?? null,
      }));
    },

    openColumn(column) {
      store.set((s) => ({ ...s, rowDrawer: null, rowDrawerPreview: null, columnSettingsId: column.id, columnDrawer: column }));
    },

    /** clearSelectedColumn is
     *  selectionStore's — composed at the bind-layer call site. */
    closeDrawers(opts) {
      store.set((s) => ({
        ...s,
        rowDrawer: null,
        rowDrawerPreview: null,
        columnDrawer: null,
        ...(opts?.clearColumnSettings ? { columnSettingsId: '' } : {}),
        ...(opts?.clearChildFilter ? { childFilter: null } : {}),
      }));
    },

    clearRowDrawer() {
      store.set((s) => (s.rowDrawer === null && s.rowDrawerPreview === null ? s : { ...s, rowDrawer: null, rowDrawerPreview: null }));
    },

    setChildFilter(childFilter) {
      store.set((s) => ({ ...s, childFilter }));
    },

    mergeColumnUpdate(column) {
      store.set((s) => {
        if (!s.columnDrawer) return { ...s, columnDrawer: column };
        if (s.columnDrawer.id !== column.id) return s;
        return { ...s, columnDrawer: { ...s.columnDrawer, ...column, ai: s.columnDrawer.ai } };
      });
    },

    /** selectedRows clear
     *  is selectionStore's — composed at the bind-layer call site. */
    clearForGridTransition() {
      store.set((s) => ({ ...s, childFilter: null, rowDrawer: null, rowDrawerPreview: null }));
    },

    /** Does NOT clear childFilter
     *  (an intentional asymmetry vs. applyGridFilter/Sort,
     *  pinned by state/gridViewStore.test.ts's cross-domain transaction
     *  parity suite). */
    clearForSavedView() {
      store.set((s) => ({ ...s, rowDrawer: null, rowDrawerPreview: null, columnDrawer: null }));
    },

    openHeaderMenu(headerMenu) {
      store.set((s) => ({
        ...s,
        headerMenu,
        rowDrawer: null,
        rowDrawerPreview: null,
      }));
    },

    /** Also the detail-owned half of the former
     *  'selectSheet' and applyLens's entry patch (both just closed the
     *  header menu; the bind layer calls this directly for those too). */
    closeHeaderMenu() {
      store.set((s) => (s.headerMenu === null ? s : { ...s, headerMenu: null }));
    },

    setProposalInspect(proposal) {
      store.set((s) => (s.proposalInspect === proposal ? s : { ...s, proposalInspect: proposal }));
    },
  };
}

export type DetailStoreHandle = ReturnType<typeof createDetailStore>;
