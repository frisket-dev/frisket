// This module holds the pure (framework-free) cache shape plus a REGISTRY keyed
// by sheetId so a slot SURVIVES a SheetGrid remount instead of restarting, and
// any other consumer (the toolbar, via useSyncExternalStore) can read the exact
// same slot directly. It lives in grid/ (not state/) because the shape is
// grid-cache domain logic, not workspace UI state — state/rowCacheStore.ts just
// re-exports this for createWorkspaceStores.ts's per-project wiring, mirroring
// how workspace/ already imports grid/typeRegistry's plain helpers.
// Framework-free: no React import, so grid/ stays reusable outside the workspace
// substrate and state/'s "React-free" boundary rule (eslint.config.js) holds
// transitively through the re-export.
//
// A createRowCacheStore() call is one PROJECT's row-cache registry — never a
// module-level singleton. Sheet ids repeat across projects (see
// SheetGrid.tsx's widthsKey comment), so a bare module-level Map would leak
// cache slots between projects that happen to reuse the same sheet id;
// createWorkspaceStores(projectId) assembles one registry per project,
// disposed on project switch, exactly like every other domain store.

import type { GridFilterSpec, GridSortSpec, Row, SheetDataOptions } from '../api/open';

export type CachedPage = Row[] | 'loading';

export interface RowCacheSnapshot {
  key: string;
  version: number;
  totalRows: number | null;
}

export interface RowCacheSlot {
  deletePage(page: number): void;
  getPage(page: number): CachedPage | undefined;
  getPageNumbers(): IterableIterator<number>;
  getSnapshot(): RowCacheSnapshot;
  hasPage(page: number): boolean;
  reset(): void;
  setLoading(page: number): void;
  setPage(key: string, page: number, rows: Row[], totalRows: number): void;
  subscribe(listener: () => void): () => void;
}

function createRowCacheSlot(): RowCacheSlot {
  let pages = new Map<number, CachedPage>();
  let snapshot: RowCacheSnapshot = { key: '', version: 0, totalRows: null };
  const listeners = new Set<() => void>();

  const emit = () => {
    for (const listener of listeners) listener();
  };

  return {
    deletePage(page: number) {
      pages.delete(page);
    },
    getPage(page: number) {
      return pages.get(page);
    },
    getPageNumbers() {
      return pages.keys();
    },
    getSnapshot() {
      return snapshot;
    },
    hasPage(page: number) {
      return pages.has(page);
    },
    reset() {
      pages = new Map();
    },
    setLoading(page: number) {
      pages.set(page, 'loading');
    },
    setPage(key: string, page: number, rows: Row[], totalRows: number) {
      pages.set(page, rows);
      snapshot = { key, version: snapshot.version + 1, totalRows };
      emit();
    },
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
}

/** One registry per project: `getSlot(sheetId)` lazily creates (and then
 *  reuses) the slot for that sheet, so a SheetGrid remount at the same
 *  sheetId picks the same cache back up instead of restarting. */
export function createRowCacheStore(): {
  getSlot(sheetId: string): RowCacheSlot;
} {
  const slots = new Map<string, RowCacheSlot>();
  return {
    getSlot(sheetId: string) {
      let slot = slots.get(sheetId);
      if (!slot) {
        slot = createRowCacheSlot();
        slots.set(sheetId, slot);
      }
      return slot;
    },
  };
}

export type RowCacheStoreHandle = ReturnType<typeof createRowCacheStore>;

/** The staleness token: a page response is dropped if this key changed while
 *  the request was in flight (sheet switch / undo / redo / review
 *  invalidation / filter / sort / lens change). Pulled out as a pure
 *  function so BOTH useRowCache (which owns
 *  the fetch/eviction side) and any passive external reader (the toolbar)
 *  can compute the SAME key from the SAME raw inputs and agree on when a
 *  cached `totalRows` is fresh — no duplicated staleness logic to drift. */
export function computeRowCacheKey(
  sheetId: string,
  dataVersion: number,
  options?: SheetDataOptions | null,
): string {
  const o = options ?? {};
  return [
    sheetId,
    dataVersion,
    o.parentRowId ?? '',
    JSON.stringify(o.filter ?? null),
    JSON.stringify(o.sort ?? null),
    JSON.stringify(o.rowIds ?? null),
  ].join(':');
}

/** Builds the SheetDataOptions a row-cache scope resolves to: lens-view rows
 *  win outright (the lens defines the row-set + order; filter/sort/parent
 *  scoping is ignored while a lens is active, matching SheetGrid's own
 *  dataOptions memo). Pulled out so the toolbar can build the identical
 *  options object from the same raw scope inputs it already threads down to
 *  SheetGrid as props, without hand-rolling the lens/filter branch twice. */
export function resolveRowCacheScope(input: {
  parentRowId?: string | null;
  filter?: GridFilterSpec | null;
  sort?: GridSortSpec | null;
  lensRowIds?: number[] | null;
}): SheetDataOptions {
  if (input.lensRowIds != null) return { rowIds: input.lensRowIds };
  return {
    parentRowId: input.parentRowId ?? null,
    filter: input.filter ?? null,
    sort: input.sort ?? null,
  };
}
