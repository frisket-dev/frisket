import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ActionLaunchPrefill } from '../actions/actionFormInitial';
import { createActionComparisonApi, type ActionComparisonContext, type ActionComparisonSource } from '../api/actionComparison';
import { actionPreviewRunErrorFromContract } from '../api/contractErrors';
import type { GeneratedActionDraft, RunEstimate, SheetDataOptions } from '../api/types';
import { useActionCatalogHandle } from '../bind/useActionCatalogHandle';
import { useCompareViewHandle } from '../bind/useCompareViewHandle';
import { useJobsHandle } from '../bind/useJobsHandle';
import { useSelector } from '../bind/useSelector';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { useWorkViewHandle } from '../bind/useWorkViewHandle';
import { actionComparisonKind } from '../grid/actionComparisonCatalog';
import type { ActionRunAnchor, ActionRunEntryDetails } from '../grid/actionRunEntry';
import type { JobStoreHandle } from '../state/jobStore';
import { openCompareTabTransition } from '../state/workspaceTransitions';
import { createActionCompareRunner, createActionCompareSession, type ActionCompareCapturedSelection,
  type ActionCompareSession, type ActionCompareSessionSnapshot, type ActionCompareUseSettings } from '../workbench/actionCompareSession';

function hasScratchWork(state: ActionCompareSessionSnapshot) {
  return state.variants.some(variant => !variant.baseline || variant.previewId !== null || variant.status === 'running');
}

interface ComparisonTarget {
  source: ActionComparisonSource;
  sheetId: string;
  selection: ActionCompareCapturedSelection | null;
  sampleRowIds?: number[];
}

class ActionComparisonUnavailableError extends Error {}

/** Bridges comparison-session cancellation to the shared cost gate without
 * letting a late abort cancel a newer request that displaced this one. */
export function createActionCompareCostConfirmation(
  jobs: Pick<JobStoreHandle, 'cancelCostGate' | 'requestCostConfirmation' | 'store'>,
) {
  return async (estimate: RunEstimate, message: string, signal?: AbortSignal) => {
    if (signal?.aborted) return false;
    const confirmation = jobs.requestCostConfirmation(estimate, message);
    const ownedGate = jobs.store.get().costGate;
    if (!signal || !ownedGate) return confirmation;
    const cancelOwnedGate = () => {
      if (jobs.store.get().costGate === ownedGate) jobs.cancelCostGate();
    };
    signal.addEventListener('abort', cancelOwnedGate, { once: true });
    try {
      return await confirmation;
    } finally {
      signal.removeEventListener('abort', cancelOwnedGate);
    }
  };
}

/** Workspace lifetime, navigation and entry points. Preview execution and draft
 * revision fencing live in the comparison session, not in the React host. */
export function useWorkspaceActionCompareController({ projectId, sheetId, selectedRowIds, selectedRowCount,
  sampleOptions, runActionFromSurface, closeActionRoute, setOpenSplit, showError,
}: {
  projectId: string;
  sheetId?: string;
  selectedRowIds: readonly string[];
  /** Total selected membership when it may exceed the currently loaded IDs. */
  selectedRowCount?: number;
  sampleOptions: SheetDataOptions;
  runActionFromSurface(action: string, source?: string, initial?: ActionLaunchPrefill): void;
  closeActionRoute(): void;
  setOpenSplit(value: null): void;
  showError(message: string): void;
}) {
  const { projectApi } = useWorkspaceStores();
  const compareView = useCompareViewHandle();
  const workView = useWorkViewHandle();
  const jobs = useJobsHandle();
  const catalog = useActionCatalogHandle();
  const entries = useSelector(catalog.store, state => state.catalog?.actions);
  const tab = useSelector(compareView.store, state => state.action);
  const api = useMemo(() => createActionComparisonApi(actionPreviewRunErrorFromContract, projectId), [projectId]);
  const [session, setSession] = useState<ActionCompareSession | null>(null);
  const sessionRef = useRef<ActionCompareSession | null>(null);
  const [pendingTarget, setPendingTarget] = useState<ComparisonTarget | null>(null);
  const [historyAnchor, setHistoryAnchor] = useState<ActionRunAnchor | null>(null);
  const latestTarget = useRef<ComparisonTarget | null>(null);
  const editAnchor = useRef<ActionRunAnchor | null>(null);
  const operation = useRef({ revision: 0, targetPending: false });
  const captureSelection = useCallback((targetSheet: string) => {
    if (targetSheet !== sheetId) return null;
    const totalCount = selectedRowCount ?? selectedRowIds.length;
    if (totalCount === 0) return null;
    if (totalCount !== selectedRowIds.length) {
      throw new Error('Some selected rows are not loaded; choose a smaller selection.');
    }
    return { rowIds: selectedRowIds.map(Number), totalCount };
  }, [selectedRowCount, sheetId, selectedRowIds]);

  useEffect(() => () => {
    operation.current = { revision: operation.current.revision + 1, targetPending: false };
    const current = sessionRef.current;
    sessionRef.current = null;
    void current?.dispose();
  }, []);
  useEffect(() => {
    if (!session) return;
    const publish = () => {
      const state = session.getSnapshot();
      compareView.setSession('action', { label: 'Compare', hasData: hasScratchWork(state), verdict: '' });
    };
    publish();
    return session.subscribe(publish);
  }, [session, compareView]);
  useEffect(() => {
    if (!tab.open && (sessionRef.current || operation.current.targetPending)) {
      operation.current = { revision: operation.current.revision + 1, targetPending: false };
      const current = sessionRef.current;
      sessionRef.current = null;
      setSession(null);
      void current?.dispose();
    }
  }, [tab.open]);

  const readContext = useCallback(async (target: ComparisonTarget) => {
    const sample = target.sampleRowIds ?? target.selection?.rowIds.slice(0, 20)
      ?? (await projectApi.getSheetData(target.sheetId, 0, 20, {
        ...(target.sheetId === sheetId ? sampleOptions : {}), columnIds: [], valueMode: 'bounded',
      })).rows.map(row => Number(row.id));
    if (!sample.length) throw new Error('Choose a sheet with rows to compare.');
    const context = await api.getContext({ source: target.source, sample_row_ids: sample });
    if (!context.available) {
      throw new ActionComparisonUnavailableError(
        context.reason?.message ?? 'This action cannot be compared.',
      );
    }
    return context;
  }, [api, projectApi, sampleOptions, sheetId]);

  const requestCostConfirmation = useMemo(
    () => createActionCompareCostConfirmation(jobs),
    [jobs],
  );

  const openTarget = useCallback(async (target: ComparisonTarget) => {
    const revision = operation.current.revision + 1;
    operation.current = { revision, targetPending: true };
    const previous = sessionRef.current;
    sessionRef.current = null;
    latestTarget.current = null;
    setSession(null);
    setPendingTarget(null);
    try {
      await previous?.dispose();
      if (operation.current.revision !== revision) return null;
      const context = await readContext(target);
      if (operation.current.revision !== revision) return null;
      const next = createActionCompareSession({ context, capturedSelection: target.selection }, {
        runVariant: createActionCompareRunner(api, requestCostConfirmation),
        cancelPreview: id => api.cancelPreview(id), discardPreview: id => api.discardPreview(id),
      });
      sessionRef.current = next; latestTarget.current = { ...target, sampleRowIds: context.sample_row_ids };
      setSession(next); setPendingTarget(null);
      openCompareTabTransition({ setOpenSplit }, workView, compareView, 'action');
      return next;
    } catch (error) {
      if (operation.current.revision === revision) {
        compareView.close('action');
        showError(error instanceof Error ? error.message : 'Could not open comparison.');
      }
      return null;
    } finally {
      if (operation.current.revision === revision) {
        operation.current = { revision, targetPending: false };
      }
    }
  }, [api, compareView, readContext, requestCostConfirmation, setOpenSplit, showError, workView]);

  const requestTarget = useCallback((target: ComparisonTarget) => {
    const existing = sessionRef.current?.getSnapshot();
    if (existing && hasScratchWork(existing)) setPendingTarget(target);
    else void openTarget(target);
  }, [openTarget]);

  const openActionCompare = useCallback((draft: GeneratedActionDraft,
    capturedSelection?: ActionCompareCapturedSelection | null) => {
    if (draft.scope.kind !== 'sheet_rows') { showError('This action needs a sheet to compare.'); return; }
    try {
      const targetSheet = String(draft.scope.sheet_id);
      const selection = capturedSelection !== undefined
        ? capturedSelection
        : captureSelection(targetSheet);
      requestTarget({ source: { kind: 'draft', draft }, sheetId: targetSheet, selection });
    } catch (error) {
      showError(error instanceof Error ? error.message : 'Could not capture the comparison selection.');
    }
  }, [captureSelection, requestTarget, showError]);

  const targetForRun = useCallback((anchor: ActionRunAnchor): ComparisonTarget => ({
    source: { kind: 'run', run_id: Number(anchor.runId), column_id: Number(anchor.columnId) },
    sheetId: anchor.sheetId, selection: captureSelection(anchor.sheetId),
  }), [captureSelection]);
  const compareActionRun = useCallback((anchor: ActionRunAnchor) => {
    try {
      requestTarget(targetForRun(anchor));
    } catch (error) {
      showError(error instanceof Error ? error.message : 'Could not capture the comparison selection.');
    }
  }, [requestTarget, showError, targetForRun]);

  const launchSettings = useCallback((context: ActionComparisonContext, draft: GeneratedActionDraft,
    selection: ActionCompareCapturedSelection | null, chosen?: ActionCompareUseSettings) => {
    editAnchor.current = context.comparison_basis.kind === 'run' ? {
      sheetId: String(context.sheet.id), columnId: String(context.comparison_basis.column_id),
      runId: String(context.comparison_basis.run_id),
    } : null;
    runActionFromSurface(draft.action_id, undefined, { actionDraft: draft, actionCompare: {
      ...(chosen ? { kind: 'use-comparison-variant' as const, variantId: chosen.variantId, variantLabel: chosen.variantLabel }
        : { kind: 'edit-existing' as const }),
      context, capturedSelection: selection, sampleRowIds: context.sample_row_ids,
      returnToCompare: Boolean(sessionRef.current), comparisonStale: chosen?.comparisonStale ?? false,
    } });
  }, [runActionFromSurface]);
  const editActionRun = useCallback(async (anchor: ActionRunAnchor) => {
    const supersedesRetarget = operation.current.targetPending;
    const revision = operation.current.revision + 1;
    operation.current = { revision, targetPending: false };
    if (supersedesRetarget) compareView.close('action');
    try {
      const target = targetForRun(anchor);
      const context = await readContext(target);
      if (operation.current.revision !== revision) return;
      launchSettings(context, context.draft, target.selection);
    } catch (error) {
      if (operation.current.revision === revision) {
        showError(error instanceof Error ? error.message : 'Could not load these settings.');
      }
    }
  }, [compareView, launchSettings, readContext, showError, targetForRun]);
  const checkFreshness = useCallback(async () => {
    if (operation.current.targetPending) return null;
    const active = sessionRef.current;
    if (!active) return null;
    const activeRevision = operation.current.revision;
    const previous = active.getSnapshot().context;
    const current = await api.getContext({ source: previous.comparison_basis, sample_row_ids: previous.sample_row_ids });
    const stale = !current.available || current.sample_fingerprint !== previous.sample_fingerprint
      || current.expected_output_signature !== previous.expected_output_signature;
    if (operation.current.targetPending
      || operation.current.revision !== activeRevision || sessionRef.current !== active) return null;
    active.setStale(stale);
    return stale;
  }, [api]);
  const useActionCompareSettings = useCallback(async (chosen: ActionCompareUseSettings) => {
    try {
      const comparisonStale = await checkFreshness();
      if (comparisonStale === null) return;
      launchSettings(chosen.context, chosen.draft, chosen.capturedSelection, { ...chosen, comparisonStale });
    } catch (error) { showError(error instanceof Error ? error.message : 'Could not recheck comparison data.'); }
  }, [checkFreshness, launchSettings, showError]);

  const loadActionRunEntry = useCallback(async (anchor: ActionRunAnchor): Promise<ActionRunEntryDetails | null> => {
    const history = await projectApi.getColumnRuns(anchor.columnId, 0, 20);
    const run = [history.latestRun, history.currentRun, ...history.runs].find(run => run?.runId === anchor.runId);
    if (!run) return null;
    const entry = entries?.find(item => item.kind === run.actionKind);
    const comparisonKind = actionComparisonKind(entry);
    let context: ActionComparisonContext | null = null;
    if (comparisonKind) {
      try {
        context = await readContext(targetForRun(anchor));
      } catch (error) {
        if (!(error instanceof ActionComparisonUnavailableError)) throw error;
        // Keep the run metadata/Edit entry usable, but do not advertise a
        // comparison that canonical context resolution refused.
      }
    }
    return { actionTitle: run.actionName, comparisonKind: context ? comparisonKind : null,
      sourceLabels: context?.source_columns.map(column => column.name) ?? [],
      outputLabels: context?.output_columns.map(column => column.current_name) ?? [history.columnName], modelLabel: run.model,
      rowCount: run.totalRows, ranAt: run.startedAt, errorCount: run.failedRows,
      outcomeLabel: run.status };
  }, [entries, projectApi, readContext, targetForRun]);

  const openActionCompareHistory = useCallback(() => {
    if (editAnchor.current) { setHistoryAnchor(editAnchor.current); return; }
    const basis = sessionRef.current?.getSnapshot().context.comparison_basis;
    const context = sessionRef.current?.getSnapshot().context;
    if (basis?.kind === 'run' && context) setHistoryAnchor({ sheetId: String(context.sheet.id),
      runId: String(basis.run_id), columnId: String(basis.column_id) });
  }, []);

  const closeActionCompare = useCallback(() => {
    operation.current = { revision: operation.current.revision + 1, targetPending: false };
    const current = sessionRef.current;
    sessionRef.current = null; setSession(null); setPendingTarget(null);
    compareView.close('action');
    void current?.dispose();
  }, [compareView]);
  const returnToActionCompare = useCallback(() => {
    closeActionRoute();
    if (!tab.open || !sessionRef.current) return;
    compareView.focus('action');
    void checkFreshness().catch(error => showError(error instanceof Error ? error.message : 'Could not recheck comparison data.'));
  }, [checkFreshness, closeActionRoute, compareView, showError, tab.open]);
  const refreshActionCompare = useCallback(async () => {
    const target = latestTarget.current;
    const before = sessionRef.current?.getSnapshot();
    if (!target || !before) return;
    const next = await openTarget(target);
    if (!next || next.getSnapshot().context.expected_output_signature !== before.context.expected_output_signature) return;
    // Keep the user's alternative settings; their results were invalidated by Refresh.
    for (const variant of before.variants.filter(variant => !variant.baseline)) {
      const id = next.addVariant();
      if (id) next.updateVariant(id, variant.draft);
    }
  }, [openTarget]);
  const cancelActionCompareClose = useCallback(() => {
    compareView.setCloseWarn('action', false);
  }, [compareView]);
  const cancelActionCompareRetarget = useCallback(() => {
    setPendingTarget(null);
  }, []);
  const confirmActionCompareRetarget = useCallback(() => {
    if (pendingTarget) void openTarget(pendingTarget);
  }, [openTarget, pendingTarget]);

  return useMemo(() => ({ actionCompareSession: session, actionCompareOpen: tab.open, actionCompareActive: tab.active,
    actionCompareCloseWarn: tab.closeWarn, actionComparePendingTarget: pendingTarget,
    actionCompareHistoryAnchor: historyAnchor, setActionCompareHistoryAnchor: setHistoryAnchor,
    openActionCompare, compareActionRun, editActionRun, loadActionRunEntry, useActionCompareSettings,
    openActionCompareHistory, returnToActionCompare, closeActionCompare,
    cancelActionCompareClose, cancelActionCompareRetarget, confirmActionCompareRetarget,
    refreshActionCompare,
  }), [cancelActionCompareClose, cancelActionCompareRetarget, closeActionCompare, compareActionRun,
    confirmActionCompareRetarget, editActionRun, historyAnchor, loadActionRunEntry, openActionCompare,
    openActionCompareHistory, pendingTarget, refreshActionCompare, returnToActionCompare, session, tab.active,
    tab.closeWarn, tab.open, useActionCompareSettings]);
}
