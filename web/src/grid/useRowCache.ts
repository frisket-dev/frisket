// Paged row cache feeding Glide's synchronous getCellContent. Pages load on
// demand from the visible region; refresh() re-pulls loaded pages near the
// viewport so live runs can animate cells in without flicker.
//
// The underlying store (pages/snapshot/listeners) lives in rowCacheStore.ts's
// shared, sheetId-keyed registry. The caller passes the registry in
// (SheetGrid.tsx threads down the shared per-project handle; falls back to a
// private one for a standalone/test render).

import { useCallback, useEffect, useMemo, useRef, useSyncExternalStore } from 'react';
import { type Row, type SheetDataOptions } from '../api/open';
import type { GridApiPort } from '../api/ports';
import { computeRowCacheKey, type RowCacheStoreHandle } from './rowCacheStore';

const PAGE_SIZE = 500;

export interface RowCache {
  getRow(rowIndex: number): Row | undefined;
  onVisibleRowsChanged(firstRow: number, lastRow: number): void;
  /** Re-fetch pages near the viewport in place (live-run animation). */
  refresh(): void;
  /** Bumps whenever cached data changes — use as a redraw dependency. */
  version: number;
  /** Server-reported total for the current filter/sort/page scope. */
  rowCount: number;
}

export function useRowCache(
  sheetId: string,
  rowCount: number,
  dataVersion: number,
  rowCacheStore: RowCacheStoreHandle,
  options: SheetDataOptions | null | undefined,
  gridApi: GridApiPort,
): RowCache {
  const queryOptions = useMemo(() => options ?? {}, [options]);
  const store = useMemo(() => rowCacheStore.getSlot(sheetId), [rowCacheStore, sheetId]);
  const visible = useRef<[number, number]>([0, 1]);

  // Staleness token: a response is dropped if the reset key changed while it
  // was in flight (sheet switch / undo / redo / review invalidation).
  const resetKey = computeRowCacheKey(sheetId, dataVersion, queryOptions);
  const loadedKey = useRef(resetKey);
  const snapshot = useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot);
  const totalRows = snapshot.key === resetKey && snapshot.totalRows !== null ? snapshot.totalRows : rowCount;

  const loadPage = useCallback(
    (page: number, force = false) => {
      const existing = store.getPage(page);
      if (!force && existing !== undefined) return;
      if (existing === undefined) store.setLoading(page);
      const key = loadedKey.current;
      void gridApi.getSheetData(sheetId, page * PAGE_SIZE, PAGE_SIZE, queryOptions).then((data) => {
        if (loadedKey.current !== key) return; // stale response, discard
        store.setPage(key, page, data.rows, data.total);
      });
    },
    [sheetId, queryOptions, store, gridApi],
  );

  // Hard reset on sheet switch / invalidation. Clearing the store here keeps
  // cache mutation out of render; loaded pages publish the redraw snapshot.
  useEffect(() => {
    loadedKey.current = resetKey;
    store.reset();
    let cancelled = false;
    queueMicrotask(() => {
      if (cancelled) return;
      loadPage(0, true);
      const [first, last] = visible.current;
      for (let p = Math.floor(first / PAGE_SIZE); p <= Math.floor(last / PAGE_SIZE); p++) {
        if (cancelled) return;
        loadPage(p, true);
      }
    });
    return () => {
      // In-flight requests are not aborted; loadedKey drops stale responses.
      cancelled = true;
    };
  }, [loadPage, resetKey, store]);

  const onVisibleRowsChanged = useCallback(
    (firstRow: number, lastRow: number) => {
      visible.current = [firstRow, lastRow];
      const firstPage = Math.floor(Math.max(0, firstRow - PAGE_SIZE / 2) / PAGE_SIZE);
      const lastPage = Math.floor(Math.min(totalRows - 1, lastRow + PAGE_SIZE / 2) / PAGE_SIZE);
      for (let p = firstPage; p <= lastPage; p++) loadPage(p);
      // Evict pages far from the viewport so 10k-row sheets stay light.
      for (const p of store.getPageNumbers()) {
        if (p < firstPage - 4 || p > lastPage + 4) store.deletePage(p);
      }
    },
    [loadPage, store, totalRows],
  );

  const refresh = useCallback(() => {
    const [first, last] = visible.current;
    const firstPage = Math.floor(first / PAGE_SIZE);
    const lastPage = Math.floor(last / PAGE_SIZE);
    for (let p = firstPage; p <= lastPage + 1; p++) {
      if (store.hasPage(p)) loadPage(p, true);
    }
  }, [loadPage, store]);

  const getRow = useCallback((rowIndex: number): Row | undefined => {
    const page = store.getPage(Math.floor(rowIndex / PAGE_SIZE));
    if (page === undefined || page === 'loading') return undefined;
    return page[rowIndex % PAGE_SIZE];
  }, [store]);

  return useMemo(
    () => ({ getRow, onVisibleRowsChanged, refresh, version: snapshot.version, rowCount: totalRows }),
    [getRow, onVisibleRowsChanged, refresh, snapshot.version, totalRows],
  );
}
