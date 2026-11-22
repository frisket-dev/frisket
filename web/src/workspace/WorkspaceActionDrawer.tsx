import { useJobsHandle } from '../bind/useJobsHandle';
import { useCurrentViewSelection } from '../bind/useCurrentViewSelection';
import { lazy, memo, Suspense, useCallback, useEffect, useMemo } from 'react';
import { ActionDrawer } from '../workbench/ActionDrawer';
import { isRunActionBlockedStatus } from '../runStatusModel';
import { ActionWorkbenchFormFrame } from '../workbench/contributions';
import { useGridViewHandle } from '../bind/useGridViewHandle';
import { useViewsHandle } from '../bind/useViewsHandle';
import { useRouteHandle } from '../bind/useRouteHandle';
import { useChromeHandle } from '../bind/useChromeHandle';
import { useDetailHandle } from '../bind/useDetailHandle';
import { useSelector } from '../bind/useSelector';
import { useWorkspaceShell, useRun, useSelectedRowIdsForSheet, useCurrentSheet } from './WorkspaceContext';
import { sameViewSelection } from './viewDefinition';
import { useWorkspaceNavigationTelemetry } from '../telemetry/navigationTelemetry';

const LazyActionPanel = lazy(() => (
  import('../components/ActionPanel').then(({ ActionPanel }) => ({ default: ActionPanel }))
));

export const WorkspaceActionDrawerRegion = memo(function WorkspaceActionDrawerRegion() {
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
    returnToActionCompare,

    project,
  } = useWorkspaceShell();
  const run = useRun();
  const jobs = useJobsHandle();
  const launchPending = useSelector(jobs.store, (state) => state.launchPending);
  const selectedRowIdsForSheet = useSelectedRowIdsForSheet();
  const { sheet } = useCurrentSheet();
  const sheetId = sheet?.id ?? null;
  const gridView = useGridViewHandle();
  const viewsHandle = useViewsHandle();
  const activeViewId = useSelector(gridView.store, (state) => state.activeViewId);
  const views = useSelector(viewsHandle.store, (state) => state.views);
  const baseSelection = useSelector(gridView.store, (state) => state.applied.selection ?? null);
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
  const currentSelection = useCurrentViewSelection(sheet);
  const activeView = useMemo(() => {
    if (!sheet || selectedRowIdsForSheet.length > 0 || activeViewId === null || !currentSelection) {
      return null;
    }
    const candidate = views.find((item) => item.id === activeViewId);
    if (!candidate?.spec?.selection) return null;
    if (
      candidate.spec.selection.scope.kind !== 'sheet'
      || candidate.spec.selection.scope.sheet_id !== Number(sheet.id)
    ) {
      return null;
    }
    if (!sameViewSelection(candidate.spec.selection, currentSelection)) return null;
    return { id: candidate.id, name: candidate.name };
  }, [sheet, selectedRowIdsForSheet.length, activeViewId, currentSelection, views]);
  const viewSelection = useMemo(() => {
    if (!sheet || selectedRowIdsForSheet.length > 0) return null;
    if (!baseSelection && !activeGridFilter && !activeChildFilter && scopeRowIds === null) return null;
    return currentSelection;
  }, [sheet, selectedRowIdsForSheet.length, baseSelection, activeGridFilter, activeChildFilter, scopeRowIds, currentSelection]);

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

  useWorkspaceNavigationTelemetry({ projectId: project.id, rightPanels: actionPanelVisible && hasActionTarget ? ['action'] : [] });
  if (!actionPanelVisible) return null;
  if (!routeActionKind && !proposalInspect) return null;
  const hasExactRowScopeInitializer = (
    selectedRowIdsForSheet.length > 0 || activeView !== null || viewSelection !== null
  );

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
          launchPending={launchPending}
          runningLabel={launchPending ? 'Starting…' : run?.status === 'queued' ? 'Queued…' : 'Running…'}
          selectedRowIds={selectedRowIdsForSheet}
          viewId={activeView?.id ?? null}
          viewName={activeView?.name ?? null}
          viewSelection={viewSelection}
          hasExactRowScopeInitializer={hasExactRowScopeInitializer}
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
          onReturnToActionCompare={returnToActionCompare}
        />
      </Suspense>
    </ActionDrawer>
  );
});
