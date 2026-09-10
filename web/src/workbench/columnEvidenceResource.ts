// Column-evidence as a KEYED ASYNC RESOURCE. The Grounded Answers middle pane
// needs "the active column's evidence for the currently loaded rows" — derived
// server state keyed by an async request. Modeling that as `useState` + an
// effect that calls `setState` is exactly the shape `no-adjust-state-on-prop-
// change` flags (and it forced a documented mount-timing race between the fetch
// effect and the default-link effect). Instead we expose it as a resource read
// SYNCHRONOUSLY during render via `useSyncExternalStore` over a tiny per-key
// request cache — the same external-store shape `grid/useRowCache.ts` uses. No
// fetch-driven `setState`, no `loaded` flag to go stale, so there is no ordering
// between two effects to get wrong.
//
// This is a SINGLE-store, single-key resource keyed by `(sheetId, columnId,
// rowIds)` and backed by one per-key request cache. It is not a cross-store
// composition and does not subscribe imperatively to any workspace store — the
// no-imperative-cross-store-subscribers invariant (state/workspaceTransitions.ts)
// holds by construction.

import { useCallback, useMemo, useSyncExternalStore } from 'react';
import type { ColumnEvidenceApiPort } from '../api/ports';
import type { CellEvidenceLinkSummary } from '../api/types';

/** The single endpoint this resource needs — a minimal, injectable port. */
type ColumnEvidenceApi = ColumnEvidenceApiPort;

export interface ColumnEvidenceSnapshot {
  /** 'pending' while the batch request is in flight; 'ready'/'error' once it
   *  settles. Both settled states expose a (possibly empty) `byRow`. */
  status: 'pending' | 'ready' | 'error';
  /** row_id (string) -> its active evidence links, in rank order. A row with
   *  no active evidence is simply absent (matches the batch payload). */
  byRow: Record<string, CellEvidenceLinkSummary[]>;
}

// Stable module constants so `getSnapshot` returns a referentially-stable value
// for the empty/pending cases (a fresh object each call would loop
// useSyncExternalStore).
const EMPTY_READY: ColumnEvidenceSnapshot = { status: 'ready', byRow: {} };
const PENDING: ColumnEvidenceSnapshot = { status: 'pending', byRow: {} };

interface Entry {
  snapshot: ColumnEvidenceSnapshot;
  listeners: Set<() => void>;
  started: boolean;
}

// Bounded per-key cache. Entries persist after their last listener unsubscribes
// (so a grid<->answers remount re-reads a resolved snapshot without a pending
// flash); we evict zero-listener entries once the cache grows past the cap.
const CACHE_CAP = 32;
const cache = new Map<string, Entry>();

function evictIfNeeded(): void {
  if (cache.size <= CACHE_CAP) return;
  for (const [key, entry] of cache) {
    if (cache.size <= CACHE_CAP) break;
    if (entry.listeners.size === 0) cache.delete(key);
  }
}

function ensureEntry(key: string): Entry {
  let entry = cache.get(key);
  if (!entry) {
    entry = { snapshot: PENDING, listeners: new Set(), started: false };
    cache.set(key, entry);
    evictIfNeeded();
  }
  return entry;
}

function publish(key: string, snapshot: ColumnEvidenceSnapshot): void {
  const entry = cache.get(key);
  if (!entry) return;
  entry.snapshot = snapshot;
  for (const listener of entry.listeners) listener();
}

async function fetchInto(
  key: string,
  gridApi: ColumnEvidenceApi,
  sheetId: string,
  columnId: string,
  rowIds: readonly string[],
): Promise<void> {
  try {
    const payload = await gridApi.getColumnEvidence(sheetId, columnId, {
      rowIds: [...rowIds],
    });
    const byRow: Record<string, CellEvidenceLinkSummary[]> = {};
    for (const row of payload.rows) byRow[String(row.row_id)] = row.links;
    publish(key, { status: 'ready', byRow });
  } catch {
    publish(key, { status: 'error', byRow: {} });
  }
}

/**
 * Read the active column's evidence for `rowIds` as a keyed resource. The batch
 * request (one `getColumnEvidence` per (column, loaded rows)) fires lazily on
 * the first subscription miss for a key; the resolved snapshot is read during
 * render. Passing `columnId === null` or empty `rowIds` yields a
 * settled-empty snapshot with no request.
 *
 * Pass a referentially-stable `rowIds` (e.g. `useMemo`); the subscription is
 * keyed on its identity, so a fresh array each render would needlessly re-
 * subscribe (it would NOT re-fetch — `started` gates that per key).
 */
export function useColumnEvidence(
  sheetId: string,
  columnId: string | null,
  rowIds: readonly string[],
  gridApi: ColumnEvidenceApi,
): ColumnEvidenceSnapshot {
  const key =
    columnId === null || rowIds.length === 0
      ? ''
      : `${sheetId}:${columnId}:${rowIds.join(',')}`;

  const subscribe = useCallback(
    (listener: () => void) => {
      if (key === '' || columnId === null) return () => {};
      const entry = ensureEntry(key);
      entry.listeners.add(listener);
      if (!entry.started) {
        entry.started = true;
        void fetchInto(key, gridApi, sheetId, columnId, rowIds);
      }
      return () => {
        entry.listeners.delete(listener);
      };
    },
    [key, gridApi, sheetId, columnId, rowIds],
  );

  const getSnapshot = useCallback(
    () => (key === '' ? EMPTY_READY : (cache.get(key)?.snapshot ?? PENDING)),
    [key],
  );

  const snapshot = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
  // Referentially-stable return (snapshot itself is already stable per key).
  return useMemo(() => snapshot, [snapshot]);
}
