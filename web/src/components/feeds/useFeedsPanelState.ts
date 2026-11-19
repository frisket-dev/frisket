import { useCallback, useEffect, useReducer, useRef } from 'react';
import type { FormEvent } from 'react';
import type { FeedHealth, FeedInfo, FeedInput } from '../../api/open';
import type { FeedApiPort } from '../../api/ports';
import { buildFeedInput, type FeedFormState } from './feedConfig';
import { initialFeedsState, feedsReducer, type BusyFeed } from './feedState';
import { useFeedHealth, type FeedCheckJobs } from './useFeedHealth';
import { errorMessage } from '../../hooks/errorMessage';

const msg = (e: unknown): string => errorMessage(e);

export function useFeedsPanelState(
  feedApi: FeedApiPort,
  jobs: FeedCheckJobs,
  onPolled?: (sheetId: number | null) => void,
) {
  const [state, dispatch] = useReducer(feedsReducer, undefined, initialFeedsState);
  const { entries, load: loadHealth, poll, forget } = useFeedHealth(feedApi, jobs);
  const currentState = useRef(state);
  useEffect(() => { currentState.current = state; }, [state]);
  const busyRequests = useRef(new Map<symbol, BusyFeed>());
  const beginBusy = useCallback((busy: BusyFeed) => {
    const request = Symbol();
    busyRequests.current.set(request, busy);
    dispatch({ type: 'set-busy', busy });
    return request;
  }, []);
  const endBusy = useCallback((request: symbol) => {
    busyRequests.current.delete(request);
    dispatch({ type: 'set-busy', busy: [...busyRequests.current.values()].at(-1) ?? null });
  }, []);

  const refresh = useCallback(async () => {
    try {
      const items = await feedApi.listFeeds();
      dispatch({ type: 'sources-loaded', items });
    } catch (e) {
      dispatch({ type: 'set-error', error: msg(e) });
    }
  }, [feedApi]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    const onChanged = () => {
      void refresh();
    };
    window.addEventListener('frisket:sources-changed', onChanged);
    return () => window.removeEventListener('frisket:sources-changed', onChanged);
  }, [refresh]);

  const loadRuns = useCallback(async (id: number, reportError = true) => {
    try {
      const detail = await feedApi.getFeed(id, 0, 20);
      dispatch({ type: 'source-detail-loaded', detail });
    } catch (e) {
      if (reportError) dispatch({ type: 'set-error', error: msg(e) });
    }
  }, [feedApi]);

  const refreshAfterMutation = useCallback(async (id: number | null) => {
    await refresh();
    if (id != null && currentState.current.healthOpenId === id) await loadHealth(id, false);
  }, [loadHealth, refresh]);

  const submit = useCallback(async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    let input: FeedInput;
    try {
      input = buildFeedInput(state.form);
    } catch (error) {
      dispatch({ type: 'set-error', error: msg(error) });
      return;
    }

    const form = state.form;
    const session = state.editorSession;
    const editingId = form.editingId;
    const request = beginBusy(editingId ?? 'new');
    dispatch({ type: 'set-error', error: null });
    try {
      const saved = editingId == null
        ? await feedApi.createFeed(input)
        : await feedApi.updateFeed(editingId, input);
      dispatch({ type: 'saved-form', session, form, sourceId: saved.id });
      await refreshAfterMutation(saved.id);
    } catch (error) {
      dispatch({ type: 'form-error', session, error: msg(error) });
    } finally {
      endBusy(request);
    }
  }, [beginBusy, endBusy, refreshAfterMutation, feedApi, state.editorSession, state.form]);

  const fetchNow = useCallback(async (id: number) => {
    const request = beginBusy(id);
    dispatch({ type: 'set-error', error: null });
    try {
      const error = await poll(id, {
        onPolled,
        afterPoll: async () => {
          dispatch({ type: 'set-expanded', id, open: true });
          await Promise.all([refresh(), loadRuns(id, false)]);
        },
        refreshHealth: () => currentState.current.healthOpenId === id,
      });
      if (error) dispatch({ type: 'set-error', error });
    } finally {
      endBusy(request);
    }
  }, [beginBusy, endBusy, loadRuns, onPolled, poll, refresh]);

  const patchFeed = useCallback(async (id: number, patch: Partial<FeedInput>) => {
    dispatch({ type: 'set-error', error: null });
    try {
      await feedApi.updateFeed(id, patch);
      await refreshAfterMutation(id);
    } catch (e) {
      dispatch({ type: 'set-error', error: msg(e) });
    }
  }, [refreshAfterMutation, feedApi]);

  const remove = useCallback(async (id: number) => {
    if (state.armedDelete !== id) {
      dispatch({ type: 'arm-delete', id });
      return;
    }
    dispatch({ type: 'arm-delete', id: null });
    try {
      await feedApi.deleteFeed(id);
      forget(id);
      dispatch({ type: 'source-deleted', id });
    } catch (e) {
      dispatch({ type: 'set-error', error: msg(e) });
    }
  }, [forget, feedApi, state.armedDelete]);

  const toggleRuns = useCallback(async (id: number) => {
    const open = !state.expanded[id];
    dispatch({ type: 'set-expanded', id, open });
    if (open && !state.runs[id]) await loadRuns(id);
  }, [loadRuns, state.expanded, state.runs]);

  const openHealth = useCallback(async (id: number) => {
    const nextId = state.healthOpenId === id ? null : id;
    dispatch({ type: 'set-health-open', id: nextId });
    if (nextId != null) await loadHealth(nextId);
  }, [loadHealth, state.healthOpenId]);

  return {
    state: {
      ...state,
      health: Object.fromEntries<FeedHealth>(Object.entries(entries).flatMap(([id, entry]) => entry.health ? [[id, entry.health] as const] : [])),
      healthLoading: state.healthOpenId != null && Boolean(entries[state.healthOpenId]?.loading),
      healthError: state.healthOpenId == null ? null : entries[state.healthOpenId]?.error ?? null,
    },
    actions: {
      setAdding: (adding: boolean) => dispatch({ type: 'set-adding', adding }),
      startEdit: (feed: FeedInfo) => dispatch({ type: 'start-edit', source: feed }),
      updateForm: (patch: Partial<FeedFormState>) => dispatch({ type: 'update-form', patch }),
      submit,
      fetchNow,
      changeSchedule: (id: number, value: string) => patchFeed(id, { schedule: value || null }),
      toggleEnabled: (id: number, enabled: boolean) => patchFeed(id, { enabled }),
      remove,
      toggleRuns,
      openHealth,
      closeHealth: () => dispatch({ type: 'set-health-open', id: null }),
    },
  };
}

export type FeedPanelActions = ReturnType<typeof useFeedsPanelState>['actions'];
