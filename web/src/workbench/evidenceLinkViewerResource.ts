import { useCallback, useSyncExternalStore } from 'react';
import type { EvidenceLinkViewerApiPort } from '../api/ports';
import type { EvidenceViewerPayload } from '../api/types';
import { createEvidenceResourceCache } from './evidenceResourceCache';

export interface EvidenceLinkViewerSnapshot {
  status: 'pending' | 'ready' | 'error';
  payload: EvidenceViewerPayload | null;
}

const PENDING: EvidenceLinkViewerSnapshot = { status: 'pending', payload: null };
const EMPTY: EvidenceLinkViewerSnapshot = { status: 'ready', payload: null };
const ERROR: EvidenceLinkViewerSnapshot = { status: 'error', payload: null };
const cache = createEvidenceResourceCache(PENDING, ERROR, 64);

/** Citation chips and the viewer share one request per scoped API/evidence link. */
export function useEvidenceLinkViewer(
  linkId: string | number | null,
  linkApi: EvidenceLinkViewerApiPort,
): EvidenceLinkViewerSnapshot {
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
