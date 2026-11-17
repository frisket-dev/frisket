import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
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
import { errorMessage } from '../../hooks/errorMessage';

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

export function useEmbeddingsPanelController({
  apiPort,
  sheet,
}: UseEmbeddingsPanelControllerOptions) {
  const sheetId = Number(sheet.id);
  const [indexes, setIndexes] = useState<EmbeddingIndexSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [providers, setProviders] = useState<EmbeddingProvider[] | null>(null);
  const [form, setForm] = useState<CreateEmbeddingIndexForm>(
    () => emptyCreateEmbeddingIndexForm(sheet.columns[0]?.name),
  );
  const [creating, setCreating] = useState(false);
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
      const loaded = await apiPort.embeddingIndexes(sheetId);
      if (!isCurrentScope(generation)) return;
      setIndexes(loaded);
      setError(null);
    } catch (caught) {
      if (!isCurrentScope(generation)) return;
      setError(errorMessage(caught));
    }
  }, [apiPort, isCurrentScope, sheetId]);

  // The panel is always open now (no second collapse level in the Discover
  // tab host) — load on mount instead of waiting for a toggle.
  // Started from a microtask so the effect body never reaches a state setter.
  useEffect(() => {
    queueMicrotask(() => void load());
  }, [load]);

  const openCreate = useCallback(async () => {
    const generation = scopeGeneration.current;
    setAdding(true);
    let cards = providers;
    if (!cards) {
      try {
        cards = await apiPort.embeddingProviderCatalog({
          modality: EMBEDDING_TEXT_MODALITY,
        });
        if (!isCurrentScope(generation)) return;
        setProviders(cards);
      } catch (caught) {
        if (!isCurrentScope(generation)) return;
        setError(errorMessage(caught));
      }
    }
    if (!isCurrentScope(generation)) return;
    setForm(emptyCreateEmbeddingIndexForm(sheet.columns[0]?.name));
  }, [apiPort, isCurrentScope, providers, sheet.columns]);

  const selectedCard = useMemo(
    () =>
      providers?.find(
        (provider) =>
          provider.providerId === form.provider &&
          provider.modelId === form.model,
      ) ?? null,
    [providers, form.provider, form.model],
  );
  const providerCard = useMemo(
    () => providers?.find((provider) => provider.providerId === form.provider) ?? null,
    [providers, form.provider],
  );
  const remoteSelected = Boolean(providerCard && !providerCard.local);
  const autoRefreshOn =
    remoteSelected &&
    form.allowRemote &&
    form.allowRemoteAutomaticRefresh;
  const maxCostValue = Number(form.maxCost);
  const modelOk =
    form.model.trim().length > 0 &&
    (selectedCard?.modalityCompatible ?? providerCard?.modalityCompatible ?? true);
  const createReady =
    form.sourceColumns.length > 0 &&
    Boolean(form.provider) &&
    modelOk &&
    (!remoteSelected || form.allowRemote) &&
    (!autoRefreshOn || (form.confirmRemote && maxCostValue > 0));

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
      setBusyId(indexId);
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
      } catch (caught) {
        if (!isCurrentScope(generation)) return;
        setError(errorMessage(caught));
      } finally {
        if (isCurrentScope(generation)) {
          setBusyId(null);
        }
      }
    },
    [apiPort, isCurrentScope, load],
  );

  const submitCreate = useCallback(
    async (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      if (!createReady || !form.provider) return;
      const generation = scopeGeneration.current;
      setCreating(true);
      try {
        const indexId = await apiPort.createEmbeddingIndex({
          sheetId,
          sourceColumns: form.sourceColumns,
          modality: EMBEDDING_TEXT_MODALITY,
          provider: form.provider,
          model: form.model,
          allowRemote: remoteSelected ? form.allowRemote : false,
          allowRemoteAutomaticRefresh: autoRefreshOn,
          maxCostUsdPerRefresh: autoRefreshOn ? maxCostValue : null,
        });
        if (!isCurrentScope(generation)) return;
        setAdding(false);
        await load();
        if (!isCurrentScope(generation)) return;
        // Refresh on creation: do not await — the dialog closes immediately
        // and the build progresses in the background, visible in the jobs
        // panel via its own busy/status.
        void runRefresh(indexId, 'full');
      } catch (caught) {
        if (!isCurrentScope(generation)) return;
        setError(errorMessage(caught));
      } finally {
        if (isCurrentScope(generation)) {
          setCreating(false);
        }
      }
    },
    [
      createReady,
      sheetId,
      form,
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
    setAdding(false);
  }, []);

  const selectModel = useCallback((provider: string, model: string) => {
    setForm((current) => ({
      ...current,
      provider,
      model,
      allowRemote: false,
      allowRemoteAutomaticRefresh: false,
      confirmRemote: false,
    }));
  }, []);

  const toggleSourceColumn = useCallback((columnName: string, checked: boolean) => {
    setForm((current) => ({
      ...current,
      sourceColumns: checked
        ? [...current.sourceColumns, columnName]
        : current.sourceColumns.filter((name) => name !== columnName),
    }));
  }, []);

  const setAllowRemote = useCallback((allowed: boolean) => {
    setForm((current) => ({
      ...current,
      allowRemote: allowed,
      allowRemoteAutomaticRefresh: allowed && current.allowRemoteAutomaticRefresh,
    }));
  }, []);

  const setAllowAutoRefresh = useCallback((allowed: boolean) => {
    setForm((current) => ({ ...current, allowRemoteAutomaticRefresh: allowed }));
  }, []);

  const setMaxCost = useCallback((value: string) => {
    setForm((current) => ({ ...current, maxCost: value }));
  }, []);

  const setConfirmRemote = useCallback((confirmed: boolean) => {
    setForm((current) => ({ ...current, confirmRemote: confirmed }));
  }, []);

  return {
    adding,
    autoRefreshOn,
    busyId,
    closeCreate,
    createReady,
    creating,
    error,
    form,
    indexes,
    load,
    openCreate,
    providers,
    providerCard,
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
