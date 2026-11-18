import { useCallback, useSyncExternalStore } from 'react';
import type { ColumnCitationsApiPort } from '../api/ports';
import type { CellCitationSummary } from '../api/types';
import { createCitationResourceCache } from './citationResourceCache';

interface ColumnCitationsSnapshot {
  status: 'pending' | 'ready' | 'error';
  /** row_id -> active links in rank order; rows without evidence are absent. */
  byRow: Record<string, CellCitationSummary[]>;
}

const EMPTY_READY: ColumnCitationsSnapshot = { status: 'ready', byRow: {} };
const PENDING: ColumnCitationsSnapshot = { status: 'pending', byRow: {} };
const ERROR: ColumnCitationsSnapshot = { status: 'error', byRow: {} };
const cache = createCitationResourceCache(PENDING, ERROR, 32);

/** One batch per scoped API/column/loaded rows, retained across consumer remounts. */
export function useColumnCitations(
  sheetId: string,
  columnId: string | null,
  rowIds: readonly string[],
  gridApi: ColumnCitationsApiPort,
): ColumnCitationsSnapshot {
  const key = columnId === null || rowIds.length === 0
    ? null
    : JSON.stringify([sheetId, columnId, rowIds]);
  const subscribe = useCallback((listener: () => void) => {
    if (key === null || columnId === null) return () => {};
    return cache.subscribe(gridApi, key, listener, async () => {
      const payload = await gridApi.getColumnEvidence(sheetId, columnId, { rowIds: [...rowIds] });
      const byRow: ColumnCitationsSnapshot['byRow'] = {};
      for (const row of payload.rows) byRow[String(row.row_id)] = row.links;
      return { status: 'ready', byRow };
    });
  }, [key, gridApi, sheetId, columnId, rowIds]);
  const getSnapshot = useCallback(
    () => key === null ? EMPTY_READY : cache.read(gridApi, key),
    [gridApi, key],
  );
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}
