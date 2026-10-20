import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { ModelPullDto } from '../api/types';
import { usePoll } from './usePoll';

export function modelPullActive(pull: Pick<ModelPullDto, 'status'> | null | undefined): boolean {
  return pull?.status === 'pending' || pull?.status === 'running';
}

/** One pull observer. The parent owns its snapshot and what completion refreshes. */
export function useModelPullPolling({
  id, scopeKey, active, fetchPull, onProgress, onError,
  intervalMs = 1000, maxIntervalMs = intervalMs, mode = 'interval',
}: {
  id: number | null;
  scopeKey: string;
  active: boolean;
  fetchPull(id: number, signal: AbortSignal): Promise<ModelPullDto>;
  onProgress(pull: ModelPullDto, terminal: boolean): void;
  onError?(error: Error): void;
  intervalMs?: number;
  maxIntervalMs?: number;
  mode?: 'interval' | 'settle-relative';
}): void {
  const session = useMemo(() => ({ requests: new Set<AbortController>() }), [id, scopeKey, active]);
  const current = useRef(session);
  useLayoutEffect(() => { current.current = session; }, [session]);
  useEffect(() => () => {
    session.requests.forEach((request) => request.abort());
  }, [session]);
  const [progress, setProgress] = useState({ session, failures: 0, terminal: false });
  const accepted = progress.session === session ? progress : null;
  usePoll(async () => {
    if (id === null) return 'stop';
    const request = new AbortController();
    session.requests.add(request);
    const isCurrent = () => !request.signal.aborted && current.current === session;
    try {
      const fresh = await fetchPull(id, request.signal);
      if (!isCurrent()) return;
      const terminal = !modelPullActive(fresh);
      setProgress({ session, failures: 0, terminal });
      onProgress(fresh, terminal);
      if (terminal) return 'stop';
    } catch (caught) {
      if (!isCurrent()) return;
      setProgress((previous) => ({
        session, terminal: false,
        failures: previous.session === session ? previous.failures + 1 : 1,
      }));
      onError?.(caught instanceof Error ? caught : new Error('Could not refresh setup progress.'));
    } finally {
      session.requests.delete(request);
    }
  }, {
    active: active && id !== null && !accepted?.terminal,
    intervalMs: Math.min(intervalMs * 2 ** (accepted?.failures ?? 0), maxIntervalMs),
    guardOverlap: true,
    mode,
  });
}
