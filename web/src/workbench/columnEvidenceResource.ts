import { useCallback, useSyncExternalStore } from 'react';
import type { ColumnEvidenceApiPort } from '../api/ports';
import type { CellEvidenceLinkSummary } from '../api/types';
import { createEvidenceResourceCache } from './evidenceResourceCache';

export interface ColumnEvidenceSnapshot {
  status: 'pending' | 'ready' | 'error';
  /** row_id -> active links in rank order; rows without evidence are absent. */
  byRow: Record<string, CellEvidenceLinkSummary[]>;
}

const EMPTY_READY: ColumnEvidenceSnapshot = { status: 'ready', byRow: {} };
const PENDING: ColumnEvidenceSnapshot = { status: 'pending', byRow: {} };
const ERROR: ColumnEvidenceSnapshot = { status: 'error', byRow: {} };
const cache = createEvidenceResourceCache(PENDING, ERROR, 32);

/** One batch per scoped API/column/loaded rows, retained across consumer remounts. */
export function useColumnEvidence(
  sheetId: string,
  columnId: string | null,
  rowIds: readonly string[],
  gridApi: ColumnEvidenceApiPort,
): ColumnEvidenceSnapshot {
  const key = columnId === null || rowIds.length === 0
    ? null
    : JSON.stringify([sheetId, columnId, rowIds]);
  const subscribe = useCallback((listener: () => void) => {
    if (key === null || columnId === null) return () => {};
    return cache.subscribe(gridApi, key, listener, async () => {
      const payload = await gridApi.getColumnEvidence(sheetId, columnId, { rowIds: [...rowIds] });
      const byRow: ColumnEvidenceSnapshot['byRow'] = {};
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
