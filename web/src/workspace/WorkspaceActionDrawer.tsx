import { useJobsHandle } from '../bind/useJobsHandle';
import { useCurrentViewSelection } from '../bind/useCurrentViewSelection';
import { lazy, memo, Suspense, useCallback, useEffect, useMemo } from 'react';
import { ActionDrawer } from '../workbench/ActionDrawer';
import { isRunActionBlockedStatus } from '../runStatusModel';
import { ActionWorkbenchFormFrame } from '../workbench/contributions';
import { useGridViewHandle } from '../bind/useGridViewHandle';
import { useRouteHandle } from '../bind/useRouteHandle';
import { useChromeHandle } from '../bind/useChromeHandle';
import { useDetailHandle } from '../bind/useDetailHandle';
import { useSelector } from '../bind/useSelector';
import { useRenderCount, useWorkspaceShell, useRun, useSelectedRowIdsForSheet, useCurrentSheet } from './WorkspaceContext';
import { useWorkspaceNavigationTelemetry } from '../telemetry/navigationTelemetry';

const LazyActionPanel = lazy(() => (
  import('../components/ActionPanel').then(({ ActionPanel }) => ({ default: ActionPanel }))
));

export const WorkspaceActionDrawerRegion = memo(function WorkspaceActionDrawerRegion() {
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
  const hasExactRowScopeInitializer = selectedRowIdsForSheet.length > 0 || viewSelection !== null;
  const rowScopeInitializerKey = selectedRowIdsForSheet.length > 0 ? 'selection' : viewSelection ? `view:${JSON.stringify(viewSelection)}` : 'all';

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
          viewSelection={viewSelection}
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
          onReturnToActionCompare={returnToActionCompare}
        />
      </Suspense>
    </ActionDrawer>
  );
});
