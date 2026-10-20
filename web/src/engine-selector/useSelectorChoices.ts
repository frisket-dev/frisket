import { useCallback, useEffect, useLayoutEffect, useReducer, useRef } from 'react';

import { readModelPull } from '../api/modelPullOperations';
import type {
  HttpSelectorChoicesQuery,
  HttpSelectorChoicesResponse,
} from '../api/selectorChoices';
import { selectorChoicesApi } from '../api/selectorChoices';
import type { ModelPullDto } from '../api/types';
import { useModelPullPolling } from '../hooks/useModelPullPolling';
import { CLOUD_CONNECTION_CHANGED } from '../cloud/connectionChanged';

export type SelectorChoicesLoader = (
  projectId: string,
  query: HttpSelectorChoicesQuery,
  options: { signal: AbortSignal },
) => Promise<HttpSelectorChoicesResponse>;

type ModelPullScope = 'workspace' | 'organization';

export type SelectorPullLoader = (
  scope: ModelPullScope,
  id: number,
  options: { signal: AbortSignal },
) => Promise<ModelPullDto>;

export interface UseSelectorChoicesOptions {
  projectId: string | null | undefined;
  query: HttpSelectorChoicesQuery;
  /** A caller-provided identity for the capability-affecting draft fields. */
  queryKey: string | ((query: HttpSelectorChoicesQuery, response: HttpSelectorChoicesResponse | null) => string);
  enabled?: boolean;
  load?: SelectorChoicesLoader;
  /** Fetches the one durable pull currently projected into this selector. */
  loadPull?: SelectorPullLoader;
}

export interface SelectorChoicesState {
  response: HttpSelectorChoicesResponse | null;
  loading: boolean;
  refreshing: boolean;
  /** The displayed projection has not been verified for the current request scope. */
  stale: boolean;
  error: Error | null;
  /** A progress read failed; the last accepted selector projection remains usable. */
  operationError: Error | null;
  /** Reloads and resolves only after the authoritative projection settles. */
  refresh(): Promise<boolean>;
}

interface State {
  response: HttpSelectorChoicesResponse | null;
  responseScopeKey: string | null;
  presentationKey: string | null;
  loading: boolean;
  refreshing: boolean;
  error: Error | null;
  operationError: Error | null;
  terminalOperation: ActiveOperation | null;
  revision: number;
  scopeKey: string | null;
}

type Action =
  | { type: 'load'; revision: number; scopeKey: string; presentationKey: string; preserveResponse: boolean }
  | { type: 'success'; revision: number; scopeKey: string; presentationKey: string; response: HttpSelectorChoicesResponse }
  | { type: 'failure'; revision: number; error: Error }
  | { type: 'progress'; revision: number; scopeKey: string; pull: ModelPullDto; terminal: boolean }
  | { type: 'progressFailure'; revision: number; scopeKey: string; error: Error };

function reducer(state: State, action: Action): State {
  if (action.type === 'load') {
    return {
      response: action.preserveResponse ? state.response : null,
      responseScopeKey: action.preserveResponse ? state.responseScopeKey : null,
      presentationKey: action.presentationKey,
      loading: !action.preserveResponse,
      refreshing: action.preserveResponse,
      error: null,
      operationError: null,
      terminalOperation: state.scopeKey === action.scopeKey ? state.terminalOperation : null,
      revision: action.revision,
      scopeKey: action.scopeKey,
    };
  }
  if (action.revision !== state.revision) return state;
  if (action.type === 'success') {
    const active = activeOperation(action.response);
    const terminalOperation = active?.id !== state.terminalOperation?.id
      ? null
      : state.terminalOperation;
    return {
      ...state,
      response: action.response,
      responseScopeKey: action.scopeKey,
      presentationKey: action.presentationKey,
      loading: false,
      refreshing: false,
      operationError: null,
      terminalOperation,
    };
  }
  if (action.type === 'progress') {
    if (action.scopeKey !== state.scopeKey || state.response === null) return state;
    const operation = activeOperation(state.response);
    if (operation?.id !== action.pull.id) return state;
    const response = replaceOperation(state.response, action.pull);
    return response === state.response ? state : {
      ...state,
      response,
      operationError: null,
      terminalOperation: action.terminal ? { id: action.pull.id, scope: operation.scope } : state.terminalOperation,
    };
  }
  if (action.type === 'progressFailure') {
    if (action.scopeKey !== state.scopeKey) return state;
    return { ...state, operationError: action.error };
  }
  return { ...state, responseScopeKey: null, loading: false, refreshing: false, error: action.error };
}

interface ActiveOperation {
  id: number;
  scope: ModelPullScope;
}

type SelectorChoiceWire = HttpSelectorChoicesResponse['groups'][number]['choices'][number];
type DownloadSetup = Extract<NonNullable<SelectorChoiceWire['setup']>, { kind: 'engine_setup' | 'artifact_download' }>;

function isDownloadSetup(setup: SelectorChoiceWire['setup'] | SelectorChoiceWire['configuration_actions'][number]): setup is DownloadSetup {
  return setup?.kind === 'engine_setup' || setup?.kind === 'artifact_download';
}

function downloadSetups(choice: SelectorChoiceWire): DownloadSetup[] {
  return [choice.setup, ...choice.configuration_actions].filter(isDownloadSetup);
}

function activeOperation(response: HttpSelectorChoicesResponse): ActiveOperation | null {
  const choices = response.groups.flatMap((group) => group.choices);
  if (response.orphaned_current) choices.push(response.orphaned_current);
  for (const choice of choices) {
    for (const setup of downloadSetups(choice)) {
      const active = choice.active_operation?.model === setup.setup_ref
        ? choice.active_operation
        : null;
      const operation = active ?? setup.blocked_by_operation;
      if (operation?.status === 'pending' || operation?.status === 'running') {
        return { id: operation.id, scope: setup.scope };
      }
    }
  }
  return null;
}

function replaceOperation(
  response: HttpSelectorChoicesResponse,
  pull: ModelPullDto,
): HttpSelectorChoicesResponse {
  let changed = false;
  const replaceChoice = (choice: HttpSelectorChoicesResponse['groups'][number]['choices'][number]) => {
    const active = choice.active_operation?.id === pull.id ? pull : choice.active_operation;
    const setup = isDownloadSetup(choice.setup) && choice.setup.blocked_by_operation?.id === pull.id
      ? { ...choice.setup, blocked_by_operation: pull }
      : choice.setup;
    let actionsChanged = false;
    const configurationActions = choice.configuration_actions.map((action) => {
      if (!isDownloadSetup(action) || action.blocked_by_operation?.id !== pull.id) return action;
      actionsChanged = true;
      return { ...action, blocked_by_operation: pull };
    });
    if (active === choice.active_operation && setup === choice.setup && !actionsChanged) return choice;
    changed = true;
    return { ...choice, active_operation: active, setup,
      configuration_actions: actionsChanged ? configurationActions : choice.configuration_actions };
  };
  const groups = response.groups.map((group) => ({
    ...group,
    choices: group.choices.map(replaceChoice),
  }));
  const orphanedCurrent = response.orphaned_current === null
    ? null
    : replaceChoice(response.orphaned_current);
  return changed ? { ...response, groups, orphaned_current: orphanedCurrent } : response;
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
 * Loads the server's authoritative selector projection. Each project,
 * subject, or capability draft change owns a request scope, so an older
 * response cannot describe a newer form. A currently projected durable pull
 * is read independently and patched into that projection; the catalog is
 * fetched again only when the pull reaches a terminal state.
 */
export function useSelectorChoices({
  projectId,
  query,
  queryKey,
  enabled = true,
  load = defaultLoad,
  loadPull = readModelPull,
}: UseSelectorChoicesOptions): SelectorChoicesState {
  const [state, dispatch] = useReducer(reducer, {
    response: null,
    responseScopeKey: null,
    presentationKey: null,
    loading: false,
    refreshing: false,
    error: null,
    operationError: null,
    terminalOperation: null,
    revision: 0,
    scopeKey: null,
  });
  const [refreshRevision, requestRefresh] = useReducer((value: number) => value + 1, 0);
  const requestRevision = useRef(0);
  const pendingRefresh = useRef<{
    scopeKey: string;
    minimumRevision: number;
    promise: Promise<boolean>;
    resolve(result: boolean): void;
  } | null>(null);
  const queryRef = useRef(query);
  const effectiveQueryKey = typeof queryKey === 'function' ? queryKey(query, state.response) : queryKey;
  const scopeKey = `${projectId ?? ''}\u0000${effectiveQueryKey}`;
  const presentationKey = presentationKeyFor(projectId, query);

  useLayoutEffect(() => {
    queryRef.current = query;
  }, [query]);

  const settleRefresh = useCallback((settledScopeKey: string, revision: number, result: boolean) => {
    const pending = pendingRefresh.current;
    if (pending?.scopeKey !== settledScopeKey || revision < pending.minimumRevision) return;
    pendingRefresh.current = null;
    pending.resolve(result);
  }, []);

  const cancelRefresh = useCallback((cancelledScopeKey: string) => {
    const pending = pendingRefresh.current;
    if (pending?.scopeKey !== cancelledScopeKey) return;
    pendingRefresh.current = null;
    pending.resolve(false);
  }, []);

  useEffect(() => () => cancelRefresh(scopeKey), [cancelRefresh, enabled, scopeKey]);

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
        if (controller.signal.aborted) return;
        dispatch({
          type: 'success',
          revision,
          scopeKey,
          presentationKey,
          response,
        });
        settleRefresh(scopeKey, revision, true);
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted && !isAbort(error)) {
          dispatch({
            type: 'failure',
            revision,
            error: error instanceof Error ? error : new Error('Could not load choices.'),
          });
          settleRefresh(scopeKey, revision, false);
        }
      });
    return () => {
      controller.abort();
    };
  }, [enabled, load, presentationKey, projectId, refreshRevision, scopeKey, settleRefresh]);

  const acceptedResponse = state.responseScopeKey === scopeKey ? state.response : null;
  const projectedOperation = acceptedResponse === null ? null : activeOperation(acceptedResponse);
  const pollingOperation = projectedOperation !== null
    && !(state.terminalOperation?.scope === projectedOperation.scope && state.terminalOperation.id === projectedOperation.id)
    ? projectedOperation
    : null;
  const pollingOperationId = pollingOperation?.id ?? null;
  const pollingOperationScope = pollingOperation?.scope ?? null;
  useModelPullPolling({
    id: pollingOperationId,
    scopeKey: `${scopeKey}\u0000${pollingOperationScope}\u0000${state.revision}`,
    active: enabled && Boolean(projectId) && pollingOperationScope !== null,
    fetchPull: (id, signal) => loadPull(pollingOperationScope!, id, { signal }),
    onProgress: (pull, terminal) => {
      dispatch({ type: 'progress', revision: state.revision, scopeKey, pull, terminal });
      if (terminal) requestRefresh();
    },
    onError: (error) => dispatch({ type: 'progressFailure', revision: state.revision, scopeKey, error }),
    intervalMs: 2000,
    maxIntervalMs: 16_000,
    mode: 'settle-relative',
  });

  const refreshChoices = useCallback((): Promise<boolean> => {
    if (!enabled || !projectId) return Promise.resolve(false);
    const pending = pendingRefresh.current;
    if (pending?.scopeKey === scopeKey) return pending.promise;
    let resolve!: (result: boolean) => void;
    const promise = new Promise<boolean>((settle) => { resolve = settle; });
    pendingRefresh.current = {
      scopeKey,
      minimumRevision: requestRevision.current + 1,
      promise,
      resolve,
    };
    requestRefresh();
    return promise;
  }, [enabled, projectId, scopeKey]);
  useEffect(() => {
    if (!enabled || !projectId) return;
    const refresh = () => { void refreshChoices(); };
    window.addEventListener(CLOUD_CONNECTION_CHANGED, refresh);
    return () => window.removeEventListener(CLOUD_CONNECTION_CHANGED, refresh);
  }, [enabled, projectId, refreshChoices]);
  if (!enabled || !projectId) {
    return {
      response: null,
      loading: false,
      refreshing: false,
      stale: false,
      error: null,
      operationError: null,
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
    operationError: active?.operationError ?? null,
    refresh: refreshChoices,
  };
}
