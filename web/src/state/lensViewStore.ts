// Owns the in-memory saved-lens grid-view slice. createLensViewStore() is
// per-project, assembled by state/createWorkspaceStores.ts — never a
// module-level singleton.

import { createStore } from '../core/store/createStore';
import type { Store } from '../core/store/types';

export interface LensGridView {
  lensId: number;
  name: string;
  sheetId: string;
  rowIds: number[];
  scores: Record<string, { distance: number | null; score: number | null }>;
  /** The resolver's full ranked hit count, so the banner can show "N of M" when the
   *  window (rowIds.length) is smaller than the total available. */
  total: number;
}

export interface LensViewState {
  /** Active saved-lens grid view. null = normal sheet view. */
  lensView: LensGridView | null;
  /** A refresh-needed / stale-index message from the LAST lens-open attempt. */
  lensOpenError: string | null;
}

export function createLensViewState(): LensViewState {
  return { lensView: null, lensOpenError: null };
}

export function createLensViewStore(): {
  store: Store<LensViewState>;
  beginLensResolve(): number;
  isCurrentLensResolve(generation: number): boolean;
  setLensView(lens: LensGridView | null): void;
  setLensOpenError(message: string | null): void;
  resetForSheetChange(): void;
} {
  const store = createStore<LensViewState>(createLensViewState());
  let lensResolveGeneration = 0;

  return {
    store,

    beginLensResolve() {
      lensResolveGeneration += 1;
      return lensResolveGeneration;
    },
    isCurrentLensResolve(generation) {
      return generation === lensResolveGeneration;
    },
    setLensView(lens) {
      store.set((s) => ({ ...s, lensView: lens }));
    },
    setLensOpenError(message) {
      store.set((s) => (s.lensOpenError === message ? s : { ...s, lensOpenError: message }));
    },
    resetForSheetChange() {
      lensResolveGeneration += 1;
      store.set((s) => ({ ...s, lensView: null, lensOpenError: null }));
    },
  };
}

export type LensViewStoreHandle = ReturnType<typeof createLensViewStore>;
