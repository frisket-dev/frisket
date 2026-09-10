import { useCallback, useEffect, useMemo, useReducer, useRef } from 'react';
import type { FormEvent } from 'react';
import {
  type EmbeddingIndexSummary,
  type EmbeddingProvider,
  type SheetMeta,
} from '../../api/open';
import type { EmbeddingApiPort } from '../../api/ports';
import {
  emptyCreateEmbeddingIndexForm,
  type CreateEmbeddingIndexForm,
} from './createForm';
import { EMBEDDING_TEXT_MODALITY } from './constants';

const TERMINAL_JOB_STATUSES = new Set(['done', 'failed', 'cancelled']);

/** Polls one action.run job (embedding.index_refresh rides the QUEUED_ACTION_JOB
 * lifecycle) until it reaches a terminal status, or gives up after
 * `maxAttempts`. Never throws — a poll failure (job endpoint unavailable, job
 * pruned) just stops polling so the caller's `load()` still runs and shows
 * whatever the indexes endpoint currently reports. */
async function pollEmbeddingJobUntilTerminal(
  apiPort: Pick<EmbeddingApiPort, 'getActionJob'>,
  jobId: number,
  {
    intervalMs = 500,
    maxAttempts = 120,
    shouldContinue = () => true,
  }: {
    intervalMs?: number;
    maxAttempts?: number;
    shouldContinue?: () => boolean;
  } = {},
): Promise<void> {
  if (!apiPort.getActionJob) return;
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    if (!shouldContinue()) return;
    try {
      const job = await apiPort.getActionJob(jobId);
      if (!shouldContinue()) return;
      if (TERMINAL_JOB_STATUSES.has(job.status)) return;
    } catch {
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
    if (!shouldContinue()) return;
  }
}

interface UseEmbeddingsPanelControllerOptions {
  apiPort: EmbeddingApiPort;
  sheet: SheetMeta;
}

interface EmbeddingsPanelState {
  indexes: EmbeddingIndexSummary[] | null;
  error: string | null;
  busyId: string | null;
  adding: boolean;
  providers: EmbeddingProvider[] | null;
  form: CreateEmbeddingIndexForm;
  creating: boolean;
}

type EmbeddingsPanelAction =
  | { type: 'load_success'; indexes: EmbeddingIndexSummary[] }
  | { type: 'set_error'; message: string | null }
  | { type: 'set_busy'; indexId: string | null }
  | { type: 'open_create' }
  | { type: 'close_create' }
  | { type: 'set_providers'; providers: EmbeddingProvider[] | null }
  | { type: 'set_form'; form: CreateEmbeddingIndexForm }
  | { type: 'select_model'; provider: string; model: string }
  | { type: 'toggle_source_column'; columnName: string; checked: boolean }
  | { type: 'set_allow_remote'; allowed: boolean }
  | { type: 'set_allow_auto_refresh'; allowed: boolean }
  | { type: 'set_max_cost'; value: string }
  | { type: 'set_confirm_remote'; confirmed: boolean }
  | { type: 'set_creating'; creating: boolean };

function createInitialState(sheet: SheetMeta): EmbeddingsPanelState {
  return {
    indexes: null,
    error: null,
    busyId: null,
    adding: false,
    providers: null,
    form: emptyCreateEmbeddingIndexForm(sheet.columns[0]?.name),
    creating: false,
  };
}

function updateForm(
  state: EmbeddingsPanelState,
  patch: Partial<CreateEmbeddingIndexForm>,
): EmbeddingsPanelState {
  return { ...state, form: { ...state.form, ...patch } };
}

function embeddingsPanelReducer(
  state: EmbeddingsPanelState,
  action: EmbeddingsPanelAction,
): EmbeddingsPanelState {
  switch (action.type) {
    case 'load_success':
      return { ...state, indexes: action.indexes, error: null };
    case 'set_error':
      return { ...state, error: action.message };
    case 'set_busy':
      return { ...state, busyId: action.indexId };
    case 'open_create':
      return { ...state, adding: true };
    case 'close_create':
      return { ...state, adding: false };
    case 'set_providers':
      return { ...state, providers: action.providers };
    case 'set_form':
      return { ...state, form: action.form };
    case 'select_model':
      return updateForm(state, {
        provider: action.provider,
        model: action.model,
        allowRemote: false,
        allowRemoteAutomaticRefresh: false,
        confirmRemote: false,
      });
    case 'toggle_source_column':
      return updateForm(state, {
        sourceColumns: action.checked
          ? [...state.form.sourceColumns, action.columnName]
          : state.form.sourceColumns.filter((name) => name !== action.columnName),
      });
    case 'set_allow_remote':
      return updateForm(state, {
        allowRemote: action.allowed,
        allowRemoteAutomaticRefresh:
          action.allowed && state.form.allowRemoteAutomaticRefresh,
      });
    case 'set_allow_auto_refresh':
      return updateForm(state, { allowRemoteAutomaticRefresh: action.allowed });
    case 'set_max_cost':
      return updateForm(state, { maxCost: action.value });
    case 'set_confirm_remote':
      return updateForm(state, { confirmRemote: action.confirmed });
    case 'set_creating':
      return { ...state, creating: action.creating };
    default:
      return state;
  }
}

function defaultSelection(
  cards: EmbeddingProvider[],
): Pick<CreateEmbeddingIndexForm, 'provider' | 'model'> {
  const selectable = cards.filter((card) => card.available && card.modalityCompatible);
  const pick = selectable.find((card) => card.recommended) ?? selectable[0] ?? null;
  return { provider: pick?.providerId ?? '', model: pick?.modelId ?? '' };
}

export function useEmbeddingsPanelController({
  apiPort,
  sheet,
}: UseEmbeddingsPanelControllerOptions) {
  const sheetId = Number(sheet.id);
  const [state, dispatch] = useReducer(
    embeddingsPanelReducer,
    sheet,
    createInitialState,
  );
  // Project switches unmount the keyed Workspace, and sheet switches remount
  // this panel. Async work already sent to the server is allowed to finish,
  // but every continuation after an await must prove it still belongs to this
  // controller scope before it polls, reloads, launches follow-up work, or
  // publishes state. The dependency cleanup also protects a host that reuses
  // the component across an apiPort/sheet change instead of remounting it.
  const scopeGeneration = useRef(0);
  useEffect(() => () => {
    scopeGeneration.current += 1;
  }, [apiPort, sheetId]);
  const isCurrentScope = useCallback(
    (generation: number) => scopeGeneration.current === generation,
    [],
  );

  const load = useCallback(async () => {
    const generation = scopeGeneration.current;
    try {
      const indexes = await apiPort.embeddingIndexes(sheetId);
      if (!isCurrentScope(generation)) return;
      dispatch({ type: 'load_success', indexes });
    } catch (error) {
      if (!isCurrentScope(generation)) return;
      dispatch({
        type: 'set_error',
        message: error instanceof Error ? error.message : String(error),
      });
    }
  }, [apiPort, isCurrentScope, sheetId]);

  // The panel is always open now (no second collapse level in the Discover
  // tab host) — load on mount instead of waiting for a toggle.
  useEffect(() => {
    void load();
  }, [load]);

  const openCreate = useCallback(async () => {
    const generation = scopeGeneration.current;
    dispatch({ type: 'open_create' });
    let cards = state.providers;
    if (!cards) {
      try {
        cards = await apiPort.embeddingProviderCatalog({
          modality: EMBEDDING_TEXT_MODALITY,
        });
        if (!isCurrentScope(generation)) return;
        dispatch({ type: 'set_providers', providers: cards });
      } catch (error) {
        if (!isCurrentScope(generation)) return;
        dispatch({
          type: 'set_error',
          message: error instanceof Error ? error.message : String(error),
        });
        cards = null;
      }
    }
    if (!isCurrentScope(generation)) return;
    dispatch({
      type: 'set_form',
      form: {
        ...emptyCreateEmbeddingIndexForm(sheet.columns[0]?.name),
        ...(cards ? defaultSelection(cards) : {}),
      },
    });
  }, [apiPort, isCurrentScope, state.providers, sheet.columns]);

  const selectedCard = useMemo(
    () =>
      state.providers?.find(
        (provider) =>
          provider.providerId === state.form.provider &&
          provider.modelId === state.form.model,
      ) ??
      state.providers?.find((provider) => provider.providerId === state.form.provider) ??
      null,
    [state.providers, state.form.provider, state.form.model],
  );
  const remoteSelected = Boolean(selectedCard && !selectedCard.local);
  const autoRefreshOn =
    remoteSelected &&
    state.form.allowRemote &&
    state.form.allowRemoteAutomaticRefresh;
  const maxCostValue = Number(state.form.maxCost);
  const modelOk =
    state.form.model.trim().length > 0 &&
    (selectedCard == null ||
      (Boolean(selectedCard.available) && selectedCard.modalityCompatible));
  const createReady =
    state.form.sourceColumns.length > 0 &&
    Boolean(state.form.provider) &&
    modelOk &&
    (!remoteSelected || state.form.allowRemote) &&
    (!autoRefreshOn || (state.form.confirmRemote && maxCostValue > 0));

  // A create is metadata-only and instant (space + index rows, no provider
  // call — `_run_index_create`); the actual (possibly long) embedding work
  // is the refresh that follows. Both the manual "Refresh" button AND a
  // just-created index route through this one queue-aware path: launch
  // (embedding.index_refresh is a QUEUED_ACTION_JOB), poll the job to a
  // terminal status if one was minted, then reload the indexes list. This
  // controller is the only consumer of its own state — no cross-panel
  // broadcast needed. NOT done if this still blocks the caller or leaves
  // the list stale after completion.
  const runRefresh = useCallback(
    async (indexId: string, mode: 'incremental' | 'full') => {
      const generation = scopeGeneration.current;
      dispatch({ type: 'set_busy', indexId });
      try {
        const launch = await apiPort.refreshEmbeddingIndex(indexId, mode);
        if (!isCurrentScope(generation)) return;
        if (launch.jobId != null && !TERMINAL_JOB_STATUSES.has(launch.status)) {
          await pollEmbeddingJobUntilTerminal(apiPort, launch.jobId, {
            shouldContinue: () => isCurrentScope(generation),
          });
        }
        if (!isCurrentScope(generation)) return;
        await load();
      } catch (error) {
        if (!isCurrentScope(generation)) return;
        dispatch({
          type: 'set_error',
          message: error instanceof Error ? error.message : String(error),
        });
      } finally {
        if (isCurrentScope(generation)) {
          dispatch({ type: 'set_busy', indexId: null });
        }
      }
    },
    [apiPort, isCurrentScope, load],
  );

  const submitCreate = useCallback(
    async (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      if (!createReady || !state.form.provider) return;
      const generation = scopeGeneration.current;
      dispatch({ type: 'set_creating', creating: true });
      try {
        const indexId = await apiPort.createEmbeddingIndex({
          sheetId,
          sourceColumns: state.form.sourceColumns,
          modality: EMBEDDING_TEXT_MODALITY,
          provider: state.form.provider,
          model: state.form.model,
          allowRemote: remoteSelected ? state.form.allowRemote : false,
          allowRemoteAutomaticRefresh: autoRefreshOn,
          maxCostUsdPerRefresh: autoRefreshOn ? maxCostValue : null,
        });
        if (!isCurrentScope(generation)) return;
        dispatch({ type: 'close_create' });
        await load();
        if (!isCurrentScope(generation)) return;
        // Refresh on creation: do not await — the dialog closes immediately
        // and the build progresses in the background, visible in the jobs
        // panel via its own busy/status.
        void runRefresh(indexId, 'full');
      } catch (error) {
        if (!isCurrentScope(generation)) return;
        dispatch({
          type: 'set_error',
          message: error instanceof Error ? error.message : String(error),
        });
      } finally {
        if (isCurrentScope(generation)) {
          dispatch({ type: 'set_creating', creating: false });
        }
      }
    },
    [
      createReady,
      sheetId,
      state.form,
      remoteSelected,
      autoRefreshOn,
      maxCostValue,
      apiPort,
      isCurrentScope,
      load,
      runRefresh,
    ],
  );

  const refresh = runRefresh;

  const closeCreate = useCallback(() => {
    dispatch({ type: 'close_create' });
  }, []);

  const selectModel = useCallback((provider: string, model: string) => {
    dispatch({ type: 'select_model', provider, model });
  }, []);

  const toggleSourceColumn = useCallback((columnName: string, checked: boolean) => {
    dispatch({ type: 'toggle_source_column', columnName, checked });
  }, []);

  const setAllowRemote = useCallback((allowed: boolean) => {
    dispatch({ type: 'set_allow_remote', allowed });
  }, []);

  const setAllowAutoRefresh = useCallback((allowed: boolean) => {
    dispatch({ type: 'set_allow_auto_refresh', allowed });
  }, []);

  const setMaxCost = useCallback((value: string) => {
    dispatch({ type: 'set_max_cost', value });
  }, []);

  const setConfirmRemote = useCallback((confirmed: boolean) => {
    dispatch({ type: 'set_confirm_remote', confirmed });
  }, []);

  return {
    adding: state.adding,
    autoRefreshOn,
    busyId: state.busyId,
    closeCreate,
    createReady,
    creating: state.creating,
    error: state.error,
    form: state.form,
    indexes: state.indexes,
    load,
    openCreate,
    providers: state.providers,
    refresh,
    remoteSelected,
    selectedCard,
    selectModel,
    setAllowAutoRefresh,
    setAllowRemote,
    setConfirmRemote,
    setMaxCost,
    submitCreate,
    toggleSourceColumn,
  };
}
