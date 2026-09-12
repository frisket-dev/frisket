import { useCallback, useEffect, useReducer, useRef } from 'react';

import type {
  HttpSelectorChoicesQuery,
  HttpSelectorChoicesResponse,
} from '../generated/openHttpContracts';
import { selectorChoicesApi } from '../api/selectorChoices';

export type SelectorChoicesLoader = (
  projectId: string,
  query: HttpSelectorChoicesQuery,
  options: { signal: AbortSignal },
) => Promise<HttpSelectorChoicesResponse>;

export interface UseSelectorChoicesOptions {
  projectId: string | null | undefined;
  query: HttpSelectorChoicesQuery;
  /** A caller-provided identity for the capability-affecting draft fields. */
  queryKey: string | ((query: HttpSelectorChoicesQuery, response: HttpSelectorChoicesResponse | null) => string);
  enabled?: boolean;
  load?: SelectorChoicesLoader;
}

export interface SelectorChoicesState {
  response: HttpSelectorChoicesResponse | null;
  loading: boolean;
  refreshing: boolean;
  refreshing: boolean;
  error: Error | null;
  refresh(): void;
}

interface State {
  response: HttpSelectorChoicesResponse | null;
  loading: boolean;
  error: Error | null;
  revision: number;
  scopeKey: string | null;
}

type Action =
  | { type: 'load'; revision: number; scopeKey: string; preserveResponse: boolean }
  | { type: 'success'; revision: number; response: HttpSelectorChoicesResponse }
  | { type: 'failure'; revision: number; error: Error };

function reducer(state: State, action: Action): State {
  if (action.type === 'load') {
    return {
      response: action.preserveResponse ? state.response : null,
      loading: !action.preserveResponse,
      refreshing: action.preserveResponse,
      error: null,
      revision: action.revision,
      scopeKey: action.scopeKey,
    };
  }
  if (action.revision !== state.revision) return state;
  if (action.type === 'success') {
    return { ...state, response: action.response, loading: false, refreshing: false };
  }
  return { ...state, loading: false, refreshing: false, error: action.error };
}

function defaultLoad(
  projectId: string,
  query: HttpSelectorChoicesQuery,
  options: { signal: AbortSignal },
): Promise<HttpSelectorChoicesResponse> {
  return selectorChoicesApi.getSelectorChoices(projectId, query, options);
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError';
}

/**
 * Loads only the server's authoritative selector projection. Each project,
 * subject, or capability draft change owns a request scope, so an older
 * response cannot describe a newer form.
 */
export function useSelectorChoices({
  projectId,
  query,
  queryKey,
  enabled = true,
  load = defaultLoad,
}: UseSelectorChoicesOptions): SelectorChoicesState {
  const [state, dispatch] = useReducer(reducer, {
    response: null,
    loading: false,
    refreshing: false,
    error: null,
    revision: 0,
    scopeKey: null,
  });
  const [refreshRevision, refresh] = useReducer((value: number) => value + 1, 0);
  const requestRevision = useRef(0);
  const queryRef = useRef(query);
  queryRef.current = query;
  const effectiveQueryKey = typeof queryKey === 'function' ? queryKey(query, state.response) : queryKey;
  const scopeKey = `${projectId ?? ''}\u0000${effectiveQueryKey}`;

  useEffect(() => {
    if (!enabled || !projectId) return undefined;
    const controller = new AbortController();
    const revision = requestRevision.current + 1;
    requestRevision.current = revision;
    dispatch({
      type: 'load',
      revision,
      scopeKey,
      preserveResponse: refreshRevision > 0 && state.scopeKey === scopeKey && state.response !== null,
    });
    void load(projectId, queryRef.current, { signal: controller.signal })
      .then((response) => {
        if (!controller.signal.aborted) dispatch({ type: 'success', revision, response });
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted && !isAbort(error)) {
          dispatch({
            type: 'failure',
            revision,
            error: error instanceof Error ? error : new Error('Could not load choices.'),
          });
        }
      });
    return () => controller.abort();
  }, [enabled, load, projectId, refreshRevision, scopeKey]);

  const refreshChoices = useCallback(() => refresh(), []);
  if (!enabled || !projectId) {
    return { response: null, loading: false, refreshing: false, error: null, refresh: refreshChoices };
  }
  const active = state.scopeKey === scopeKey ? state : {
    response: null,
    loading: true,
    refreshing: false,
    error: null,
  };
  return {
    response: active.response,
    loading: active.loading,
    refreshing: active.refreshing,
    error: active.error,
    refresh: refreshChoices,
  };
}
