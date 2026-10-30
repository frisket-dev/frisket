import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ActionLaunchPrefill } from '../actions/actionFormInitial';
import { createActionComparisonApi, type ActionComparisonContext, type ActionComparisonSource } from '../api/actionComparison';
import { createEvaluationApi } from '../api/evaluation';
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

export type ActionCompareRowScope = 'sample' | 'all' | 'expected';
const MAX_COMPARE_ROWS = 500;
const DEFAULT_SAMPLE_ROWS = 20;

interface ComparisonTarget {
  source: ActionComparisonSource;
  sheetId: string;
  selection: ActionCompareCapturedSelection | null;
  sampleRowIds?: number[];
  initialLens?: 'each_other' | 'expected';
  rowScope?: ActionCompareRowScope;
}

interface ActionCompareHistoryMenuTarget {
  runAnchor: ActionRunAnchor;
  trigger: HTMLButtonElement;
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
  const evaluationApi = useMemo(() => createEvaluationApi(actionPreviewRunErrorFromContract, projectId), [projectId]);
  const [rowScope, setRowScope] = useState<ActionCompareRowScope>('sample');
  const [rowScopeTotal, setRowScopeTotal] = useState<number | null>(null);
  const activeTarget = useRef<ComparisonTarget | null>(null);
  const [initialLens, setInitialLens] = useState<'each_other' | 'expected'>('each_other');
  const [session, setSession] = useState<ActionCompareSession | null>(null);
  const sessionRef = useRef<ActionCompareSession | null>(null);
  const [pendingTarget, setPendingTarget] = useState<ComparisonTarget | null>(null);
  const [historyMenuTarget, setHistoryMenuTarget] = useState<ActionCompareHistoryMenuTarget | null>(null);
  const historySeedAnchor = useRef<ActionRunAnchor | null>(null);
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
      activeTarget.current = null;
      setSession(null);
      void current?.dispose();
    }
  }, [tab.open]);

  const readContext = useCallback(async (target: ComparisonTarget) => {
    const scope = target.rowScope ?? (target.selection ? 'all' : target.initialLens === 'expected' ? 'expected' : 'sample');
    const options = scope !== 'expected' && target.sheetId === sheetId ? sampleOptions : {};
    const limit = scope === 'sample' ? DEFAULT_SAMPLE_ROWS : MAX_COMPARE_ROWS;
    let total: number | null = target.selection?.totalCount ?? null;
    let sample: number[];
    if (target.sampleRowIds) sample = target.sampleRowIds.slice(0, limit);
    else if (scope !== 'expected' && target.selection) sample = target.selection.rowIds.slice(0, limit);
    else {
      const data = await projectApi.getSheetData(target.sheetId, 0, scope === 'expected' ? 1 : limit, {
        ...options, columnIds: [], valueMode: 'bounded',
      });
      sample = data.rows.map(row => Number(row.id));
      total = data.total;
    }
    if (!sample.length) throw new Error('Choose a sheet with rows to compare.');
    let context = await api.getContext({ source: target.source, sample_row_ids: sample });
    if (!context.available) {
      throw new ActionComparisonUnavailableError(context.reason?.message ?? 'This action cannot be compared.');
    }
    if (scope === 'expected') {
      const { links } = await evaluationApi.getLinks(Number(target.sheetId));
      const link = links.find(item => item.action_id === context.draft.action_id
        && item.fields.some(field => context.output_columns.some(output => output.column_id === field.output_column_id)));
      if (!link) throw new Error('Join expected answers for this action first.');
      const expectedRows = await evaluationApi.getExpectedRows({ link_id: link.id, limit: MAX_COMPARE_ROWS });
      if (!expectedRows.row_ids.length) throw new Error('No rows have expected answers for this action.');
      total = expectedRows.total;
      context = await api.getContext({ source: target.source, sample_row_ids: expectedRows.row_ids });
      if (!context.available) throw new ActionComparisonUnavailableError(context.reason?.message ?? 'This action cannot be compared.');
    }
    return { context, total };
  }, [api, evaluationApi, projectApi, sampleOptions, sheetId]);

  const requestCostConfirmation = useMemo(
    () => createActionCompareCostConfirmation(jobs),
    [jobs],
  );

  const openTarget = useCallback(async (target: ComparisonTarget) => {
    const revision = operation.current.revision + 1;
    operation.current = { revision, targetPending: true };
    const previous = sessionRef.current;
    sessionRef.current = null;
    setSession(null);
    setPendingTarget(null);
    try {
      await previous?.dispose();
      if (operation.current.revision !== revision) return null;
      const { context, total } = await readContext(target);
      if (operation.current.revision !== revision) return null;
      const resolvedScope = target.rowScope ?? (target.selection ? 'all' : target.initialLens === 'expected' ? 'expected' : 'sample');
      const next = createActionCompareSession({ context, capturedSelection: resolvedScope === 'expected' ? null : target.selection }, {
        runVariant: createActionCompareRunner(api, requestCostConfirmation, evaluationApi),
        cancelPreview: id => api.cancelPreview(id), discardPreview: id => api.discardPreview(id),
        discardCaptured: id => evaluationApi.discardCapture(id),
      });
      setRowScopeTotal(total);
      setInitialLens(target.initialLens ?? 'each_other');
      setRowScope(resolvedScope);
      activeTarget.current = target;
      sessionRef.current = next;
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
  }, [api, evaluationApi, compareView, readContext, requestCostConfirmation, setOpenSplit, showError, workView]);

  const requestTarget = useCallback((target: ComparisonTarget) => {
    const existing = sessionRef.current?.getSnapshot();
    if (existing && hasScratchWork(existing)) setPendingTarget(target);
    else void openTarget(target);
  }, [openTarget]);

  const changeActionCompareRowScope = useCallback((scope: ActionCompareRowScope, lens?: 'each_other' | 'expected') => {
    const target = activeTarget.current;
    if (!target || scope === rowScope) return;
    requestTarget({ ...target, sampleRowIds: undefined, rowScope: scope, initialLens: lens ?? target.initialLens });
  }, [requestTarget, rowScope]);

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
  const compareActionRun = useCallback((anchor: ActionRunAnchor, expected = false) => {
    try {
      requestTarget({ ...targetForRun(anchor), initialLens: expected ? 'expected' : 'each_other' });
    } catch (error) {
      showError(error instanceof Error ? error.message : 'Could not capture the comparison selection.');
    }
  }, [requestTarget, showError, targetForRun]);

  const launchSettings = useCallback((context: ActionComparisonContext, draft: GeneratedActionDraft,
    selection: ActionCompareCapturedSelection | null, chosen?: ActionCompareUseSettings) => {
    historySeedAnchor.current = context.comparison_basis.kind === 'run' ? {
      sheetId: String(context.sheet.id), columnId: String(context.comparison_basis.column_id),
      runId: String(context.comparison_basis.run_id),
    } : null;
    runActionFromSurface(draft.action_id, undefined, { actionDraft: draft, actionCompare: {
      ...(chosen ? { kind: 'use-comparison-variant' as const, variantId: chosen.variantId, variantLabel: chosen.variantLabel }
        : { kind: 'edit-existing' as const }),
      context, capturedSelection: selection, sampleRowIds: context.sample_row_ids,
      returnToCompare: Boolean(sessionRef.current),
    } });
  }, [runActionFromSurface]);
  const restoreActionRunSettings = useCallback(async (anchor: ActionRunAnchor) => {
    const supersedesRetarget = operation.current.targetPending;
    const revision = operation.current.revision + 1;
    operation.current = { revision, targetPending: false };
    if (supersedesRetarget) compareView.close('action');
    try {
      const target = targetForRun(anchor);
      const { context } = await readContext(target);
      if (operation.current.revision !== revision) return;
      launchSettings(context, context.draft, target.selection);
    } catch (error) {
      if (operation.current.revision === revision) {
        showError(error instanceof Error ? error.message : 'Could not load these settings.');
      }
    }
  }, [compareView, launchSettings, readContext, showError, targetForRun]);
  const useActionCompareSettings = useCallback((chosen: ActionCompareUseSettings) => {
    if (operation.current.targetPending || !sessionRef.current) return;
    launchSettings(chosen.context, chosen.draft, chosen.capturedSelection, chosen);
  }, [launchSettings]);

  const loadActionRunEntry = useCallback(async (anchor: ActionRunAnchor): Promise<ActionRunEntryDetails | null> => {
    const history = await projectApi.getColumnRuns(anchor.columnId, 0, 20);
    const run = [history.latestRun, history.currentRun, ...history.runs].find(run => run?.runId === anchor.runId);
    if (!run) return null;
    const entry = entries?.find(item => item.kind === run.actionKind);
    const comparisonKind = actionComparisonKind(entry);
    let context: ActionComparisonContext | null = null;
    if (comparisonKind) {
      try {
        context = (await readContext({ ...targetForRun(anchor), rowScope: 'sample' })).context;
      } catch (error) {
        if (!(error instanceof ActionComparisonUnavailableError)) throw error;
        // Keep run metadata usable, but do not advertise a comparison that
        // canonical context resolution refused.
      }
    }
    return { actionTitle: run.actionName, comparisonKind: context ? comparisonKind : null,
      sourceLabels: context?.source_columns.map(column => column.name) ?? [],
      outputLabels: context?.output_columns.map(column => column.current_name) ?? [history.columnName], modelLabel: run.model,
      rowCount: run.totalRows, ranAt: run.startedAt, errorCount: run.failedRows,
      outcomeLabel: run.status };
  }, [entries, projectApi, readContext, targetForRun]);

  const openActionCompareHistory = useCallback((trigger: HTMLButtonElement) => {
    const seededRun = historySeedAnchor.current;
    if (seededRun) {
      setHistoryMenuTarget((current) => current?.trigger === trigger
        ? null
        : { runAnchor: seededRun, trigger });
      return;
    }
    const basis = sessionRef.current?.getSnapshot().context.comparison_basis;
    const context = sessionRef.current?.getSnapshot().context;
    if (basis?.kind === 'run' && context) {
      const runAnchor = { sheetId: String(context.sheet.id),
        runId: String(basis.run_id), columnId: String(basis.column_id) };
      setHistoryMenuTarget((current) => current?.trigger === trigger
        ? null
        : { runAnchor, trigger });
    }
  }, []);
  const closeActionCompareHistory = useCallback(() => setHistoryMenuTarget(null), []);

  const closeActionCompare = useCallback(() => {
    operation.current = { revision: operation.current.revision + 1, targetPending: false };
    const current = sessionRef.current;
    sessionRef.current = null; activeTarget.current = null; setSession(null); setPendingTarget(null);
    compareView.close('action');
    void current?.dispose();
  }, [compareView]);
  const returnToActionCompare = useCallback(() => {
    closeActionRoute();
    if (!tab.open || !sessionRef.current) return;
    compareView.focus('action');
  }, [closeActionRoute, compareView, tab.open]);
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
    rowScopePending: pendingTarget !== null,
    actionCompareHistoryMenuTarget: historyMenuTarget,
    openActionCompare, compareActionRun, restoreActionRunSettings, loadActionRunEntry, useActionCompareSettings, initialLens, rowScope, rowScopeTotal, changeActionCompareRowScope,
    openActionCompareHistory, closeActionCompareHistory, returnToActionCompare, closeActionCompare,
    cancelActionCompareClose, cancelActionCompareRetarget, confirmActionCompareRetarget,
  }), [cancelActionCompareClose, cancelActionCompareRetarget, closeActionCompare, compareActionRun,
    closeActionCompareHistory, confirmActionCompareRetarget, historyMenuTarget, loadActionRunEntry, openActionCompare,
    openActionCompareHistory, pendingTarget, returnToActionCompare, session, tab.active,
    initialLens, rowScope, rowScopeTotal, changeActionCompareRowScope, restoreActionRunSettings, tab.closeWarn, tab.open, useActionCompareSettings]);
}
