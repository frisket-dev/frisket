import './Workspace.css';
import { useMemo } from 'react';
import { PanelLoading } from '../components/PanelPrimitives';
import { useWorkspaceModel } from './useWorkspaceModel';
import { type WorkspaceProps, type WorkspaceViewModel, type WorkspaceShellHandle, type WorkspaceActHandle, WorkspaceShellContext, WorkspaceActContext, WorkspacePluginDetailRenderContext, WorkspaceRowWalkContext, WorkspaceLayoutContext, WorkspaceHostContextContext, WorkspaceMainViewModelContext, WorkspaceOverlayModelContext } from './WorkspaceContext';
import { WorkspaceChromeBarRegion, WorkspaceActRegion, WorkspaceAskRegion } from './WorkspaceChrome';
import { WorkspaceNavigateRegion, WorkspaceMainViewRegion } from './WorkspaceMainView';
import { WorkspaceInspectDetailRegion, WorkspaceRightInspectorRegion } from './WorkspaceInspect';
import { WorkspaceDiscoverRegion } from './WorkspaceDiscovery';
import { WorkspaceBottomDockRegion } from './WorkspaceBottomDock';
import { WorkspaceActionDrawerRegion } from './WorkspaceActionDrawer';
import { WorkspaceOverlayRegion } from './WorkspaceOverlays';
import { useShellIdentity } from '../shellIdentity';
import { DiagnosticReportButton } from './DiagnosticReport';

export function Workspace(props: WorkspaceProps) {
  const { identityMode } = useShellIdentity();
  const model = useWorkspaceModel(props);
  if (!model.sheetsLoaded) {
    return <>
      <PanelLoading className="panel-loading-page" label="Loading project…" />
      {identityMode && <footer className="status-bar diagnostic-footer"><DiagnosticReportButton /></footer>}
    </>;
  }
  return <WorkspaceView model={model} />;
}

function WorkspaceView({ model }: { model: WorkspaceViewModel }) {
  const shell = useMemo<WorkspaceShellHandle>(
    () => ({
      askNavigation: model.askNavigation,
      project: model.project,
      projectApi: model.projectApi,
      runActCommand: model.runActCommand,
      revealPluginLauncher: model.revealPluginLauncher,
      onImported: model.onImported,
      showError: model.showError,
      setRibbonMode: model.setRibbonMode,
      setActiveRibbonTab: model.setActiveRibbonTab,
      onMainViewTabKeyDown: model.onMainViewTabKeyDown,
      closePreviewView: model.closePreviewView,
      activePromotedView: model.activePromotedView,
      openPromotedTab: model.openPromotedTab,
      closePromotedTab: model.closePromotedTab,
      selectSheetTab: model.selectSheetTab,
      selectSheet: model.selectSheet,
      refreshSheets: model.refreshSheets,
      handleCellEdit: model.handleCellEdit,
      closeRoutePanel: model.closeRoutePanel,
      openRowInDocumentView: model.openRowInDocumentView,
      applyLens: model.applyLens,
      leftSidebarPluginPanelDescriptors: model.leftSidebarPluginPanelDescriptors,
      openEvidenceViewer: model.openEvidenceViewer,
      openRowRef: model.openRowRef,
      openSourceHealthMainView: model.openSourceHealthMainView,
      refreshHistory: model.refreshHistory,
      invalidateProjectData: model.invalidateProjectData,
      setDiscoverOpen: model.setDiscoverOpen,
      setDiscoverTab: model.setDiscoverTab,
      openDiscover: model.openDiscover,
      rightInspectorPluginPanelDescriptors: model.rightInspectorPluginPanelDescriptors,
      actionPanelVisible: model.actionPanelVisible,
      closeActionRoute: model.closeActionRoute,
      actionLaunchInitial: model.actionLaunchInitial,
      actionDraftLaunchId: model.actionDraftLaunchId,
      runActionFromSurface: model.runActionFromSurface,
      runActionBackfill: model.runActionBackfill,
      resumeHaltedRun: model.resumeHaltedRun,
      runWithPreview: model.runWithPreview,
      executeRegisteredAction: model.executeRegisteredAction,
      openOcrCompareTab: model.openOcrCompareTab,
      doStepTo: model.doStepTo,
      loadHistoryPage: model.loadHistoryPage,
      pluginManagerOperations: model.pluginManagerOperations,
      selectBottomDockTab: model.selectBottomDockTab,
      bottomDockPluginPanelDescriptors: model.bottomDockPluginPanelDescriptors,
      hideContribution: model.hideContribution,
    }),
    [
      model.askNavigation,
      model.project,
      model.projectApi,
      model.runActCommand,
      model.revealPluginLauncher,
      model.onImported,
      model.showError,
      model.setRibbonMode,
      model.setActiveRibbonTab,
      model.onMainViewTabKeyDown,
      model.closePreviewView,
      model.activePromotedView,
      model.openPromotedTab,
      model.closePromotedTab,
      model.selectSheetTab,
      model.selectSheet,
      model.refreshSheets,
      model.handleCellEdit,
      model.closeRoutePanel,
      model.openRowInDocumentView,
      model.applyLens,
      model.leftSidebarPluginPanelDescriptors,
      model.openEvidenceViewer,
      model.openRowRef,
      model.openSourceHealthMainView,
      model.refreshHistory,
      model.invalidateProjectData,
      model.setDiscoverOpen,
      model.setDiscoverTab,
      model.openDiscover,
      model.rightInspectorPluginPanelDescriptors,
      model.actionPanelVisible,
      model.closeActionRoute,
      model.actionLaunchInitial,
      model.actionDraftLaunchId,
      model.runActionFromSurface,
      model.runActionBackfill,
      model.resumeHaltedRun,
      model.runWithPreview,
      model.executeRegisteredAction,
      model.openOcrCompareTab,
      model.doStepTo,
      model.loadHistoryPage,
      model.pluginManagerOperations,
      model.selectBottomDockTab,
      model.bottomDockPluginPanelDescriptors,
      model.hideContribution,
    ],
  );  const actHandle = useMemo<WorkspaceActHandle>(
    () => ({
      actRibbonTabs: model.actRibbonTabs,
      launchActionFromSurface: model.launchActionFromSurface,
      runActionFromSurface: model.runActionFromSurface,
    }),
    [
      model.actRibbonTabs,
      model.launchActionFromSurface,
      model.runActionFromSurface,
    ],
  );
  const pluginDetailRender = model.renderPluginDetailTab;
  const rowWalk = model.walkDetailRow;
  return (
    <WorkspaceShellContext.Provider value={shell}>
      <WorkspaceActContext.Provider value={actHandle}>
        <WorkspacePluginDetailRenderContext.Provider value={pluginDetailRender}>
        <WorkspaceRowWalkContext.Provider value={rowWalk}>
          <WorkspaceLayoutContext.Provider value={model.resolvedWorkbenchLayout}>
            <WorkspaceHostContextContext.Provider value={model.workbenchHostContext}>
              <WorkspaceMainViewModelContext.Provider value={model.mainViewModel}>
                <WorkspaceOverlayModelContext.Provider value={model.overlayModel}>
                  <WorkspaceViewContent />
                </WorkspaceOverlayModelContext.Provider>
              </WorkspaceMainViewModelContext.Provider>
            </WorkspaceHostContextContext.Provider>
          </WorkspaceLayoutContext.Provider>
        </WorkspaceRowWalkContext.Provider>
        </WorkspacePluginDetailRenderContext.Provider>
      </WorkspaceActContext.Provider>
    </WorkspaceShellContext.Provider>
  );
}

function WorkspaceViewContent() {
  return (
    <div className="app">
      <div className="workbench-shell" data-testid="workbench-shell">
        <WorkspaceChromeBarRegion />
        <WorkspaceActRegion />
        <div className="workbench-body">
          <WorkspaceAskRegion />
          <div className="workbench-content">
            <WorkspaceNavigateRegion />
            <div className="app-main workbench-main-row">
              <WorkspaceMainViewRegion />
              <WorkspaceInspectDetailRegion />
              <WorkspaceRightInspectorRegion />
              <WorkspaceDiscoverRegion />
            </div>
          </div>
        </div>
        <WorkspaceBottomDockRegion />
        <WorkspaceActionDrawerRegion />
      </div>
      <WorkspaceOverlayRegion />
    </div>
  );
}
