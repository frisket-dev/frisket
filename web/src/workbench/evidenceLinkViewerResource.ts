// evidence-link-viewer as a KEYED ASYNC RESOURCE, the same shape
// columnEvidenceResource.ts uses: a per-link request cache read synchronously via
// `useSyncExternalStore`, no fetch-driven `setState`. The Answers view needs
// "this evidence link's full viewer payload" (every span, not just the chip
// summary's rank-0 snippet) from MULTIPLE call sites — each citation chip AND the
// evidence pane's "Citation" header — so it is its own keyed resource rather than
// a plain `useEffect` per call site: two call sites for the SAME link share one
// cache entry/one in-flight request instead of double-fetching.

import { useCallback, useMemo, useSyncExternalStore } from 'react';
import type { EvidenceLinkViewerApiPort } from '../api/ports';
import type { EvidenceViewerPayload } from '../api/types';

/** The single endpoint this resource needs — a minimal, injectable port. */
type EvidenceLinkViewerApi = EvidenceLinkViewerApiPort;

export interface EvidenceLinkViewerSnapshot {
  status: 'pending' | 'ready' | 'error';
  payload: EvidenceViewerPayload | null;
}

const PENDING: EvidenceLinkViewerSnapshot = { status: 'pending', payload: null };
const EMPTY: EvidenceLinkViewerSnapshot = { status: 'ready', payload: null };

interface Entry {
  snapshot: EvidenceLinkViewerSnapshot;
  listeners: Set<() => void>;
  started: boolean;
}

// Bounded per-key cache, same eviction shape as columnEvidenceResource.ts:
// entries persist after their last listener unsubscribes (a chip list
// re-render or an answers-row swap-back re-reads a resolved snapshot
// without a pending flash) and are evicted only once the cache overflows.
const CACHE_CAP = 64;
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

function publish(key: string, snapshot: EvidenceLinkViewerSnapshot): void {
  const entry = cache.get(key);
  if (!entry) return;
  entry.snapshot = snapshot;
  for (const listener of entry.listeners) listener();
}

async function fetchInto(
  key: string,
  linkApi: EvidenceLinkViewerApi,
  linkId: string | number,
): Promise<void> {
  try {
    const payload = await linkApi.getEvidenceViewer(linkId);
    publish(key, { status: 'ready', payload });
  } catch {
    publish(key, { status: 'error', payload: null });
  }
}

/**
 * Read one evidence link's full viewer payload (every artifact/span, not the
 * chip-summary's single rank-0 snippet) as a keyed resource. The fetch fires
 * lazily on the first subscription miss for a key; `linkId === null` yields a
 * settled-empty snapshot with no request.
 */
export function useEvidenceLinkViewer(
  linkId: string | number | null,
  linkApi: EvidenceLinkViewerApi,
): EvidenceLinkViewerSnapshot {
  const key = linkId === null ? '' : String(linkId);

  const subscribe = useCallback(
    (listener: () => void) => {
      if (key === '' || linkId === null) return () => {};
      const entry = ensureEntry(key);
      entry.listeners.add(listener);
      if (!entry.started) {
        entry.started = true;
        void fetchInto(key, linkApi, linkId);
      }
      return () => {
        entry.listeners.delete(listener);
      };
    },
    [key, linkApi, linkId],
  );

  const getSnapshot = useCallback(
    () => (key === '' ? EMPTY : (cache.get(key)?.snapshot ?? PENDING)),
    [key],
  );

  const snapshot = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
  return useMemo(() => snapshot, [snapshot]);
}
