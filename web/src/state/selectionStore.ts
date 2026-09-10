// Owns exactly `selectedRows`/`selectedColumnId`. Does NOT own detail-drawer
// contents (rowDrawer/columnDrawer/headerMenu — detailStore's) even though
// several call sites write selectedColumnId ALONGSIDE those detail fields in the
// same transition; the bind layer (useWorkspaceModel.tsx) composes both stores'
// calls together — state/ must not import a sibling store.
//
// createSelectionStore() is per-project, assembled by
// state/createWorkspaceStores.ts — never a module-level singleton.

import { createStore } from '../core/store/createStore';
import type { Store } from '../core/store/types';
import { emptySelectedRows, type SelectedGridRows } from '../workspace/workspaceState';

export interface SelectionState {
  selectedRows: SelectedGridRows;
  selectedColumnId: string | null;
}

export function createSelectionState(): SelectionState {
  return {
    selectedRows: emptySelectedRows(),
    selectedColumnId: null,
  };
}

export function createSelectionStore(): {
  store: Store<SelectionState>;
  setSelectedRows(rows: SelectedGridRows): void;
  setSelectedColumnId(id: string | null): void;
  /** = every transition that resets selectedRows to
   *  emptySelectedRows(sheetId): applyGridFilter/applyGridSort/
   *  applySavedViewGrid/clearGridFilter/clearGridSort/selectSheet/
   *  confirmDeleteRows.
   *  The caller decides WHETHER to call this (e.g. clearGridFilter/
   *  clearGridSort only call it when a sheetId is known). */
  clearRowSelection(sheetId: string): void;
} {
  const store = createStore<SelectionState>(createSelectionState());

  return {
    store,

    setSelectedRows(rows) {
      store.set((s) => ({ ...s, selectedRows: rows }));
    },

    setSelectedColumnId(id) {
      store.set((s) => (s.selectedColumnId === id ? s : { ...s, selectedColumnId: id }));
    },

    clearRowSelection(sheetId) {
      store.set((s) => ({ ...s, selectedRows: emptySelectedRows(sheetId) }));
    },
  };
}

export type SelectionStoreHandle = ReturnType<typeof createSelectionStore>;
