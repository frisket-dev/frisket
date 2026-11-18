import { useCallback, useSyncExternalStore } from 'react';
import type { CitationViewerApiPort } from '../api/ports';
import type { CitationViewerPayload } from '../api/types';
import { createCitationResourceCache } from './citationResourceCache';

interface CitationViewerSnapshot {
  status: 'pending' | 'ready' | 'error';
  payload: CitationViewerPayload | null;
}

const PENDING: CitationViewerSnapshot = { status: 'pending', payload: null };
const EMPTY: CitationViewerSnapshot = { status: 'ready', payload: null };
const ERROR: CitationViewerSnapshot = { status: 'error', payload: null };
const cache = createCitationResourceCache(PENDING, ERROR, 64);

/** Citation chips and the viewer share one request per scoped API/evidence link. */
export function useCitationViewerResource(
  linkId: string | number | null,
  linkApi: CitationViewerApiPort,
): CitationViewerSnapshot {
  const key = linkId === null ? null : String(linkId) || null;
  const subscribe = useCallback((listener: () => void) => {
    if (key === null || linkId === null) return () => {};
    return cache.subscribe(linkApi, key, listener, async () => ({
      status: 'ready', payload: await linkApi.getEvidenceViewer(linkId),
    }));
  }, [key, linkApi, linkId]);
  const getSnapshot = useCallback(
    () => key === null ? EMPTY : cache.read(linkApi, key),
    [linkApi, key],
  );
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}
