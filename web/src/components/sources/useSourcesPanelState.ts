import { useCallback, useEffect, useReducer } from 'react';
import type { FormEvent } from 'react';
import type { SourceInfo, SourceInput } from '../../api/open';
import type { SourceApiPort } from '../../api/ports';
import { buildSourceInput, type SourceFormState } from './sourceConfig';
import { initialSourcesState, sourcesReducer } from './sourceState';

const msg = (e: unknown): string => (e instanceof Error ? e.message : String(e));

export function useSourcesPanelState(
  sourceApi: SourceApiPort,
  onPolled?: (sheetId: number | null) => void,
) {
  const [state, dispatch] = useReducer(sourcesReducer, undefined, initialSourcesState);

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

  const loadHealth = useCallback(async (id: number, reportError = true) => {
    dispatch({ type: 'set-health-loading', loading: true });
    try {
      const health = await sourceApi.getSourceHealth(id, 0, 20);
      dispatch({ type: 'health-loaded', health });
    } catch (e) {
      if (reportError) dispatch({ type: 'set-health-error', error: msg(e) });
      else dispatch({ type: 'set-health-loading', loading: false });
    }
  }, [sourceApi]);

  const submit = useCallback(async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    let input: SourceInput;
    try {
      input = buildSourceInput(state.form);
    } catch (error) {
      dispatch({ type: 'set-error', error: msg(error) });
      return;
    }

    const editingId = state.form.editingId;
    dispatch({ type: 'set-busy', busy: editingId ?? 'new' });
    dispatch({ type: 'set-error', error: null });
    try {
      if (editingId == null) {
        await sourceApi.createSource(input);
      } else {
        await sourceApi.updateSource(editingId, input);
      }
      dispatch({ type: 'reset-form' });
      await refresh();
      if (editingId != null && state.healthOpenId === editingId) {
        await loadHealth(editingId, false);
      }
    } catch (error) {
      dispatch({ type: 'set-error', error: msg(error) });
    } finally {
      dispatch({ type: 'set-busy', busy: null });
    }
  }, [loadHealth, refresh, sourceApi, state.form, state.healthOpenId]);

  const fetchNow = useCallback(async (id: number) => {
    dispatch({ type: 'set-busy', busy: id });
    dispatch({ type: 'set-error', error: null });
    try {
      const res = await sourceApi.fetchSource(id);
      onPolled?.(res.sheetId);
    } catch (e) {
      // A failed poll is recorded server-side; surface it inline and let the
      // run history / health drawer show the detail from the redacted routes.
      dispatch({ type: 'set-error', error: `Poll failed: ${msg(e)}` });
    } finally {
      dispatch({ type: 'set-busy', busy: null });
      dispatch({ type: 'set-expanded', id, open: true });
      await loadRuns(id, false);
      if (state.healthOpenId === id) await loadHealth(id, false);
    }
  }, [loadHealth, loadRuns, onPolled, sourceApi, state.healthOpenId]);

  const changeSchedule = useCallback(async (id: number, value: string) => {
    dispatch({ type: 'set-error', error: null });
    try {
      await sourceApi.updateSource(id, { schedule: value || null });
      await refresh();
      if (state.healthOpenId === id) await loadHealth(id, false);
    } catch (e) {
      dispatch({ type: 'set-error', error: msg(e) });
    }
  }, [loadHealth, refresh, sourceApi, state.healthOpenId]);

  const toggleEnabled = useCallback(async (id: number, enabled: boolean) => {
    dispatch({ type: 'set-error', error: null });
    try {
      await sourceApi.updateSource(id, { enabled });
      await refresh();
      if (state.healthOpenId === id) await loadHealth(id, false);
    } catch (e) {
      dispatch({ type: 'set-error', error: msg(e) });
    }
  }, [loadHealth, refresh, sourceApi, state.healthOpenId]);

  const remove = useCallback(async (id: number) => {
    if (state.armedDelete !== id) {
      dispatch({ type: 'arm-delete', id });
      return;
    }
    dispatch({ type: 'arm-delete', id: null });
    try {
      await sourceApi.deleteSource(id);
      dispatch({ type: 'source-deleted', id });
    } catch (e) {
      dispatch({ type: 'set-error', error: msg(e) });
    }
  }, [sourceApi, state.armedDelete]);

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
    state,
    actions: {
      setAdding: (adding: boolean) => dispatch({ type: 'set-adding', adding }),
      startEdit: (source: SourceInfo) => dispatch({ type: 'start-edit', source }),
      updateForm: (patch: Partial<SourceFormState>) => dispatch({ type: 'update-form', patch }),
      submit,
      fetchNow,
      changeSchedule,
      toggleEnabled,
      remove,
      toggleRuns,
      openHealth,
      closeHealth: () => dispatch({ type: 'set-health-open', id: null }),
    },
  };
}

export type SourcePanelActions = ReturnType<typeof useSourcesPanelState>['actions'];
