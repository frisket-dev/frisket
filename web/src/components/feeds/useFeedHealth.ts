import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { FeedHealth } from '../../api/open';
import type { FeedApiPort } from '../../api/ports';
import { errorMessage } from '../../hooks/errorMessage';

interface HealthEntry {
  health: FeedHealth | null;
  loading: boolean;
  readError: string | null;
  pollError: string | null;
  busy: boolean;
}

const emptyEntry: HealthEntry = { health: null, loading: false, readError: null, pollError: null, busy: false };
const message = (error: unknown) => errorMessage(error);

/** Each source owns its request status; only its latest health read can publish. */
export function useFeedHealth(feedApi: FeedApiPort) {
  const [state, setState] = useState<{ api: FeedApiPort; entries: Record<number, HealthEntry> }>(() => ({ api: feedApi, entries: {} }));
  const scope = useMemo(() => ({ feedApi, reads: new Map<number, symbol>(), polls: new Map<number, symbol>() }), [feedApi]);
  const activeScope = useRef<typeof scope | null>(null);
  useEffect(() => {
    activeScope.current = scope;
    return () => {
      activeScope.current = null;
      scope.reads.clear();
      scope.polls.clear();
    };
  }, [scope]);

  const patch = useCallback((id: number, update: Partial<HealthEntry>) => {
    if (activeScope.current !== scope) return;
    setState((current) => ({
      api: feedApi,
      entries: { ...(current.api === feedApi ? current.entries : {}),
        [id]: { ...((current.api === feedApi ? current.entries[id] : null) ?? emptyEntry), ...update } },
    }));
  }, [scope, feedApi]);

  const load = useCallback(async (id: number, reportError = true) => {
    if (activeScope.current !== scope) return;
    const request = Symbol();
    scope.reads.set(id, request);
    const isCurrent = () => activeScope.current === scope && scope.reads.get(id) === request;
    patch(id, { loading: true, ...(reportError ? { readError: null, pollError: null } : {}) });
    try {
      const health = await feedApi.getFeedHealth(id, 0, 20);
      if (isCurrent()) patch(id, { health, loading: false, readError: null });
    } catch (error) {
      if (isCurrent()) patch(id, { loading: false, ...(reportError ? { readError: message(error) } : {}) });
    }
  }, [patch, scope, feedApi]);

  const poll = useCallback(async (id: number, options: {
    onPolled?(sheetId: number | null): void;
    afterPoll?(): Promise<void>;
    refreshHealth?(): boolean;
  } = {}) => {
    if (activeScope.current !== scope || scope.polls.has(id)) return;
    const request = Symbol();
    scope.polls.set(id, request);
    const isCurrent = () => activeScope.current === scope && scope.polls.get(id) === request;
    patch(id, { busy: true, readError: null, pollError: null });
    let problem: string | null = null;
    try {
      const result = await feedApi.fetchFeed(id);
      if (isCurrent()) options.onPolled?.(result.sheetId);
    } catch (error) {
      if (isCurrent()) {
        problem = `Poll failed: ${message(error)}`;
        patch(id, { pollError: problem });
      }
    } finally {
      if (isCurrent()) {
        scope.polls.delete(id);
        patch(id, { busy: false });
        await options.afterPoll?.();
        if (activeScope.current === scope && (options.refreshHealth?.() ?? true)) await load(id, false);
      }
    }
    return problem;
  }, [load, patch, scope, feedApi]);

  const forget = useCallback((id: number) => {
    if (activeScope.current !== scope) return;
    scope.reads.delete(id);
    scope.polls.delete(id);
    setState((current) => {
      const entries = { ...current.entries };
      delete entries[id];
      return { ...current, entries };
    });
  }, [scope]);

  const entries = Object.fromEntries(Object.entries(state.api === feedApi ? state.entries : {}).map(([id, entry]) =>
    [id, { ...entry, error: entry.pollError ?? entry.readError }] as const));
  return { entries, load, poll, forget };
}
