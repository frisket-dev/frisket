import { useCallback, useEffect, useLayoutEffect, useReducer, useRef } from 'react';

import type {
  HttpSelectorChoicesQuery,
  HttpSelectorChoicesResponse,
} from '../api/selectorChoices';
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
  /** The displayed projection belongs to an earlier draft while a new one loads. */
  stale: boolean;
  error: Error | null;
  refresh(): void;
}

interface State {
  response: HttpSelectorChoicesResponse | null;
  responseScopeKey: string | null;
  presentationKey: string | null;
  loading: boolean;
  refreshing: boolean;
  error: Error | null;
  revision: number;
  scopeKey: string | null;
}

type Action =
  | { type: 'load'; revision: number; scopeKey: string; presentationKey: string; preserveResponse: boolean }
  | { type: 'success'; revision: number; scopeKey: string; presentationKey: string; response: HttpSelectorChoicesResponse }
  | { type: 'failure'; revision: number; error: Error };

function reducer(state: State, action: Action): State {
  if (action.type === 'load') {
    return {
      response: action.preserveResponse ? state.response : null,
      responseScopeKey: action.preserveResponse ? state.responseScopeKey : null,
      presentationKey: action.presentationKey,
      loading: !action.preserveResponse,
      refreshing: action.preserveResponse,
      error: null,
      revision: action.revision,
      scopeKey: action.scopeKey,
    };
  }
  if (action.revision !== state.revision) return state;
  if (action.type === 'success') {
    return {
      ...state,
      response: action.response,
      responseScopeKey: action.scopeKey,
      presentationKey: action.presentationKey,
      loading: false,
      refreshing: false,
    };
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

function presentationKeyFor(
  projectId: string | null | undefined,
  query: HttpSelectorChoicesQuery,
): string {
  const subject = query.subject;
  if (subject.kind === 'action') {
    return JSON.stringify([projectId ?? '', subject.kind, subject.action_id, subject.field]);
  }
  return JSON.stringify([projectId ?? '', subject.kind]);
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
    responseScopeKey: null,
    presentationKey: null,
    loading: false,
    refreshing: false,
    error: null,
    revision: 0,
    scopeKey: null,
  });
  const [refreshRevision, refresh] = useReducer((value: number) => value + 1, 0);
  const requestRevision = useRef(0);
  const queryRef = useRef(query);
  const effectiveQueryKey = typeof queryKey === 'function' ? queryKey(query, state.response) : queryKey;
  const scopeKey = `${projectId ?? ''}\u0000${effectiveQueryKey}`;
  const presentationKey = presentationKeyFor(projectId, query);

  useLayoutEffect(() => {
    queryRef.current = query;
  }, [query]);

  useEffect(() => {
    if (!enabled || !projectId) return undefined;
    const controller = new AbortController();
    const revision = requestRevision.current + 1;
    requestRevision.current = revision;
    dispatch({
      type: 'load',
      revision,
      scopeKey,
      presentationKey,
      preserveResponse: state.response !== null
        && state.presentationKey === presentationKey,
    });
    void load(projectId, queryRef.current, { signal: controller.signal })
      .then((response) => {
        if (!controller.signal.aborted) dispatch({
          type: 'success',
          revision,
          scopeKey,
          presentationKey,
          response,
        });
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
  }, [enabled, load, presentationKey, projectId, refreshRevision, scopeKey]);

  const refreshChoices = useCallback(() => refresh(), []);
  if (!enabled || !projectId) {
    return {
      response: null,
      loading: false,
      refreshing: false,
      stale: false,
      error: null,
      refresh: refreshChoices,
    };
  }
  // Effects run after paint. Never let that first render bind a prior
  // project's (or a different selector subject's) setup/details to this
  // field while the new scope begins loading. Draft changes within the same
  // subject deliberately retain the presentation below so pinned setup can
  // survive its authoritative refresh.
  const active = state.presentationKey === presentationKey ? state : null;
  return {
    response: active?.response ?? null,
    loading: active?.loading ?? true,
    refreshing: active?.refreshing ?? false,
    stale: active?.response != null && active.responseScopeKey !== scopeKey,
    error: active?.error ?? null,
    refresh: refreshChoices,
  };
}
