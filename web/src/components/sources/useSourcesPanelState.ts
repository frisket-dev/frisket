import { useCallback, useEffect, useReducer, useRef } from 'react';
import type { FormEvent } from 'react';
import type { SourceHealth, SourceInfo, SourceInput } from '../../api/open';
import type { SourceApiPort } from '../../api/ports';
import { buildSourceInput, type SourceFormState } from './sourceConfig';
import { initialSourcesState, sourcesReducer, type BusySource } from './sourceState';
import { useSourceHealth } from './useSourceHealth';

const msg = (e: unknown): string => (e instanceof Error ? e.message : String(e));

export function useSourcesPanelState(
  sourceApi: SourceApiPort,
  onPolled?: (sheetId: number | null) => void,
) {
  const [state, dispatch] = useReducer(sourcesReducer, undefined, initialSourcesState);
  const { entries, load: loadHealth, poll, forget } = useSourceHealth(sourceApi);
  const currentState = useRef(state);
  useEffect(() => { currentState.current = state; }, [state]);
  const busyRequests = useRef(new Map<symbol, BusySource>());
  const beginBusy = useCallback((busy: BusySource) => {
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
      const items = await sourceApi.listSources();
      dispatch({ type: 'sources-loaded', items });
    } catch (e) {
      dispatch({ type: 'set-error', error: msg(e) });
    }
  }, [sourceApi]);

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
      const detail = await sourceApi.getSource(id, 0, 20);
      dispatch({ type: 'source-detail-loaded', detail });
    } catch (e) {
      if (reportError) dispatch({ type: 'set-error', error: msg(e) });
    }
  }, [sourceApi]);

  const refreshAfterMutation = useCallback(async (id: number | null) => {
    await refresh();
    if (id != null && currentState.current.healthOpenId === id) await loadHealth(id, false);
  }, [loadHealth, refresh]);

  const submit = useCallback(async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    let input: SourceInput;
    try {
      input = buildSourceInput(state.form);
    } catch (error) {
      dispatch({ type: 'set-error', error: msg(error) });
      return;
    }

    const form = state.form;
    const editingId = form.editingId;
    const request = beginBusy(editingId ?? 'new');
    dispatch({ type: 'set-error', error: null });
    try {
      if (editingId == null) {
        await sourceApi.createSource(input);
      } else {
        await sourceApi.updateSource(editingId, input);
      }
      dispatch({ type: 'saved-form', form });
      await refreshAfterMutation(editingId);
    } catch (error) {
      dispatch({ type: 'form-error', form, error: msg(error) });
    } finally {
      endBusy(request);
    }
  }, [beginBusy, endBusy, refreshAfterMutation, sourceApi, state.form]);

  const fetchNow = useCallback(async (id: number) => {
    const request = beginBusy(id);
    dispatch({ type: 'set-error', error: null });
    try {
      const error = await poll(id, {
        onPolled,
        afterPoll: async () => {
          dispatch({ type: 'set-expanded', id, open: true });
          await loadRuns(id, false);
        },
        refreshHealth: () => currentState.current.healthOpenId === id,
      });
      if (error) dispatch({ type: 'set-error', error });
    } finally {
      endBusy(request);
    }
  }, [beginBusy, endBusy, loadRuns, onPolled, poll]);

  const patchSource = useCallback(async (id: number, patch: Partial<SourceInput>) => {
    dispatch({ type: 'set-error', error: null });
    try {
      await sourceApi.updateSource(id, patch);
      await refreshAfterMutation(id);
    } catch (e) {
      dispatch({ type: 'set-error', error: msg(e) });
    }
  }, [refreshAfterMutation, sourceApi]);

  const remove = useCallback(async (id: number) => {
    if (state.armedDelete !== id) {
      dispatch({ type: 'arm-delete', id });
      return;
    }
    dispatch({ type: 'arm-delete', id: null });
    try {
      await sourceApi.deleteSource(id);
      forget(id);
      dispatch({ type: 'source-deleted', id });
    } catch (e) {
      dispatch({ type: 'set-error', error: msg(e) });
    }
  }, [forget, sourceApi, state.armedDelete]);

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
      health: Object.fromEntries<SourceHealth>(Object.entries(entries).flatMap(([id, entry]) => entry.health ? [[id, entry.health] as const] : [])),
      healthLoading: state.healthOpenId != null && Boolean(entries[state.healthOpenId]?.loading),
      healthError: state.healthOpenId == null ? null : entries[state.healthOpenId]?.error ?? null,
    },
    actions: {
      setAdding: (adding: boolean) => dispatch({ type: 'set-adding', adding }),
      startEdit: (source: SourceInfo) => dispatch({ type: 'start-edit', source }),
      updateForm: (patch: Partial<SourceFormState>) => dispatch({ type: 'update-form', patch }),
      submit,
      fetchNow,
      changeSchedule: (id: number, value: string) => patchSource(id, { schedule: value || null }),
      toggleEnabled: (id: number, enabled: boolean) => patchSource(id, { enabled }),
      remove,
      toggleRuns,
      openHealth,
      closeHealth: () => dispatch({ type: 'set-health-open', id: null }),
    },
  };
}

export type SourcePanelActions = ReturnType<typeof useSourcesPanelState>['actions'];
