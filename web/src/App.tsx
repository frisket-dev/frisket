import { ActionPreviewBanner } from './components/ActionPreviewBanner';
import {
  lazy,
  memo,
  Suspense,
  createContext,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type RefObject,
  type ReactNode,
} from 'react';
import { PanelSelect } from './components/PanelSelect';
import {
  AtSign,
  Bell,
  BellRing,
  BookOpen,
  Boxes,
  Database,
  Eye,
  FileText,
  GitFork,
  Images,
  Info,
  LayoutGrid,
  ListFilter,
  MapPin,
  MessageSquareQuote,
  MoreHorizontal,
  Network,
  Plus,
  Puzzle,
  RotateCw,
  Rows3,
  AudioLines,
  Languages,
  ScanText,
  Sheet as SheetGlyph,
  ShieldCheck,
  Tags,
  Trash2,
  WrapText,
  Watch,
  X,
  type LucideIcon,
} from 'lucide-react';
import {
  ConfirmationRequiredError,
  listProjects,
  MAX_LENS_VIEW_ROWS,
  type ProjectInfo,
  type RunEstimate,
  type RunProgress,
  type SheetMeta,
} from './api/open';
import { canEditProject } from './api/projectRole';
import { useNativePopover } from './hooks/useNativePopover';
import { useAnchoredPosition } from './hooks/useAnchoredPosition';
import { useEscapeDismiss } from './hooks/useEscapeDismiss';
import { countLabel, formatUsd } from './format';
import { MENTIONS_EXTRACT_ACTION_KIND } from './components/mentionsPanelModel';
import { StatusBar } from './components/StatusBar';
import { NowPlayingWidget } from './components/NowPlayingWidget';
import { PanelEmpty, PanelLoading, SegmentedToggle, StatusChip } from './components/PanelPrimitives';
import { useResizable } from './components/useResizable';
import { ResizeSeam } from './components/ResizeSeam';
import { ColumnDrawer } from './components/ColumnDrawer';
import { PdfTablesPicker } from './components/PdfTablesPicker';
import { ReviewQueue } from './components/ReviewQueue';
import { ProvenanceManifest } from './components/ProvenanceManifest';
import { CompletedClusterResult } from './components/CompletedClusterResult';
import { entityTableDraft } from './actions/entityTable';
import { HomeScreen } from './components/HomeScreen';
import { ReplayModeBanner } from './components/ReplayModeBanner';
import { ImportDropzone, ImportWorkspaceDialog } from './components/ImportCsv';
import { CostGateModal } from './components/CostGateModal';
import { ApplicationGuidance } from './components/ApplicationGuidance';
import { SampleProjectOnboarding } from './workbench/SampleProjectOnboarding';
import { setSampleGuideArrival } from './workbench/sampleGuideArrival';
import { ChromeBar } from './workbench/ChromeBar';
import { useWalkthrough } from './walkthrough/context';
import { readSettingsProjectContext } from './settings/settingsProjectContext';
import { ProductTelemetryProvider } from './telemetry/ProductTelemetryProvider';
import { sendProductTelemetry } from './telemetry/productTelemetry';
import { ActRibbon } from './workbench/ActRibbon';
import { ActMenuBar } from './workbench/ActMenuBar';
import { InspectDetailColumn } from './workbench/InspectDetailColumn';
import { ActionDrawer } from './workbench/ActionDrawer';
import { DiscoverPanel, type DiscoverTabDescriptor } from './workbench/DiscoverPanel';
import { nerReplayPlan } from './workbench/nerReplayModel';
import { DocumentView } from './workbench/DocumentView';
import { AnswersView } from './workbench/AnswersView';
import { resolveTitleColumn } from './workbench/rowTitle';
import {
  DISCOVER_TABS,
  type DiscoverTab,
  type WorkViewKind,
} from './workspace/useWorkspaceChromeState';
import { ProjectExportModals } from './components/TopNav';
import { actionsForColumn, orderColumnActionsForNextStep } from './actions/model';
import { pdfTablesMaterializeRequest } from './actions/pdfTablesMaterialize';
import { columnTablesExportRequest } from './actions/columnTablesExport';
import { quotedCost } from './actions/quotedCost';
import { ConfirmDeleteRowsModal } from './components/ConfirmDeleteRowsModal';
import { ConfirmDeleteSheetModal } from './components/ConfirmDeleteSheetModal';
import { AddSheetButton, DeleteRowsButton } from './components/TabStripButtons';
import { OutputColumnCollisionModal } from './components/OutputColumnCollisionModal';
import { SupportContactNote } from './components/SupportContactNote';
import { remediateApiError } from './errors/remediation';
import {
  SourceHealthMainView,
  SourcesConnectionsDialog,
} from './components/SourcesPanel';
import {
  navigate,
  useRoute,
  type RoutePanel,
  type SettingsRoute,
} from './routes';
import { isRunActionBlockedStatus } from './runStatusModel';
import {
  ActionWorkbenchFormFrame,
  BottomDockTabFrame,
  CostGateWorkbenchViewFrame,
  EmbeddingsWorkbenchPanel,
  FriendlyFiltersWorkbenchPanel,
  MentionsWorkbenchPanel,
  EvidenceWorkbenchViewFrame,
  NotificationsWorkbenchPanel,
  GraphNeighborhoodWorkbenchViewFrame,
  OutputColumnCollisionWorkbenchViewFrame,
  ProvenanceWorkbenchPanelFrame,
  ResolvedWorkbenchLayoutRegion,
  ReviewQueueWorkbenchViewFrame,
  RowDeleteConfirmWorkbenchViewFrame,
  SourceHealthMainViewFrame,
  SourcesWorkbenchPanel,
  HistoryWorkbenchPanel,
  SavedViewsWorkbenchPanel,
  WatchesWorkbenchPanel,
} from './workbench/contributions';
import {
  BOTTOM_DOCK_ERRORS_DESCRIPTOR,
  BOTTOM_DOCK_JOBS_DESCRIPTOR,
  BOTTOM_DOCK_LINEAGE_DESCRIPTOR,
  IMAGE_GALLERY_VIEW_DESCRIPTOR,
  type WorkbenchHostId,
} from './workbench/descriptors';
import {
  type WorkbenchResolvedLayoutContribution,
  type WorkbenchResolvedLayoutRegion,
} from './workbench/layout';
import { OcrCompareTab } from './workbench/OcrCompareTab';
import { TranscribeCompareTab } from './workbench/TranscribeCompareTab';
import { TranslateCompareTab } from './workbench/TranslateCompareTab';
import { TopicSegmentationCompareTab } from './workbench/TopicSegmentationCompareTab';
import { PluginPeekHost } from './workbench/PluginPeekHost';
import { PluginDockTabHost } from './workbench/PluginDockTabHost';
import { PluginMainViewHost } from './workbench/PluginMainViewHost';
import { PluginPanelHost } from './workbench/PluginPanelHost';
import {
  WorkbenchBottomDock,
  WorkbenchJobSplitPanel,
  type DockActionJob,
  type WorkbenchBottomDockRenderContext,
} from './workbench/WorkbenchBottomDock';
import {
  deriveDockErrorJobs,
  deriveDockRunSummary,
  derivePluginErrorJobs,
} from './workbench/dockJobSummary';
import { CascadeConfirmDialog, LineagePanel } from './workbench/LineagePanel';
import { staleSheetsDeepestFirst } from './workbench/lineageStale';
import {
  WorkbenchCommandPalette,
} from './workbench/WorkbenchCommandPalette';
import {
  ROW_HEIGHTS,
} from './workspace/workspaceState';
import {
  gridFilterLabel,
  gridSortLabel,
  loadColumnOrder,
  loadHiddenColumns,
} from './workspace/gridColumnState';
import { CopilotDialogPopover } from './workspace/popovers';
import { beginCopilotImportHandoff } from './state/copilotImportTransition';
import { DiagnosticReportButton } from './workspace/DiagnosticReport';
import {
  COPILOT_CONTRIBUTION_ID,
  EMBEDDINGS_CONTRIBUTION_ID,
  FRIENDLY_FILTERS_CONTRIBUTION_ID,
  MENTIONS_CONTRIBUTION_ID,
  EVIDENCE_CONTRIBUTION_ID,
  GRAPH_NEIGHBORHOOD_CONTRIBUTION_ID,
  NOTIFICATIONS_CONTRIBUTION_ID,
  SEARCH_CONTRIBUTION_ID,
  SAVED_VIEWS_CONTRIBUTION_ID,
  SOURCES_CONTRIBUTION_ID,
  SOURCE_HEALTH_CONTRIBUTION_ID,
  WATCHES_CONTRIBUTION_ID,
} from './workspace/contributionIds';
import { GridColumnHeaderMenu } from './workspace/gridMenus';
import { GridSortPanel } from './workspace/GridSortPanel';
import { useWorkspaceModel } from './workspace/useWorkspaceModel';
import { WorkspaceStoresProvider } from './bind/WorkspaceStoresProvider';
import { useWorkspaceStores } from './bind/useWorkspaceStores';
import { useGridViewHandle } from './bind/useGridViewHandle';
import {
  selectInlineFilterColumn,
  selectInlineFilterOpen,
} from './state/gridViewStore';
import { useProjectDataResource, useRouteHandle } from './bind/useRouteHandle';
import { selectActiveSheetId } from './state/routeStore';
import { previewViewForSheet } from './state/previewViewStore';
import { useChromeHandle } from './bind/useChromeHandle';
import { useActSurfaceHandle } from './bind/useActSurfaceHandle';
import { useActionCatalogHandle } from './bind/useActionCatalogHandle';
import { useWorkViewHandle } from './bind/useWorkViewHandle';
import { useCompareViewHandle } from './bind/useCompareViewHandle';
import { usePreviewViewHandle } from './bind/usePreviewViewHandle';
import { useDetailHandle } from './bind/useDetailHandle';
import { useJobsHandle } from './bind/useJobsHandle';
import { usePluginLayoutHandle } from './bind/usePluginLayoutHandle';
import { useSelectionHandle } from './bind/useSelectionHandle';
import { useLensViewHandle } from './bind/useLensViewHandle';
import { useSelector } from './bind/useSelector';
import { useShellIdentity } from './shellIdentity';
import { useEditionModule } from './editions/module';
import { focusCompareTabTransition } from './state/workspaceTransitions';

let proposalInspectSeq = 0;

const LazySignIn = lazy(() => (
  import('./components/SignIn').then(({ SignIn }) => ({ default: SignIn }))
));
const LazySettingsWorkspace = lazy(() => (
  import('./settings/SettingsWorkspace').then(({ SettingsWorkspace }) => ({
    default: SettingsWorkspace,
  }))
));
const LazyAdminPage = lazy(() => (
  import('./components/AdminPage').then(({ AdminPage }) => ({ default: AdminPage }))
));
const LazyActionPanel = lazy(() => (
  import('./components/ActionPanel').then(({ ActionPanel }) => ({ default: ActionPanel }))
));
const LazyEvidenceViewer = lazy(() => (
  import('./components/EvidenceViewer').then(({ EvidenceViewer }) => ({ default: EvidenceViewer }))
));
const LazyGenericGraphView = lazy(() => (
  import('./components/GenericGraphView').then(({ GenericGraphView }) => ({
    default: GenericGraphView,
  }))
));

function lazyPage(node: ReactNode) {
  return (
    <Suspense fallback={<PanelLoading className="panel-loading-page" label="Loading…" />}>
      {node}
    </Suspense>
  );
}

const openHome = () => (
  <HomeScreen onOpen={(p, options) => {
    setSampleGuideArrival(options?.openGuide ? p.id : null);
    navigate({ kind: 'project', projectId: p.id });
  }} />
);

const openAdminUnavailable = () => (
  <div className="picker-screen" data-testid="admin-unavailable">
    <div className="picker-card">
      <PanelEmpty>Admin isn&apos;t available in local mode.</PanelEmpty>
      <button
        type="button"
        className="btn"
        data-testid="admin-unavailable-home-link"
        style={{ marginTop: 12 }}
        onClick={() => navigate({ kind: 'picker' })}
      >
        ← All projects
      </button>
    </div>
  </div>
);

function nextProposalInspectSeq(): number {
  proposalInspectSeq += 1;
  return proposalInspectSeq;
}

export default function App() {
  const route = useRoute();
  const { routes: editionRoutes } = useEditionModule();
  const activeRoute = editionRoutes.find((candidate) => candidate.path === window.location.pathname);
  const ActiveRouteComponent = activeRoute ? activeRoute.component : undefined;

  const { identityMode, me, resolved: identityResolved } = useShellIdentity();
  const needsSignIn = identityMode && identityResolved && me === null;

  if (activeRoute?.access === 'public') {
    if (ActiveRouteComponent) return lazyPage(<ActiveRouteComponent />);
    if (activeRoute.handler) {
      const contribution = activeRoute.handler();
      if (contribution) return lazyPage(contribution);
    }
  }
  if (!identityResolved) {
    return <div className="picker-screen" />;
  }
  if (needsSignIn) {
    return lazyPage(<LazySignIn />);
  }
  const withProductTelemetry = (node: ReactNode) => (
    <ProductTelemetryProvider>{node}</ProductTelemetryProvider>
  );
  if (activeRoute?.access === 'authenticated' && ActiveRouteComponent) {
    return withProductTelemetry(lazyPage(<ActiveRouteComponent />));
  }
  if (activeRoute?.access === 'authenticated' && activeRoute.handler) {
    const contribution = activeRoute.handler();
    if (contribution) return withProductTelemetry(lazyPage(contribution));
  }
  const withDiagnostics = (node: ReactNode) => (
    <div className="app-shell">
      <ReplayModeBanner />
      <div className="app-shell-body">{node}</div>
      {identityMode && <DiagnosticReportButton />}
    </div>
  );
  const content = (() => {
    switch (route.kind) {
    case 'settings':
      if (route.scope === 'project' && route.projectId) {
        return withDiagnostics(
          <ProjectSettingsRoute
            key={`settings-${route.projectId}`}
            route={route}
            projectId={route.projectId}
            identityMode={identityMode}
          />,
        );
      }
      return withDiagnostics(lazyPage(
        <LazySettingsWorkspace route={route} identityMode={identityMode} />,
      ));
    case 'admin':
      return editionRoutes.some((candidate) => candidate.id.startsWith('admin-'))
        ? withDiagnostics(lazyPage(<LazyAdminPage />))
        : withDiagnostics(openAdminUnavailable());
    case 'project':
      return withDiagnostics(
        <ProjectRoute
          key={route.projectId}
          projectId={route.projectId}
          sheetId={route.sheetId}
          actionKind={route.actionKind}
          review={route.review}
          panel={route.panel}
        />,
      );
    default:
      return withDiagnostics(openHome());
    }
  })();
  const walkthroughProjectId = route.kind === 'project'
    ? route.projectId
    : route.kind === 'settings'
      ? route.projectId ?? readSettingsProjectContext()?.id
      : undefined;
  return withProductTelemetry(
    <ApplicationGuidance projectId={walkthroughProjectId}>
      {content}
    </ApplicationGuidance>,
  );
}

type ProjectRouteState = {
  projectId: string | null;
  project: ProjectInfo | null;
  error: string | null;
};

function ProjectSettingsRoute({
  route,
  projectId,
  identityMode,
}: {
  route: SettingsRoute;
  projectId: string;
  identityMode: boolean;
}) {
  const [state, setState] = useState<ProjectRouteState>({ projectId: null, project: null, error: null });

  useEffect(() => {
    let alive = true;
    void (async () => {
      let nextState: ProjectRouteState;
      try {
        const list = await listProjects();
        const p = list.find((x) => x.id === projectId);
        if (!p) {
          nextState = { projectId, project: null, error: `No project “${projectId}” in this workspace.` };
        } else {
          nextState = { projectId, project: p, error: null };
        }
      } catch (e) {
        const message = e instanceof Error ? e.message : String(e);
        nextState = { projectId, project: null, error: `Cannot reach the frisket server: ${message}` };
      }
      if (alive) setState(nextState);
    })();
    return () => {
      alive = false;
    };
  }, [projectId]);

  if (state.projectId !== projectId) {
    return <PanelLoading className="panel-loading-page" label="Loading project settings…" />;
  }
  if (state.error) {
    return lazyPage(
      <LazySettingsWorkspace
        route={route}
        identityMode={identityMode}
        routeError={state.error}
      />,
    );
  }
  if (!state.project) {
    return <PanelLoading className="panel-loading-page" label="Loading project settings…" />;
  }
  return lazyPage(
    <WorkspaceStoresProvider projectId={state.project.id}>
      <LazySettingsWorkspace
        route={route}
        project={state.project}
        identityMode={identityMode}
      />
    </WorkspaceStoresProvider>,
  );
}

export function ProjectRoute({
  projectId,
  sheetId,
  actionKind,
  review,
  panel,
}: {
  projectId: string;
  sheetId?: string;
  actionKind?: string;
  review?: boolean;
  panel?: RoutePanel;
}) {
  const [state, setState] = useState<ProjectRouteState>({ projectId: null, project: null, error: null });

  useEffect(() => {
    let alive = true;
    void (async () => {
      let nextState: ProjectRouteState;
      try {
        const list = await listProjects();
        const p = list.find((x) => x.id === projectId);
        if (!p) {
          nextState = { projectId, project: null, error: `No project “${projectId}” in this workspace.` };
        } else {
          nextState = { projectId, project: p, error: null };
        }
      } catch (e) {
        const message = e instanceof Error ? e.message : String(e);
        nextState = { projectId, project: null, error: `Cannot reach the frisket server: ${message}` };
      }
      if (alive) setState(nextState);
    })();
    return () => {
      alive = false;
    };
  }, [projectId]);

  const openedProjectRef = useRef<string | null>(null);
  useEffect(() => {
    if (state.projectId !== projectId || !state.project || openedProjectRef.current === projectId) return;
    openedProjectRef.current = projectId;
    sendProductTelemetry({ type: 'Project.opened', properties: {} }, projectId);
  }, [projectId, state.project, state.projectId]);

  if (state.error) {
    return (
      <div className="picker-screen">
        <div className="picker-card">
          <div className="picker-error" data-testid="project-route-error">{state.error}</div>
          <button
            type="button"
            className="btn"
            style={{ marginTop: 12 }}
            onClick={() => navigate({ kind: 'picker' })}
          >
            ← All projects
          </button>
        </div>
      </div>
    );
  }
  if (!state.project) {
    return <PanelLoading className="panel-loading-page" label="Loading project…" />;
  }
  return (
    <WorkspaceStoresProvider projectId={state.project.id}>
      <Workspace
        key={state.project.id}
        project={state.project}
        routeSheetId={sheetId}
        routeActionKind={actionKind}
        routeReview={review}
        routePanel={panel}
      />
    </WorkspaceStoresProvider>
  );
}

type WorkspaceProps = Parameters<typeof useWorkspaceModel>[0];
type WorkspaceViewModel = ReturnType<typeof useWorkspaceModel>;

declare global {
  interface Window {
    __renderCounts?: Record<string, number>;
  }
}

function useRenderCount(region: string): void {
  if (import.meta.env.DEV) {
    window.__renderCounts = window.__renderCounts ?? {};
    window.__renderCounts[region] = (window.__renderCounts[region] ?? 0) + 1;
  }
}

export interface WorkspaceShellHandle {
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
  doStepTo: WorkspaceViewModel['doStepTo'];
  loadHistoryPage: WorkspaceViewModel['loadHistoryPage'];
  pluginManagerOperations: WorkspaceViewModel['pluginManagerOperations'];
  selectBottomDockTab: WorkspaceViewModel['selectBottomDockTab'];
  bottomDockPluginPanelDescriptors: WorkspaceViewModel['bottomDockPluginPanelDescriptors'];

  hideContribution: WorkspaceViewModel['hideContribution'];
}

function resolveRegionContributions(
  resolvedWorkbenchLayout: WorkspaceViewModel['resolvedWorkbenchLayout'],
  regionId: WorkbenchHostId,
) {
  const region = resolvedWorkbenchLayout.find((item) => item.regionId === regionId);
  return region ?? { regionId, contributions: [] };
}

const WorkspaceShellContext = createContext<WorkspaceShellHandle | null>(null);

const WorkspaceLayoutContext = createContext<WorkspaceViewModel['resolvedWorkbenchLayout'] | null>(
  null,
);

function useResolvedWorkbenchLayout(): WorkspaceViewModel['resolvedWorkbenchLayout'] {
  const layout = useContext(WorkspaceLayoutContext);
  if (!layout) {
    throw new Error('useResolvedWorkbenchLayout must be used within WorkspaceLayoutContext.Provider');
  }
  return layout;
}

function useWorkspaceShell(): WorkspaceShellHandle {
  const shell = useContext(WorkspaceShellContext);
  if (!shell) {
    throw new Error('useWorkspaceShell must be used within WorkspaceShellContext.Provider');
  }
  return shell;
}

const WorkspaceHostContextContext = createContext<
  WorkspaceViewModel['workbenchHostContext'] | null
>(null);

function useWorkbenchHostContext(): WorkspaceViewModel['workbenchHostContext'] {
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
  launchActionFromSurface: WorkspaceViewModel['launchActionFromSurface'];
  runActionFromSurface: WorkspaceViewModel['runActionFromSurface'];
}
const WorkspaceActContext = createContext<WorkspaceActHandle | null>(null);
function useWorkspaceAct(): WorkspaceActHandle {
  const act = useContext(WorkspaceActContext);
  if (!act) {
    throw new Error('useWorkspaceAct must be used within WorkspaceActContext.Provider');
  }
  return act;
}

export type WorkspacePluginDetailRender = WorkspaceViewModel['renderPluginDetailTab'];
export type WorkspaceRowWalk = WorkspaceViewModel['walkDetailRow'];
const WorkspacePluginDetailRenderContext = createContext<WorkspacePluginDetailRender | null>(null);
const WorkspaceRowWalkContext = createContext<WorkspaceRowWalk | null>(null);
function useWorkspacePluginDetailRender(): WorkspacePluginDetailRender {
  const render = useContext(WorkspacePluginDetailRenderContext);
  if (!render) {
    throw new Error('useWorkspacePluginDetailRender must be used within its Provider');
  }
  return render;
}
function useWorkspaceRowWalk(): WorkspaceRowWalk {
  const walk = useContext(WorkspaceRowWalkContext);
  if (!walk) {
    throw new Error('useWorkspaceRowWalk must be used within its Provider');
  }
  return walk;
}

function useRun(): RunProgress | null {
  const jobs = useJobsHandle();
  return useSelector(jobs.store, (s) => s.run);
}

function useSelectedRowIdsForSheet(): WorkspaceViewModel['mainViewModel']['selectedRowIdsForSheet'] {
  const selection = useSelectionHandle();
  const selectedRows = useSelector(selection.store, (s) => s.selectedRows);
  const { sheet } = useCurrentSheet();
  return useMemo(
    () => (sheet && selectedRows.sheetId === sheet.id ? selectedRows.rowIds : []),
    [sheet, selectedRows.sheetId, selectedRows.rowIds],
  );
}

function useCurrentSheet(): {
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

function useTitleColumnOrder(sheet: SheetMeta | undefined): string[] {
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
const WorkspaceMainViewModelContext = createContext<WorkspaceMainViewModel | null>(null);
function useMainViewModel(): WorkspaceMainViewModel {
  const mainViewModel = useContext(WorkspaceMainViewModelContext);
  if (!mainViewModel) {
    throw new Error('useMainViewModel must be used within WorkspaceMainViewModelContext.Provider');
  }
  return mainViewModel;
}

type WorkspaceOverlayModel = WorkspaceViewModel['overlayModel'];
const WorkspaceOverlayModelContext = createContext<WorkspaceOverlayModel | null>(null);
function useOverlayModel(): WorkspaceOverlayModel {
  const overlayModel = useContext(WorkspaceOverlayModelContext);
  if (!overlayModel) {
    throw new Error('useOverlayModel must be used within WorkspaceOverlayModelContext.Provider');
  }
  return overlayModel;
}

function Workspace(props: WorkspaceProps) {
  const model = useWorkspaceModel(props);
  if (!model.sheetsLoaded) {
    return <PanelLoading className="panel-loading-page" label="Loading project…" />;
  }
  return <WorkspaceView model={model} />;
}

function WorkspaceView({ model }: { model: WorkspaceViewModel }) {
  const shell = useMemo<WorkspaceShellHandle>(
    () => ({
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
      doStepTo: model.doStepTo,
      loadHistoryPage: model.loadHistoryPage,
      pluginManagerOperations: model.pluginManagerOperations,
      selectBottomDockTab: model.selectBottomDockTab,
      bottomDockPluginPanelDescriptors: model.bottomDockPluginPanelDescriptors,
      hideContribution: model.hideContribution,
    }),
    [
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
      model.doStepTo,
      model.loadHistoryPage,
      model.pluginManagerOperations,
      model.selectBottomDockTab,
      model.bottomDockPluginPanelDescriptors,
      model.hideContribution,
    ],
  );
  const actHandle = useMemo<WorkspaceActHandle>(
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
        <WorkspaceNavigateRegion />
        <div className="app-main workbench-main-row">
          <WorkspaceMainViewRegion />
          <WorkspaceInspectDetailRegion />
          <WorkspaceRightInspectorRegion />
          <WorkspaceDiscoverRegion />
        </div>
        <WorkspaceBottomDockRegion />
        <WorkspaceActionDrawerRegion />
      </div>
      <WorkspaceOverlayRegion />
    </div>
  );
}

const WorkspaceChromeBarRegion = memo(function WorkspaceChromeBarRegion() {
  useRenderCount('chromeBar');
  const { project, projectApi } = useWorkspaceShell();
  const { sheet, sheets } = useCurrentSheet();
  const chrome = useChromeHandle();
  const copilotOpen = useSelector(chrome.store, (s) => s.copilotPopoverOpen);
  const toggleCopilot = chrome.toggleCopilotPopover;
  const openCommandPalette = chrome.openCommandPalette;
  const actSurface = useActSurfaceHandle();
  const actExportModal = useSelector(actSurface.store, (s) => s.actExportModal);
  const catalogExportTargets = useSelector(actSurface.store, (s) => s.actExportTargets);
  const closeActExportModal = actSurface.closeActExportModal;
  const actionCatalog = useActionCatalogHandle();
  const columnTablesExportEntry = useSelector(actionCatalog.store, (state) => (
    state.status === 'ready'
      ? state.catalog?.actions.find((entry) => entry.kind === 'export.column_tables')
      : undefined
  ));
  const gridView = useGridViewHandle();
  const activeGridFilter = useSelector(gridView.store, (s) => s.applied.filter);
  const activeGridSort = useSelector(gridView.store, (s) => s.applied.sort);
  const currentSheetExportOptions =
    activeGridFilter || activeGridSort
      ? { filter: activeGridFilter, sort: activeGridSort }
      : null;
  const walkthrough = useWalkthrough();
  return (
    <>
      <SampleProjectOnboarding project={project} />
      <ChromeBar
        project={project}
        projectApi={projectApi}
        currentSheet={sheet ?? null}
        sheets={sheets}
        currentSheetExportOptions={currentSheetExportOptions}
        catalogExportTargets={catalogExportTargets}
        onOpenCommandPalette={openCommandPalette}
        copilotOpen={copilotOpen}
        onToggleCopilot={toggleCopilot}
        walkthroughActive={walkthrough.active}
        walkthroughCanResume={walkthrough.canResume}
        walkthroughGuideSeen={walkthrough.guideSeen}
        walkthroughGuideEmphasized={walkthrough.guideEmphasized}
        walkthroughGuideHintVisible={walkthrough.guideHintVisible}
        onDismissGuideHint={walkthrough.dismissGuideHint}
        onOpenWalkthrough={walkthrough.openWalkthroughChooser}
        onResumeWalkthrough={walkthrough.resumeWalkthrough}
      />
      <ProjectExportModals
        modal={actExportModal}
        project={project}
        projectApi={projectApi}
        currentSheet={sheet ?? null}
        sheets={sheets}
        currentSheetExportOptions={currentSheetExportOptions}
        columnTablesExportEntry={columnTablesExportEntry}
        onClose={closeActExportModal}
      />
    </>
  );
});

const WorkspaceActRegion = memo(function WorkspaceActRegion() {
  useRenderCount('act');
  const {
    runActCommand,
    revealPluginLauncher,
    onImported,
    showError,
    setRibbonMode,
    setActiveRibbonTab,
  } = useWorkspaceShell();
  const { actRibbonTabs, launchActionFromSurface, runActionFromSurface } = useWorkspaceAct();
  const chrome = useChromeHandle();
  const ribbonMode = useSelector(chrome.store, (s) => s.ribbonMode);
  const activeRibbonTab = useSelector(chrome.store, (s) => s.activeRibbonTab);
  const actSurface = useActSurfaceHandle();
  const actionCatalog = useActionCatalogHandle();
  const catalogStatus = useSelector(actionCatalog.store, (state) => state.status);
  const importDialogOpen = useSelector(actSurface.store, (s) => s.importDialogOpen);
  const importDialogEntryMode = useSelector(actSurface.store, (s) => s.importDialogEntryMode);
  const importDialogCsv = useSelector(actSurface.store, (s) => s.importDialogCsv);
  const importDialogFiles = useSelector(actSurface.store, (s) => s.importDialogFiles);
  const nerTemplate = useSelector(actionCatalog.store, (state) =>
    state.status === 'ready'
      ? state.resolvedTemplates.find((template) => template.kind === 'map.ner')
      : undefined,
  );
  const ftmImportEnabled = useSelector(actionCatalog.store, (state) =>
    state.status === 'ready'
      && state.resolvedTemplates.some((template) => template.kind === 'frisket.ftm.ftm_import'),
  );
  const closeImportDialog = actSurface.closeImportDialog;
  useEffect(() => {
    // Command tabs appear before catalog-backed tabs; keep the saved selection
    // until the catalog has settled so loading cannot overwrite it.
    if (catalogStatus === 'idle' || catalogStatus === 'loading') return;
    if (actRibbonTabs.length === 0 || actRibbonTabs.some((tab) => tab.id === activeRibbonTab)) {
      return;
    }
    const fallbackTab = actRibbonTabs.find((tab) => tab.id === 'analyze') ?? actRibbonTabs[0];
    if (fallbackTab) setActiveRibbonTab(fallbackTab.id);
  }, [catalogStatus, actRibbonTabs, activeRibbonTab, setActiveRibbonTab]);
  return (
    <section
      className="workbench-region workbench-act"
      data-testid="workbench-region-act"
      aria-label="Act region"
      data-ribbon-mode={ribbonMode}
    >
      {ribbonMode === 'ribbon' ? (
        <ActRibbon
          tabs={actRibbonTabs}
          activeTabId={activeRibbonTab}
          onSelectTab={setActiveRibbonTab}
          onRunAction={launchActionFromSurface}
          onCommand={runActCommand}
          onLaunchContribution={revealPluginLauncher}
          onCollapse={() => setRibbonMode('menu')}
        />
      ) : (
        <ActMenuBar
          tabs={actRibbonTabs}
          onRunAction={launchActionFromSurface}
          onCommand={runActCommand}
          onLaunchContribution={revealPluginLauncher}
          onExpandRibbon={() => setRibbonMode('ribbon')}
        />
      )}
      <ImportWorkspaceDialog
        open={importDialogOpen}
        entryMode={importDialogEntryMode}
        initialCsv={importDialogCsv}
        initialFiles={importDialogFiles}
        onClose={closeImportDialog}
        onImported={onImported}
        onError={showError}
        onLaunchDownload={runActionFromSurface}
        onExtractEntities={() => runActionFromSurface(MENTIONS_EXTRACT_ACTION_KIND)}
        nerTemplate={nerTemplate}
        ftmImportEnabled={ftmImportEnabled}
      />
    </section>
  );
});

function SidebarContributionBody({ contribution }: { contribution: WorkbenchResolvedLayoutContribution }) {
  const { projectApi } = useWorkspaceStores();
  const {
    applyLens,
    leftSidebarPluginPanelDescriptors,
    openSourceHealthMainView,
    project,
    refreshHistory,
    refreshSheets,
    selectSheet,
    invalidateProjectData,
  } = useWorkspaceShell();
  const renderPluginDetailTab = useWorkspacePluginDetailRender();
  const workbenchHostContext = useWorkbenchHostContext();
  const resolvedWorkbenchLayout = useResolvedWorkbenchLayout();
  const { sheet, sheets } = useCurrentSheet();
  const {
    applySavedView,
    canCreateSavedView,
    cancelSavedViewEditor,
    cancelSavedViewsConfirmation,
    consumeSavedViewsPublication,
    deleteView,
    replaceSavedViewDefinition,
    renameSavedView,
    saveCurrentView,
    savedViewsConfirmation,
    savedViewsConfirmationError,
    savedViewsEditor,
    savedViewsLastPublication,
    savedViewsSaving,
    activeSavedViewId,
    addWatchForCurrentView,
    setViewName,
    startCreatingSavedView,
    startEditingView,
    startSavedViewDefinitionUpdate,
    startSavedViewDelete,
    viewName,
    views,
  } = useMainViewModel();

  switch (contribution.contributionId) {
    case 'frisket.core.panel.notifications':
      return <NotificationsWorkbenchPanel projectId={project.id} />;
    case 'frisket.core.panel.sources':
      return (
        <SourcesWorkbenchPanel
          sourceApi={projectApi}
          sourceDetailContributions={
            resolveRegionContributions(resolvedWorkbenchLayout, 'sourceDetail').contributions
          }
          renderPluginDetailTab={renderPluginDetailTab}
          onPolled={(sheetId) => {

            void refreshSheets().then(() => {
              if (sheetId != null) selectSheet(String(sheetId));
            });
            void refreshHistory();
            invalidateProjectData();
          }}
          onOpenSourceHealthMainView={openSourceHealthMainView}
        />
      );
    case 'frisket.investigative.panel.friendly_filters':
      return (
        <FriendlyFiltersWorkbenchPanel
          sheets={sheets}
          hostContext={workbenchHostContext}
        />
      );
    case 'frisket.investigative.panel.mentions':
      return (
        <MentionsWorkbenchPanel
          sheets={sheets}
          hostContext={workbenchHostContext}
        />
      );
    case SAVED_VIEWS_CONTRIBUTION_ID:
      return (
        <SavedViewsWorkbenchPanel
          views={views}
          viewName={viewName}
          editor={savedViewsEditor}
          activeSavedViewId={activeSavedViewId}
          isSaving={savedViewsSaving}
          lastPublication={savedViewsLastPublication}
          onPublicationConsumed={consumeSavedViewsPublication}
          confirmation={savedViewsConfirmation}
          confirmationError={savedViewsConfirmationError}
          canEdit={canCreateSavedView}
          onNameChange={setViewName}
          onStartCreating={() => startCreatingSavedView()}
          onSave={saveCurrentView}
          onUpdate={renameSavedView}
          onCancel={cancelSavedViewEditor}
          onEdit={startEditingView}
          onApply={applySavedView}
          onStartDefinitionUpdate={startSavedViewDefinitionUpdate}
          onStartDelete={startSavedViewDelete}
          onConfirmDefinitionUpdate={replaceSavedViewDefinition}
          onConfirmDelete={deleteView}
          onCancelConfirmation={cancelSavedViewsConfirmation}
        />
      );
    case 'frisket.core.panel.watches':
      return (
        <WatchesWorkbenchPanel
          canEdit={canEditProject(project)}
          canCreateFromCurrentView={sheet !== null && canEditProject(project)}
          onCreateFromCurrentView={addWatchForCurrentView}
        />
      );
    case 'frisket.embeddings.panel.indexes':
      return sheet ? (
        <EmbeddingsWorkbenchPanel
          apiPort={projectApi}
          key={sheet.id}
          sheet={sheet}
          onSelectSheet={(sheetId) => {

            void refreshSheets().then(() => selectSheet(sheetId));
          }}
          onOpenLens={(lensId, name) => {
            void applyLens(lensId, name);
          }}
        />
      ) : null;
    default: {
      if (contribution.runtimeSource === 'runtimeIndex') {
        const descriptor = leftSidebarPluginPanelDescriptors.find(
          (candidate) => candidate.id === contribution.contributionId,
        );
        if (descriptor) {
          return (
            <PluginPanelHost
              descriptor={descriptor}
              sheet={sheet ?? null}
              hostContext={workbenchHostContext}
              host="leftSidebar"
            />
          );
        }
      }
      return (
        <div
          className="sidebar-contribution-unavailable"
          data-testid="sidebar-unavailable-contribution"
          data-contribution-id={contribution.contributionId}
        >
          This panel&apos;s content is unavailable.
        </div>
      );
    }
  }
}

const NON_DISCOVER_LEFT_SIDEBAR_CONTRIBUTION_IDS = new Set<string>([
  SEARCH_CONTRIBUTION_ID,
  COPILOT_CONTRIBUTION_ID,
]);

const DISCOVER_TAB_CONTRIBUTION_IDS: Record<DiscoverTab, readonly string[]> = {
  Facets: [FRIENDLY_FILTERS_CONTRIBUTION_ID],
  Mentions: [MENTIONS_CONTRIBUTION_ID],
  Sources: [SOURCES_CONTRIBUTION_ID],
  Views: [SAVED_VIEWS_CONTRIBUTION_ID],
  Watches: [WATCHES_CONTRIBUTION_ID],
  Embeddings: [EMBEDDINGS_CONTRIBUTION_ID],
  Notifications: [NOTIFICATIONS_CONTRIBUTION_ID],
};

const DISCOVER_TAB_ICON: Record<DiscoverTab, LucideIcon> = {
  Facets: Tags,
  Mentions: AtSign,
  Sources: Database,
  Views: BookOpen,
  Watches: Watch,
  Embeddings: Boxes,
  Notifications: BellRing,
};

const PLUGIN_TAB_ICONS: Record<string, LucideIcon> = {
  AtSign,
  Bell,
  Boxes,
  Database,
  Images,
  ListFilter,
  MapPin,
  Network,
  Tags,
};

function pluginTabIcon(iconName?: string): LucideIcon {
  return (iconName && PLUGIN_TAB_ICONS[iconName]) || Puzzle;
}

function contributionTestSlug(contributionId: string): string {
  return contributionId.replace(/[^a-zA-Z0-9]+/g, '-');
}

function filterRegionContributions(
  region: WorkbenchResolvedLayoutRegion,
  keep: (contributionId: string) => boolean,
): WorkbenchResolvedLayoutRegion {
  return {
    ...region,
    contributions: region.contributions.filter((c) => keep(c.contributionId)),
  };
}

const WorkspaceInspectDetailRegion = memo(function WorkspaceInspectDetailRegion() {
  useRenderCount('inspectDetail');
  const {
    project,
    closeRoutePanel,
    handleCellEdit,
    openRowInDocumentView,
    runActionBackfill,
  } = useWorkspaceShell();
  const walkDetailRow = useWorkspaceRowWalk();
  const renderPluginDetailTab = useWorkspacePluginDetailRender();
  const resolvedWorkbenchLayout = useResolvedWorkbenchLayout();
  const { sheet } = useCurrentSheet();
  const detail = useDetailHandle();
  const rowDrawer = useSelector(detail.store, (s) => s.rowDrawer);
  const rowDrawerPreview = useSelector(detail.store, (s) => s.rowDrawerPreview);
  const selection = useSelectionHandle();
  const selectedColumnId = useSelector(selection.store, (s) => s.selectedColumnId);
  const titleColumnOrder = useTitleColumnOrder(sheet);

  const renderRowDetailContribution = useCallback(
    (contribution: WorkbenchResolvedLayoutContribution) => {
      if (!sheet || !rowDrawer) return null;
      return renderPluginDetailTab(contribution, {
        kind: 'row',
        sheetId: sheet.id,
        rowId: String(rowDrawer.id),
      });
    },
    [renderPluginDetailTab, rowDrawer, sheet],
  );

      // Per-row retries must use the cost-gated backfill path.
  const rowDrawerId = rowDrawer?.id;
  const onRetryCell = useCallback(
    (columnName: string) =>
      rowDrawerId == null
        ? Promise.resolve()
        : runActionBackfill(columnName, [Number(rowDrawerId)]),
    [rowDrawerId, runActionBackfill],
  );

  if (!rowDrawer || !sheet) return null;

  return (
    <section
      className="workbench-region workbench-inspect-detail"
      data-testid="workbench-region-inspect"
      aria-label="Inspect detail"
    >
      <InspectDetailColumn
        projectId={project.id}
        sheet={sheet}
        row={rowDrawer}
        selectedColumnId={selectedColumnId}
        preview={rowDrawerPreview}
        onClose={closeRoutePanel}
        onEdit={handleCellEdit}
        onPrev={() => walkDetailRow(-1)}
        onNext={() => walkDetailRow(1)}
        canPrev={rowDrawer.index > 0}
        canNext={rowDrawer.index < sheet.rowCount - 1}
        detailContributions={resolveRegionContributions(resolvedWorkbenchLayout, 'rowDetail').contributions}
        renderDetailContribution={renderRowDetailContribution}
        onOpenInDocumentView={(columnId) => openRowInDocumentView(String(rowDrawer.id), columnId)}
        onRetryCell={onRetryCell}
        titleColumnOrder={titleColumnOrder}
      />
    </section>
  );
});

function DiscoverTabBody({ tabId }: { tabId: string }) {
  const resolvedWorkbenchLayout = useResolvedWorkbenchLayout();
  const leftSidebarRegion = resolveRegionContributions(resolvedWorkbenchLayout, 'leftSidebar');
  const contributionById = useMemo(() => {
    const map = new Map<string, WorkbenchResolvedLayoutContribution>();
    for (const c of leftSidebarRegion.contributions) map.set(c.contributionId, c);
    return map;
  }, [leftSidebarRegion]);
  const firstPartyIds = DISCOVER_TAB_CONTRIBUTION_IDS[tabId as DiscoverTab];
  const contributionIds = firstPartyIds ?? [tabId];
  return (
    <>
      {contributionIds.map((contributionId) => {
        const contribution = contributionById.get(contributionId);
        if (!contribution || (contribution.status !== 'enabled' && contribution.status !== 'disabled')) {
          return null;
        }
        return (
          <div
            key={contributionId}
            className="discover-contribution"
            data-testid={`discover-contribution-${contributionTestSlug(contributionId)}`}
            data-contribution-id={contributionId}
          >
            <SidebarContributionBody contribution={contribution} />
          </div>
        );
      })}
    </>
  );
}

const WorkspaceDiscoverRegion = memo(function WorkspaceDiscoverRegion() {
  useRenderCount('discover');
  const { project, setDiscoverTab, setDiscoverOpen, openDiscover, actionPanelVisible, closeActionRoute } =
    useWorkspaceShell();
  const resolvedWorkbenchLayout = useResolvedWorkbenchLayout();
  const chrome = useChromeHandle();
  const discoverOpen = useSelector(chrome.store, (s) => s.discoverOpen);
  const discoverTab = useSelector(chrome.store, (s) => s.discoverTab);
  const detailHandle = useDetailHandle();

  const discoverRevealKey = `${discoverOpen ? '1' : '0'}:${discoverTab}`;
  const previousDiscoverRevealKey = useRef(discoverRevealKey);
  useEffect(() => {
    if (previousDiscoverRevealKey.current === discoverRevealKey) return;
    previousDiscoverRevealKey.current = discoverRevealKey;
    if (actionPanelVisible) {
      chrome.hideActionPanel();
      detailHandle.setProposalInspect(null);
      closeActionRoute();
    }
  }, [discoverRevealKey, actionPanelVisible, chrome, detailHandle, closeActionRoute]);

  const leftSidebarRegion = resolveRegionContributions(resolvedWorkbenchLayout, 'leftSidebar');

  const pluginDiscoverContributions = useMemo(
    () =>
      leftSidebarRegion.contributions.filter(
        (c) =>
          c.runtimeSource === 'runtimeIndex' &&
          (c.status === 'enabled' || c.status === 'disabled'),
      ),
    [leftSidebarRegion],
  );

  const tabs = useMemo<DiscoverTabDescriptor[]>(() => {
    const firstParty = DISCOVER_TABS.map((tab) => ({
      id: tab,
      label: tab === 'Facets' ? 'Filter' : tab,
      slug: tab,
      Icon: DISCOVER_TAB_ICON[tab],
    }));
    const plugin = pluginDiscoverContributions.map((c) => ({
      id: c.contributionId,
      label: c.shortTitle ?? c.title,
      slug: contributionTestSlug(c.contributionId),
      Icon: pluginTabIcon(c.icon),
    }));
    return [...firstParty, ...plugin];
  }, [pluginDiscoverContributions]);

  const activeTab = tabs.some((tab) => tab.id === discoverTab)
    ? discoverTab
    : tabs[0]?.id ?? 'Facets';

  const discoverRegion = filterRegionContributions(
    leftSidebarRegion,
    (id) => !NON_DISCOVER_LEFT_SIDEBAR_CONTRIBUTION_IDS.has(id),
  );

  return (
    <section
      className="workbench-region workbench-discover"
      data-testid="workbench-region-discover"
      aria-label="Discover"
      data-discover-open={discoverOpen ? 'true' : 'false'}
    >
      <ResolvedWorkbenchLayoutRegion region={discoverRegion}>
        <DiscoverPanel
          projectId={project.id}
          open={discoverOpen}
          tabs={tabs}
          activeTab={activeTab}
          onSelectTab={setDiscoverTab}
          onCollapse={() => setDiscoverOpen(false)}
          onExpand={() => setDiscoverOpen(true)}
          onOpenTab={openDiscover}
          TabBody={DiscoverTabBody}
        />
      </ResolvedWorkbenchLayoutRegion>
    </section>
  );
});

const WorkspaceNavigateRegion = memo(function WorkspaceNavigateRegion() {
  useRenderCount('navigate');
  return (
    <section
      className="workbench-region workbench-navigate"
      data-testid="workbench-region-navigate"
      aria-label="Navigate"
    >
      <WorkspaceMainViewTabs />
    </section>
  );
});

const WorkspaceMainViewRegion = memo(function WorkspaceMainViewRegion() {
  useRenderCount('mainView');
  const {
    evidenceMainViewHidden,
    evidenceViewerState,
    graphSplitShowing,
    mapSplitShowing,
    mapContributionId,
    resolvedRegion,
    routePanel,
    sourceHealthHidden,
  } = useMainViewModel();

  return (
    <section
      className="workbench-region workbench-main-view"
      data-testid="workbench-region-mainView"
      aria-label="Workbench main view"
      data-layout-state-schema="frisket.workbench.mainview_layout.v1"
      data-active-contribution-id={
        routePanel?.kind === 'sourceHealth' && !sourceHealthHidden
          ? SOURCE_HEALTH_CONTRIBUTION_ID
          : graphSplitShowing
            ? GRAPH_NEIGHBORHOOD_CONTRIBUTION_ID
            : evidenceViewerState?.host === 'mainView' && !evidenceMainViewHidden
              ? EVIDENCE_CONTRIBUTION_ID
              : mapSplitShowing && mapContributionId
                ? mapContributionId
                : 'frisket.core.view.grid'
      }
    >
      <ResolvedWorkbenchLayoutRegion region={resolvedRegion('mainView')}>
        <main className="workspace" data-testid="workbench-mainView-host">
          <WorkspaceMainViewBody />
        </main>
      </ResolvedWorkbenchLayoutRegion>
    </section>
  );
});

function WorkspaceMainViewBody() {
  const {
    activePreviewView,
    routePanel,
    sheet,
    sourceHealthHidden,
    ocrCompareOpen,
    ocrCompareActive,
    transcribeCompareOpen,
    transcribeCompareActive,
    translateCompareOpen,
    translateCompareActive,
    topicCompareOpen,
    topicCompareActive,
  } = useMainViewModel();

  // Compare sessions are scratch state and must never be persisted.
  const body =
    activePreviewView?.result?.kind === 'table' ? <WorkspaceSheetMainView /> :
    routePanel?.kind === 'sourceHealth' && !sourceHealthHidden ? (
      <WorkspaceSourceHealthMainView />
    ) : sheet || activePreviewView ? (
      <WorkspaceSheetMainView />
    ) : (
      <WorkspaceImportMainView />
    );
  return (
    <>
      {ocrCompareOpen && (
        <div className="workbench-ocr-compare-keepalive" hidden={!ocrCompareActive}>
          <WorkspaceOcrCompareView />
        </div>
      )}
      {transcribeCompareOpen && (
        <div className="workbench-ocr-compare-keepalive" hidden={!transcribeCompareActive}>
          <WorkspaceTranscribeCompareView />
        </div>
      )}
      {translateCompareOpen && (
        <div className="workbench-ocr-compare-keepalive" hidden={!translateCompareActive}>
          <WorkspaceTranslateCompareView />
        </div>
      )}
      {topicCompareOpen && (
        <div className="workbench-ocr-compare-keepalive" hidden={!topicCompareActive}>
          <WorkspaceTopicCompareView />
        </div>
      )}
      {!ocrCompareActive &&
        !transcribeCompareActive &&
        !translateCompareActive &&
        !topicCompareActive &&
        body}
    </>
  );
}

function WorkspaceOcrCompareView() {
  const {
    ocrCompareTarget,
    ocrCompareActive,
    ocrCompareSession,
    ocrCompareCloseWarn,
    setOcrCompareSession,
    setOcrCompareCloseWarn,
    closeOcrCompareTab,
  } = useMainViewModel();
  const [copied, setCopied] = useState(false);
  return (
    <div className="workbench-ocr-compare-view" data-testid="workbench-ocr-compare-view">
      <OcrCompareTab active={ocrCompareActive} target={ocrCompareTarget} onSessionChange={setOcrCompareSession} />
      {ocrCompareCloseWarn && (
        <div className="ocr-compare-discard-backdrop" data-testid="ocr-compare-discard-warning">
          <dialog
            open
            className="ocr-compare-discard-card"
            role="alertdialog"
            aria-modal="true"
            aria-label="Discard this OCR comparison?"
            style={{ position: 'static', margin: 0 }}
          >
            <strong>Discard this OCR comparison?</strong>
            <p className="muted">
              Leaving discards the preview text and your votes — nothing is saved to the project.
              Copy the verdict first if you want to keep it.
            </p>
            <div className="ocr-compare-discard-verdict mono">{ocrCompareSession.verdict}</div>
            <div className="ocr-compare-discard-actions">
              <button
                type="button"
                className="btn"
                data-testid="ocr-compare-copy-verdict"
                onClick={() => {
                  void navigator.clipboard?.writeText(ocrCompareSession.verdict).catch(() => undefined);
                  setCopied(true);
                }}
              >
                {copied ? 'Copied' : 'Copy verdict'}
              </button>
              <span className="ocr-compare-discard-spacer" />
              <button
                type="button"
                className="btn"
                data-testid="ocr-compare-discard-cancel"
                onClick={() => setOcrCompareCloseWarn(false)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn btn-danger"
                data-testid="ocr-compare-discard-confirm"
                onClick={closeOcrCompareTab}
              >
                Discard
              </button>
            </div>
          </dialog>
        </div>
      )}
    </div>
  );
}

function WorkspaceTranscribeCompareView() {
  const {
    transcribeCompareActive,
    transcribeCompareSession,
    transcribeCompareCloseWarn,
    setTranscribeCompareSession,
    setTranscribeCompareCloseWarn,
    closeTranscribeCompareTab,
  } = useMainViewModel();
  const [copied, setCopied] = useState(false);
  return (
    <div className="workbench-ocr-compare-view" data-testid="workbench-transcribe-compare-view">
      <TranscribeCompareTab active={transcribeCompareActive} onSessionChange={setTranscribeCompareSession} />
      {transcribeCompareCloseWarn && (
        <div
          className="ocr-compare-discard-backdrop"
          data-testid="transcribe-compare-discard-warning"
        >
          <dialog
            open
            className="ocr-compare-discard-card"
            role="alertdialog"
            aria-modal="true"
            aria-label="Discard this scratch session?"
            style={{ position: 'static', margin: 0 }}
          >
            <strong>Discard this scratch session?</strong>
            <p className="muted">
              Leaving discards the transcripts and your votes — nothing is saved to the project.
              Copy the verdict first if you want to keep it.
            </p>
            <div className="ocr-compare-discard-verdict mono">
              {transcribeCompareSession.verdict}
            </div>
            <div className="ocr-compare-discard-actions">
              <button
                type="button"
                className="btn"
                data-testid="transcribe-compare-copy-verdict"
                onClick={() => {
                  void navigator.clipboard
                    ?.writeText(transcribeCompareSession.verdict)
                    .catch(() => undefined);
                  setCopied(true);
                }}
              >
                {copied ? 'Copied' : 'Copy verdict'}
              </button>
              <span className="ocr-compare-discard-spacer" />
              <button
                type="button"
                className="btn"
                data-testid="transcribe-compare-discard-cancel"
                onClick={() => setTranscribeCompareCloseWarn(false)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn btn-danger"
                data-testid="transcribe-compare-discard-confirm"
                onClick={closeTranscribeCompareTab}
              >
                Discard
              </button>
            </div>
          </dialog>
        </div>
      )}
    </div>
  );
}

function WorkspaceTranslateCompareView() {
  const {
    translateCompareSession,
    translateCompareCloseWarn,
    setTranslateCompareSession,
    setTranslateCompareCloseWarn,
    closeTranslateCompareTab,
  } = useMainViewModel();
  const [copied, setCopied] = useState(false);
  return (
    <div className="workbench-ocr-compare-view" data-testid="workbench-translate-compare-view">
      <TranslateCompareTab onSessionChange={setTranslateCompareSession} />
      {translateCompareCloseWarn && (
        <div
          className="ocr-compare-discard-backdrop"
          data-testid="translate-compare-discard-warning"
        >
          <dialog
            open
            className="ocr-compare-discard-card"
            role="alertdialog"
            aria-modal="true"
            aria-label="Discard this scratch session?"
            style={{ position: 'static', margin: 0 }}
          >
            <strong>Discard this scratch session?</strong>
            <p className="muted">
              Leaving discards the sample translations — nothing is saved to the project.
              Copy the verdict first if you want to keep it.
            </p>
            <div className="ocr-compare-discard-verdict mono">
              {translateCompareSession.verdict}
            </div>
            <div className="ocr-compare-discard-actions">
              <button
                type="button"
                className="btn"
                data-testid="translate-compare-copy-verdict"
                onClick={() => {
                  void navigator.clipboard
                    ?.writeText(translateCompareSession.verdict)
                    .catch(() => undefined);
                  setCopied(true);
                }}
              >
                {copied ? 'Copied' : 'Copy verdict'}
              </button>
              <span className="ocr-compare-discard-spacer" />
              <button
                type="button"
                className="btn"
                data-testid="translate-compare-discard-cancel"
                onClick={() => setTranslateCompareCloseWarn(false)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn btn-danger"
                data-testid="translate-compare-discard-confirm"
                onClick={closeTranslateCompareTab}
              >
                Discard
              </button>
            </div>
          </dialog>
        </div>
      )}
    </div>
  );
}

function WorkspaceTopicCompareView() {
  const {
    topicCompareSession,
    topicCompareCloseWarn,
    setTopicCompareSession,
    setTopicCompareCloseWarn,
    closeTopicCompareTab,
  } = useMainViewModel();
  const [copied, setCopied] = useState(false);
  return (
    <div className="workbench-ocr-compare-view" data-testid="workbench-topic-compare-view">
      <TopicSegmentationCompareTab onSessionChange={setTopicCompareSession} />
      {topicCompareCloseWarn && (
        <div
          className="ocr-compare-discard-backdrop"
          data-testid="topic-compare-discard-warning"
        >
          <dialog
            open
            className="ocr-compare-discard-card"
            role="alertdialog"
            aria-modal="true"
            aria-label="Discard this scratch session?"
            style={{ position: 'static', margin: 0 }}
          >
            <strong>Discard this scratch session?</strong>
            <p className="muted">
              Leaving discards the topic sections and your votes — nothing is saved to the
              project. Copy the verdict first if you want to keep it.
            </p>
            <div className="ocr-compare-discard-verdict mono">
              {topicCompareSession.verdict}
            </div>
            <div className="ocr-compare-discard-actions">
              <button
                type="button"
                className="btn"
                data-testid="topic-compare-copy-verdict"
                onClick={() => {
                  void navigator.clipboard
                    ?.writeText(topicCompareSession.verdict)
                    .catch(() => undefined);
                  setCopied(true);
                }}
              >
                {copied ? 'Copied' : 'Copy verdict'}
              </button>
              <span className="ocr-compare-discard-spacer" />
              <button
                type="button"
                className="btn"
                data-testid="topic-compare-discard-cancel"
                onClick={() => setTopicCompareCloseWarn(false)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn btn-danger"
                data-testid="topic-compare-discard-confirm"
                onClick={closeTopicCompareTab}
              >
                Discard
              </button>
            </div>
          </dialog>
        </div>
      )}
    </div>
  );
}

function WorkspaceSheetMainView() {
  const { activePromotedView, activePreviewView } = useMainViewModel();

  if (activePromotedView && activePreviewView?.result?.kind !== 'table') {
    return <WorkspacePromotedView />;
  }
  return (
    <>
      <WorkspaceSheetToolbar />
      <WorkspaceGridControls />
      <WorkspaceInlineFilterRow />
      <WorkspaceGridBanners />
      <WorkspacePrimarySurface />
      <WorkspaceHeaderMenuSlot />
    </>
  );
}

const WorkspaceMainViewTabs = memo(function WorkspaceMainViewTabs() {
  const {
    onMainViewTabKeyDown,
    closePreviewView,
    activePromotedView,
    openPromotedTab,
    closePromotedTab,
    selectSheetTab,
    selectSheet,
    refreshSheets,
  } = useWorkspaceShell();
  const { sheet, sheets } = useCurrentSheet();
  const actSurface = useActSurfaceHandle();
  const openImportDialog = actSurface.openImportDialog;
  const workView = useWorkViewHandle();
  const compareView = useCompareViewHandle();
  const previewViewHandle = usePreviewViewHandle();
  const activePreviewView = useSelector(previewViewHandle.store, (s) =>
    previewViewForSheet(s.previewView, sheet?.id),
  );
  const ocrCompareOpen = useSelector(compareView.store, (s) => s.ocr.open);
  const ocrCompareActive = useSelector(compareView.store, (s) => s.ocr.active);
  const focusOcrCompareTab = useCallback(
    () => focusCompareTabTransition(workView, compareView, 'ocr'),
    [workView, compareView],
  );
  const requestCloseOcrCompareTab = useCallback(
    () => compareView.requestClose('ocr'),
    [compareView],
  );
  const transcribeCompareOpen = useSelector(compareView.store, (s) => s.transcribe.open);
  const transcribeCompareActive = useSelector(compareView.store, (s) => s.transcribe.active);
  const focusTranscribeCompareTab = useCallback(
    () => focusCompareTabTransition(workView, compareView, 'transcribe'),
    [workView, compareView],
  );
  const requestCloseTranscribeCompareTab = useCallback(
    () => compareView.requestClose('transcribe'),
    [compareView],
  );
  const translateCompareOpen = useSelector(compareView.store, (s) => s.translate.open);
  const translateCompareActive = useSelector(compareView.store, (s) => s.translate.active);
  const focusTranslateCompareTab = useCallback(
    () => focusCompareTabTransition(workView, compareView, 'translate'),
    [workView, compareView],
  );
  const requestCloseTranslateCompareTab = useCallback(
    () => compareView.requestClose('translate'),
    [compareView],
  );
  const topicCompareOpen = useSelector(compareView.store, (s) => s.topic.open);
  const topicCompareActive = useSelector(compareView.store, (s) => s.topic.active);
  const focusTopicCompareTab = useCallback(
    () => focusCompareTabTransition(workView, compareView, 'topic'),
    [workView, compareView],
  );
  const requestCloseTopicCompareTab = useCallback(
    () => compareView.requestClose('topic'),
    [compareView],
  );
  const chrome = useChromeHandle();
  const { projectApi } = useWorkspaceStores();

  const promotedViews = useSelector(chrome.store, (s) => s.promotedViews);
  const setActiveBottomDockTab = chrome.setActiveBottomDockTab;

  const [infoSheetId, setInfoSheetId] = useState<string | null>(null);
  const infoSheet = sheets.find((s) => s.id === infoSheetId) ?? null;
  const [deleteSheetId, setDeleteSheetId] = useState<string | null>(null);
  const [deleteSheetBusy, setDeleteSheetBusy] = useState(false);
  const [deleteSheetError, setDeleteSheetError] = useState<string | null>(null);
  const deleteSheet = sheets.find((candidate) => candidate.id === deleteSheetId) ?? null;
  const deleteSheetDependents = deleteSheet
    ? (deleteSheet.dependentSheetIds ?? []).flatMap((id) => {
        const dependent = sheets.find((candidate) => candidate.id === id);
        return dependent ? [dependent.name] : [];
      })
    : [];

  const confirmDeleteSheet = useCallback(async () => {
    if (!deleteSheet || deleteSheetBusy || deleteSheetDependents.length > 0) return;
    setDeleteSheetBusy(true);
    setDeleteSheetError(null);
    try {
      await projectApi.deleteSheet(deleteSheet.id);
      setDeleteSheetId(null);
      await refreshSheets();
    } catch (error) {
      setDeleteSheetError(error instanceof Error ? error.message : String(error));
    } finally {
      setDeleteSheetBusy(false);
    }
  }, [deleteSheet, deleteSheetBusy, deleteSheetDependents.length, projectApi, refreshSheets]);

  // Preserve deepest-first cascade order.
  const staleDeepestFirst = staleSheetsDeepestFirst(sheets);

  return (
    <>
      <div
        className="workbench-mainView-tabs"
        data-testid="workbench-mainView-tabs"
        data-host="mainView"
        data-mode="tab"
      >
        <div className="workbench-mainView-tab-list" role="tablist" aria-label="Open work surfaces">
          {sheets.map((tabSheet) => {
            const active =
              sheet?.id === tabSheet.id &&
              !activePromotedView &&
              !ocrCompareActive &&
              !transcribeCompareActive &&
              !translateCompareActive &&
              !topicCompareActive;
            const derived = Boolean(tabSheet.parent);
            return (
              <button
                key={tabSheet.id}
                type="button"
                role="tab"
                className={`workbench-mainView-tab${active ? ' active' : ''}`}
                aria-selected={active}
                data-active={active ? 'true' : undefined}
                data-derived={derived ? 'true' : undefined}
                data-testid={`workbench-mainView-tab-${tabSheet.id}`}
                data-contribution-id="frisket.core.view.grid"
                data-host="mainView"
                data-mode="tab"
                tabIndex={active ? 0 : -1}
                onClick={() => selectSheetTab(tabSheet.id)}
                onKeyDown={(event) => onMainViewTabKeyDown(event, tabSheet.id)}
              >
                {derived ? (
                  <GitFork
                    size={13}
                    className="workbench-mainView-tab-glyph"
                    data-testid={`workbench-mainView-tab-derived-${tabSheet.id}`}
                    aria-label="Derived sheet"
                  />
                ) : (
                  <SheetGlyph size={13} className="workbench-mainView-tab-glyph" aria-hidden />
                )}
                <span className="workbench-mainView-tab-name">{tabSheet.name}</span>
                {derived && tabSheet.syncState && (
                  <span
                    className="workbench-mainView-tab-syncDot"
                    data-sync-state={tabSheet.syncState}
                    data-testid={`workbench-mainView-tab-syncDot-${tabSheet.id}`}
                    aria-label={
                      tabSheet.syncState === 'stale'
                        ? 'Stale — parent changed'
                        : 'Live · in sync'
                    }
                    title={
                      tabSheet.syncState === 'stale'
                        ? 'Stale — parent changed'
                        : 'Live · in sync'
                    }
                  />
                )}
                {active && (
                  <span
                    className="workbench-mainView-tab-rowcount"
                    data-testid={`workbench-mainView-tab-rowcount-${tabSheet.id}`}
                  >
                    {tabSheet.rowCount.toLocaleString()}
                  </span>
                )}
                {active && derived && (

                  // HTML forbids nested buttons.

                  <span
                    role="button"
                    tabIndex={0}
                    className="workbench-mainView-tab-info"
                    data-testid={`workbench-mainView-tab-info-${tabSheet.id}`}
                    aria-label="Sheet info"
                    title="Sheet info"
                    onClick={(event) => {
                      event.stopPropagation();
                      setInfoSheetId((cur) => (cur === tabSheet.id ? null : tabSheet.id));
                    }}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault();
                        event.stopPropagation();
                        setInfoSheetId((cur) => (cur === tabSheet.id ? null : tabSheet.id));
                      }
                    }}
                  >
                    <Info size={12} aria-hidden />
                  </span>
                )}
                {active && (
                  <span
                    role="button"
                    tabIndex={0}
                    className="workbench-mainView-tab-info"
                    data-testid={`workbench-mainView-tab-delete-${tabSheet.id}`}
                    aria-label={`Delete ${tabSheet.name}`}
                    title="Delete sheet"
                    onClick={(event) => {
                      event.stopPropagation();
                      setDeleteSheetError(null);
                      setDeleteSheetId(tabSheet.id);
                    }}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault();
                        event.stopPropagation();
                        setDeleteSheetError(null);
                        setDeleteSheetId(tabSheet.id);
                      }
                    }}
                  >
                    <Trash2 size={12} aria-hidden />
                  </span>
                )}
              </button>
            );
          })}
          {activePreviewView && (
            <span
              className="workbench-mainView-tab workbench-mainView-previewTab active"
              role="tab"
              aria-selected="true"
              data-testid="workbench-mainView-previewTab"
            >
              <span className="preview-tab-glyph" aria-hidden />
              {activePreviewView.sheetId && sheet ? `${sheet.name} · ` : ''}{activePreviewView.actionName} Preview
              <button
                type="button"
                className="preview-tab-close"
                aria-label="Close preview"
                data-testid="preview-tab-close"
                onClick={(event) => {
                  event.stopPropagation();

                  closePreviewView();
                }}
              >
                <X size={13} />
              </button>
            </span>
          )}
          {promotedViews.map((view) => {
            const active = activePromotedView?.key === view.key;
            return (
              <span
                key={view.key}
                role="tab"
                tabIndex={0}
                className={`workbench-mainView-tab workbench-mainView-promotedTab${active ? ' active' : ''}`}
                aria-selected={active}
                data-active={active ? 'true' : undefined}
                data-testid="workbench-mainView-promotedTab"
                data-promoted-key={view.key}
                data-view-kind={view.kind}
                data-host="mainView"
                data-mode="tab"
                onClick={() => openPromotedTab(view)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    openPromotedTab(view);
                  }
                }}
              >
                {view.kind === 'map' ? (
                  <MapPin size={13} className="workbench-mainView-tab-glyph" aria-hidden />
                ) : view.kind === 'gallery' ? (
                  <Images size={13} className="workbench-mainView-tab-glyph" aria-hidden />
                ) : (
                  <Network size={13} className="workbench-mainView-tab-glyph" aria-hidden />
                )}
                <span className="workbench-mainView-tab-name workbench-promotedTab-label">
                  {view.label}
                </span>
                <button
                  type="button"
                  className="preview-tab-close"
                  aria-label={`Close ${view.label}`}
                  data-testid="workbench-mainView-promotedTab-close"
                  data-promoted-key={view.key}
                  onClick={(event) => {
                    event.stopPropagation();
                    closePromotedTab(view);
                  }}
                >
                  <X size={13} />
                </button>
              </span>
            );
          })}
          {ocrCompareOpen && (
            <span
              role="tab"
              tabIndex={0}
              className={`workbench-mainView-tab workbench-mainView-promotedTab${
                ocrCompareActive ? ' active' : ''
              }`}
              aria-selected={ocrCompareActive}
              data-active={ocrCompareActive ? 'true' : undefined}
              data-testid="ocr-compare-maintab"
              data-host="mainView"
              data-mode="tab"
              onClick={focusOcrCompareTab}
              onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault();
                  focusOcrCompareTab();
                }
              }}
            >
              <ScanText size={13} className="workbench-mainView-tab-glyph" aria-hidden />
              <span className="workbench-mainView-tab-name workbench-promotedTab-label">
                OCR compare
              </span>
              <button
                type="button"
                className="preview-tab-close"
                aria-label="Close OCR compare"
                data-testid="ocr-compare-maintab-close"
                onClick={(event) => {
                  event.stopPropagation();
                  requestCloseOcrCompareTab();
                }}
              >
                <X size={13} />
              </button>
            </span>
          )}
          {transcribeCompareOpen && (
            <span
              role="tab"
              tabIndex={0}
              className={`workbench-mainView-tab workbench-mainView-promotedTab${
                transcribeCompareActive ? ' active' : ''
              }`}
              aria-selected={transcribeCompareActive}
              data-active={transcribeCompareActive ? 'true' : undefined}
              data-testid="transcribe-compare-maintab"
              data-host="mainView"
              data-mode="tab"
              onClick={focusTranscribeCompareTab}
              onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault();
                  focusTranscribeCompareTab();
                }
              }}
            >
              <AudioLines size={13} className="workbench-mainView-tab-glyph" aria-hidden />
              <span className="workbench-mainView-tab-name workbench-promotedTab-label">
                Transcribe compare
              </span>
              <button
                type="button"
                className="preview-tab-close"
                aria-label="Close Transcribe compare"
                data-testid="transcribe-compare-maintab-close"
                onClick={(event) => {
                  event.stopPropagation();
                  requestCloseTranscribeCompareTab();
                }}
              >
                <X size={13} />
              </button>
            </span>
          )}
          {translateCompareOpen && (
            <span
              role="tab"
              tabIndex={0}
              className={`workbench-mainView-tab workbench-mainView-promotedTab${
                translateCompareActive ? ' active' : ''
              }`}
              aria-selected={translateCompareActive}
              data-active={translateCompareActive ? 'true' : undefined}
              data-testid="translate-compare-maintab"
              data-host="mainView"
              data-mode="tab"
              onClick={focusTranslateCompareTab}
              onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault();
                  focusTranslateCompareTab();
                }
              }}
            >
              <Languages size={13} className="workbench-mainView-tab-glyph" aria-hidden />
              <span className="workbench-mainView-tab-name workbench-promotedTab-label">
                Translate compare
              </span>
              <button
                type="button"
                className="preview-tab-close"
                aria-label="Close Translate compare"
                data-testid="translate-compare-maintab-close"
                onClick={(event) => {
                  event.stopPropagation();
                  requestCloseTranslateCompareTab();
                }}
              >
                <X size={13} />
              </button>
            </span>
          )}
          {topicCompareOpen && (
            <span
              role="tab"
              tabIndex={0}
              className={`workbench-mainView-tab workbench-mainView-promotedTab${
                topicCompareActive ? ' active' : ''
              }`}
              aria-selected={topicCompareActive}
              data-active={topicCompareActive ? 'true' : undefined}
              data-testid="topic-compare-maintab"
              data-host="mainView"
              data-mode="tab"
              onClick={focusTopicCompareTab}
              onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault();
                  focusTopicCompareTab();
                }
              }}
            >
              <Rows3 size={13} className="workbench-mainView-tab-glyph" aria-hidden />
              <span className="workbench-mainView-tab-name workbench-promotedTab-label">
                Topic compare
              </span>
              <button
                type="button"
                className="preview-tab-close"
                aria-label="Close Topic compare"
                data-testid="topic-compare-maintab-close"
                onClick={(event) => {
                  event.stopPropagation();
                  requestCloseTopicCompareTab();
                }}
              >
                <X size={13} />
              </button>
            </span>
          )}
          <AddSheetButton onClick={openImportDialog} />
        </div>
        {staleDeepestFirst.length > 0 && (
          <SheetStalePill
            staleDeepestFirst={staleDeepestFirst}
            refreshSheets={refreshSheets}
          />
        )}
      </div>
      {deleteSheet && (
        <ConfirmDeleteSheetModal
          sheetName={deleteSheet.name}
          dependentSheetNames={deleteSheetDependents}
          busy={deleteSheetBusy}
          error={deleteSheetError}
          onConfirm={() => void confirmDeleteSheet()}
          onCancel={() => {
            if (!deleteSheetBusy) setDeleteSheetId(null);
          }}
        />
      )}
      {infoSheet && infoSheet.parent && (
        <SheetInfoPopover
          sheet={infoSheet}
          onClose={() => setInfoSheetId(null)}
          onNavigateParent={(parentId) => {
            setInfoSheetId(null);
            selectSheet(parentId);
          }}
          onViewLineage={() => {
            setInfoSheetId(null);
            setActiveBottomDockTab('lineage');
          }}
          refreshSheets={refreshSheets}
        />
      )}
    </>
  );
});

function SheetStalePill({
  staleDeepestFirst,
  refreshSheets,
}: {
  staleDeepestFirst: SheetMeta[];
  refreshSheets: () => void | Promise<void>;
}) {
  const [confirmOpen, setConfirmOpen] = useState(false);

  return (
    <>
      <button
        type="button"
        className="workbench-mainView-stalePill"
        data-testid="workbench-mainView-stalePill"
        onClick={() => setConfirmOpen(true)}
      >
        <RotateCw size={12} aria-hidden />
        Parent changed · re-run {staleDeepestFirst.length}
      </button>
      {confirmOpen && (
        <CascadeConfirmDialog
          staleDeepestFirst={staleDeepestFirst}
          onClose={() => setConfirmOpen(false)}
          refreshSheets={refreshSheets}
        />
      )}
    </>
  );
}

function SheetInfoPopover({
  sheet,
  onClose,
  onNavigateParent,
  onViewLineage,
  refreshSheets,
}: {
  sheet: SheetMeta;
  onClose: () => void;
  onNavigateParent: (parentId: string) => void;
  onViewLineage: () => void;
  refreshSheets: () => void | Promise<void>;
}) {
  const { projectApi } = useWorkspaceStores();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [costGate, setCostGate] = useState<{
    message: string;
    estimate: RunEstimate;
  } | null>(null);
  const sheetRefreshQuote = quotedCost(costGate?.estimate);
  const stale = sheet.syncState === 'stale';
  const parent = sheet.parent;

  useEscapeDismiss(onClose);

  const runReRun = useCallback(
    async (confirmation?: string) => {
      setBusy(true);
      setError(null);
      try {
        await projectApi.refreshSheet(sheet.id, confirmation);
        await refreshSheets();
        onClose();
      } catch (err) {
        if (err instanceof ConfirmationRequiredError) {
          setCostGate({
            message: err.message,
            estimate: err.estimate,
          });
        } else {
          setError(err instanceof Error ? err.message : 'Refresh failed');
        }
      } finally {
        setBusy(false);
      }
    },
    [sheet.id, refreshSheets, onClose],
  );

  return (

    <div
      className="sheet-info-backdrop"
      data-testid="sheet-info-backdrop"
      onClick={onClose}
    >
      <dialog
        open
        className="sheet-info-popover"
        data-testid="sheet-info-popover"
        aria-label={`${sheet.name} info`}

        style={{ position: 'static', margin: 0 }}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="sheet-info-head">
          <span className="sheet-info-name">{sheet.name}</span>
          <StatusChip tone={stale ? 'warning' : 'success'} size="md" testId="sheet-info-badge">
            {stale ? 'Stale' : 'Live · in sync'}
          </StatusChip>
        </div>
        <div className="sheet-info-section">
          <div className="sheet-info-label">HOW IT&apos;S MADE</div>
          <div className="sheet-info-value" data-testid="sheet-info-transform">
            {parent?.viaAction}
          </div>
        </div>
        <div className="sheet-info-section">
          <div className="sheet-info-label">LINEAGE</div>
          <button
            type="button"
            className="sheet-info-parent-link"
            data-testid="sheet-info-parent-link"
            onClick={() => parent && onNavigateParent(parent.sheetId)}
          >
            ← {parent?.sheetName}
          </button>
        </div>

        <p className="muted" data-testid="sheet-info-rerun-scope">
          Re-running rebuilds this sheet from{' '}
          {parent?.sheetName ?? 'its parent'} — every row is replaced, not just
          the changed ones. That makes it a new run, not a resume: every
          derived row is charged again.
        </p>
        {error && <div className="sheet-info-error">{error}</div>}
        {costGate && (
          <div className="sheet-info-costGate" data-testid="sheet-info-cost-confirm">
            <div className="sheet-info-costGate-message">
              This re-run calls a model
              {/* Display the exact server-hashed consent cost. */}
              {sheetRefreshQuote.usd != null
                ? ` · est. ${formatUsd(sheetRefreshQuote.usd)}`
                : sheetRefreshQuote.refused
                  ? ' · est. UNKNOWN'
                  : ''}
            </div>
            <button
              type="button"
              className="btn btn-primary"
              data-testid="sheet-info-cost-confirm-run"
              disabled={busy}
              onClick={() =>
                void runReRun(costGate.estimate.promise_set_hash)
              }
            >
              {busy ? 'Re-running…' : 'Confirm re-run'}
            </button>
          </div>
        )}
        <div className="sheet-info-footer">
          <button
            type="button"
            className="btn btn-ghost"
            data-testid="sheet-info-view-lineage"
            onClick={onViewLineage}
          >
            View lineage
          </button>
          <button
            type="button"
            className="btn btn-primary"
            data-testid="sheet-info-rerun"
            disabled={busy || costGate != null}
            onClick={() => void runReRun()}
          >
            {busy ? 'Re-running…' : 'Re-run now'}
          </button>
        </div>
      </dialog>
    </div>
  );
}

function WorkspaceSourceHealthMainView() {
  const { projectApi } = useWorkspaceStores();
  const {
    closeRoutePanel,
    refreshHistory,
    refreshSheets,
    routePanel,
    selectSheet,
    sourceHealthHidden,
    invalidateProjectData,
  } = useMainViewModel();

  if (routePanel?.kind !== 'sourceHealth' || sourceHealthHidden) {
    return null;
  }

  return (
            <SourceHealthMainViewFrame>
              <SourceHealthMainView
                sourceApi={projectApi}
                key={routePanel.sourceId}
                sourceId={Number(routePanel.sourceId)}
                onClose={closeRoutePanel}
                onPolled={(sheetId) => {
                  void refreshSheets().then(() => {
                    if (sheetId != null) selectSheet(String(sheetId));
                  });
                  void refreshHistory();
                  invalidateProjectData();
                }}
              />
            </SourceHealthMainViewFrame>
  );
}

const WORK_VIEW_SWITCH: Array<{ kind: WorkViewKind; label: string; Icon: LucideIcon }> = [
  { kind: 'grid', label: 'Grid', Icon: LayoutGrid },
  { kind: 'document', label: 'Document', Icon: FileText },
  { kind: 'answers', label: 'Answers', Icon: MessageSquareQuote },
  { kind: 'map', label: 'Map', Icon: MapPin },
  { kind: 'gallery', label: 'Gallery', Icon: Images },
  { kind: 'graph', label: 'Graph', Icon: Network },
];

function WorkViewSwitcher({
  active,
  segments,
  disabledReasons,
  onSelect,
}: {
  active: WorkViewKind;
  segments: Record<WorkViewKind, boolean>;

  disabledReasons?: Partial<Record<WorkViewKind, string>>;
  onSelect: (kind: WorkViewKind) => void;
}) {
  const options: Array<{
    value: WorkViewKind;
    label: string;
    icon: LucideIcon;
    disabledReason: string | undefined;
  }> = [];
  for (const segment of WORK_VIEW_SWITCH) {
    if (!segments[segment.kind] && !disabledReasons?.[segment.kind]) continue;
    options.push({
      value: segment.kind,
      label: segment.label,
      icon: segment.Icon,
      disabledReason: segments[segment.kind] ? undefined : disabledReasons?.[segment.kind],
    });
  }
  return (
    <SegmentedToggle
      className="segmented-toolbar"
      fullWidth={false}
      testId="view-switcher"
      ariaLabel="View"
      value={active}
      onValueChange={(value) => onSelect(value as WorkViewKind)}
      buttonTestId={(value) => `view-switch-${value}`}
      options={options}
    />
  );
}

function WorkspaceInlineFilterRow() {
  const {
    applyInlineFilter,
    sheet,
  } = useMainViewModel();
  const gridView = useGridViewHandle();
  const activeGridFilter = useSelector(gridView.store, (s) => s.applied.filter);
  const inlineFilterOpen = useSelector(gridView.store, (s) =>
    selectInlineFilterOpen(s, sheet?.id),
  );
  const inlineFilterColumn = useSelector(gridView.store, (s) =>
    selectInlineFilterColumn(s, sheet?.id),
  );
  if (!sheet) return null;
  const column = inlineFilterColumn || sheet.columns[0]?.name || '';
  const activeEntry = activeGridFilter?.[column]?.contains;

  const activeText = typeof activeEntry === 'string' ? activeEntry : '';
  if (!inlineFilterOpen) return null;
  return (
    <div className="inline-filter-row" data-testid="inline-filter-row">
      <span className="inline-filter-label mono">Filter</span>
      <PanelSelect
        className="row-height-select"
        data-testid="inline-filter-column"
        aria-label="Filter column"
        value={column}
        onChange={(event) => {
          gridView.setInlineFilterColumn(sheet.id, event.target.value);
        }}
      >
        {sheet.columns.map((col) => (
          <option key={col.id} value={col.name}>
            {col.name}
          </option>
        ))}
      </PanelSelect>
      <input
        className="inline-filter-input"
        data-testid="inline-filter-input"
        aria-label="Filter value"
        placeholder="contains…"
        value={activeText}
        onChange={(event) => {
          applyInlineFilter(column, event.target.value);
        }}
      />
      <button
        type="button"
        className="pill-btn"
        data-testid="inline-filter-clear"
        onClick={() => {
          applyInlineFilter(column, '');
        }}
      >
        clear
      </button>
      <button
        type="button"
        className="pill-btn"
        data-testid="inline-filter-done"
        onClick={gridView.closeInlineFilter}
      >
        done
      </button>
    </div>
  );
}

function WorkspaceSheetToolbar() {
  const {
    appendRow,
    requestDeleteRows,
    selectedRowIdsForSheet,
    sheet,
    toggleWrapText,
    activeWorkView,
    workViewSegments,
    workViewDisabledReasons,
    setWorkView,
    visibleRowCount,
    canCreateSavedView,
    openNewSavedView,
    openSavedViews,
    activeSavedViewId,
    views,
  } = useMainViewModel();
  const gridView = useGridViewHandle();
  const wrapText = useSelector(gridView.store, (s) => s.wrapText);
  if (!sheet) return null;

  const selectedRowCount = selectedRowIdsForSheet.length;

  return (
<>
              <div className="workspace-toolbar">
                <div className="sheet-heading">
                  <span className="sheet-title">{sheet.name}</span>
                  {sheet.parent && (
                    <span className="sheet-breadcrumb" data-testid="sheet-breadcrumb">
                      ← {sheet.parent.sheetName} via <em>{sheet.parent.viaAction}</em>
                    </span>
                  )}
                  <span className="muted" data-testid="sheet-stats">
                    {countLabel(visibleRowCount, 'row')} · {countLabel(sheet.columns.length, 'column')}
                  </span>
                  {activeSavedViewId !== null && views.find((view) => view.id === activeSavedViewId) && (
                    <button
                      type="button"
                      className="active-saved-view-chip"
                      data-testid="active-saved-view-chip"
                      onClick={openSavedViews}
                    >
                      Active view: {views.find((view) => view.id === activeSavedViewId)!.name}
                    </button>
                  )}
                  <WorkViewSwitcher
                    active={activeWorkView}
                    segments={workViewSegments}
                    disabledReasons={workViewDisabledReasons}
                    onSelect={setWorkView}
                  />
                  {activeWorkView !== 'grid' &&
                    activeWorkView !== 'document' &&
                    activeWorkView !== 'answers' && (
                    <span className="work-split-hint" data-testid="work-view-split-hint">
                      · split view
                    </span>
                  )}
                </div>
                <div className="toolbar-actions">
                  <button
                    type="button"
                    className="icon-btn"
                    data-testid="add-row-button"
                    title="Add row"
                    onClick={appendRow}
                  >
                    <Plus size={15} />
                  </button>

                  <DeleteRowsButton count={selectedRowCount} onClick={requestDeleteRows} />
                  <div className="toolbar-spacer" />

                  {canCreateSavedView && (
                    <button
                      type="button"
                      className="icon-btn"
                      data-testid="save-as-view"
                      title="Save current view"
                      aria-label="Save current view"
                      onClick={() => openNewSavedView()}
                    >
                      <BookOpen size={15} />
                    </button>
                  )}

                  <button
                    type="button"
                    className={`icon-btn${wrapText ? ' icon-btn-active' : ''}`}
                    data-testid="toggle-wrap"
                    title={wrapText ? 'Wrap text: on' : 'Wrap text: off'}
                    aria-label="Wrap text"
                    aria-pressed={wrapText}
                    onClick={toggleWrapText}
                  >
                    <WrapText size={15} />
                  </button>
                  <ToolbarOverflowMenu />
                </div>
              </div>
  </>
  );
}

interface SavedViewsSubmenuPosition {
  top: number;
  left: number;
  width: number;
  maxHeight: number;
}

function useSavedViewsSubmenuPosition(
  anchorRef: RefObject<HTMLElement | null>,
  enabled: boolean,
): SavedViewsSubmenuPosition | null {
  const [position, setPosition] = useState<SavedViewsSubmenuPosition | null>(null);

  useLayoutEffect(() => {
    if (!enabled) return undefined;
    const measure = () => {
      const anchor = anchorRef.current;
      if (!anchor) return;
      const margin = 8;
      const gap = 4;
      const width = Math.max(0, Math.min(280, window.innerWidth - margin * 2));
      const maxHeight = Math.max(0, Math.min(320, window.innerHeight - margin * 2));
      const rect = anchor.getBoundingClientRect();
      const rightwardLeft = rect.right + gap;
      const preferredLeft = rightwardLeft + width <= window.innerWidth - margin
        ? rightwardLeft
        : rect.left - gap - width;
      const left = Math.max(margin, Math.min(preferredLeft, window.innerWidth - margin - width));
      const top = Math.max(margin, Math.min(rect.top, window.innerHeight - margin - maxHeight));
      setPosition({ top, left, width, maxHeight });
    };
    measure();
    window.addEventListener('resize', measure);
    window.addEventListener('scroll', measure, true);
    return () => {
      window.removeEventListener('resize', measure);
      window.removeEventListener('scroll', measure, true);
    };
  }, [anchorRef, enabled]);

  return position;
}

function ToolbarOverflowMenu() {
  const {
    activeHiddenColumns,
    changeRowHeight,
    overflowMenuOpen,
    setOverflowMenuOpen,
    provenanceOpen,
    toggleProvenanceWithDrawers,
    revealAllHiddenColumns,
    canCreateSavedView,
    ensureViewsLoaded,
    openSavedViews,
    openNewSavedView,
    applySavedView,
    views,
    activeSavedViewId,
  } = useMainViewModel();
  const gridView = useGridViewHandle();
  const rowHeight = useSelector(gridView.store, (s) => s.rowHeight);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const rowHeightMenuRef = useRef<HTMLDivElement>(null);
  const savedViewsTriggerRef = useRef<HTMLButtonElement>(null);
  const savedViewsMenuRef = useRef<HTMLDivElement>(null);
  const savedViewsLoadRequestedRef = useRef(false);
  const suppressNextSavedViewsFocusOpenRef = useRef(false);
  const [savedViewsMenuOpen, setSavedViewsMenuOpen] = useState(false);
  const [savedViewsQuery, setSavedViewsQuery] = useState('');
  const [applyAnnouncement, setApplyAnnouncement] = useState('');

  const closeSavedViewsSubmenu = useCallback(() => {
    savedViewsLoadRequestedRef.current = false;
    setSavedViewsMenuOpen(false);
    setSavedViewsQuery('');
  }, []);

  const openSavedViewsSubmenu = useCallback(() => {
    if (savedViewsLoadRequestedRef.current) return;
    savedViewsLoadRequestedRef.current = true;
    setSavedViewsMenuOpen(true);
    void ensureViewsLoaded();
  }, [ensureViewsLoaded]);

  const setOverflowOpen = useCallback(
    (next: boolean) => {
      setOverflowMenuOpen(next);
      if (!next) {
        closeSavedViewsSubmenu();
      }
    },
    [closeSavedViewsSubmenu, setOverflowMenuOpen],
  );

  useNativePopover(menuRef, () => setOverflowOpen(false), {
    enabled: overflowMenuOpen,
    ignoreSelector: '[data-testid="toolbar-overflow-button"]',
    extraRefs: [rowHeightMenuRef, savedViewsMenuRef],
  });

  useNativePopover(savedViewsMenuRef, closeSavedViewsSubmenu, {
    enabled: overflowMenuOpen && savedViewsMenuOpen,
    escape: false,
    ignoreSelector: '[data-testid="open-saved-views-submenu"]',
  });

  const menuPos = useAnchoredPosition(triggerRef, {
    enabled: overflowMenuOpen,
    align: 'right',
    width: 210,
    gap: 4,
  });
  const savedViewsMenuPos = useSavedViewsSubmenuPosition(
    menuRef,
    overflowMenuOpen && savedViewsMenuOpen,
  );
  const normalizedViewsQuery = savedViewsQuery.trim().toLocaleLowerCase();
  const filteredViews = normalizedViewsQuery
    ? views.filter((view) => view.name.toLocaleLowerCase().includes(normalizedViewsQuery))
    : views;

  return (
    <div className="toolbar-overflow" ref={rootRef}>
      <button
        type="button"
        ref={triggerRef}
        className={`icon-btn${overflowMenuOpen ? ' icon-btn-active' : ''}`}
        data-testid="toolbar-overflow-button"
        title="More view options"
        aria-label="More view options"
        aria-haspopup="menu"
        aria-expanded={overflowMenuOpen}
        onClick={() => setOverflowOpen(!overflowMenuOpen)}
      >
        <MoreHorizontal size={15} />
      </button>
      {overflowMenuOpen && (
        <div
          ref={menuRef}
          className="menu-pop toolbar-overflow-menu"
          data-testid="toolbar-overflow-menu"
          role="menu"
          style={
            menuPos
              ? { position: 'fixed', inset: 'auto', top: menuPos.top, bottom: menuPos.bottom, left: menuPos.left, right: 'auto', width: menuPos.width, margin: 0 }
              : { position: 'fixed', visibility: 'hidden' }
          }
        >
          <div className="menu-item menu-item-select" title="Row height (applies to all rows)">
            <Rows3 size={13} className="menu-item-icon" aria-hidden />
            <span className="menu-item-name">Row height</span>
            <PanelSelect
              className="row-height-select"
              data-testid="row-height-select"
              aria-label="Row height"
              value={rowHeight}
              topLayer
              menuRef={rowHeightMenuRef}
              onChange={(e) => changeRowHeight(Number(e.target.value))}
            >
              {ROW_HEIGHTS.map((h) => (
                <option key={h.value} value={h.value}>{h.label}</option>
              ))}
            </PanelSelect>
          </div>
          <button
            type="button"
            ref={savedViewsTriggerRef}
            className="menu-item"
            data-testid="open-saved-views-submenu"
            aria-haspopup="menu"
            aria-expanded={savedViewsMenuOpen}
            onPointerEnter={openSavedViewsSubmenu}
            onFocus={() => {
              if (suppressNextSavedViewsFocusOpenRef.current) {
                suppressNextSavedViewsFocusOpenRef.current = false;
                return;
              }
              openSavedViewsSubmenu();
            }}
            onKeyDown={(event) => {
              if (event.key === 'ArrowRight' || event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                openSavedViewsSubmenu();
                requestAnimationFrame(() => savedViewsMenuRef.current?.querySelector<HTMLElement>('button, input')?.focus());
              }
            }}
            onClick={openSavedViewsSubmenu}
          >
            <BookOpen size={13} className="menu-item-icon" />
            <span className="menu-item-name">Saved views</span>
          </button>
          <button
            type="button"
            className={`menu-item${provenanceOpen ? ' menu-item-active' : ''}`}
            data-testid="open-provenance-manifest"
            onClick={() => {
              setOverflowOpen(false);
              toggleProvenanceWithDrawers();
            }}
          >
            <ShieldCheck size={13} className="menu-item-icon" />
            <span className="menu-item-name">Provenance</span>
          </button>
          {activeHiddenColumns.length > 0 && (
            <button
              type="button"
              className="menu-item"
              data-testid="toolbar-show-hidden-columns"
              onClick={() => {
                setOverflowOpen(false);
                revealAllHiddenColumns();
              }}
            >
              <Eye size={13} className="menu-item-icon" />
              <span className="menu-item-name">
                {`Show ${activeHiddenColumns.length} hidden column${
                  activeHiddenColumns.length === 1 ? '' : 's'
                }`}
              </span>
            </button>
          )}
        </div>
      )}
      {overflowMenuOpen && savedViewsMenuOpen && (
        <div
          ref={savedViewsMenuRef}
          className="menu-pop saved-views-overflow-menu"
          data-testid="saved-views-overflow-menu"
          role="menu"
          onKeyDown={(event) => {
            if (event.key === 'ArrowLeft' || event.key === 'Escape') {
              event.preventDefault();
              closeSavedViewsSubmenu();
              suppressNextSavedViewsFocusOpenRef.current = true;
              requestAnimationFrame(() => savedViewsTriggerRef.current?.focus());
            }
          }}
          style={
            savedViewsMenuPos
              ? { position: 'fixed', inset: 'auto', top: savedViewsMenuPos.top, bottom: 'auto', left: savedViewsMenuPos.left, right: 'auto', width: savedViewsMenuPos.width, maxHeight: savedViewsMenuPos.maxHeight, margin: 0 }
              : { position: 'fixed', visibility: 'hidden' }
          }
        >
          {views.length > 8 && (
            <input
              className="form-input saved-views-overflow-search"
              aria-label="Search saved views"
              value={savedViewsQuery}
              onChange={(event) => setSavedViewsQuery(event.target.value)}
            />
          )}
          <div className="saved-views-overflow-list" data-testid="saved-views-overflow-list">
            {filteredViews.length === 0 ? (
              <div className="menu-empty">No matching saved views</div>
            ) : (
              filteredViews.map((view) => (
                <button
                  type="button"
                  className="menu-item"
                  data-testid={`saved-views-overflow-view-${view.id}`}
                  key={view.id}
                  onClick={() => {
                    applySavedView(view);
                    setApplyAnnouncement(`Applied ${view.name}`);
                    setOverflowOpen(false);
                  }}
                >
                  <span className="menu-item-name">{view.name}</span>
                  {activeSavedViewId === view.id && <span className="menu-item-note">Active</span>}
                </button>
              ))
            )}
          </div>
          {canCreateSavedView && (
            <button
              type="button"
              className="menu-item"
              data-testid="saved-views-overflow-new"
              onClick={() => {
                setOverflowOpen(false);
                openNewSavedView();
              }}
            >
              <Plus size={13} className="menu-item-icon" />
              <span className="menu-item-name">New view</span>
            </button>
          )}
          <button
            type="button"
            className="menu-item"
            data-testid="saved-views-overflow-open-panel"
            onClick={() => {
              setOverflowOpen(false);
              openSavedViews();
            }}
          >
            <BookOpen size={13} className="menu-item-icon" />
            <span className="menu-item-name">Open Saved Views</span>
          </button>
        </div>
      )}
      <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">
        {applyAnnouncement}
      </div>
    </div>
  );
}

function WorkspaceGridControls() {
  const {
    applyGridSort,
    clearGridFilter,
    clearGridSort,
    effectiveSortColumn,
    sheet,
  } = useMainViewModel();
  const gridView = useGridViewHandle();
  const activeGridFilter = useSelector(gridView.store, (s) => s.applied.filter);

  const activeGridFilterValueLabel = useSelector(
    gridView.store,
    (s) => s.applied.filterValueLabel,
  );
  const activeGridSort = useSelector(gridView.store, (s) => s.applied.sort);
  const sortPanelOpen = useSelector(gridView.store, (s) => s.sortPanelOpen);
  const draftSortDirection = useSelector(gridView.store, (s) => s.draft.sortDirection);
  if (!sheet) return null;

  return (
<>
              {sortPanelOpen && (
                <GridSortPanel
                  sheet={sheet}
                  column={effectiveSortColumn}
                  direction={draftSortDirection}
                  onColumnChange={gridView.setDraftSortColumn}
                  onDirectionChange={gridView.setDraftSortDirection}
                  onApply={applyGridSort}
                  onClose={() => gridView.setSortPanelOpen(false)}
                />
              )}

              {(activeGridFilter || activeGridSort) && (
                <div className="grid-active-bar">
                  {activeGridFilter && (
                    <span className="active-grid-state" data-testid="active-grid-filter">
                      Filter: {gridFilterLabel(activeGridFilter, activeGridFilterValueLabel)}
                      <button
                        type="button"
                        className="mini-btn"
                        data-testid="clear-grid-filter"
                        onClick={clearGridFilter}
                      >
                        Clear
                      </button>
                    </span>
                  )}
                  {activeGridSort && (
                    <span className="active-grid-state" data-testid="active-grid-sort">
                      Sort: {gridSortLabel(activeGridSort)}
                      <button
                        type="button"
                        className="mini-btn"
                        data-testid="clear-grid-sort"
                        onClick={clearGridSort}
                      >
                        Clear
                      </button>
                    </span>
                  )}
                </div>
              )}
  </>
  );
}

function WorkspaceGridBanners() {
  const {
    activeChildFilter,
    activeLensView,
    activePreviewView,
    clearChildFilter,
    closePreviewView,
    runPreviewForReal,
    exitLensView,
    lensOpenError,
    sheet,
  } = useMainViewModel();
  if (!sheet && !activePreviewView) return null;

  return (
<>
              {activeChildFilter && sheet && (
                <div className="child-filter-banner" data-testid="child-filter-banner">
                  <ListFilter size={13} aria-hidden />
                  <span>
                    Showing <strong>{activeChildFilter.count.toLocaleString()}</strong>{' '}
                    {activeChildFilter.count === 1 ? 'row' : 'rows'} derived from{' '}
                    <strong>{activeChildFilter.parentSheetName}</strong>
                    {activeChildFilter.parentRowIndex != null && (
                      <> row {activeChildFilter.parentRowIndex + 1}</>
                    )}
                  </span>
                  <button
                    type="button"
                    className="mini-btn"
                    data-testid="child-filter-clear"
                    onClick={() => clearChildFilter()}
                  >
                    Show all {sheet.rowCount.toLocaleString()} rows
                  </button>
                </div>
              )}

              {lensOpenError && !activeLensView && (
                <div className="lens-view-banner lens-view-error" data-testid="lens-view-open-error" role="alert">
                  {lensOpenError}
                </div>
              )}

              {activeLensView && (
                <div className="lens-view-banner" data-testid="lens-view-banner">
                  <Boxes size={13} aria-hidden />
                  <span>
                    Lens view: <strong>{activeLensView.name}</strong> ·{' '}
                    {activeLensView.total > activeLensView.rowIds.length ? (
                      <>
                        showing {activeLensView.rowIds.length.toLocaleString()} of{' '}
                        {activeLensView.total.toLocaleString()} ranked rows
                      </>
                    ) : (
                      <>
                        {activeLensView.rowIds.length.toLocaleString()} ranked{' '}
                        {activeLensView.rowIds.length === 1 ? 'row' : 'rows'}
                      </>
                    )}
                  </span>
                  <span className="lens-view-score-hint" data-testid="lens-view-score-columns">
                    showing distance · score columns
                  </span>
                  <button
                    type="button"
                    className="mini-btn"
                    data-testid="lens-view-exit"
                    onClick={exitLensView}
                  >
                    Exit lens view
                  </button>
                </div>
              )}

              {activePreviewView && <ActionPreviewBanner view={activePreviewView}
                onRun={runPreviewForReal} onClose={closePreviewView} />}
  </>
  );
}

function WorkViewSplit({
  companionContributionId,
  companionSlot = 'work.companion',
  title,
  grid,
  children,
  selectionCount,
  onPromote,
  onClose,
  resize,
}: {
  companionContributionId: string;
  companionSlot?: string;
  title: string;
  grid: ReactNode;
  children: ReactNode;
  selectionCount: number;
  onPromote: () => void;
  onClose: () => void;
  resize: ReturnType<typeof useResizable>;
}) {
  return (
    <div
      className="workbench-mainView-split"
      data-testid="workbench-mainView-split"
      data-layout-state-schema="frisket.workbench.mainview_layout.v1"
      data-primary-contribution-id="frisket.core.view.grid"
      data-companion-contribution-id={companionContributionId}
      data-primary-slot="work.primary"
      data-companion-slot={companionSlot}
    >
      {grid}

      <ResizeSeam
        className={`work-split-seam${resize.resizing ? ' resizing' : ''}`}
        testId="work-split-seam"
        ariaLabel="Resize view"
        width={resize.width}
        min={280}
        max={820}
        onResizeStart={resize.onResizeStart}
        onResizeKeyDown={resize.onResizeKeyDown}
      />
      <div
        className="work-split-companion"
        data-testid="work-split-companion"
        style={{ width: resize.width, flex: `0 0 ${resize.width}px` }}
      >
        <div className="work-split-header" data-testid="work-split-header">
          <span className="work-split-title">{title}</span>
          <span
            className="work-split-live mono"
            data-testid="work-split-selection"
            data-selection-count={selectionCount}
          >
            live · synced selection{selectionCount > 0 ? ` · ${selectionCount}` : ''}
          </span>
          <button
            type="button"
            className="work-split-btn"
            data-testid="work-split-promote"
            title="Promote to its own tab"
            onClick={onPromote}
          >
            ↗ new tab
          </button>
          <button
            type="button"
            className="work-split-btn work-split-close"
            data-testid="work-split-close"
            aria-label="Close split"
            title="Close"
            onClick={onClose}
          >
            <X size={13} />
          </button>
        </div>
        <div className="work-split-body">{children}</div>
      </div>
    </div>
  );
}

const NO_DISABLED_TOGGLE_KEYS: readonly string[] = [];

function WorkspacePrimarySurface() {
  const { projectApi } = useWorkspaceStores();
  const actionCatalog = useActionCatalogHandle();
  const nerCatalogEntry = useSelector(actionCatalog.store, (state) => (
    state.status === 'ready'
      ? state.catalog?.actions.find((entry) => entry.kind === 'map.ner')
      : undefined
  ));
  const {
    activePreviewView,
    activeMainViewPluginViewDescriptor,
    answersViewShowing,
    answersCitedColumns,
    applyGridSortForColumn,
    clearGridSort,
    closeEvidenceViewer,
    closeSplit,
    documentView,
    documentViewShowing,
    annotatedTextColumnIds,
    documentAnnotationPreferences,
    setSheetAnnotationToggles,
    evidenceMainViewHidden,
    evidenceViewerState,
    gridContribution,
    gridOnly,
    graphSplitShowing,
    hiddenContributionIds,
    imageGalleryAvailability,
    mapColumn,
    mapSplitShowing,
    mapViewDescriptor,
    openRowById,
    openRowRef,
    project,
    promoteCurrentView,
    renderPluginDetailTab,
    resolvedRegion,
    selectAnswersRow,
    selectDocumentRow,
    selectedRowCountForSheet,
    selectedRowIdsForSheet,
    setDocumentView,
    setWorkView,
    sheet,
    showError,
    startRun,
    visibleOrderedColumns,
    workbenchHostContext,
  } = useMainViewModel();
  const gridView = useGridViewHandle();
  const workView = useWorkViewHandle();
  const answersViewState = useSelector(workView.store, (state) => state.answersView);

  // Replay the stored action through the ordinary launcher.

  const replayAnnotationLayer = useCallback(
    (outputColumnId: string) => {
      if (!sheet) return;
      void projectApi
        .getColumnRuns(outputColumnId, 0, 1)
        .then((info) => {
          const plan = nerReplayPlan(info.latestRun, {
            sheetId: sheet.id,
            columnId: outputColumnId,
            columnName: info.columnName,
          }, nerCatalogEntry);

          if (plan.kind === 'refused') showError(plan.reason);
          else startRun(plan.request);
        })
        .catch((e: unknown) => showError(e instanceof Error ? e.message : String(e)));
    },
    [nerCatalogEntry, projectApi, sheet, showError, startRun],
  );
  const activeGridFilter = useSelector(gridView.store, (s) => s.applied.filter);
  const activeGridSort = useSelector(gridView.store, (s) => s.applied.sort);
  const splitResize = useResizable({
    storageKey: `frisket:work-split-w:${project.id}`,
    minWidth: 280,
    maxWidth: 820,
    defaultWidth: 520,
    handleEdge: 'left',
  });
  const sheetIdForGallery = sheet?.id ?? '';
  const queryImageGalleryRows = useCallback(
    (args: { columnIds?: string[]; offset: number; limit: number }) =>
      projectApi.getSheetData(sheetIdForGallery, args.offset, args.limit, {
        filter: activeGridFilter,
        sort: activeGridSort,
      }),
    [activeGridFilter, activeGridSort, sheetIdForGallery],
  );
  const showImageGalleryPane =
    !gridOnly &&
    !activeMainViewPluginViewDescriptor &&
    Boolean(sheet) &&
    !hiddenContributionIds.includes(IMAGE_GALLERY_VIEW_DESCRIPTOR.id) &&
    (imageGalleryAvailability.available ||
      imageGalleryAvailability.reason?.startsWith('missing_capability:') === true);
  const companionMainViewDescriptor =
    activeMainViewPluginViewDescriptor ?? (showImageGalleryPane ? IMAGE_GALLERY_VIEW_DESCRIPTOR : null);

  if (activePreviewView?.result?.kind === 'table') return <>{gridContribution}</>;

  if (documentViewShowing && documentView && sheet) {

    const titleColumnOrder = visibleOrderedColumns.map((column) => column.name);
    const titleColumn = resolveTitleColumn(sheet, {
      overrideColumnId: documentView.titleColumnId,
      columnOrder: titleColumnOrder,
    });
    const listSortDir =
      (activeGridSort?.find((rule) => rule.column === titleColumn?.name)?.dir as
        | 'asc'
        | 'desc'
        | undefined) ?? null;
    const orderKey = `${JSON.stringify(activeGridFilter ?? null)}|${JSON.stringify(
      activeGridSort ?? null,
    )}`;
    return (
      <DocumentView
        projectId={project.id}
        sheet={sheet}
        state={documentView}
        onChangeState={setDocumentView}
        onDocumentFocus={selectDocumentRow}
        onOpenDetail={openRowById}
        queryRows={queryImageGalleryRows}
        orderKey={orderKey}
        listSortDir={listSortDir}
        titleColumnOrder={titleColumnOrder}
        annotatedTextColumnIds={annotatedTextColumnIds}
        disabledToggleKeys={documentAnnotationPreferences[sheet.id] ?? NO_DISABLED_TOGGLE_KEYS}
        onSetDisabledToggleKeys={(next) => setSheetAnnotationToggles(sheet.id, next)}
        onReplayAnnotationLayer={replayAnnotationLayer}
        onListSort={(columnName, dir) => {
          if (dir === null) clearGridSort();
          else applyGridSortForColumn(columnName, dir);
        }}
      />
    );
  }

  if (answersViewShowing && sheet) {

    const titleColumnOrder = visibleOrderedColumns.map((column) => column.name);
    const orderKey = `${JSON.stringify(activeGridFilter ?? null)}|${JSON.stringify(
      activeGridSort ?? null,
    )}`;
    return (
      <AnswersView
        projectId={project.id}
        sheet={sheet}
        citedColumns={answersCitedColumns}
        chosenColumnId={answersViewState.chosenColumnId}
        onChangeColumn={workView.setAnswersColumn}
        activeLinkId={answersViewState.activeLinkId}
        onSetActiveLink={workView.setAnswersActiveLink}
        selectedRowIds={selectedRowIdsForSheet}
        onSyncSelectRow={selectAnswersRow}
        queryRows={queryImageGalleryRows}
        orderKey={orderKey}
        titleColumnOrder={titleColumnOrder}
      />
    );
  }

  if (graphSplitShowing && sheet) {

    return (
      <WorkViewSplit
        companionContributionId={GRAPH_NEIGHBORHOOD_CONTRIBUTION_ID}
        title={`Graph of ${sheet.name}`}
        grid={gridContribution}
        selectionCount={selectedRowCountForSheet}
        onPromote={promoteCurrentView}
        onClose={closeSplit}
        resize={splitResize}
      >
        <GraphNeighborhoodWorkbenchViewFrame>
          <Suspense fallback={<PanelLoading className="grid-host" label="Loading graph…" />}>
            <LazyGenericGraphView
              key={sheet.id}
              sheet={sheet}
              onClose={closeSplit}
              onOpenRowRef={openRowRef}
              entityDetailContributions={resolvedRegion('entityDetail').contributions}
              renderPluginDetailTab={renderPluginDetailTab}
            />
          </Suspense>
        </GraphNeighborhoodWorkbenchViewFrame>
      </WorkViewSplit>
    );
  }
  if (evidenceViewerState?.host === 'mainView' && !evidenceMainViewHidden) {
    return (
      <div
        className="workbench-mainView-split"
        data-testid="workbench-mainView-split"
        data-layout-state-schema="frisket.workbench.mainview_layout.v1"
        data-primary-contribution-id="frisket.core.view.grid"
        data-companion-contribution-id="frisket.core.view.evidence"
        data-primary-slot="work.primary"
        data-companion-slot="work.companion"
      >
        {gridContribution}
        <EvidenceWorkbenchViewFrame host="mainView">
          <Suspense fallback={<PanelLoading className="grid-host" label="Loading evidence…" />}>
            <LazyEvidenceViewer
              key={String(evidenceViewerState.linkId)}
              evidenceLinkId={evidenceViewerState.linkId}
              mode="pane"
              onClose={closeEvidenceViewer}
            />
          </Suspense>
        </EvidenceWorkbenchViewFrame>
      </div>
    );
  }
  if (mapSplitShowing && mapColumn && mapViewDescriptor && sheet) {
    return (
      <WorkViewSplit
        companionContributionId={mapViewDescriptor.id}
        title={`Map of ${sheet.name}`}
        grid={gridContribution}
        selectionCount={selectedRowCountForSheet}
        onPromote={promoteCurrentView}
        onClose={closeSplit}
        resize={splitResize}
      >
        <PluginMainViewHost
          key={`${sheet.id}:${mapColumn.id}`}
          descriptor={mapViewDescriptor}
          projectId={project.id}
          sheet={sheet}
          visibleColumns={visibleOrderedColumns}
          queryRows={queryImageGalleryRows}
          projectionApi={projectApi}
          openRow={openRowById}
          hostContext={workbenchHostContext}
          targetColumnId={String(mapColumn.id)}
          onCloseView={closeSplit}
        />
      </WorkViewSplit>
    );
  }
  if (companionMainViewDescriptor && sheet) {
    const isGallery = companionMainViewDescriptor.id === IMAGE_GALLERY_VIEW_DESCRIPTOR.id;
    return (
      <WorkViewSplit
        companionContributionId={companionMainViewDescriptor.id}
        companionSlot={companionMainViewDescriptor.placements[0]?.slot ?? 'work.primary'}
        title={isGallery ? `Gallery of ${sheet.name}` : companionMainViewDescriptor.title ?? sheet.name}
        grid={gridContribution}
        selectionCount={selectedRowCountForSheet}
        onPromote={promoteCurrentView}
        onClose={() => setWorkView('grid')}
        resize={splitResize}
      >
        <PluginMainViewHost
          descriptor={companionMainViewDescriptor}
          projectId={project.id}
          sheet={sheet}
          visibleColumns={visibleOrderedColumns}
          queryRows={queryImageGalleryRows}
          projectionApi={projectApi}
          openRow={openRowById}
          hostContext={workbenchHostContext}
        />
      </WorkViewSplit>
    );
  }
  return <>{gridContribution}</>;
}

function WorkspacePromotedView() {
  const { projectApi } = useWorkspaceStores();
  const {
    activePromotedView,
    graphNeighborhoodHidden,
    gridContribution,
    mapViewDescriptor,
    openRowById,
    openRowRef,
    project,
    renderPluginDetailTab,
    resolvedRegion,
    selectedRowCountForSheet,
    sheet,
    visibleOrderedColumns,
    workbenchHostContext,
  } = useMainViewModel();
  const gridView = useGridViewHandle();
  const activeGridFilter = useSelector(gridView.store, (s) => s.applied.filter);
  const activeGridSort = useSelector(gridView.store, (s) => s.applied.sort);
  const sheetId = sheet?.id ?? '';
  const queryRows = useCallback(
    (args: { columnIds?: string[]; offset: number; limit: number }) =>
      projectApi.getSheetData(sheetId, args.offset, args.limit, {
        filter: activeGridFilter,
        sort: activeGridSort,
      }),
    [activeGridFilter, activeGridSort, sheetId],
  );
  if (!activePromotedView || !sheet) return <>{gridContribution}</>;
  const view = activePromotedView;
  let body: ReactNode;
  if (view.kind === 'graph' && !graphNeighborhoodHidden) {
    body = (
      <GraphNeighborhoodWorkbenchViewFrame>
        <Suspense fallback={<PanelLoading className="grid-host" label="Loading graph…" />}>
          <LazyGenericGraphView
            key={sheet.id}
            sheet={sheet}
            onClose={() => undefined}
            onOpenRowRef={openRowRef}
            entityDetailContributions={resolvedRegion('entityDetail').contributions}
            renderPluginDetailTab={renderPluginDetailTab}
          />
        </Suspense>
      </GraphNeighborhoodWorkbenchViewFrame>
    );
  } else if (view.kind === 'map' && mapViewDescriptor) {
    body = (
      <PluginMainViewHost
        key={`${sheet.id}:${view.columnId ?? ''}`}
        descriptor={mapViewDescriptor}
        projectId={project.id}
        sheet={sheet}
        visibleColumns={visibleOrderedColumns}
        queryRows={queryRows}
        projectionApi={projectApi}
        openRow={openRowById}
        hostContext={workbenchHostContext}
        targetColumnId={view.columnId ? String(view.columnId) : undefined}
      />
    );
  } else {
    body = (
      <PluginMainViewHost
        descriptor={IMAGE_GALLERY_VIEW_DESCRIPTOR}
        projectId={project.id}
        sheet={sheet}
        visibleColumns={visibleOrderedColumns}
        queryRows={queryRows}
        projectionApi={projectApi}
        openRow={openRowById}
        hostContext={workbenchHostContext}
      />
    );
  }
  return (
    <div className="workbench-promoted-view" data-testid="workbench-promoted-view">
      <div className="work-split-header" data-testid="promoted-view-header">
        <span className="work-split-title">{view.label}</span>
        <span
          className="work-split-live mono"
          data-testid="promoted-view-selection"
          data-selection-count={selectedRowCountForSheet}
        >
          live · synced selection{selectedRowCountForSheet > 0 ? ` · ${selectedRowCountForSheet}` : ''}
        </span>
      </div>
      <div className="workbench-promoted-view-body">{body}</div>
    </div>
  );
}

function WorkspaceHeaderMenuSlot() {
  const {
    actActionTemplates,
    activeFrozenColumnCount,
    applyGridSortForColumn,
    changeFrozenColumnCount,
    clearGridFilter,
    clearGridSort,
    closeHeaderMenu,
    headerMenuActiveFilter,
    headerMenuActiveSort,
    headerMenuAdjacentHidden,
    hideColumn,
    insertColumnBeside,
    mapAvailabilityReason,
    mapAvailabilityStatus,
    mapAvailable,
    canCreateSavedView,
    openHeaderMenuColumnSettings,
    openHeaderMenuSaveView,
    openMapPanel,
    revealColumns,
    runActionFromSurface,
    setRowTitleColumn,
    sheet,
    startRun,
    visibleHeaderMenu,
    visibleOrderedColumns,
  } = useMainViewModel();
  const { openDiscover } = useWorkspaceShell();
  const run = useRun();
  const gridView = useGridViewHandle();
  const actionCatalog = useActionCatalogHandle();
  const columnTablesExportEntry = useSelector(actionCatalog.store, (state) => (
    state.status === 'ready'
      ? state.catalog?.actions.find((entry) => entry.kind === 'export.column_tables')
      : undefined
  ));

  const resolvedTitleColumn = useMemo(
    () =>
      sheet
        ? resolveTitleColumn(sheet, { columnOrder: visibleOrderedColumns.map((c) => c.name) })
        : null,
    [sheet, visibleOrderedColumns],
  );

  const columnActions = visibleHeaderMenu
    ? orderColumnActionsForNextStep(
        actionsForColumn(actActionTemplates, visibleHeaderMenu.column),
        visibleHeaderMenu.column,
      )
    : [];

  const [columnTablesPicker, setColumnTablesPicker] = useState<{ id: string; name: string } | null>(null);

  const materializeColumnTables = useCallback(
    (args: { columnName: string; includeColumns: string[]; targetName: string }) => {
      const column = columnTablesPicker;
      setColumnTablesPicker(null);
      if (!sheet || !column) return;
      startRun(pdfTablesMaterializeRequest({
        sheetId: sheet.id,
        columnId: column.id,
        ...args,
      }));
    },
    [sheet, columnTablesPicker, startRun],
  );

  const exportColumnTables = useCallback(
    (args: { columnName: string; groupBy: string; excludeColumns: string[] }) => {
      const column = columnTablesPicker;
      setColumnTablesPicker(null);
      if (!sheet || !column || !columnTablesExportEntry) return;
      startRun(columnTablesExportRequest({
        sheetId: sheet.id,
        columnId: column.id,
        groupBy: args.groupBy,
        excludeColumns: args.excludeColumns,
      }));
    },
    [columnTablesExportEntry, columnTablesPicker, sheet, startRun],
  );

  return (
<>
              {visibleHeaderMenu && (
                <GridColumnHeaderMenu
                  state={visibleHeaderMenu}
                  activeSort={headerMenuActiveSort}
                  activeFilter={headerMenuActiveFilter}
                  onSort={(direction) => {
                    applyGridSortForColumn(visibleHeaderMenu.column.name, direction);
                    closeHeaderMenu();
                  }}
                  onClearSort={() => {
                    if (headerMenuActiveSort) clearGridSort();
                    closeHeaderMenu();
                  }}
                  onFilter={() => {
                    if (sheet) {
                      gridView.setInlineFilterColumn(sheet.id, visibleHeaderMenu.column.name);
                    }
                    closeHeaderMenu();
                  }}
                  onClearFilter={() => {
                    if (headerMenuActiveFilter) clearGridFilter();
                    closeHeaderMenu();
                  }}
                  onOpenFilterSidebar={() => {
                    openDiscover('Facets');
                    closeHeaderMenu();
                  }}
                  onAdvancedSort={() => {
                    gridView.setSortPanelOpen(true);
                    closeHeaderMenu();
                  }}
                  columnActions={columnActions}
                  onColumnAction={(template) => {
                    runActionFromSurface(template.kind, visibleHeaderMenu.column.name);
                    closeHeaderMenu();
                  }}
                  actionsDisabled={isRunActionBlockedStatus(run?.status)}
                  frozenColumnCount={activeFrozenColumnCount}
                  onFreezeColumns={(count) => changeFrozenColumnCount(count)}
                  onUnfreezeColumns={() => changeFrozenColumnCount(0)}
                  onOpenSettings={openHeaderMenuColumnSettings}
                  onSaveView={canCreateSavedView ? openHeaderMenuSaveView : undefined}
                  onOpenMap={() => {
                    openMapPanel(visibleHeaderMenu.column);
                    closeHeaderMenu();
                  }}
                  mapAvailable={mapAvailable}
                  mapAvailabilityStatus={mapAvailabilityStatus}
                  mapAvailabilityReason={mapAvailabilityReason}
                  onHideColumn={() => hideColumn(visibleHeaderMenu.column.name)}
                  adjacentHiddenColumns={headerMenuAdjacentHidden}
                  onUnhideColumns={() => revealColumns(headerMenuAdjacentHidden)}
                  onInsertColumnLeft={() => insertColumnBeside('left')}
                  onInsertColumnRight={() => insertColumnBeside('right')}
                  isRowTitleColumn={resolvedTitleColumn?.id === visibleHeaderMenu.column.id}
                  onSetRowTitleColumn={() => setRowTitleColumn(String(visibleHeaderMenu.column.id))}
                  onExportColumnTables={() => {
                    setColumnTablesPicker({
                      id: visibleHeaderMenu.column.id,
                      name: visibleHeaderMenu.column.name,
                    });
                    closeHeaderMenu();
                  }}
                />
              )}
              {columnTablesPicker && sheet && (
                <div className="pdf-tables-picker-overlay">
                  <PdfTablesPicker
                    sheetId={sheet.id}
                    column={columnTablesPicker}
                    defaultTargetName={columnTablesPicker.name}
                    onMaterialize={materializeColumnTables}
                    onExportTables={columnTablesExportEntry ? exportColumnTables : undefined}
                    onClose={() => setColumnTablesPicker(null)}
                  />
                </div>
              )}
  </>
  );
}

function WorkspaceImportMainView() {
  const {
    onImported,
    showError,
    runActionFromSurface,
  } = useMainViewModel();

  const actSurface = useActSurfaceHandle();
  const actionCatalog = useActionCatalogHandle();
  const ftmImportEnabled = useSelector(actionCatalog.store, (state) => (
    state.status === 'ready'
      && state.resolvedTemplates.some((template) => template.kind === 'frisket.ftm.ftm_import')
  ));

  return (
            <ImportDropzone
              onImported={onImported}
              onError={showError}
              onLaunchDownload={runActionFromSurface}
              onOpenWorkspace={actSurface.openImportDialog}
              onOpenCsv={actSurface.openCsvImport}
              onOpenBulk={actSurface.openBulkImport}
              ftmImportEnabled={ftmImportEnabled}
            />
  );
}

const WorkspaceRightInspectorRegion = memo(function WorkspaceRightInspectorRegion() {
  useRenderCount('rightInspector');
  const { rightInspectorPluginPanelDescriptors } = useWorkspaceShell();
  const workbenchHostContext = useWorkbenchHostContext();
  const resolvedWorkbenchLayout = useResolvedWorkbenchLayout();
  const { sheet } = useCurrentSheet();

  return (
        <section
          className="workbench-region workbench-right-inspector"
          data-testid="workbench-region-rightInspector"
          aria-label="Workbench right inspector"
        >
          <ResolvedWorkbenchLayoutRegion
            region={resolveRegionContributions(resolvedWorkbenchLayout, 'rightInspector')}
          >

          {rightInspectorPluginPanelDescriptors.map((descriptor) => (
            <PluginPanelHost
              key={descriptor.id}
              descriptor={descriptor}
              sheet={sheet ?? null}
              hostContext={workbenchHostContext}
            />
          ))}
          </ResolvedWorkbenchLayoutRegion>
        </section>
  );
});

const WorkspaceActionDrawerRegion = memo(function WorkspaceActionDrawerRegion() {
  const { projectApi } = useWorkspaceStores();
  const { openOcrCompareTab } = useMainViewModel();
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
  const chrome = useChromeHandle();
  // Action-panel state must not reach localStorage.

  const hideActionPanel = chrome.hideActionPanel;
  const detail = useDetailHandle();
  const childFilter = useSelector(detail.store, (state) => state.childFilter);
  const activeChildFilter = childFilter?.sheetId === sheetId ? childFilter : null;
  const proposalInspect = useSelector(detail.store, (s) => s.proposalInspect);
  const activeDetailRowId = useSelector(detail.store, (s) => s.rowDrawer?.id ?? null);
  const setProposalInspect = detail.setProposalInspect;
  const route = useRouteHandle();
  const routeActionKind = useSelector(route.store, (s) => s.actionKind);
  const hasActionTarget = Boolean(routeActionKind || proposalInspect);
  const actionViewScopeKey = useMemo(() => (
    sheetId !== null && (activeGridFilter || activeChildFilter)
      ? JSON.stringify([
          sheetId,
          activeChildFilter?.parentRowId ?? null,
          activeGridFilter ?? null,
        ])
      : null
  ), [activeChildFilter, activeGridFilter, sheetId]);
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
    let cancelled = false;
    if (
      !actionPanelVisible
      || !hasActionTarget
      || sheetId === null
      || !actionViewScopeKey
      || actionLensScopeKey !== null
      || selectedRowIdsForSheet.length > 0
    ) {
      queueMicrotask(() => {
        if (!cancelled) setActionViewScope(null);
      });
      return () => {
        cancelled = true;
      };
    }
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
        const [scopeSheetId, parentRowId, filter] = JSON.parse(
          actionViewScopeKey,
        ) as [string, string | null, typeof activeGridFilter];
        const rowIds: string[] = [];
        const pageSize = 1000;
        let expectedTotal: number | null = null;
        for (let offset = 0; ; ) {
          const page = await projectApi.getSheetData(scopeSheetId, offset, pageSize, {
            parentRowId,
            filter,
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
  }, [
    actionPanelVisible,
    actionLensScopeKey,
    actionViewScopeKey,
    hasActionTarget,
    selectedRowIdsForSheet.length,
    sheetId,
  ]);

  useEffect(() => {
    let cancelled = false;
    if (
      !actionPanelVisible
      || !hasActionTarget
      || sheetId === null
      || !actionLensScopeKey
      || selectedRowIdsForSheet.length > 0
    ) {
      queueMicrotask(() => {
        if (!cancelled) setActionLensScope(null);
      });
      return () => {
        cancelled = true;
      };
    }
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
  }, [
    actionLensScopeKey,
    actionPanelVisible,
    hasActionTarget,
    selectedRowIdsForSheet.length,
    sheetId,
  ]);

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
        />
      </Suspense>
    </ActionDrawer>
  );
});

const WorkspaceBottomDockRegion = memo(function WorkspaceBottomDockRegion() {
  useRenderCount('bottomDock');
  const {
    bottomDockPluginPanelDescriptors,
    doStepTo,
    hideContribution,
    loadHistoryPage,
    refreshSheets,
    resumeHaltedRun,
    selectBottomDockTab,
  } = useWorkspaceShell();
  const workbenchHostContext = useWorkbenchHostContext();
  const resolvedWorkbenchLayout = useResolvedWorkbenchLayout();
  const { sheet, sheets } = useCurrentSheet();
  const job = useJobsHandle();
  const run = useSelector(job.store, (s) => s.run);
  const actionJobs = useSelector(job.store, (s) => s.actionJobs.jobs);
  const actionJobsError = useSelector(job.store, (s) => s.actionJobs.error);
  const actionJobsLoading = useSelector(job.store, (s) => s.actionJobs.loading);
  const chrome = useChromeHandle();
  const activeBottomDockTab = useSelector(chrome.store, (s) => s.activeBottomDockTab);
  const projectData = useProjectDataResource();
  const history = useSelector(projectData.store, (state) => state.history);
  const pluginLayout = usePluginLayoutHandle();
  const workbenchPluginRuntimeIndex = useSelector(
    pluginLayout.store,
    (s) => s.workbenchPluginRuntimeIndex,
  );

  // Never synthesize action status.
  const runSummary = useMemo(
    () => deriveDockRunSummary(actionJobs, run),
    [actionJobs, run],
  );
  // Direct-run failures have no queued-job record.

  const errorJobs = useMemo(
    () => [
      ...deriveDockErrorJobs(actionJobs, run),
      ...derivePluginErrorJobs(workbenchPluginRuntimeIndex),
    ],
    [actionJobs, run, workbenchPluginRuntimeIndex],
  );
  const tabBadges = useMemo(
    () => ({ errors: errorJobs.length }),
    [errorJobs],
  );

  const closeDockTab = useCallback(
    (contribution: WorkbenchResolvedLayoutContribution) => {
      hideContribution(contribution.contributionId);
    },
    [hideContribution],
  );

  const resumeHaltedDockJob = useCallback(
    (job: DockActionJob) => {
      const progress = job.progress;
      if (!progress?.sheetId || !progress.targetColumnId) return;
      void resumeHaltedRun(progress.sheetId, progress.targetColumnId);
    },
    [resumeHaltedRun],
  );

  const renderDockContribution = useCallback(
    (
      contribution: WorkbenchResolvedLayoutContribution,
      dock: WorkbenchBottomDockRenderContext,
    ) => {
      switch (contribution.contributionId) {
        case 'frisket.core.panel.jobs':
          return (
            <BottomDockTabFrame descriptor={BOTTOM_DOCK_JOBS_DESCRIPTOR}>
              <WorkbenchJobSplitPanel
                jobs={actionJobs}
                loading={actionJobsLoading}
                error={actionJobsError}
                liveActionJobs={job.liveActionJobs}
                mode="jobs"
                onResumeHaltedRun={resumeHaltedDockJob}
              />
            </BottomDockTabFrame>
          );
        case 'frisket.core.panel.errors':
          return (
            <BottomDockTabFrame descriptor={BOTTOM_DOCK_ERRORS_DESCRIPTOR}>
              <WorkbenchJobSplitPanel
                jobs={errorJobs}
                loading={actionJobsLoading}
                error={actionJobsError}
                liveActionJobs={job.liveActionJobs}
                mode="errors"
                onResumeHaltedRun={resumeHaltedDockJob}
              />
            </BottomDockTabFrame>
          );
        case 'frisket.core.panel.history':
          return (
            <HistoryWorkbenchPanel
              host="bottomDock"
              history={history}
              onStepTo={doStepTo}
              onLoadPage={loadHistoryPage}
            />
          );
        case 'frisket.core.panel.lineage':
          return (
            <BottomDockTabFrame descriptor={BOTTOM_DOCK_LINEAGE_DESCRIPTOR}>
              <LineagePanel sheets={sheets} refreshSheets={refreshSheets} />
            </BottomDockTabFrame>
          );

        default: {
          if (contribution.runtimeSource === 'runtimeIndex') {
            const descriptor = bottomDockPluginPanelDescriptors.find(
              (candidate) => candidate.id === contribution.contributionId,
            );
            if (descriptor) {
              return (
                <PluginDockTabHost
                  descriptor={descriptor}
                  sheet={sheet ?? null}
                  hostContext={workbenchHostContext}
                  isActiveTab={dock.isActiveTab}
                  focusTab={dock.focusTab}
                />
              );
            }
          }
          return (
            <div
              className="bottom-dock-empty"
              data-testid="bottom-dock-unavailable-contribution"
              data-contribution-id={contribution.contributionId}
            >
              This tab&apos;s content is unavailable.
            </div>
          );
        }
      }
    },
    [
      actionJobs,
      actionJobsError,
      actionJobsLoading,
      errorJobs,
      bottomDockPluginPanelDescriptors,
      doStepTo,
      history,
      job.liveActionJobs,
      loadHistoryPage,
      refreshSheets,
      resumeHaltedDockJob,
      sheet,
      sheets,
      workbenchHostContext,
    ],
  );

  const bottomDockRegion = resolveRegionContributions(resolvedWorkbenchLayout, 'bottomDock');
  return (
      <ResolvedWorkbenchLayoutRegion region={bottomDockRegion}>
        <WorkbenchBottomDock
          region={bottomDockRegion}
          activeTabPlacementId={activeBottomDockTab}
          onSelectTab={selectBottomDockTab}
          onCloseTab={closeDockTab}
          renderContribution={renderDockContribution}
          runSummary={runSummary}
          tabBadges={tabBadges}
        />
      </ResolvedWorkbenchLayoutRegion>
  );
});

const WorkspaceOverlayRegion = memo(function WorkspaceOverlayRegion() {
  useRenderCount('overlay');
  const overlayChrome = useChromeHandle();
  const projectionStatus = useSelector(overlayChrome.store, (s) => s.projectionStatus);
  const {
    invalidateProjectData,
    openSourceHealthMainView,
    projectApi,
    refreshHistory,
    refreshSheets,
  } = useWorkspaceShell();
  const {
    afterColumnUpdated,
    applyGridFailedFilter,
    retryFailedRows,
    clearDeleteRowsConfirm,
    closeEvidenceViewer,
    closeReview,
    closeRoutePanel,
    columnDrawer,
    commandPaletteOpen,
    closeCommandPaletteAndReset,
    commandPaletteQuery,
    setCommandPaletteQuery,
    commandPaletteBestMatchItems,
    commandPaletteActionItems,
    commandPaletteGotoItems,
    navigateToSearchHit,
    closeRowDrawer,
    copilotOpen,
    closeCopilotPopover,
    runActionFromSurface,
    selectSheet,
    sheets,
    startProposal,
    openActionPanel,
    confirmDeleteRows,
    deleteRowsConfirm,
    dismissPluginPeek,
    doRedo,
    doUndo,
    error,
    evidenceViewerState,
    hideWorkbenchContribution,
    history,
    lastCommandAction,
    openEvidenceViewer,
    openReview,
    project,
    openPluginPeekDescriptor,
    pluginPeekState,
    provenanceOpen,
    closeProvenanceOpen,
    pluginLauncherCommands,
    renderPluginDetailTab,
    resolvedRegion,
    revealPluginLauncher,
    revealWorkbenchContribution,
    reviewCount,
    reviewOpen,
    reviewRunId,
    setProposalInspect,
    sheet,
    commitReviewDecision,
    workbenchCommandEntries,
    workbenchHostContext,
    workbenchVisibilityTargets,
  } = useOverlayModel();
  const actSurfaceHandle = useActSurfaceHandle();
  const sourcesConnectionsOpen = useSelector(
    actSurfaceHandle.store,
    (state) => state.sourcesConnectionsOpen,
  );
  const jobs = useJobsHandle();
  const run = useSelector(jobs.store, (state) => state.run);
  const costGate = useSelector(jobs.store, (state) => state.costGate);
  const outputColumnCollision = useSelector(
    jobs.store,
    (state) => state.outputColumnCollision,
  );
  const afterBackfill = jobs.afterBackfill;
  const cancelCostGate = jobs.cancelCostGate;
  const cancelCurrentRun = jobs.cancelCurrentRun;
  const confirmCostGate = jobs.confirmCostGate;
  const cancelOutputColumnCollision = jobs.cancelOutputColumnCollision;
  const confirmOutputColumnCollision = jobs.confirmOutputColumnCollision;
  const requestCostConfirmation = jobs.requestCostConfirmation;

  const paletteLauncherCommands = useMemo(
    () =>
      pluginLauncherCommands.map((launcher) => ({
        contributionId: launcher.contributionId,
        label: launcher.label,
        run: () => {
          revealPluginLauncher(launcher.contributionId);
          closeCommandPaletteAndReset();
        },
      })),
    [pluginLauncherCommands, revealPluginLauncher, closeCommandPaletteAndReset],
  );

  const openDiagnosePanel = () => overlayChrome.openDiagnosePanel(project);

  // Do not restore Copilot focus when Import takes ownership.

  const beginImportFromCopilot = useCallback(() => {
    const next = beginCopilotImportHandoff({
      chrome: { copilotPopoverOpen: copilotOpen },
      actSurface: { importDialogOpen: actSurfaceHandle.store.get().importDialogOpen },
    });
    if (!next.chrome.copilotPopoverOpen) closeCopilotPopover();
    if (next.actSurface.importDialogOpen) actSurfaceHandle.openImportDialog();
  }, [copilotOpen, closeCopilotPopover, actSurfaceHandle]);

  const renderColumnDetailContribution = useCallback(
    (contribution: WorkbenchResolvedLayoutContribution) => {
      if (!sheet || !columnDrawer) return null;
      return renderPluginDetailTab(contribution, {
        kind: 'column',
        sheetId: sheet.id,
        columnId: String(columnDrawer.id),
        columnName: columnDrawer.name,
      });
    },
    [columnDrawer, renderPluginDetailTab, sheet],
  );

  return (
<>
      <StatusBar
        run={run}
        history={history}
        reviewCount={reviewCount}
        projectionStatus={projectionStatus}
        onUndo={doUndo}
        onRedo={doRedo}
        onOpenReview={() => openReview()}
        onCancelRun={cancelCurrentRun}
        onShowFailedRows={applyGridFailedFilter}
        onRetryFailedRows={(columnName, outcome) => void retryFailedRows(columnName, outcome)}
      />
      <NowPlayingWidget />
      <div
        className="workbench-modal-or-peek"
        data-testid="workbench-region-modalOrPeek"
        data-modal-stack-host="true"
        data-plugin-peek-open={pluginPeekState.openContributionId ?? ''}
        data-plugin-peek-rejected={pluginPeekState.rejectedContributionId ?? ''}
      >
        <ResolvedWorkbenchLayoutRegion region={resolvedRegion('modalOrPeek')} />
        {openPluginPeekDescriptor && sheet && (
          <PluginPeekHost
            descriptor={openPluginPeekDescriptor}
            sheet={sheet}
            hostContext={workbenchHostContext}
            onDismiss={dismissPluginPeek}
          />
        )}
      </div>
      <SourcesConnectionsDialog
        open={sourcesConnectionsOpen}
        sourceApi={projectApi}
        onClose={actSurfaceHandle.closeSourcesConnections}
        onOpenImportDialog={() => actSurfaceHandle.openImportDialog('feed')}
        onPolled={(sheetId) => {
          void refreshSheets().then(() => {
            if (sheetId != null) selectSheet(String(sheetId));
          });
          void refreshHistory();
          invalidateProjectData();
        }}
        onOpenSourceHealthMainView={(sourceId) => {
          actSurfaceHandle.closeSourcesConnections();
          openSourceHealthMainView(sourceId);
        }}
        sourceDetailContributions={resolvedRegion('sourceDetail').contributions}
        renderPluginDetailTab={renderPluginDetailTab}
      />
      {commandPaletteOpen && (
        <WorkbenchCommandPalette
          onClose={closeCommandPaletteAndReset}
          commands={workbenchCommandEntries}
          onHideContribution={hideWorkbenchContribution}
          onRevealContribution={revealWorkbenchContribution}
          lastAction={lastCommandAction}
          visibilityTargets={workbenchVisibilityTargets}
          query={commandPaletteQuery}
          onQueryChange={setCommandPaletteQuery}
          bestMatchItems={commandPaletteBestMatchItems}
          actionItems={commandPaletteActionItems}
          gotoItems={commandPaletteGotoItems}
          onLaunchAction={runActionFromSurface}
          onNavigateSheet={selectSheet}
          searchSheets={sheets}
          onNavigateSearchHit={navigateToSearchHit}
          launcherCommands={paletteLauncherCommands}
        />
      )}

      {copilotOpen && (
        <CopilotDialogPopover
          onRunProposal={startProposal}
          onClose={closeCopilotPopover}
          onImportNeeded={beginImportFromCopilot}
          onInspectProposal={(proposal) => {

            openActionPanel();
            setProposalInspect({
              seq: nextProposalInspectSeq(),
              title: proposal.title,
              spec: proposal.spec,
            });
          }}
        />
      )}
      {!error && <CompletedClusterResult onCreate={(receiptId) => {
        runActionFromSurface('resolve.entities', undefined, { actionDraft: entityTableDraft(receiptId) });
      }} />}
      {error && (() => {
        const remediated = remediateApiError(error);
        return (
          <div
            className="toast-error"
            data-testid="error-toast"
            role="alert"

            // Modal dialogs outrank z-index, so render the toast inside.

            popover="manual"
            ref={(el) => {
              if (!el || typeof el.showPopover !== 'function') return;
              try {
                if (!el.matches(':popover-open')) el.showPopover();
              } catch {
                /* Fallback for browsers without popover support. */
              }
            }}
          >
            {error.message}
            {remediated.remediation && (
              <div className="toast-error-remediation" data-testid="error-toast-remediation">
                {remediated.remediation}
              </div>
            )}

            {remediated.fieldErrors && remediated.fieldErrors.length > 0 && (
              <ul className="toast-error-details" data-testid="error-toast-details">
                {remediated.fieldErrors.map((fieldError, index) => (
                  <li key={`${fieldError.path}-${index}`}>
                    {fieldError.path && <code>{fieldError.path}</code>}
                    {fieldError.path ? ': ' : ''}
                    {fieldError.message}
                  </li>
                ))}
              </ul>
            )}
            {remediated.showDiagnose && (
              <button
                type="button"
                className="toast-error-diagnose-link"
                data-testid="error-toast-diagnose"
                onClick={openDiagnosePanel}
              >
                Open Diagnose
              </button>
            )}

            <SupportContactNote supportContact={undefined} />
          </div>
        );
      })()}

      {costGate && (
        <CostGateWorkbenchViewFrame>
          <CostGateModal
            estimate={costGate.estimate}
            message={costGate.message}
            onCancel={cancelCostGate}
            onConfirm={confirmCostGate}
          />
        </CostGateWorkbenchViewFrame>
      )}

      {deleteRowsConfirm && (
        <RowDeleteConfirmWorkbenchViewFrame>
          <ConfirmDeleteRowsModal
            count={deleteRowsConfirm.rowIds.length}
            onCancel={clearDeleteRowsConfirm}
            onConfirm={confirmDeleteRows}
          />
        </RowDeleteConfirmWorkbenchViewFrame>
      )}

      {outputColumnCollision && (
        <OutputColumnCollisionWorkbenchViewFrame>
          <OutputColumnCollisionModal
            columns={outputColumnCollision.columns}
            message={outputColumnCollision.message}
            onCancel={cancelOutputColumnCollision}
            onConfirm={confirmOutputColumnCollision}
          />
        </OutputColumnCollisionWorkbenchViewFrame>
      )}

      {columnDrawer && sheet && (
        <ColumnDrawer
          column={columnDrawer}
          sheetId={sheet.id}
          onClose={() => {
            closeRoutePanel();
          }}
          onBackfillComplete={afterBackfill}
          onColumnUpdated={afterColumnUpdated}
          onReviewRun={openReview}
          onReviseRun={(draft) => {
            closeRoutePanel();
            runActionFromSurface(draft.action_id, undefined, { actionDraft: draft });
          }}
          requestCostConfirmation={requestCostConfirmation}
          detailContributions={[
            ...resolvedRegion('columnDetail').contributions,
            ...resolvedRegion('columnInspector').contributions,
          ]}
          renderDetailContribution={renderColumnDetailContribution}
        />
      )}
      {provenanceOpen && (
        <ProvenanceWorkbenchPanelFrame>
          <ProvenanceManifest
            projectName={project.name}
            onClose={() => closeProvenanceOpen()}
            onCreateEntityTable={(receiptId) => {
              closeProvenanceOpen();
              runActionFromSurface('resolve.entities', undefined, { actionDraft: entityTableDraft(receiptId) });
            }}
          />
        </ProvenanceWorkbenchPanelFrame>
      )}
      {evidenceViewerState?.host === 'modalOrPeek' && (
        <EvidenceWorkbenchViewFrame host="modalOrPeek">
          <Suspense
            fallback={
              <PanelLoading
                className="panel-loading-page"
                testId="evidence-viewer-loading"
                label="Loading evidence…"
              />
            }
          >
            <LazyEvidenceViewer
              key={String(evidenceViewerState.linkId)}
              evidenceLinkId={evidenceViewerState.linkId}
              onClose={closeEvidenceViewer}
              onOpenCompanion={() => {
                closeRowDrawer();
                openEvidenceViewer(evidenceViewerState.linkId, 'mainView');
              }}
            />
          </Suspense>
        </EvidenceWorkbenchViewFrame>
      )}
      {reviewOpen && (
        <ReviewQueueWorkbenchViewFrame host="modalOrPeek">
          <ReviewQueue
            onClose={closeReview}
            onChanged={commitReviewDecision}
            runId={reviewRunId ?? undefined}
          />
        </ReviewQueueWorkbenchViewFrame>
      )}
  </>
  );
});
