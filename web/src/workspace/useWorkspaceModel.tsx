import '../grid/SheetGrid.css';
import { Suspense, useCallback, useEffect, useMemo, useState, useSyncExternalStore } from 'react';
import type { CellValue, ColumnDef, HistoryState, ProjectInfo, Row } from '../api/open';
import { subscribeMapPointsMeta } from '../api/mapPointsMeta';
import { PreviewTable } from '../components/PreviewTable';
import { PanelLoading } from '../components/PanelPrimitives';
import { gridReadColumnIds } from '../components/fieldValueDependencies';
import {
  pendingImportRowCount as selectPendingImportRowCount, preferredImportActivity,
  type ImportProgressChange, useImportProgress,
} from '../components/useImportProgress';
import { applyColumnTypeRegistry } from '../grid/typeRegistry';
import { computeRowCacheKey, resolveRowCacheScope } from '../grid/rowCacheStore';
import { createStore } from '../core/store/createStore';
import type { Store } from '../core/store/types';
import type { CommandContext } from '../core/commands/types';
import { useCommand } from '../bind/useCommand';
import { useSelector } from '../bind/useSelector';
import { useGridViewHandle } from '../bind/useGridViewHandle';
import { useRowCacheHandle } from '../bind/useRowCacheHandle';
import { useSelectionHandle } from '../bind/useSelectionHandle';
import { useDetailHandle } from '../bind/useDetailHandle';
import { useSavedViewsHandle } from '../bind/useSavedViewsHandle';
import { useChromeHandle } from '../bind/useChromeHandle';
import { useActSurfaceHandle } from '../bind/useActSurfaceHandle';
import { useActionCatalogHandle } from '../bind/useActionCatalogHandle';
import { usePluginLayoutHandle } from '../bind/usePluginLayoutHandle';
import { useJobsHandle } from '../bind/useJobsHandle';
import { useProjectDataResource, useRouteHandle } from '../bind/useRouteHandle';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { useRunController } from '../bind/useRunController';
import { selectActiveSheetId } from '../state/routeStore';
import {
  actionLaunchForRoute, toggleProvenanceWithDrawersTransition, confirmDeleteRowsTransition,
} from '../state/workspaceTransitions';
import { routePath, type RoutePanel } from '../routes';
import type { ActMenuCommand } from '../workbench/actSurface';
import { GridWorkbenchViewFrame } from '../workbench/contributions';
import { sheetDataRequirementContext, type WorkbenchDataRequirementContext } from '../workbench/dataRequirements';
import { isProjectionViewDescriptor, type WorkbenchViewDescriptor } from '../workbench/descriptors';
import { projectHostIdentity, type WorkbenchHostContext } from '../workbench/hostContext';
import type { WorkbenchResolvedLayoutContribution } from '../workbench/layout';
import { pluginViewDescriptorsFromRuntimeIndex } from '../workbench/pluginRuntimeDescriptors';
import { GRAPH_NEIGHBORHOOD_CONTRIBUTION_ID } from './contributionIds';
import { AddColumnPopover } from './popovers';
import { LazySheetGrid } from './lazyGridView';
import { useAskNavigation } from './useAskNavigation';
import { useWorkbenchContributionVisibility } from './useWorkbenchContributionVisibility';
import { useWorkspaceChromeState } from './useWorkspaceChromeState';
import { useReplayReviewController } from './useReplayReviewController';
import { useWorkspaceActionCompareController } from './useWorkspaceActionCompareController';
import { useWorkspaceActionController } from './useWorkspaceActionController';
import { useWorkspaceGridController } from './useWorkspaceGridController';
import { useWorkspaceSavedViewsController } from './useWorkspaceSavedViewsController';
import { useWorkspaceViewController } from './useWorkspaceViewController';
import { useWorkspaceContributionController } from './useWorkspaceContributionController';
import { useWorkspaceNavigationController } from './useWorkspaceNavigationController';

const EMPTY_ROW_CACHE_SNAPSHOT = { key: '', version: 0, totalRows: null };
const subscribeNoop = () => () => undefined;
const getEmptyRowCacheSnapshot = () => EMPTY_ROW_CACHE_SNAPSHOT;

export function useWorkspaceModel({
  project,
  routeSheetId,
  routeActionKind: externalActionKind,
  routeReview: externalReview,
  routePanel: externalPanel,
}: {
  project: ProjectInfo;
  routeSheetId?: string;
  routeActionKind?: string;
  routeReview?: boolean;
  routePanel?: RoutePanel;
}) {
  const projectData = useProjectDataResource();
  const job = useJobsHandle();
  const { projectApi } = useWorkspaceStores();
  const sheets = useSelector(projectData.store, (state) => state.sheets);
  const sheetsLoaded = useSelector(projectData.store, (state) => state.sheetsLoaded);
  const dataVersion = useSelector(projectData.store, (state) => state.dataVersion);
  const history = useSelector(projectData.store, (state) => state.history);
  const reviewCount = useSelector(projectData.store, (state) => state.reviewCount);
  const run = useSelector(job.store, (state) => state.run);
  const requestCostConfirmation = job.requestCostConfirmation;

  const gridView = useGridViewHandle();
  const rowCache = useRowCacheHandle();
  const selection = useSelectionHandle();
  const detail = useDetailHandle();
  const savedViews = useSavedViewsHandle();
  const chrome = useChromeHandle();
  // Navigation controller owns route projection and the sole history writer.
  const route = useRouteHandle();
  const activeSheetId = useSelector(route.store, selectActiveSheetId);
  const effectiveRoute = useSelector(route.store, (state) => state);
  const routeActionKind = effectiveRoute.actionKind ?? undefined;
  const routePanel = effectiveRoute.panel ?? undefined;

  useEffect(() => {
    projectApi
      .listColumnTypes()
      .then(applyColumnTypeRegistry)
      .catch(() => undefined);
  }, [project.id, projectApi]);

  const provenanceOpen = useSelector(chrome.store, (s) => s.provenanceOpen);
  const overflowMenuOpen = useSelector(chrome.store, (s) => s.overflowMenuOpen);

  const selectedColumnId = useSelector(selection.store, (s) => s.selectedColumnId);
  const selectedRows = useSelector(selection.store, (s) => s.selectedRows);

  const rowDrawer = useSelector(detail.store, (s) => s.rowDrawer);
  const columnDrawer = useSelector(detail.store, (s) => s.columnDrawer);
  const headerMenu = useSelector(detail.store, (s) => s.headerMenu);

  const chromeState = useWorkspaceChromeState(project.id);
  const {
    clearDeleteRowsConfirm,
    closeAsk,
    askOpen,
    closeEvidenceViewer,
    commandPaletteOpen,
    deleteRowsConfirm,
    error,
    evidenceViewerState,
    lastCommandAction,
    openActionPanel,
    openEvidenceViewer,
    projectionStatus,
    recordCommandAction,
    setActiveBottomDockTab,
    setActiveRibbonTab,
    setDeleteRowsConfirm,
    setProjectionStatus,
    setRibbonMode,
    showError,
    setDiscoverOpen,
    setDiscoverTab,
    openDiscover,
    setOpenSplit,
    documentView,
    setDocumentView,
    documentAnnotationPreferences,
    setSheetAnnotationToggles,
  } = chromeState;
  const askNavigationCommands = useMemo(() => ({
    openEvidenceViewer,
    setDocumentView,
    setOpenSplit,
  }), [openEvidenceViewer, setDocumentView, setOpenSplit]);
  const askNavigation = useAskNavigation(sheets, askNavigationCommands);
  const visibility = useWorkbenchContributionVisibility(project.id);
  const {
    hiddenContributionIds,
    hideContribution,
  } = visibility;
  const pluginLayout = usePluginLayoutHandle();
  const workbenchPluginRuntimeIndex = useSelector(
    pluginLayout.store,
    (s) => s.workbenchPluginRuntimeIndex,
  );
  const actionCatalog = useActionCatalogHandle();
  const catalogActionTemplates = useSelector(
    actionCatalog.store,
    (state) => state.resolvedTemplates,
  );
  const actActionTemplates = catalogActionTemplates;
  const actSurface = useActSurfaceHandle();
  const actionLaunch = useSelector(actSurface.store, (s) => s.actionLaunch);
  const actionLaunchId = useSelector(actSurface.store, (s) => s.actionLaunchId);
  const routeActionLaunch = actionLaunchForRoute(actionLaunch, routeActionKind);
  const actionLaunchInitial = routeActionLaunch?.initial;
  const actionDraftLaunchId = routeActionLaunch ? actionLaunchId : 0;
  const addColumnPrompt = useSelector(actSurface.store, (s) => s.addColumnPrompt);
  useEffect(() => {
    void actionCatalog.start();
  }, [actionCatalog, project.id]);

  const refreshSheets = useCallback(
    () =>
      projectData
        .refresh('sheets')
        .then(() => {
          const normalized = projectData.normalize(route.baseRoute());
          if (normalized) route.normalize(normalized);
        })
        .catch((e: Error) => showError(e.message)),
    [projectData, route, showError],
  );
  const refreshHistory = useCallback(
    () =>
      projectData
        .refresh('history')
        .catch((e: Error) => showError(e.message)),
    [projectData, showError],
  );
  const refreshReviewCount = useCallback(
    () =>
      projectData
        .refresh('reviewCount')
        .catch((e: Error) => showError(e.message)),
    [projectData, showError],
  );
  const pluginManagerOperations = useMemo(
    () => ({
      installLocalWorkbenchPlugin: projectApi.installLocalWorkbenchPlugin.bind(projectApi),
      activateWorkbenchPlugin: projectApi.activateWorkbenchPlugin.bind(projectApi),
      activateWorkbenchPluginBackend: projectApi.activateWorkbenchPluginBackend.bind(projectApi),
      disableWorkbenchPlugin: projectApi.disableWorkbenchPlugin.bind(projectApi),
      uninstallWorkbenchPlugin: projectApi.uninstallWorkbenchPlugin.bind(projectApi),
      onRefresh: pluginLayout.invalidate,
    }),
    [pluginLayout, projectApi],
  );
  useEffect(() => {
    const initial = projectData.start();
    void initial.sheets
      .then(() => {
        const normalized = projectData.normalize(route.baseRoute());
        if (normalized) route.normalize(normalized);
      })
      .catch((e: Error) => showError(e.message));
    void initial.history.catch((e: Error) => showError(e.message));
    void initial.reviewCount.catch((e: Error) => showError(e.message));
    void pluginLayout.start();
  }, [
    pluginLayout,
    projectData,
    project.id,
    route,
    showError,
  ]);

  const sheet = sheets.find((candidate) => candidate.id === activeSheetId);
  const handleImportSessionChanged = useCallback(({ refreshData }: ImportProgressChange) => {
    if (refreshData) projectData.invalidate();
    void refreshSheets();
    void job.refresh();
  }, [job, projectData, refreshSheets]);
  const importProgress = useImportProgress({
    projectId: project.id,
    onChanged: handleImportSessionChanged,
    onError: showError,
  });
  const activeImportActivity = sheet ? preferredImportActivity(
    importProgress.activities.filter((activity) => activity.session.sheet_id === Number(sheet.id)),
  ) : null;
  const importLiveRun = activeImportActivity?.liveRun ?? null;
  const pendingImportRowCount = selectPendingImportRowCount(activeImportActivity);
  const importRowRefreshKey = activeImportActivity
    ? `${activeImportActivity.importRef}:${activeImportActivity.session.committed_rows}`
    : null;
  const [importRunRowScope, setImportRunRowScope] = useState<{
    runId: string;
    rowIds: string[];
  } | null>(null);
  useEffect(() => {
    const runId = importLiveRun?.actionKind === 'media.ocr' ? importLiveRun.runId : null;
    if (runId === null) return undefined;
    let alive = true;
    void projectApi.getRunRows(runId, 0, 500)
      .then((page) => {
        if (alive) setImportRunRowScope({ runId, rowIds: page.rows.map((row) => row.rowId) });
      })
      .catch(() => {
        if (alive) setImportRunRowScope({ runId, rowIds: [] });
      });
    return () => {
      alive = false;
    };
  }, [importLiveRun?.actionKind, importLiveRun?.runId, projectApi]);
  const selectBottomDockTab = useCallback(
    (contribution: WorkbenchResolvedLayoutContribution) => {
      setActiveBottomDockTab(contribution.placementId);
      if (contribution.contributionId === 'frisket.core.panel.errors') {
        void pluginLayout.refresh();
      }
    },
    [pluginLayout, setActiveBottomDockTab],
  );
  const grid = useWorkspaceGridController({
    project, sheet, chromeState, refreshHistory, refreshSheets, showError,
  });
  const {
    activeGridFilter, activeGridSort, scopeRowIds, rowHeight, wrapText, columnGroupsVersion,
    activeColumnOrder, activeHiddenColumns, hiddenColumnNames, currentViewColumns,
    activeChildFilter, activeFrozenColumnCount, changeColumnOrder, revealColumns,
    applyGridFilterForColumn, applyGridBboxFilter, applyGridFilterSpec, applyGridFailedFilter,
    applyGridEntityFilter, sortHeaderColumn, clearGridFilter, openGridHeaderMenu, closeHeaderMenu,
    openAddColumnPrompt, closeAddColumnPrompt, submitAddColumn,
  } = grid;
  const mapViewDescriptor = useMemo<WorkbenchViewDescriptor | null>(
    () =>
      pluginViewDescriptorsFromRuntimeIndex(workbenchPluginRuntimeIndex).find(
        (descriptor) =>
          isProjectionViewDescriptor(descriptor) &&
          descriptor.placements.some(
            (placement) => placement.host === 'mainView' && placement.mode === 'pane',
          ) &&
          (descriptor.dataRequirements ?? []).some(
            (requirement) =>
              requirement.kind === 'sheetHasColumnType' &&
              requirement.columnType === 'geo_point' &&
              !requirement.optional,
          ),
      ) ?? null,
    [workbenchPluginRuntimeIndex],
  );
  const mapContributionId = mapViewDescriptor?.id ?? null;

  useEffect(
    () =>
      subscribeMapPointsMeta((meta) => {
        if (!sheet || meta.sheetId !== sheet.id || !mapContributionId) return;
        const metaColumn = sheet.columns.find(
          (column) => String(column.id) === String(meta.columnId),
        );
        setProjectionStatus({
          activeContributionId: mapContributionId,
          sheetId: meta.sheetId,
          columnId: meta.columnId,
          columnName: metaColumn?.name ?? String(meta.columnId),
          count: meta.validPoints,
          validPoints: meta.validPoints,
          transient: meta.transient,
          generation: meta.generation,
          schema: meta.schema,
          backend: meta.backend,
        });
      }),
    [mapContributionId, setProjectionStatus, sheet],
  );

  const navigation = useWorkspaceNavigationController({
    project, sheet, sheets,
    externalRoute: { routeSheetId, routeActionKind: externalActionKind, routeReview: externalReview, routePanel: externalPanel },
    chromeState, visibility, grid, mapContributionId, actActionTemplates, importLiveRun, showError,
  });
  const {
    selectSheet, onMainViewTabKeyDown, workspaceRoute, runActionFromSurface,
    openSourcesFromCommandPalette, openSettingsFromCommandPalette, openRowPanel, openColumnPanel,
    walkDetailRow, openMapPanel, openGraphPanel, openMapSplit, openSourceHealthMainView,
    openEvidenceViewerForWorkspace, activeDocumentPriority, openRowInDocumentView, openDocumentRef, openRowRef,
    closeRoutePanel, closeActionRoute, closeActionDrawerOnLaunch, actionPanelVisible,
    openImportDialog,
  } = navigation;
  const { startProposal, startRun } = useRunController({
    invalidateProjectData: projectData.invalidate,
    refreshHistory,
    refreshReviewCount,
    refreshSheets,
    sheet,
    showError,
    onLaunchAccepted: closeActionDrawerOnLaunch,
    onMaterializedSheetCreated: selectSheet,
  });

  const selectedRowIdsForSheet = useMemo(
    () => (sheet && selectedRows.sheetId === sheet.id ? selectedRows.rowIds : []),
    [sheet, selectedRows.sheetId, selectedRows.rowIds],
  );
  const afterHistoryChange = useCallback(
    (h: HistoryState) => {
      projectData.commitHistoryChange(h);
      detail.clearRowDrawer();
      void refreshSheets();
    },
    [detail, projectData, refreshSheets],
  );

  const doUndo = useCallback(() => { void projectApi.undo().then(afterHistoryChange); }, [afterHistoryChange, projectApi]);
  const doRedo = useCallback(() => { void projectApi.redo().then(afterHistoryChange); }, [afterHistoryChange, projectApi]);
  const doStepTo = useCallback(
    (i: number) => { void projectApi.stepTo(i).then(afterHistoryChange); },
    [afterHistoryChange, projectApi],
  );
  const loadHistoryPage = useCallback(
    (offset: number, limit: number) =>
      projectData
        .refresh('history', { offset, limit })
        .catch((e: Error) => showError(e.message)),
    [projectData, showError],
  );

  const handleCellEdit = useCallback(
    async (row: Row, col: ColumnDef, value: CellValue) => {
      try {
        await projectApi.editCells([{ rowId: row.id, columnId: col.id, value }]);
        projectData.invalidate();
        void refreshHistory();
      } catch (e: unknown) {
        showError(e instanceof Error ? e.message : String(e));
        throw e;
      }
    },
    [projectApi, projectData, refreshHistory, showError],
  );

  const appendRow = useCallback(() => {
    if (!sheet) return;
    void projectApi
      .addRow(sheet.id)
      .then(() => {
        projectData.invalidate();
        detail.closeDrawers({ clearChildFilter: true });
        void refreshSheets();
        void refreshHistory();
      })
      .catch((e: Error) => showError(e.message));
  }, [detail, projectApi, projectData, refreshHistory, refreshSheets, sheet, showError]);

  const requestDeleteRows = useCallback(() => {
    if (!sheet) return;
    if (selectedRowIdsForSheet.length === 0) return;
    setDeleteRowsConfirm({ sheetId: sheet.id, rowIds: [...selectedRowIdsForSheet] });
  }, [selectedRowIdsForSheet, setDeleteRowsConfirm, sheet]);

  const confirmDeleteRows = useCallback(() => {
    if (!deleteRowsConfirm) return;
    const { sheetId, rowIds } = deleteRowsConfirm;
    clearDeleteRowsConfirm();
    void projectApi
      .deleteRows(sheetId, rowIds)
      .then(() => {
        projectData.invalidate();

        confirmDeleteRowsTransition({ selection, detail }, sheetId);
        void refreshSheets();
        void refreshHistory();
      })
      .catch((e: Error) => showError(e.message));
  }, [clearDeleteRowsConfirm, deleteRowsConfirm, detail, projectApi, projectData, refreshHistory, refreshSheets, selection, showError]);

  const savedViewController = useWorkspaceSavedViewsController({
    project, sheet, currentViewColumns, chromeState, visibility, showError,
  });
  const {
    openNewSavedView,
  } = savedViewController;

  // Block on a stale or incomplete runtime index.

  const actions = useWorkspaceActionController({ sheet, closeRoutePanel, startRun, showError });
  const {
    activeLensView, activePreviewView, applyLens, closePreviewView, runWithPreview,
    executeRegisteredAction, runActionBackfill, resumeHaltedRun, retryFailedRows,
  } = actions;

  const openHeaderMenuColumnSettings = useCallback(() => {
    if (!headerMenu) return;
    openColumnPanel(headerMenu.column);
    closeHeaderMenu();
  }, [closeHeaderMenu, headerMenu, openColumnPanel]);

  const openHeaderMenuSaveView = useCallback(() => {
    if (!headerMenu) return;

    openNewSavedView(`${headerMenu.column.name} view`);
    closeHeaderMenu();
  }, [closeHeaderMenu, headerMenu, openNewSavedView]);

  const afterColumnUpdated = useCallback(
    (updated: ColumnDef) => {
      detail.mergeColumnUpdate(updated);
      projectData.mergeColumnUpdate(updated);
      void refreshSheets();
      void refreshHistory();
    },
    [detail, projectData, refreshHistory, refreshSheets],
  );

  const onImported = useCallback(
    (sheetId: number) => {
      const targetSheetId = String(sheetId);
      void projectData
        .refresh('sheets')
        .then(() => {
          if (projectData.has(targetSheetId)) {
            route.navigate({
              ...route.store.get(),
              sheetId: targetSheetId,
              actionKind: null,
              review: false,
              panel: null,
            });
          } else {
            const normalized = projectData.normalize(route.baseRoute());
            if (normalized) route.normalize(normalized);
          }
        })
        .catch((error: Error) => showError(error.message));
      void refreshHistory();
    },
    [
      projectData,
      refreshHistory,
      route,
      showError,
    ],
  );

  const childSheet = sheet
    ? sheets.find((s) => s.parent?.sheetId === sheet.id) ?? null
    : null;
  const view = useWorkspaceViewController({
    sheet, chromeState, visibility, mapViewDescriptor, selectSheet, openMapPanel, openGraphPanel,
  });
  const {
    activePromotedView, openPromotedTab, closePromotedTab, selectSheetTab, openOcrCompareTab,
    openTranscribeCompareTab, openTranslateCompareTab, openTopicCompareTab,
  } = view;

  const unusedCommandHandleStore = useMemo(() => createStore<unknown>(null), []);
  const commandContext = useMemo<CommandContext>(
    () => ({
      route: {
        store: unusedCommandHandleStore,
        openSheet: selectSheet,
        openRow: (sheetId, rowId) => openRowRef(sheetId, rowId),
        openColumn: (columnId) => {
          const column = sheet?.columns.find((item) => item.id === columnId);
          if (column) openColumnPanel(column);
        },
        openSource: (sourceId) => openSourceHealthMainView(Number(sourceId)),
        openActionRoute: (actionKind) => {
          if (actionKind) {
            runActionFromSurface(actionKind);
          } else {
            openActionPanel();
          }
        },
      },
      grid: {

        store: gridView.store as Store<unknown>,
      },
      chrome: {
        store: unusedCommandHandleStore,
        openEvidence: (linkId, host) => openEvidenceViewerForWorkspace(linkId, host),
        openMap: (columnId) => {
          const column = columnId
            ? sheet?.columns.find((item) => item.id === columnId)
            : sheet?.columns.find((item) => item.type === 'geo_point');
          if (column) openMapPanel(column);
        },
        openGraph: () => openGraphPanel(),
        openMainViewContribution: (contributionId) => {
          if (mapContributionId && contributionId === mapContributionId) {
            openMapSplit();
          }
          if (contributionId === GRAPH_NEIGHBORHOOD_CONTRIBUTION_ID) openGraphPanel();
        },
        openSources: openSourcesFromCommandPalette,
        openSettings: openSettingsFromCommandPalette,
      },
      scratch: {
        store: unusedCommandHandleStore,
        openOcrCompare: () => {
          openOcrCompareTab();
          recordCommandAction('Opened OCR Compare');
        },
        openTranscribeCompare: () => {
          openTranscribeCompareTab();
          recordCommandAction('Opened Transcribe Compare');
        },
        openTranslateCompare: () => {
          openTranslateCompareTab();
          recordCommandAction('Opened Translate Compare');
        },
        openTopicCompare: () => {
          openTopicCompareTab();
          recordCommandAction('Opened Topic Compare');
        },
      },
      actSurface: {
        store: unusedCommandHandleStore,
        openImportDialog,
        setExportModal: actSurface.setActExportModal,
      },
      jobs: { store: unusedCommandHandleStore },
      selection: { store: unusedCommandHandleStore },
    }),
    [
      actSurface,
      gridView,
      mapContributionId,
      openActionPanel,
      openColumnPanel,
      openEvidenceViewerForWorkspace,
      openGraphPanel,
      openImportDialog,
      openMapPanel,
      openMapSplit,
      openOcrCompareTab,
      openRowRef,
      openSettingsFromCommandPalette,
      openSourceHealthMainView,
      openSourcesFromCommandPalette,
      openTranscribeCompareTab,
      openTranslateCompareTab,
      openTopicCompareTab,
      recordCommandAction,
      runActionFromSurface,
      selectSheet,
      sheet,
      unusedCommandHandleStore,
    ],
  );
  const dispatchCommand = useCommand(commandContext);

  const workbenchHostContext = useMemo<WorkbenchHostContext>(() => ({
    identity: projectHostIdentity(project, sheet?.id ?? null, routePath(workspaceRoute())),
    selection: {
      selectedRowIds: selectedRowIdsForSheet.map(String),
      activeRowId: routePanel?.kind === 'row' ? routePanel.rowId : null,
      activeCell: null,
      activeColumnId: routePanel?.kind === 'column' ? routePanel.columnId : null,
      activeSourceId: routePanel?.kind === 'sourceHealth' ? routePanel.sourceId : null,
      activeEntityId: null,
      activeEvidenceLinkId: evidenceViewerState?.linkId ?? null,
    },
    gridState: {
      filter: activeGridFilter,
      sort: activeGridSort,
      lensId: activeLensView?.lensId ?? null,
      columnOrder: activeColumnOrder,
      frozenColumns: activeFrozenColumnCount,
      dataVersion,
    },
    navigation: {
      openSheet: (sheetId) => dispatchCommand({ type: 'openSheet', sheetId }),
      openRow: (sheetId, rowId) =>
        dispatchCommand({ type: 'openRow', sheetId: String(sheetId), rowId: String(rowId) }),
      openColumn: (columnId) => dispatchCommand({ type: 'openColumn', columnId }),
      openSource: (sourceId) => dispatchCommand({ type: 'openSource', sourceId: String(sourceId) }),
      openEvidence: (evidenceLinkId) =>
        dispatchCommand({ type: 'openEvidence', linkId: evidenceLinkId }),
      openMap: (columnId) => dispatchCommand({ type: 'openMap', columnId }),
      openGraph: () => dispatchCommand({ type: 'openGraph' }),
      openActionRoute: (actionKind) => dispatchCommand({ type: 'openActionRoute', actionKind }),
      openMainViewContribution: (contributionId) =>
        dispatchCommand({ type: 'openMainViewContribution', contributionId }),
    },
    gridFilter: {
      applySpec: applyGridFilterSpec,
      applyBbox: (columnId, bbox) => {
        const column = sheet?.columns.find((item) => item.id === columnId);
        if (column) applyGridBboxFilter(column.name, bbox);
      },
      applyValue: (columnId, value, operator = 'eq') => {
        const column = sheet?.columns.find((item) => item.id === columnId);
        if (column) applyGridFilterForColumn(column.name, operator, value);
      },
      applyEntity: (columnId, value, valueLabel) => {
        const column = sheet?.columns.find((item) => item.id === columnId);
        if (column) applyGridEntityFilter(column.name, value, valueLabel);
      },
      clear: () => clearGridFilter(),
    },
    actions: {
      runAction: async (actionKind) => {
        runActionFromSurface(actionKind);
      },
      previewAction: async (actionKind) => {
        runActionFromSurface(actionKind);
      },
      inspectProposal: () => {
        openActionPanel();
      },
      applyReviewDecision: async () => {
        await refreshReviewCount();
      },
      pollSource: async (sourceId) => {
        openSourceHealthMainView(Number(sourceId));
      },
    },
    invalidation: {

      refreshSheets: () => projectData.refresh('sheets', { markInventoryStale: false }),
      refreshHistory: () => projectData.refresh('history'),
      refreshReviewCount: () => projectData.refresh('reviewCount'),
      refreshGridRows: () => projectData.invalidate(),
    },
    policy: {

      confirmCost: (actionKind) =>
        requestCostConfirmation(
          { cost: null, rows: 0 },
          `A plugin requested launching "${actionKind}". Confirm to open the action panel prefilled — runs still require your confirmation there.`,
        ),

      confirmEgress: async () => false,
      preferStaleEvidence: false,
    },
    layoutProfile: {
      persistPersonalOverride: () => undefined,
      persistProjectProfile: async () => undefined,
      resetPersonalLayout: () => undefined,
      resetProjectProfile: async () => undefined,
      rewriteAlias: (alias) => alias,
    },
  }), [
    activeColumnOrder,
    activeFrozenColumnCount,
    activeGridFilter,
    activeGridSort,
    activeLensView?.lensId,
    applyGridBboxFilter,
    applyGridFilterSpec,
    applyGridEntityFilter,
    applyGridFilterForColumn,
    clearGridFilter,
    dataVersion,
    dispatchCommand,
    evidenceViewerState?.linkId,
    openActionPanel,
    openSourceHealthMainView,
    projectData,
    project,
    refreshReviewCount,
    requestCostConfirmation,
    routePanel,
    runActionFromSurface,
    selectedRowIdsForSheet,
    sheet,
    workspaceRoute,
  ]);

  const workbenchDataRequirementContext = useMemo<WorkbenchDataRequirementContext>(
    () => ({
      ...sheetDataRequirementContext(sheet ?? null),
      activeRow: workbenchHostContext.selection.activeRowId !== null,
      activeColumn: workbenchHostContext.selection.activeColumnId !== null,
      activeCell: workbenchHostContext.selection.activeCell !== null,
      activeEvidence: workbenchHostContext.selection.activeEvidenceLinkId !== null,
      activeSource: workbenchHostContext.selection.activeSourceId !== null,
      activeEntity: workbenchHostContext.selection.activeEntityId !== null,
      activeProjection: projectionStatus !== null,
      selectedRowIds: workbenchHostContext.selection.selectedRowIds,
    }),
    [sheet, workbenchHostContext, projectionStatus],
  );

  const rowPreview = activePreviewView?.status === 'done' && activePreviewView.result?.kind === 'row_overlay'
    ? activePreviewView.result : null;
  const activeGridLensRowIds =
    rowPreview
      ? rowPreview.rowIds
      : activeLensView?.rowIds ?? null;
  const activeRowCacheBaseCount =
    activeGridLensRowIds !== null
      ? activeGridLensRowIds.length
      : activeChildFilter
        ? activeChildFilter.count
        : sheet?.rowCount ?? 0;
  const activeRowCacheOptions = useMemo(
    () => ({
      ...resolveRowCacheScope({
        parentRowId: activeChildFilter?.parentRowId,
        filter: activeGridFilter,
        sort: activeGridSort,
        lensRowIds: activeGridLensRowIds,
        scopeRowIds,
      }),
      columnIds: sheet ? gridReadColumnIds(sheet.columns, hiddenColumnNames) : [],
      valueMode: 'bounded' as const,
    }),
    [activeChildFilter?.parentRowId, activeGridFilter, activeGridSort, activeGridLensRowIds, scopeRowIds, sheet, hiddenColumnNames],
  );
  const activeRowCacheKey = sheet
    ? computeRowCacheKey(sheet.id, dataVersion, activeRowCacheOptions)
    : '';
  const activeRowCacheSheetId = sheet?.id ?? null;
  const activeRowCacheSlot = useMemo(
    () => (activeRowCacheSheetId ? rowCache.getSlot(activeRowCacheSheetId) : null),
    [rowCache, activeRowCacheSheetId],
  );
  const activeRowCacheSnapshot = useSyncExternalStore(
    activeRowCacheSlot ? activeRowCacheSlot.subscribe : subscribeNoop,
    activeRowCacheSlot ? activeRowCacheSlot.getSnapshot : getEmptyRowCacheSnapshot,
    activeRowCacheSlot ? activeRowCacheSlot.getSnapshot : getEmptyRowCacheSnapshot,
  );
  const visibleRowCount =
    activeRowCacheSnapshot.key === activeRowCacheKey &&
    activeRowCacheSnapshot.totalRows !== null
      ? activeRowCacheSnapshot.totalRows
      : activeRowCacheBaseCount;

  const selectedColumnName = useMemo(() => {
    if (!sheet || !selectedColumnId) return null;
    return sheet.columns.find((column) => column.id === selectedColumnId)?.name ?? null;
  }, [sheet, selectedColumnId]);
  const launchActionFromSurface = useCallback(
    (actionKind: string) => {
      runActionFromSurface(actionKind, selectedColumnName ?? undefined);
    },
    [runActionFromSurface, selectedColumnName],
  );
  const runActCommand = useCallback(
    (command: ActMenuCommand) => {
      switch (command) {
        case 'import':
          dispatchCommand({ type: 'import.open' });
          return;
        case 'sources':
          dispatchCommand({ type: 'sources.open' });
          return;
        case 'export-csv':
          dispatchCommand({ type: 'export.open', kind: 'dataset' });
          return;
        case 'export-google-sheets':
          dispatchCommand({ type: 'export.open', kind: 'google_sheets' });
          return;
        case 'export-column-tables':
          dispatchCommand({ type: 'export.open', kind: 'column_tables' });
          return;
        case 'ocr-compare':
          dispatchCommand({ type: 'ocrCompare.open' });
          return;
        case 'transcribe-compare':
          dispatchCommand({ type: 'transcribeCompare.open' });
          return;
        case 'translate-compare':
          dispatchCommand({ type: 'translateCompare.open' });
          return;
        case 'topic-compare':
          dispatchCommand({ type: 'topicCompare.open' });
          return;
      }
    },
    [dispatchCommand],
  );

  const contributions = useWorkspaceContributionController({
    sheet, actActionTemplates, commandContext, workbenchHostContext,
    workbenchDataRequirementContext, chromeState, visibility, view, mapContributionId,
  });
  const {
    actRibbonTabs, rightInspectorPluginPanelDescriptors, bottomDockPluginPanelDescriptors,
    leftSidebarPluginPanelDescriptors, renderPluginDetailTab, resolvedWorkbenchLayout,
    revealPluginLauncher,
  } = contributions;

  const rowDrawerOpen = rowDrawer !== null;

  const { replayColumnAnnotations, replayReviewPopover } = useReplayReviewController({
    sheet, refreshHistory, refreshSheets, showError,
  });
  const comparisonSampleOptions = useMemo(() => ({
    filter: activeGridFilter, sort: activeGridSort, scopeRowIds,
    ...(activeGridLensRowIds ? { rowIds: activeGridLensRowIds } : {}),
    ...(activeChildFilter ? { parentRowId: activeChildFilter.parentRowId } : {}),
  }), [activeGridFilter, activeGridSort, scopeRowIds, activeGridLensRowIds, activeChildFilter]);
  const actionCompare = useWorkspaceActionCompareController({
    projectId: project.id, sheetId: sheet?.id, selectedRowIds: selectedRowIdsForSheet,
    selectedRowCount: selectedRows.sheetId === sheet?.id
      ? selectedRows.rowIndexes.length || selectedRowIdsForSheet.length : 0,
    sampleOptions: comparisonSampleOptions, runActionFromSurface, closeActionRoute, setOpenSplit, showError,
  });

  const gridContribution = useMemo(() => activePreviewView?.result?.kind === 'table'
    ? <PreviewTable result={activePreviewView.result} /> : sheet ? (
    <GridWorkbenchViewFrame>
      <Suspense fallback={<PanelLoading className="grid-host" label="Loading grid…" />}>
        <LazySheetGrid
          sheet={sheet}
          dataVersion={dataVersion}
          liveRun={run}
          backgroundLiveRun={importLiveRun?.actionKind === 'media.ocr' ? {
            ...importLiveRun,
            targetRowIds: importRunRowScope?.runId === importLiveRun.runId
              ? importRunRowScope.rowIds
              : [],
          } : importLiveRun}
          priorityReadingRowId={activeDocumentPriority?.sheetId === sheet.id ? activeDocumentPriority.rowId : null}
          pendingImportRowCount={pendingImportRowCount}
          importRowRefreshKey={importRowRefreshKey}
          rowHeight={rowHeight}
          wrapText={wrapText}
          columnGroupsStorageScope={project.id}
          columnGroupsVersion={columnGroupsVersion}
          hiddenColumnNames={hiddenColumnNames}
          userHiddenColumnNames={activeHiddenColumns}
          onRevealColumns={revealColumns}
          onAddColumnAtEnd={openAddColumnPrompt}
          childSheet={childSheet}
          parentRowFilter={
            activeChildFilter
              ? { parentRowId: activeChildFilter.parentRowId, count: activeChildFilter.count }
              : null
          }
          rowDrawerOpen={rowDrawerOpen}
          frozenColumnCount={activeFrozenColumnCount}
          scopeRowIds={scopeRowIds}
          activeFilter={activeGridFilter}
          activeSort={activeGridSort}
          columnOrder={activeColumnOrder}
          lensRowIds={activeGridLensRowIds}
          lensScores={activeLensView?.scores ?? null}
          previewColumns={
            rowPreview?.columns ?? null
          }
          previewCells={
            rowPreview?.rows ?? null
          }
          onRowOpen={(row, col, preview) => {
            openRowPanel(row, col, preview);
          }}
          loadActionRunEntry={actionCompare.loadActionRunEntry}
          onCompareActionRun={actionCompare.compareActionRun}
          onEditActionRun={actionCompare.editActionRun}
          onColumnOpen={openColumnPanel}
          onHeaderSort={sortHeaderColumn}
          onColumnHeaderMenu={openGridHeaderMenu}
          onColumnGroupsChange={(columnGroupSpecs) => {
            gridView.setColumnGroupSpecs(columnGroupSpecs);
          }}
          onSelectedRowsChange={(selectedRows) => {
            selection.setSelectedRows(selectedRows);
          }}
          onColumnOrderChange={changeColumnOrder}
          onCellEdit={handleCellEdit}
          onFacetValueFilter={(column, value, operator) => {
            applyGridFilterForColumn(column.name, operator, value);
          }}
          rowCacheStore={rowCache}
          columnAnnotations={replayColumnAnnotations}
        />
      </Suspense>
      {addColumnPrompt && (
        <AddColumnPopover
          anchor={addColumnPrompt.anchor}
          existingNames={sheet.columns.map((column) => column.name)}
          onSubmit={submitAddColumn}
          onClose={closeAddColumnPrompt}
        />
      )}
      {replayReviewPopover}
    </GridWorkbenchViewFrame>
  ) : null, [
    actionCompare,
    scopeRowIds,
    sheet,
    dataVersion,
    run,
    importLiveRun,
    importRunRowScope,
    pendingImportRowCount,
    importRowRefreshKey,
    activeDocumentPriority,
    rowHeight,
    wrapText,
    project.id,
    columnGroupsVersion,
    hiddenColumnNames,
    activeHiddenColumns,
    revealColumns,
    openAddColumnPrompt,
    childSheet,
    activeChildFilter,
    rowDrawerOpen,
    activeFrozenColumnCount,
    activeGridFilter,
    activeGridSort,
    activeColumnOrder,
    activeGridLensRowIds,
    activePreviewView,
    activeLensView,
    rowPreview,
    openRowPanel,
    openColumnPanel,
    sortHeaderColumn,
    openGridHeaderMenu,
    gridView,
    selection,
    rowCache,
    changeColumnOrder,
    handleCellEdit,
    applyGridFilterForColumn,
    addColumnPrompt,
    submitAddColumn,
    closeAddColumnPrompt,
    replayColumnAnnotations,
    replayReviewPopover,
  ]);

  // Keep clearChildFilter in parity coverage.

  const toggleProvenanceWithDrawers = useCallback(
    () => toggleProvenanceWithDrawersTransition({ detail, chrome }),
    [detail, chrome],
  );
  const clearChildFilter = useCallback(() => detail.setChildFilter(null), [detail]);

  const mainViewModel = useMemo(
    () => ({
      ...grid,
      ...actionCompare,
      ...savedViewController,
      ...view,
      ...actions,
      ...navigation.mainView,
      ...contributions.mainView,
      routePanel,
      sheet,
      evidenceViewerState,
      mapContributionId,
      refreshHistory,
      refreshSheets,
      invalidateProjectData: projectData.invalidate,
      appendRow,
      requestDeleteRows,
      selectedRowIdsForSheet,
      visibleRowCount,
      overflowMenuOpen,
      setOverflowMenuOpen: chrome.setOverflowMenuOpen,
      provenanceOpen,
      toggleProvenanceWithDrawers,
      consumeSavedViewsPublication: savedViews.consumePublication,
      cancelSavedViewsConfirmation: savedViews.cancelConfirmation,
      cancelSavedViewEditor: savedViews.cancelEditor,
      setViewName: savedViews.setViewName,
      startEditingView: savedViews.startEditingView,
      clearChildFilter,
      closeEvidenceViewer,
      documentView,
      documentAnnotationPreferences,
      setSheetAnnotationToggles,
      gridContribution,
      hiddenContributionIds,
      mapViewDescriptor,
      project,
      selectedRowCountForSheet: selectedRowIdsForSheet.length,
      setDocumentView,
      workbenchHostContext,
      actActionTemplates,
      openHeaderMenuColumnSettings,
      openHeaderMenuSaveView,
      startRun,
      onImported,
      showError,
    }),
    [
      grid,
      savedViewController,
      actionCompare,
      view,
      actions,
      navigation.mainView,
      contributions.mainView,
      routePanel,
      sheet,
      documentAnnotationPreferences,
      setSheetAnnotationToggles,
      evidenceViewerState,
      mapContributionId,
      refreshHistory,
      refreshSheets,
      projectData,
      appendRow,
      requestDeleteRows,
      selectedRowIdsForSheet,
      visibleRowCount,
      overflowMenuOpen,
      chrome,
      provenanceOpen,
      toggleProvenanceWithDrawers,
      savedViews,
      clearChildFilter,
      closeEvidenceViewer,
      documentView,
      gridContribution,
      hiddenContributionIds,
      mapViewDescriptor,
      project,
      setDocumentView,
      workbenchHostContext,
      actActionTemplates,
      openHeaderMenuColumnSettings,
      openHeaderMenuSaveView,
      startRun,
      onImported,
      showError,
    ],
  );
  const overlayModel = useMemo(
    () => ({
      ...navigation.overlays,
      ...contributions.overlays,
      afterColumnUpdated,
      applyGridFailedFilter,
      retryFailedRows,
      clearDeleteRowsConfirm,
      closeEvidenceViewer,
      columnDrawer,
      commandPaletteOpen,
      closeRowDrawer: detail.clearRowDrawer,
      askOpen,
      closeAsk,
      sheets,
      startProposal,
      openActionPanel,
      confirmDeleteRows,
      deleteRowsConfirm,
      doRedo,
      doUndo,
      error,
      evidenceViewerState,
      history,
      lastCommandAction,
      project,
      provenanceOpen,
      closeProvenanceOpen: chrome.closeProvenanceOpen,
      reviewCount,
      setProposalInspect: detail.setProposalInspect,
      sheet,
      commitReviewDecision: projectData.commitReviewDecision,
      workbenchHostContext,
      importProgress,
    }),
    [
      navigation.overlays,
      contributions.overlays,
      afterColumnUpdated,
      applyGridFailedFilter,
      retryFailedRows,
      clearDeleteRowsConfirm,
      closeEvidenceViewer,
      columnDrawer,
      commandPaletteOpen,
      detail,
      askOpen,
      closeAsk,
      sheets,
      startProposal,
      openActionPanel,
      confirmDeleteRows,
      deleteRowsConfirm,
      doRedo,
      doUndo,
      error,
      evidenceViewerState,
      history,
      lastCommandAction,
      project,
      provenanceOpen,
      chrome,
      reviewCount,
      sheet,
      projectData,
      workbenchHostContext,
      importProgress,
    ],
  );

  return {
    askNavigation,
    project,
    projectApi,
    runActCommand,
    revealPluginLauncher,
    onImported,
    showError,
    setRibbonMode,
    setActiveRibbonTab,
    onMainViewTabKeyDown,
    closePreviewView,
    activePromotedView,
    openPromotedTab,
    closePromotedTab,
    selectSheetTab,
    selectSheet,
    refreshSheets,
    handleCellEdit,
    closeRoutePanel,
    openRowInDocumentView,
    openDocumentRef,
    applyLens,
    leftSidebarPluginPanelDescriptors,
    openEvidenceViewer: openEvidenceViewerForWorkspace,
    openRowRef,
    openSourceHealthMainView,
    refreshHistory,
    invalidateProjectData: projectData.invalidate,
    setDiscoverOpen,
    setDiscoverTab,
    openDiscover,
    rightInspectorPluginPanelDescriptors,
    actionPanelVisible,
    closeActionRoute,
    actionLaunchInitial,
    actionDraftLaunchId,
    runActionFromSurface,
    runActionBackfill,
    resumeHaltedRun,
    retryFailedRows,
    applyGridFailedFilter,
    runWithPreview,
    executeRegisteredAction,
    openOcrCompareTab,
    openActionCompare: actionCompare.openActionCompare,
    openActionCompareHistory: actionCompare.openActionCompareHistory,
    returnToActionCompare: actionCompare.returnToActionCompare,
    doStepTo,
    loadHistoryPage,
    pluginManagerOperations,
    selectBottomDockTab,
    bottomDockPluginPanelDescriptors,
    hideContribution,

    actRibbonTabs,
    launchActionFromSurface,
    renderPluginDetailTab,
    walkDetailRow,
    workbenchHostContext,
    resolvedWorkbenchLayout,

    mainViewModel,
    overlayModel,

    sheetsLoaded,
  };
}
