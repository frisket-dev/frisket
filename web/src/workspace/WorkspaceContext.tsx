import { createContext, useContext, useMemo } from 'react';
import { type RunProgress, type SheetMeta } from '../api/open';
import { type WorkbenchHostId } from '../workbench/descriptors';
import { loadColumnOrder, loadHiddenColumns } from './gridColumnState';
import type { useWorkspaceModel } from './useWorkspaceModel';
import { useGridViewHandle } from '../bind/useGridViewHandle';
import { useProjectDataResource, useRouteHandle } from '../bind/useRouteHandle';
import { selectActiveSheetId } from '../state/routeStore';
import { useJobsHandle } from '../bind/useJobsHandle';
import { useSelectionHandle } from '../bind/useSelectionHandle';
import { useSelector } from '../bind/useSelector';

export type WorkspaceProps = Parameters<typeof useWorkspaceModel>[0];

export type WorkspaceViewModel = ReturnType<typeof useWorkspaceModel>;

declare global {
  interface Window {
    __renderCounts?: Record<string, number>;
  }
}

export function useRenderCount(region: string): void {
  if (import.meta.env.DEV) {
    window.__renderCounts = window.__renderCounts ?? {};
    window.__renderCounts[region] = (window.__renderCounts[region] ?? 0) + 1;
  }
}

export interface WorkspaceShellHandle {
  askNavigation: WorkspaceViewModel['askNavigation'];
  project: WorkspaceViewModel['project'];
  projectApi: WorkspaceViewModel['projectApi'];

  runActCommand: WorkspaceViewModel['runActCommand'];
  revealPluginLauncher: WorkspaceViewModel['revealPluginLauncher'];
  onImported: WorkspaceViewModel['onImported'];
  showError: WorkspaceViewModel['showError'];

  setRibbonMode: WorkspaceViewModel['setRibbonMode'];
  setActiveRibbonTab: WorkspaceViewModel['setActiveRibbonTab'];
  onMainViewTabKeyDown: WorkspaceViewModel['onMainViewTabKeyDown'];
  closePreviewView: WorkspaceViewModel['closePreviewView'];
  activePromotedView: WorkspaceViewModel['activePromotedView'];
  openPromotedTab: WorkspaceViewModel['openPromotedTab'];
  closePromotedTab: WorkspaceViewModel['closePromotedTab'];
  selectSheetTab: WorkspaceViewModel['selectSheetTab'];

  selectSheet: WorkspaceViewModel['selectSheet'];
  refreshSheets: WorkspaceViewModel['refreshSheets'];

  handleCellEdit: WorkspaceViewModel['handleCellEdit'];
  closeRoutePanel: WorkspaceViewModel['closeRoutePanel'];
  openRowInDocumentView: WorkspaceViewModel['openRowInDocumentView'];
  openDocumentRef: WorkspaceViewModel['openDocumentRef'];
  applyLens: WorkspaceViewModel['applyLens'];
  leftSidebarPluginPanelDescriptors: WorkspaceViewModel['leftSidebarPluginPanelDescriptors'];
  openEvidenceViewer: WorkspaceViewModel['openEvidenceViewer'];
  openRowRef: WorkspaceViewModel['openRowRef'];
  openSourceHealthMainView: WorkspaceViewModel['openSourceHealthMainView'];
  refreshHistory: WorkspaceViewModel['refreshHistory'];
  invalidateProjectData: WorkspaceViewModel['invalidateProjectData'];

  setDiscoverOpen: WorkspaceViewModel['setDiscoverOpen'];
  setDiscoverTab: WorkspaceViewModel['setDiscoverTab'];
  openDiscover: WorkspaceViewModel['openDiscover'];
  rightInspectorPluginPanelDescriptors: WorkspaceViewModel['rightInspectorPluginPanelDescriptors'];
  actionPanelVisible: WorkspaceViewModel['actionPanelVisible'];
  closeActionRoute: WorkspaceViewModel['closeActionRoute'];
  actionLaunchInitial: WorkspaceViewModel['actionLaunchInitial'];
  actionDraftLaunchId: WorkspaceViewModel['actionDraftLaunchId'];

  runActionFromSurface: WorkspaceViewModel['runActionFromSurface'];
  runActionBackfill: WorkspaceViewModel['runActionBackfill'];

  resumeHaltedRun: WorkspaceViewModel['resumeHaltedRun'];
  runWithPreview: WorkspaceViewModel['runWithPreview'];
  executeRegisteredAction: WorkspaceViewModel['executeRegisteredAction'];
  openOcrCompareTab: WorkspaceViewModel['openOcrCompareTab'];
  openActionCompare: WorkspaceViewModel['openActionCompare'];
  openActionCompareHistory: WorkspaceViewModel['openActionCompareHistory'];
  returnToActionCompare: WorkspaceViewModel['returnToActionCompare'];
  doStepTo: WorkspaceViewModel['doStepTo'];
  loadHistoryPage: WorkspaceViewModel['loadHistoryPage'];
  pluginManagerOperations: WorkspaceViewModel['pluginManagerOperations'];
  selectBottomDockTab: WorkspaceViewModel['selectBottomDockTab'];
  bottomDockPluginPanelDescriptors: WorkspaceViewModel['bottomDockPluginPanelDescriptors'];

  hideContribution: WorkspaceViewModel['hideContribution'];
}

export function resolveRegionContributions(
  resolvedWorkbenchLayout: WorkspaceViewModel['resolvedWorkbenchLayout'],
  regionId: WorkbenchHostId,
) {
  const region = resolvedWorkbenchLayout.find((item) => item.regionId === regionId);
  return region ?? { regionId, contributions: [] };
}

export const WorkspaceShellContext = createContext<WorkspaceShellHandle | null>(null);

export const WorkspaceLayoutContext = createContext<WorkspaceViewModel['resolvedWorkbenchLayout'] | null>(
  null,
);

export function useResolvedWorkbenchLayout(): WorkspaceViewModel['resolvedWorkbenchLayout'] {
  const layout = useContext(WorkspaceLayoutContext);
  if (!layout) {
    throw new Error('useResolvedWorkbenchLayout must be used within WorkspaceLayoutContext.Provider');
  }
  return layout;
}

export function useWorkspaceShell(): WorkspaceShellHandle {
  const shell = useContext(WorkspaceShellContext);
  if (!shell) {
    throw new Error('useWorkspaceShell must be used within WorkspaceShellContext.Provider');
  }
  return shell;
}

export const WorkspaceHostContextContext = createContext<
  WorkspaceViewModel['workbenchHostContext'] | null
>(null);

export function useWorkbenchHostContext(): WorkspaceViewModel['workbenchHostContext'] {
  const hostContext = useContext(WorkspaceHostContextContext);
  if (!hostContext) {
    throw new Error(
      'useWorkbenchHostContext must be used within WorkspaceHostContextContext.Provider',
    );
  }
  return hostContext;
}

export interface WorkspaceActHandle {
  actRibbonTabs: WorkspaceViewModel['actRibbonTabs'];
  expectedAnswers: WorkspaceViewModel['expectedAnswers'];
  compareActionRun: WorkspaceViewModel['compareActionRun'];
  launchActionFromSurface: WorkspaceViewModel['launchActionFromSurface'];
  runActionFromSurface: WorkspaceViewModel['runActionFromSurface'];
}

export const WorkspaceActContext = createContext<WorkspaceActHandle | null>(null);

export function useWorkspaceAct(): WorkspaceActHandle {
  const act = useContext(WorkspaceActContext);
  if (!act) {
    throw new Error('useWorkspaceAct must be used within WorkspaceActContext.Provider');
  }
  return act;
}

export type WorkspacePluginDetailRender = WorkspaceViewModel['renderPluginDetailTab'];

export type WorkspaceRowWalk = WorkspaceViewModel['walkDetailRow'];

export const WorkspacePluginDetailRenderContext = createContext<WorkspacePluginDetailRender | null>(null);

export const WorkspaceRowWalkContext = createContext<WorkspaceRowWalk | null>(null);

export function useWorkspacePluginDetailRender(): WorkspacePluginDetailRender {
  const render = useContext(WorkspacePluginDetailRenderContext);
  if (!render) {
    throw new Error('useWorkspacePluginDetailRender must be used within its Provider');
  }
  return render;
}

export function useWorkspaceRowWalk(): WorkspaceRowWalk {
  const walk = useContext(WorkspaceRowWalkContext);
  if (!walk) {
    throw new Error('useWorkspaceRowWalk must be used within its Provider');
  }
  return walk;
}

export function useRun(): RunProgress | null {
  const jobs = useJobsHandle();
  return useSelector(jobs.store, (s) => s.run);
}

export function useSelectedRowIdsForSheet(): WorkspaceViewModel['mainViewModel']['selectedRowIdsForSheet'] {
  const selection = useSelectionHandle();
  const selectedRows = useSelector(selection.store, (s) => s.selectedRows);
  const { sheet } = useCurrentSheet();
  return useMemo(
    () => (sheet && selectedRows.sheetId === sheet.id ? selectedRows.rowIds : []),
    [sheet, selectedRows.sheetId, selectedRows.rowIds],
  );
}

export function useCurrentSheet(): {
  sheet: SheetMeta | undefined;
  sheets: SheetMeta[];
  sheetsLoaded: boolean;
} {
  const projectData = useProjectDataResource();
  const route = useRouteHandle();
  const sheets = useSelector(projectData.store, (state) => state.sheets);
  const sheetsLoaded = useSelector(projectData.store, (state) => state.sheetsLoaded);
  const activeSheetId = useSelector(route.store, selectActiveSheetId);
  const sheet = sheets.find((candidate) => candidate.id === activeSheetId);
  return { sheet, sheets, sheetsLoaded };
}

export function useTitleColumnOrder(sheet: SheetMeta | undefined): string[] {
  const { project } = useWorkspaceShell();
  const gridView = useGridViewHandle();
  const columnOrderBySheet = useSelector(gridView.store, (s) => s.columnOrderBySheet);
  const hiddenColumnsBySheet = useSelector(gridView.store, (s) => s.hiddenColumnsBySheet);
  return useMemo(() => {
    if (!sheet) return [];
    const order = columnOrderBySheet[sheet.id] ?? loadColumnOrder(project.id, sheet);
    const hidden = new Set(
      hiddenColumnsBySheet[sheet.id] ?? loadHiddenColumns(project.id, sheet),
    );
    return order.filter((name) => !hidden.has(name));
  }, [sheet, project.id, columnOrderBySheet, hiddenColumnsBySheet]);
}

type WorkspaceMainViewModel = WorkspaceViewModel['mainViewModel'];

export const WorkspaceMainViewModelContext = createContext<WorkspaceMainViewModel | null>(null);

export function useMainViewModel(): WorkspaceMainViewModel {
  const mainViewModel = useContext(WorkspaceMainViewModelContext);
  if (!mainViewModel) {
    throw new Error('useMainViewModel must be used within WorkspaceMainViewModelContext.Provider');
  }
  return mainViewModel;
}

type WorkspaceOverlayModel = WorkspaceViewModel['overlayModel'];

export const WorkspaceOverlayModelContext = createContext<WorkspaceOverlayModel | null>(null);

export function useOverlayModel(): WorkspaceOverlayModel {
  const overlayModel = useContext(WorkspaceOverlayModelContext);
  if (!overlayModel) {
    throw new Error('useOverlayModel must be used within WorkspaceOverlayModelContext.Provider');
  }
  return overlayModel;
}
