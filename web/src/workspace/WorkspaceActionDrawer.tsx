import { lazy, memo, Suspense, useCallback, useEffect, useMemo, useState } from 'react';
import { MAX_LENS_VIEW_ROWS } from '../api/open';
import { PanelLoading } from '../components/PanelPrimitives';
import { ActionDrawer } from '../workbench/ActionDrawer';
import { isRunActionBlockedStatus } from '../runStatusModel';
import { ActionWorkbenchFormFrame } from '../workbench/contributions';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { useGridViewHandle } from '../bind/useGridViewHandle';
import { useRouteHandle } from '../bind/useRouteHandle';
import { useChromeHandle } from '../bind/useChromeHandle';
import { useDetailHandle } from '../bind/useDetailHandle';
import { useLensViewHandle } from '../bind/useLensViewHandle';
import { useSelector } from '../bind/useSelector';
import { useRenderCount, useWorkspaceShell, useRun, useSelectedRowIdsForSheet, useCurrentSheet } from './WorkspaceContext';

const LazyActionPanel = lazy(() => (
  import('../components/ActionPanel').then(({ ActionPanel }) => ({ default: ActionPanel }))
));

export const WorkspaceActionDrawerRegion = memo(function WorkspaceActionDrawerRegion() {
  const { projectApi } = useWorkspaceStores();
  useRenderCount('actionDrawer');
  const {
    actionPanelVisible,
    closeActionRoute,
    actionLaunchInitial,
    actionDraftLaunchId,
    runActionFromSurface,
    refreshSheets,
    runActionBackfill,
    runWithPreview,
    executeRegisteredAction,
    openOcrCompareTab,
    openActionCompare,
    openActionCompareHistory,
    returnToActionCompare,

    project,
  } = useWorkspaceShell();
  const run = useRun();
  const selectedRowIdsForSheet = useSelectedRowIdsForSheet();
  const { sheet } = useCurrentSheet();
  const sheetId = sheet?.id ?? null;
  const lensView = useLensViewHandle();
  const activeLensView = useSelector(lensView.store, (state) => state.lensView);
  const activeLensOnSheet = activeLensView?.sheetId === sheetId
    ? activeLensView
    : null;
  const gridView = useGridViewHandle();
  const activeGridFilter = useSelector(gridView.store, (state) => state.applied.filter);
  const scopeRowIds = useSelector(gridView.store, (state) => state.applied.scopeRowIds ?? null);
  const chrome = useChromeHandle();
  // Action-panel state must not reach localStorage.

  const hideActionPanel = chrome.hideActionPanel;
  const detail = useDetailHandle();
  const childFilter = useSelector(detail.store, (state) => state.childFilter);
  const activeChildFilter = childFilter?.sheetId === sheetId ? childFilter : null;
  const proposalInspect = useSelector(detail.store, (s) => s.proposalInspect);
  const activeDetailRowId = useSelector(detail.store, (s) => (
    actionPanelVisible ? s.rowDrawer?.id ?? null : null
  ));
  const setProposalInspect = detail.setProposalInspect;
  const route = useRouteHandle();
  const routeActionKind = useSelector(route.store, (s) => s.actionKind);
  const hasActionTarget = Boolean(routeActionKind || proposalInspect);
  const actionViewScopeKey = useMemo(() => (
    sheetId !== null && (activeGridFilter || activeChildFilter || scopeRowIds !== null)
      ? JSON.stringify([
          sheetId,
          activeChildFilter?.parentRowId ?? null,
          activeGridFilter ?? null,
          scopeRowIds,
        ])
      : null
  ), [activeChildFilter, activeGridFilter, sheetId, scopeRowIds]);
  const [actionViewScope, setActionViewScope] = useState<{
    key: string;
    status: 'loading' | 'ready' | 'error';
    rowIds: string[];
    error?: string;
  } | null>(null);
  const actionLensScopeKey = useMemo(() => (
    activeLensOnSheet
      ? JSON.stringify([
          activeLensOnSheet.lensId,
          activeLensOnSheet.sheetId,
          activeLensOnSheet.total,
          activeLensOnSheet.rowIds,
        ])
      : null
  ), [activeLensOnSheet]);
  const [actionLensScope, setActionLensScope] = useState<{
    key: string;
    status: 'loading' | 'ready' | 'error';
    rowIds: string[];
    queryHash?: string;
    error?: string;
  } | null>(null);
  const shouldResolveViewScope = Boolean(
    actionPanelVisible && hasActionTarget && sheetId !== null
      && actionViewScopeKey && actionLensScopeKey === null && selectedRowIdsForSheet.length === 0
  );
  const shouldResolveLensScope = Boolean(
    actionPanelVisible && hasActionTarget && sheetId !== null
      && actionLensScopeKey && selectedRowIdsForSheet.length === 0
  );

  useEffect(() => {
    if (shouldResolveViewScope || actionViewScope === null) return;
    let cancelled = false;
    queueMicrotask(() => {
      if (!cancelled) setActionViewScope(null);
    });
    return () => { cancelled = true; };
  }, [shouldResolveViewScope, actionViewScope]);

  useEffect(() => {
    if (shouldResolveLensScope || actionLensScope === null) return;
    let cancelled = false;
    queueMicrotask(() => {
      if (!cancelled) setActionLensScope(null);
    });
    return () => { cancelled = true; };
  }, [shouldResolveLensScope, actionLensScope]);

  const closeDrawer = useCallback(() => {
    hideActionPanel();
    setProposalInspect(null);
    closeActionRoute();
    // Return focus to the grid after closing its overlay.
    requestAnimationFrame(() => {
      const grid = document.querySelector<HTMLElement>('[data-testid="grid"]');
      grid?.focus();
    });
  }, [hideActionPanel, setProposalInspect, closeActionRoute]);

  // The server remains authoritative when metadata is stale.

  useEffect(() => {
    if (actionPanelVisible) void refreshSheets();
  }, [actionPanelVisible, refreshSheets]);

  useEffect(() => {
    if (!shouldResolveViewScope || actionViewScopeKey === null) return;
    let cancelled = false;
    queueMicrotask(() => {
      if (cancelled) return;
      setActionViewScope({
        key: actionViewScopeKey,
        status: 'loading',
        rowIds: [],
      });
    });
    void (async () => {
      try {
        const [scopeSheetId, parentRowId, filter, scopeRowIds] = JSON.parse(
          actionViewScopeKey,
        ) as [string, string | null, typeof activeGridFilter, number[] | null];
        const rowIds: string[] = [];
        const pageSize = 1000;
        let expectedTotal: number | null = null;
        for (let offset = 0; ; ) {
          const page = await projectApi.getSheetData(scopeSheetId, offset, pageSize, {
            columnIds: [], valueMode: 'bounded',
            parentRowId,
            filter,
            scopeRowIds,
          });
          if (cancelled) return;
          if (expectedTotal === null) expectedTotal = page.total;
          if (page.total !== expectedTotal) {
            throw new Error('The current view changed while its complete row scope was loading.');
          }
          rowIds.push(...page.rows.map((row) => String(row.id)));
          offset += page.rows.length;
          if (offset >= page.total) break;
          if (page.rows.length === 0) {
            throw new Error('The current view could not load its complete row scope.');
          }
        }
        if (new Set(rowIds).size !== rowIds.length) {
          throw new Error('The current view returned duplicate rows.');
        }
        setActionViewScope({
          key: actionViewScopeKey,
          status: 'ready',
          rowIds,
        });
      } catch (error) {
        if (cancelled) return;
        setActionViewScope({
          key: actionViewScopeKey,
          status: 'error',
          rowIds: [],
          error: error instanceof Error ? error.message : String(error),
        });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [shouldResolveViewScope, actionViewScopeKey, projectApi]);

  useEffect(() => {
    if (!shouldResolveLensScope || actionLensScopeKey === null) return;
    let cancelled = false;
    queueMicrotask(() => {
      if (cancelled) return;
      setActionLensScope({
        key: actionLensScopeKey,
        status: 'loading',
        rowIds: [],
      });
    });
    void (async () => {
      try {
        const [lensId, lensSheetId, lensTotal, lensWindowRowIds] = JSON.parse(
          actionLensScopeKey,
        ) as [number, string, number, number[]];
        const first = await projectApi.resolveLens(lensId, {
          limit: MAX_LENS_VIEW_ROWS,
          offset: 0,
        });
        if (cancelled) return;
        const sameWindow = (
          first.rowIds.length === lensWindowRowIds.length
          && first.rowIds.every((rowId, index) => rowId === lensWindowRowIds[index])
        );
        if (
          first.lensId !== lensId
          || first.sheetId !== Number(lensSheetId)
          || first.offset !== 0
          || first.total !== lensTotal
          || !sameWindow
          || !first.queryHash
        ) {
          throw new Error('The lens changed after it was opened. Reopen it before running an action.');
        }
        const rowIds = [...first.rowIds];
        for (let offset = rowIds.length; offset < first.total; ) {
          const page = await projectApi.resolveLens(lensId, {
            limit: MAX_LENS_VIEW_ROWS,
            offset,
          });
          if (cancelled) return;
          if (
            page.lensId !== first.lensId
            || page.sheetId !== first.sheetId
            || page.queryHash !== first.queryHash
            || page.total !== first.total
            || page.offset !== offset
            || page.rowIds.length === 0
          ) {
            throw new Error('The lens changed while its complete row scope was loading.');
          }
          rowIds.push(...page.rowIds);
          offset += page.rowIds.length;
        }
        if (rowIds.length !== first.total || new Set(rowIds).size !== rowIds.length) {
          throw new Error('The lens returned an invalid complete row scope.');
        }
        setActionLensScope({
          key: actionLensScopeKey,
          status: 'ready',
          rowIds: rowIds.map(String),
          queryHash: first.queryHash,
        });
      } catch (error) {
        if (cancelled) return;
        setActionLensScope({
          key: actionLensScopeKey,
          status: 'error',
          rowIds: [],
          error: error instanceof Error ? error.message : String(error),
        });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [shouldResolveLensScope, actionLensScopeKey, projectApi]);

  if (!actionPanelVisible) return null;
  if (!routeActionKind && !proposalInspect) return null;
  const activeActionViewScope = actionViewScope?.key === actionViewScopeKey
    ? actionViewScope
    : null;
  const activeActionLensScope = actionLensScope?.key === actionLensScopeKey
    ? actionLensScope
    : null;
  if (activeLensOnSheet && selectedRowIdsForSheet.length === 0) {
    if (!activeActionLensScope || activeActionLensScope.status === 'loading') {
      return (
        <ActionDrawer onClose={closeDrawer}>
          <PanelLoading testId="action-lens-scope-loading" label="Loading complete lens…" />
        </ActionDrawer>
      );
    }
    if (activeActionLensScope.status === 'error') {
      return (
        <ActionDrawer onClose={closeDrawer}>
          <div className="action-form action-catalog-error" role="alert">
            {activeActionLensScope.error ?? 'The complete lens could not be loaded.'}
          </div>
        </ActionDrawer>
      );
    }
  }
  if (actionViewScopeKey && !activeLensOnSheet && selectedRowIdsForSheet.length === 0) {
    if (!activeActionViewScope || activeActionViewScope.status === 'loading') {
      return (
        <ActionDrawer onClose={closeDrawer}>
          <PanelLoading testId="action-view-scope-loading" label="Loading complete view…" />
        </ActionDrawer>
      );
    }
    if (activeActionViewScope.status === 'error') {
      return (
        <ActionDrawer onClose={closeDrawer}>
          <div className="action-form action-catalog-error" role="alert">
            {activeActionViewScope.error ?? 'The current view could not be loaded.'}
          </div>
        </ActionDrawer>
      );
    }
  }
  const actionScopeRowIds = selectedRowIdsForSheet.length > 0
    ? selectedRowIdsForSheet
    : activeLensOnSheet
      ? activeActionLensScope?.rowIds ?? []
      : activeActionViewScope?.status === 'ready'
        ? activeActionViewScope.rowIds
        : selectedRowIdsForSheet;
  const hasExactRowScopeInitializer = (
    selectedRowIdsForSheet.length > 0
    || activeActionLensScope?.status === 'ready'
    || activeActionViewScope?.status === 'ready'
  );
  const rowScopeInitializerKey = selectedRowIdsForSheet.length > 0
    ? 'selection'
    : activeLensOnSheet && activeActionLensScope?.status === 'ready'
      ? `lens:${activeLensOnSheet.lensId}:${activeActionLensScope.queryHash}`
      : activeActionViewScope?.status === 'ready'
        ? `view:${activeActionViewScope.key}`
        : 'all';

  return (
    <ActionDrawer onClose={closeDrawer}>
      <Suspense
        fallback={
          <div className="action-drawer-form-host">
            <div className="panel-header">Loading action…</div>
          </div>
        }
      >
        <LazyActionPanel
          sheet={sheet ?? null}
          project={project}
          running={isRunActionBlockedStatus(run?.status)}
          runningLabel={run?.status === 'queued' ? 'Queued…' : 'Running…'}
          selectedRowIds={actionScopeRowIds}
          hasExactRowScopeInitializer={hasExactRowScopeInitializer}
          rowScopeInitializerKey={rowScopeInitializerKey}
          activeRowId={activeDetailRowId ?? undefined}
          inspectProposal={proposalInspect}
          routeActionKind={routeActionKind ?? undefined}
          routeActionInitial={actionLaunchInitial ?? undefined}
          routeActionLaunchId={actionDraftLaunchId}
          onNavigateToAction={runActionFromSurface}
          onClose={closeDrawer}
          onBackfill={runActionBackfill}
          actionFormFrame={ActionWorkbenchFormFrame}
          onActionRouteClose={closeDrawer}
          onRun={runWithPreview}
          onExecuteRegisteredAction={executeRegisteredAction}
          onOpenOcrCompare={openOcrCompareTab}
          onCompareAction={openActionCompare}
          onOpenActionHistory={openActionCompareHistory}
          onReturnToActionCompare={returnToActionCompare}
        />
      </Suspense>
    </ActionDrawer>
  );
});
