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
  const optionsRef = useRef(queryOptions);
  const anchorPending = useRef<{ key: string; epoch: number } | null>(null);

  useEffect(() => {
    optionsRef.current = queryOptions;
  }, [queryOptions]);

  // Staleness token: a response is dropped if the reset key changed while it
  // was in flight (sheet switch / undo / redo / review invalidation).
  const resetKey = computeRowCacheKey(sheetId, dataVersion, queryOptions);
  const snapshot = useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot);
  const totalRows = snapshot.key === resetKey && snapshot.totalRows !== null ? snapshot.totalRows : rowCount;

  const fetchPage = useCallback(
    async (page: number, epoch: number, includeMetadata: boolean, force = false) => {
      const existing = store.getPage(page);
      if (!force && existing !== undefined) return true;
      if (!store.beginRequest(resetKey, epoch, page)) return false;
      if (existing === undefined) store.setLoading(page);
      try {
        if (includeMetadata) {
          const data = await gridApi.getSheetData(
            sheetId,
            page * PAGE_SIZE,
            PAGE_SIZE,
            optionsRef.current,
          );
          return store.setPage(resetKey, epoch, page, data.rows, data.total);
        }
        const data = gridApi.getSheetRows
          ? await gridApi.getSheetRows(
            sheetId,
            page * PAGE_SIZE,
            PAGE_SIZE,
            optionsRef.current,
          )
          : await gridApi.getSheetData(
            sheetId,
            page * PAGE_SIZE,
            PAGE_SIZE,
            optionsRef.current,
          );
        return store.setPage(resetKey, epoch, page, data.rows);
      } catch {
        store.failPage(resetKey, epoch, page);
        return false;
      } finally {
        store.finishRequest(resetKey, epoch, page);
      }
    },
    [sheetId, resetKey, store, gridApi],
  );

  const startEpoch = useCallback((anchorPage: number, refreshPages?: number[]) => {
    if (anchorPending.current?.key === resetKey) return;
    const epoch = store.beginEpoch(resetKey);
    if (epoch < 0) return;
    anchorPending.current = { key: resetKey, epoch };
    void fetchPage(anchorPage, epoch, true, true).then((accepted) => {
      if (!accepted || !store.isMetadataReady(resetKey, epoch)) return;
      const pages = refreshPages ?? (() => {
        const [first, last] = visible.current;
        const firstPage = Math.floor(Math.max(0, first - PAGE_SIZE / 2) / PAGE_SIZE);
        const exactTotal = store.getSnapshot().totalRows ?? rowCount;
        const lastPage = Math.floor(
          Math.min(exactTotal - 1, last + PAGE_SIZE / 2) / PAGE_SIZE,
        );
        return Array.from(
          { length: Math.max(0, lastPage - firstPage + 1) },
          (_unused, index) => firstPage + index,
        );
      })();
      for (const page of pages) {
        if (page !== anchorPage) void fetchPage(page, epoch, false, refreshPages !== undefined);
      }
    }).finally(() => {
      if (
        anchorPending.current?.key === resetKey
        && anchorPending.current.epoch === epoch
      ) {
        anchorPending.current = null;
      }
    });
  }, [fetchPage, resetKey, rowCount, store]);

  // A new key clears the previous key's exact total before its anchor starts.
  // Old responses retain their original key/epoch and cannot publish here.
  useEffect(() => {
    store.activate(resetKey);
    let cancelled = false;
    queueMicrotask(() => {
      if (cancelled) return;
      startEpoch(0);
    });
    return () => {
      cancelled = true;
    };
  }, [resetKey, startEpoch, store]);

  const onVisibleRowsChanged = useCallback(
    (firstRow: number, lastRow: number) => {
      visible.current = [firstRow, lastRow];
      const firstPage = Math.floor(Math.max(0, firstRow - PAGE_SIZE / 2) / PAGE_SIZE);
      const lastPage = Math.floor(Math.min(totalRows - 1, lastRow + PAGE_SIZE / 2) / PAGE_SIZE);
      const epoch = store.getEpoch();
      if (!store.isMetadataReady(resetKey, epoch)) {
        startEpoch(firstPage);
        return;
      }
      for (let p = firstPage; p <= lastPage; p++) {
        void fetchPage(p, epoch, false);
      }
      // Evict pages far from the viewport so 10k-row sheets stay light.
      for (const p of store.getPageNumbers()) {
        if (p < firstPage - 4 || p > lastPage + 4) store.deletePage(p);
      }
    },
    [fetchPage, resetKey, startEpoch, store, totalRows],
  );

  const refresh = useCallback(() => {
    const [first, last] = visible.current;
    const firstPage = Math.floor(first / PAGE_SIZE);
    const lastPage = Math.floor(last / PAGE_SIZE);
    const pages: number[] = [];
    for (let p = firstPage; p <= lastPage + 1; p++) {
      if (store.hasPage(p)) pages.push(p);
    }
    const anchorPage = pages[0] ?? firstPage;
    startEpoch(anchorPage, pages.length > 0 ? pages : [anchorPage]);
  }, [startEpoch, store]);

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
